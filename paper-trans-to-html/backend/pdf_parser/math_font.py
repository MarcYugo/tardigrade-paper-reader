"""公式定位 · 底层：字体名判据保留，字形擦除链路已停用。

本地解析方案的公式**内容**来自 `local_parse_pipeline`（texify 识别公式框），不再把
公式字形擦成等长空格；但「哪些字形是数学字体」仍然要用 —— 它服务于两处**正文字符**
处理：

  * `text_layer._insert_gap_spaces`：相邻 span 之间补空格时，公式字形两侧**一律不补**
    （补进去会把一处公式切成两段，行内公式的占位与前后文就对不上了）；
  * `sentences`：按句子重切块时用「探针文本」（数学字形换成等长空格 —— 长度不变，
    偏移与真实行文本一一对应）判句末，公式里的 `.`/`!` 不会被当成句末。

所以这里只保留字体名判据（CM/AMS/Symbol）与页级两个开关（`_math_font_flags`）；
`_blank_math_spans` / `_line_gap_widths` / `_group_math_pieces` 那套「字形→空格」
的实现已随旧链路删除，函数体留空壳（名字仍被 `pdf_parser.__init__` 再导出）。
"""

from __future__ import annotations

import re
from typing import Optional


# 「强」数学字体：只在数学排版里出现，命中即可判为公式字形。
_MATH_FONT_STRONG = (
    "cmmi", "cmsy", "cmex", "cmmib", "cmbsy", "cmbex",
    "msam", "msbm", "eufm", "eurm", "symbol", "mtmi", "mtsy", "mtex",
    "euclid", "rsfs", "stmary", "stmry", "wasy",
    "lmmi", "lmsy", "lmex", "lmmib",
)
# 「弱」数学字体：数学与正文都可能用 —— 只有它**不是本页主字体**时才算公式字形。
_MATH_FONT_WEAK = ("cmr", "cmb", "cmss", "cmtt", "cmu",
                   "latinmodern", "lmr", "lmss", "lmtt",
                   "stix", "xits", "cambria math", "campmath",
                   "newtxmath", "mathpazo", "txfonts")
# 「符号专用」字体：数学排版里的大符号/运算符才用它们。
_SYMBOL_FONT = ("cmsy", "cmex", "msam", "msbm", "wasy", "stmary", "stmry",
                "eufm", "euex", "symbol")
# 一行「几乎全是数学字形」的下限。
_LINE_MATH_PURE = 0.9
# 同一行里两段空格相隔多少个字符以内还算同一条公式。
_MERGE_GAP_CHARS = 4
# CM 系「文字」字体：算不算公式要按占比判。
_CM_TEXT_RE = re.compile(r"cm(r|bx|ti|ss|tt|sl)\d*", re.IGNORECASE)
# 正文空格的「字宽 / 字号」经验值。
_SPACE_EM = 0.25


def _font_base(font: str) -> str:
    """字体名归一：去子集前缀(`ABCDEF+`)与结尾字号(`CMMI10`→`cmmi`)，只留小写字母。"""
    name = (font or "").split("+")[-1].strip().lower()
    return re.sub(r"[\d\s]+$", "", re.sub(r"[^a-z0-9\s]", "", name))


def _font_math_level(font: str, weak_on: bool) -> int:
    """0=非数学字体 1=弱数学字体 2=强数学字体。"""
    base = _font_base(font)
    if not base:
        return 0
    if any(base.startswith(p) for p in _MATH_FONT_STRONG):
        return 2
    if weak_on and any(base.startswith(p) for p in _MATH_FONT_WEAK):
        return 1
    return 0


def _weak_math_enabled(local_texts: list[dict]) -> bool:
    """本页能不能用「弱数学字体」判据。

    主字体本身就是弱数学字体(整篇 CM 系，如 LaTeX 默认模板)时不能 —— 否则整页
    正文都会被当成公式字形。
    """
    counts: dict[str, int] = {}
    for blk in local_texts:
        for ln in blk.get("lines") or []:
            for r in ln.get("runs") or []:
                f = r.get("f") or ""
                counts[f] = counts.get(f, 0) + len(r.get("t") or "")
    if not counts:
        return False
    top = max(counts.items(), key=lambda kv: kv[1])[0]
    base = _font_base(top)
    return not any(base.startswith(p) for p in _MATH_FONT_WEAK)


def _line_math_spans(line: dict, weak_on: bool
                     ) -> tuple[list[tuple[int, int]], int]:
    """一行的数学字形区间 `[(a, b), …]`（行内**原串**下标）。

    相邻的数学 run 会合并成一段，**纯空白的数学 run 也算在内**：数学排版里的间距
    字形(如 `CMSY10` 的空格)会把 `′` 和 `×` 隔开，不合并就断成两段。
    """
    runs = line.get("runs") or []
    bounds: list[tuple[int, int]] = []
    levels: list[int] = []
    pos = 0
    for r in runs:
        t = r.get("t") or ""
        bounds.append((pos, pos + len(t)))
        levels.append(_font_math_level(r.get("f") or "", weak_on) if t else 0)
        pos += len(t)
    spans: list[tuple[int, int]] = []
    math_chars = 0
    for i, lvl in enumerate(levels):
        a, b = bounds[i]
        if not lvl or b <= a:
            continue
        math_chars += b - a
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], b)
        else:
            spans.append((a, b))
    return spans, math_chars


