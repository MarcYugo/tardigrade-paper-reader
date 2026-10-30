"""文字层：字体族判定 + span→run 抽取 + 补间距空格 + 行级合并。

见包文档 `backend/pdf_parser/__init__.py`。

本模块只处理**正文字符**，不关心公式内容（公式框由 `local_parse_pipeline` 给出，
`backend_pymupdf` 把它们替换成占位 run）。字符级的四件事：

  * `_is_super` —— 校验 PyMuPDF 报的「上标」：TeX 把公式的上下标排回正文基线时
    bit0(上标) 会误判一大片正文，前端见到 up 就 `font-size × 0.7`，于是
    「公式后面的正文变小」。用「本行正文基线 + 抬升量」复核。
  * `_row_top` —— 行的 `y` 取**正文 span 自己**的顶：行内公式(分式/求和/积分)会把
    整行并集框的顶撑高，前端按 `y` 当文字顶定位就会叠字。
  * `_insert_gap_spaces` —— PyMuPDF 按 span 切字，同一视觉行里两截文字只要字体/字号
    不同就是两个 span，**中间的空隙不体现在任何字符上**（表格把 `Method`/`Accuracy`
    并进同一行最常见）。这里按 bbox 空隙补一个**空格字符**，否则合并后会粘成
    `MethodAccuracy`。公式字形两侧**一律不补**：补进去会把一处公式切成两段，
    行内公式的占位与前后文就对不上了（见 `_needs_gap_space`）。
  * `_merge_line_runs` —— 每行的多个 run 合并成「一段一个」：**颜色边界保留**
    （引用/交叉引用常被 hyperref 染成链接蓝，抹掉边界会把整行正文染蓝），
    其余样式（字号/字体/粗斜体）取本行字符数最多的那个 run，与前端 `bodyRunOf`
    同口径 —— 行内上标/小字号碎片不会再让整行字号忽大忽小。本地解析方案的公式
    占位 run（`run.gap`）必须单独保留，所以用 `keep_gaps=True` —— 一行仍然是
    「一段正文 + 若干公式占位」。

补间距必须在公式占位**之前**想清楚字符数：占位是**一个**空格字符（宽度由 `run.gw`
下发给前端撑开），块 canonical / `copy` 区间 / 译文缓存键全都按字符数算。
"""

from __future__ import annotations

import re
from typing import Optional


# 相邻 span 的水平空隙超过 `0.25 × 字号` 就认为「这里本该有一个空格」(字距/连字
# 造成的空隙远小于它：实测同一词内的 span 间隙 ≤ 0.1×字号)。
_GAP_SPACE_MIN = 0.25
# 基线差超过 `0.5 × 字号` 就不是「同一行的连续文字」(上下标被拆成同 y 的两个 span)
_GAP_BASE_TOL = 0.5
# run 上只在本层内部使用的临时几何键：补间距(`_insert_gap_spaces`)要靠它；
# 出页面前由 `_drop_gap_geom`(及兜底的 `backend_pymupdf._strip_font_keys`)摘掉。
_GAP_KEYS = ("x0", "x1", "oy")
# run 上的逐字符左边界(`_cx`)与末字符右边界(`_cx_end`)：从 `rawdict` 的
# `chars[*].bbox` 来，按句拆行时拆点要精确落在字符边界上。同样是临时键。
_CX_KEYS = ("_cx", "_cx_end")
# **行**上的临时几何键：逐字符的左边界 x（长度 = 行字符数 + 1，最后一个是最右边界）。
# 只有按句切块(`sentences.cut_blocks_by_sentence`)要，出页面前由 `_merge_line_runs`
# / `_drop_gap_geom` 摘掉 —— 不进 doc.json。
_CHAR_X = "_cx"


_SERIF = ("times", "georgia", "garamond", "palatino", "book", "caslon",
          "minion", "charter", "utopia", "song", "serif", "latinmodern", "notoserif")
