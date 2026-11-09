r"""Unlimited-OCR 解析后端：版式来源 = Unlimited-OCR 整页 OCR 的版面项。

**独立实现**：本模块不引用 `backend_surya` / `surya_parser` 的任何代码 —— 两条整页
OCR 链（Surya 与 Unlimited-OCR）的解析、排版估算与前端渲染各自维护、互不影响；
共享的只有项目级通用件（客户端 `../unlimited_ocr.py`、`entry` / `converter` 的
装配约定）。改名 / 调整一边的排版策略时，另一边不会被牵连。

与本地解析方案的区别：

  * **完全使用 Unlimited-OCR 的数据**：文字 / 公式（LaTeX）/ 表格（HTML）/ 插图框 /
    阅读顺序全部来自模型回包；不读 PyMuPDF 的文字层、行框与字体。两条来源都不可用时**直接报错**
    （`unlimited_ocr.UnlimitedOcrUnavailable`，不会悄悄回退 PyMuPDF，
    免得用户以为看到了 OCR 的效果）。

两条**数据来源**（`parser` 取值都是 `unlimited_ocr`，前端渲染路径完全一致）：

  * **本地推理服务**（`../unlimited_ocr.py` + 仓库根 `unlimited_ocr_service/`，
    vLLM 的 OpenAI 兼容接口，默认 `http://127.0.0.1:9010/v1`）—— 逐页 chat/completions；
  * **在线解析**（`../unlimited_ocr_online.py` + 百度智能云「文档解析（Unlimited-OCR）」）
    —— 提交整份 PDF → 轮询 → 下载结果 JSON，回包被转换成与本地**同构**的中间结构
    （`pages[].items[]`，bbox 仍是 0–1000 归一化）。

路由规则（`opts["unlimited_ocr_route"]`，默认 `auto`，环境变量
`UNLIMITED_OCR_ROUTE` 可覆盖；**网页设置项不变**）：

  * `auto`（默认）：先探测本地服务（`GET /models`，≤2s）；
    **本地没配置 / 没起来就自动切在线解析**；本地探测通过但解析中途报错时也回退在线（附 warning）。
  * `local` / `online`：强制其中一条（排障 / 离线验证用）。

实际走了哪条记在 `parser_info["unlimited_ocr_route"]`（`local` / `online`）里，
页面统计可查。在线解析需要容器/服务器能访问公网。
  * 页面 `texts[]` 是**块级**数据，字段：

        {"id": "p1b2", "kind": "heading"|"text"|"reference"|"caption"|"footnote"
                |"formula"|"table"|"header"|"footer",
         "utype": "title",                  # Unlimited-OCR 原始类型（排障 / 统计）
         "label": "",                       # 统一为空（前端不会把它当表格标题渲染）
         "x","y","w","h": 86.6,             # 绝对坐标（PDF pt）
         "size","line_h": 10.0,             # 字号 / 行高估算（pt）
         "align": "justify",                # 对齐（正文类＝justify）
         "text": "1 Introduction",          # 块文本（行内数学已摘成 math 区间）
         "lines": [{"runs": [{"t": "1 Introduction"}]}],
         "math": [{"a": 3, "b": 12, "latex": "…"}],   # text 里的数学区间
         "latex": "…",                      # 仅公式块：KaTeX 源码
         "html": "<table>…"}                # 仅表格块：原始表格 HTML

  * 前端走**它自己那套**块级流式渲染（`frontend/js/reader2.js::buildUnlimitedItem`，
    `DOC.parser === "unlimited_ocr"` 时启用）：块按 bbox 绝对定位、块内文字正常换行；
    正文类两端对齐、标题加粗放大、图注/脚注/页眉页脚小一号；行内公式按 `math` 的
    精确字符区间就地换 KaTeX；独立公式（kind="formula" + latex）KaTeX 块渲染；
    表格（kind="table" + html）白名单渲染、不参与句子选中。
  * 坐标换算：模型回包 bbox 是 **0–1000 归一化坐标**（x、y 各自除以页面宽高再乘
    1000；见 `../unlimited_ocr.py` 模块注释）—— 这里按 `归一化值 / 1000 × 页面 pt
    边长` 换成 PDF 点；页面 `w/h` 由渲染图像素 × 72/dpi 得到，全页只有一把尺子。
  * 行内公式：模型把 LaTeX 直接写进正文（`\(…\)` / `$$…$$` / 单 `$…$`）——
    解析时把定界符摘掉、字符区间记进 `math`，前端在原位换成 KaTeX。
    ⚠️ 定界符**内侧允许带空格**（在线回包写 `$ x(t) \in \mathbb{R}^N $`），
    只要求长度 ≤120、不含换行、且含 `\ _ ^ { }` 之一（挡货币符号）。
  * 公式块（`equation`）：文本形如 `\[ … \]` —— 剥掉定界符存进 `latex`。
  * 表格：原样存进 `html`（前端白名单渲染）；另用本地 `_table_text` 生成纯文本，
    作为可翻译 / 可复制的文本。
  * 插图（`image` / `chart`）：由客户端按 bbox 裁剪落盘到 `images_dir`，写进
    `pages[].images`（`source="unlimited_ocr"`），前端沿用图片层渲染。
  * 字号：模型不给字号 —— 本模块自带一套估算（`_document_body_size` ×
    `_block_metrics`，从块几何 + 字符数反解），全 PDF 统一正文基准。
    **在线链路**能额外拿到 PDF 文字层的真实字号（`unlimited_ocr_pdf_body_size`），
    有它时直接当正文基准（几何反解对在线回包的紧贴框会偏大一号；实测 10pt 正文
    被反解成 ~11.9pt），否则仍用反解值。

统计（`parser_info`）：`unlimited_ocr_route` / `unlimited_ocr_url` /
`unlimited_ocr_model` / `unlimited_ocr_dpi` / `unlimited_ocr_ms` /
`unlimited_ocr_blocks` / `unlimited_ocr_images` / `unlimited_ocr_pdf_pages` /
`unlimited_ocr_types`（模型原始类型直方图）+ 各渲染 kind 的条数
（`unlimited_ocr_kind_*`）；在线链路另有 `unlimited_ocr_task_id` /
`unlimited_ocr_task_ms` / `unlimited_ocr_rotated_pages` 等。

服务端镜像见仓库根 `unlimited_ocr_service/`；客户端见 `../unlimited_ocr.py`（本地）
与 `../unlimited_ocr_online.py`（在线）。
"""
from __future__ import annotations

