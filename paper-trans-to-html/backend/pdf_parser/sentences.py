# -*- coding: utf-8 -*-
"""按句子重切文字块：让**每个文字块 = 恰好一句**（以 `.!?。！？` 收尾）。

为什么需要（2026-09）
--------------------
`text_layer._merge_line_runs` 已经让「一行只剩几种颜色的正文 run」，但**一行不是一句**；
更要紧的是**块**本身常被公式框/分栏切碎：同一段话横跨好几个块，而且两块的**行在
同一视觉行上左右相接**。实测样本第 2 页：

    p1b45  In mathematics, the quadratic equation ▊ can be solved using the formula ▊
    p1b47  , where the discriminant ▊ determines the nature of the roots. …
    p1b48…p1b51 / p1b52+p1b54 / p1b56  同一段被切成 3 组块

前端 `reader2.splitSentences()` 是**按块**切句的，所以点一下选中的「句子」就是这种半句
碎片。本模块把块按句子重切，让「块 = 句 = 可选中的最小单位」在**数据层**就成立。

做法（五步，全是几何 + 字面，不猜语义）
--------------------------------------
1. **探针文本**（`_probe`）：把数学字形的字符与**公式占位 run** 换成空格
   （占位本来就是「一个空格 + 原宽」，所以偏移与真实行文本一一对应；落到块里的
   还是真文本）。公式字形里也可能有 `.`/`!`/`?`，不换掉就会被当成句末。
2. **行链**（`_cluster_rows` + `_corridors` + `_mark_groups`）：同一视觉行上横向相接
   （≤ `_CHAIN_TOL`）的片段属于同一段 —— 这就是被公式切碎的同一行。两栏之间那条
   **走廊**（同一个 x 带在 ≥ `_CORRIDOR_MIN_ROWS` 行上都是空的）不算相接，免得把
   双栏页的两栏粘成一行；`(12)` 这类公式编号/页码也不并进来。
3. **续句**（`_flow_chains`）：上组**末行**与下组**首行** x 重叠、纵向间距 ≤ `_VGAP_MAX`，
   且「上组末尾不是 `.!?。！？:`」+「下组首字符是小写字母或 `,;)`」→ 两组属于同一条
   **流**（句子可以跨过去）。证据不足就断开 —— **宁缺勿错**：断开只是保持现状（一个
   碎片），接错却会把标题、页码、公式编号粘进句子里。
4. **切句**（`split_sentences`）：在流文本上找句末标点，口径与前端
   `reader2.splitSentences` 对齐（另加「小数点 `1.1` 不切」与中文 `。！？`）。
5. **落块**：每个句子一个块；端点落在行内部时按逐字符几何（`text_layer` 的 `_CHAR_X`）
   把行**拆成两段** —— 拆点精确落在字符边界上，run 的几何也跟着重算。

不变式（下游全都要靠）
--------------------
* **必须在公式占位/补间距定稿之后、`_strip_font_keys` 之前跑**（调用点见
  `backend_pymupdf._local_text_blocks`）：占位与补空格都会改变字符数，而重切只改
  「分组」不改「字符」 ⇒ 排在后面就不用做任何偏移重映射。
* **一个字都不增删**：只重新分组、拆行；块文本由 `_join_lines` 重算。
* **整行都是公式占位的行不参与切句**（`blank` 的片段）：
  * 它所在的块**还有正文** → 这些行作为**乘客**挂到邻近的句子单元上（几何不能丢：
    前端要靠 `.rngap` 定位公式覆盖层）；
  * 它所在的块**整块都是这种行** → **原样保留**，不重切（免得凭空造出一个空壳块）。
* **块 id**：没被拆、没被合的块**沿用原 id**（笔记/高亮按 `"<块 id>#<句号>"` 存），
  其余用 `p{页}s{序号}`。重切必然让「被切碎过的那几块」的笔记锚点失效（前端会显示
  「原文句已不存在」）—— 这是「块 = 句」的固有代价。
"""

from __future__ import annotations

import re
from typing import Optional

from .math_font import _math_font_flags
from .text_layer import _CHAR_X, _is_math_run, _join_lines, _union_line_rect


