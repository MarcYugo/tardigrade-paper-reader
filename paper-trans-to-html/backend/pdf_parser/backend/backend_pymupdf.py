"""后端一 · 本地解析方案（PyMuPDF 版式 + local_parse_pipeline 小模型链路）。

链路（见 `local_parse_pipeline/README.md`）：

  1. PyMuPDF `page.get_text("dict")` 取字符/字体/行框；`merge_blocks` 按公式字体
     聚合出公式框（行内公式不会漏），再用上下行正文修正框边界；
  2. 按公式框裁剪页面渲染图，`texify` 转 LaTeX；
  3. OXR 的 D-FINE 版面分析模型（12 类、960 输入）在整页一次性给出所有区域：
     `Table` 交 `SLANet_plus` 转 HTML（单元格文字由流水线用 PDF 文字层回填），
     `Picture` 区域直接裁图，作为 `pages[].images[]` 的**唯一**来源（`pictures`）；
  4. `page.get_drawings()` 的矢量图形矩形（`figures`）**默认不启用**
     （`local_use_vector_figures=false`）：它们几乎都是表格线/公式框/图元碎片，
     直接裁图会把插图切碎；只在需要时作为补充打开。

本模块只做“流水线输出 → 阅读器 doc.json”的转换：

  * `pages[].texts[]` 是**逐行绝对定位**的块（`lines[].runs[]` 带字号/字体/颜色），
    公式字形替换成**一个占位空格 + 实际空档宽度**（`run.gap` / `run.gw`，前端
    `.rngap` 撑开），真公式由页面级 `pages[].formula_boxes[]` 覆盖层渲染；
  * **正文字符**的处理流程与 `paper-trans-to-html-v11.3-release2` 的 PyMuPDF 后端一致
    （见 `_local_text_blocks`）：`rawdict` 逐字符抽取 → 上标校正（`_is_super`）→
    行顶对齐正文（`_row_top`）→ 相邻 span 补**真空格**（`_insert_gap_spaces`）→
    按句重切块（`sentences.cut_blocks_by_sentence`，一句一块）→ 每行合并成一个正文
    run（`_merge_line_runs(keep_gaps=True)`，公式占位单独保留）；
  * `pages[].formula_boxes[]`：`{bbox, bbox_norm, latex, role, file, copy[]}`，
    前端按 `bbox_norm` 定位 KaTeX，失败时用 `file` 的裁图兜底；`copy[]` 记录
    “该公式在哪个文字块的哪个字符区间”，供选句/复制还原 LaTeX；
  * `pages[].images[]`：Figure/Picture 裁图落盘（`p{页:03d}_i{序号:03d}.png`），
    与 `/api/doc/{id}/img/{fname}` 口径一致；
  * 表格作为 `kind="table"` 的块下发 HTML，前端白名单渲染。

模型/依赖缺失时抛 `LocalParseUnavailable`（而不是悄悄回退别的后端，免得看起来
像解析成功了）。模型加载一次后会缓存在进程里，后续解析直接复用。
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import re
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from ..math_font import _accumulate_stats, _math_font_flags
from ..sentences import cut_blocks_by_sentence
from ..text_layer import (_CHAR_X, _CX_KEYS, _GAP_KEYS, _fill_gap_geom,
                         _insert_gap_spaces, _is_super, _join_lines,
                         _line_baseline, _line_char_x, _line_rot,
                         _merge_line_runs, _row_top, _span_char_x, _span_text,
                         _union_line_rect)


# =====================================================================
#  本地流水线与模型位置
# =====================================================================
_LOCAL_DIR = Path(__file__).resolve().parents[1] / "local_parse_pipeline"
_MODEL_DIR = _LOCAL_DIR / "models"
_TEXIFY_DIR = _MODEL_DIR / "texify-quantized-onnx"
# 公式引擎已改为 ONNX Runtime 版（`utils/load_texify.py`），权重是两个量化图
# + tokenizer.json，**不再**需要 `texify` / `transformers` 这两个第三方包。
_TEXIFY_FILES = ("encoder_model_quantized.onnx",
                 "decoder_model_merged_quantized.onnx",
                 "tokenizer.json")
# 版面分析（OXR D-FINE，12 类）：last.pth + metadata.json
# （引擎加载时会按 metadata 校验架构/类别/尺寸，以及 checkpoint 的字节数与 SHA-256）
_LAYOUT_DIR = _MODEL_DIR / "oxr_layout_analysis"
_LAYOUT_CHECKPOINT = _LAYOUT_DIR / "last.pth"
_SLANET_DIR = _MODEL_DIR / "SLANet_plus"

# 公式裁图的文件名从 `i9000` 起编：与插图（i0…）分开，避免同日录重名；
# `/api/doc/{id}/img/{fname}` 只校验 `p\d+_i\d+`，所以仍然是合法图片名。
_FORMULA_IMAGE_BASE = 9000

_DEFAULT_DPI = 300
_DEFAULT_PADDING = 2.0
_DEFAULT_MAX_TOKENS = 384
# 版面分析的置信度阈值（与 `load_oxr_layout` 默认值一致）
_DEFAULT_LAYOUT_CONFIDENCE = 0.3

_MONO_KEYS = ("courier", "consolas", "menlo", "mono", "sfmono", "dejavusansmono")
_SANS_KEYS = ("helvetica", "arial", "verdana", "tahoma", "trebuchet", "gill",
              "futura", "univers", "myriad", "calibri", "roboto", "inter",
              "notosans", "sans")
_BOLD_RE = re.compile(r"bold|black|heavy|semibold|demibold", re.I)
_ITALIC_RE = re.compile(r"italic|oblique|slant", re.I)


class LocalParseUnavailable(RuntimeError):
    """本地解析方案不可用（依赖/模型缺失，或流水线执行失败）。"""


# 模型加载一次、进程内复用；推理串行化（小模型不一定线程安全，解析本身也不多）。
_ENGINES: Optional[dict] = None
_ENGINE_LOCK = threading.Lock()
_PARSE_LOCK = threading.Lock()


# =====================================================================
#  可用性探测（供 options.status() 展示；不导入重依赖）
# =====================================================================
def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def local_status() -> dict:
    """本地解析方案的依赖/模型就绪情况。"""
    missing: list[str] = []
    if not _module_available("pymupdf") and not _module_available("fitz"):
        missing.append("PyMuPDF")
    # 注意：公式识别走 `utils/load_texify.py`（ONNX Runtime + int8 量化权重），
    # 这里**不能**再探测 `texify` 包 —— 官方 texify 库已弃用，装了反而会走老路径。
    for mod, label in (("torch", "torch"),
                       ("onnxruntime", "onnxruntime"),
                       ("tokenizers", "tokenizers"),
                       ("paddle", "paddlepaddle"), ("paddleocr", "paddleocr"),
                       ("cv2", "opencv-python-headless")):
        if not _module_available(mod):
            missing.append(label)
    if not _LOCAL_DIR.is_dir():
        missing.append("local_parse_pipeline/")
    if not all((_TEXIFY_DIR / name).is_file() for name in _TEXIFY_FILES):
        missing.append("models/texify-quantized-onnx/")
    # 版面分析（表格/插图定位）：权重与 metadata 缺一不可
    if not (_LAYOUT_CHECKPOINT.is_file() and (_LAYOUT_DIR / "metadata.json").is_file()):
        missing.append("models/oxr_layout_analysis/")
    if not (_SLANET_DIR / "inference.json").is_file():
        missing.append("models/SLANet_plus/")

    if missing:
        message = ("缺少 " + "、".join(missing) + "；请按 local_parse_pipeline/README.md "
                   "安装依赖并运行 bash download.sh 下载模型")
    else:
        message = "本地模型与依赖已就绪"
    return {
        "ready": not missing,
        "missing": missing,
        "message": message,
        "model_dir": str(_MODEL_DIR),
    }


def _import_pipeline():
    """导入 `local_parse_pipeline/pipeline.py`。

    该目录里的模块用的是 `from utils…` / `from merge_modules…` 这种**顶层导入**，
    所以必须先把目录放进 `sys.path`，否则只能按包路径导入会失败。
    """
    local = str(_LOCAL_DIR)
    if local not in sys.path:
        sys.path.insert(0, local)
    try:
        return importlib.import_module("pipeline")
    except Exception as exc:
        raise LocalParseUnavailable(
            f"无法导入本地解析流水线（{_LOCAL_DIR}）：{exc}"
        ) from exc


def _resolve_device(opts: dict) -> Optional[str]:
    value = str(opts.get("local_device") or "").strip().lower()
    return None if value in ("", "auto") else value


def _layout_device(device: Optional[str]) -> str:
    """版面分析引擎只认 `auto` / `cpu` / `cuda`（`cuda:0`、`mps` 之类要归一）。"""
    value = str(device or "").strip().lower()
    if value.startswith("cuda"):
        return "cuda"
    if value == "cpu":
        return "cpu"
    return "auto"


def _load_texify_engine(model_dir: str, device: Optional[str]):
    """加载 texify 公式引擎（`utils/load_texify.py`）。

    `pipeline.py` 只导入了 `load_pp_formulanet_s`，模块上已经没有 `load_texify`，
    所以这里按**文件路径**加载：既不受 `pipeline` 的导入列表影响，也不依赖
    `utils` 这个没有 `__init__.py` 的包名（可能被环境里同名的 `utils` 抢走）。
    """
    path = _LOCAL_DIR / "utils" / "load_texify.py"
    if not path.is_file():
        raise LocalParseUnavailable(f"找不到 texify 加载模块：{path}")
    spec = importlib.util.spec_from_file_location("_local_load_texify", path)
    if spec is None or spec.loader is None:
        raise LocalParseUnavailable(f"无法加载 texify 模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_texify(model_dir=model_dir, device=device)


def _load_engines(opts: dict) -> dict:
    """加载（或复用）texify / 版面分析（OXR D-FINE）/ SLANet 三个本地模型。"""
    global _ENGINES
    if _ENGINES is not None:
        return _ENGINES

    with _ENGINE_LOCK:
        if _ENGINES is not None:
            return _ENGINES

        status = local_status()
        if not status["ready"]:
            raise LocalParseUnavailable("本地解析方案不可用：" + status["message"])

        pipe = _import_pipeline()
        device = _resolve_device(opts)
        try:
            _ENGINES = {
                "pipeline": pipe,
                # 公式引擎：texify（`local_parse_pipeline/models/texify`），
                # 加载器从 `utils/load_texify.py` 取（pipeline 不再导出它）。
                "texify": _load_texify_engine(str(_TEXIFY_DIR), device),
                # 版面分析：OXR D-FINE（`utils/load_oxr_layout.py`），一次前向同时
                # 给出 Table / Picture / Text / Formula 等 12 类区域（像素坐标），
                # 流水线内会换算成 PDF 点坐标并去掉重复框。
                "layout": pipe.load_oxr_layout(checkpoint=str(_LAYOUT_CHECKPOINT),
                                               device=_layout_device(device)),
                "slanet": pipe.load_slanet(model_dir=str(_SLANET_DIR), device=device),
                "device": device or "auto",
            }
            # 模型版本等诊断信息（`info()` 不触发推理）
            _ENGINES["layout_info"] = _ENGINES["layout"].info()
        except Exception as exc:
            raise LocalParseUnavailable(
                "本地解析模型加载失败：" + str(exc)
                + "（若缺少依赖，请按 local_parse_pipeline/requirements.txt 安装）"
            ) from exc
        return _ENGINES


# =====================================================================
#  通用小工具
# =====================================================================
def _round_rect(value: Any) -> Optional[list[float]]:
    """`(x0,y0,x1,y1)` → 合法矩形列表；退化框返回 None。"""
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value[:4])
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)]


def _area(rect: list[float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _inter_area(a: list[float], b: list[float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def _overlap_ratio(rect: list[float], containers: list[list[float]]) -> float:
    area = _area(rect)
    if area <= 0:
        return 0.0
    best = 0.0
    for other in containers:
        best = max(best, _inter_area(rect, other) / area)
    return best


def _covered_ratio(rect: list[float], containers: list[list[float]]) -> float:
    """被若干矩形覆盖的比例（面积相加后夹到 1，重复覆盖不影响结论）。"""
    area = _area(rect)
    if area <= 0:
        return 0.0
    covered = sum(_inter_area(rect, other) for other in containers)
    return min(1.0, covered / area)


def _font_family(font: str) -> str:
    name = str(font or "").lower()
    if any(key in name for key in _MONO_KEYS):
        return "mono"
    if any(key in name for key in _SANS_KEYS):
        return "sans"
    return "serif"


def _color_hex(value: Any) -> str:
    """PyMuPDF 的 span color 是 0xRRGGBB 整数。"""
    try:
        return "#%06x" % (int(value) & 0xFFFFFF)
    except (TypeError, ValueError):
        text = str(value or "").strip()
        return text if re.fullmatch(r"#[0-9a-fA-F]{3,8}", text) else "#000000"


def _span_style(span: dict) -> dict:
    size = 0.0
    try:
        size = float(span.get("size") or 0.0)
    except (TypeError, ValueError):
        pass
    if size <= 0:
        rect = _round_rect(span.get("bbox"))
        size = (rect[3] - rect[1]) if rect else 10.0
    flags = 0
    try:
        flags = int(span.get("flags") or 0)
    except (TypeError, ValueError):
        pass
    font = str(span.get("font") or "")
    return {
        "s": round(max(1.0, size), 2),
        "fam": _font_family(font),
        "b": bool(flags & 16) or bool(_BOLD_RE.search(font)),
        "i": bool(flags & 2) or bool(_ITALIC_RE.search(font)),
        "up": bool(flags & 1),          # PyMuPDF flags bit0 = superscript
        "c": _color_hex(span.get("color")),
    }


def _line_body_size_baseline(spans: list[dict]) -> tuple[float, Optional[float]]:
    """估计一行的「正文字号 + 正文基线」，用来给公式框定基线（`formula_boxes[].base`）。

    基线按「字号² × 字符数」加权投票：正文既比公式下标大、字符又多，权重会明显压过
    公式小字形。窗口行（几乎没有正文）拿不到可靠结果时返回 `None`。

    基线会写进 `formula_boxes[].base`：前端按**基线**对齐公式（而不是按 bbox 顶边）
    —— 行框顶部会被行内公式的大字形抬得很高，按顶边定位会把公式放高、与正文错位。
    （正文行的基线走 `text_layer._line_baseline`，口径相同。）
    """
    votes: dict[float, float] = defaultdict(float)
    sizes_at: dict[float, dict[float, int]] = defaultdict(lambda: defaultdict(int))
    for span in spans:
        text = str(span.get("text") or "")
        if not text.strip():
            continue
        try:
            size = float(span.get("size") or 0.0)
            origin = span.get("origin") or ()
            oy = float(origin[1])
        except (TypeError, ValueError, IndexError):
            continue
        if size <= 0:
            continue
        bucket = round(oy * 2) / 2          # 0.5pt 一档，吸收浮点抖动
        votes[bucket] += size * size * len(text)
        sizes_at[bucket][round(size, 2)] += len(text)
    if not votes:
        return 0.0, None
    bucket = max(votes.items(), key=lambda kv: kv[1])[0]
    body_size = max(sizes_at[bucket].items(), key=lambda kv: kv[1])[0]

    # 桶中心只到 0.5pt，够判断“是不是同一条基线”，但排版要更准：
    # 把落在当选桶里的 span 的 origin 再按同样权重平均一次（误差 < 0.1pt）。
    num = weight = 0.0
    for span in spans:
        text = str(span.get("text") or "")
        if not text.strip():
            continue
        try:
            size = float(span.get("size") or 0.0)
            origin = span.get("origin") or ()
            oy = float(origin[1])
        except (TypeError, ValueError, IndexError):
            continue
        if size <= 0 or round(oy * 2) / 2 != bucket:
            continue
        w = size * size * len(text)
        num += oy * w
        weight += w
    baseline = (num / weight) if weight > 0 else bucket
    return body_size, baseline


def _normalize_latex(value: Any) -> str:
    """texify 返回的是 list；顺便剥掉外层 `$` / `\\[` 定界符与特殊 token。"""
    if isinstance(value, (list, tuple)):
        value = next((v for v in value if str(v or "").strip()), "")
    text = str(value or "").strip()
    text = re.sub(r"<\|[^>]*\|>", "", text).strip()
    if len(text) > 4 and text.startswith("$$") and text.endswith("$$"):
        text = text[2:-2].strip()
    elif len(text) > 4 and text.startswith("\\[") and text.endswith("\\]"):
        text = text[2:-2].strip()
    elif len(text) > 2 and text.startswith("$") and text.endswith("$"):
        text = text[1:-1].strip()
    if text.lower() in ("none", "null", "nan"):
        return ""
    return text


def _looks_like_prose(text: str) -> bool:
    """粗判一段“公式字形文本”其实是正文（用于避免整页被当成公式覆盖）。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) < 60:
        return False
    words = value.split(" ")
    letters = sum(ch.isalpha() for ch in value)
    symbols = sum(ch in "=+−-×÷∑∏∫√^_{}[]<>≤≥≈±∓→⇒∈∉⊂⊃" for ch in value)
    return (len(words) >= 12 and letters / max(1, len(value)) > 0.78
            and symbols <= 2)


