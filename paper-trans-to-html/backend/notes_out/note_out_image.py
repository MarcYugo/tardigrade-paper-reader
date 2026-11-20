"""模块十 · 附：笔记导出**图片**（Matplotlib + XKCD 手绘风）

把 `note_out.export_paper()` 的导出结果画成一张 PNG，**版式固定**：

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ TARDIGRADE · 论文笔记导出                              机械导出 · 日期    │
│ ┌────────────┐ ┌──────────────────────────┐ ┌───────────────────────────┐ │
│ │1 论文概览  │ │2 笔记与原文              │ │3 AI 整理笔记              │ │
│ │ 标题       │ │  P3 · 第 2 句            │ │ 总览 …                   │ │
│ │ 作者       │ │  原文 │ 笔记        │ │ 主题 …                   │ │
│ │ 统计数字   │ │  “…”  │ …            │ │ 关联 / 疑问 / 下一步      │ │
│ └────────────┘ └──────────────────────────┘ └───────────────────────────┘ │
│ Matplotlib · XKCD 风格                                          页脚     │
└───────────────────────────────────────────────────────────────────────────┘
```

* 画布固定 **16:9**：内容越多就**整体变宽**（高度按比例一起长），
  不把整理笔记截短；只有到 `_W_HARD_MAX` 这种极端尺寸才截断；
  三栏宽度按内容量分配（`√内容量` 阻尼 + 24%~40% 的份额上下限）；
* 三栏从左到右：**概览**（题目 / 作者 / 笔记数 / 参考文献数…）、
  **笔记与原文**（左右双栏：**左原文 / 右笔记**，中间一道竖线）、
  **AI 整理笔记**（AI 辅助阅读面板里「整理笔记」的内容）；
* 配色跟随用户当前主题（`light` / `dark`），与阅读页一致；
* 文本里的 **`$…$` LaTeX 公式会用 matplotlib mathtext 渲染**（按片段测宽度混排）；
  mathtext 不支持的写法或不含数学符号的 `$…$`（如货币）回退成原文；
* 绘制用 Matplotlib 的 XKCD 模式（`path.sketch` + 手写体字体族）。

本模块只做**纯数据转换**，不碰存储与权限；matplotlib 在函数内**延迟导入**，
所以不能画图的环境（没装 matplotlib）也不会影响正常的 JSON 导出。
"""
from __future__ import annotations

import io
import math
import re
import threading
import unicodedata

__all__ = ["render", "THEMES"]

# =====================================================================
#  主题（与 frontend/css/style.css 的 :root / html.dark 对齐）
# =====================================================================
THEMES = {
    "light": {
        "bg": "#eef1f6",          # --bg
        "panel": "#ffffff",       # --page-bg
        "ink": "#1f2430",         # --ink
        "muted": "#5b6675",       # 次级文字
        "line": "#c3cddc",        # --line-strong
        "accent": "#2f6fed",      # --accent
        "chip": "#e9f0fe",        # --accent-soft
    },
    "dark": {
        "bg": "#22262d",
        "panel": "#2b3038",
        "ink": "#edeff3",
        "muted": "#9aa6b6",
        "line": "#3a414c",
        "accent": "#6ea8ff",
        "chip": "#2b3a53",
    },
}

# =====================================================================
#  版式常量（固定版式，只改内容不改结构）
# =====================================================================
_F_TITLE = 15.5          # 论文标题
_F_HEAD = 10.6           # 栏目名
_F_BODY = 9.3            # 正文
_F_SMALL = 8.0           # 引文 / 小字
_F_CRUMB = 8.2           # 页眉页脚
_LH = 1.56               # 行高倍率
_CELL_EM = 0.52          # 一个「显示格」≈ 0.52 em
_PAD = 0.30              # 栏内边距（英寸）
_GAP = 0.32              # 栏间距（英寸）
_MARGIN = 0.44           # 画布外边距（英寸）
_HEADER_H = 0.52         # 页眉高
_FOOTER_H = 0.34         # 页脚高
_KEY_CELLS = 9           # 「键值行」键名占的显示格
_TRUNC_RESERVE = 0.55    # 尾部省略提示占的高度（英寸），截断时预留
_SPLIT_DIV = 0.5         # 双栏竖线：相对左栏右边界的偏移（显示格）
_SPLIT_GAP = 1.5         # 双栏右栏文字起点：相对左栏右边界的偏移（显示格，给竖线留空）

_CELL_BASE = 40          # 内容量为均值时的栏宽权重（显示格）
_CELL_MIN = 25
_CELL_MAX = 58