# 同一视觉行的两段算不算「同一行的左右两半」：横向接边容差(pt)。
# 实测同一行的两半只隔 1.2pt（公式占位之后）；两栏之间最小的栏间距 ≈ 9.9pt ——
# 靠 `_corridors` 把栏间空档排除掉之后，这里可以放宽到「一个长词」。
_CHAIN_TOL = 16.0
# 「中间只夹着公式字形」的两段（`In mathematics, … ▊ can be solved …`）：那段公式可以很宽
# （实测 44pt），只要中间没有**别的正文**、又不跨栏走廊，就还算同一行的两半。
_BLANK_GAP_TOL = 300.0
# 分栏走廊：同一个 x 带在这么多行上都是「左有内容 / 带宽空着 / 右有内容」→ 栏间空档
_CORRIDOR_MIN_ROWS = 3
_CORRIDOR_MIN_W = 5.0
_CORRIDOR_SNAP = 3.0
# 纵向续句：上组末行与下组首行之间最多隔这么远（中间夹一条整行公式时会更大）
_VGAP_MAX = 60.0
# 纵向续句：两组的**左边距**差多少以内算同一栏（实测同栏完全相等；栏间距 ≥ 52pt）
_XLEFT_TOL = 12.0
# 同一视觉行上两段之间横向差多少以上，补一个虚拟分隔（只进探针文本）
_FRAG_GAP = 4.0
# 同一视觉行：纵向交集 > 这个比例 × 较矮的那行
_ROW_OVERLAP = 0.5
# 「到此为止」的字符：句末标点之外，冒号也算（标签/标题多以此收尾）
_HARD_END = ".!?。！？:"
# 续句时，下一行/下一组的**首字符**允许是这些小写字母或符号（= 半句接着写）
_JOIN_START = re.compile(r"[a-z,;:)\]}，；）】》”’]")
# 公式编号 / 页码：`(12)` `[3]` `7` —— 绝不并进正文
_EQNUM = re.compile(r"^[\(\[]?\d{1,3}[\)\]]?$")


# =====================================================================
#  句子切分（口径与前端 `reader2.splitSentences` 对齐）
# =====================================================================
# 缩写/称谓：`. ` 后面还有内容时不当句末（与前端同一份表）
_ABBR = frozenset("fig figs eq eqs sec sect ref refs no nos tab tabs ch chaps app pp vol vols "
                  "approx et al dept univ".split())
_TITLE = frozenset("dr prof mr mrs ms st mt fr jr sr rev gen col lt capt sgt adm rep sen gov "
                   "corp inc ltd co".split())
_CLOSERS = ")]}\u201d\u2019\u00bb\"'"


def split_sentences(text: str) -> list[tuple[int, int]]:
    """句末区间 `[(起, 止), …]`：`止` 在句末标点（含右引号/右括号）**之后**。

    口径与前端 `reader2.splitSentences` 一致（那边是渲染/选中的真源），只多两条：

      * **小数点不切**：`1.1`、`3.14`（前端会把 `1.1 Single column` 切成两句）；
      * **中文句末**：`。！？`（含全角句点 `．`）一律断句。

    ⚠️ 区间必须**铺满整段**（尾部空白归前一句）：不然那条尾巴既不属于上一句也不属于
    下一句，拆出来的两段会各自少一截。
    """
    n, ends, i = len(text), [], 0
    while i < n:
        c = text[i]
        if c in ".!?":
            j = i + 1
            while j < n and text[j] in ".!?\u2026":
                j += 1
            while j < n and text[j] in _CLOSERS:
                j += 1
            ws = j
            while j < n and text[j].isspace():
                j += 1
            if j >= n:
                ends.append(ws)
                break
            nxt = text[j]
            boundary = False
            # 与前端同一套触发集：大写字母 / 数字 / `"'“’(`
            if nxt.isupper() or nxt.isdigit() or nxt in "\"'\u201c\u2019(":
                m = re.search(r"([A-Za-z]+)\s*$", text[:i])
                tok = m.group(1).lower() if m else ""
                numeral = nxt.isdigit() and tok in _ABBR
                namelike = tok in _TITLE
                etal = tok == "al" and bool(re.search(r"(^|\s)et\s*$", text[:ws], re.I))
                boundary = not (numeral or namelike or etal)
            if boundary and i > 0 and text[i - 1].isdigit() and nxt.isdigit():
                boundary = False                      # 1.1 / 3.14 不是句末
            if boundary:
                ends.append(ws)
            i = j
        elif c in "\u3002\uff01\uff1f\uff0e":          # 。！？．
            ends.append(i + 1)
            i += 1
        else:
            i += 1
    sents: list[tuple[int, int]] = []
    start = 0
    for e in ends:
        if e > start:
            sents.append((start, e))
            start = e
    if start < n:
        sents.append((start, n))
    return sents