import hashlib
import html as _html_mod
import math
import re
import statistics
from pathlib import Path
from typing import Optional

from .. import unlimited_ocr, unlimited_ocr_online

__all__ = ["_parse_with_unlimited_ocr"]

# ---- 数据来源路由（见模块文档）----
ROUTE_AUTO = "auto"
ROUTE_LOCAL = "local"
ROUTE_ONLINE = "online"
_ROUTES = (ROUTE_AUTO, ROUTE_LOCAL, ROUTE_ONLINE)

# =====================================================================
#  排版估算（本模块自带的一套：块几何 + 字符数 → 字号/行高）
# =====================================================================
# 页基准字号（pt）的兜底：拿不到任何块估算值时用
_FALLBACK_BODY_SIZE = 10.0
# 字号反解系数：`h·w = k · chars · s²`（k = 行距倍率 × 平均字宽/em）。
# 实测校准（latex_sample.pdf，正文 10pt）：OCR 框紧贴字形，k≈0.55 反解最准；
# 换用 0.725 那档（给“检测框向外膨胀”的版面校准用）会把字号估小 ~20%。
_SIZE_K = 0.55
# 标题级别 → 相对页正文的字号倍率（模型不给字号，只能按排版惯例估）
_HEADING_MUL = {1: 1.45, 2: 1.22, 3: 1.08, 4: 1.02, 5: 1.0, 6: 1.0}
# 正文类 kind：两端对齐（align="justify"）、参与列间隙恢复（见 _restore_column_gaps）
_JUSTIFY_KINDS = ("text", "reference", "caption", "footnote")


def _estimate_size_pt(block_w: float, block_h: float, chars: int) -> float:
    """单块字号粗估：`h = nlines × 1.2s`、`nlines = chars × w_em·s / w`，解出 s。"""
    chars = max(8, int(chars))
    if block_w <= 0 or block_h <= 0:
        return 0.0
    return math.sqrt(block_h * block_w / (_SIZE_K * chars))


def _document_body_size(blocks: list[dict]) -> float:
    """全 PDF 正文基准字号（pt）：跨所有页面取中位数，避免不同页差异。

    只统计 `text` 类块（`reference` / 图注 / 页脚等小字号块除外，免得把基准拉低）。
    """
    ests = []
    for b in blocks:
        if b["kind"] not in ("text",) or len(b["text"]) < 60:
            continue
        est = _estimate_size_pt(b["w"], b["h"], len(b["text"]))
        if 5.0 <= est <= 22.0:
            ests.append(est)
    if not ests:
        return _FALLBACK_BODY_SIZE
    return min(20.0, max(6.0, statistics.median(ests)))


