"""Surya 2 推理服务客户端（版面分析 / 整页 OCR / 插图裁剪 / 块 HTML 工具）。

服务端见仓库根 `surya_doc_parse_service/`（llama.cpp / vLLM 跑 surya-2 模型，OpenAI
兼容接口）；本模块是**客户端**，运行时需要本机安装 `surya-ocr>=0.22`（它负责前处理、
调远端推理、把结果整理成 block 列表），见 `requirements.txt` 的说明。

调用链（与 `surya_doc_parse_service/test_surya_service.py` 同一套）：

    render_pages（PDF 逐页渲染成 PIL 图像，坐标即页面图像素）
      → RecognitionPredictor 整页 OCR（每页一次 VLM 调用）
      → 每页拿到 blocks：{label, raw_label, reading_order, confidence, skipped,
                          error, polygon(px), html}
      → 图片类 block（Picture/Figure/Diagram/ChemicalBlock）按 polygon 从页面图像
        裁剪落盘（命名 `p{页:03d}_i{序号:03d}.png`，与阅读器图片接口口径一致）；
        裁剪分辨率可用 `image_dpi` 单独调高（对该页按高分辨率重渲一次再裁，
        只影响插图清晰度，OCR 与坐标仍走 `dpi` 空间）

⚠️ 两个必须记住的点：

  1. `SURYA_INFERENCE_URL` / `SURYA_INFERENCE_BACKEND` 必须在 `import surya` **之前**
     写进环境变量，否则 surya 会尝试自己拉起本地后端（本项目里没有）；
  2. 图片类 block 一定是 `skipped=True` / `html=""` —— 模型本就不对图做 OCR，
     **绝不能拿 `skipped` / 空 html 当过滤条件**，否则要提取的图会被全部丢掉。

对外接口（`pdf_parser/backend/backend_surya.py` 与 `pdf_parser/options.py::status()` 在用）：

  * `check_server` / `client_ready` / `ocr_pdf` / `SuryaUnavailable`；
  * `html_to_text_runs`：块 HTML →（纯文本, runs, 数学区间），翻译 / 句子 / 渲染共用；
  * `label_kind` / `heading_level` / `latex_from_html`：标签 → 归一化 kind、
    标题级别、equation 块里的 LaTeX（Surya 2 的 `<math>` 里装的是 **LaTeX 源码**，
    不是 MathML）；
  * `DEFAULT_URL` / `DEFAULT_BACKEND` / `DEFAULT_IMAGE_LABELS` / `PROBE_TIMEOUT`
    （`pdf_parser/options.py` 从这里取默认值，`config.json` 的键与之一一对应）。
"""
from __future__ import annotations

import html as _html_mod
import importlib.util
import json
import math
import os
import re
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

__all__ = [
    "DEFAULT_BACKEND", "DEFAULT_IMAGE_LABELS", "DEFAULT_URL", "PROBE_TIMEOUT",
    "SuryaUnavailable", "check_server", "client_ready", "crop_filename",
    "heading_level", "html_to_text_runs", "label_kind", "latex_from_html",
    "normalize_url", "ocr_pdf", "render_pages",
]

# 默认地址与 `surya_doc_parse_service` 的端口保持一致（见其 docker-compose.yml）
DEFAULT_URL = "http://127.0.0.1:8060/v1"
DEFAULT_BACKEND = "llamacpp"                     # llamacpp | vllm
# 版面标签里属于「图片类」的 canonical 名称（见 surya.layout.label.LAYOUT_PRED_RELABEL）：
#   Image -> Picture, Complex-Block/Figure -> Figure, Diagram, Chemical-Block -> ChemicalBlock
DEFAULT_IMAGE_LABELS: tuple[str, ...] = ("Picture", "Figure", "Diagram", "ChemicalBlock")
# 就绪探测的超时：面板拉状态时不能卡住
PROBE_TIMEOUT = 2.0
_MODEL_HINT = "datalab-to/surya-ocr-2"


