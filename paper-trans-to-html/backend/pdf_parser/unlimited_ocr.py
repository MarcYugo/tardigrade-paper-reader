"""Unlimited-OCR 推理服务客户端（整页 OCR：文字 / 公式 LaTeX / 表格 HTML / 插图框）。

服务端见仓库根 `unlimited_ocr_service/`（PaddlePaddle/Unlimited-OCR，vLLM 的
OpenAI 兼容接口，默认 http://127.0.0.1:9010/v1）；本模块是**客户端** —— 只用标准库
发请求（不依赖 `openai` 包；见 `unlimited_ocr_service/README.md`），页面渲染与插图
裁剪走 PyMuPDF。

调用链（与 `unlimited_ocr_service/test_unlimited_ocr_service.py` 同一套）：

    render（PDF 逐页渲染成 PNG）
      → 每页一次 `POST {url}/chat/completions`（一页一张图、逐页串行；提示词必须以
        字面量 `<image>` 开头，`skip_special_tokens=False` 与 `vllm_xargs` 必须带上，
        否则输出为空 / 会在 `<|det|>` 上打转）
      → `parse_layout_items` 把模型的 grounding 输出（行级 `类型 [bbox] 文本`）解析成
        结构化项（page / type / bbox / text）
      → 图片类（image/chart）按 bbox 从 PDF 页面裁剪落盘
        （命名 `p{页:03d}_i{序号:03d}.png`，与阅读器图片接口口径一致）

⚠️ **坐标口径**：模型输出的 bbox 是 **0–1000 归一化坐标**（x、y 各自除以页面宽高
再乘 1000），**不是渲染图像素**（官方 README 的说法有误导；已用 w=612pt 的样本页
逐条验证：`pt = 归一化值 / 1000 × 页面边长`）。所以渲染 dpi 只影响 OCR 清晰度，
不影响坐标换算；换 dpi 后回包坐标不用重新标定。

⚠️ 失败一律抛 `UnlimitedOcrUnavailable`；调用方（`pdf_parser/backend/backend_unlimited_ocr.py`）
**不回退 PyMuPDF**，直接让整次解析失败（免得用户以为看到了 OCR 的效果）。

对外接口（`pdf_parser/backend/backend_unlimited_ocr.py` 与 `pdf_parser/options.py::status()` 在用）：

  * `check_server` / `ocr_pdf` / `parse_layout_items` / `UnlimitedOcrUnavailable`；
  * `DEFAULT_URL` / `DEFAULT_MODEL` / `DEFAULT_TIMEOUT` / `PROBE_TIMEOUT`
    （`pdf_parser/options.py` 从这里取默认值，`config.json` 的键与之一一对应）。
"""
from __future__ import annotations

import base64
import json
import math
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "DEFAULT_MODEL", "DEFAULT_TIMEOUT", "DEFAULT_URL", "PROBE_TIMEOUT",
    "UnlimitedOcrUnavailable", "check_server", "normalize_url", "ocr_pdf",
    "parse_layout_items",
]

# 默认地址与 `unlimited_ocr_service` 的端口保持一致（见其 docker-compose.yml：
# VLLM_PORT=9010，服务名 Unlimited-OCR）
DEFAULT_URL = "http://127.0.0.1:9010/v1"
DEFAULT_MODEL = "Unlimited-OCR"
# 单页请求的读取超时（秒）：长文档解析模型单页可能要几十秒，给足余量
DEFAULT_TIMEOUT = 600.0
# 就绪探测的超时：面板拉状态时不能卡住
PROBE_TIMEOUT = 2.0
# 单页生成的 token 上限（服务端 VLLM_MAX_MODEL_LEN 默认 8192，别顶满）
DEFAULT_MAX_TOKENS = 2048

# 提示词必须以字面量 `<image>` 开头（服务端模型约定，缺了会返回空）
_PROMPT = "<image>document parsing."
# vLLM 扩展参数：保留 <|ref|>/<|det|> grounding token + n-gram 去环
# （window_size 跟官方冒烟脚本一致；缺这些参数输出会在 <|det|> 上打转）
_VLLM_XARGS = {"ngram_size": 35, "window_size": 1024}

# 占位/图片类类型（客户端按 bbox 裁图；其余类型只回结构化文本给解析后端）
IMAGE_TYPES = ("image", "chart", "figure", "diagram")