def _decode_data_url(data: Any) -> bytes:
    value = str(data or "").strip()
    if not value:
        return b""
    match = re.match(r"^data:[^,]*;base64,(.*)$", value, re.S)
    payload = match.group(1) if match else value
    try:
        return base64.b64decode(payload, validate=False)
    except Exception:
        return b""


def _write_bytes(images_dir: Optional[Path], fname: str, data: bytes) -> bool:
    if images_dir is None or not data:
        return False
    target = Path(images_dir) / fname
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_bytes() != data:
            target.write_bytes(data)
        return True
    except OSError:
        return False


def _cache_key(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:12]


def _js_len(text: str) -> int:
    """JS 的 `String.length` 按 UTF-16 码元计数；公式复制区间必须与前端一致。"""
    return len(str(text).encode("utf-16-le")) // 2


# =====================================================================
#  插图：过滤 + 去重 + 落盘
# =====================================================================
def _dedupe_rects(items: list[tuple[list[float], dict]], keep_ratio: float = 0.85
                  ) -> list[tuple[list[float], dict]]:
    """大框优先；被已保留大框吃掉 85% 以上的小框丢弃（矢量图常有层层嵌套的框）。"""
    info = sorted(items, key=lambda pair: _area(pair[0]), reverse=True)
    kept: list[tuple[list[float], dict]] = []
    for rect, item in info:
        if any(_inter_area(rect, other) / max(1.0, _area(rect)) > keep_ratio
               for other, _ in kept):
            continue
        kept.append((rect, item))
    return kept