def _block_metrics(kind: str, level: Optional[int], w: float, h: float,
                   text: str, body: float) -> tuple[float, float]:
    """块的（字号, 行高）估算，单位 pt（按类型相对页基准缩放）。

    字号只按类型给：标题按级别放大、图注/脚注/页眉页脚/参考文献小一号、正文用页
    基准字号；块宽高只决定换行与容器位置，不二次缩放（同一份 PDF 全篇字号才统一）。
    行高按类型给固定倍率（正文 1.42、标题 1.25、图注 1.32、表格 1.3）。
    """
    if kind == "heading":
        size = body * _HEADING_MUL.get(level or 2, 1.12)
        lh_mul = 1.25
    elif kind == "caption":
        size = body * 0.92
        lh_mul = 1.32
    elif kind == "reference":
        # 参考文献：比正文小一号
        size = body * 0.92
        lh_mul = 1.32
    elif kind in ("footnote", "header", "footer"):
        size = body * 0.88
        lh_mul = 1.32
    elif kind == "table":
        size = body * 0.95
        lh_mul = 1.30
    elif kind == "formula":
        size = body
        lh_mul = 1.30
    else:                        # text
        size = body
        lh_mul = 1.42
    size = min(28.0, max(5.5, size))
    return round(size, 2), round(size * lh_mul, 2)


# 列间隙恢复（双栏页面：OCR 框的膨胀/漂移把列间空当挤没，两端对齐后两栏会贴到一起）
_MIN_COL_GAP = 10.0        # pt：相邻列之间应保留的空隙（LaTeX 默认 \columnsep）
_COL_EDGE_TOL = 1.5        # pt：框允许的轻微重叠（负间隙）容差


def _restore_column_gaps(items: list[dict]) -> None:
    """把双栏页被挤没的列间空隙恢复到 _MIN_COL_GAP（就地修改）。

    「垂直重叠、水平相邻且间隙 < _MIN_COL_GAP」的正文类块对，两侧各让一半；
    每个块至多修一次。要求**至少一侧是多行块**（单行×单行的紧邻更可能是检测器
    把一行拆成两块，不碰）；标题/公式/表格/页眉页脚不参与。
    """
    cand = [it for it in items if it.get("kind") in _JUSTIFY_KINDS]
    if len(cand) < 2:
        return

    def _multiline(it: dict) -> bool:
        lh = float(it.get("line_h") or 0)
        return lh > 0 and it.get("h", 0) >= lh * 2.0

    used = set()
    for a in cand:
        if a["id"] in used:
            continue
        for b in cand:
            if b is a or b["id"] in used or b["x"] <= a["x"]:
                continue
            gap = b["x"] - (a["x"] + a["w"])
            if not (-_COL_EDGE_TOL <= gap < _MIN_COL_GAP):
                continue
            overlap = (min(a["y"] + a["h"], b["y"] + b["h"])
                       - max(a["y"], b["y"]))
            if overlap <= 0 or overlap < min(a["h"], b["h"]) * 0.5:
                continue
            if not (_multiline(a) or _multiline(b)):
                continue
            d = (_MIN_COL_GAP - gap) / 2.0
            if a["w"] - d < 24 or b["w"] - d < 24:
                continue        # 防御：别把块挤没了
            a["w"] = round(a["w"] - d, 3)
            b["x"] = round(b["x"] + d, 3)
            b["w"] = round(b["w"] - d, 3)
            # lines[0] 是块宽/位置的镜像（前端主要用 item 的 x/w，保持一致）
            if a.get("lines"):
                a["lines"][0]["w"] = a["w"]
            if b.get("lines"):
                b["lines"][0]["x"] = b["x"]
                b["lines"][0]["w"] = b["w"]
            used.add(a["id"])
            used.add(b["id"])
            break


def _pdf_meta(pdf_path: Path) -> tuple[str, list[dict], int]:
    """PDF 标题 / 自带书签 / 页数（只读元数据，不参与版式渲染）。"""
    try:
        import fitz
    except ImportError:
        return "", [], 0
    doc = fitz.open(str(pdf_path))
    try:
        title = str((doc.metadata or {}).get("title") or "").strip()
        toc: list[dict] = []
        try:
            raw = doc.get_toc(simple=True) or []
        except Exception:
            raw = []
        for row in raw:
            try:
                level, name, page = int(row[0]), str(row[1] or "").strip(), int(row[2])
            except Exception:
                continue
            if name:
                toc.append({"level": max(1, min(level, 4)),
                            "title": name[:200], "page": max(1, page)})
        return title, toc, int(doc.page_count)
    finally:
        doc.close()


_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)")