_MONO = ("courier", "consolas", "menlo", "mono", "sfmono", "dejavusansmono")
_SANS = ("helvetica", "arial", "verdana", "tahoma", "trebuchet", "gill",
         "futura", "univers", "myriad", "calibri", "roboto", "inter", "notosans", "sans")


def _family(font: str) -> str:
    """字体名 → 粗分类（serif / sans / mono）。"""
    name = re.sub(r"[^a-z0-9 ]", "", (font or "").split("+")[-1].lower())
    if any(k in name for k in _MONO):
        return "mono"
    if "symbol" in name or "song" in name:
        return "serif"
    if any(k in name for k in _SERIF):
        return "serif"
    if any(k in name for k in _SANS):
        return "sans"
    return "serif"


def _join_lines(lines: list) -> str:
    """把块内多行拼成整段纯文本(处理断行连字符/换行空格)，用于翻译/zh_all。"""
    out = ""
    for ln in lines:
        t = "".join(r["t"] for r in ln["runs"]).strip()
        if not t:
            continue
        if out.endswith("-"):
            out = out[:-1] + t
        elif out:
            out += " " + t
        else:
            out = t
    return out


# =====================================================================
#  相邻 run 补间距 + 每行合并成单个 run
# =====================================================================
def _is_math_run(run: dict, cm_on: bool, weak_on: bool) -> bool:
    """这个 run 是不是公式字形（或公式占位）—— 补间距/探针文本都要跳过它。

    * 本地解析方案的公式占位 run 带 `gap`(公式框 id)：它就是「一块公式」，
      两侧不能再补空格（否则一处空位被切成两段）；
    * 其余按字体名判（见 `math_font._blank_math_level`）。
    """
    if run.get("gap") is not None:
        return True
    from .math_font import _blank_math_level
    return _blank_math_level(run.get("f") or "", cm_on, weak_on) > 0


def _span_text(sp: dict) -> str:
    """span 的文本：`dict` 抽法给 `text`，`rawdict` 抽法只给逐字符的 `chars`。

    两种抽法逐 span 逐字一致，所以用哪一种都不影响下游；`rawdict` 多给的 `chars`
    里带**逐字符 bbox**，那是拆行时唯一的精确几何来源(见 `_CHAR_X`)。
    """
    t = sp.get("text")
    if t is not None:
        return t
    return "".join((c.get("c") or "") for c in sp.get("chars") or [])


def _span_char_x(sp: dict, text: str) -> Optional[tuple[list[float], float]]:
    """一个 span 的**逐字符左边界** + 末字符右边界；取不到就返回 None。

    优先用 `rawdict` 的 `chars[*].bbox`（精确）；字符数对不上/没有 chars（`dict`
    抽法）时退回按 span 框的**线性插值** —— 误差上限就是几个字符宽，而且只有
    「句子端点正好落在这个 span 里」时才会看出来。
    """
    n = len(text)
    if not n:
        return None
    chars = sp.get("chars") or []
    if len(chars) == n:
        lefts = [float((c.get("bbox") or (0.0,))[0]) for c in chars]
        box = chars[-1].get("bbox") or (0.0, 0.0, 0.0, 0.0)
        return lefts, float(box[2])
    bx = sp.get("bbox")
    if not bx:
        return None
    x0, x1 = float(bx[0]), float(bx[2])
    return [x0 + (x1 - x0) * i / n for i in range(n)], x1


def _space_run(src: dict) -> dict:
    """照 `src` 的样式造一个空格 run(补间距用)。`f` 留空 → 不会被当公式字形。"""
    return {"t": " ", "s": src.get("s", 10.0), "f": "",
            "fam": src.get("fam", "serif"), "b": False, "i": False,
            "up": False, "c": src.get("c", "#000000")}