def _collect_page_images(page_dict: dict, table_rects: list[list[float]],
                         text_rects: list[list[float]], page_w: float, page_h: float,
                         opts: dict) -> list[tuple[list[float], dict, str, str]]:
    """插图候选：只用 `pictures`（版面分析给出的 `Picture` 区域），`figures` 默认关闭。

    流水线把插图拆成了两种来源（见 `local_parse_pipeline/pipeline.py`）：
      * `page_dict["pictures"]` —— `extract_tables()` 顺带返回的 `Figure` / `Picture`
        区域（**整幅插图**，带裁图；现在唯一的图片来源）；
      * `page_dict["figures"]` —— `extrac_figures_pictures()` 用 `page.get_drawings()`
        提取的矢量图形矩形（几乎都是表格线/公式框/图元碎片，裁出来是碎图）；
        只有 `local_use_vector_figures=true` 时才作为检测框之外的补充。
    """
    min_side = float(opts.get("local_min_figure_size") or 24.0)
    max_items = int(opts.get("local_max_figures") or 24)

    # ① 版面分析的 `Picture` 区域（流水线已改名为 Figure）：整幅插图，唯一的图片来源
    pictures: list[tuple[list[float], dict]] = []
    for item in page_dict.get("pictures") or []:
        rect = _round_rect(item.get("bbox"))
        if rect is None:
            continue
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w < min_side or h < min_side or w * h < min_side * min_side:
            continue
        if _overlap_ratio(rect, table_rects) > 0.5:
            continue                                  # 已由表格块渲染，不重复出图
        pictures.append((rect, item))
    pictures = _dedupe_rects(pictures)
    picture_rects = [rect for rect, _ in pictures]

    # ② get_drawings() 的矢量图形矩形：默认关闭（`local_use_vector_figures`）；
    #    打开时也只补检测框没盖到的部分，避免同一幅插图重复出图。
    figures: list[tuple[list[float], dict]] = []
    if bool(opts.get("local_use_vector_figures", False)):
        for item in page_dict.get("figures") or []:
            rect = _round_rect(item.get("bbox"))
            if rect is None:
                continue
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if w < min_side or h < min_side or w * h < min_side * min_side:
                continue
            if w * h > page_w * page_h * 0.85:      # 满页背景
                continue
            if _overlap_ratio(rect, table_rects) > 0.55:
                continue
            if _overlap_ratio(rect, picture_rects) > 0.55:
                continue                              # 已被检测框整幅覆盖
            if _covered_ratio(rect, text_rects) > 0.6:  # 框里主要是文字 → 不是插图
                continue
            figures.append((rect, item))
        figures = _dedupe_rects(figures)

    candidates = ([(rect, item, "Picture", "local_picture") for rect, item in pictures]
                  + [(rect, item, "Figure", "local_figure") for rect, item in figures])
    candidates.sort(key=lambda row: (round(row[0][1], 1), row[0][0]))
    return candidates[:max_items]


def _save_page_images(candidates: list[tuple[list[float], dict, str, str]],
                      images_dir: Optional[Path], page_no: int) -> list[dict]:
    out: list[dict] = []
    if images_dir is None:
        return out
    for idx, (rect, item, cls, source) in enumerate(candidates):
        data = _decode_data_url(item.get("image"))
        if not data:
            continue
        fname = f"p{page_no:03d}_i{idx:03d}.png"
        if not _write_bytes(images_dir, fname, data):
            continue
        out.append({
            "x": rect[0], "y": rect[1],
            "w": round(rect[2] - rect[0], 3), "h": round(rect[3] - rect[1], 3),
            "file": fname, "source": source, "class_name": cls,
            "cache_key": _cache_key(data),
        })
    return out


# =====================================================================
#  公式框：收集（跨 block 去重）→ 裁图落盘 → 判定能否覆盖
# =====================================================================
def _box_quality(box: dict) -> int:
    if _normalize_latex(box.get("latex")) or str(box.get("image") or "").strip():
        return 2
    return 1


def _collect_formula_boxes(blocks: list) -> dict[int, dict]:
    """把 `formula_boxes` 按 id 去重，并记下每个框的字号。

    跨 block 的公式框会挂在多个 block 上；`fix_formula_box_trans_latex()` 只在
    第一次遇到时把它换成识别结果，其余 block 上仍是未识别的原始框 —— 所以
    同一 id 取“质量更高”的那份（带 latex/image 的）。
    """
    boxes: dict[int, dict] = {}
    sizes: dict[int, float] = defaultdict(float)
    bid_spans: dict[int, list[dict]] = defaultdict(list)
    for block in blocks:
        if int(block.get("type") or 0) != 0:
            continue
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                bid = span.get("formula_box_id")
                if bid is None:
                    continue
                try:
                    bid_i = int(bid)
                except (TypeError, ValueError):
                    continue
                sizes[bid_i] = max(sizes[bid_i], float(span.get("size") or 0))
                bid_spans[bid_i].append(span)
        for box in block.get("formula_boxes") or []:
            if not isinstance(box, dict) or box.get("id") is None:
                continue
            try:
                bid = int(box["id"])
            except (TypeError, ValueError):
                continue
            if bid not in boxes or _box_quality(box) > _box_quality(boxes[bid]):
                boxes[bid] = box

    out: dict[int, dict] = {}
    for bid, box in boxes.items():
        item = dict(box)
        item["bbox"] = _round_rect(box.get("bbox"))
        item["latex"] = _normalize_latex(box.get("latex"))
        item["size"] = round(sizes.get(bid) or 10.0, 2)
        _body_size, base_y = _line_body_size_baseline(bid_spans.get(bid) or [])
        item["base"] = round(base_y, 3) if base_y is not None else None
        item["role"] = (str(box.get("role") or "").strip()
                        or ("inline" if box.get("inline") else "display"))
        item["_drawable"] = False
        item["_file"] = ""
        out[bid] = item
    return out


def _save_formula_crops(box_map: dict[int, dict], images_dir: Optional[Path],
                        page_no: int, keep: bool) -> None:
    if not keep or images_dir is None:
        return
    ordered = sorted(box_map.items(),
                     key=lambda kv: ((kv[1].get("bbox") or [0, 0, 0, 0])[1],
                                     (kv[1].get("bbox") or [0, 0, 0, 0])[0]))
    for idx, (_bid, box) in enumerate(ordered):
        data = _decode_data_url(box.get("image"))
        if not data:
            continue
        fname = f"p{page_no:03d}_i{_FORMULA_IMAGE_BASE + idx:04d}.png"
        if _write_bytes(images_dir, fname, data):
            box["_file"] = fname
            box["_cache"] = _cache_key(data)


def _mark_drawable(box: dict, page_w: float, page_h: float) -> bool:
    """这个公式框能不能用覆盖层渲染（有 latex 或裁图，且不是整页/正文误检）。"""
    bbox = box.get("bbox")
    if bbox is None or not (box.get("latex") or box.get("_file")):
        return False
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if w < 2 or h < 2 or w * h > page_w * page_h * 0.5:
        return False
    role = str(box.get("role") or "inline")
    if role == "inline" and h > page_h * 0.5:
        return False
    pdf_text = str(box.get("pdf_text") or box.get("text") or "")
    if role == "inline" and w > page_w * 0.35 and _looks_like_prose(pdf_text):
        return False
    return True