def _heading_level(text: str) -> int:
    """标题级别：按编号（`1.2.3` → 3）判定，认不出按 2。"""
    m = _NUM_RE.match(text or "")
    if m:
        return max(1, min(4, m.group(1).count(".") + 1))
    return 2


# 在线回包给得比本地 grounding 输出更细：`sub_type` 里带标题层级（`title_1` …）
_SUB_TYPE_LEVEL_RE = re.compile(r"^(?:title|heading|h)[_-]?(\d)$", re.I)


def _sub_type_level(it: dict) -> Optional[int]:
    """`sub_type`（`title_1` / `title_2` …）→ 标题层级；认不出返回 None。

    本地 grounding 输出没有这个字段（只有 `type`），所以那边仍走
    `_heading_level(text)` 的数字编号推断。
    """
    m = _SUB_TYPE_LEVEL_RE.match(str(it.get("sub_type") or "").strip())
    if not m:
        return None
    return max(1, min(4, int(m.group(1))))


# =====================================================================
#  表格 HTML → 纯文本（本模块自带；表格的展示用原始 html，前端白名单渲染）
# =====================================================================
def _strip_html(s: str) -> str:
    return " ".join(_html_mod.unescape(re.sub(r"<[^>]+>", "", s or "")).split())


def _table_text(html: str) -> str:
    """表格 HTML → 可读纯文本：一行一个 `<tr>`，单元格用 `" | "` 分隔。

    模型给的表格 HTML 结构简单（`<table><tr><td>…`）；万一没有 `<tr>`，
    退化为整段剥标签的文本。
    """
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", html or "", re.I | re.S)
    out: list[str] = []
    for row in rows:
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.I | re.S)
        line = " | ".join(_strip_html(c) for c in cells).strip()
        if line:
            out.append(line)
    if not out:
        return _strip_html(html)
    return "\n".join(out)

# 模型原始类型 → 前端渲染 kind（认不出的类型：有文本按普通文本，无文本丢弃）
_KIND_BY_TYPE = {
    "title": "heading",
    "text": "text",
    "paragraph": "text",
    "content": "text",
    "abstract": "text",
    "aside_text": "text",
    "ref_text": "reference",              # 参考文献：比正文小一号
    "reference": "reference",
    "footnote": "footnote",
    "page_footnote": "footnote",
    "endnote": "footnote",
    "equation": "formula",
    "equation_block": "formula",
    "formula": "formula",
    "table": "table",
    "table_caption": "caption",
    "table_title": "caption",
    "table_name": "caption",
    "image_caption": "caption",
    "figure_caption": "caption",
    "figure_title": "caption",
    "figure_name": "caption",
    "image_footnote": "footnote",           # 在线回包的图注脚注
    "page_number": "footer",
    "page_header": "header",
    "header": "header",
    "footer": "footer",
    "image": "figure",
    "figure": "figure",
    "chart": "figure",
    "diagram": "figure",
    "seal": "figure",                       # 在线回包：印章（当插图裁图展示）
    "algorithm": "text",                    # 在线回包：代码块（当普通段落）
    "doc_title": "heading",
    "vertical_text": "text",
    "list": "text",
    "aside": "text",
}

#：公式块文本的定界符（按优先级匹配；`$$` 必须排在 `$` 前面）
_LATEX_DELIMS = (("\\[", "\\]"), ("\\(", "\\)"), ("$$", "$$"), ("$", "$"))

# 单 `$…$` 的保守判据（与前端 `r2-math.js::_splitInlineMath` 同口径）：
# 长度上限、必须含 \ _ ^ { } 之一（挡货币符号）
# ⚠️ 允许定界符内侧带空格：在线解析（百度智能云）的输出习惯写 `$ x(t) \in \mathbb{R}^N $`，
#    而本地 grounding 输出写 `$x$` 或 `\(x\)` —— 旧判据要求 `inner.strip() == inner`，
#    会把在线回包里带空格的公式**整段当普通文本**（界面上直接看到 LaTeX 源码）。
_DOLLAR_MAX = 200
_MATH_HINT_RE = re.compile(r"[\\_^{}]")
# 「像公式」的宽松判据（定界符内侧可能只有个变量名或短式子）：
#   允许的字符集（ASCII 字母数字 + 常用运算符 / 括号）+ 不能含 3 个以上连续字母
#   （挡掉 "$5 and $10" 这种把两个金额之间的正文当公式），且至少要有一个运算符。
_DOLLAR_ALLOWED_RE = re.compile(r"^[A-Za-z0-9\s\\{}_^()\[\]+\-=*/<>|,.'\u2019]*$")
_LONG_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_SINGLE_LETTER_RE = re.compile(r"^[A-Za-z]$")
_DOLLAR_NUMERIC_RE = re.compile(r"^[\[(]?[\d\s.,\-\u2013]+[\])]?$")
_DOLLAR_OPERATORS = "=()[]+-*/<>"