# 记录行：`类型 [x0, y0, x1, y1] 文本`（与官方 test_unlimited_ocr_service.py 同口径）
_RECORD_RE = re.compile(
    r"^(?P<kind>[A-Za-z_][\w-]*)\s+\[(?P<bbox>[^\]]+)\]\s*(?P<text>.*)$")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


class UnlimitedOcrUnavailable(RuntimeError):
    """Unlimited-OCR 推理服务不可用 / 请求失败 / 回包异常。"""


def normalize_url(url: str | None = None) -> str:
    """补全默认地址，并去掉结尾多余的 '/'。"""
    return (str(url or "").strip() or DEFAULT_URL).rstrip("/")


def _get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


# =====================================================================
#  就绪探测（面板状态 + 解析前置检查）
# =====================================================================
def check_server(url: str | None = None, model: str | None = None,
                 timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """探测推理服务就绪状态（`GET {url}/models`）。返回 `(是否就绪, 说明文本)`。

    vLLM 的模型挂在 `/v1/models` 上；地址末尾带 `/v1` 时补 `/models`。
    模型名对不上只提示、不算失败（服务端可用 `--served-model-name` 改名，
    对不上时请求会 404，把提示写清楚）。
    """
    base = normalize_url(url)
    models_url = base + "/models" if base.endswith("/v1") else base
    try:
        data = _get_json(models_url, timeout)
    except Exception as exc:  # 连接失败/超时/非 JSON 都算不可用
        return False, f"无法连接 Unlimited-OCR 推理服务 {models_url}：{exc}"
    names = [str(m.get("id")) for m in (data.get("data") or [])
             if isinstance(m, dict) and m.get("id")]
    want = str(model or "").strip() or DEFAULT_MODEL
    if names and want not in names:
        return True, (f"服务已就绪，但模型列表里没有 {want}"
                      f"（当前：{'、'.join(names[:4])}）——请确认服务端 "
                      f"--served-model-name 或本页配置的模型名")
    tail = f"（模型：{'、'.join(names[:4])}）" if names else "（未返回模型列表）"
    return True, "服务已就绪" + tail


# =====================================================================
#  模型输出 → 结构化版面项（官方冒烟脚本的解析口径）
# =====================================================================
def parse_layout_items(text: str, pages=None) -> list[dict]:
    """把模型的行级 grounding 输出解析成结构化项。

    每条形如 `title [255, 203, 745, 229] Latex Formular ...`（bbox 为
    **0–1000 归一化坐标**）；`<PAGE>` 标记翻页；不匹配记录格式的续行
    追加到当前项的文本上（`\\n` 分隔）。返回
    `[{page, type, bbox: [x0,y0,x1,y1] | None, text}, …]`。

    `pages` 传 0 基页号列表（与官方脚本同口径），用于把结果标到正确页码上；
    单页请求时传 `[页下标]` 即可。
    """
    page_numbers = [page + 1 for page in pages] if pages is not None else []
    page_index = 0
    current_page = page_numbers[0] if page_numbers else 1
    items: list[dict] = []
    current_item: Optional[dict] = None

    # 去掉 token 包装但保留内容（尤其是 <|det|> 里的类别名）
    text = re.sub(r"<\|/?(?:ref|det)\|>", "", text or "")

    for line in text.splitlines():
        for page_part, part in enumerate(line.split("<PAGE>")):
            if page_part:
                page_index += 1
                if page_numbers:
                    current_page = page_numbers[min(page_index, len(page_numbers) - 1)]
                else:
                    current_page += 1
            part = part.strip()
            if not part:
                continue

            match = _RECORD_RE.match(part)
            if match:
                coordinates = _NUMBER_RE.findall(match.group("bbox"))
                if len(coordinates) == 4:
                    bbox = [int(v) if float(v).is_integer() else float(v)
                            for v in coordinates]
                    current_item = {
                        "page": current_page,
                        "type": match.group("kind"),
                        "bbox": bbox,
                        "text": match.group("text").strip(),
                    }
                    items.append(current_item)
                    continue

            if current_item is None or current_item["page"] != current_page:
                current_item = {"page": current_page, "type": "text",
                                "bbox": None, "text": part}
                items.append(current_item)
            else:
                current_item["text"] += ("\n" if current_item["text"] else "") + part

    return items


# =====================================================================
#  页面渲染 / 请求
# =====================================================================
def _post_chat(base_url: str, *, model: str, image_b64: str, api_key: str,
               timeout: float, max_tokens: int) -> str:
    """一次 `POST {base}/chat/completions`（单页单图），返回模型原始输出文本。"""
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + image_b64}},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        # ⚠️ 这两个是 vLLM 扩展参数（官方要求放请求体顶层）：缺了会输出为空或打转
        "skip_special_tokens": False,
        "vllm_xargs": dict(_VLLM_XARGS),
    }
    req = urllib.request.Request(
        base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise UnlimitedOcrUnavailable(
            f"Unlimited-OCR 请求失败 HTTP {exc.code}：{detail}") from exc
    except Exception as exc:
        raise UnlimitedOcrUnavailable(f"Unlimited-OCR 请求失败：{exc}") from exc

    if isinstance(payload, dict) and payload.get("error"):
        raise UnlimitedOcrUnavailable(f"Unlimited-OCR 服务返回错误：{payload['error']}")
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise UnlimitedOcrUnavailable("Unlimited-OCR 回包里没有 choices（模型可能尚未加载完成）")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):      # 兼容 content 分片
        content = "".join(str(p.get("text") or "") for p in content
                          if isinstance(p, dict))
    if not content:
        raise UnlimitedOcrUnavailable(
            "Unlimited-OCR 返回为空（检查提示词以 <image> 开头、"
            "skip_special_tokens=False、服务端装了 --logits-processors）")
    return str(content)


