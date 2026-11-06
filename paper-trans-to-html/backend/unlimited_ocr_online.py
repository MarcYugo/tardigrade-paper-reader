"""Unlimited-OCR **在线解析**客户端（百度智能云「文档解析（Unlimited-OCR）」）。

与本地推理客户端 `./unlimited_ocr.py` 的关系
--------------------------------------------
* 本地：`./unlimited_ocr.py` → 仓库根 `unlimited_ocr_service/`（vLLM，逐页
  `chat/completions`，坐标 0–1000 归一化）。
* 在线：本模块 → 百度智能云**异步**接口（提交任务 → 轮询 → 下载结果 JSON）。
* `pdf_parser/backend_unlimited_ocr.py` 在**本地服务没有配置 / 未就绪**时自动切到在线链路
  （`UNLIMITED_OCR_ROUTE` = `auto`(默认) / `local` / `online` 可强制）。
  **网页设置面板不新增项**：在线凭据写在 `config.json` 的 `parser` 段
  （`unlimited_ocr_online_api_key` / `unlimited_ocr_online_secret_key`），
  也可用环境变量 `UNLIMITED_OCR_ONLINE_API_KEY` / `UNLIMITED_OCR_ONLINE_SECRET_KEY` 覆盖。
  凭据不齐全时在线链路直接判为不可用（不会静默失败）。

本模块的输出**刻意与本地客户端一致**（`pages[].items[]`，`bbox` 为 0–1000 归一化坐标），
所以 `backend_unlimited_ocr` 的版式转换与前端渲染路径（`DOC.parser === "unlimited_ocr"`）
完全不用分叉 —— 本地 / 在线只是「数据从哪来」不同。

在线接口（官方文档 https://cloud.baidu.com/doc/OCR/s/fmr1p39gb）
--------------------------------------------------------------
* 提交：POST `…/unlimited-ocr-parser/task?access_token=…`（`x-www-form-urlencoded`）
  参数 `file_data`（文件 base64，PDF ≤ 50MB / ≤ 500 页）或 `file_url`、`file_name`（必填）
  → `result.task_id`
* 查询：POST `…/unlimited-ocr-parser/task/query?access_token=…`，body `task_id`
  → `result.status`：`pending` / `running` / `success` / `failed`（+ `task_error` /
  `parse_result_url`，成功后 30 天内可下载）
* 结果 JSON 的关键字段（→ 本模块映射）：

    pages[].page_num                       0 基页码 → 本地结构的 `page`（1 基）
    pages[].meta.page_width/page_height    页面坐标空间 → 归一化时除以它（×1000）
    pages[].layouts[].position             `[x, y, w, h]` → 本地 bbox `[x0,y0,x1,y1]`（归一化）
    pages[].layouts[].type                 title/text/table/image/equation/header/…
                                           （kind 映射见 `backend_unlimited_ocr._KIND_BY_TYPE`）
    pages[].layouts[].sub_type             `title_1` / `title_2` … → 标题层级
    pages[].layouts[].merge_text           begin/inner/end：跨块拼接标记（仅统计）
    pages[].tables[].table_html            表格 HTML（按 `layout_id` 关联 layout）→ item.text
    pages[].images[].data_url              服务端裁好的图片外链（**不用**：见下）
    pages[].page_angle                     角度矫正的旋转角；坐标仍在**原页面坐标系**

⚠️ 插图**不下载 `data_url`**：按 bbox 从原 PDF 裁剪（与本地链路同一套裁图口径），
   图片随文档进库，不依赖 30 天有效期的外链。

⚠️ 失败一律抛 `UnlimitedOcrOnlineUnavailable`（由 `backend_unlimited_ocr` 包成
   `unlimited_ocr.UnlimitedOcrUnavailable` 抛给转换接口）。

坐标：`position` 是**页面坐标**（`meta.page_width/page_height` 为同一空间的长边），
本模块把它归一化成 0–1000 交出去；页面 `w/h` 用**原 PDF 页面的 pt 尺寸**、`dpi` 记 72，
于是 `backend_unlimited_ocr` 里 `pt = 归一化/1000 × 页面 pt 边长` 那把尺子都不用改。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "DEFAULT_API_KEY", "DEFAULT_SECRET_KEY", "DEFAULT_TIMEOUT", "POLL_INTERVAL",
    "TASK_URL", "QUERY_URL", "TOKEN_URL", "UnlimitedOcrOnlineUnavailable",
    "available", "get_access_token", "normalize_url", "ocr_pdf",
]

# ---- 百度智能云「文档解析（Unlimited-OCR）」固定地址 ----
TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
TASK_URL = ("https://aip.baidubce.com/rest/2.0/brain/online/v2/"
            "unlimited-ocr-parser/task")
QUERY_URL = TASK_URL + "/query"

# 凭据来自 `config.json` 的 `parser` 段（见 `pdf_parser/options.py::DEFAULT_OPTIONS`）：
#   unlimited_ocr_online_api_key / unlimited_ocr_online_secret_key
# 也可用环境变量覆盖（UNLIMITED_OCR_ONLINE_API_KEY / _SECRET_KEY）。
# 下面两个常量只在“直接调本模块、什么都没传”时的最终兑底，一律留空。
DEFAULT_API_KEY = ""
DEFAULT_SECRET_KEY = ""

# 单次 HTTP 超时（秒）：提交要上传整份 PDF，给足；查询/下载是轻请求
SUBMIT_TIMEOUT = 300.0
QUERY_TIMEOUT = 60.0
RESULT_TIMEOUT = 120.0
TOKEN_TIMEOUT = 30.0
# 轮询间隔（官方建议提交后 5–10 秒轮询一次；提交/查询 QPS 都是 2）
POLL_INTERVAL = 5.0
# 整份文档的等待上限（秒）：可用 config.json 的 unlimited_ocr_timeout 覆盖
DEFAULT_TIMEOUT = 900.0
# 服务端限制：file_data 方式 PDF ≤ 50MB（更大的只能走 file_url，这里不支持）
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# 展示用的「模型名」（在线接口没有 model 参数，写一个固定串给统计 / 界面）
ONLINE_MODEL = "Unlimited-OCR(online)"

# 在线回包里视为「插图」的类型（与本地客户端的 IMAGE_TYPES 对齐，另加印章）
IMAGE_TYPES = ("image", "chart", "figure", "diagram", "seal")

# access_token 缓存：{凭据指纹: (token, 过期时间戳)}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


class UnlimitedOcrOnlineUnavailable(RuntimeError):
    """在线 Unlimited-OCR 不可用 / 请求失败 / 任务失败 / 回包异常。"""


def normalize_url(url: str | None = None) -> str:
    """补全默认任务地址，并去掉结尾多余的 '/'。"""
    return (str(url or "").strip() or TASK_URL).rstrip("/")


# =====================================================================
#  凭据 / 可用性
# =====================================================================
def _credentials(options: dict | None = None) -> tuple[str, str]:
    """在线凭据：显式 options（= `config.json` 的 parser 段）> 环境变量 > 模块默认（空）。"""
    options = options or {}
    api = str(options.get("unlimited_ocr_online_api_key")
              or os.environ.get("UNLIMITED_OCR_ONLINE_API_KEY")
              or DEFAULT_API_KEY).strip()
    secret = str(options.get("unlimited_ocr_online_secret_key")
                 or os.environ.get("UNLIMITED_OCR_ONLINE_SECRET_KEY")
                 or DEFAULT_SECRET_KEY).strip()
    return api, secret


def available(options: dict | None = None) -> tuple[bool, str]:
    """在线链路是否**配置齐全**（不联网）。返回 `(可用, 说明)`。

    `options` 传 `pdf_parser.options.load_options()` 的结果（含 `config.json` 的
    `parser` 段）；不传时只看环境变量 / 模块默认 —— 所以 `status()` 必须把 opts 传进来。
    这里只判断凭据是否存在，真正的登录 / 额度错误会在解析时以
    `UnlimitedOcrOnlineUnavailable` 抛出。
    """
    api, secret = _credentials(options)
    if not api or not secret:
        return False, ("未配置在线解析凭据：请在 config.json 的 parser 段填 "
                       "unlimited_ocr_online_api_key / unlimited_ocr_online_secret_key"
                       "（或用环境变量 UNLIMITED_OCR_ONLINE_API_KEY / "
                       "UNLIMITED_OCR_ONLINE_SECRET_KEY）")
    return True, "在线解析可用（百度智能云「文档解析（Unlimited-OCR）」，按页计费）"


# =====================================================================
#  HTTP（只用标准库，和本地客户端同一套依赖习惯）
# =====================================================================
def _http_json(url: str, *, data: Optional[dict] = None,
               timeout: float = QUERY_TIMEOUT) -> dict:
    """GET（data=None）或 POST x-www-form-urlencoded → JSON dict。"""
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise UnlimitedOcrOnlineUnavailable(
            f"在线 Unlimited-OCR 请求失败 HTTP {exc.code}：{detail}") from exc
    except Exception as exc:
        raise UnlimitedOcrOnlineUnavailable(
            f"在线 Unlimited-OCR 请求失败（{url.split('?')[0]}）：{exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _check_envelope(payload: dict, what: str) -> dict:
    """校验百度通用回包（error_code / error_msg），返回 `result` 段。"""
    code = payload.get("error_code")
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        code = 0
    if code:
        msg = str(payload.get("error_msg") or "").strip() or "未知错误"
        raise UnlimitedOcrOnlineUnavailable(
            f"在线 Unlimited-OCR {what}失败（error_code={code}）：{msg}")
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def get_access_token(api_key: str | None = None, secret_key: str | None = None,
                     timeout: float = TOKEN_TIMEOUT) -> str:
    """取（并缓存）access_token（百度 OAuth，有效期约 30 天）。"""
    api, secret = _credentials()
    api = str(api_key or api).strip()
    secret = str(secret_key or secret).strip()
    if not api or not secret:
        raise UnlimitedOcrOnlineUnavailable(
            "在线 Unlimited-OCR 缺少 API Key / Secret Key（环境变量 "
            "UNLIMITED_OCR_ONLINE_API_KEY / UNLIMITED_OCR_ONLINE_SECRET_KEY）")
    fingerprint = api + ":" + secret[-6:]
    hit = _TOKEN_CACHE.get(fingerprint)
    if hit and hit[1] > time.time() + 60:            # 留 60 秒余量
        return hit[0]

    url = TOKEN_URL + "?" + urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": api,
        "client_secret": secret,
    })
    payload = _http_json(url, timeout=timeout)
    token = str(payload.get("access_token") or "").strip()
    if not token:
        err = payload.get("error_description") or payload.get("error") or payload
        raise UnlimitedOcrOnlineUnavailable(f"获取百度 access_token 失败：{err}")
    try:
        expires = float(payload.get("expires_in") or 2592000)
    except (TypeError, ValueError):
        expires = 2592000.0
    _TOKEN_CACHE[fingerprint] = (token, time.time() + expires)
    return token


def _with_token(url: str, token: str) -> str:
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode({"access_token": token})


# =====================================================================
#  异步任务：提交 → 轮询 → 下载结果
# =====================================================================
def submit_task(pdf_path: str | Path, *, api_key: str | None = None,
                secret_key: str | None = None, file_name: str | None = None,
                timeout: float = SUBMIT_TIMEOUT) -> str:
    """上传整份 PDF，返回 `task_id`。"""
    path = Path(pdf_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise UnlimitedOcrOnlineUnavailable(f"读取 PDF 失败：{exc}") from exc
    if size <= 0:
        raise UnlimitedOcrOnlineUnavailable(f"PDF 是空文件：{path}")
    if size > MAX_UPLOAD_BYTES:
        raise UnlimitedOcrOnlineUnavailable(
            f"PDF 太大（{size / 1048576:.1f}MB）：在线接口用 file_data 上传上限 50MB，"
            "请先压缩 / 拆分后再解析")
    token = get_access_token(api_key, secret_key)
    name = str(file_name or path.name or "document.pdf")
    if not Path(name).suffix:
        name += ".pdf"
    data = {
        "file_data": base64.b64encode(path.read_bytes()).decode("ascii"),
        "file_name": name,
    }
    payload = _http_json(_with_token(TASK_URL, token), data=data, timeout=timeout)
    result = _check_envelope(payload, "提交任务")
    task_id = str(result.get("task_id") or "").strip()
    if not task_id:
        raise UnlimitedOcrOnlineUnavailable(
            f"在线 Unlimited-OCR 提交任务没有返回 task_id：{payload}")
    return task_id


def query_task(task_id: str, *, api_key: str | None = None,
               secret_key: str | None = None,
               timeout: float = QUERY_TIMEOUT) -> dict:
    """查询任务状态，返回 `result` 段（status / task_error / parse_result_url …）。"""
    token = get_access_token(api_key, secret_key)
    payload = _http_json(_with_token(QUERY_URL, token),
                         data={"task_id": task_id}, timeout=timeout)
    return _check_envelope(payload, "查询任务")


def wait_result(task_id: str, *, api_key: str | None = None,
                secret_key: str | None = None, deadline: float | None = None,
                poll_interval: float = POLL_INTERVAL) -> dict:
    """轮询直到 `success`，返回结果段；`failed` / 超时抛异常。"""
    deadline = time.time() + DEFAULT_TIMEOUT if deadline is None else deadline
    last = ""
    while True:
        res = query_task(task_id, api_key=api_key, secret_key=secret_key)
        status = str(res.get("status") or "").strip().lower()
        last = status or last
        if status == "success":
            if not str(res.get("parse_result_url") or "").strip():
                raise UnlimitedOcrOnlineUnavailable(
                    "在线 Unlimited-OCR 任务成功但没有 parse_result_url")
            return res
        if status == "failed":
            why = str(res.get("task_error") or "").strip() or "未返回原因"
            raise UnlimitedOcrOnlineUnavailable(
                f"在线 Unlimited-OCR 任务失败：{why}（task_id={task_id}）")
        if time.time() >= deadline:
            raise UnlimitedOcrOnlineUnavailable(
                f"在线 Unlimited-OCR 等待超时（最后状态：{last or '未知'}，"
                f"task_id={task_id}）")
        time.sleep(max(0.5, float(poll_interval)))


def fetch_result(url: str, timeout: float = RESULT_TIMEOUT) -> dict:
    """下载 `parse_result_url` 指向的解析结果 JSON。"""
    payload = _http_json(str(url), timeout=timeout)
    if not payload.get("pages"):
        raise UnlimitedOcrOnlineUnavailable(
            f"在线解析结果为空（{url[:120]}）：{str(payload)[:200]}")
    return payload


# =====================================================================
#  结果 JSON → 本地客户端同构的中间结构
# =====================================================================
def _page_space(pg: dict) -> tuple[float, float]:
    """页面坐标空间（`meta.page_width/page_height`）；缺失时按 layout 边界估。"""
    meta = pg.get("meta") or {}
    try:
        w = float(meta.get("page_width") or 0)
        h = float(meta.get("page_height") or 0)
    except (TypeError, ValueError):
        w = h = 0.0
    if w > 0 and h > 0:
        return w, h
    right = bottom = 0.0
    for lay in pg.get("layouts") or []:
        pos = lay.get("position")
        if isinstance(pos, (list, tuple)) and len(pos) == 4:
            try:
                right = max(right, float(pos[0]) + float(pos[2]))
                bottom = max(bottom, float(pos[1]) + float(pos[3]))
            except (TypeError, ValueError):
                continue
    return (w or right or 1000.0), (h or bottom or 1000.0)


def _table_texts(pg: dict) -> dict[str, str]:
    """`layout_id → 表格内容`：优先 `table_html`（保结构），退回 markdown。"""
    out: dict[str, str] = {}
    for tb in pg.get("tables") or []:
        if not isinstance(tb, dict):
            continue
        lid = str(tb.get("layout_id") or "").strip()
        body = str(tb.get("table_html") or "").strip() or str(tb.get("markdown") or "").strip()
        if lid and body:
            out[lid] = body
    return out


def _crop_pdf_image(page, box: tuple[float, float, float, float], images_dir: Path,
                    pno: int, idx: int, *, crop_dpi: int, pad_pt: float,
                    min_size: int) -> Optional[dict]:
    """按归一化 bbox（0–1000）从 **原 PDF 页面** 裁剪 PNG 落盘。

    返回的 `bbox` 是**页面 pt**（与本地链路一致：页面 `w/h` 就是 pt、`dpi=72`，
    所以 `backend_unlimited_ocr._figure_image` 的 `scale` 是 1）。
    """
    import fitz  # 局部导入：只有裁图需要 Rect

    x0, y0, x1, y1 = box
    rect = (x0 / 1000.0 * page.rect.width, y0 / 1000.0 * page.rect.height,
            x1 / 1000.0 * page.rect.width, y1 / 1000.0 * page.rect.height)
    padded = fitz.Rect(rect[0] - pad_pt, rect[1] - pad_pt,
                       rect[2] + pad_pt, rect[3] + pad_pt) & page.rect
    if padded.is_empty or padded.width <= 0 or padded.height <= 0:
        return None
    crop = page.get_pixmap(clip=padded, dpi=max(48, int(crop_dpi)), alpha=False)
    if min(crop.width, crop.height) < max(1, min_size):
        return None
    fname = f"p{int(pno):03d}_i{int(idx):03d}.png"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / fname).write_bytes(crop.tobytes("png"))
    return {
        "file": fname,
        "bbox": [padded.x0, padded.y0, padded.x1, padded.y1],
        "size": [int(crop.width), int(crop.height)],
    }


def _pdf_body_size_hint(doc, max_pages: int = 40) -> float:
    """PDF 自带文字层的**正文字号**（pt，按字符数加权中位数）；拿不到返回 0。

    在线回包不带字号，而 `backend_unlimited_ocr` 那套「块几何 + 字符数」反解对**在线**的
    紧贴框会偏大（实测整篇正文 10pt 被反解成 ~11.9pt，页面上看着大一号）。电子版 PDF
    都有文字层，直接读它最准：只统计 6～24pt、水平行、≥ 8 个字符的 span，按字符数
    加权取中位数 —— 正文占绝对多数字符，标题 / 页眉页脚不会把基准抬高。
    扫描件（无文字层）返回 0，调用方回退到几何反解。
    """
    counts: dict[float, int] = {}
    for pno in range(min(doc.page_count, max(1, int(max_pages)))):
        try:
            data = doc.load_page(pno).get_text("dict")
        except Exception:
            continue
        for blk in data.get("blocks") or []:
            if blk.get("type") != 0:          # 0 = 文本块
                continue
            for line in blk.get("lines") or []:
                direction = line.get("dir") or (1, 0)
                if abs(float(direction[1])) > 0.1:    # 跳过竖排行（页边水印）
                    continue
                for span in line.get("spans") or []:
                    try:
                        size = round(float(span.get("size") or 0), 1)
                    except (TypeError, ValueError):
                        continue
                    chars = len(str(span.get("text") or "").strip())
                    if 6.0 <= size <= 24.0 and chars >= 8:
                        counts[size] = counts.get(size, 0) + chars
    if not counts:
        return 0.0
    total = sum(counts.values())
    acc = 0
    for size in sorted(counts):
        acc += counts[size]
        if acc * 2 >= total:
            return float(size)
    return float(max(counts))


def _page_spans(page) -> list[dict]:
    """页面文字层 → span 列表（`x0/y0/x1/y1/size/chars/oy`；跳过竖排行）。

    给「逐块量字号 / 行距」用：电子版 PDF 的 span 自带真实字号与基线，比
    「块几何 + 字符数」反解准得多（后者只能给出整篇一个正文基准）。
    """
    out: list[dict] = []
    try:
        data = page.get_text("dict")
    except Exception:
        return out
    for blk in data.get("blocks") or []:
        if blk.get("type") != 0:              # 0 = 文本块
            continue
        for line in blk.get("lines") or []:
            if abs(float((line.get("dir") or (1, 0))[1])) > 0.1:
                continue
            for span in line.get("spans") or []:
                chars = len(str(span.get("text") or "").strip())
                if not chars:
                    continue
                box = span.get("bbox") or (0, 0, 0, 0)
                try:
                    out.append({
                        "x0": float(box[0]), "y0": float(box[1]),
                        "x1": float(box[2]), "y1": float(box[3]),
                        "size": float(span.get("size") or 0),
                        "chars": chars,
                        "oy": round(float((span.get("origin") or (0, 0))[1]), 1),
                    })
                except (TypeError, ValueError, IndexError):
                    continue
    return out


def _rect_metrics(spans: list[dict], rect: tuple[float, float, float, float]
                  ) -> tuple[float, float]:
    """一个块矩形内的（字号, 行距），按块内字符数取众数；量不到返回 `(0, 0)`。

    * 字号：中心点落在块内的 span，按字符数加权取最多的那档（避免图注里夹的
      上下标 / 公式字形把字号带偏）。
    * 行距：同字号的 span 按基线 `origin.y` 去重排序，相邻差值取中位数
      （单行块没有行距 → 0，由调用方按倍率补）。
    """
    x0, y0, x1, y1 = rect
    weights: dict[float, int] = {}
    hits: list[dict] = []
    for sp in spans:
        cx, cy = (sp["x0"] + sp["x1"]) / 2, (sp["y0"] + sp["y1"]) / 2
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue
        hits.append(sp)
        key = round(sp["size"], 1)
        weights[key] = weights.get(key, 0) + sp["chars"]
    if not weights:
        return 0.0, 0.0
    size = max(weights.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    origins = sorted({sp["oy"] for sp in hits if abs(sp["size"] - size) < 0.6})
    advances = [round(b - a, 2) for a, b in zip(origins, origins[1:]) if b - a > 0.5]
    line_h = 0.0
    if advances:
        advances.sort()
        line_h = advances[len(advances) // 2]
    return float(size), float(line_h)


def _layout_bbox(pos, mw: float, mh: float) -> Optional[list[float]]:
    """`position` `[x, y, w, h]`（页面坐标）→ 归一化 bbox `[x0,y0,x1,y1]`（0–1000）。"""
    if not isinstance(pos, (list, tuple)) or len(pos) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in pos)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or mw <= 0 or mh <= 0:
        return None
    return [round(x / mw * 1000.0, 3), round(y / mh * 1000.0, 3),
            round((x + w) / mw * 1000.0, 3), round((y + h) / mh * 1000.0, 3)]


def convert_result(data: dict, pdf_path: str | Path, *,
                   images_dir: str | Path | None = None,
                   opts: dict | None = None) -> dict:
    """在线结果 JSON → 与 `unlimited_ocr.ocr_pdf` **同构**的中间结构。

         {"pages": [{"page", "w", "h",              # w/h = 原 PDF 页面 pt
                     "items": [{"page", "type", "bbox"(0–1000), "text",
                                "sub_type", "layout_id", "image"}]}],
         "dpi": 72, "url", "model", "route": "online", "stats": {...}}
    """
    options = opts or {}
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:                  # 裁图 / 页面尺寸都要它
        raise UnlimitedOcrOnlineUnavailable(
            f"缺少 PyMuPDF（用于页面尺寸与插图裁剪）：{exc}") from exc

    try:
        raw_crop_dpi = int(options.get("image_dpi") or 0)
    except (TypeError, ValueError):
        raw_crop_dpi = 0
    try:
        use_dpi = int(options.get("dpi") or 150)
    except (TypeError, ValueError):
        use_dpi = 150
    crop_dpi = max(use_dpi, raw_crop_dpi) if raw_crop_dpi > 0 else max(use_dpi, 150)
    pad = int(options.get("image_pad") or 0)
    min_size = int(options.get("min_image_size") or 0)
    pad_pt = pad * 72.0 / crop_dpi

    imgdir = Path(images_dir) if images_dir is not None else None
    pages_out: list[dict] = []
    type_counts: dict[str, int] = {}
    item_count = 0
    image_count = 0
    rotated_pages = 0
    merged = 0

    pages_raw = [p for p in (data.get("pages") or []) if isinstance(p, dict)]
    pages_raw.sort(key=lambda p: int(p.get("page_num") or 0))

    doc = fitz.open(str(pdf_path))
    body_hint = 0.0
    try:
        # 文字层正文字号（校准渲染字号用；扫描件拿不到就是 0）
        try:
            body_hint = _pdf_body_size_hint(doc)
        except Exception:
            body_hint = 0.0
        for pg in pages_raw:
            try:
                pno = int(pg.get("page_num") or 0) + 1
            except (TypeError, ValueError):
                continue
            if pno < 1 or pno > doc.page_count:
                continue            # 回包页码对不上（理论上不会发生）
            try:
                angle = int(pg.get("page_angle") or 0) % 360
            except (TypeError, ValueError):
                angle = 0
            if angle:
                rotated_pages += 1
            page = doc.load_page(pno - 1)
            pw, ph = float(page.rect.width), float(page.rect.height)
            mw, mh = _page_space(pg)
            tables = _table_texts(pg)
            spans = _page_spans(page)      # 文字层 span（逐块量字号 / 行距用）

            page_items: list[dict] = []
            img_idx = 0
            for lay in pg.get("layouts") or []:
                if not isinstance(lay, dict):
                    continue
                typ = str(lay.get("type") or "").strip().lower()
                if not typ:
                    continue
                bbox = _layout_bbox(lay.get("position"), mw, mh)
                if bbox is None:
                    continue
                text = str(lay.get("text") or "")
                lid = str(lay.get("layout_id") or "").strip()
                if typ == "table":
                    text = tables.get(lid) or text
                if str(lay.get("merge_text") or "").strip() in ("inner", "end"):
                    merged += 1
                image = None
                if typ in IMAGE_TYPES and imgdir is not None:
                    image = _crop_pdf_image(
                        page, (bbox[0], bbox[1], bbox[2], bbox[3]), imgdir,
                        pno, img_idx, crop_dpi=crop_dpi, pad_pt=pad_pt,
                        min_size=min_size)
                    if image is not None:
                        img_idx += 1
                        image_count += 1
                type_counts[typ] = type_counts.get(typ, 0) + 1
                item_count += 1
                # 块内真实字号 / 行距（PDF 文字层；扫描件拿不到就是 0，
                # 由 backend_unlimited_ocr 回退到「块几何反解」）
                rect_pt = (bbox[0] / 1000.0 * pw, bbox[1] / 1000.0 * ph,
                           bbox[2] / 1000.0 * pw, bbox[3] / 1000.0 * ph)
                pdf_size, pdf_line_h = _rect_metrics(spans, rect_pt)
                page_items.append({
                    "page": pno, "type": typ, "bbox": bbox, "text": text,
                    "sub_type": str(lay.get("sub_type") or "").strip(),
                    "layout_id": lid, "image": image,
                    "pdf_size": pdf_size, "pdf_line_h": pdf_line_h,
                })

            pages_out.append({"page": pno, "w": round(pw, 3),
                              "h": round(ph, 3), "items": page_items})
    finally:
        doc.close()

    if not pages_out:
        raise UnlimitedOcrOnlineUnavailable("在线解析结果里没有可用的页面")

    token_usage = data.get("token_usage") if isinstance(data.get("token_usage"), dict) else {}
    return {
        "pages": pages_out,
        "dpi": 72,
        "url": TASK_URL,
        "model": ONLINE_MODEL,
        "route": "online",
        "stats": {
            "unlimited_ocr_pages": len(pages_out),
            "unlimited_ocr_items": item_count,
            "unlimited_ocr_images": image_count,
            "unlimited_ocr_types": dict(sorted(type_counts.items(),
                                               key=lambda kv: -kv[1])),
            "unlimited_ocr_rotated_pages": rotated_pages,
            "unlimited_ocr_merged_blocks": merged,
            # PDF 文字层的正文字号（渲染字号的基准；0 = 无文字层 / 未拿到）
            "unlimited_ocr_pdf_body_size": body_hint,
            "unlimited_ocr_online_cost_pages": data.get("cost_page_num"),
            "unlimited_ocr_online_total_tokens": token_usage.get("total_tokens"),
        },
    }


# =====================================================================
#  对外入口
# =====================================================================
def ocr_pdf(pdf_path: str | Path, opts: dict | None = None, *,
            images_dir: str | Path | None = None,
            result: Optional[dict] = None,
            task_id: str | None = None,
            poll_interval: float = POLL_INTERVAL,
            submit: Optional[Callable] = None,
            query: Optional[Callable] = None,
            fetch: Optional[Callable] = None) -> dict:
    """整份 PDF → 在线 Unlimited-OCR 版面数据（与本地客户端同构）。

    * `opts` 读 `unlimited_ocr_timeout`（整体等待上限）/ `dpi` / `image_dpi` /
      `image_pad` / `min_image_size`（插图裁剪），以及在线凭据
      `unlimited_ocr_online_api_key` / `unlimited_ocr_online_secret_key`
      （来自 `config.json` 的 parser 段，可用环境变量覆盖）；
    * `result` 可直接传入**已下载的结果 JSON**（离线自检 / 重放，不联网）；
    * `submit` / `query` / `fetch` 可注入桩（测试用），签名分别同
      `submit_task(pdf_path, **kw)` / `query_task(task_id, **kw)` /
      `fetch_result(url, **kw)`。
    """
    options = opts or {}
    t0 = time.time()
    api, secret = _credentials(options)
    try:
        wait = max(60.0, float(options.get("unlimited_ocr_timeout") or DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        wait = DEFAULT_TIMEOUT

    data = result
    task_id = str(task_id or "")
    task_ms = 0
    cost_pages = None
    if data is None:
        t_submit = time.time()
        submitted = (submit or submit_task)(pdf_path, api_key=api, secret_key=secret)
        # `submit_task` 返回 task_id 字符串；注入的桩可以返回 `{"task_id": …}`
        if isinstance(submitted, dict):
            task_id = str(submitted.get("task_id") or "").strip()
        else:
            task_id = str(submitted or "").strip()
        if not task_id:
            raise UnlimitedOcrOnlineUnavailable("在线 Unlimited-OCR 提交任务没有返回 task_id")
        # `query` 桩的约定与 `wait_result` 一致：自己轮询到 success 并返回结果段
        res = (query or wait_result)(task_id, api_key=api, secret_key=secret,
                                     deadline=time.time() + wait,
                                     poll_interval=poll_interval)
        task_ms = int((time.time() - t_submit) * 1000)
        cost_pages = (res or {}).get("cost_page_num")
        url = str((res or {}).get("parse_result_url") or "").strip()
        if not url:
            raise UnlimitedOcrOnlineUnavailable(
                "在线 Unlimited-OCR 任务没有 parse_result_url")
        data = (fetch or fetch_result)(url)

    out = convert_result(data, pdf_path, images_dir=images_dir, opts=options)
    out["stats"]["unlimited_ocr_ms"] = int((time.time() - t0) * 1000)
    out["stats"]["unlimited_ocr_task_ms"] = task_ms
    if cost_pages is not None:
        out["stats"]["unlimited_ocr_online_cost_pages"] = cost_pages
    if task_id:
        out["stats"]["unlimited_ocr_task_id"] = task_id
    return out