def _looks_inline_math(inner: str) -> bool:
    """单 `$…$` 的内容是否**像**行内公式（挡货币符号 / 正文片段）。

    认这几种：
      * 含 LaTeX 命令 / 上下标 / 分组的字符（反斜杠、下划线、尖号、花括号）；
      * 单个拉丁字母（`$A$` / `$q$` / `$u$` —— 论文里最常见的变量写法）；
      * 纯数字 / 带括号的编号（`$ [29] $`）；
      * 纯 ASCII 字母数字 + 常用运算符，**且**含 `= ( ) [ ] + - * / < >` 之一，
        **且**至少有一个字母、**且**没有 3 个以上连续字母
        （`$ h(0) = Dh(0) = 0 $` ✓，`$5 and $10` ✗，`$5 ($6` ✗）。
    """
    core = (inner or "").strip()
    if not core or len(core) > _DOLLAR_MAX:
        return False
    if _MATH_HINT_RE.search(core):
        return True
    if _SINGLE_LETTER_RE.match(core):
        return True
    if _DOLLAR_NUMERIC_RE.match(core):
        return True
    if not _DOLLAR_ALLOWED_RE.match(core) or _LONG_WORD_RE.search(core):
        return False
    if not re.search(r"[A-Za-z]", core):
        return False
    return any(op in core for op in _DOLLAR_OPERATORS)


def _latex_block(raw: str) -> str:
    """公式块文本（`\\[ … \\]` / `$$…$$`）→ 纯 LaTeX；没有定界符就原样返回去空格。"""
    s = (raw or "").strip()
    for left, right in _LATEX_DELIMS:
        if (len(s) > len(left) + len(right)
                and s.startswith(left) and s.endswith(right)):
            return s[len(left):-len(right)].strip()
    return s


def _record_math(out: list[str], ranges: list[dict], inner: str) -> None:
    """把一段内联 LaTeX（`inner`）追加进字符流并记下区间（首尾空白不入区间）。"""
    core = inner.strip()
    if not core:
        return
    start = len(out)
    out.extend(core)
    ranges.append({"a": start, "b": start + len(core), "latex": core})


def _text_with_math(raw: str) -> tuple[str, list[dict], list[dict]]:
    """块文本 →（text, runs, math 区间）。

    把 `\\(…\\)` / `\\[…\\]` / `$$…$$`（以及保守判据下的单 `$…$`）摘成数学区间，
    `text` 里不再含 LaTeX 定界符；runs 覆盖整段文本（单 run，前端 styleRun 直接用）。
    """
    out: list[str] = []
    ranges: list[dict] = []
    i, n = 0, len(raw or "")
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n and raw[i + 1] in "([":
            close = "\\)" if raw[i + 1] == "(" else "\\]"
            j = raw.find(close, i + 2)
            if j >= 0:
                _record_math(out, ranges, raw[i + 2:j])
                i = j + 2
                continue
        elif ch == "$":
            if raw.startswith("$$", i):
                j = raw.find("$$", i + 2)
                if j >= 0:
                    _record_math(out, ranges, raw[i + 2:j])
                    i = j + 2
                    continue
            else:
                k = raw.find("$", i + 1)
                inner = raw[i + 1:k] if k >= 0 else ""
                if (k >= 0 and "\n" not in inner and _looks_inline_math(inner)):
                    # 定界符内侧的空格允许存在（在线回包的惯用写法），
                    # 区间只记核心字符（`_record_math` 会 strip）
                    _record_math(out, ranges, inner)
                    i = k + 1
                    continue
        out.append(ch)
        i += 1
    text = "".join(out)
    runs = [{"t": text}] if text else []
    return text, runs, ranges


def _figure_image(it: dict, images_dir: Optional[Path], scale: float) -> Optional[dict]:
    """图片类项 → `pages[].images` 条目（pt 坐标；图片由客户端已落盘）。"""
    info = it.get("image")
    if not isinstance(info, dict) or images_dir is None:
        return None
    fname = str(info.get("file") or "").strip()
    bounds = info.get("bbox") or it.get("bbox")
    if not fname or not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bounds)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    try:
        digest = hashlib.sha1((Path(images_dir) / fname).read_bytes()).hexdigest()[:12]
    except OSError:
        return None
    typ = str(it.get("type") or "").strip().lower()
    return {
        "x": round(x0 * scale, 3), "y": round(y0 * scale, 3),
        "w": round((x1 - x0) * scale, 3), "h": round((y1 - y0) * scale, 3),
        "file": fname, "source": "unlimited_ocr",
        "class_name": "Chart" if typ == "chart" else "Figure",
        "cache_key": digest,
    }