def _build_formula_box_list(box_map: dict[int, dict], copy_map: dict[int, list[dict]],
                            page_w: float, page_h: float) -> list[dict]:
    out: list[dict] = []
    ordered = sorted(box_map.items(),
                     key=lambda kv: ((kv[1].get("bbox") or [0, 0, 0, 0])[1],
                                     (kv[1].get("bbox") or [0, 0, 0, 0])[0]))
    for bid, box in ordered:
        if not box.get("_drawable"):
            continue
        bbox = box["bbox"]
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        role = str(box.get("role") or "inline")
        copies = sorted(
            {(c.get("id"), int(c.get("start") or 0), int(c.get("end") or 0))
             for c in copy_map.get(bid) or []
             if c.get("id") and int(c.get("end") or 0) > int(c.get("start") or 0)},
            key=lambda row: (str(row[0]), row[1]),
        )
        out.append({
            "id": bid,
            "role": role,
            "cls": ("InlineFormula" if role == "inline"
                    else "DisplayedFormulaBlock"),
            "bbox": bbox,
            "bbox_norm": [
                round(((bbox[0] + bbox[2]) / 2.0) / max(1.0, page_w), 6),
                round(((bbox[1] + bbox[3]) / 2.0) / max(1.0, page_h), 6),
                round(w / max(1.0, page_w), 6),
                round(h / max(1.0, page_h), 6),
            ],
            "latex": box.get("latex") or "",
            "file": box.get("_file") or "",
            "cache_key": box.get("_cache") or "",
            "size": box.get("size") or 10.0,
            "base": box.get("base"),
            "text": str(box.get("pdf_text") or "")[:200],
            "copy": [{"id": row[0], "start": row[1], "end": row[2]}
                     for row in copies],
        })
    return out


# =====================================================================
#  表格：HTML 块
# =====================================================================
def _build_table_items(page_dict: dict, page_no: int,
                       page_w: float, page_h: float) -> tuple[list[dict], list[list[float]]]:
    items: list[dict] = []
    rects: list[list[float]] = []
    for idx, table in enumerate(page_dict.get("tables") or []):
        rect = _round_rect(table.get("bbox"))
        if rect is None:
            continue
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w < 8 or h < 8 or w * h > page_w * page_h * 0.98:
            continue
        html = table.get("html_table")
        # 表格字号：流水线按 PDF 文字层量出的实际字号（前端当 font-size 用；
        # 缺省 0 → 前端退回 10pt）。单元格文字也是流水线用文字层回填的。
        size = 0.0
        if isinstance(html, dict):
            try:
                size = float(html.get("size") or 0.0)
            except (TypeError, ValueError):
                size = 0.0
            html = html.get("html")
        html = str(html or "").strip()
        if "<table" not in html.lower():
            continue
        items.append({
            "id": f"p{page_no}t{idx}",
            "kind": "table",
            "label": "table",
            "x": rect[0], "y": rect[1],
            "w": round(w, 3), "h": round(h, 3),
            "size": round(size, 2),
            "text": "",                  # 表格不参与翻译/切句
            "lines": [],                  # selftest 等工具按 lines 统计
            "html": html,
            "score": round(float(table.get("det_score") or 0.0), 4),
        })
        rects.append(rect)
    return items, rects


# =====================================================================
#  文字块：PyMuPDF 正文字符 → 行/run（与 v11.3 的 pymupdf 后端同一套流程）
#
#  五个步骤（顺序不能换）：
#    ① `_local_text_blocks` 抽字符：**逐字符**（`rawdict` 的 `chars[*].bbox`）、
#       上标校正（`text_layer._is_super`）、行顶对齐正文（`_row_top`），
#       公式字形**不进来**——按公式框换成「一个空格 + 原宽」的占位 run（`gap`/`gw`）；
#    ② `text_layer._insert_gap_spaces` 补间距：相邻 span 之间有视觉空隙却没有空格
#       字符时补一个**真空格**（公式占位两侧不补）；
#    ③ `_line_char_x` 按 run 几何重建逐字符左边界（`_CHAR_X`）；
#    ④ `sentences.cut_blocks_by_sentence` 按句重切块（一句一块）；
#    ⑤ `_merge_line_runs(keep_gaps=True)` 把每行的正文 run 合并成「一段一个」
#       （颜色边界保留，公式占位单独留）。
#
#  ⚠️ **旋转（竖排/倒排）行**（PDF 里 `line["dir"] != (1,0)`，典型是 arXiv 侧边章）：
#     它们的 bbox 是「窄而高」的竖条、逐字符 x 全相同、origin 在竖条底部，不能当横排
#     行用。`text_layer._line_rot` 会额外下发 `rot/ox/oy/rw/rh`（未旋转时的盒子 +
#     CSS 旋转角），前端按它旋转渲染；这些行**不进句子的流**（见 `sentences`），
#     整块带 `vertical: True`。
# =====================================================================
def _local_text_source_blocks(page, content_blocks: list) -> list[dict]:
    """正文字符的来源：有 PyMuPDF `page` 就重新用 `rawdict` 抽（逐字符 bbox 精确），
    否则退回流水线给的 `dict` 块（`chars` 缺失时 `_span_char_x` 会按 span 框插值）。
    """
    if page is not None:
        try:
            raw = page.get_text("rawdict") or {}
            blocks = [b for b in (raw.get("blocks") or [])
                      if int(b.get("type") or 0) == 0]
            if blocks:
                return blocks
        except Exception:
            pass
    return [b for b in content_blocks or [] if int(b.get("type") or 0) == 0]


def _formula_span_ids(content_blocks: list) -> tuple[list, dict]:
    """流水线给每个公式 span 打的 `formula_box_id`。

    返回 `(按 block/line/span 下标对齐的 [(bid, bbox)], bbox→bid 兜底表)`。
    下标对齐只在「两次抽法的 span 顺序一致」时成立（同一页的 `dict`/`rawdict`
    本来逐 span 一致），所以取用时会再比一次 bbox；对不上就查 bbox 表。
    """
    table: list = []
    by_box: dict[tuple, int] = {}
    for blk in content_blocks or []:
        if int(blk.get("type") or 0) != 0:
            continue
        blines: list = []
        for ln in blk.get("lines") or []:
            ids: list = []
            for span in ln.get("spans") or []:
                try:
                    bid = span.get("formula_box_id")
                    bid = int(bid) if bid is not None else None
                except (TypeError, ValueError):
                    bid = None
                rect = _round_rect(span.get("bbox"))
                ids.append((bid, tuple(rect) if rect else None))
                if bid is not None and rect:
                    by_box[tuple(rect)] = bid
            blines.append(ids)
        table.append(blines)
    return table, by_box


def _formula_bid(table: list, by_box: dict, bi: int, li: int, si: int,
                 span: dict) -> Optional[int]:
    """取一个 span 的公式框 id（下标对齐优先，bbox 兜底）。"""
    rect = _round_rect(span.get("bbox"))
    key = tuple(rect) if rect else None
    if bi < len(table) and li < len(table[bi]) and si < len(table[bi][li]):
        bid, bbox = table[bi][li][si]
        if bbox is None or key is None or bbox == key:
            return bid
    return by_box.get(key) if key else None


def _inside_boxes(rect: list[float], boxes: dict[int, list[float]]) -> Optional[int]:
    """矩形中心落在哪个行内公式框里（检测框常只标到公式的一部分字形）。"""
    cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    for bid, bb in boxes.items():
        if bb[0] - 1 <= cx <= bb[2] + 1 and bb[1] - 1 <= cy <= bb[3] + 1:
            return bid
    return None


def _gap_run(bid: int, x0: float, x1: float, size: float = 10.0,
             fam: str = "serif") -> dict:
    """公式占位 run：**一个空格字符** + 原宽(`gw`)；前端 `.rngap` 按 `gw` 撑开。

    宽度由解析侧给出（`gw`，pt）：文字用替代字体排版时宽度会漂移，行内公式框
    要靠这个占位块定位（见前端 `_anchorFormulaX`），所以不能只靠自然字宽。
    """
    return {"t": " ", "s": round(float(size or 10.0), 2), "fam": fam,
            "b": False, "i": False, "up": False, "c": "#000000",
            "gap": bid, "gw": round(max(0.5, float(x1) - float(x0)), 2),
            "x0": float(x0), "x1": float(x1), "_cx": [float(x0)],
            "_cx_end": float(x1)}