def _needs_gap_space(prev: dict, nxt: dict, cm_on: bool, weak_on: bool) -> bool:
    """`prev` 与 `nxt` 之间要不要补一个空格(口径见 `_insert_gap_spaces`)。"""
    if any(k not in prev for k in _GAP_KEYS) or any(k not in nxt for k in _GAP_KEYS):
        return False                     # 补进去的空格 run 自己没有几何 → 不再补
    if (prev.get("t") or "")[-1:].isspace() or (nxt.get("t") or "")[:1].isspace():
        return False                     # 原文已经有空白，别补成两个
    if _is_math_run(prev, cm_on, weak_on) or _is_math_run(nxt, cm_on, weak_on):
        return False                     # 公式内部不补：会把一处空位切成两段
    size = max(prev.get("s") or 10.0, nxt.get("s") or 10.0)
    if abs((prev.get("oy") or 0.0) - (nxt.get("oy") or 0.0)) > _GAP_BASE_TOL * size:
        return False                     # 基线不同 → 上下标/另一行，不是连续文字
    return (nxt["x0"] - prev["x1"]) > _GAP_SPACE_MIN * size


def _gap_space_x(cx: list[float], pos: int, prev: dict) -> float:
    """补出来的那个空格该落在哪（逐字符几何，见 `_CHAR_X`）。

    它就是那段空隙的左端 —— `prev` 的右边界(`x1`)，夹在前后两个字符的左边界之间。
    """
    left = cx[pos - 1] if 0 < pos <= len(cx) - 1 else None
    right = cx[pos] if 0 <= pos < len(cx) else None
    x = prev.get("x1")
    x = float(x) if x is not None else (
        (left + right) / 2.0 if left is not None and right is not None else
        left if left is not None else right)
    if x is None:
        return 0.0
    if left is not None:
        x = max(x, left)
    if right is not None:
        x = min(x, right)
    return x


def _insert_gap_spaces(blocks: list[dict], cm_on: bool, weak_on: bool) -> int:
    """相邻 span 之间「有空隙、没空格」的位置补一个空格；返回补了几处。

    为什么需要：PyMuPDF 是按 **span** 切字的，同一视觉行里的两截文字只要字体/字号/颜色
    不同就是两个 span，**中间的空隙不体现在任何字符上**。表格最常见：`Method` /
    `Accuracy` 并进同一行，两个 span 之间隔了 20pt，字符流里却是 `MethodAccuracy`。

    ⚠️ 改变字符数 ⇒ 必须在按句切块**之前**调用：块的 canonical、公式 `copy` 区间、
    译文缓存键全都按字符数算，两处口径必须同源。

    补出来的空格没有几何，调用方随后用 `_fill_gap_geom` 按前后 run 的边界把它补上。
    """
    added = 0
    for blk in blocks or []:
        for ln in blk.get("lines") or []:
            runs = ln.get("runs") or []
            out: list[dict] = []
            ins: list[tuple[int, float]] = []      # (字符下标, 那个空格的左边界)
            pos = 0
            for r in runs:
                if out and _needs_gap_space(out[-1], r, cm_on, weak_on):
                    cx = ln.get(_CHAR_X)
                    if cx:
                        ins.append((pos, _gap_space_x(cx, pos, out[-1])))
                    out.append(_space_run(out[-1]))
                    added += 1
                    pos += 1
                out.append(r)
                pos += len(r.get("t") or "")
            ln["runs"] = out
            cx = ln.get(_CHAR_X)
            if ins and cx is not None:
                for p, x in reversed(ins):              # 从后往前插，前面的下标不挪
                    cx.insert(p, x)
    return added