def _norm_rect(box, page_w: float, page_h: float):
    """归一化 bbox（0–1000）→ PDF 点矩形（PyMuPDF Rect）。返回 None 表示退化。"""
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    x0, x1 = x0 / 1000.0 * page_w, x1 / 1000.0 * page_w
    y0, y1 = y0 / 1000.0 * page_h, y1 / 1000.0 * page_h
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def ocr_pdf(pdf_path: str | Path, opts: dict | None = None, *,
            images_dir: str | Path | None = None,
            page_numbers=None, responder: Optional[Callable] = None,
            dpi: int | None = None) -> dict:
    """整份 PDF → Unlimited-OCR 版面数据（客户端全流程）。

    返回（`bbox` / `image.bbox` 为渲染图像素；`items[*].bbox` 为**模型原始归一化
    坐标**原样保留，由解析后端换算 —— 那里有页面 pt 尺寸与同一把尺子）：

        {"pages": [{"page", "w", "h",                 # 渲染图尺寸(px)
                    "items": [{"page", "type", "bbox": [x0,y0,x1,y1], "text",
                               "image": {"file", "bbox", "size"} | None}, …]}, …],
         "dpi", "url", "model", "stats": {...}}

    * `opts` 读 `unlimited_ocr_url` / `unlimited_ocr_model` /
      `unlimited_ocr_api_key` / `unlimited_ocr_timeout` / `dpi` / `image_dpi` /
      `image_pad` / `min_image_size`（与 config.json 的 parser 段一一对应）。
      `dpi` 是页面渲染分辨率（坐标是归一化的，dpi 只影响 OCR 清晰度与裁图底清）；
      `image_dpi` 是插图裁剪的独立分辨率（0/未设 = 跟 `dpi` 相同）；
    * 图片类（`image`/`chart`/…）按 bbox 裁剪落盘到 `images_dir`（None 则只报不存）；
      `min_image_size` 以下的裁图跳过（不产 `image`）；
    * `responder` 可注入（测试桩：接 `(页码, PNG bytes)` 返回模型原始输出文本）。
    """
    options = opts or {}
    url = normalize_url(options.get("unlimited_ocr_url"))
    model = str(options.get("unlimited_ocr_model") or "").strip() or DEFAULT_MODEL
    api_key = str(options.get("unlimited_ocr_api_key") or "").strip()
    try:
        timeout = max(30.0, float(options.get("unlimited_ocr_timeout")
                                  or DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    use_dpi = max(48, min(400, int(dpi if dpi is not None
                                   else (options.get("dpi") or 150))))
    try:
        raw_crop_dpi = int(options.get("image_dpi") or 0)
    except (TypeError, ValueError):
        raw_crop_dpi = 0
    crop_dpi = max(use_dpi, min(600, raw_crop_dpi)) if raw_crop_dpi > 0 else use_dpi
    pad = int(options.get("image_pad") or 0)
    min_size = int(options.get("min_image_size") or 0)
    # 裁图外扩：image_pad 按**输出像素**理解（与 Surya 客户端同口径）
    pad_pt = pad * 72.0 / crop_dpi
    t0 = time.time()

    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise UnlimitedOcrUnavailable(f"缺少 PyMuPDF（用于页面渲染）：{exc}") from exc

    picks = None if page_numbers is None else {int(p) for p in page_numbers}
    pages_out: list[dict] = []
    image_count = 0
    item_count = 0
    type_counts: dict[str, int] = {}

    doc = fitz.open(str(pdf_path))
    try:
        for pno in range(1, doc.page_count + 1):
            if picks is not None and pno not in picks:
                continue
            page = doc.load_page(pno - 1)
            if page.rotation % 360 != 0:
                # 旋转换算统一在渲染时归一化，坐标才与模型看到的图一致
                page.set_rotation(0)
            page_w, page_h = float(page.rect.width), float(page.rect.height)
            pix = page.get_pixmap(dpi=use_dpi, alpha=False)
            png = pix.tobytes("png")
            if responder is None:
                text = _post_chat(url, model=model,
                                  image_b64=base64.b64encode(png).decode("ascii"),
                                  api_key=api_key, timeout=timeout,
                                  max_tokens=DEFAULT_MAX_TOKENS)
            else:
                text = responder(pno, png)
            items = parse_layout_items(text, pages=[pno - 1])

            page_items: list[dict] = []
            page_img_idx = 0
            for it in items:
                typ = str(it.get("type") or "").strip().lower()
                type_counts[typ] = type_counts.get(typ, 0) + 1
                item_count += 1
                img_info = None
                if typ in IMAGE_TYPES and images_dir is not None:
                    img_info = _crop_image(page, it.get("bbox"), page_w, page_h,
                                           Path(images_dir), pno, page_img_idx,
                                           use_dpi=use_dpi, crop_dpi=crop_dpi,
                                           pad_pt=pad_pt, min_size=min_size)
                    if img_info is not None:
                        page_img_idx += 1
                        image_count += 1
                it = dict(it)
                it["image"] = img_info
                page_items.append(it)

            pages_out.append({"page": pno, "w": int(pix.width), "h": int(pix.height),
                              "items": page_items})
    finally:
        doc.close()

    if not pages_out:
        raise UnlimitedOcrUnavailable("PDF 没有可解析的页面")
    return {
        "pages": pages_out,
        "dpi": use_dpi,
        "url": url,
        "model": model,
        "stats": {
            "unlimited_ocr_pages": len(pages_out),
            "unlimited_ocr_items": item_count,
            "unlimited_ocr_images": image_count,
            "unlimited_ocr_types": dict(sorted(type_counts.items(),
                                               key=lambda kv: -kv[1])),
            "unlimited_ocr_ms": int((time.time() - t0) * 1000),
        },
    }


def _crop_image(page, box, page_w: float, page_h: float, images_dir: Path,
                pno: int, idx: int, *, use_dpi: int, crop_dpi: int,
                pad_pt: float, min_size: int) -> Optional[dict]:
    """图片类项目的 bbox（归一化）→ 从 PDF 页面裁剪 PNG 落盘。

    返回 `{"file", "bbox"(渲染图像素, 外扩后), "size"}`；裁剪失败/过小返回 None。
    """
    import fitz  # 局部导入：仅本函数用到 Rect
    rect = _norm_rect(box, page_w, page_h)
    if rect is None:
        return None
    x0, y0, x1, y1 = rect
    padded = fitz.Rect(x0 - pad_pt, y0 - pad_pt, x1 + pad_pt, y1 + pad_pt)
    padded = padded & page.rect
    if padded.is_empty or padded.width <= 0 or padded.height <= 0:
        return None
    crop = page.get_pixmap(clip=padded, dpi=crop_dpi, alpha=False)
    if min(crop.width, crop.height) < max(1, min_size):
        return None
    fname = f"p{int(pno):03d}_i{int(idx):03d}.png"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / fname).write_bytes(crop.tobytes("png"))
    k = use_dpi / 72.0                       # pt → 渲染图像素（后端再换回 pt）
    bbox = [int(math.floor(padded.x0 * k)), int(math.floor(padded.y0 * k)),
            int(math.ceil(padded.x1 * k)), int(math.ceil(padded.y1 * k))]
    return {"file": fname, "bbox": bbox,
            "size": [int(crop.width), int(crop.height)]}