# 画布：16:9；内容多就整体变宽（高度随之增长），宽度不设下限以外的上限
_ASPECT = 9.0 / 16.0
_W_MIN = 19.0            # 起始宽度（英寸）
_W_HARD_MAX = 60.0       # 极端情况下的安全上限（到顶才截断尾部内容）
_SHARE_MIN = 0.24        # 单栏最少占总内容宽的比例
_SHARE_MAX = 0.40        # 单栏最多占比（防止某一栏吃掉全部宽度）

_LOCK = threading.Lock()


# =====================================================================
#  文本度量 / 折行
# =====================================================================
def _norm(value) -> str:
    return " ".join(str(value or "").split())


def _wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _cells(text) -> int:
    """显示格数：中日韩全角字符 2 格，其余 1 格。"""
    return sum(2 if _wide(ch) else 1 for ch in str(text or ""))


# 一个「词」：数学片段 ($$…$$ / $…$ / \(…\) / \[…\]) 优先，其次 CJK 单字、
# 西文连续串、空白。数学片段整体不拆，避免 $…$ 被折行拆断。
_CJK = r"\u2e80-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef"
_MATH_SRC = r"\$\$.+?\$\$|\$[^$\n]+?\$|\\\(.+?\\\)|\\\[.+?\\\]"
_TOKEN = re.compile(
    rf"(?:{_MATH_SRC})|[{_CJK}]|[^\s{_CJK}]+|\s+", re.S)
_MATH_RE = re.compile(_MATH_SRC, re.S)


def _math_inner(tok: str) -> str:
    if tok.startswith("$$"):
        return tok[2:-2]
    if tok.startswith("$"):
        return tok[1:-1]
    return tok[2:-2]                      # \( … \) / \[ … \]