def _fill_gap_geom(blocks: list[dict]) -> int:
    """给「补出来的空格 run」（没有 `x0`/`x1`）补上几何，返回补了几个。

    左边界取前一个 run 的右边界（`x1` / `_cx_end`），右边界取后一个 run 的左边界
    （`x0`）；两侧都没有时按 `0.25em` 估。逐字符几何（`_cx`）也一起给出来，
    按句拆行时才能把拆点落到字符边界上。
    """
    filled = 0
    for blk in blocks or []:
        for ln in blk.get("lines") or []:
            runs = ln.get("runs") or []
            for i, r in enumerate(runs):
                if "x0" in r and "x1" in r:
                    continue
                t = r.get("t") or ""
                if not t:
                    continue
                size = float(r.get("s") or 10.0)
                prev_end = None
                if i > 0:
                    p = runs[i - 1]
                    prev_end = p.get("x1", p.get("_cx_end"))
                next_x0 = None
                for q in runs[i + 1:]:
                    if "x0" in q:
                        next_x0 = float(q["x0"])
                        break
                left = float(prev_end) if prev_end is not None else None
                if left is None:
                    left = float(next_x0) - 0.25 * size * len(t) \
                        if next_x0 is not None else float(ln.get("x") or 0.0)
                right = float(next_x0) if next_x0 is not None else left + 0.25 * size * len(t)
                if right < left:
                    right = left + 0.25 * size * len(t)
                r["x0"], r["x1"] = left, right
                n = len(t)
                r["_cx"] = [left + (right - left) * k / n for k in range(n)]
                r["_cx_end"] = right
                filled += 1
    return filled


def _line_char_x(ln: dict) -> list[float]:
    """按 run 的几何重建一行的逐字符左边界（长度 = 行字符数 + 1）。

    本地解析方案的行不是从 `rawdict` 一次抽出来的：中间会插「补间距空格」与公式占位
    run（都没有 `chars`），所以 `_CHAR_X` 只能在**所有 run 定型之后**统一重建：
    有 `_cx`（逐字符 bbox）的用它，只有 `x0`/`x1` 的按区间线性铺开。
    """
    cx: list[float] = []
    end: Optional[float] = None
    for r in ln.get("runs") or []:
        t = r.get("t") or ""
        n = len(t)
        if not n:
            continue
        own = r.get("_cx")
        if own and len(own) == n:
            lefts = [float(v) for v in own]
            right = float(r.get("_cx_end", lefts[-1]))
        elif "x0" in r and "x1" in r:
            x0, x1 = float(r["x0"]), float(r["x1"])
            lefts = [x0 + (x1 - x0) * k / n for k in range(n)]
            right = x1
        else:
            step = 0.25 * float(r.get("s") or 10.0)
            base = cx[-1] if cx else float(ln.get("x") or 0.0)
            lefts = [base + step * k for k in range(n)]
            right = base + step * n
        if cx and lefts[0] < cx[-1]:
            lefts[0] = cx[-1]                     # 单调化：边界不能往回走
        cx.extend(lefts)
        end = right
    if not cx:
        return []
    cx.append(float(end if end is not None else cx[-1]))
    return cx


def _drop_gap_geom(blocks: list[dict]) -> None:
    """摘掉临时几何键：run 上的 `_GAP_KEYS`/`_CX_KEYS` 与行上的逐字符几何(`_CHAR_X`)。

    都是内部辅助，不进 doc.json（`_merge_line_runs` 会顺带调一次）。
    """
    for blk in blocks or []:
        for ln in blk.get("lines") or []:
            ln.pop(_CHAR_X, None)
            for r in ln.get("runs") or []:
                for k in _GAP_KEYS + _CX_KEYS:
                    r.pop(k, None)