# =====================================================================
#  片段 / 行 / 组 / 流
# =====================================================================
def _runs_text(ln: dict) -> str:
    return "".join(r.get("t") or "" for r in ln.get("runs") or [])


def _probe(ln: dict, cm_on: bool, weak_on: bool) -> str:
    """行文本的「句子判读版」：数学字形换成**等长空格**（长度不变 ⇒ 偏移一一对应）。

    公式占位 run(`run.gap`) 本身就是「一个空格 + 原宽」，正好落在这条口径里。
    """
    out = []
    for r in ln.get("runs") or []:
        t = r.get("t") or ""
        if not t:
            continue
        out.append(" " * len(t) if _is_math_run(r, cm_on, weak_on) else t)
    return "".join(out)


def _fragments(texts: list[dict], cm_on: bool, weak_on: bool) -> list[dict]:
    """一页的「行片段」：`{bi, li, ln, text, probe, x, y, w, h, blank, gid}`。"""
    frags: list[dict] = []
    for bi, blk in enumerate(texts or []):
        if blk.get("kind") == "formula" and blk.get("latex"):
            continue                       # 已带 LaTeX 的公式块：不重切（见模块文档）
        for li, ln in enumerate(blk.get("lines") or []):
            if ln.get("rot"):
                # 旋转（竖排/倒排）行：bbox 是「窄而高」的竖条，横向相接/分栏/纵向
                # 续句那些判据全不成立，进流只会被切碎（见 `cut_blocks_by_sentence`）。
                continue
            probe = _probe(ln, cm_on, weak_on)
            frags.append({
                "bi": bi, "li": li, "ln": ln, "text": _runs_text(ln), "probe": probe,
                "x": float(ln.get("x") or 0.0), "y": float(ln.get("y") or 0.0),
                "w": float(ln.get("w") or 0.0), "h": float(ln.get("h") or 0.0),
                "blank": not probe.strip(), "gid": -1,
            })
    return frags


def _same_row_band(a: dict, b: dict) -> bool:
    """两段是不是同一视觉行（只看纵向；横向由调用方判）。"""
    ov = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    return ov > _ROW_OVERLAP * min(a["h"], b["h"])