def _font_math_index(metas: list[dict], local_texts: list[dict]
                     ) -> tuple[dict[int, list[tuple[int, int]]], dict]:
    """[已停用] 每个行下标 → 该行的数学字形区间（原 OCR 配对链路用）。"""
    return {}, {}


def _cm_text_math_on(texts: list[dict], threshold: float = 0.5) -> bool:
    """CM 系**文字**字体(CMR/CMBX/CMTI…)到底算不算公式 —— 按**占比**判。

    为什么得单独判：`_weak_math_enabled` 的开关是「整页主字体是不是弱数学字体」，
    而实测 `samples/latex_sample.pdf` 的正文是 `LMRoman10-Regular`(65.7%，它**本身**
    就在弱字体表里)、公式的数字与运算符是 `CMR10`/`CMR7`(11.1%)。共用一个开关时
    “正文是弱字体 → 弱判据整体关掉”，公式里的 `=`/`+`/数字就全留在正文里。
    """
    counts: dict[str, int] = {}
    for blk in texts or []:
        for ln in blk.get("lines") or []:
            for r in ln.get("runs") or []:
                f = r.get("f") or ""
                counts[f] = counts.get(f, 0) + len(r.get("t") or "")
    total = sum(counts.values())
    if not total:
        return False
    cm = sum(n for f, n in counts.items() if _CM_TEXT_RE.match(_font_base(f)))
    return cm / total < threshold


def _math_font_flags(texts: list[dict]) -> tuple[bool, bool]:
    """本页字体判据的两个开关 `(cm_on, weak_on)` —— 两条都是**页级**判据。

    必须在**同一份 run 集合**上算一次、多处复用：`text_layer._insert_gap_spaces`
    与 `sentences` 的探针文本用的是同一套 `_blank_math_level` 判据。
    """
    return _cm_text_math_on(texts), _weak_math_enabled(texts)


def _accumulate_stats(acc: Optional[dict], add: dict) -> None:
    """把单页统计累加进整份文档的统计(`acc` 为 None 时忽略)。"""
    if acc is None:
        return
    for k, v in (add or {}).items():
        acc[k] = acc.get(k, 0) + v


def _blank_math_level(font: str, cm_on: bool, weak_on: bool) -> int:
    """字体分级：0=正文 1=弱(CM 系按占比判、其余按主字体判) 2=强数学字体。"""
    if _font_math_level(font, weak_on):
        return _font_math_level(font, weak_on)
    if cm_on and _CM_TEXT_RE.match(_font_base(font)):
        return 1
    return 0


def _kept_width(geo: list[tuple], erased: set) -> tuple[float, bool]:
    """[已停用] 保留正文在原稿里占的总宽。"""
    return 0.0, False


def _line_gap_widths(ln: dict, geo: list[tuple], gaps: list[dict],
                     erased: set) -> list[Optional[float]]:
    """[已停用] 一条行里每条空位的原宽。"""
    return [None] * len(gaps)


def _split_count(n: int, widths: list[Optional[float]]) -> list[int]:
    """[已停用] 把 `n` 个空格分给一处空位覆盖的几个 run。"""
    return []


def _blank_math_spans(texts: list[dict]) -> tuple[list[dict], dict]:
    """[已停用] 把数学字体的 run 就地换成等长空格。"""
    return [], {}


def _group_math_pieces(texts: list[dict], segs: list[dict]) -> list[dict]:
    """[已停用] 把同一行里挨得够近的几段空格并成一条公式。"""
    return []


def _line_text(line: dict) -> str:
    """[已停用] 一行擦除后的原文。"""
    return ""


def _mask_out(raw: str, spans) -> str:
    """[已停用] 把数学字形区间从原串里抹掉。"""
    return ""


def _has_symbol_font(line: dict) -> bool:
    """[已停用] 这行用没用「符号专用」数学字体。"""
    return False


def _font_math_line(raw: str, spans, covered: int) -> bool:
    """[已停用] 这一行能不能整行当公式摘掉。"""
    return False


def _widen_with_cand(a: int, b: int, spans) -> list[tuple[int, int]]:
    """[已停用] 把区间与字体候选区取并集。"""
    return [(a, b)]


def _retighten(raw: str, a: int, b: int) -> tuple[int, int]:
    """[已停用] 扩张之后把区间再收紧一次。"""
    return a, b


def _widen_cut(pos: int, spans, *, left: bool) -> int:
    """[已停用] 把切点吸附到字体候选区边界上。"""
    return pos