def _merged_run(runs: list[dict], style: Optional[dict] = None) -> dict:
    """把若干相邻的**同色**正文 run 合成一个。

    * 字号/字体/粗斜体取 `style`（默认本段字符数最多的那个 run）：公式里的上标/
      下标是独立的小字号 span，取「第一个 run」会让整行字号随排版顺序乱跳。
    * **颜色只认本段**（调用方 `_color_groups` 已按色切好段）。MuPDF 是按
      「字体/字号/颜色」切 span 的，行内换色是有意义的：`\\cite` / `\\ref` 被
      hyperref 染成链接蓝（实测 `#0064e0`）。若整行只留一个 run 并按字数挑样式，
      取到链接色就会把**整行正文**染蓝（实测某篇 ACL 论文 38 行整行变蓝），
      取到正文色又让引用丢掉蓝色 —— 两种都不忠实。
    * `up` 一律置 false —— 整行不该被当成上标(前端会 `font-size × 0.7` +
      `vertical-align:super`)。
    """
    longest = max(runs, key=lambda r: len(r.get("t") or ""))
    main = style or longest
    return {
        "t": "".join(r.get("t") or "" for r in runs),
        "s": main.get("s", 10.0),
        "fam": main.get("fam", "serif"),
        "b": bool(main.get("b")),
        "i": bool(main.get("i")),
        "up": False,
        "c": longest.get("c") or "#000000",
    }

def _color_groups(runs: list[dict]) -> list[list[dict]]:
    """把一段相邻 run 按**颜色**切段（相邻同色的连着，颜色一变就断开）。"""
    groups: list[list[dict]] = []
    key = None
    for r in runs:
        own = str(r.get("c") or "#000000").lower()
        if not groups or own != key:
            groups.append([])
            key = own
        groups[-1].append(r)
    return groups


def _merge_seg(seg: list[dict], style: dict) -> list[dict]:
    """一段正文 run → 按颜色分组合并（颜色边界保留，字号/字体按 `style` 统一）。"""
    return [_merged_run(g, style) for g in _color_groups(seg)]


def _merge_line_runs(blocks: list[dict], keep_gaps: bool = False) -> list[dict]:
    """把每行的多个 run 合并成**若干**个（行几何一个不动）；原地改并返回 `blocks`。

    口径有四条硬约束：

      * **字符数守恒** —— 只是 `"".join(...)`，不 strip、不补空格：下游(块 canonical、
        行内公式的 `copy` 区间)全都按字符数寻址。
      * **颜色边界保留** —— 行内按颜色切段，相邻同色的才合并。`\\cite` / `\\ref` 被
        hyperref 染成链接蓝时，只有引用那截是蓝的；整行并成一段会让「链接色」
        顺着整行铺开（正文变蓝）。
      * **字号/字体/粗斜体取本行字符数最多的那个 run**(与前端 `bodyRunOf` 同口径)：
        公式里的上标/下标是独立的小字号 span，取「第一个 run」会让整行字号随排版
        顺序乱跳。
      * `up` 一律置 false —— 整行不该被当成上标。

    `keep_gaps=True`（本地解析方案）：公式占位 run(`run.gap`) 必须**单独保留**
    —— 前端 `.rngap[data-g]` 是公式覆盖层的定位锚点；只在它们之间做正文合并，
    于是一行 = 「一段正文(+公式占位)」的交替序列，通常是 `[正文]` 或
    `[正文, 占位, 正文, …]`。

    行上的逐字符几何(`_CHAR_X`)在这里摘掉 —— 它只服务于「按句切块」(`sentences`)。
    """
    for blk in blocks or []:
        for ln in blk.get("lines") or []:
            ln.pop(_CHAR_X, None)
            runs = ln.get("runs") or []
            if not runs:
                continue
            # 整行的「正文样式」模板：字号/字体/粗斜体按字数最多的正文 run 统一
            # （公式占位 run 不算，它们的 `t` 只是一个空格）
            body = [r for r in runs if r.get("gap") is None] or runs
            style = max(body, key=lambda r: len(r.get("t") or ""))
            has_gap = len(body) != len(runs)
            if keep_gaps and has_gap:
                out: list[dict] = []
                seg: list[dict] = []
                for r in runs:
                    if r.get("gap") is not None:
                        if seg:
                            out.extend(_merge_seg(seg, style))
                            seg = []
                        out.append(r)
                    else:
                        seg.append(r)
                if seg:
                    out.extend(_merge_seg(seg, style))
                ln["runs"] = out
            elif len(runs) > 1:
                ln["runs"] = _merge_seg(runs, style)
    _drop_gap_geom(blocks)
    return blocks