class SuryaUnavailable(RuntimeError):
    """Surya 2 推理服务不可用 / 客户端依赖缺失 / 推理或回包异常。"""


def normalize_url(url: str | None = None) -> str:
    """补全默认地址，并去掉结尾多余的 '/'。"""
    return (str(url or "").strip() or DEFAULT_URL).rstrip("/")


# =====================================================================
#  就绪探测（面板状态 + 解析前置检查）
# =====================================================================
def _get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def check_server(url: str | None = None,
                 timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """探测推理服务就绪状态（`GET {url}/models`）。返回 `(是否就绪, 说明文本)`。

    llama-server 的模型挂在 `/v1/models` 上；地址末尾带 `/v1` 时补 `/models`，
    否则按传入地址本身就是模型端点处理（与 `test_surya_service.py` 同一口径）。
    """
    base = normalize_url(url)
    models_url = base + "/models" if base.endswith("/v1") else base
    try:
        data = _get_json(models_url, timeout)
    except Exception as exc:  # 连接失败/超时/非 JSON 都算不可用
        return False, f"无法连接 Surya 2 推理服务 {models_url}：{exc}"
    names = [str(m.get("id")) for m in (data.get("data") or [])
             if isinstance(m, dict) and m.get("id")]
    if names and _MODEL_HINT not in names:
        return True, (f"服务已就绪，但模型列表里没有 {_MODEL_HINT}"
                      f"（当前：{'、'.join(names[:4])}）——请确认服务端以 --alias "
                      f"{_MODEL_HINT} 启动")
    tail = f"（模型：{'、'.join(names[:4])}）" if names else "（未返回模型列表）"
    return True, "服务已就绪" + tail


def client_ready() -> tuple[bool, str]:
    """本机 surya-ocr 客户端依赖是否可用（只做 import 探测，不加载模型）。"""
    try:
        spec = importlib.util.find_spec("surya")
    except Exception as exc:  # 环境损坏（如 __init__ 导入即报错）也算不可用
        return False, f"surya-ocr 客户端依赖探测失败：{exc}"
    if spec is None:
        return False, ("本机未安装 surya-ocr 客户端依赖"
                       "（pip install -U surya-ocr；见 requirements.txt 的说明）")
    return True, "surya-ocr 客户端已安装"


# =====================================================================
#  页面渲染 / 插图裁剪
# =====================================================================
def render_pages(pdf_path: str | Path, *, dpi: int = 192, page_numbers=None):
    """把 PDF 逐页渲染成 PIL 图像。返回 `[(1 基页码, PIL.Image), …]`。

    页面旋转先归一化为 0 —— 之后所有坐标（block polygon、裁剪框）都在这套
    「渲染图像素」坐标系里，换算 `pt = px * 72 / dpi` 即可。
    `page_numbers` 传 1 基页码集合可只渲染部分页面；None = 全部。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise SuryaUnavailable(f"缺少 PyMuPDF（用于页面渲染）：{exc}") from exc
    try:
        from PIL import Image
    except ImportError as exc:
        raise SuryaUnavailable(
            f"缺少 Pillow（页面图像转换）：pip install Pillow ({exc})") from exc

    # 上限 600：OCR 路径在 ocr_pdf 里已先夹到 400，这里放宽是给插图高清
    # 裁剪（image_dpi）留空间 —— 同一页可按更高 dpi 重渲一次、裁出更清晰
    # 的位图（见 ocr_pdf 里的“高清裁剪”）。
    use_dpi = max(48, min(600, int(dpi or 192)))
    picks = None if page_numbers is None else {int(p) for p in page_numbers}
    out: list[tuple[int, object]] = []
    doc = fitz.open(str(pdf_path))
    try:
        for pno in range(1, doc.page_count + 1):
            if picks is not None and pno not in picks:
                continue
            page = doc.load_page(pno - 1)
            if page.rotation % 360 != 0:
                page.set_rotation(0)
            pix = page.get_pixmap(dpi=use_dpi, alpha=False)
            out.append((pno, Image.frombytes("RGB", (pix.width, pix.height),
                                             pix.samples)))
    finally:
        doc.close()
    return out


def crop_filename(page_no: int, index: int, image_format: str = "png") -> str:
    """插图文件名 —— 与 `/api/doc/{id}/img/{fname}` 的 `p\\d+_i\\d+` 口径一致。"""
    fmt = "jpg" if str(image_format).lower() in ("jpg", "jpeg") else "png"
    return f"p{int(page_no):03d}_i{int(index):03d}.{fmt}"


def _crop_block(image, polygon, pad: int = 4):
    """按 block polygon（页面图像素坐标）裁剪。返回 `(bbox, crop)`。

    区域退化/越界时 `crop` 为 None（bbox 仍给出，便于上层判断与统计）。
    """
    if not polygon:
        return None, None
    try:
        xs = [float(p[0]) for p in polygon]
        ys = [float(p[1]) for p in polygon]
    except (TypeError, ValueError, IndexError):
        return None, None
    if not xs or not ys:
        return None, None
    x0 = max(0, int(math.floor(min(xs))) - int(pad))
    y0 = max(0, int(math.floor(min(ys))) - int(pad))
    x1 = min(int(image.size[0]), int(math.ceil(max(xs))) + int(pad))
    y1 = min(int(image.size[1]), int(math.ceil(max(ys))) + int(pad))
    bbox = [x0, y0, x1, y1]
    if x1 <= x0 or y1 <= y0:
        return bbox, None
    return bbox, image.crop((x0, y0, x1, y1))


# =====================================================================
#  整页 OCR（客户端全流程）
# =====================================================================
def _make_predictor(url: str, backend: str):
    """构造 surya 的整页 OCR 预测器（延迟导入；环境变量必须在导入前写）。"""
    ok, msg = client_ready()
    if not ok:
        raise SuryaUnavailable(msg)
    # ⚠️ 顺序不能反：先写环境变量、再 import surya —— 否则 surya 会尝试自己拉起
    # 本地后端（它默认从环境变量读外部服务地址/后端类型）。
    os.environ["SURYA_INFERENCE_URL"] = url
    os.environ["SURYA_INFERENCE_BACKEND"] = backend
    try:
        from surya.inference import SuryaInferenceManager
        from surya.recognition import RecognitionPredictor
    except ImportError as exc:
        raise SuryaUnavailable(f"surya-ocr 客户端导入失败：{exc}") from exc
    except Exception as exc:      # 客户端自身初始化报错（版本不匹配等）
        raise SuryaUnavailable(f"surya-ocr 客户端初始化失败：{exc}") from exc
    try:
        manager = SuryaInferenceManager(method=backend)
        return RecognitionPredictor(manager)
    except Exception as exc:
        raise SuryaUnavailable(
            f"Surya 2 客户端连接失败（{url}，后端 {backend}）：{exc}") from exc


def ocr_pdf(pdf_path: str | Path, opts: dict | None = None, *,
            images_dir: str | Path | None = None,
            page_numbers=None, predictor=None, dpi: int | None = None) -> dict:
    """整份 PDF → Surya 版面数据（客户端全流程）。

    返回（`bbox` / `image.bbox` 都是**页面图像像素**，`bbox = [x0, y0, x1, y1]`）：

        {"pages": [{"page", "w", "h",
                    "blocks": [{"label", "raw_label", "reading_order",
                                "confidence", "skipped", "error", "bbox", "html",
                                "image": {"file", "bbox", "size"} | None}, …]}, …],
         "dpi", "url", "backend", "stats": {...}}

    * `opts` 读 `surya_url` / `surya_backend` / `dpi` / `image_dpi` / `image_labels` /
      `image_pad` / `min_image_size`（与 config.json 的 parser 段一一对应）。
      `dpi` 是**页面渲染 / 整页 OCR** 的分辨率（版面坐标都在它上面）；
      `image_dpi` 是**插图裁剪**的独立分辨率（0/未设 = 跟 `dpi` 相同）——
      调高它只让插图更清晰（对该页按高分辨率重渲一次再裁），不影响 OCR 速度；
    * 图片类 block 按 `image_labels` 裁剪落盘到 `images_dir`（None 则只报不存）；
      `min_image_size` 以下的裁图跳过（不产 `image`）；
    * `predictor` 可注入（测试桩：接 `list[PIL.Image]`，返回含 `.blocks` 的结果列表）。
    """
    options = opts or {}
    url = normalize_url(options.get("surya_url"))
    backend = str(options.get("surya_backend") or DEFAULT_BACKEND).strip().lower()
    if dpi is not None:
        raw_dpi = dpi
    else:
        raw_dpi = options.get("dpi") or 192
    use_dpi = max(48, min(400, int(raw_dpi)))
    # 插图裁剪的独立分辨率（image_dpi；0/未设 = 跟 dpi 相同）。检测框/blocks 都在
    # use_dpi 空间，但**插图位图不必同分辨率**；调高它时在裁剪处对该页按
    # crop_dpi 重渲一次、polygon 按比例映射（见下方“高清裁剪”）。
    try:
        raw_crop_dpi = int(options.get("image_dpi") or 0)
    except (TypeError, ValueError):
        raw_crop_dpi = 0
    crop_dpi = max(use_dpi, min(600, raw_crop_dpi)) if raw_crop_dpi > 0 else use_dpi
    labels = {str(s).strip() for s in (options.get("image_labels")
                                       or DEFAULT_IMAGE_LABELS) if str(s).strip()}
    pad = int(options.get("image_pad") or 0)
    min_size = int(options.get("min_image_size") or 0)
    t0 = time.time()

    rendered = render_pages(pdf_path, dpi=use_dpi, page_numbers=page_numbers)
    if not rendered:
        raise SuryaUnavailable("PDF 没有可渲染的页面")
    images = [img for _, img in rendered]
    if predictor is None:
        predictor = _make_predictor(url, backend)
    try:
        results = list(predictor(images))
    except SuryaUnavailable:
        raise
    except Exception as exc:
        raise SuryaUnavailable(f"Surya 2 推理失败（{url}）：{exc}") from exc
    if len(results) < len(images):
        raise SuryaUnavailable(
            f"Surya 2 只返回了 {len(results)}/{len(images)} 页结果，无法继续")

    # 插图高清裁剪：{1 基页码: PIL.Image|None} 惰性缓存 —— 只有某页真的有图片块
    # 要落盘时才按 crop_dpi 把该页重渲一次（没图的页不花这份钱）；渲染失败
    # 记 None（裁剪退回低清版本），不让裁剪拖垮整次解析。
    hires_pages: dict[int, object] = {}

    def _hires_page(pno: int):
        if pno not in hires_pages:
            img_hi = None
            if crop_dpi > use_dpi:
                try:
                    got = render_pages(pdf_path, dpi=crop_dpi, page_numbers=[pno])
                    img_hi = got[0][1] if got else None
                except Exception:
                    img_hi = None
            hires_pages[pno] = img_hi
        return hires_pages[pno]

    pages_out: list[dict] = []
    image_count = 0
    label_counts: dict[str, int] = {}
    for (pno, img), page_res in zip(rendered, results):
        blocks_out: list[dict] = []
        page_img_idx = 0
        for b in (getattr(page_res, "blocks", None) or []):
            label = str(getattr(b, "label", "") or "")
            raw_label = str(getattr(b, "raw_label", "") or "")
            conf = getattr(b, "confidence", None)
            bbox, crop = _crop_block(img, getattr(b, "polygon", None) or [], pad)
            if bbox is None:
                bbox = [0, 0, 0, 0]
            image_info = None
            # 图片类 block 一定是 skipped/空 html（模型不对图 OCR）——这里只认
            # 「标签在图片类集合里 + 裁图有效 + 不是 error 条目 + 尺寸达标」。
            if (crop is not None and label in labels and
                    not bool(getattr(b, "error", False)) and
                    min(crop.size) >= min_size):
                if images_dir is not None:
                    # 高清裁剪（image_dpi > dpi 时）：该页重渲后按 dpi 比例映射
                    # polygon 与边距，裁出更清晰的位图；几何数据（bbox 等）一律
                    # 不动 —— 前端按 bbox 显示，只是源图更清晰。筛选口径
                    # （min_image_size）仍按低清尺寸，保持既有行为不变。
                    save_crop = crop
                    hi = _hires_page(pno)
                    if hi is not None:
                        k = crop_dpi / use_dpi
                        poly = getattr(b, "polygon", None) or []
                        _, hi_crop = _crop_block(
                            hi, [(float(p[0]) * k, float(p[1]) * k) for p in poly],
                            round(pad * k))
                        if hi_crop is not None:
                            save_crop = hi_crop
                    fname = crop_filename(pno, page_img_idx)
                    path = Path(images_dir) / fname
                    path.parent.mkdir(parents=True, exist_ok=True)
                    save_crop.save(path)
                    image_info = {"file": fname, "bbox": bbox,
                                  "size": [int(save_crop.size[0]),
                                           int(save_crop.size[1])]}
                    page_img_idx += 1
                    image_count += 1
            blocks_out.append({
                "label": label,
                "raw_label": raw_label,
                "reading_order": getattr(b, "reading_order", None),
                "confidence": (round(float(conf), 4)
                               if isinstance(conf, (int, float)) else None),
                "skipped": bool(getattr(b, "skipped", False)),
                "error": bool(getattr(b, "error", False)),
                "bbox": [int(v) for v in bbox],
                "html": getattr(b, "html", "") or "",
                "image": image_info,
            })
            label_counts[label] = label_counts.get(label, 0) + 1
        # 按阅读顺序排（reading_order 缺失的排最前，保持稳定）
        blocks_out.sort(key=lambda x: (x["reading_order"]
                                       if isinstance(x["reading_order"], int) else 0))
        pages_out.append({"page": pno, "w": int(img.size[0]), "h": int(img.size[1]),
                          "blocks": blocks_out})

    return {
        "pages": pages_out,
        "dpi": use_dpi,
        "url": url,
        "backend": backend,
        "stats": {
            "surya_pages": len(pages_out),
            "surya_blocks": sum(len(p["blocks"]) for p in pages_out),
            "surya_images": image_count,
            "surya_labels": dict(sorted(label_counts.items(), key=lambda kv: -kv[1])),
            "surya_ms": int((time.time() - t0) * 1000),
        },
    }


# =====================================================================
#  块 HTML 工具（文本 / runs / 数学区间 / 标签 → kind）
# =====================================================================
# 块级闭合标签 → 换行；单元格闭合标签 → " | "（表格行的可读文本形态）
_BREAK_TAGS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "blockquote", "table", "section", "article", "ul", "ol",
               "figure", "figcaption", "pre"}
_CELL_TAGS = {"td", "th"}


class _HtmlWalker(HTMLParser):
    """把 Surya 的块 HTML 拆成「文本 / 换行 / 单元格 / 数学」原子流。

    `convert_charrefs=True`：实体（`&lt;` 等）在 handle_data 前就解码好。
    `<math>` 里的内容整体当 LaTeX 源码收集（Surya 2 的 `<math>` 是 LaTeX，
    不是 MathML —— 别按标签嵌套去解析）。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.atoms: list[tuple] = []
        self._b = self._i = self._up = 0
        self._math = 0
        self._math_buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "br":
            self.atoms.append(("break",))
            return
        if tag == "math":
            self._math += 1
            if self._math == 1:
                self._math_buf = []
            return
        if self._math:
            return
        if tag in ("b", "strong"):
            self._b += 1
        elif tag in ("i", "em"):
            self._i += 1
        elif tag == "sup":
            self._up += 1
        # <sub> 按普通字处理：阅读器只支持上标样式

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style"):
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag == "math":
            if self._math:
                self._math -= 1
                if self._math == 0:
                    self.atoms.append(("math", "".join(self._math_buf).strip()))
            return
        if self._math:
            return
        if tag in ("b", "strong"):
            self._b = max(0, self._b - 1)
        elif tag in ("i", "em"):
            self._i = max(0, self._i - 1)
        elif tag == "sup":
            self._up = max(0, self._up - 1)
        if tag in _BREAK_TAGS:
            self.atoms.append(("break",))
        elif tag in _CELL_TAGS:
            self.atoms.append(("cell",))

    def handle_data(self, data):
        if self._skip or not data:
            return
        if self._math:
            self._math_buf.append(data)
        else:
            self.atoms.append(("text", data, self._b > 0, self._i > 0,
                               self._up > 0))


def _runs_from(chars: list[str], styles: list[tuple[bool, bool, bool]]) -> list[dict]:
    """字符流 + 样式流 → runs（连续同样式合并；`"".join(t) == text`）。"""
    runs: list[dict] = []
    cur: tuple[bool, bool, bool] | None = None
    for ch, sty in zip(chars, styles):
        if runs and sty == cur:
            runs[-1]["t"] += ch
            continue
        run: dict = {"t": ch}
        if sty[0]:
            run["b"] = True
        if sty[1]:
            run["i"] = True
        if sty[2]:
            run["up"] = True
        runs.append(run)
        cur = sty
    return runs


def html_to_text_runs(html: str | None) -> tuple[str, list[dict], list[dict]]:
    """块 HTML →（纯文本, runs, 数学区间）。

    * 文本口径（前端渲染与翻译共用这一份，前后端同一算法）：
      块级闭合标签与 `<br>` 记换行、`</td>/</th>` 记 `" | "`；其余标签剥掉；
      空白折叠成单空格 / 单换行（行首行尾不留空白）。
    * `runs` 覆盖整段文本（`"".join(r["t"] …) == text`），带 `b` / `i` / `up`
      行内样式标记（前端 `styleRun` 直接吃这套标记）；
    * `math` 区间 `[{a, b, latex}]`：`<math>` 的 **LaTeX 源码**在文本里的 `[a, b)`
      字符区间 —— 前端据此在原位盖 KaTeX 覆盖层，复制时同样拿得到源码。
    """
    walker = _HtmlWalker()
    try:
        walker.feed(html or "")
        walker.close()
    except Exception:
        pass                     # HTML 不完整也不该让整篇解析失败

    out: list[str] = []
    styles: list[tuple[bool, bool, bool]] = []
    math_ranges: list[dict] = []
    pending = "none"             # none | space | break | cell

    def emit(ch: str, sty: tuple[bool, bool, bool]) -> None:
        out.append(ch)
        styles.append(sty)

    def flush_pending() -> None:
        nonlocal pending
        if pending == "break":
            if out and out[-1] != "\n":
                emit("\n", (False, False, False))
            pending = "none"
        elif pending == "cell":
            # 单元格分隔：" | "（首尾空格会被折叠逻辑吸收，不会堆空格）
            emit(" ", (False, False, False))
            emit("|", (False, False, False))
            emit(" ", (False, False, False))
            pending = "none"
        elif pending == "space":
            if out and out[-1] not in (" ", "\n"):
                emit(" ", (False, False, False))
            pending = "none"

    for atom in walker.atoms:
        kind = atom[0]
        if kind == "break":
            pending = "break"
            continue
        if kind == "cell":
            if pending in ("none", "space"):
                pending = "cell"
            continue
        if kind == "text":
            chars, sty = atom[1], (atom[2], atom[3], atom[4])
        else:                    # math
            chars, sty = atom[1], (False, False, False)
        if not chars:
            continue
        math_start = None
        for ch in chars:
            if ch.isspace():
                if pending in ("none", "space"):
                    pending = "space"
                continue
            flush_pending()      # 起始处的换行/空格先落下，公式区间从公式第一个字开始
            if kind == "math" and math_start is None:
                math_start = len(out)
            emit(ch, sty)
        if kind == "math" and math_start is not None and len(out) > math_start:
            math_ranges.append({"a": math_start, "b": len(out), "latex": atom[1]})

    text = "".join(out)
    return text, _runs_from(out, styles), math_ranges


# 标签规范化 → 渲染 kind。键是「去掉非字母字符后的小写形式」：
#   Page-Header → pageheader，Section-Header → sectionheader，Equation-Block → …
_KIND_BY_LABEL = {
    "text": "text", "paragraph": "text", "body": "text",
    "sectionheader": "heading", "heading": "heading", "title": "heading",
    "subheader": "heading", "subtitle": "heading",
    "caption": "caption", "footnote": "footnote", "endnote": "footnote",
    "equation": "formula", "formula": "formula", "equationblock": "formula",
    "formulablock": "formula", "mathblock": "formula",
    "listgroup": "list", "list": "list", "listitem": "list",
    "table": "table", "tableblock": "table",
    "pageheader": "header", "pagefooter": "footer",
    "header": "header", "footer": "footer", "pagenumber": "footer",
    "picture": "figure", "figure": "figure", "diagram": "figure",
    "image": "figure", "chemicalblock": "figure", "complexblock": "figure",
}


def _strip_tags(s: str) -> str:
    return _html_mod.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def label_kind(label: str | None, raw_label: str | None = None,
               html: str | None = None, text: str | None = None) -> str | None:
    """Surya 标签 → 归一化 kind（前端按它分渲染分支）；认不出的返回 None。

    顺序：canonical label → raw_label → HTML 形态（`<table>` / 纯 `<math>`）→
    有文本就按普通文本兜底。图片类返回 `"figure"`（由调用方用裁图落地）。
    """
    for name in (label, raw_label):
        key = re.sub(r"[^a-z]", "", str(name or "").lower())
        if key in _KIND_BY_LABEL:
            return _KIND_BY_LABEL[key]
    h = str(html or "")
    if "<table" in h.lower():
        return "table"
    if "<math" in h.lower() and not _strip_tags(h):
        return "formula"
    if (text or "").strip():
        return "text"
    return None


_H_TAG_RE = re.compile(r"<h([1-6])\b", re.I)
_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)")


def heading_level(html: str | None, text: str | None = None) -> int:
    """标题级别：优先 HTML 的 `<h1..h6>`，否则按编号（`1.2.3` → 3），默认 2。"""
    m = _H_TAG_RE.search(html or "")
    if m:
        return max(1, min(6, int(m.group(1))))
    m = _NUM_RE.match(text or "")
    if m:
        return max(1, min(4, m.group(1).count(".") + 1))
    return 2


_MATH_RE = re.compile(r"<math[^>]*>([\s\S]*?)</math>", re.I)


def latex_from_html(html: str | None) -> str:
    """equation 块 HTML → 第一条 `<math>` 里的 LaTeX 源码（没有则返回 ""）。

    Surya 2 的 `<math>` 里装的是 LaTeX（**不是 MathML**）；个别实现会套一层
    `<semantics>/<annotation>`，这里一并剥掉标签只留文字。
    """
    m = _MATH_RE.search(html or "")
    if not m:
        return ""
    body = re.sub(r"<[^>]+>", "", m.group(1))
    return _html_mod.unescape(body).strip()