def _cluster_rows(frags: list[dict]) -> list[list[dict]]:
    """按纵向重叠把片段聚成「行」（不含横向判据）；行按 y 排序、行内按 x 排序。"""
    parent = list(range(len(frags)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(frags)):
        for j in range(i + 1, len(frags)):
            if _same_row_band(frags[i], frags[j]):
                ri, rj = find(i), find(j)
                parent[max(ri, rj)] = min(ri, rj)
    buckets: dict[int, list[dict]] = {}
    for i, f in enumerate(frags):
        buckets.setdefault(find(i), []).append(f)
    rows = [sorted(v, key=lambda f: f["x"]) for v in buckets.values()]
    rows.sort(key=lambda r: min(f["y"] for f in r))
    return rows


def _union_spans(row: list[dict]) -> list[tuple[float, float]]:
    """一行里所有片段（含公式占位行）x 区间的**并**（相接/重叠的合并掉）。"""
    spans: list[list[float]] = []
    for f in row:
        lo, hi = f["x"], f["x"] + f["w"]
        if spans and lo <= spans[-1][1] + 0.5:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    return [(a, b) for a, b in spans]


def _corridors(rows: list[list[dict]]) -> list[tuple[float, float]]:
    """分栏走廊：同一个 x 带在 ≥ `_CORRIDOR_MIN_ROWS` 行上都是空档 → 栏间空档。

    双栏页里两栏的行是**逐行对齐**的（左栏某行与右栏某行 y 相同），所以单看一行
    「左边有内容、右边也有内容、中间空 9.9pt」与「同一行被公式切开的左右两半」长得
    一模一样（实测后者只隔 1.2~8.6pt）。区别在：栏间空档**每行都落在同一个 x 上**，
    而被公式切开的空档每行都在不同位置 —— 数「同一 x 带上出现了几行」就能分开。

    ⚠️ 空档要按**所有**片段的并集算（公式占位行也算）：被公式切开的那一行，那段空档
    其实被公式占着，不该当成栏间距。
    """
    bands: list[tuple[float, float, int]] = []
    for ri, row in enumerate(rows):
        spans = _union_spans(row)
        for (a_lo, a_hi), (b_lo, b_hi) in zip(spans, spans[1:]):
            if b_lo - a_hi >= _CORRIDOR_MIN_W:
                bands.append((a_hi, b_lo, ri))
    groups: list[dict] = []
    for lo, hi, ri in sorted(bands):
        for g in groups:
            if abs(g["lo"] - lo) <= _CORRIDOR_SNAP and abs(g["hi"] - hi) <= _CORRIDOR_SNAP:
                g["rows"].add(ri)
                break
        else:
            groups.append({"lo": lo, "hi": hi, "rows": {ri}})
    return [(g["lo"], g["hi"]) for g in groups
            if len(g["rows"]) >= _CORRIDOR_MIN_ROWS]


def _vetoed(corridors: list[tuple[float, float]], lo: float, hi: float) -> bool:
    """这条「相接」是不是跨在一条分栏走廊上（跨栏 → 不是同一行的两半）。"""
    for c_lo, c_hi in corridors:
        if min(hi, c_hi) - max(lo, c_lo) >= 0.6 * (c_hi - c_lo):
            return True
    return False


def _same_column(f: dict, g: dict,
                 corridors: list[tuple[float, float]]) -> bool:
    """两段在不在**同一栏**：x 区间相交，或两者之间的横带没有跨过分栏走廊。

    「整行公式占位」常与所属正文**首行/末行**同栏却 x 不重叠（缩进、短行、公式在
    行尾），所以不能只要求 x 相交；但只要中间跨了 `_corridors()` 认出的栏间空档，
    就一定是另一栏 —— 这条挡住「右栏的公式行被挂到左栏句子」。
    """
    f1, g1 = f["x"] + f["w"], g["x"] + g["w"]
    if min(f1, g1) - max(f["x"], g["x"]) > 0:
        return True
    lo, hi = (f1, g["x"]) if f["x"] < g["x"] else (g1, f["x"])
    return not _vetoed(corridors, lo, hi)


def _mark_groups(texts: list[dict], rows: list[list[dict]],
                 corridors: list[tuple[float, float]]) -> dict[int, list[int]]:
    """行链：把块并成「组」，并写回每个片段的 `gid`（= 组的块下标，并查集根）。

    返回 `{组: [块下标, …]}`（只连「有正文」的片段，所以整块都是公式占位的块不会
    跟别的块并到一起）。
    """
    parent = list(range(len(texts or [])))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for row in rows:
        prev: Optional[dict] = None
        blanks_between = False
        for b in row:
            if b["blank"]:
                blanks_between = True          # 夹在中间的是公式占位（一段空格）
                continue
            if prev is None:
                prev = b
                continue
            lo, hi = prev["x"] + prev["w"], b["x"]
            gap = hi - lo
            # 两种「同一行的左右两半」：紧挨着(≤ `_CHAIN_TOL`)，或中间只夹着公式占位
            ok = blanks_between and gap <= _BLANK_GAP_TOL or gap <= _CHAIN_TOL
            if (ok and prev["bi"] != b["bi"] and not _EQNUM.match(b["text"].strip())
                    and not _vetoed(corridors, lo, hi)):
                ra, rb = find(prev["bi"]), find(b["bi"])
                parent[max(ra, rb)] = min(ra, rb)
            prev, blanks_between = b, False
    gids: dict[int, list[int]] = {}
    for f in (f for r in rows for f in r):
        f["gid"] = find(f["bi"])
        if not f["blank"]:
            gids.setdefault(f["gid"], []).append(f["bi"])
    return gids


def _row_rect(row: list[dict]) -> tuple[float, float, float, float]:
    x0 = min(f["x"] for f in row)
    x1 = max(f["x"] + f["w"] for f in row)
    y0 = min(f["y"] for f in row)
    y1 = max(f["y"] + f["h"] for f in row)
    return x0, y0, x1, y1


def _row_tail(row: list[dict]) -> Optional[str]:
    """一行里**最后一个非空片段**的探针文本（判「到此为止」用）。"""
    for f in reversed(row):
        if not f["blank"]:
            return f["probe"]
    return None


def _row_head(row: list[dict]) -> Optional[str]:
    """一行里**第一个非空片段**的探针文本。"""
    for f in row:
        if not f["blank"]:
            return f["probe"]
    return None


def _continues(up: list[dict], down: list[dict]) -> bool:
    """上组末行能不能接到下组首行（字面证据，见模块文档第 3 步）。"""
    tail, head = _row_tail(up), _row_head(down)
    if not tail or not head:
        return False
    tail = tail.rstrip()
    if not tail or tail[-1] in _HARD_END:
        return False
    head = head.lstrip()
    if not head or _EQNUM.match(head):
        return False
    return bool(_JOIN_START.match(head[0]))


def _flow_chains(groups: list[dict]) -> list[list[dict]]:
    """把组按纵向续句串成**流**（没续上的组各自成流）；返回流列表（自上而下）。

    每个组最多一条出边/入边（贪心，先接间距最近的）—— 一条流里的行序就是阅读顺序。
    判据（见 `_continues` 与模块文档第 3 步）：

      * 下组的**首行**在上组**末行**之下，且纵向间距 ≤ `_VGAP_MAX`；
      * 两组的**左边距**差 ≤ `_XLEFT_TOL` —— 同一栏的正文都从同一条左边距起排；
        双栏页里右栏的左边距差着 176pt（实测），这一条就把跨栏的路堵死了；
      * 两组的 x 区间有交集（粗判，两道保险都留着）；
      * 字面证据（`_continues`）：上面还没写完 + 下面接着写。
    """
    cand: list[tuple[tuple, int, int]] = []
    for i, u in enumerate(groups):
        ur = _row_rect(u["tail_row"])
        ux0 = min(f["x"] for f in u["frags"])
        ux1 = max(f["x"] + f["w"] for f in u["frags"])
        best = None
        for j, v in enumerate(groups):
            if i == j:
                continue
            vr = _row_rect(v["head_row"])
            if vr[1] <= ur[1] + 1.0:
                continue                       # 必须在下边
            gap = vr[1] - ur[3]
            if gap > _VGAP_MAX:
                continue
            vx0 = min(f["x"] for f in v["frags"])
            vx1 = max(f["x"] + f["w"] for f in v["frags"])
            if abs(vx0 - ux0) > _XLEFT_TOL:
                continue                       # 左边距不同 → 另一栏（或另一块版式）
            if min(ux1, vx1) - max(ux0, vx0) <= 0:
                continue                       # 横向没有交集 → 另一栏
            if not _continues(u["tail_row"], v["head_row"]):
                continue
            score = (gap, -(min(ux1, vx1) - max(ux0, vx0)))
            if best is None or score < best[0]:
                best = (score, j)
        if best is not None:
            cand.append((best[0], i, best[1]))
    nxt: dict[int, int] = {}
    taken: set[int] = set()
    for _score, i, j in sorted(cand, key=lambda c: (c[0], c[1], c[2])):
        if i in nxt or j in taken:
            continue                           # 一个组最多一条出边/入边
        nxt[i] = j
        taken.add(j)
    chains: list[list[dict]] = []
    for i in range(len(groups)):
        if i in taken:
            continue                           # 不是流的起点
        chain, k, seen = [], i, set()
        while k not in seen:
            seen.add(k)
            chain.append(groups[k])
            if k not in nxt:
                break
            k = nxt[k]
        chains.append(chain)
    chains.sort(key=lambda ch: min(f["y"] for g in ch for f in g["frags"]))
    return chains


def _flow_text(chain: list[dict]) -> tuple[str, list[dict]]:
    """把一条流拼成**探针文本**；返回 `(文本, [{fr, s, e}, …])`。

    * 行内相邻片段：横向**相接**（≤ `_FRAG_GAP`）就直接接上 —— 它们本来就是同一行的
      左右两半；隔得远则补一个虚拟分隔；
    * 换行：一律补一个虚拟分隔 —— 探针只需要「这里有个空白」这个事实。

    虚拟分隔只是探针里的一个空格，**不进任何行的文本**：下游按偏移取到的仍是真实文本。
    """
    text, spans = "", []
    for group in chain:
        for row in group["rows"]:
            row_prev = None
            for fr in row:
                if fr["blank"]:
                    continue
                if text and (row_prev is None
                             or fr["x"] > row_prev["x"] + row_prev["w"] + _FRAG_GAP):
                    text += " "
                start = len(text)
                text += fr["probe"]
                spans.append({"fr": fr, "s": start, "e": len(text)})
                row_prev = fr
    return text, spans


# =====================================================================
#  落块：按句子切、必要时拆行
# =====================================================================
def _slice_runs(runs: list[dict], a: int, b: int) -> list[dict]:
    """按字符区间 `[a, b)` 切 runs（样式与 `gap`/`gw` 占位属性跟着走）。"""
    out: list[dict] = []
    p = 0
    for r in runs:
        t = r.get("t") or ""
        s0, s1 = max(a, p), min(b, p + len(t))
        if s1 > s0:
            out.append({**r, "t": t[s0 - p:s1 - p]})
        p += len(t)
    return out


def _retag_run_geo(piece: dict) -> None:
    """拆行之后把每个 run 的几何(`x0`/`x1`)按**字符边界**重算。

    公式占位 run 的 `gw`（下发给前端的原宽）不在这里改：占位是一个字符，
    拆行只会把它整个分给某一句，宽度不变。
    """
    cx = piece.get(_CHAR_X)
    if not cx:
        return
    p = 0
    for r in piece.get("runs") or []:
        n = len(r.get("t") or "")
        if "x0" in r and 0 <= p < len(cx):
            r["x0"] = float(cx[p])
            r["x1"] = float(cx[min(p + n, len(cx) - 1)])
        p += n


def _slice_line(ln: dict, a: int, b: int) -> dict:
    """把一行按字符区间 `[a, b)` 拆出来（几何落到字符边界上）。"""
    n = sum(len(r.get("t") or "") for r in ln.get("runs") or [])
    piece = {"y": ln.get("y"), "h": ln.get("h"),
             "runs": _slice_runs(ln.get("runs") or [], a, b)}
    for key in ("s", "base"):                  # `base` = 前端按基线定位要用的
        if ln.get(key) is not None:
            piece[key] = ln[key]
    cx = ln.get(_CHAR_X)
    if cx and len(cx) == n + 1:
        left = float(ln.get("x") or 0.0) if a == 0 else float(cx[a])
        right = (float(ln.get("x") or 0.0) + float(ln.get("w") or 0.0)
                 if b == n else float(cx[b]))
        piece["x"] = left
        piece["w"] = max(1.0, right - left)
        piece[_CHAR_X] = [float(v) for v in cx[a:b + 1]]
    else:
        # 没有逐字符几何 → 按字符数比例估：只有「句子端点正好落在这一行内部」时
        # 才会差一点宽度。
        x0, w = float(ln.get("x") or 0.0), float(ln.get("w") or 0.0)
        piece["x"] = x0 + w * a / max(1, n)
        piece["w"] = max(1.0, w * (b - a) / max(1, n))
    _retag_run_geo(piece)
    return piece


def cut_blocks_by_sentence(texts: list[dict]) -> tuple[list[dict], dict]:
    """一页的文字块 → 按句子重切成「一句话一块」；返回 `(新块列表, 统计)`。

    调用点见 `backend_pymupdf._local_text_blocks`（**必须在公式占位/补间距定稿之后**）。
    不修改传入的块/行对象（只在「整块原样」时沿用原对象），返回新列表。
    """
    stats = {"sentence_units": 0, "sentence_merged": 0,
             "sentence_line_splits": 0, "sentence_tail_units": 0}
    # 旋转行（竖排页边水印等）：**整行一块原样保留**，不进句子的流。
    # 它们的 bbox 是「窄而高」的竖条（实测 arXiv 侧边章 26.68 × 358.86pt），横向
    # 相接/分栏/纵向续句那些判据全不成立 —— 实测会被句号切成两块、宽度还被
    # `_slice_line` 的 `max(1.0, …)` clamp 成 1pt。
    vert: dict[int, list[dict]] = {}
    for bi, blk in enumerate(texts or []):
        for ln in blk.get("lines") or []:
            if ln.get("rot"):
                vert.setdefault(bi, []).append(ln)
    frags = _fragments(texts, *_math_font_flags(texts)) if texts else []
    if not frags or not any(not f["blank"] for f in frags):
        return texts, stats                   # 没有文字 / 整页没有正文行：原样返回
    rows = _cluster_rows(frags)
    corridors = _corridors(rows)
    by_gid = _mark_groups(texts, rows, corridors)

    groups: list[dict] = []
    for gid in by_gid:
        gf = [f for f in frags if f["gid"] == gid]
        grows = _cluster_rows(gf)
        solid = [r for r in grows if _row_head(r)]
        if not solid:
            continue                          # 整组都是公式占位行 → 整块原样保留
        groups.append({"rows": grows, "frags": gf,
                       "head_row": solid[0], "tail_row": solid[-1]})
    groups.sort(key=lambda g: min(f["y"] for f in g["frags"]))

    blocks_used: set[int] = set()
    units: list[dict] = []
    owner: dict[int, int] = {}
    splits = 0
    for chain in _flow_chains(groups):
        text, spans = _flow_text(chain)
        if not text.strip():
            continue
        for s, e in split_sentences(text):
            lines: list[dict] = []
            src: set[int] = set()
            whole = True
            for sp in spans:
                a, b = max(sp["s"], s), min(sp["e"], e)
                if a >= b:
                    continue
                fr, pa, pb = sp["fr"], a - sp["s"], b - sp["s"]
                n = len(fr["text"])
                src.add(fr["bi"])
                if pa == 0 and pb >= n:
                    lines.append(fr["ln"])                 # 整行原样（同一个 dict）
                else:
                    lines.append(_slice_line(fr["ln"], pa, pb))
                    whole = False
                    splits += 1
            if not lines:
                continue
            units.append({"lines": lines, "body": list(lines), "src": src,
                          "whole": whole, "seq": len(units)})
            for sp in spans:
                owner.setdefault(id(sp["fr"]), len(units) - 1)

    # ---- 乘客：**有正文的块**里那些「整行都是公式占位」的行 ----
    # 几何不能丢：前端要靠 `.rngap` 定位公式覆盖层。
    # 整块都是这种行的块不进 `blocks_used` → 下面按原样保留（不会被拆散）。
    # 归属判据（2026-10 修「不同行/不同列的公式被算进一句」）：
    #   ① **同原块**优先 —— PyMuPDF 把这些行和正文放在同一个 block 里，本来是一体的；
    #   ② 否则必须**同栏**（`_same_column`：跨分栏走廊一律拒绝）；
    #   ③ 再按纵向距离、横向重叠度取最近 —— `above` **不再**无条件压过 `below`
    #      （旧逻辑让公式总和「上面最近的那句」粘在一起，跨段落也算）；
    #   ④ 找不到同栏候选 → 进 `orphans` 单独成块，绝不跨栏硬挂（几何不丢）。
    solid_blocks = {b for u in units for b in u["src"]}
    orphans: list[dict] = []
    for f in frags:
        if not f["blank"] or f["bi"] not in solid_blocks:
            continue
        best = None
        for g in frags:
            if g["blank"]:
                continue
            ui = owner.get(id(g))
            if ui is None:
                continue
            above = g["y"] + g["h"] <= f["y"] + 0.5 * f["h"]
            below = f["y"] + f["h"] <= g["y"] + 0.5 * g["h"]
            if not (above or below):
                continue
            same_bi = g["bi"] == f["bi"]
            ov = min(f["x"] + f["w"], g["x"] + g["w"]) - max(f["x"], g["x"])
            if not same_bi and not _same_column(f, g, corridors):
                continue                               # 另一栏：不参与
            d = (f["y"] - (g["y"] + g["h"])) if above else (g["y"] - (f["y"] + f["h"]))
            score = (0 if same_bi else 1, 0 if ov > 0 else 1, abs(d), -ov)
            if best is None or score < best[0]:
                best = (score, ui)
        if best is None:
            orphans.append(f)                          # 兜底：单独成块（几何不丢）
        else:
            units[best[1]].setdefault("extra", []).append(f["ln"])

    # ---- 排版面：每个单元一块；「一块一单元、没拆没并」的沿用原 id ----
    # 新块号按**最终顺序**编（排序键是「最小原块下标」，与流的顺序不完全一致）。
    prefix = _page_prefix(texts)
    emitted: list[tuple[tuple, dict]] = []
    for u in units:
        # ⚠️ **不要**按 y 重排行：行的阅读顺序来自流（同一视觉行的左右两半 y 可能差
        # 0.25pt，重排会把 `,` 排到它前面那句之前）。只把乘客行按 y 插进去。
        for ln in sorted(u.pop("extra", []),
                         key=lambda l: (float(l.get("y") or 0.0),
                                        float(l.get("x") or 0.0))):
            y = float(ln.get("y") or 0.0)
            i = next((k for k, o in enumerate(u["lines"])
                      if float(o.get("y") or 0.0) > y), len(u["lines"]))
            u["lines"].insert(i, ln)
        src = sorted(u["src"])
        blocks_used |= u["src"]
        bid = None
        # ⚠️ 「原样」只看**正文行**（`body`）：乘客是公式占位行，挂在它下面不该让这个
        # 块丢掉原 id（笔记/高亮按 id 存）。
        if len(src) == 1 and u["whole"]:
            old = texts[src[0]].get("lines") or []
            if (len(u["body"]) == len(old)
                    and all(a is b for a, b in zip(u["body"], old))):
                bid = texts[src[0]].get("id")              # 原样：沿用旧 id
        # 块框只按**正文行**量（`body`）：乘客行只是「几何锚点」，把它算进来会让
        # 块矩形跨到另一栏/另一段（前端 `.blk.sel` 的 outline 就是这么跨栏的）。
        x0, y0, x1, y1 = _union_line_rect(u["body"] or u["lines"])
        emitted.append(((src[0] if src else 0, u["seq"]),
                        {"id": bid, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                         "text": _join_lines(u["lines"]), "lines": u["lines"]}))
    for bi, blk in enumerate(texts):
        if bi not in blocks_used:
            emitted.append(((bi, -1), blk))                # 原样保留（见模块文档）
    # 混排块（既有正文行又有旋转行）：旋转行已从句子的流里摘掉，这里补回来单独成块
    # （整块都是旋转行的块不在这里处理 —— 上面「原样保留」已经带走了）。
    for bi, lns in vert.items():
        if bi not in blocks_used:
            continue
        for k, ln in enumerate(lns):
            x0, y0, x1, y1 = _union_line_rect([ln])
            emitted.append(((bi, 2_000_000 + k),
                            {"id": None, "x": x0, "y": y0,
                             "w": x1 - x0, "h": y1 - y0,
                             "text": _join_lines([ln]), "lines": [ln],
                             "vertical": True}))
    # 没有同栏归属的整行公式占位：单独成块（否则这条行会从所有块里消失，
    # 前端靠 `.rngap` 定位公式覆盖层就丢了）。
    for oi, f in enumerate(orphans):
        ln = f["ln"]
        x0, y0, x1, y1 = _union_line_rect([ln])
        emitted.append(((f["bi"], 1_000_000 + oi),
                        {"id": None, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                         "text": _join_lines([ln]), "lines": [ln]}))
    emitted.sort(key=lambda kv: kv[0])
    fresh = 0
    for _key, blk in emitted:
        if blk.get("id") is None:                          # 新块：按最终顺序编号
            blk["id"] = "%ss%d" % (prefix, fresh)
            fresh += 1

    stats["sentence_units"] = len(units)
    stats["sentence_merged"] = sum(1 for u in units if len(u["src"]) > 1)
    stats["sentence_line_splits"] = splits
    stats["sentence_tail_units"] = sum(
        1 for u in units if not _ends_sentence(_join_lines(u["lines"])))
    return [blk for _k, blk in emitted], stats


def _page_prefix(texts: list[dict]) -> str:
    """页面前缀（`p1b47` → `p1`）：新块号用它接在 `s` 后面（`p1s0`）。"""
    for blk in texts:
        m = re.match(r"(p\d+)", str(blk.get("id") or ""))
        if m:
            return m.group(1)
    return "b"


def _ends_sentence(text: str) -> bool:
    """这段文本是不是以句末标点收尾（不是的话就是「合并不了的碎片」，供排障）。"""
    t = (text or "").rstrip()
    return bool(t) and t[-1] in ".!?。！？"