def _line_baseline(spans: list) -> Optional[float]:
    """本行「正文基线」：按**字符数加权**取 span 基线(origin.y)的众数。

    一行里正文的字数一定多于上下标，所以众数就是正文基线；并列时取字号大的
    那个（上标必然比它所属的正文小）。用于校验 PyMuPDF 报的「上标」是不是真抬升了，
    见 `_is_super`；本地解析方案还把它写进行的 `base`（前端按基线定位）。
    """
    w: dict[float, tuple[int, float]] = {}
    for sp in spans:
        org = sp.get("origin")
        if not org:
            continue
        y = round(org[1], 1)
        n, sz = w.get(y, (0, 0.0))
        w[y] = (n + max(1, len(_span_text(sp).strip())),
                max(sz, sp.get("size", 0)))
    if not w:
        return None
    return max(w.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]


def _is_super(sp, fl: int, base: Optional[float]) -> bool:
    """这个 span 是不是**真的**上标。

    ⚠️ PyMuPDF 的 bit0(上标) 不能单独用：它是「基线比前一个 span 抬高」的启发式，
    TeX 把公式的上下标排回正文基线时会**误判**。实测本样本真论文 7 页：bit0 标了
    173 个字符，其中 **157 个(91%) 是「正文字号 + 正好压在本行基线上」的正文**。
    前端 `styleRun` 见到 up 就 `vertical-align:super; font-size × 0.7`，于是
    「公式后面的正文」被缩掉 30% —— 这就是「正文公式后字号变小」的根因。
    真上标实测抬升 3.6~5.2pt，阈值 0.2×字号(≈1.4pt) 安全边际很大；假上标抬升 0.00。
    """
    if not (fl & 1):
        return False
    org = sp.get("origin")
    if not org or base is None:
        return True                     # 拿不到几何信息时，只能信 bit0
    size = sp.get("size", 10) or 10
    return (base - org[1]) > 0.2 * size


# 行的文字方向判据：`line["dir"]` 是**文字前进方向**的单位向量（页面坐标，y 向下）。
# 只有明显竖直/反向的才算旋转行；斜排（很少见）仍按横排处理，免得动到常规版面。
_DIR_SKEW_TOL = 0.45