# =====================================================================
#  数据来源路由：本地推理服务 ↔ 在线解析
# =====================================================================
def _route_pref(opts: dict) -> str:
    """路由偏好：`auto`（默认）/ `local` / `online`；非法值按 auto。"""
    pref = str((opts or {}).get("unlimited_ocr_route") or ROUTE_AUTO).strip().lower()
    return pref if pref in _ROUTES else ROUTE_AUTO


def _local_ready(opts: dict) -> tuple[bool, str]:
    """本地推理服务是否就绪（`GET /models` 真探测，≤ PROBE_TIMEOUT 秒）。

    探测本身出错（超时 / DNS / 连接被拒）一律当「未就绪」—— 这正是切在线解析的触发条件。
    """
    try:
        return unlimited_ocr.check_server(opts.get("unlimited_ocr_url"),
                                         opts.get("unlimited_ocr_model"))
    except Exception as exc:                       # 兜底：探测异常不该炸掉解析
        return False, f"探测失败：{exc}"


def _parse_local(pdf_path: Path, images_dir: Optional[Path], opts: dict) -> dict:
    """本地推理服务（vLLM，逐页 chat/completions）→ 中间结构。"""
    return unlimited_ocr.ocr_pdf(pdf_path, opts, images_dir=images_dir)


def _parse_online(pdf_path: Path, images_dir: Optional[Path], opts: dict) -> dict:
    """在线解析（百度智能云 Unlimited-OCR）→ 同构中间结构。异常统一成
    `UnlimitedOcrUnavailable`，让转换接口显示一条完整原因。"""
    try:
        return unlimited_ocr_online.ocr_pdf(pdf_path, opts, images_dir=images_dir)
    except unlimited_ocr_online.UnlimitedOcrOnlineUnavailable as exc:
        raise unlimited_ocr.UnlimitedOcrUnavailable(
            f"在线 Unlimited-OCR 解析失败：{exc}") from exc