def _math_cells(tok: str) -> int:
    """估算一段 LaTeX 渲染后的显示格数（保守一点，避免顶出栏宽）。"""
    s = _math_inner(tok)
    s = re.sub(r"\\(?:left|right|displaystyle|,|;|!|quad|qquad)\b|\\[,;! ]", "", s)
    s = re.sub(r"\\(?:mathrm|text|mathbf|mathit|mathsf|mathtt)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"\1\2", s)
    s = re.sub(r"\\[a-zA-Z]+", "x", s)
    s = re.sub(r"[{}^_]", "", s)
    s = re.sub(r"\s+", "", s)
    return max(3, int(round(len(s) * 0.95)))


def _token_cells(tok: str) -> int:
    if tok and (tok[0] == "$" or tok.startswith("\\(") or tok.startswith("\\[")):
        return _math_cells(tok)
    return _cells(tok)


def _wrap(text, cells: int) -> list[str]:
    """按显示格折行（CJK 逐字可断、西文按词断、数学片段整体不拆）。"""
    text = _norm(text)
    if not text:
        return []
    cells = max(4, int(cells))
    lines: list[str] = []
    cur, cur_w = "", 0
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if not tok:
            continue
        if tok.isspace():
            if cur:
                cur += " "
                cur_w += 1
            continue
        tw = _token_cells(tok)
        is_math = tok[0] == "$" or tok.startswith("\\(") or tok.startswith("\\[")
        if cur and cur_w + tw > cells:
            lines.append(cur.rstrip())
            cur, cur_w = "", 0
        if tw > cells and not is_math:       # 超长单词 / URL：优先在 _ / - 处断，否则硬切
            for chunk in _hard_split(tok, cells):
                if cur:
                    lines.append(cur.rstrip())
                    cur, cur_w = "", 0
                cur, cur_w = chunk, _cells(chunk)
            continue
        cur += tok
        cur_w += tw
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def _looks_math(inner: str) -> bool:
    """防误判：`$5 和 $10` 这种「货币」不当公式。"""
    s = inner.strip()
    if not s:
        return False
    if re.search(r"[\\^_{}=<>()]", s):         # 命令 / 上下标 / 括号 / 等号
        return True
    return bool(len(s) <= 8 and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", s))


def _segments(text: str) -> list[tuple[bool, str]]:
    """把一行拆成 `[(是否公式, 文本)]`；未配对 / 不像公式的 `$…$` 当普通文本。"""
    out: list[tuple[bool, str]] = []

    def push(is_math: bool, s: str) -> None:
        if not s:
            return
        if out and not out[-1][0] and not is_math:      # 相邻纯文本合并，少画几个 text
            out[-1] = (False, out[-1][1] + s)
        else:
            out.append((is_math, s))

    pos = 0
    for m in _MATH_RE.finditer(text):
        push(False, text[pos:m.start()])
        raw = m.group(0)
        inner = _math_inner(raw).strip()
        if inner and _looks_math(inner):
            push(True, "$" + inner + "$")
        else:
            push(False, raw)
        pos = m.end()
    push(False, text[pos:])
    return out


_BREAKS = set("_-/.:")


def _hard_split(tok: str, cells: int) -> list[str]:
    """把过长的串切成不超过 `cells` 的块，优先在分隔符后断开。"""
    out: list[str] = []
    rest = tok
    while rest:
        w, cut = 0, 0
        for ch in rest:
            cw = 2 if _wide(ch) else 1
            if w + cw > cells:
                break
            w += cw
            cut += 1
        if cut >= len(rest):
            out.append(rest)
            break
        # 在切点附近找个分隔符当断点（不能断得太短）
        best = -1
        for idx in range(min(cut, len(rest) - 1), max(1, cut // 2) - 1, -1):
            if rest[idx - 1] in _BREAKS:
                best = idx
                break
        cut = best if best > 0 else cut
        out.append(rest[:cut])
        rest = rest[cut:]
    return out


def _date(value) -> str:
    text = str(value or "").strip().replace("T", " ")
    return text[:16] if text else ""


# =====================================================================
#  栏目内容模型（先量后画）
# =====================================================================
class _Col:
    """一栏的「行」集合。行只有：文字 / 左右双栏文字 / 分隔线 / 空白。"""

    def __init__(self, cells: int, split_cells: int | None = None):
        self.cells = cells
        self.split_cells = split_cells          # 双栏行的左栏宽度（显示格）
        self.rows: list[dict] = []

    # -- 行构造 ---------------------------------------------------------
    def _scale(self, size: float) -> int:
        """字号越大，同样物理宽度能放的格数越少。"""
        return max(6, int(self.cells * (_F_BODY / size)))

    def head(self, text, *, gap=0.16, size=_F_HEAD):
        self.rows.append({"t": "text", "size": size, "color": "accent",
                          "gap": gap, "indent": 0,
                          "lines": _wrap(text, self._scale(size))})

    def label(self, text, *, gap=0.13, size=_F_BODY):
        self.rows.append({"t": "text", "size": size, "color": "accent",
                          "gap": gap, "indent": 0,
                          "lines": _wrap(text, self._scale(size))})

    def para(self, text, *, size=_F_BODY, color="ink", gap=0.10,
             indent=0, prefix=""):
        cells = max(6, self._scale(size) - indent)
        lines = _wrap(text, cells - _cells(prefix) if prefix else cells)
        if not lines:
            return
        if prefix:
            lines[0] = prefix + lines[0]
        self.rows.append({"t": "text", "size": size, "color": color,
                          "gap": gap, "indent": indent, "lines": lines})

    def kv(self, key, value, *, gap=0.09):
        value_lines = _wrap(value, max(6, self.cells - _KEY_CELLS - 1))
        self.rows.append({"t": "kv", "size": _F_BODY, "gap": gap,
                          "key_cells": _KEY_CELLS, "key": str(key),
                          "value_lines": value_lines})

    def rule(self, gap=0.14):
        self.rows.append({"t": "rule", "gap": gap})

    def split(self, left, right, *, left_size=_F_SMALL, right_size=_F_BODY,
              left_color="muted", right_color="ink", gap=0.08):
        """一行里左右并排两段文字（左原文 / 右笔记）。"""
        left_cells = self.split_cells or max(8, int(self.cells * 0.42))
        right_cells = max(6, self.cells - left_cells - _SPLIT_GAP)
        lc = max(6, int(left_cells * (_F_BODY / left_size)) - 1)
        rc = max(6, int(right_cells * (_F_BODY / right_size)))
        self.rows.append({
            "t": "split", "gap": gap, "left_cells": left_cells,
            "left_size": left_size, "right_size": right_size,
            "left_color": left_color, "right_color": right_color,
            "left_lines": _wrap(left, lc), "right_lines": _wrap(right, rc),
        })

    def space(self, h: float):
        self.rows.append({"t": "space", "h": h})

    # -- 高度 -----------------------------------------------------------
    def height(self) -> float:
        total = 2 * _PAD
        for row in self.rows:
            total += _row_h(row)
        return total


def _row_h(row: dict) -> float:
    if row["t"] == "space":
        return row["h"]
    if row["t"] == "rule":
        return row["gap"] + 0.016
    if row["t"] == "split":
        n = max(len(row["left_lines"]), len(row["right_lines"]))
        lh = max(row["left_size"], row["right_size"]) * _LH / 72.0
        return row["gap"] + n * lh
    lines = row.get("lines") or row.get("value_lines") or []
    return row["gap"] + len(lines) * row["size"] * _LH / 72.0


def _col_w(cells: int) -> float:
    return 2 * _PAD + cells * _CELL_EM * _F_BODY / 72.0


# =====================================================================
#  内容 → 三栏
# =====================================================================
def _note_items(notes: dict) -> list[dict]:
    rows: list[dict] = []
    for n in notes.get("sentence_notes") or []:
        if not isinstance(n, dict):
            continue
        rows.append({"kind": "sentence",
                     "page": int(n.get("page") or 0),
                     "si": int(n.get("sentence_index") or -1),
                     "sentence": _norm(n.get("sentence")),
                     "note": _norm(n.get("note")),
                     "author": _norm(n.get("author"))})
    for n in notes.get("free_notes") or []:
        if not isinstance(n, dict):
            continue
        rows.append({"kind": "free", "page": 0, "si": -1,
                     "title": _norm(n.get("title")),
                     "sentence": "",
                     "note": _norm(n.get("text")),
                     "author": _norm(n.get("author"))})
    rows.sort(key=lambda r: (r["kind"] != "sentence",
                             r["page"] or 10 ** 6, max(0, r["si"])))
    return rows


def _content_cells(export: dict) -> tuple[int, int, int]:
    """三栏的「内容量」（显示格）。用于算栏宽。"""
    paper = export.get("paper") or {}
    notes = export.get("notes") or {}
    summary = (export.get("ai_summary") or {}).get("summary") or {}
    report = export.get("report") or {}

    m1 = max(320, _cells(paper.get("title")) + 180)
    if export.get("mode") == "agent":
        m1 += _cells(report.get("summary"))
        ref_stats = report.get("references_stats") or {}
        m1 += sum(_cells(ref_stats.get(key)) for key in
                  ("total", "checked", "matched", "verified", "likely",
                   "not_found", "unverified", "not_checked"))
    items = _note_items(notes)
    m2 = sum(_cells(i.get("sentence")) + _cells(i.get("note")) for i in items)
    m2 = max(m2 + 24 * len(items), 80)

    parts = [_cells(summary.get("overview"))]
    for theme in summary.get("themes") or []:
        parts.append(_cells((theme or {}).get("title")))
        parts.extend(_cells(p) for p in (theme or {}).get("points") or [])
    for key in ("connections", "questions", "next"):
        parts.extend(_cells(x) for x in summary.get(key) or [])
    m3 = max(sum(parts), 80)
    return m1, m2, m3


def _cells_for(mass: int, ref: float) -> int:
    k = math.sqrt(mass / ref) if ref > 0 else 1.0
    return int(max(_CELL_MIN, min(_CELL_MAX, round(_CELL_BASE * k))))


def _build_info_col(export: dict, cells: int) -> _Col:
    paper = export.get("paper") or {}
    notes = export.get("notes") or {}
    stats = notes.get("stats") or {}
    sections = export.get("sections") or {}
    refs = sections.get("references") or {}

    col = _Col(cells)
    col.head("论文概览", gap=0.02)
    col.para(paper.get("title") or "未命名论文", size=_F_TITLE, gap=0.10)

    authors = paper.get("authors") or []
    col.para("作者：" + ("、".join(authors) if authors else "（未识别作者）"),
             size=_F_SMALL, color="muted", gap=0.05)
    col.rule(gap=0.16)

    for key, value in (
        ("笔记", f"{stats.get('notes_total', 0)} 条"),
        ("参考文献", f"{refs.get('count') or 0} 条"),
        ("页数", f"{paper.get('num_pages') or '?'} 页"),
        ("高亮", f"{stats.get('highlights', 0)} 处"),
        ("解析", paper.get("parser") or "—"),
        ("导出", _date(export.get("exported_at")) or "—"),
    ):
        col.kv(key, value)

    if export.get("mode") == "agent":
        report = export.get("report") or {}
        report_summary = _norm(report.get("summary"))
        ref_stats = report.get("references_stats") or {}
        if report_summary or ref_stats:
            col.rule(gap=0.18)
            col.label("Agent 文献检查与分析", gap=0.06)
            if report_summary:
                col.para(report_summary, size=_F_SMALL, gap=0.08)
            if ref_stats:
                counts = (
                    ("总数", ref_stats.get("total")),
                    ("已核验", ref_stats.get("checked")),
                    ("匹配", ref_stats.get("matched")),
                    ("确认存在", ref_stats.get("verified")),
                    ("高度疑似", ref_stats.get("likely")),
                    ("未找到", ref_stats.get("not_found")),
                    ("待核验", ref_stats.get("unverified")),
                    ("未检查", ref_stats.get("not_checked")),
                )
                col.para("文献核验：" + " · ".join(
                    f"{label} {value}" for label, value in counts
                    if value is not None), size=_F_SMALL, color="muted", gap=0.05)
    else:
        col.rule(gap=0.18)
        col.para("机械导出：本地统计，未调用大模型。", size=_F_SMALL, color="muted")
    return col


def _note_entry_rows(it: dict, cells: int, split_cells: int) -> list[dict]:
    """一条笔记：小标签行 + 左右双栏行（左原文 / 右笔记）。"""
    rows: list[dict] = []
    if it["kind"] == "free":
        label = "独立笔记" + ((" · " + it["title"]) if it.get("title") else "")
        left = "（独立笔记）"
        right = it.get("note") or "（空笔记）"
    else:
        label = f"P{it['page']}" if it.get("page") else "原文"
        if it.get("si", -1) > 0:
            label += f" · 第 {it['si'] + 1} 句"
        if it.get("author"):
            label += " · " + it["author"]
        left = ("“" + it["sentence"] + "”") if it.get("sentence") else "—"
        right = it.get("note") or "（空笔记）"

    tmp = _Col(cells, split_cells)
    tmp.label(label, gap=0.10, size=_F_SMALL)
    tmp.split(left, right, left_size=_F_SMALL, right_size=_F_BODY, gap=0.05)
    rows.extend(tmp.rows)
    return rows


def _build_notes_col(items: list[dict], cells: int, max_h: float) -> _Col:
    """笔记栏：左右双栏（左原文 / 右笔记），超出高度预算时从尾部截断。"""
    split_cells = max(9, int(cells * 0.44))
    col = _Col(cells, split_cells)
    col.head("笔记与原文", gap=0.02)
    if not items:
        col.para("（还没有笔记：在正文里选中句子，就能写下属于这句话的笔记。）",
                 color="muted", size=_F_SMALL, gap=0.12)
        return col

    # 双栏表头 + 一道横线（下面是「原文 | 笔记」两列）
    col.split("原文", "笔记", left_size=_F_SMALL, right_size=_F_SMALL,
              left_color="accent", right_color="accent", gap=0.04)
    col.rule(gap=0.05)

    total = len(items)
    used = 0
    used_h = col.height()
    truncated = False
    for it in items:
        rows = _note_entry_rows(it, cells, split_cells)
        if used:
            rows = [{"t": "rule", "gap": 0.16}] + rows
        rows_h = sum(_row_h(r) for r in rows)
        if used_h + rows_h > max_h - _TRUNC_RESERVE:
            truncated = True
            break
        col.rows.extend(rows)
        used_h += rows_h
        used += 1

    if truncated:
        col.rule(gap=0.14)
        col.para(f"…… 还有 {total - used} 条笔记未显示（图片版式高度上限）",
                 size=_F_SMALL, color="muted")
    return col


def _bullet(text) -> str:
    """去掉 AI 自己写的列表符号（我们统一用「· 」），避免「· - xxx」双重符号。"""
    return re.sub(r"^[-–—•*·]\s*", "", _norm(text))


def _ai_chunks(summary: dict, cells: int) -> list[list[dict]]:
    """把整理笔记切成「可整块丢弃」的片段（总览 / 每个主题 / 关联 / 疑问 / 下一步）。"""
    chunks: list[list[dict]] = []
    if summary.get("overview"):
        tmp = _Col(cells)
        tmp.label("总览", gap=0.06)
        tmp.para(summary["overview"], gap=0.04)
        chunks.append(tmp.rows)

    themes = summary.get("themes") or []
    for idx, theme in enumerate(themes):
        theme = theme or {}
        tmp = _Col(cells)
        if idx == 0:
            tmp.label("主题", gap=0.16)
        tmp.para(theme.get("title") or "要点", gap=0.04 if idx else 0.10)
        for point in theme.get("points") or []:
            tmp.para(_bullet(point), size=_F_SMALL, color="muted",
                     indent=1, prefix="· ", gap=0.03)
        chunks.append(tmp.rows)

    for key, label in (("connections", "关联"), ("questions", "待想清楚"),
                       ("next", "下一步")):
        rows = summary.get(key) or []
        if not rows:
            continue
        tmp = _Col(cells)
        tmp.label(label, gap=0.16)
        for row in rows:
            tmp.para(_bullet(row), size=_F_SMALL, color="muted",
                     indent=1, prefix="· ", gap=0.03)
        chunks.append(tmp.rows)
    return chunks


def _build_ai_col(export: dict, cells: int, max_h: float) -> _Col:
    """AI 整理笔记：按高度预算逐块加入，放不下的整块丢掉并给省略提示。"""
    col = _Col(cells)
    col.head("AI 整理笔记", gap=0.02)
    ai = export.get("ai_summary") or {}
    summary = ai.get("summary") or {}
    if not summary:
        col.para("（AI 辅助阅读面板里还没有「整理笔记」。先在文末 AI 栏点一下整理，再导出图片。）",
                 color="muted", size=_F_SMALL, gap=0.12)
        return col

    truncated = False
    used_h = col.height()
    for rows in _ai_chunks(summary, cells):
        rows_h = sum(_row_h(r) for r in rows)
        if used_h + rows_h <= max_h - _TRUNC_RESERVE:
            col.rows.extend(rows)
            used_h += rows_h
            continue
        # 整块放不下：能塞几行是几行（保留前半段上下文）
        for row in rows:
            h = _row_h(row)
            if used_h + h <= max_h - _TRUNC_RESERVE:
                col.rows.append(row)
                used_h += h
            else:
                break
        truncated = True
        break

    at = _date(ai.get("at"))
    tail = [{"t": "rule", "gap": 0.16}] if at else []
    if at:
        tmp = _Col(cells)
        tmp.para("整理时间：" + at, size=_F_SMALL, color="muted")
        tail.extend(tmp.rows)
    tail_h = sum(_row_h(r) for r in tail)
    reserve = _TRUNC_RESERVE if truncated else 0.0
    if tail and used_h + tail_h <= max_h - reserve:
        col.rows.extend(tail)
        used_h += tail_h

    if truncated:
        col.rule(gap=0.14)
        col.para("……（整理笔记其余内容已省略，完整内容可在 AI 面板查看）",
                 size=_F_SMALL, color="muted")
    return col


# =====================================================================
#  Matplotlib（延迟导入）
# =====================================================================
def _mpl():
    try:
        import matplotlib
        matplotlib.use("Agg", force=False)
        from matplotlib import font_manager, patheffects  # noqa: F401
        from matplotlib import pyplot as plt
        from matplotlib.patches import FancyBboxPatch
    except Exception as exc:                    # pragma: no cover - 环境缺依赖
        raise RuntimeError(
            "图片导出需要 matplotlib（pip install matplotlib）：" + str(exc)) from exc
    return matplotlib, plt, font_manager, FancyBboxPatch


def _font_family(font_manager) -> list[str]:
    """手写体优先（XKCD 观感），中文字形由后面的字体兜底。"""
    names = {f.name for f in font_manager.fontManager.ttflist}
    want = ["xkcd", "xkcd Script", "Humor Sans", "Comic Sans MS", "Comic Neue",
            "Microsoft YaHei", "微软雅黑", "SimHei", "Noto Sans CJK SC",
            "Source Han Sans SC", "PingFang SC", "WenQuanYi Zen Hei", "DejaVu Sans"]
    family = [name for name in want if name in names]
    return family or ["sans-serif"]


# =====================================================================
#  绘制
# =====================================================================
def render(export: dict, *, theme: str = "light", dpi: int = 180) -> bytes:
    """把导出 JSON 画成 PNG 字节流。`theme` 取 `light` / `dark`。"""
    if not isinstance(export, dict):
        raise ValueError("导出数据为空")
    pal = THEMES.get(str(theme or "").lower()) or THEMES["light"]
    with _LOCK:
        return _render(export, pal, max(96, min(400, int(dpi or 180))))


def _allocate(fig_w: float, m1: int, m2: int, m3: int) -> tuple[list[float], list[int]]:
    """按内容量把画布宽度分给三栏（单栏份额 24%~40%）。"""
    cell_in = _CELL_EM * _F_BODY / 72.0
    usable = fig_w - 2 * _MARGIN - 2 * _GAP
    room_cells = max(30.0, (usable - 6 * _PAD) / cell_in)   # 三栏各扣掉左右内边距

    ref = (m1 + m2 + m3) / 3.0
    weights = [_cells_for(m1, ref), _cells_for(m2, ref), _cells_for(m3, ref)]
    wsum = float(sum(weights)) or 1.0
    shares = [min(_SHARE_MAX, max(_SHARE_MIN, w / wsum)) for w in weights]
    ssum = sum(shares) or 1.0
    shares = [s / ssum for s in shares]

    cells = [int(room_cells * s) for s in shares]
    cells[-1] += int(round(room_cells)) - sum(cells)   # 取整误差给最后一栏
    cells = [max(_CELL_MIN // 2, c) for c in cells]
    return [_col_w(c) for c in cells], cells


def _plan(export: dict):
    """16:9 画布规划：内容放不下就**加宽**（高度跟着长），而不是截断内容。

    只做测量（`max_h=∞`）来确定需要多宽；最后再按真实高度预算建一次，
    正常情况下不会触发截断，只有到 `_W_HARD_MAX` 这种极端尺寸才截尾。
    """
    m1, m2, m3 = _content_cells(export)
    items = _note_items(export.get("notes") or {})
    fixed_v = 2 * _MARGIN + _HEADER_H + _FOOTER_H

    fig_w = _W_MIN
    body_h = fig_w * _ASPECT - fixed_v
    widths, cells = _allocate(fig_w, m1, m2, m3)
    for _ in range(24):
        fig_h = fig_w * _ASPECT
        body_h = fig_h - fixed_v
        widths, cells = _allocate(fig_w, m1, m2, m3)
        probe = [_build_info_col(export, cells[0]),
                 _build_notes_col(items, cells[1], float("inf")),
                 _build_ai_col(export, cells[2], float("inf"))]
        need = max(c.height() for c in probe)
        if need <= body_h + 1e-6 or fig_w >= _W_HARD_MAX:
            break
        # 逐步加宽（不一步跳到「按比例算出来的宽度」—— 加宽后行数是非线性下降的）
        fig_w = min(_W_HARD_MAX, fig_w * 1.12)

    cols = [_build_info_col(export, cells[0]),
            _build_notes_col(items, cells[1], body_h),
            _build_ai_col(export, cells[2], body_h)]
    return fig_w, fig_h, body_h, widths, cells, cols


def _render(export: dict, pal: dict, dpi: int) -> bytes:
    _, plt, font_manager, FancyBboxPatch = _mpl()

    # ---- 1) 16:9 画布 + 三栏宽度（内容多 → 整体变宽） --------------
    fig_w, fig_h, body_h, widths, cells, cols = _plan(export)
    heights = [body_h] * 3                 # 统一栏高：一页三栏、底边对齐

    # 画布很宽时收敛 dpi，别让 PNG 像素尺寸失控（视觉尺寸不变）
    dpi = max(90, min(dpi, int(4200 / max(1.0, fig_w))))

    # ---- 3) 绘制 ---------------------------------------------------
    family = _font_family(font_manager)
    rc = {
        "font.family": family,
        "font.size": _F_BODY,
        "path.sketch": (1.0, 100.0, 2.0),        # XKCD 手绘抖动
        "path.effects": [],
        "text.parse_math": False,                 # 默认不解析；$公式$ 片段单独用 parse_math=True
        "axes.linewidth": 1.3,
        "axes.unicode_minus": False,
        "figure.facecolor": pal["bg"],
        "savefig.facecolor": pal["bg"],
        "text.color": pal["ink"],
        "lines.solid_capstyle": "round",
    }
    with plt.rc_context(rc):
        fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi)
        try:
            ax = fig.add_axes([0, 0, 1, 1])
            ax.set_xlim(0, fig_w)
            ax.set_ylim(fig_h, 0)               # y 向下长，和「书页」一致
            ax.axis("off")
            ax.set_facecolor(pal["bg"])

            _draw_header(ax, pal, fig_w, export)
            top = _MARGIN + _HEADER_H
            try:
                renderer = fig.canvas.get_renderer()
            except Exception:               # pragma: no cover - 非 Agg 后端
                renderer = None
            x = _MARGIN
            for col, width, height in zip(cols, widths, heights):
                _draw_panel(ax, col, pal, x, top, width, height, FancyBboxPatch,
                            renderer=renderer, dpi=dpi)
                x += width + _GAP
            _draw_footer(ax, pal, fig_w, fig_h, export)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=dpi, facecolor=pal["bg"])
        finally:
            plt.close(fig)
    return buf.getvalue()


def _draw_header(ax, pal, fig_w, export):
    y = _MARGIN + 0.26
    ax.text(_MARGIN, y, "TARDIGRADE · 论文笔记导出",
            fontsize=_F_CRUMB + 1.6, color=pal["ink"], va="baseline")
    right = "机械导出" if export.get("mode") != "agent" else "Agent 导出"
    stamp = _date(export.get("exported_at"))
    if stamp:
        right += " · " + stamp
    ax.text(fig_w - _MARGIN, y, right, fontsize=_F_CRUMB,
            color=pal["muted"], va="baseline", ha="right")
    ax.plot([_MARGIN, fig_w - _MARGIN], [y + 0.09, y + 0.09],
            color=pal["line"], linewidth=1.2)


def _text_obj(ax, x, y, s, *, size, color, parse_math, zorder=4):
    kw = dict(fontsize=size, color=color, va="baseline", zorder=zorder)
    try:
        return ax.text(x, y, s, parse_math=parse_math, **kw)
    except TypeError:                    # 老版本 matplotlib 没有 parse_math
        return ax.text(x, y, s, **kw)


def _put_text(ax, renderer, dpi, x, y, s, *, size, color, zorder=4) -> float:
    """画一行（可混排 `$公式$`），返回行尾 x（英寸）。

    * 普通文字用 `parse_math=False`，保证笔记里没配对的 `$` 不会被当成公式；
    * 公式片段用 mathtext 实测宽度顺序摆放；
    * mathtext 不支持的写法（`\text` / `\begin{matrix}` 等）自动回退成原文。
    """
    cx = x
    for is_math, seg in _segments(s):
        if not seg:
            continue
        t = None
        if is_math:
            t = _text_obj(ax, cx, y, seg, size=size, color=color,
                          parse_math=True, zorder=zorder)
            try:
                t.get_window_extent(renderer)      # 触发一次排版，验证公式可用
            except Exception:
                t.remove()
                t = None
        if t is None:
            t = _text_obj(ax, cx, y, seg, size=size, color=color,
                          parse_math=False, zorder=zorder)
        w = None
        if renderer is not None:
            try:
                w = t.get_window_extent(renderer).width / float(dpi)
            except Exception:
                w = None
        if w is None:
            w = _token_cells(seg) * _CELL_EM * size / 72.0
        cx += w
    return cx


def _draw_panel(ax, col, pal, x, y, w, h, FancyBboxPatch,
                renderer=None, dpi=180):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0,rounding_size=0.16",
        linewidth=1.4, edgecolor=pal["line"], facecolor=pal["panel"],
        zorder=2))
    cell_in = _CELL_EM * _F_BODY / 72.0
    cx = x + _PAD
    cy = y + _PAD
    right = x + w - _PAD
    div_x = (cx + (col.split_cells + _SPLIT_DIV) * cell_in) if col.split_cells else None
    div_top = div_bot = None
    for row in col.rows:
        if row["t"] == "space":
            cy += row["h"]
            continue
        if row["t"] == "rule":
            cy += row["gap"]
            ax.plot([cx, right], [cy, cy], color=pal["line"],
                    linewidth=1.0, zorder=3)
            cy += 0.016
            continue
        if row["t"] == "split":
            lh = max(row["left_size"], row["right_size"]) * _LH / 72.0
            n = max(len(row["left_lines"]), len(row["right_lines"]))
            cy += row["gap"]
            for i, line in enumerate(row["left_lines"]):
                _put_text(ax, renderer, dpi, cx, cy + i * lh + lh * 0.80, line,
                          size=row["left_size"],
                          color=pal.get(row["left_color"], pal["muted"]))
            rx = cx + (row["left_cells"] + _SPLIT_GAP) * cell_in
            for i, line in enumerate(row["right_lines"]):
                _put_text(ax, renderer, dpi, rx, cy + i * lh + lh * 0.80, line,
                          size=row["right_size"],
                          color=pal.get(row["right_color"], pal["ink"]))
            div_top = cy if div_top is None else min(div_top, cy)
            div_bot = max(div_bot or cy, cy + n * lh)
            cy += n * lh
            continue
        size = row["size"]
        lh = size * _LH / 72.0
        cy += row["gap"]
        if row["t"] == "kv":
            _put_text(ax, renderer, dpi, cx, cy + lh * 0.80, row["key"],
                      size=size, color=pal["muted"])
            vx = cx + row["key_cells"] * cell_in
            for i, line in enumerate(row["value_lines"]):
                _put_text(ax, renderer, dpi, vx, cy + i * lh + lh * 0.80, line,
                          size=size, color=pal["ink"])
            cy += len(row["value_lines"]) * lh
            continue
        ix = cx + row.get("indent", 0) * cell_in
        color = pal.get(row.get("color") or "ink", pal["ink"])
        for i, line in enumerate(row["lines"]):
            _put_text(ax, renderer, dpi, ix, cy + i * lh + lh * 0.80, line,
                      size=size, color=color)
        cy += len(row["lines"]) * lh

    # 双栏分隔竖线（跟手绘抖动一致的细线）
    if div_x and div_top is not None:
        ax.plot([div_x, div_x], [div_top - 0.04, (div_bot or div_top) + 0.04],
                color=pal["line"], linewidth=1.0, zorder=3)


def _draw_footer(ax, pal, fig_w, fig_h, export):
    y = fig_h - _MARGIN - 0.06
    ax.plot([_MARGIN, fig_w - _MARGIN], [y - 0.06, y - 0.06],
            color=pal["line"], linewidth=1.0)
    notes = (export.get("notes") or {}).get("stats") or {}
    refs = ((export.get("sections") or {}).get("references") or {})
    left = (f"笔记 {notes.get('notes_total', 0)} 条 · "
            f"参考文献 {refs.get('count') or 0} 条 · "
            "Matplotlib · XKCD 风格")
    ax.text(_MARGIN, y, left, fontsize=_F_CRUMB, color=pal["muted"],
            va="baseline")
    ax.text(fig_w - _MARGIN, y, "paper-trans-to-html",
            fontsize=_F_CRUMB, color=pal["muted"], va="baseline", ha="right")