def _line_rot(ln: dict, rect: Optional[tuple] = None) -> Optional[dict]:
    """行的**旋转信息**（横排返回 None）——竖排/倒排文字必须靠它才能摆对。

    背景（实测 arXiv 侧边章）：PDF 里旋转 90° 的文字，PyMuPDF 给的是
      * `line["dir"] = (0.0, -1.0)`（自下而上；wmode 仍是 0）；
      * `line/span["bbox"]` = 旋转后的**轴对齐外框** —— 一条很窄很高的竖条
        （实测宽 26.68pt = 字高、高 358.86pt = 文字长度）；
      * 每个 `char["bbox"]` 的 **x 完全相同**、只有 y 在变；`span["origin"]` 在竖条的
        **底部**（文字起点）。
    所以竖排行不能当横排行用：行的 `w` 会变成字高（前端按 `ln.w` 设盒宽 ⇒ 文字横着
    溢出、压住正文），`base` 也不再是「水平基线」，`_CHAR_X` 退化成常数。

    返回 `{rot, ox, oy, rw, rh}`：
      * `rot`：**CSS 角**（`transform: rotate(rot deg)`，页面坐标系同向）——
        `-90` 自下而上、`90` 自上而下、`180` 倒排；
      * `(ox, oy)`：该行「**未旋转时**的左上角」落在页面的哪个点；
      * `rw/rh`：未旋转时的宽（文字长度）/高（字高）。
    前端 `left=ox, top=oy, width=rw, height=rh, transform-origin: 0 0` 摆好再旋转，
    就精确落回原来的竖条上；`x/y/w/h` 仍然是**旋转后**的轴对齐外框（版面几何用它）。
    """
    d = ln.get("dir")
    if not d:
        return None
    try:
        dx, dy = float(d[0]), float(d[1])
    except (TypeError, ValueError, IndexError):
        return None
    if abs(dx) >= abs(dy):
        if dx > 0:
            return None                       # (1, 0)：正常横排
        if abs(dy) > _DIR_SKEW_TOL:
            return None                       # 斜排（左下）：不认
        rot = 180
    else:
        if abs(dx) > _DIR_SKEW_TOL:
            return None                       # 斜排（斜下）：不认
        rot = -90 if dy < 0 else 90
    bx = rect or ln.get("bbox")
    if not bx:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bx[:4])
    except (TypeError, ValueError):
        return None
    w, h = max(0.0, x1 - x0), max(0.0, y1 - y0)
    if rot == 180:
        # 文字从右往左：局部 x 轴指向 -x、局部 y 轴（字的下行方向）指向 -y
        ox, oy, rw, rh = x1, y1, w, h
    elif rot == -90:
        # 文字自下而上：局部 x 轴指向 -y、局部 y 轴指向 +x
        ox, oy, rw, rh = x0, y1, h, w
    else:
        # 文字自上而下：局部 x 轴指向 +y、局部 y 轴指向 -x
        ox, oy, rw, rh = x1, y0, h, w
    return {"rot": rot,
            "ox": round(ox, 3), "oy": round(oy, 3),
            "rw": round(max(0.5, rw), 3), "rh": round(max(0.5, rh), 3)}


def _row_top(spans: list, fallback: float) -> float:
    """本行「文字顶」：取**可见字符最多的那个 span 自己** bbox 的顶。

    ⚠️ PyMuPDF 给的行 bbox 是**该行所有 span 的并集**。行内公式(分式/求和/积分)
    的字形上下都伸出正文，于是含公式的行顶比正文文字顶高出约 7pt —— 实测本样本：

      「conjugates.」            并集顶 484.91  基线 492.94  并集 ascent  8.03
      「For sequences, the … ∑」 并集顶 490.61  基线 506.05  并集 ascent 15.44
                                 （该行正文 span 顶 = 498.02）

    前端是按 `y` 当**文字顶**绝对定位的，所以这里必须给「正文顶」。
    """
    best = None
    best_n = 0
    for sp in spans:
        n = len(_span_text(sp).strip())
        if n == 0 or n <= best_n:
            continue
        best, best_n = sp, n
    if best is None or "bbox" not in best:
        return fallback
    return best["bbox"][1]