def _text_run(span: dict, text: str, base: Optional[float]) -> dict:
    """一个正文 span → run（样式 + 临时几何 + 逐字符左边界）。"""
    size = 0.0
    try:
        size = float(span.get("size") or 0.0)
    except (TypeError, ValueError):
        size = 0.0
    if size <= 0:
        rect = _round_rect(span.get("bbox"))
        size = (rect[3] - rect[1]) if rect else 10.0
    try:
        flags = int(span.get("flags") or 0)
    except (TypeError, ValueError):
        flags = 0
    font = str(span.get("font") or "")
    run = {
        "t": text,
        "s": round(max(1.0, size), 1),
        "f": font,                      # 只服务于公式字体判据，出页面前剥掉
        "fam": _font_family(font),
        "b": bool(flags & 16),
        "i": bool(flags & 2),
        "up": _is_super(span, flags, base),
        "c": _color_hex(span.get("color")),
    }
    rect = _round_rect(span.get("bbox")) or [0.0, 0.0, 0.0, 0.0]
    org = span.get("origin")
    run.update({"x0": float(rect[0]), "x1": float(rect[2]),
                "oy": float(org[1]) if org else float(rect[1])})
    got = _span_char_x(span, text)
    if got is not None:
        run["_cx"], run["_cx_end"] = list(got[0]), float(got[1])
    return run


# 公式框/文字落在表格、插图里 → **不参与正文占位，也不下发覆盖层**：那部分内容
# 已经由表格 HTML / 插图本身呈现，再画一遍就是重影（典型：表头 `Top-k` 的 `k`、
# 竖排的 `Top1..Top5` 标签列、图表里的 `h=1` / `h=5` 图例）。
# 判据：矩形中心在容器内，或被**单个**容器覆盖到这个比例（0.45 能拦住"半个框压在
# 图框边上"的图表标签，实测 p6 的 `h=5` 只重合 0.49、中心点还差 0.1pt 在框外）。
_BOX_IN_GRAPHIC = 0.45
# 公式框与一行的横向间隙超过它 → 判为「不在同一栏」。兜底找归属行时用。
_BOX_COL_TOL = 12.0


def _center_in(rect: list[float], containers: list[list[float]]) -> bool:
    """矩形中心点是否落在任一容器里。"""
    if not rect:
        return False
    cx, cy = (float(rect[0]) + float(rect[2])) / 2.0, (float(rect[1]) + float(rect[3])) / 2.0
    for other in containers or []:
        if other[0] <= cx <= other[2] and other[1] <= cy <= other[3]:
            return True
    return False


def _in_graphic(rect: list[float], table_rects: list[list[float]],
                image_rects: list[list[float]],
                ratio: float = _BOX_IN_GRAPHIC) -> bool:
    """矩形是不是表格/插图里的内容（中心点在内，或被单个容器覆盖 ≥`ratio`）。"""
    if not rect:
        return False
    containers = list(table_rects or []) + list(image_rects or [])
    if not containers:
        return False
    if _center_in(rect, containers):
        return True
    area = _area(rect)
    return bool(area > 0 and _overlap_ratio(rect, containers) >= ratio)


def _same_column(line_rect: list[float], box: list[float],
                 tol: float = _BOX_COL_TOL) -> bool:
    """行的横带与公式框是否在同一栏（x 相交，或左右间隙不超过 `tol`）。"""
    gap = max(float(box[0]) - float(line_rect[2]),
              float(line_rect[0]) - float(box[2]), 0.0)
    return gap <= tol


def _hide_graphic_formula_boxes(boxes: list[dict],
                                table_rects: list[list[float]],
                                image_rects: list[list[float]]) -> tuple[list[dict], int]:
    """表格/插图里的公式框不下发（内容由表格 HTML 与插图本身呈现）。

    返回 `(留下的框, 被隐藏的个数)`。前端 `buildFormulaBoxes` 只画 `formula_boxes`
    里的条目，所以这里剔掉就不会再出现 `Top-kᵏ` / `Top1 Top1` 这类重影。
    """
    kept: list[dict] = []
    hidden = 0
    for item in boxes or []:
        rect = _round_rect(item.get("bbox"))
        if rect is not None and _in_graphic(rect, table_rects, image_rects):
            hidden += 1
            continue
        kept.append(item)
    return kept, hidden


