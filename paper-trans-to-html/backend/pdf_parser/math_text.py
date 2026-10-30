"""公式文本工具（已停用）：原 LaTeX 定界符解析 / 锚点规整化实现已整体移除。

本模块只保留函数名 / 签名与导出常量（`backend/pdf_parser/__init__.py` 仍会把它们
再导出一次），函数体全部为空壳，方便之后用新的实现替换。
"""

from __future__ import annotations

import re
from typing import Optional


_NORM_CHAR_RE = re.compile(r"[0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff\ufb00-\ufb06]")
# 「算字形」的字符类。
_GLYPH_CLS = (
    "0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff\ufb00-\ufb06"    # 拉丁 / 汉字 / 连字
    "\u0370-\u03ff\u1d00-\u1d7f\u1d400-\u1d7ff"            # 希腊字母 / 数学字母
    "\u2032-\u2037"                                        # ′ ″ ‴ ‵ ‶ ‷
    "\u2070-\u209f"                                        # 上标 / 下标
    "\u2100-\u214f"                                        # ℘ ℝ ℓ 等
    "\u2190-\u21ff\u2300-\u23ff"                           # 箭头 / ⌈ ⌉ ⟨ ⟩
    "\u2200-\u22ff\u27c0-\u27ef\u2980-\u2aff"              # 数学算子 / 数学括号
)
_GLYPH_RE = re.compile("[" + _GLYPH_CLS + "]")
_NON_GLYPH_PREFIX_RE = re.compile("[^" + _GLYPH_CLS + "]*")
# 连字(ligature)展开表。
_LIG_EXPAND = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
               "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
# 独立公式：\[…\] / $$…$$(可跨行)
_DISPLAY_MATH_RE = re.compile(r"\\\[(.+?)\\\]|\$\$(.+?)\$\$", re.S)
# 行内公式：独立公式占位符 / \(…\) / $…$
_LINE_MATH_TOKEN_RE = re.compile(r"\x00(\d+)\x00|\\\((.+?)\\\)|\$([^$\n]{1,200}?)\$")
_STYLE_KEYS = ("s", "fam", "b", "i", "up", "c")
# 行内公式锚点的候选长度(规整字符数)。
_ANCHOR_LENS = (48, 32, 20, 12, 8)
# 左右锚点加起来至少要这么多规整字符。
_ANCHOR_MIN_TOTAL = 8
# 两个锚点之间在整页规整文本里最多能隔多少字符。
_ANCHOR_SPAN_MAX = 200
# 多条锚点候选都说得通时的相似度阈值。
_ANCHOR_RANK_MIN = 0.35
_ANCHOR_RANK_GAP = 0.15
# 一条跨块公式最多允许吞掉几行本地文字行。
_SPAN_MAX_LINES = 12


def _norm_keep(s: str) -> str:
    """[已停用] 匹配用规整。"""
    return ""


def _norm_keep_idx(s: str) -> tuple[str, list[int]]:
    """[已停用] 同 `_norm_keep`，另外给出每个规整字符在原串里的下标。"""
    return "", []


def _looks_math_body(body: str) -> bool:
    """[已停用] 单 `$…$` 的保守判定。"""
    return False


def _same_style(a: dict, b: dict) -> bool:
    """[已停用] 两个 run 的样式是否一致。"""
    return False


def _body_run(runs: list[dict]) -> dict:
    """[已停用] 行内公式用哪个样式渲染。"""
    return {}


def _split_line_math(line: str, keep: list[str]) -> Optional[list]:
    """[已停用] 把一行切成 [文字, {latex}, 文字, …]。"""
    return None


def _ocr_math_lines(ocr_text: str) -> list[dict]:
    """[已停用] 把整页 OCR 文本切成「行」，并标出每行的公式。"""
    return []


def _gap_ok(gap: str, latex: str, plain: str = "") -> bool:
    """[已停用] 两段锚点之间那截是不是「这条公式的字形」。"""
    return False


def _page_index(local_texts: list[dict]) -> tuple[list[dict], str]:
    """[已停用] 把本地各行拼成整页规整文本。"""
    return [], ""


def _meta_at(metas: list[dict], pos: int) -> tuple[Optional[dict], int]:
    """[已停用] 规整下标 pos 落在哪一行。"""
    return None, -1


def _apply_replacements(runs: list[dict], raw: str, repls: list,
                        body: dict) -> Optional[list[dict]]:
    """[已停用] 按 [(a, b, latex), …] 重建 runs。"""
    return None


def _all_pos(hay: str, needle: str) -> list[int]:
    """[已停用] `needle` 在 `hay` 里的所有出现下标。"""
    return []


def _plain_math(latex: str) -> str:
    """[已停用] LaTeX 的「纯文本形态」。"""
    return ""


def _bag_ratio(latex: str, glyphs: str) -> float:
    """[已停用] 乱序字符多重集相似度(0~1)。"""
    return 0.0


def _count_chars(s: str) -> dict:
    """[已停用] 字符计数。"""
    return {}


def _lines_text(lines: list[dict]) -> str:
    """[已停用] 几行原字形拼成纯文本。"""
    return ""


def _has_glyphs(text: str) -> bool:
    """[已停用] 这段残留里有没有真的「字形」。"""
    return False


def _math_glyphs(text: str, latex: str, plain: str) -> bool:
    """[已停用] 这段本地字形就是「这条公式的字形」。"""
    return False


def _tighten(raw: str, a: int, b: int) -> tuple[int, int]:
    """[已停用] 把 `[a, b)` 收成真正的公式字形区间。"""
    return a, b


def _math_ish(text: str) -> bool:
    """[已停用] 像公式而不是像一句正文。"""
    return False


def _anchor_of(ocr_lines: list[dict], idx: int, step: int) -> str:
    """[已停用] 取第 idx 行前后最近的一段连续普通文字当锚点。"""
    return ""