def _parse_text_blocks(page, align_row_top: bool = False,
                       gap_spaces: bool = False,
                       char_x: bool = False) -> list[dict]:
    """抽一页文字块（纯 PyMuPDF，不认识公式框；保留给工具/对照使用）。

    `align_row_top` 时行的 `y` 由「整行并集 bbox 的顶」改成「行内正文 span 的顶」
    (见 `_row_top`)；`gap_spaces` 时补相邻 span 之间的空格(见 `_insert_gap_spaces`)；
    `char_x` 时用 `page.get_text("rawdict")` 抽并给每行挂 `_CHAR_X`。

    ⚠️ 本地解析方案不走这个函数：它要同时拿到 `local_parse_pipeline` 给的
    `formula_box_id`（见 `backend_pymupdf._local_text_blocks`），但字符级的四步
    （`_is_super` / `_row_top` / `_insert_gap_spaces` / `_merge_line_runs`）完全一致。
    """
    blocks: list[dict] = []
    raw = page.get_text("rawdict" if char_x else "dict")
    for blk in raw.get("blocks", []):
        if blk.get("type") != 0:
            continue  # 图片块在这里忽略，交给专门的图片管线
        lines = []
        for ln in blk.get("lines", []):
            sps = ln.get("spans", [])
            base = _line_baseline(sps)
            runs = []
            cx: list[float] = []                   # 逐字符左边界(见 `_CHAR_X`)
            cx_end = None                          # 末字符右边界
            cx_ok = bool(char_x)
            for sp in sps:
                t = _span_text(sp)
                if not t:
                    continue
                fl = sp.get("flags", 0)
                run = {
                    "t": t,
                    "s": round(sp.get("size", 10), 1),
                    # 原始字体名：只服务于「公式候选区」判定；出页面前由
                    # `_strip_font_keys` 剥掉 —— 前端不认识它。
                    "f": sp.get("font", ""),
                    "fam": _family(sp.get("font", "")),
                    "b": bool(fl & 16),   # 粗体
                    "i": bool(fl & 2),    # 斜体
                    "up": _is_super(sp, fl, base),   # 上标（已校验基线）
                    "c": "#%06x" % (sp.get("color", 0) & 0xFFFFFF),
                }
                if gap_spaces:
                    bx = sp.get("bbox") or (0.0, 0.0, 0.0, 0.0)
                    org = sp.get("origin")
                    run.update({"x0": float(bx[0]), "x1": float(bx[2]),
                                "oy": float(org[1]) if org else float(bx[1])})
                if cx_ok:
                    got = _span_char_x(sp, t)
                    if got is None:
                        cx_ok = False              # 有一截拿不到几何 → 整行不给(会错位)
                    else:
                        cx.extend(got[0])
                        cx_end = got[1]
                runs.append(run)
            if not runs:
                continue
            lx0, ly0, lx1, ly1 = ln["bbox"]
            rot = _line_rot(ln, (lx0, ly0, lx1, ly1))
            if align_row_top and not rot:
                ly0 = _row_top(sps, ly0)
            line = {"x": lx0, "y": ly0, "w": lx1 - lx0, "h": ly1 - ly0, "runs": runs}
            if rot:
                # 竖排/倒排：`y` 必须留 bbox 顶（`_row_top` 在这里是文字的**末端**）
                line.update(rot)
            if base is not None:
                line["base"] = float(base)
            if cx_ok and cx_end is not None and len(cx) == sum(len(r["t"]) for r in runs):
                line[_CHAR_X] = cx + [float(cx_end)]
            lines.append(line)
        if not lines:
            continue
        x0, y0, x1, y1 = blk["bbox"]
        text = _join_lines(lines)
        if not text:
            continue
        blocks.append({
            "id": f"p{page.number}b{len(blocks)}",
            "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
            "text": text, "lines": lines,
        })
    if gap_spaces and blocks:
        from .math_font import _math_font_flags
        # 页级字体判据要先看到整页 span 才算得准 → 收完再回头补间距
        cm_on, weak_on = _math_font_flags(blocks)
        if _insert_gap_spaces(blocks, cm_on, weak_on):
            _fill_gap_geom(blocks)
            for blk in blocks:
                blk["text"] = _join_lines(blk["lines"])   # 字符数变了，块文本要重算
    return blocks


def _text_line_rects(texts: list[dict]) -> list:
    """所有行的矩形（`[x0, y0, x1, y1]`，不依赖 PyMuPDF 的 `Rect` 类型）。"""
    rects = []
    for blk in texts:
        for ln in blk.get("lines", []):
            x, y = float(ln.get("x") or 0.0), float(ln.get("y") or 0.0)
            rects.append((x, y, x + float(ln.get("w") or 0.0),
                          y + float(ln.get("h") or 0.0)))
    return rects


def _union_line_rect(lines: list[dict]) -> tuple[float, float, float, float]:
    x0 = min(l["x"] for l in lines)
    y0 = min(l["y"] for l in lines)
    x1 = max(l["x"] + l["w"] for l in lines)
    y1 = max(l["y"] + l["h"] for l in lines)
    return x0, y0, x1, y1