def _local_text_blocks(page, content_blocks: list, *, page_no: int,
                       table_rects: list[list[float]],
                       image_rects: list[list[float]],
                       box_map: dict[int, dict],
                       opts: Optional[dict] = None) -> tuple[list[dict], dict]:
    """纯 PyMuPDF 抽一页的**正文字符**（公式字形换成占位 run）；返回 `(块, 统计)`。

    公式框来自 `local_parse_pipeline`（`box_map`）：
      * **行内公式**（role=inline）：整个框换成**一个**占位 run。占位落在
        「框左边紧挨着的那条正文行」上（不是字形最多的那条 —— 一条公式常被
        PyMuPDF 拆成好几条行碎片，字形最多的那条可能从公式**中间**开始），
        宽度 = 「前一段正文右边界 → 后一段正文左边界」（不超过框右边），
        这样公式左边的字不会被挤、右边也不会压住后文。框里的字形（含没打 id
        的残余）全部丢掉，其余行碎片按留下的文字重算行左边/行宽。
      * **独立公式**（role=display）：同一行里打了 id 的 span 合成一个占位
        （宽度 = 这些 span 的并集），落在占位 x 区间里的残余字形一起丢掉。
    """
    opts = opts or {}
    blocks_in = _local_text_source_blocks(page, content_blocks)
    span_ids, span_id_box = _formula_span_ids(content_blocks)
    drawable = {bid for bid, box in box_map.items() if box.get("_drawable")}
    # 图表里的公式框：**不参与正文占位**（见 `_BOX_IN_GRAPHIC` 注释）。
    in_graphic = {bid for bid in drawable
                  if box_map[bid].get("bbox")
                  and _in_graphic(list(box_map[bid]["bbox"]), table_rects, image_rects)}
    inline_boxes = {bid: list(box["bbox"]) for bid, box in box_map.items()
                    if bid in drawable and bid not in in_graphic
                    and str(box.get("role") or "") == "inline"
                    and box.get("bbox")}
    display_ids = {bid for bid in drawable
                   if bid not in in_graphic and bid not in inline_boxes}

    drop_in_images = bool(opts.get("drop_text_in_images", True))
    try:
        image_overlap = min(1.0, max(0.0, float(opts.get("text_in_image_overlap", 0.6))))
    except (TypeError, ValueError):
        image_overlap = 0.6

    # ① 先挑「要保留的行」，再给每个行内公式框找**归属行**
    #    ⚠️ 只看纵向重叠会挑错：双栏页里另一栏的行与公式框的 y 完全一样，
    #    而「公式框自己那几个字形所在的 PyMuPDF 行」才是唯一确定的归属行。
    #    所以按「该框的**字形**落在哪条行里最多」来定，纵向重叠只当兜底。
    kept: list[tuple[int, dict, list[tuple[int, dict, list[float]]]]] = []
    for bi, blk in enumerate(blocks_in):
        rows: list[tuple[int, dict, list[float]]] = []
        for li, ln in enumerate(blk.get("lines") or []):
            rect = _round_rect(ln.get("bbox"))
            if rect is None:
                continue
            if _covered_ratio(rect, table_rects) >= 0.55:
                continue                              # 表格里的文字：由表格 HTML 负责
            if drop_in_images and image_rects and \
                    (_covered_ratio(rect, image_rects) >= image_overlap
                     or _center_in(rect, image_rects)):
                continue                              # 图内文字（避免与插图叠字）
            rows.append((li, ln, rect))
        if rows:
            kept.append((bi, blk, rows))

    glyph_votes: dict[int, dict[tuple[int, int], int]] = {}
    line_rects: dict[tuple[int, int], list[float]] = {}
    for gi, (bi, _blk, rows) in enumerate(kept):
        for (li, ln, rect) in rows:
            line_rects[(gi, li)] = rect
            for si, span in enumerate(ln.get("spans") or []):
                srect = _round_rect(span.get("bbox"))
                if srect is None:
                    continue
                text = _span_text(span)
                if not text.strip():
                    continue
                bid = _formula_bid(span_ids, span_id_box, bi, li, si, span)
                if bid not in inline_boxes:
                    bid = _inside_boxes(srect, inline_boxes)
                    if bid is None:
                        continue
                    if _inside_boxes(srect, {bid: inline_boxes[bid]}) is None:
                        continue                          # 只是压到框边，不是框里的字
                votes = glyph_votes.setdefault(bid, {})
                votes[(gi, li)] = votes.get((gi, li), 0) + len(text.strip())

    # 「公式前面紧挨着的正文」：右边界落在框左边界前 `_PRE_TOL` 以内的 span。
    # 为什么不能只看「公式字形最多的那条行」：一条行内公式常被 PyMuPDF 拆成好几条
    # 行碎片（分子/分母/上下限的基线各不相同），字形最多的那条**可能从公式中间开始**
    # （实测 `x = (-b±√…)/2a` 的归属行从 x=231 开始，公式框却从 x=188 开始）。占位
    # 落进那种行，整条公式就被推到右边、压住后面的单词。
    _PRE_TOL = 12.0
    pre_ends: dict[int, dict[tuple[int, int], float]] = {}
    for gi, (bi, _blk, rows) in enumerate(kept):
        for (li, ln, rect) in rows:
            if not inline_boxes:
                continue
            for si, span in enumerate(ln.get("spans") or []):
                srect = _round_rect(span.get("bbox"))
                if srect is None:
                    continue
                text = _span_text(span)
                if not text.strip():
                    continue
                for bid, bb in inline_boxes.items():
                    # 行必须从框的左边开始、并且一直伸进框里（说明公式的左边字形就
                    # 排在这条行上）；只伸到框左边附近不算，避免把隔壁栏的行抢过来。
                    if rect[0] > bb[0] - 0.5 or rect[2] < bb[0] - 2.0:
                        continue
                    if not (bb[0] - _PRE_TOL <= srect[2] <= bb[0] + 1.5):
                        continue
                    ov = min(srect[3], bb[3]) - max(srect[1], bb[1])
                    if ov < 0.5 * max(0.5, srect[3] - srect[1]):
                        continue
                    if _inside_boxes(srect, inline_boxes) is not None:
                        continue                          # 公式字形（自己或隔壁框里的）
                    ends = pre_ends.setdefault(bid, {})
                    ends[(gi, li)] = max(ends.get((gi, li), -1e9), srect[2])

    owners: dict[int, tuple[int, int]] = {}
    for bid, bb in inline_boxes.items():
        pres = pre_ends.get(bid) or {}
        if pres:
            # ① 紧挨着公式左边的那条行（右边界离框左边最近；并列取靠上的）
            owners[bid] = max(pres.items(),
                              key=lambda kv: (kv[1], -kv[0][0], -kv[0][1]))[0]
            continue
        votes = glyph_votes.get(bid) or {}
        if votes:
            # ② 公式从行首开始（前面没有正文）：含公式字形、行左边最贴近框左边的行
            owners[bid] = min(
                votes,
                key=lambda k: (abs((line_rects.get(k) or [bb[0]])[0] - bb[0]),
                               -votes[k], k[0], k[1]))
            continue
        # 兜底：**同栏**且纵向重叠最多。
        # ⚠️ 不能只比纵向距离：公式框的字形行被表格/插图过滤掉之后（`kept` 里没有它），
        # 另一栏里恰好压着同一条 y 带的行就会赢（实测右栏 "quency Memory…" 抢走了
        # 左栏表头 `k` 的占位 → 整行左边被拉到 x=73.95，压在表格上）。
        best, best_ov = None, 0.0
        for gi, (_bi, _blk, rows) in enumerate(kept):
            for (li, _ln, rect) in rows:
                if not _same_column(rect, bb):
                    continue                          # 别栏的行：宁可丢占位也不跨栏挂
                ov = min(rect[3], bb[3]) - max(rect[1], bb[1])
                if ov > best_ov:
                    best, best_ov = (gi, li), ov
        if best is not None:
            owners[bid] = best

    # ② 逐行抽字符 → run（公式字形换成占位）
    texts: list[dict] = []
    for gi, (bi, blk, rows) in enumerate(kept):
        lines: list[dict] = []
        for (li, ln, lrect) in rows:
            sps = ln.get("spans") or []
            base = _line_baseline(sps)
            runs: list[dict] = []
            inline_here: list[int] = []
            dropped = False                 # 本行有没有删掉公式字形（行框要重算）
            for si, span in enumerate(sps):
                text = _span_text(span)
                if not text:
                    continue
                srect = _round_rect(span.get("bbox")) or lrect
                bid = _formula_bid(span_ids, span_id_box, bi, li, si, span)
                if bid is not None and bid in drawable:
                    if bid in inline_boxes:
                        if owners.get(bid) == (gi, li) and bid not in inline_here:
                            inline_here.append(bid)
                        dropped = True
                        continue                      # 行内公式的字形：整框占位后不再画
                    if bid in display_ids:
                        dropped = True
                        prev = next((r for r in runs if r.get("gap") == bid), None)
                        if prev is not None:          # 同一条公式在本行不连续 → 并成一段
                            prev["x1"] = max(float(prev["x1"]), float(srect[2]))
                            prev["gw"] = round(prev["x1"] - float(prev["x0"]), 2)
                            prev["_cx_end"] = prev["x1"]
                            continue
                        style = _span_style(span)
                        runs.append(_gap_run(bid, srect[0], srect[2],
                                             style.get("s"), style.get("fam")))
                        continue
                if _inside_boxes(srect, inline_boxes) is not None:
                    dropped = True
                    continue                          # 行内框里没打 id 的残余字形
                runs.append(_text_run(span, text, base))

            # 行内公式：占位补在**本行**（按 x 排序插进去）。
            # 占位宽度 = 「前一段正文的右边界 → 后一段正文的左边界」，但不超过公式框
            # 右边：框本身常常比 PyMuPDF 的行框宽（分式/根号落在别的行碎片上），
            # 按整框宽写进一条窄碎片会把行撑爆（前端整行 scaleX 压扁）并压住后文。
            for bid, own in owners.items():
                if own == (gi, li) and bid not in inline_here:
                    inline_here.append(bid)               # 归属行本身没有公式字形也要占位
            for bid in sorted(inline_here,
                              key=lambda b: float(inline_boxes[b][0])):
                bb = inline_boxes[bid]
                if not _same_column(lrect, bb):
                    continue                          # 框不在本行这一栏：不挂占位
                style = box_map.get(bid) or {}
                idx = next((k for k, r in enumerate(runs)
                            if float(r.get("x0") or 0.0) > float(bb[0])), len(runs))
                # 占位左边界 = 前面最后一个 run 的右边界（公式/文字都算）；
                # 但不往左拉超过 6pt —— 两端对齐的行里那种大空格不该算进公式宽度，
                # 否则公式会在一个过宽的盒子里居中、看起来往右跑。
                ends = [float(r["x1"]) for r in runs[:idx] if r.get("x1") is not None]
                left = max(ends) if ends else float(bb[0])
                left = max(left, float(bb[0]) - 6.0)
                if left > float(bb[0]) + 1.5:
                    left = float(bb[0])          # 前一段正文压进框里：从框左边留位
                right = float(bb[2])
                for r in runs[idx:]:
                    if r.get("x0") is None:
                        continue
                    nxt = float(r["x0"])
                    if nxt > left + 0.5:
                        right = min(right, nxt)   # 后一段正文（或另一个占位）压过来
                        break
                placeholder = _gap_run(bid, left, max(left + 0.5, right),
                                       style.get("size") or 10.0)
                runs.insert(idx, placeholder)
                dropped = True

            # 独立公式占位区间里的残余字形（公式里用正文字体排的标识符）一并丢掉
            gaps = [r for r in runs if r.get("gap") in display_ids]
            if gaps:
                before = len(runs)
                runs = [r for r in runs
                        if r.get("gap") is not None
                        or not any(float(r["x1"]) > float(g["x0"]) + 0.5
                                   and float(r["x0"]) < float(g["x1"]) - 0.5
                                   for g in gaps)]
                dropped = dropped or len(runs) != before
            if not runs:
                continue                              # 整行都在公式里 → 不留空壳行

            lx0, ly0, lx1, ly1 = ln["bbox"]
            # 旋转（竖排/倒排）行：`_row_top` 给的是文字的**末端**，必须留 bbox 顶；
            # 并且要把「未旋转时的盒子 + 旋转角」一起下发（见 `text_layer._line_rot`）。
            rot = _line_rot(ln, (lx0, ly0, lx1, ly1))
            top = float(ly0) if rot else float(_row_top(sps, ly0))
            if dropped:
                # 行框是按**原始字形**算的：公式字形删掉后，剩下的文字可能从行中间
                # 开始（分母/根号那几截在别的行碎片上）。行左边/宽度必须按留下的 run
                # 重算 —— 否则文字会被拉回左边压住公式，行宽也会偏窄（前端会误判
                # 超宽 → 整行 scaleX 压扁，公式前的字看起来像被挤扁了）。
                xs = [(float(r["x0"]), float(r["x1"])) for r in runs
                      if r.get("x0") is not None and r.get("x1") is not None]
                if xs:
                    lx0 = min(p[0] for p in xs)
                    lx1 = max(p[1] for p in xs)
            line = {"x": float(lx0), "y": top,
                    "w": max(1.0, float(lx1) - float(lx0)),
                    "h": float(ly1) - top, "runs": runs}
            if rot:
                line.update(rot)                       # rot/ox/oy/rw/rh（前端旋转渲染）
            if base is not None:
                line["base"] = float(base)            # 前端按基线定位（旋转行不用）
            lines.append(line)

        if not lines:
            continue
        text = _join_lines(lines)
        if not text and not any(r.get("gap") for ln in lines for r in ln["runs"]):
            continue                                  # 只剩空白的块不留
        x0, y0, x1, y1 = _union_line_rect(lines)
        blk_item = {"id": f"p{page_no}b{bi}",
                    "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                    "text": text, "lines": lines}
        if all(ln.get("rot") for ln in lines):
            # 整块都是旋转行（页边水印/竖排标题）：不参与句子的流（见 sentences）
            blk_item["vertical"] = True
        texts.append(blk_item)

    # ③ 补间距空格（公式占位两侧不补）—— 必须在算字符偏移之前
    cm_on, weak_on = _math_font_flags(texts) if texts else (False, False)
    if _insert_gap_spaces(texts, cm_on, weak_on):
        _fill_gap_geom(texts)
        for blk in texts:
            blk["text"] = _join_lines(blk["lines"])   # 字符数变了，块文本要重算

    # ④ 逐字符几何（按句拆行的拆点要落在字符边界上）
    for blk in texts:
        for ln in blk["lines"]:
            cx = _line_char_x(ln)
            if cx:
                ln[_CHAR_X] = cx

    # ⑤ 按句子重切块（一个块 = 恰好一句）
    texts, sstats = cut_blocks_by_sentence(texts)

    # ⑥ 每行正文 run 按颜色合并（公式占位单独留）；顺带摘掉内部几何键
    _merge_line_runs(texts, keep_gaps=True)
    _strip_font_keys(texts)
    return texts, sstats


def _build_text_items(page_dict: dict, page_no: int, box_map: dict[int, dict],
                      table_rects: list[list[float]],
                      image_rects: Optional[list[list[float]]] = None,
                      opts: Optional[dict] = None,
                      page=None,
                      stats: Optional[dict] = None
                      ) -> tuple[list[dict], dict[int, list[dict]]]:
    """把文字层拼成阅读器用的块/行（`page` 给出时用 `rawdict` 抽正文字符）。

    返回 `(items, copy_map)`：
      * `items[]`：`{id, kind="text", label, x/y/w/h, text, lines[]}`；
        `lines[]` 是**逐 PyMuPDF 行**（不再合并视觉行 —— 同一视觉行的左右两半由
        `sentences.cut_blocks_by_sentence` 归到同一个**块**里，各自保留自己的行几何）；
      * `copy_map`：公式框 id → `[{id, start, end}]`（块 canonical 里的占位字符区间，
        前端选句/复制时据此还原 LaTeX）。

    `stats` 非空时把本页的切句统计（`sentence_*`）累加进去。
    """
    texts, sstats = _local_text_blocks(
        page, (page_dict or {}).get("blocks") or [],
        page_no=page_no,
        table_rects=table_rects,
        image_rects=list(image_rects or []),
        box_map=box_map,
        opts=opts,
    )
    _accumulate_stats(stats, sstats)

    items: list[dict] = []
    for blk in texts:
        lines = blk.get("lines") or []
        if not lines:
            continue
        x0, y0, x1, y1 = _union_line_rect(lines)
        item = {
            "id": blk.get("id"),
            "kind": "text",
            "label": "text",
            "x": round(x0, 3), "y": round(y0, 3),
            "w": round(max(1.0, x1 - x0), 3), "h": round(max(1.0, y1 - y0), 3),
            "text": blk.get("text") or _join_lines(lines),
            "lines": lines,
        }
        if blk.get("vertical"):
            item["vertical"] = True
        items.append(item)

    # copy 区间：按 item canonical 计字符偏移（前端 JS 按 UTF-16 计）
    copy_map: dict[int, list[dict]] = defaultdict(list)
    for item in items:
        char_pos = 0
        for line in item["lines"]:
            for run in line["runs"]:
                gap = run.get("gap")
                if gap is not None:
                    copy_map[int(gap)].append({
                        "id": item["id"], "start": char_pos,
                        "end": char_pos + _js_len(str(run.get("t") or " ")),
                    })
                char_pos += _js_len(str(run.get("t") or ""))
            char_pos += 1                      # 行末 '\n'（与前端 canonical 一致）
    return items, copy_map


# （旧「视觉行合并」三件套 `_fragments_adjacent` / `_merge_visual_rows` /
#   `_row_content` 已删除：正文字符改走 PyMuPDF 行 + `sentences.cut_blocks_by_sentence`，
#   同一视觉行的左右两半由「按句重切块」并进同一个块，行几何各自保留。）


# （旧「先合并视觉行、再按显式 sp 空隙排版」的 `_build_text_items` 已删除：
#   正文字符改走 `_local_text_blocks` —— rawdict 逐字符 → 补真空格 → 按句切块 →
#   每行一个正文 run，与 `paper-trans-to-html-v11.3-release2` 的 PyMuPDF 后端一致。）


# =====================================================================
#  PDF 元数据
# =====================================================================
def _pdf_outline(doc) -> list[dict]:
    """PDF 自带书签 → `[{level, title, page}]`（没有就返回空表）。"""
    try:
        raw = doc.get_toc(simple=True) or []
    except Exception:
        return []
    toc: list[dict] = []
    for row in raw:
        try:
            level, title, page = int(row[0]), str(row[1] or "").strip(), int(row[2])
        except Exception:
            continue
        if title:
            toc.append({"level": max(1, min(level, 4)),
                        "title": title[:200], "page": max(1, page)})
    return toc


def _pdf_meta(doc) -> tuple[str, list[dict], int]:
    """标题 / 书签 / 页数。"""
    try:
        title = str((doc.metadata or {}).get("title") or "").strip()
    except Exception:
        title = ""
    return title, _pdf_outline(doc), int(doc.page_count)

# 「无主续段」与正文的接边容差(pt)。
_CHAIN_TOL = 4.0
_GAP_JOIN_TOL = 2.0
_HOST_TOL = 8.0


def _strip_font_keys(texts: list[dict]) -> None:
    """出页面前剥掉 runs/行上的内部辅助键。

    `f` 只服务于公式字体判据（补间距与按句切块的探针文本）；`_GAP_KEYS`(`x0`/`x1`/`oy`)
    只服务于「相邻 span 补空格」；`_CX_KEYS`(`_cx`/`_cx_end`) 与行上的 `_CHAR_X`
    (逐字符几何) 只服务于「按句切块」—— 前端不认识它们，doc.json 也没必要为它们变大。
    （正常路径上 `_merge_line_runs` 已经摘过，这里再兜一道，任何分支都不会漏。）
    """
    for blk in texts or []:
        for ln in blk.get("lines") or []:
            ln.pop(_CHAR_X, None)
            for r in ln.get("runs") or []:
                r.pop("f", None)
                for k in _GAP_KEYS + _CX_KEYS:
                    r.pop(k, None)


def _blank_math_and_prune(texts: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """[已停用] 公式字形 → 等长空格，并清理被擦空的行/块。"""
    return texts, [], {}


def _line_rect(ln: dict) -> list[float]:
    """[已停用] 行的矩形 `[x0, x1, y0, y1]`。"""
    return [0.0, 0.0, 0.0, 0.0]


def _band_overlap(a: list[float], b: list[float]) -> float:
    """[已停用] 两行的纵向交集高度。"""
    return 0.0


def _joinable(a: list[float], b: list[float]) -> bool:
    """[已停用] 两段算不算同一视觉行上紧跟着的内容。"""
    return False


def _tail_gap(ln: dict) -> Optional[list]:
    """[已停用] 行的行尾空位。"""
    return None


def _head_gap(ln: dict) -> Optional[list]:
    """[已停用] 行的行首空位。"""
    return None


def _space_count(runs: list[dict], w: float) -> int:
    """[已停用] 把宽度折算成几个空格。"""
    return 0


def _absorb_orphan_gaps(texts: list[dict], keep_lines: dict[int, list[int]]
                        ) -> tuple[list[tuple[int, int, int, int]], int]:
    """[已停用] 把被擦空而删掉的行/块并进相邻正文的空位。"""
    return [], 0


def _local_page(page, doc, images_dir, pno: int, idx0: int = 0,
                opts: Optional[dict] = None,
                mstats: Optional[dict] = None) -> tuple[dict, int]:
    """[已停用] 纯 PyMuPDF 抽一页。"""
    return {}, idx0


def _convert_pages(doc, raw: dict, page_sizes: list[tuple[float, float]],
                   images_dir: Optional[Path], opts: dict,
                   warnings: list[str]) -> tuple[list[dict], dict]:
    """流水线输出 → 阅读器页面数据（在 `doc` 关闭**之前**调用）。

    正文/表格/插图的处理顺序与旧版一致，只有文字块换了实现：
    `_build_text_items(..., page=doc.load_page(pi))` —— 正文字符用 `rawdict` 重新抽，
    才能拿到逐字符 bbox（按句拆行的拆点要落在字符边界上）。
    """
    pages_out: list[dict] = []
    stats = {
        "local_text_blocks": 0,
        "local_text_lines": 0,
        "local_tables": 0,
        "local_images": 0,
        "local_formula_boxes": 0,
        "local_formula_inline": 0,
        "local_formula_display": 0,
        "local_formula_failed": 0,
        # 落在表格/插图里、因此**不下发**的公式框（内容是表格 HTML/插图自己的，见
        # `_hide_graphic_formula_boxes`）
        "local_formula_graphic": 0,
    }

    raw_pages = raw.get("pages") or {}
    for key in sorted(raw_pages,
                      key=lambda k: int(k) if str(k).lstrip("-").isdigit() else 0):
        entry = raw_pages[key] or {}
        try:
            pi = int(entry.get("page_index", key))
        except (TypeError, ValueError):
            pi = len(pages_out)
        contents = entry.get("contents") or {}
        page_no = pi + 1

        fallback_w, fallback_h = (page_sizes[pi] if 0 <= pi < len(page_sizes)
                                  else (595.0, 842.0))
        try:
            page_w = float(contents.get("width") or fallback_w)
            page_h = float(contents.get("height") or fallback_h)
        except (TypeError, ValueError):
            page_w, page_h = fallback_w, fallback_h

        blocks = contents.get("blocks") or []
        table_items, table_rects = _build_table_items(contents, page_no, page_w, page_h)

        # 图内文字覆盖率要用到“所有行的矩形”（在过滤表格文字之前统计）。
        text_rects: list[list[float]] = []
        for block in blocks:
            if int(block.get("type") or 0) != 0:
                continue
            for line in block.get("lines") or []:
                rect = _round_rect(line.get("bbox"))
                if rect is not None:
                    text_rects.append(rect)

        candidates = _collect_page_images(contents, table_rects, text_rects,
                                          page_w, page_h, opts)
        page_images = _save_page_images(candidates, images_dir, page_no)
        # 只有真的落盘了插图才剔除图内文字；没有 images_dir（不显示图）时保留文字
        image_rects = [rect for rect, _item, _cls, _src in candidates] if page_images else []

        box_map = _collect_formula_boxes(blocks)
        _save_formula_crops(box_map, images_dir, page_no,
                            bool(opts.get("local_keep_crops", True)))
        drawable_count = 0
        for box in box_map.values():
            box["_drawable"] = _mark_drawable(box, page_w, page_h)
            if box["_drawable"]:
                drawable_count += 1

        page = None
        try:
            if 0 <= pi < doc.page_count:
                page = doc.load_page(pi)
        except Exception:
            page = None

        text_items, copy_map = _build_text_items(contents, page_no, box_map,
                                                 table_rects, image_rects, opts,
                                                 page=page, stats=stats)
        formula_boxes = _build_formula_box_list(box_map, copy_map, page_w, page_h)
        # 表格/插图里的公式框不下发：那部分内容已经由表格 HTML / 插图本身呈现，
        # 再盖一层 KaTeX 就是重影（实测表头出现 `Top-kᵏ`、`Top1 Top1`、`h=1 ·h=1`）。
        formula_boxes, graphic_hidden = _hide_graphic_formula_boxes(
            formula_boxes, table_rects, image_rects)
        stats["local_formula_graphic"] += graphic_hidden

        texts = text_items + table_items
        pages_out.append({
            "w": round(page_w, 3),
            "h": round(page_h, 3),
            "texts": texts,
            "images": page_images,
            "text": "\n\n".join(t.get("text") or "" for t in texts if t.get("text")),
            "formula_boxes": formula_boxes,
        })

        stats["local_text_blocks"] += len(text_items)
        stats["local_text_lines"] += sum(len(t.get("lines") or []) for t in text_items)
        stats["local_tables"] += len(table_items)
        stats["local_images"] += len(page_images)
        stats["local_formula_boxes"] += len(formula_boxes)
        stats["local_formula_failed"] += max(0, len(box_map) - drawable_count)
        stats["local_formula_inline"] += sum(1 for b in formula_boxes
                                             if b.get("role") == "inline")
        stats["local_formula_display"] += sum(1 for b in formula_boxes
                                              if b.get("role") != "inline")

    if not pages_out:
        warnings.append("本地解析方案没有产出任何页面")
    return pages_out, stats


def _parse_with_pymupdf(pdf_path: Path, images_dir: Optional[Path], opts: dict) -> dict:
    """本地解析方案的入口：跑 `local_parse_pipeline.process_pages` 并转成版式数据。

    返回结构与其它后端一致（`parser` / `parser_info` / `toc` / `warnings` /
    `num_pages` / `page_w` / `page_h` / `pdf_title` / `pages`），并额外给出
    `formula_action="boxes"`（公式由页面级覆盖层渲染）。
    """
    opts = dict(opts or {})
    started = time.time()
    pdf_path = Path(pdf_path)
    if images_dir is not None:
        images_dir = Path(images_dir)
        images_dir.mkdir(parents=True, exist_ok=True)

    engines = _load_engines(opts)
    warnings: list[str] = []

    try:
        import pymupdf
    except ImportError as exc:                       # pragma: no cover
        raise LocalParseUnavailable("缺少 PyMuPDF：pip install PyMuPDF") from exc

    dpi = int(opts.get("local_dpi") or opts.get("dpi") or _DEFAULT_DPI)
    dpi = max(96, min(600, dpi))
    try:
        padding = float(opts.get("local_padding", _DEFAULT_PADDING))
    except (TypeError, ValueError):
        padding = _DEFAULT_PADDING
    try:
        temperature = float(opts.get("local_temperature", 0.0))
    except (TypeError, ValueError):
        temperature = 0.0
    try:
        max_tokens = int(opts.get("local_max_tokens", _DEFAULT_MAX_TOKENS))
    except (TypeError, ValueError):
        max_tokens = _DEFAULT_MAX_TOKENS
    try:
        confidence = float(opts.get("local_layout_confidence",
                                    _DEFAULT_LAYOUT_CONFIDENCE))
    except (TypeError, ValueError):
        confidence = _DEFAULT_LAYOUT_CONFIDENCE
    confidence = min(0.95, max(0.05, confidence))

    doc = pymupdf.open(str(pdf_path))
    try:
        pdf_title, toc, pdf_pages = _pdf_meta(doc)
        page_sizes = [(float(page.rect.width), float(page.rect.height))
                      for page in doc]
        try:
            with _PARSE_LOCK:
                # `texify_engine` 是 pipeline 的公式引擎参数名，这里传的就是 texify。
                raw = engines["pipeline"].process_pages(
                    doc=doc,
                    layout_engine=engines["layout"],
                    slanet_engine=engines["slanet"],
                    texify_engine=engines["texify"],
                    dpi=dpi, padding=padding,
                    temperature=temperature, max_tokens=max_tokens,
                    confidence=confidence,
                )
        except LocalParseUnavailable:
            raise
        except Exception as exc:
            raise LocalParseUnavailable(
                f"本地解析流水线执行失败：{exc}"
            ) from exc
        # 转换要在 `doc` 关闭之前做完：正文字符还要用 `rawdict` 再抽一次
        # （逐字符 bbox 只有 page 能给，见 `_convert_pages`）。
        pages_out, stats = _convert_pages(doc, raw, page_sizes, images_dir,
                                          opts, warnings)
    finally:
        doc.close()

    if stats["local_formula_failed"]:
        warnings.append(
            f"有 {stats['local_formula_failed']} 个公式框没有拿到 LaTeX/裁图，"
            "已保留 PDF 原字形")

    elapsed_ms = int((time.time() - started) * 1000)
    info = dict(stats)
    info.update({
        "local_ms": elapsed_ms,
        "local_pages": len(pages_out),
        "local_pdf_pages": pdf_pages,
        "local_dpi": dpi,
        "local_padding": padding,
        "local_device": engines.get("device") or "auto",
        "local_model_dir": str(_MODEL_DIR),
        "local_layout_model": (engines.get("layout_info") or {}).get(
            "model_version", ""),
        "local_layout_confidence": confidence,
    })

    return {
        "num_pages": len(pages_out),
        "page_w": pages_out[0]["w"] if pages_out else 0,
        "page_h": pages_out[0]["h"] if pages_out else 0,
        "pdf_title": pdf_title,
        "pages": pages_out,
        "toc": toc,
        "parser": "pymupdf",
        "formula_action": ("boxes" if stats["local_formula_boxes"] else "none"),
        "parser_info": info,
        "warnings": warnings,
    }