# =====================================================================
#  入口：Unlimited-OCR 版面数据 → doc.json 的 pages 结构
# =====================================================================
def _parse_with_unlimited_ocr(pdf_path: Path, images_dir: Optional[Path],
                              opts: dict) -> dict:
    """整份 PDF 走 Unlimited-OCR 整页 OCR，转成阅读器的版式数据。

    数据来源按 `opts["unlimited_ocr_route"]` 选择（默认 `auto`：**本地服务没配置 /
    没起来就自动切在线解析**，见模块文档）。两条来源失败都抛
    `unlimited_ocr.UnlimitedOcrUnavailable`（由 `entry.parse_pdf` 向上抛给转换接口，
    页面上会显示具体原因）—— **不回退 PyMuPDF**。
    """
    pref = _route_pref(opts)
    warnings: list[str] = []
    if pref == ROUTE_LOCAL:
        route = ROUTE_LOCAL
    elif pref == ROUTE_ONLINE:
        route = ROUTE_ONLINE
        warnings.append("已按 unlimited_ocr_route=online 强制走在线解析")
    else:
        ok, why = _local_ready(opts)
        route = ROUTE_LOCAL if ok else ROUTE_ONLINE
        if not ok:
            warnings.append(f"本地 Unlimited-OCR 服务未配置 / 未就绪（{why}），"
                            "已自动切换为在线解析（百度智能云 Unlimited-OCR）")

    if route == ROUTE_LOCAL:
        try:
            raw = _parse_local(pdf_path, images_dir, opts)
        except unlimited_ocr.UnlimitedOcrUnavailable as exc:
            if pref != ROUTE_AUTO:
                raise
            # 探测通过但解析中途失败（服务重启 / 显存不足 / 单页超时…）→ 兜底在线
            warnings.append(f"本地 Unlimited-OCR 解析失败（{exc}），已改用在线解析")
            route = ROUTE_ONLINE
            raw = _parse_online(pdf_path, images_dir, opts)
    else:
        raw = _parse_online(pdf_path, images_dir, opts)

    raw_route = str(raw.get("route") or route).strip().lower()
    scale = 72.0 / float(raw.get("dpi") or 150)
    stats = dict(raw.get("stats") or {})
    pdf_title, toc, pdf_pages = _pdf_meta(pdf_path)

    pages_out: list[dict] = []
    kind_counts: dict[str, int] = {}
    total_images = 0
    document_blocks: list[dict] = []
    # 逐块真实字号 / 行距（在线链路从 PDF 文字层量到；没有就不记）
    pdf_metrics: dict[str, tuple[float, float]] = {}
    for pg in raw.get("pages") or []:
        pw = float(pg.get("w") or 0) * scale           # 页面 pt 宽
        ph = float(pg.get("h") or 0) * scale           # 页面 pt 高
        mids: list[dict] = []
        images: list[dict] = []
        for order, it in enumerate(pg.get("items") or []):
            typ = str(it.get("type") or "").strip().lower()
            kind = _KIND_BY_TYPE.get(typ)
            if kind is None and str(it.get("text") or "").strip():
                kind = "text"                          # 认不出的类型：有文本按文本
            if kind is None:
                continue
            box = it.get("bbox")
            if not (isinstance(box, (list, tuple)) and len(box) == 4):
                continue
            if kind == "figure":
                image = _figure_image(it, images_dir, scale)
                if image is None:
                    continue        # 裁图缺失/过小：没有可显示的东西
                images.append(image)
                total_images += 1
                kind_counts["figure"] = kind_counts.get("figure", 0) + 1
                continue
            try:
                nx0, ny0, nx1, ny1 = (float(v) for v in box)
            except (TypeError, ValueError):
                continue
            # 归一化（0–1000）→ PDF 点
            x0, x1 = nx0 / 1000.0 * pw, nx1 / 1000.0 * pw
            y0, y1 = ny0 / 1000.0 * ph, ny1 / 1000.0 * ph
            if x1 - x0 <= 0.5 or y1 - y0 <= 0.5:
                continue

            raw_text = str(it.get("text") or "")
            item: dict = {
                "id": f"p{pg.get('page')}b{order}",
                "kind": kind,
                "utype": typ,               # 模型原始类型（排障 / 统计）
                "unlimited": True,          # 数据侧标记：本块来自 Unlimited-OCR
                "label": "",                # 统一为空（前端不会拿它当表格标题）
                "x": round(x0, 3), "y": round(y0, 3),
                "w": round(x1 - x0, 3), "h": round(y1 - y0, 3),
                "text": "", "math": [], "html": "", "lines": [],
            }
            item["align"] = "justify" if kind in _JUSTIFY_KINDS else "left"

            if kind == "formula":
                latex = _latex_block(raw_text)
                if not latex:
                    continue            # 拿不到 LaTeX 的公式块：没有可渲染的内容
                item["latex"] = latex
            elif kind == "table":
                if "<table" in raw_text.lower():
                    # 表格 HTML 原样下发（前端白名单渲染）；纯文本用本地 _table_text
                    # 生成（可翻译 / 可复制）—— 不引用任何其它后端模块
                    item["html"] = raw_text
                    text = _table_text(raw_text)
                    runs = [{"t": text}] if text else []
                    math = []
                else:
                    # 模型没给表格结构（个别页会给 Markdown 文本）：按普通段落处理
                    text, runs, math = _text_with_math(raw_text)
                if not text:
                    continue
                item["text"], item["math"] = text, math
                item["lines"] = [{"x": item["x"], "y": item["y"],
                                  "w": item["w"], "h": item["h"], "runs": runs}]
            else:
                text, runs, math = _text_with_math(raw_text)
                if not text:
                    continue            # 空文本块：没有可显示 / 可选中的内容
                item["text"], item["math"] = text, math
                item["lines"] = [{"x": item["x"], "y": item["y"],
                                  "w": item["w"], "h": item["h"], "runs": runs}]
                if kind == "heading":
                    # 在线回包的 sub_type（title_1 / title_2…）比数字编号更准；
                    # 本地 grounding 输出没有 sub_type → 退回按编号推断。
                    item["level"] = _sub_type_level(it) or _heading_level(text)

            mids.append(item)
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            document_blocks.append({"kind": kind, "w": item["w"],
                                    "h": item["h"], "text": item["text"]})
            # 客户端（在线链路）量到的块内字号 / 行距：只在合理区间内采纳
            try:
                ps = float(it.get("pdf_size") or 0)
                pl = float(it.get("pdf_line_h") or 0)
            except (TypeError, ValueError):
                ps = pl = 0.0
            if 4.0 <= ps <= 60.0:
                pdf_metrics[item["id"]] = (ps, pl)

        page = {
            "w": round(pw, 3),
            "h": round(ph, 3),
            "texts": mids,
            "images": images,
            "text": "\n\n".join(t["text"] for t in mids if t.get("text")),
        }
        pages_out.append(page)

    # 全 PDF 统一正文基准；caption、table、footnote 等仅按类型相对缩放。
    body = _document_body_size(document_blocks)
    # 在线链路能从 PDF 文字层读到**真实**正文字号 —— 用它当基准（几何反解对在线回包的
    # 紧贴框偏大约 1 号，实测 10pt 正文被反解成 ~11.9pt，页面上明显偏大）。
    # 本地 grounding 输出没有这个提示，仍用几何反解（那套 k 就是按它标定的）。
    try:
        hint = float(stats.get("unlimited_ocr_pdf_body_size") or 0)
    except (TypeError, ValueError):
        hint = 0.0
    if 5.0 <= hint <= 22.0:
        body = hint
    for page in pages_out:
        for item in page["texts"]:
            size, line_h = _block_metrics(item["kind"], item.get("level"),
                                          item["w"], item["h"], item["text"], body)
            # 有 PDF 文字层的实测值就用它：字号直接用，行距用实测基线间隔
            # （实测正文 11.95pt / 行距 14.4pt = 1.20×，而估算倍率是 1.42 —— 那才是
            #  「字看着偏大」的主因之一）。行距异常（<1×或 >2.2×）时回退到倍率。
            ps, pl = pdf_metrics.get(item["id"], (0.0, 0.0))
            if ps:
                ratio = (line_h / size) if size else 1.42
                size = round(ps, 2)
                if pl and 1.0 <= pl / size <= 2.2:
                    line_h = round(pl, 2)
                else:
                    line_h = round(size * ratio, 2)
            item["size"] = size
            item["line_h"] = line_h
            if item.get("lines"):
                item["lines"][0]["s"] = size

    # 列间隙恢复：双栏页面里 OCR 框的膨胀/漂移会把两列之间的空当挤没（两端对齐后
    # 两栏文字贴到一起）—— 把间隙修回 _MIN_COL_GAP（见本函数说明）。
    for page in pages_out:
        _restore_column_gaps(page["texts"])

    rotated = stats.get("unlimited_ocr_rotated_pages")
    if raw_route == ROUTE_ONLINE and rotated:
        warnings.append(
            f"有 {rotated} 页被在线服务做了角度矫正（page_angle≠0）：坐标仍按原页面"
            "坐标系换算，若发现版面错位可先把页面转正再解析")
    info: dict = {
        # 数据来源：local = 本地推理服务 / online = 百度智能云在线解析
        "unlimited_ocr_route": raw_route,
        "unlimited_ocr_url": raw.get("url"),
        "unlimited_ocr_model": raw.get("model"),
        "unlimited_ocr_dpi": raw.get("dpi"),
        "unlimited_ocr_ms": stats.get("unlimited_ocr_ms", 0),
        "unlimited_ocr_blocks": sum(len(p["texts"]) + len(p["images"])
                                    for p in pages_out),
        "unlimited_ocr_pdf_pages": pdf_pages,
        "unlimited_ocr_images": total_images,
        "unlimited_ocr_types": stats.get("unlimited_ocr_types") or {},
        # 实际用的正文基准字号（pt）：在线链路优先取 PDF 文字层的真实字号
        "unlimited_ocr_body_size": round(body, 2),
    }
    # 在线链路的附加统计（本地链路没有这些键）
    if raw_route == ROUTE_ONLINE:
        if stats.get("unlimited_ocr_task_id"):
            info["unlimited_ocr_task_id"] = stats["unlimited_ocr_task_id"]
        info["unlimited_ocr_task_ms"] = stats.get("unlimited_ocr_task_ms", 0)
        if rotated:
            info["unlimited_ocr_rotated_pages"] = rotated
    info.update({f"unlimited_ocr_kind_{k}": v for k, v in sorted(kind_counts.items())})
    return {
        "num_pages": len(pages_out),
        "page_w": pages_out[0]["w"] if pages_out else 0,
        "page_h": pages_out[0]["h"] if pages_out else 0,
        "pdf_title": pdf_title,
        "pages": pages_out,
        "toc": toc,                    # PDF 自带书签（没有就是空列表）
        "parser": "unlimited_ocr",
        # 公式怎么落地的：unlimited_ocr = 公式来自 OCR 回包的 LaTeX（行内区间 +
        # 独立公式块），与 "surya"（Surya 的 <math>）、"space"（PyMuPDF 擦成空格）、
        # "boxes"（服务组公式框覆盖层）并列。
        "formula_action": "unlimited_ocr",
        "warnings": warnings,         # entry 会并进整次解析的 warnings
        "parser_info": info,
    }
