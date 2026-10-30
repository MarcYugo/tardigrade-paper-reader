'''
先完成跨span的公式框合并，再根据公式框位置辅助重新测定公式框位置，之后通过识别模型根据公式框裁剪图片转换为latex公式，之后完成回填
'''
# 先对block内的元素进行分类，分为公式字符和正文字符：原字典转换为新字典
# 跨span的公式框合并，仅针对公式字符 -> 公式span向右扫描，没有正文字符间隔的block或lines或span合并，作为公式框
#
# 合并前先把公式分成两类，两类的合并规则相反：
#   - 行内公式（夹在正文里）：不跨正文行合并，最后再按「同一行 + 间隔小于一个空格」
#     做一遍补合并；
#   - 独立公式（display，常带 (1) 编号）：不跨公式行合并，只放行上下限/分式/矩阵行
#     这种结构附件的窄间隙。
import re
from copy import deepcopy
from collections import defaultdict

_FORMULA_FONT_RE = re.compile(
    r"cmmi|cmsy|cmex|msam|msbm|cmr|lmmi|lmsy|lmex|euler|stmary|cmbx|cmti|cmsl|cmss|cmtt",
    re.I,
)
_EQUATION_NO_RE = re.compile(r"^\s*[\(\[]\s*\d+(?:[.\-]\d+)*\s*[\)\]]\s*$")
_LARGE_OP_CHARS = set("∑∏∫∮√")
# 括号类分隔符：单个括号字形本身就跨越多行（矩阵、分段函数、\left( … \right)）
_DELIM_ONLY_CHARS = set("()[]{}|‖⟨⟩")
# 关系符：一个位于大运算符左侧、只隔几个点的碎片如果含关系符，通常说明
# 公式还在继续（如 `‖v‖₂ =` + `√∑…`），可以跨正文行合并。
_RELATION_CHARS = set("=≈≠≡≤≥<>±∓→⇒⇔∈∉⊂⊃⊆⊇∝∼")


def merge_formula_boxes(
    blocks: list,
    *,
    body_line_tol: float = 1.6,
    space_ratio: float = 0.35,
    display_row_gap_ratio: float = 0.55,
) -> list:
    """
    输入:
        blocks: page.get_text("dict")["blocks"]
                建议先经过 merge_unformula_chars_to_string() 处理

    输出:
        blocks 的深拷贝。
        每个 block 新增 formula_boxes: List[dict]
        每个公式 span 新增 formula_box_id: int

    跨 block 的公式框会出现在多个 block 的 formula_boxes 中，
    但 id 相同，使用 collect_formula_boxes() 可去重。

    每个碎片/公式框都带 role（"inline" / "display"）：
      - 行内公式：同一视觉行上有紧挨着的正文（或同一 PyMuPDF 行里就有正文），
        受「一个簇不跨两条正文行」约束，最后还会做一轮「同一行 + 间隔小于
        一个空格」的补合并；
      - 独立（display）公式：独占一行，右侧常配 (1) 这样的编号。它们之间
        只允许很窄的跨行间隙（上下限/分式/矩阵行），否则上下相邻的两条公式
        会被串进同一个框（如 `y = ax²+bx+c` 和 `Δ = b²−4ac`）。

    含 ∑/∏/∫/√ 等大运算符的公式会额外做一轮结构补合并：
    上下限、被积式、分式分母即使落在相邻正文行，也会按几何附件关系
    并入运算符所在的簇。
    """

    def is_formula_font(font: str) -> bool:
        return bool(font) and bool(_FORMULA_FONT_RE.search(font))

    def union_bbox(a, b):
        return (
            min(a[0], b[0]),
            min(a[1], b[1]),
            max(a[2], b[2]),
            max(a[3], b[3]),
        )

    def box_area(b):
        return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])

    def overlap_len(a0, a1, b0, b1):
        return max(0.0, min(a1, b1) - max(a0, b0))

    def box_gap(a, b):
        return (
            max(a[0] - b[2], b[0] - a[2], 0.0),
            max(a[1] - b[3], b[1] - a[3], 0.0),
        )

    def bridge_rect(a, b, pad=0.3):
        """两个框之间用于检查正文障碍的走廊矩形。"""
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b

        # 已经明显重叠，不需要走廊
        if (
            min(ax1, bx1) - max(ax0, bx0) > 0.01
            and min(ay1, by1) - max(ay0, by0) > 0.01
        ):
            return None

        if ax1 <= bx0:
            x0, x1 = ax1, bx0
        elif bx1 <= ax0:
            x0, x1 = bx1, ax0
        else:
            x0, x1 = max(ax0, bx0), min(ax1, bx1)

        if ay1 <= by0:
            y0, y1 = ay1, by0
        elif by1 <= ay0:
            y0, y1 = by1, ay0
        else:
            y0, y1 = max(ay0, by0), min(ay1, by1)

        return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)

    def has_body_between(a, b, obstacles, pad=0.3):
        """两个公式片段之间如果夹着正文，则不允许合并。"""
        bridge = bridge_rect(a, b, pad=pad)
        if bridge is None:
            return False

        bx0, by0, bx1, by1 = bridge
        for ob in obstacles:
            ox0, oy0, ox1, oy1 = ob
            if (
                min(bx1, ox1) - max(bx0, ox0) > 0.35
                and min(by1, oy1) - max(by0, oy0) > 0.35
            ):
                return True
        return False

    # ------------------------------------------------------------
    # 1. 深拷贝并清理旧标记
    # ------------------------------------------------------------
    blocks = deepcopy(blocks)

    for block in blocks:
        block.pop("formula_boxes", None)
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span.pop("formula_box_id", None)

    # ------------------------------------------------------------
    # 2. 收集公式片段、正文障碍、正文行
    # ------------------------------------------------------------
    formula_frags = []
    body_raw = []
    obstacles = []

    for bi, block in enumerate(blocks):
        if block.get("type", 0) != 0:
            continue

        for li, line in enumerate(block.get("lines", [])):
            for si, span in enumerate(line.get("spans", [])):
                text = span.get("text", "")
                if not text.strip():
                    continue

                bbox = tuple(float(v) for v in span["bbox"])
                origin = span.get("origin", ((bbox[0] + bbox[2]) / 2, bbox[3]))
                origin = tuple(float(v) for v in origin)

                rec = {
                    "bbox": bbox,
                    "text": text,
                    "font": span.get("font", ""),
                    "size": float(span.get("size", bbox[3] - bbox[1])),
                    "origin": origin,
                    "block": bi,
                    "line": li,
                    "span": si,
                    "span_ref": span,
                }

                if is_formula_font(rec["font"]):
                    formula_frags.append(rec)
                else:
                    body_raw.append(rec)
                    obstacles.append(bbox)

    # 公式编号 (9) / [12] 不参与正文行归属
    body_for_line = [
        rec
        for rec in body_raw
        if not _EQUATION_NO_RE.fullmatch(rec["text"].strip())
    ]
    body_for_line.sort(key=lambda r: r["origin"][1])

    body_lines = []
    for rec in body_for_line:
        y = rec["origin"][1]
        matched = None

        for bline in body_lines:
            if abs(y - bline["y"]) <= body_line_tol:
                matched = bline
                break

        if matched is None:
            body_lines.append({
                "ys": [y],
                "y": y,
                "bbox": rec["bbox"],
            })
        else:
            matched["ys"].append(y)
            matched["y"] = sum(matched["ys"]) / len(matched["ys"])
            matched["bbox"] = union_bbox(matched["bbox"], rec["bbox"])

    def assign_body_line(bbox, size, origin=None, is_anchor=False):
        """
        根据公式片段与正文行的纵向关系，把行内公式归属到某一行正文。

        普通片段仍按 bbox 重叠判定；大运算符片段改用 origin（基线）到正文行
        纵向区间的距离：∑/∫/√ 的 bbox 很高，常同时压住上下两行，按重叠会把
        下一行行首的运算符误判成上一行公式的碎片（实测第 2 页 `‖v‖? =`
        后面的 `√∑` 就被吸进了上一行的点积公式）。
        独立公式通常归不到正文行，后面允许跨行合并。

        返回 (正文行下标, 该行到片段的水平间隙)；匹配不到返回 (None, inf)。
        间隙用来区分行内公式（紧挨正文）与「带说明文字的独立公式」
        （如 `Numbered formula:    E = mc²`，间隙有几十点）。
        """
        best_idx = None
        best_key = None

        for idx, bline in enumerate(body_lines):
            lb = bline["bbox"]

            gx = max(lb[0] - bbox[2], bbox[0] - lb[2], 0.0)
            if gx > max(40.0, 4.0 * size):
                continue

            if is_anchor and origin is not None:
                origin_y = origin[1]
                if lb[1] <= origin_y <= lb[3]:
                    dy = 0.0
                else:
                    dy = min(abs(origin_y - lb[1]), abs(origin_y - lb[3]))
                if dy > max(2.0, 0.6 * size):
                    continue
                key = (dy, gx)
            else:
                ov = overlap_len(bbox[1], bbox[3], lb[1], lb[3])
                if ov <= 0.0 or ov < max(0.35, 0.04 * size):
                    continue
                key = (-ov, gx)  # 重叠越大越好

            if best_key is None or key < best_key:
                best_idx = idx
                best_key = key

        if best_idx is None:
            return None, float("inf")
        return best_idx, float(best_key[1])

    for frag in formula_frags:
        line_idx, line_gap = assign_body_line(
            frag["bbox"],
            frag["size"],
            frag["origin"],
            any(ch in _LARGE_OP_CHARS for ch in frag["text"])
            or "cmex" in frag["font"].lower(),
        )
        frag["body_line"] = line_idx
        frag["body_gap"] = line_gap

    # ------------------------------------------------------------
    # 2.5 区分「行内公式」与「独立公式」
    # ------------------------------------------------------------
    # 两类公式的合并约束完全相反：
    #   - 行内公式不能跨正文行合并（否则上下两行的公式被串起来）；
    #   - 独立公式之间也不能跨「公式行」合并（否则 `E = mc²`、`∑∑`、
    #     `∬`、`lim` 会连成一个大框，实测这一条是主要错误来源）。
    # 判据（按优先级）：
    #   1. 与正文行的水平间隙小于半个字宽，或同一 PyMuPDF 行（block+line）
    #      里就有正文 → 行内公式；
    #   2. 其余 → 独立公式；独立公式还会去认领同一视觉行右侧的 `(1)`/`[12]`
    #      编号，用来阻止不同编号的两条公式被合并（双栏排版两栏各有一套编号，
    #      按「水平距离 + 纵向偏差」选）。
    eq_no_recs = [
        rec
        for rec in body_raw
        if _EQUATION_NO_RE.fullmatch(rec["text"].strip())
    ]

    line_has_body = set()
    for bi, block in enumerate(blocks):
        if block.get("type", 0) != 0:
            continue
        for li, line in enumerate(block.get("lines", [])):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if text.strip() and not is_formula_font(span.get("font", "")):
                    line_has_body.add((bi, li))
                    break

    def nearest_eq_no(frag, dy_tol):
        """同一视觉行、位于右侧、纵向对齐的公式编号。

        容差要按页面主字号（而不是碎片自己的字号）算：∑/∫ 的上下限是 7pt
        小字，但它们在编号的纵向覆盖范围内，按 1.5*7 算会错过大括号里的编号。
        """
        cy = (frag["bbox"][1] + frag["bbox"][3]) / 2.0
        best = None
        best_key = None

        for k, rec in enumerate(eq_no_recs):
            rb = rec["bbox"]
            if rb[0] < frag["bbox"][2] - 1.0:
                continue

            dy = abs((rb[1] + rb[3]) / 2.0 - cy)
            if dy > dy_tol:
                continue

            # 先按水平距离（同一栏的编号最近），再按纵向偏差。
            # 双栏排版时左右两栏各有一套编号，只看 dy 会把右栏的编号
            # 安到左栏公式头上（实测第 2 页大面积错配）。
            gap_x = max(0.0, rb[0] - frag["bbox"][2])
            key = (gap_x + 2.0 * dy, dy)
            if best_key is None or key < best_key:
                best_key = key
                best = k

        return best

    base_size = max(
        (frag["size"] for frag in formula_frags),
        default=10.0,
    )
    eq_dy_tol = max(4.0, min(1.5 * base_size, 16.0))

    for frag in formula_frags:
        inline = (
            frag["body_line"] is not None
            and frag["body_gap"] <= max(3.0, 0.6 * frag["size"])
        ) or (frag["block"], frag["line"]) in line_has_body

        # 行内公式不去抢右侧的编号：双栏排版里行内公式和另一条公式的编号
        # 很容易处在同一个水平带上。
        frag["eq_id"] = None if inline else nearest_eq_no(frag, eq_dy_tol)
        frag["role"] = "inline" if inline else "display"

    # 视觉行：基线相近的碎片算同一行。独立公式不允许跨行合并，
    # 只放行很窄的行间间隙（分式分子分母、矩阵行、∑/∫ 的上下限）。
    row_ids = [-1] * len(formula_frags)
    row_y = None
    row_tol = 0.0
    row_id = -1

    for i in sorted(
        range(len(formula_frags)),
        key=lambda k: formula_frags[k]["origin"][1],
    ):
        frag = formula_frags[i]
        y = frag["origin"][1]
        tol = max(1.5, 0.5 * frag["size"])

        if row_y is None or abs(y - row_y) > max(row_tol, tol):
            row_id += 1
            row_y, row_tol = y, tol
        else:
            row_tol = max(row_tol, tol)
            row_y = (row_y + y) / 2.0

        row_ids[i] = row_id

    for i, frag in enumerate(formula_frags):
        frag["row"] = row_ids[i]

    # ------------------------------------------------------------
    # 3. 修正上下标/根号被归到相邻正文行的问题
    # ------------------------------------------------------------
    def side_attach(a, b):
        """判断两个框是否是同一基线上的左右相邻结构。"""
        ba, bb = a["bbox"], b["bbox"]
        gx, _ = box_gap(ba, bb)

        scale = max(a["size"], b["size"], 1.0)
        w_min = max(1e-6, min(ba[2] - ba[0], bb[2] - bb[0]))
        h_min = max(1e-6, min(ba[3] - ba[1], bb[3] - bb[1]))

        ox = overlap_len(ba[0], ba[2], bb[0], bb[2])
        oy = overlap_len(ba[1], ba[3], bb[1], bb[3])

        if gx > max(0.8, 0.15 * scale):
            return False
        if ox > max(0.6, 0.18 * w_min):
            return False
        if oy < max(0.5, 0.08 * h_min):
            return False

        return True

    for _ in range(4):
        changed = False

        for i, frag in enumerate(formula_frags):
            la = frag["body_line"]
            best_line = None
            best_area = box_area(frag["bbox"])

            for j, other in enumerate(formula_frags):
                if i == j:
                    continue

                lb = other["body_line"]
                if lb is None or lb == la:
                    continue

                if not side_attach(frag, other):
                    continue

                other_area = box_area(other["bbox"])
                if other_area > best_area:
                    best_area = other_area
                    best_line = lb

            if best_line is not None:
                frag["body_line"] = best_line
                changed = True

        if not changed:
            break

    # ------------------------------------------------------------
    # 4. 判断两个公式片段是否可合并
    # ------------------------------------------------------------
    def large_op_attach(a, b):
        """大运算符与它的上下限/被积式之间的附件关系。

        只认「横向重叠」或「与运算符中心对齐」两种位置关系，不再用
        `gx≈0` 兜底：那个兜底会把上一行公式的右端和下一行公式的 ∑/∫
        左端当成一个结构（实测 `E = mc²` 和下一行 `∑∑` 就是这样被串起来的）。
        """
        for op, other in ((a, b), (b, a)):
            if not (
                any(ch in _LARGE_OP_CHARS for ch in op["text"])
                or "cmex" in op["font"].lower()
            ):
                continue

            ob, tb = op["bbox"], other["bbox"]
            op_w = max(1e-6, ob[2] - ob[0])
            op_size = max(op["size"], 1.0)

            ox = overlap_len(ob[0], ob[2], tb[0], tb[2])
            cx_op = (ob[0] + ob[2]) / 2.0
            cx_t = (tb[0] + tb[2]) / 2.0
            centered = abs(cx_t - cx_op) <= max(0.9 * op_w, op_size)
            # 横向重叠只有在 other 是「限字号」小字时才算数：上下限/被积式
            # 都是小一号的字。否则上一行公式末尾的碎片只要横向压住运算符
            # 就会被吸进来（实测 lim 的分母和下一行的 ∫ 就撞在一起）。
            limit_like = other["size"] <= 0.92 * op["size"] + 1e-6

            gy = max(ob[1] - tb[3], tb[1] - ob[3], 0.0)
            # 限字号碎片可以离得远一些（∑/∫ 的 bbox 只有字高，实际字形更高），
            # 主体字号则要求几乎贴着，否则上下相邻的公式会被串起来。
            max_gy = (
                max(8.0, 2.2 * op_size)
                if limit_like
                else max(3.0, 0.8 * op_size)
            )
            if (centered or (ox > 0.0 and limit_like)) and gy <= max_gy:
                return True

        return False

    def stacked_attach(a, b):
        """跨行的两个 display 碎片是否属于同一条公式（不是上下相邻的两条）。

        只认四种结构：
          1. 两者纵向已经重叠（只是基线略有不同，例如上下标）；
          2. 括号类分隔符（`(` `[` `{` …）—— 它本身就横跨两行；
          3. 同一个 PyMuPDF block（矩阵行、分式的分子/分母通常归在一块）；
          4. 斜向相邻（如行末的指数 `n` 和下一行的 `= e`）。

        表格里一列数据（`2.3·10^19` / `9.6·10^18` …）虽然上下紧贴、
        左右对齐，但分属不同 block、也没有任何结构附件，会被正确拒绝。
        """
        ba, bb = a["bbox"], b["bbox"]
        gx, gy = box_gap(ba, bb)
        scale = max(a["size"], b["size"], 1.0)

        # 字号明显不同 → 一方是另一方的上下标/限
        small = min(a["size"], b["size"])
        large = max(a["size"], b["size"], 1e-6)
        size_ratio_ok = small <= 0.85 * large
        # 括号类的邻接碎片（如 `)n` 里的 `)`）允许斜向附件
        delim_chars = any(
            ch in _DELIM_ONLY_CHARS for ch in a["text"] + b["text"]
        )

        # 真正的纵向重叠 → 上下标、分式的分子与括号等，算同一结构。
        # （表格上下两行的数字 bbox 只是相接、不重叠。）
        if overlap_len(ba[1], ba[3], bb[1], bb[3]) > 0.0:
            return True

        if gy > max(1.5, display_row_gap_ratio * scale):
            return False

        for frag in (a, b):
            text = frag["text"].strip()
            if (
                text
                and len(text) <= 2
                and all(ch in _DELIM_ONLY_CHARS for ch in text)
            ):
                return True

        if a["block"] == b["block"]:
            return True

        # 斜向相邻要求「横纵都真的错开」，且要么字号不同（指数/下标 vs 主体），
        # 要么紧贴括号（如 `= e` 紧跟 `)n`）。
        diag_gap = max(5.5, 0.65 * scale)
        if (
            (size_ratio_ok or delim_chars)
            and 0.5 < gx <= diag_gap
            and 0.5 < gy <= diag_gap
        ):
            return True

        return False

    def merge_score(a, b):
        ba, bb = a["bbox"], b["bbox"]
        gx, gy = box_gap(ba, bb)

        scale = max(a["size"], b["size"], 1.0)
        # 纵向仍按放宽的间距，交给后面的「行」规则细判（∑/∫ 的上下限
        # 离运算符有几个点到十几个点）；横向则只对独立公式收紧，避免把
        # 表格同一行的两列数字/表头并成一个框。
        max_gap = max(7.0, 2.2 * scale)
        max_gap_x = max_gap
        if not (a["role"] == "inline" and b["role"] == "inline"):
            max_gap_x = max(6.0, 1.2 * scale)

        if gx > max_gap_x or gy > max_gap:
            return None

        # 不同视觉行之间：只有「大运算符 + 上下限」这种结构附件能跨行，
        # 其余（尤其上下相邻的两条独立公式）一律不能跨行合并。
        # 行内碎片还可以靠更小的「一个空格」闸门例外（见第二遍扫描）。
        op_attached = large_op_attach(a, b)
        if a["row"] != b["row"]:
            # 行内 / 独立混排：通常是排版把同一条公式拆到了两行上，
            # 只允许几乎贴在一起的情况，大运算符附件也不能例外。
            if a["role"] != b["role"]:
                if gy > max(1.5, 0.35 * scale):
                    return None
            elif not op_attached and not stacked_attach(a, b):
                return None

        w1, h1 = ba[2] - ba[0], ba[3] - ba[1]
        w2, h2 = bb[2] - bb[0], bb[3] - bb[1]
        w_min = max(1e-6, min(w1, w2))
        h_min = max(1e-6, min(h1, h2))

        ox = overlap_len(ba[0], ba[2], bb[0], bb[2])
        oy = overlap_len(ba[1], ba[3], bb[1], bb[3])

        cx1, cy1 = (ba[0] + ba[2]) / 2, (ba[1] + ba[3]) / 2
        cx2, cy2 = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2

        # 包围盒互相擦碰：只有**横纵都**重叠很弱时才当作相邻正文行的字形碰撞。
        # 大运算符的上下限常呈「横向只压住几个点、纵向整个嵌进去」的形态
        # （如 ∑ 的下标 `i=1 ui vi`），只看横向会把它们误杀；但纵向重叠也必须
        # 过半，否则两条紧邻的独立公式会因高根号/分式的 bbox 外溢而被串起来。
        if ox > 0.05 and oy > 0.05:
            if (
                ox < max(0.35, 0.22 * w_min)
                and oy < max(0.35, 0.5 * h_min)
            ):
                return None

        # 同行左右结构
        horiz = (
            gx <= max_gap
            and (
                oy >= max(0.25, 0.04 * h_min)
                or abs(cy1 - cy2) <= 0.8 * max(h1, h2)
            )
        )

        # 同列上下结构：上下标、分式、矩阵行等
        vert = (
            gy <= max(3.0, 0.8 * scale)
            and (
                ox >= max(0.45, 0.08 * w_min)
                or abs(cx1 - cx2) <= max(1.5, 0.45 * w_min)
            )
        )

        # ∑ / ∫ 等大符号的上下限可能离得较远（上下限通常与运算符中心对齐）
        if not vert and op_attached:
            vert = True

        # 斜向相邻：矩阵括号、根号内部结构等
        diag = (
            gx > 0.0
            and gy > 0.0
            and gx <= max(5.5, 0.65 * scale)
            and gy <= max(5.5, 0.65 * scale)
        )

        if not (horiz or vert or diag):
            return None

        # 不允许跨过正文合并
        if has_body_between(ba, bb, obstacles, pad=max(0.25, 0.03 * scale)):
            return None

        return gx + gy + 0.04 * (abs(cx1 - cx2) + abs(cy1 - cy2))

    if not formula_frags:
        for block in blocks:
            block["formula_boxes"] = []
        return blocks

    # ------------------------------------------------------------
    # 5. 按空间距离从近到远合并，并保证一个簇不跨越两条正文行
    # ------------------------------------------------------------
    # 只有行内公式才记录正文行；独立公式（哪怕旁边有个说明文字）不算
    line_sets = [
        {frag["body_line"]}
        if frag["body_line"] is not None and frag["role"] == "inline"
        else set()
        for frag in formula_frags
    ]
    # 公式编号：同一行不同编号的两条公式不能被并进同一个框
    eq_sets = [
        {frag["eq_id"]} if frag["eq_id"] is not None else set()
        for frag in formula_frags
    ]

    def line_compatible(sa, sb):
        return not sa or not sb or bool(sa & sb)

    def eq_compatible(sa, sb):
        return not sa or not sb or bool(sa & sb)

    n = len(formula_frags)

    # ------------------------------------------------------------
    # 5.1 找出分式的分子 / 分母配对
    # ------------------------------------------------------------
    # 分子、分母纵向紧贴（几个点以内、但不重叠）、水平中心对齐。
    # 把它们记成一对，后面判断「这个碎片该归谁」时用来阻止跨公式吸收：
    # 如果一个碎片的另一半在**别的簇**里，它就是相邻那条公式的分式部件，
    # 而不是本行 ∑/∫ 的上下限（实测：上一行 `lim (…)/h` 的分母 h 会压在
    # 下一行 `∫` 的上沿，被当成积分的下限吸走）。
    frac_peers = [set() for _ in range(n)]
    for i in range(n):
        ba = formula_frags[i]["bbox"]
        ca = (ba[0] + ba[2]) / 2.0
        for j in range(i + 1, n):
            # 同一公式的分子/分母一定属于同一条正文行（独立公式为 None）。
            # 相邻两行各自的小字号碎片虽然也「上下紧贴、中心对齐」，
            # 但 body_line 不同，不能当成一个分式。
            line_i = formula_frags[i]["body_line"]
            line_j = formula_frags[j]["body_line"]
            if line_i is not None and line_j is not None and line_i != line_j:
                continue
            # 只认「脚本字号」的上下两半：表格里上下紧贴、字号相同的
            # 两行数字（2.3·10^19 / 9.6·10^18）不能当成一个分式。
            if (
                min(formula_frags[i]["size"], formula_frags[j]["size"])
                > 0.85 * base_size
            ):
                continue
            bb = formula_frags[j]["bbox"]
            gap_y = max(bb[1] - ba[3], ba[1] - bb[3], 0.0)
            if gap_y <= 0.0:
                continue
            if gap_y > max(
                1.0,
                0.45 * max(formula_frags[i]["size"], formula_frags[j]["size"]),
            ):
                continue
            w_min = max(1e-6, min(ba[2] - ba[0], bb[2] - bb[0]))
            if abs((bb[0] + bb[2]) / 2.0 - ca) <= max(1.0, 0.35 * w_min):
                frac_peers[i].add(j)
                frac_peers[j].add(i)

    # 分子常常由多个碎片组成（`n(a₁+aₙ)` 被拆成 8 个碎片），逐个碎片比中心
    # 会漏配：分母 `2` 的中心 279.34 落在分子的**整体**范围 261.99…296.67 内，
    # 却与任何一个碎片中心都差 1.9pt 以上，于是配不上对 —— 分母随后被下一行
    # 同 block 的 ∑ 当成上下限吸走（`stacked_attach` 认「同一 PyMuPDF block」），
    # 只剩分子的框在裁剪时把分母切掉一半（实测第 3 页 `n(a₁+aₙ)/2`）。
    # 所以再按「整行」配一次：紧贴上方的那一排脚本字号碎片串起来以后，
    # 横向范围包住分母中心、纵向只隔一点点 → 这一排都是分母的配对碎片。
    def script_row_above(j, max_gap):
        """返回紧贴在碎片 j 上方、同一字号档的那一排碎片下标。

        不比较 body_line：分式的分母比分子低半行，纵向会压到下一行正文上，
        body_line 经常和分子不同（实测 `n(a₁+aₙ)/2` 的 `2` 被归到下一行）。
        判定只靠几何：纵向只隔几个点、横向同一连续段、双方都是脚本字号。
        """
        frag = formula_frags[j]
        bbox = frag["bbox"]

        above = []
        for i in range(n):
            if i == j:
                continue
            other = formula_frags[i]
            if other["size"] > 0.85 * base_size:
                continue
            ob = other["bbox"]
            if ob[1] >= bbox[1] or ob[3] > bbox[1] + 0.3:
                continue
            if bbox[1] - ob[3] > max_gap:
                continue
            above.append(i)

        if not above:
            return []

        # 只留贴近分式横线的那一排（底边最高的一档）
        bottom = max(formula_frags[i]["bbox"][3] for i in above)
        return [
            i for i in above
            if formula_frags[i]["bbox"][3] >= bottom - 1.5
        ]

    def x_run_around(indices, cx, tol):
        """indices 里与 cx 横向相连的那一段，防止把另一栏的碎片也串进来。"""
        run = {
            k for k in indices
            if formula_frags[k]["bbox"][0] - tol <= cx
            <= formula_frags[k]["bbox"][2] + tol
        }
        if not run:
            return []

        changed = True
        while changed:
            changed = False
            r0 = min(formula_frags[k]["bbox"][0] for k in run)
            r1 = max(formula_frags[k]["bbox"][2] for k in run)
            for k in indices:
                if k in run:
                    continue
                b = formula_frags[k]["bbox"]
                if b[0] <= r1 + tol and b[2] >= r0 - tol:
                    run.add(k)
                    changed = True

        return sorted(run)

    for j in range(n):
        frag = formula_frags[j]
        if frag["size"] > 0.85 * base_size:
            continue

        bbox = frag["bbox"]
        cx = (bbox[0] + bbox[2]) / 2.0
        # 分子/分母之间只隔一根分数线，纵向间隙很小（实测 `n(a₁+aₙ)/2`
        # 只有 1.29pt）；相邻两行公式虽然也「贴得很近」，但通常 ≥ 2.5pt。
        max_gap = max(1.2, 0.22 * frag["size"])
        tol = max(2.0, 0.4 * frag["size"])

        run = x_run_around(script_row_above(j, max_gap), cx, tol)
        if not run:
            continue

        # 分母通常不比分子宽。∑/∫ 的上下限是「下宽上窄」（上限 `n` 压在
        # `i=1` 上方），用这一条把它们排除，避免误配成「分式」后挡住
        # 大运算符的附件合并。
        run_w = (
            max(formula_frags[k]["bbox"][2] for k in run)
            - min(formula_frags[k]["bbox"][0] for k in run)
        )
        if (bbox[2] - bbox[0]) > run_w + 1.0:
            continue

        # 分数线居中：分母必须对准分子的中心。相邻两行公式里碰巧
        # 压在下面的一段（如 `∑ᵏ…` 下面那行的 `f(x+h)`）通常偏在一侧。
        run_cx = (
            max(formula_frags[k]["bbox"][2] for k in run)
            + min(formula_frags[k]["bbox"][0] for k in run)
        ) / 2.0
        if abs(run_cx - cx) > max(1.2, 0.15 * run_w):
            continue

        # 分子与分母之间的那条缝里不能夹着别的公式碎片：`1/√d_k` 的
        # `d` 上方贴着的是根号 `√`（不是分子 `1`），少了这一条会把根号里的
        # `d` 配给上一行的 `1`，把一条公式劈成两个框。
        # 只看「分母自己那段横向范围」：换行后紧邻在左边的大运算符（`∑`）
        # 纵向也压着这条缝，但横向不碰分母，不能算夹在中间。
        gap_lo = max(formula_frags[k]["bbox"][3] for k in run)
        gap_hi = bbox[1]
        run_x0 = bbox[0] - 0.3
        run_x1 = bbox[2] + 0.3
        blocked = False
        for i in range(n):
            if i == j or i in run:
                continue
            ob = formula_frags[i]["bbox"]
            if ob[3] <= gap_lo + 1e-6 or ob[1] >= gap_hi - 1e-6:
                continue
            if ob[2] <= run_x0 or ob[0] >= run_x1:
                continue
            blocked = True
            break
        if blocked:
            continue

        for k in run:
            frac_peers[j].add(k)
            frac_peers[k].add(j)

        # 分子 / 分母是同一条视觉行：把分母的正文行对齐到分子的。
        # 分母比分子低半行，纵向会和**下一行**的正文行大面积重叠（实测
        # `n(a₁+aₙ)/2` 的 `2` 被归给了下一行 `∑` 所在的第 18 行），
        # 行集合不一致会让 5.2 的「同一正文行」闸门拒绝成对合并，
        # 反而让 `2` 被下一行的 ∑ 吸走。
        run_lines = {
            formula_frags[k]["body_line"]
            for k in run
            if formula_frags[k]["body_line"] is not None
        }
        if len(run_lines) == 1:
            line = next(iter(run_lines))
            formula_frags[j]["body_line"] = line
            if formula_frags[j]["role"] == "inline":
                line_sets[j] = {line}

    def has_foreign_peer(indices, keep):
        """indices 里是否有分式部件，而它的另一半不在 keep 里。"""
        for idx in indices:
            if frac_peers[idx] - keep:
                return True
        return False

    edges = []

    for i in range(n):
        for j in range(i + 1, n):
            if not line_compatible(line_sets[i], line_sets[j]):
                continue

            score = merge_score(formula_frags[i], formula_frags[j])
            if score is not None:
                edges.append((score, i, j))

    edges.sort(key=lambda x: x[0])

    parent = list(range(n))
    rank = [0] * n

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # ------------------------------------------------------------
    # 5.2 先把分式的分子/分母并起来
    # ------------------------------------------------------------
    # 它们是同一结构的上下两半，先并成簇后字号会回到主体字号，
    # 后面的「大运算符附件」就不会把其中一半当成相邻行 ∑/∫ 的上下限。
    for i in range(n):
        for j in sorted(frac_peers[i]):
            if j <= i:
                continue
            # 只把「确定是同一个分式」的两半并起来：同一条正文行，
            # 或者同一个 PyMuPDF block。避免把上下两条独立公式里
            # 恰好对齐的两个小字号碎片配成一对。
            fi, fj = formula_frags[i], formula_frags[j]
            same_line = (
                fi["body_line"] is not None
                and fi["body_line"] == fj["body_line"]
            )
            if not same_line and fi["block"] != fj["block"]:
                continue
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            if rank[ri] < rank[rj]:
                ri, rj = rj, ri
            parent[rj] = ri
            if rank[ri] == rank[rj]:
                rank[ri] += 1
            line_sets[ri] |= line_sets[rj]
            line_sets[rj] = set()
            eq_sets[ri] |= eq_sets[rj]
            eq_sets[rj] = set()

    for _, i, j in edges:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue

        if not line_compatible(line_sets[ri], line_sets[rj]):
            continue

        if not eq_compatible(eq_sets[ri], eq_sets[rj]):
            continue

        if rank[ri] < rank[rj]:
            ri, rj = rj, ri

        parent[rj] = ri
        if rank[ri] == rank[rj]:
            rank[ri] += 1

        line_sets[ri] |= line_sets[rj]
        line_sets[rj] = set()
        eq_sets[ri] |= eq_sets[rj]
        eq_sets[rj] = set()

    # ------------------------------------------------------------
    # 5.5 大运算符结构补合并
    # ------------------------------------------------------------
    # 上一阶段要求「一个簇不跨越两条正文行」，但大运算符公式天然跨行：
    #   - ∑/∫ 的上下限、被积式会落在运算符基线之外，被归到相邻正文行；
    #   - 分式的分子/分母、根号内部也可能各占一行；
    #   - `body_line` 的上下标修正又会把求和项挪到运算符行，把分母留在下一行，
    #     使同一条公式的两半永远无法通过 line_compatible。
    # 因此这里在**已有簇**上再做一轮：只有含大运算符的簇（锚点簇）才能吸收
    # 其它簇，且必须满足明确的几何附件关系，避免把上下相邻的独立公式串起来。
    def frag_has_large_op(idx: int) -> bool:
        frag = formula_frags[idx]
        return (
            any(ch in _LARGE_OP_CHARS for ch in frag["text"])
            or "cmex" in frag["font"].lower()  # CMEX 就是 TeX 的大运算符字体
        )

    anchor_flags = [frag_has_large_op(i) for i in range(n)]
    relation_flags = [
        any(ch in _RELATION_CHARS for ch in formula_frags[i]["text"])
        for i in range(n)
    ]

    def component_info():
        """按当前并查集收集簇的 bbox/size/锚点/关系符信息。"""
        comps = defaultdict(list)
        for i in range(n):
            comps[find(i)].append(i)

        info = {}
        for root, idxs in comps.items():
            bbox = formula_frags[idxs[0]]["bbox"]
            for i in idxs[1:]:
                bbox = union_bbox(bbox, formula_frags[i]["bbox"])
            info[root] = {
                "indices": idxs,
                "bbox": bbox,
                "size": max(formula_frags[i]["size"] for i in idxs),
                "has_anchor": any(anchor_flags[i] for i in idxs),
                "has_relation": any(relation_flags[i] for i in idxs),
            }
        return info

    def op_attach_ok(ainfo, binfo):
        """锚点簇 A 能否吸收簇 B。

        三种受支持的附件关系：
        1. 上下限/被积式/分式分母：B 的字号明显更小，且位于运算符右半侧，
           与 A 有纵向重叠；
        2. 关系符续接：B 在 A 左侧、只隔几个点、含 `=` 等关系符，且 A 的
           大运算符不比 B 小（排除行内小根号把上一行公式吸进来）；
        3. 独立成行的上下限：B 整体在运算符上方/下方，水平居中、字号更小、
           间隙只有几个点。
        """
        ab = ainfo["bbox"]
        bb = binfo["bbox"]

        anchor_idx = [
            i for i in ainfo["indices"] if anchor_flags[i]
        ]
        if not anchor_idx:
            return False

        a_size = max(formula_frags[i]["size"] for i in anchor_idx)
        b_size = binfo["size"]

        oy = overlap_len(ab[1], ab[3], bb[1], bb[3])
        h_min = max(1e-6, min(ab[3] - ab[1], bb[3] - bb[1]))

        for ai in anchor_idx:
            op_bbox = formula_frags[ai]["bbox"]
            op_w = max(1e-6, op_bbox[2] - op_bbox[0])

            # 1) 上下限 / 被积式 / 分式分母：小字号 + 位于运算符右半侧 + 纵向重叠
            if b_size <= 0.9 * a_size + 1e-6:
                right_side = bb[0] >= op_bbox[0] + 0.35 * op_w
                inside_span = bb[0] <= ab[2] + max(2.0, 0.5 * a_size)
                if (
                    right_side
                    and inside_span
                    and oy >= max(0.5, 0.05 * h_min)
                ):
                    return True

            # 2) 关系符续接：B 在运算符左侧，间隙很小，且运算符是「主体字号」
            if (
                binfo["has_relation"]
                and b_size <= a_size + 1e-6
                and oy >= max(0.5, 0.05 * h_min)
            ):
                gap_left = op_bbox[0] - bb[2]
                if 0.0 < gap_left <= max(3.0, 0.35 * a_size):
                    return True

            # 3) 独立成行的上下限：完全在运算符上方/下方，水平居中且间隙很小
            if b_size <= 0.85 * a_size + 1e-6 and oy <= 0.0:
                gap_y = max(
                    op_bbox[1] - bb[3],
                    bb[1] - op_bbox[3],
                    0.0,
                )
                cx_op = (op_bbox[0] + op_bbox[2]) / 2
                cx_b = (bb[0] + bb[2]) / 2
                if (
                    gap_y <= max(1.0, 0.3 * a_size)
                    and abs(cx_b - cx_op)
                    <= 0.35 * max(op_w, bb[2] - bb[0])
                ):
                    return True

        return False

    # 逐个吸收；每合并一次就重建簇信息（页内碎片量很小，代价可忽略）。
    for _ in range(n):
        comps = component_info()
        changed = False

        for ar, ainfo in comps.items():
            if not ainfo["has_anchor"]:
                continue

            for br, binfo in comps.items():
                if br == ar or binfo["has_anchor"]:
                    continue
                if not op_attach_ok(ainfo, binfo):
                    continue

                # 直接并簇：这里不看 line_compatible，因为大运算符的上下限/
                # 被积式天然会落在相邻正文行上。但「分式的另一半」必须保护：
                # 上一行分式的分子/分母会被下一行的 ∑/∫ 当成自己的上下限
                # 吸走（两者的 bbox 在竖直方向上真的重叠）。
                ra, rb = find(ar), find(br)
                if ra == rb:
                    continue
                if not eq_compatible(eq_sets[ra], eq_sets[rb]):
                    continue
                # B 里如果混着「另一条公式里分式的另一半」，说明它是
                # 那条公式的分子/分母，不能当成本行运算符的上下限。
                if has_foreign_peer(
                    binfo["indices"],
                    set(ainfo["indices"]) | set(binfo["indices"]),
                ):
                    continue
                if rank[ra] < rank[rb]:
                    ra, rb = rb, ra
                parent[rb] = ra
                if rank[ra] == rank[rb]:
                    rank[ra] += 1
                line_sets[ra] |= line_sets[rb]
                line_sets[rb] = set()
                eq_sets[ra] |= eq_sets[rb]
                eq_sets[rb] = set()
                changed = True
                break

            if changed:
                break

        if not changed:
            break

    # ------------------------------------------------------------
    # 5.6 行内公式补合并：同一正文行、间隔小于一个空格
    # ------------------------------------------------------------
    # 第一遍受「间隙 / 包围盒碰撞 / 正文障碍」三道闸门限制，行内公式经常
    # 还剩下一两个没并进来的碎片（典型：公式两端与正文只隔半个空格，
    # 或相邻 span 的 bbox 只擦碰了一点点被判成碰撞）。
    # 这里对已经成形的簇再扫一遍：只要落在**同一条正文行**、水平间隔小于
    # 一个空格宽，就直接并进来。
    for _ in range(2):
        comps = defaultdict(list)
        for i in range(n):
            comps[find(i)].append(i)

        comp_bbox = {}
        comp_size = {}
        for root, idxs in comps.items():
            bbox = formula_frags[idxs[0]]["bbox"]
            for i in idxs[1:]:
                bbox = union_bbox(bbox, formula_frags[i]["bbox"])
            comp_bbox[root] = bbox
            comp_size[root] = max(formula_frags[i]["size"] for i in idxs)

        roots = list(comps)
        changed = False

        for ai in range(len(roots)):
            for bj in range(ai + 1, len(roots)):
                ra, rb = find(roots[ai]), find(roots[bj])
                if ra == rb:
                    continue

                # 只补行内公式：两侧都必须已经属于某条正文行
                if not line_sets[ra] or not line_sets[rb]:
                    continue
                if not line_compatible(line_sets[ra], line_sets[rb]):
                    continue
                if not eq_compatible(eq_sets[ra], eq_sets[rb]):
                    continue

                size = max(comp_size[ra], comp_size[rb], 1.0)
                space = max(1.2, space_ratio * size)

                ba, bb = comp_bbox[ra], comp_bbox[rb]
                gx, gy = box_gap(ba, bb)

                if gx > space:
                    continue
                if gy > max(1.0, 0.3 * size):
                    continue
                # 必须是同一视觉行（基线差不到半个字高）
                if (
                    overlap_len(ba[1], ba[3], bb[1], bb[3]) <= 0.0
                    and abs((ba[1] + ba[3]) - (bb[1] + bb[3])) > size
                ):
                    continue
                if has_body_between(ba, bb, obstacles, pad=0.2):
                    continue

                if rank[ra] < rank[rb]:
                    ra, rb = rb, ra
                parent[rb] = ra
                if rank[ra] == rank[rb]:
                    rank[ra] += 1

                line_sets[ra] |= line_sets[rb]
                line_sets[rb] = set()
                eq_sets[ra] |= eq_sets[rb]
                eq_sets[rb] = set()
                comp_bbox[ra] = union_bbox(ba, bb)
                comp_size[ra] = size
                changed = True

        if not changed:
            break

    # ------------------------------------------------------------
    # 5.7 同一行的左右两半（**簇级**补合并）
    # ------------------------------------------------------------
    # 一条公式被拆成「左半 + 右半」两个簇时（下标/上下限的 body_line 不同、
    # 或一半被投成 display），两个簇的 bbox 在**横向**上只差不到 1pt、在
    # **纵向**上几乎完全重叠 —— 视觉上就是一条公式。实测 AutoAlpha 第 2 页
    # `lim_{i→∞}(1/1)·{corr(a_t^{(i)},r_t^{(h)})}_{t=1}^T`：左半
    # `{corr(a_t^{(i)}, r_t^{(h)}`（role=display，正文行 81）与右半
    # `)}_{t=1}^T`（正文行 79）只差 **0.5pt**，前端画出「裁图 + KaTeX」两个框。
    #
    # ⚠️ 必须在**簇级**判：碎片级判不开 —— 竖直相叠的两条独立公式里，上一条的
    # 下标与下一条的字形常常横向相接、纵向也重叠一半（实测 latex_sample p2
    # `u·v=Σuᵢvᵢ` 的 `u` 与下面 `∬` 的 `∫`：gap=0、纵向重叠 0.54）；而整体
    # 比时那两条公式的 bbox 只重叠 13%，一判就分开。
    for _ in range(3):
        comps = defaultdict(list)
        for i in range(n):
            comps[find(i)].append(i)

        comp_bbox = {}
        for root, idxs in comps.items():
            bbox = formula_frags[idxs[0]]["bbox"]
            for i in idxs[1:]:
                bbox = union_bbox(bbox, formula_frags[i]["bbox"])
            comp_bbox[root] = bbox

        roots = list(comps)
        changed = False

        for ai in range(len(roots)):
            for bj in range(ai + 1, len(roots)):
                ra, rb = find(roots[ai]), find(roots[bj])
                if ra == rb:
                    continue
                if not eq_compatible(eq_sets[ra], eq_sets[rb]):
                    continue

                ba, bb = comp_bbox[ra], comp_bbox[rb]
                gx, _gy = box_gap(ba, bb)
                if gx > 1.0:
                    continue                      # 中间有空隙 → 不是左右两半
                oy = overlap_len(ba[1], ba[3], bb[1], bb[3])
                h_min = max(1e-6, min(ba[3] - ba[1], bb[3] - bb[1]))
                h_max = max(1e-6, max(ba[3] - ba[1], bb[3] - bb[1]))
                if oy < 0.5 * h_min:
                    continue                      # 上下相叠的两条公式
                # 还要「同一水平带」：左右两半的纵向中心几乎重合；上下相叠的
                # 两个簇中心会明显错开（实测 latex_sample p3 分母 `2` 与下面那条
                # `Σ_{k=0}^∞ ar^k` 的 `∫`：重叠 0.51、但中心差 3.4pt = 0.49 个高度）。
                if abs((ba[1] + ba[3]) / 2 - (bb[1] + bb[3]) / 2) > 0.35 * h_max:
                    continue

                if rank[ra] < rank[rb]:
                    ra, rb = rb, ra
                parent[rb] = ra
                if rank[ra] == rank[rb]:
                    rank[ra] += 1
                line_sets[ra] |= line_sets[rb]
                line_sets[rb] = set()
                eq_sets[ra] |= eq_sets[rb]
                eq_sets[rb] = set()
                comp_bbox[ra] = union_bbox(ba, bb)
                changed = True

        if not changed:
            break

    # ------------------------------------------------------------
    # 6. 生成合并后的公式框，并写回 blocks
    # ------------------------------------------------------------
    def dominant_baseline(indices):
        """簇内碎片按基线聚类，返回碎片最多的那一组的平均基线。

        并列时取字号更大的一组（大符号的主基线比上下限更可信）。
        """
        buckets = []  # [y_sum, count, max_size]
        for k in indices:
            y = formula_frags[k]["origin"][1]
            size = formula_frags[k]["size"]
            for b in buckets:
                if abs(y - b[0] / b[1]) <= 1.0:
                    b[0] += y
                    b[1] += 1
                    b[2] = max(b[2], size)
                    break
            else:
                buckets.append([y, 1, size])

        y_sum, count, _ = max(buckets, key=lambda b: (b[1], b[2]))
        return y_sum / count

    def glued_to_body_text(bbox, size, indices):
        """整簇是否与「同基线、同栏」的正文紧贴（间隔 ≤ 一个空格）。

        只认与簇主基线重合的正文，所以上一行/下一行的正文不会把独立公式
        判成行内公式；只认单个正文片段（而不是整行合并后的 bbox），所以
        双栏页里另一栏的正文不会被当成「贴着」。

        正文必须落在簇的**左右外侧**。被簇的 bbox 横向罩住的正文片段是
        「跨行簇里夹着的正文」（如 `MultiHead(…) = Concat(…)` 与下一行
        `headᵢ = Attention(…)` 被并成一簇，中间那条正文行落在簇内部），
        不能算「贴着」——那是独立公式的特征。
        """
        baseline = dominant_baseline(indices)
        tol = max(1.2, 0.15 * size)
        space = max(3.0, 0.6 * size)

        for rec in body_for_line:
            if abs(rec["origin"][1] - baseline) > tol:
                continue

            rb = rec["bbox"]
            if rb[2] <= bbox[0]:
                gx = bbox[0] - rb[2]
            elif rb[0] >= bbox[2]:
                gx = rb[0] - bbox[2]
            else:
                continue  # 正文横跨在簇内部，不是「相邻正文」

            if gx <= space:
                return True

        return False

    clusters = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    raw_boxes = []

    for indices in clusters.values():
        indices = sorted(indices)

        bbox = formula_frags[indices[0]]["bbox"]
        for k in indices[1:]:
            bbox = union_bbox(bbox, formula_frags[k]["bbox"])

        source_blocks = sorted({
            formula_frags[k]["block"] for k in indices
        })

        inline_count = sum(
            1 for k in indices if formula_frags[k]["role"] == "inline"
        )
        is_inline = inline_count * 2 >= len(indices)

        # 碎片级投票有两个系统性偏差，会把行内公式判成独立公式：
        #   1. 跨栏误配：碎片纵向压到**另一栏**的正文行上时，body_gap 会
        #      被算成十几点，于是被判成「离正文很远」的 display；
        #   2. 簇中段碎片天生离正文很远（左右都隔着别的公式碎片），真正
        #      贴着正文的只有簇的两端。
        # 实测：双栏第 3 页 `‖v‖₂ = √∑ᵢ vᵢ²`（行首公式 + 右侧正文）里
        # `‖ v ‖ = 2` 五个碎片都配到了左栏正文行、`√∑`/`n` 也因 origin 判定
        # 落到远处，12 个碎片里 10 个成了 display，整簇被投成独立公式。
        # 因此这里再用「整簇」验一次：主基线与某条正文行重合、且簇边缘离
        # 该行正文不超过一个空格 → 就是同一视觉行里的行内公式。
        if not is_inline:
            max_size = max(formula_frags[k]["size"] for k in indices)
            if glued_to_body_text(bbox, max_size, indices):
                is_inline = True

        raw_boxes.append({
            "id": None,
            "bbox": bbox,
            "text": "".join(formula_frags[k]["text"] for k in indices),
            "role": "inline" if is_inline else "display",
            "inline": is_inline,
            "fragment_indices": indices,
            "fragment_count": len(indices),
            # 每个碎片的 bbox：裁剪时用来区分「本公式的墨迹」与
            # 「相邻公式伸进框里的碎片」（如分式分母挂到下一行时，
            # 下一行 ∑ 的上半截会一起进框）。
            "fragment_bboxes": [
                tuple(float(v) for v in formula_frags[k]["bbox"])
                for k in indices
            ],
            "source_ids": [
                (
                    formula_frags[k]["block"],
                    formula_frags[k]["line"],
                    formula_frags[k]["span"],
                )
                for k in indices
            ],
            "source_blocks": source_blocks,
        })

    # 按阅读顺序排序：先上后下，再左后右
    raw_boxes.sort(key=lambda box: (round(box["bbox"][1], 1), box["bbox"][0]))

    for cid, box in enumerate(raw_boxes, start=1):
        box["id"] = cid

        for idx in box["fragment_indices"]:
            formula_frags[idx]["span_ref"]["formula_box_id"] = cid

    for block in blocks:
        block["formula_boxes"] = []

    for box in raw_boxes:
        for bi in box["source_blocks"]:
            blocks[bi]["formula_boxes"].append(box)

    return blocks


def limit_inline_formula_box_height(
    blocks: list,
    *,
    body_line_tol: float = 1.6,
    min_height: float = 0.5,
    min_width: float = 1.0,
):
    """
    限制行内公式框的纵向范围，避免分式/根号/上下标把公式框撑到相邻正文行上，
    同时把左右边界限制在同行正文的外侧。

    规则（PDF 坐标，y 轴向下）：
      - 公式框的顶部最高到上一行正文的底部：new_y0 >= 上一行的 bbox[3]
        （∑/∫/√ 的 bbox 顶部是字体框外扩，收边不伤墨迹）；
      - 公式框的底部只在「小的字形外溢」时收到下一行正文顶部
        （≤ max(1.5, 0.1*框高)）；大溢出视为公式自身内容，保留原框；
      - 左边框不小于同行正文最右侧字符的右边缘：new_x0 >= 左侧文字 bbox[2]；
      - 右边框不大于同行正文最左侧字符的左边缘：new_x1 <= 右侧文字 bbox[0]。

    「上一行 / 下一行」只在**与公式框同栏**（水平方向相接或重叠）的正文行里找，
    双栏排版时不会被另一栏的行限制；找不到某一侧时该侧不做限制。

    约束对所有行内公式一致（含 ∑/∫/√ 等大运算符）：
      - 顶部严格取上一行正文的**实际底部**（含 q/g 等字母的降部），
        不因公式含大运算符而放宽；
      - 底部取下一行正文的实际顶部。

    上一行 / 下一行以**同行的正文行为锚点**定位：纵向跨过公式框中心的那一行
    就是同行文字，它不会被当成上一行或下一行；上一行取它上方最近的正文行，
    下一行取它下方最近的正文行。

    只处理 role == "inline" 的公式框（display 公式本来就允许跨行），
    且只收窄、不放大。传入的 blocks 不会被修改，返回深拷贝后的 blocks；
    bbox 会同步到所有引用同一个 id 的 block。

    每个被处理的框还会写入 ``crop_clearance``（left, top, right, bottom），
    表示框边到最近同行/相邻行正文的距离。裁剪时可用它限制 padding，
    避免外扩后把相邻正文再带进公式图里；无相邻正文的一侧为 None。
    """
    blocks = deepcopy(blocks)

    def is_formula_font(font: str) -> bool:
        return bool(font) and bool(_FORMULA_FONT_RE.search(font))

    def union_bbox(a, b):
        return (
            min(a[0], b[0]),
            min(a[1], b[1]),
            max(a[2], b[2]),
            max(a[3], b[3]),
        )

    def span_ink_left(rec):
        """span 内首个实际字符的左边缘（去掉前导空格）。"""
        text = rec["text"]
        n_space = len(text) - len(text.lstrip())
        return rec["bbox"][0] + n_space * 0.25 * rec["size"]

    def span_ink_right(rec):
        """span 内末个实际字符的右边缘（去掉尾部空格）。"""
        text = rec["text"]
        n_space = len(text) - len(text.rstrip())
        return rec["bbox"][2] - n_space * 0.25 * rec["size"]

    # ------------------------------------------------------------
    # 1. 收集正文片段与公式片段
    # ------------------------------------------------------------
    # 正文行只看非公式字体的 span（与 merge_formula_boxes 的口径一致）；
    # 公式 span 按 formula_box_id 归组，用来把框的左右边界收到实际字符。
    body_spans = []
    box_spans = defaultdict(list)

    for block in blocks:
        if block.get("type", 0) != 0:
            continue

        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue

                bbox = tuple(float(v) for v in span["bbox"])
                origin = span.get("origin", ((bbox[0] + bbox[2]) / 2.0, bbox[3]))

                rec = {
                    "bbox": bbox,
                    "y": float(origin[1]),
                    "size": float(span.get("size", bbox[3] - bbox[1])),
                    "text": text,
                }

                box_id = span.get("formula_box_id")
                if box_id is not None:
                    box_spans[box_id].append(rec)
                    continue
                if is_formula_font(span.get("font", "")):
                    # 没进任何公式框的公式字体碎片，不算正文
                    continue
                if _EQUATION_NO_RE.fullmatch(text.strip()):
                    # 公式编号 (1) / [12] 不参与正文行
                    continue

                body_spans.append(rec)

    # ------------------------------------------------------------
    # 2. 逐个行内公式框计算上下左右边界
    # ------------------------------------------------------------
    limited_info = {}
    seen_ids = set()

    for block in blocks:
        for box in block.get("formula_boxes", []):
            box_id = box.get("id")
            if box_id is None:
                continue
            if box_id in seen_ids:
                continue
            seen_ids.add(box_id)

            if box.get("role") != "inline" and not box.get("inline"):
                continue

            bbox = tuple(float(v) for v in box["bbox"])
            x0, y0, x1, y1 = bbox
            if x1 <= x0 or y1 <= y0:
                continue

            # 公式 span 往往带前导/尾部空格（如 ' |r| < 1'）。先把左右边界
            # 收到实际字符，否则 bbox 里的空格会让 padding 又贴回正文。
            spans = box_spans.get(box_id, [])
            base_x0, base_x1 = x0, x1
            if spans:
                left_span = min(spans, key=lambda s: s["bbox"][0])
                if (
                    left_span["text"][:1].isspace()
                    and left_span["bbox"][0] <= x0 + 1.0
                ):
                    edge = span_ink_left(left_span)
                    if x0 < edge < x1:
                        base_x0 = edge

                right_span = max(spans, key=lambda s: s["bbox"][2])
                if (
                    right_span["text"][-1:].isspace()
                    and right_span["bbox"][2] >= x1 - 1.0
                ):
                    edge = span_ink_right(right_span)
                    if x0 < edge < x1:
                        base_x1 = edge

            if base_x1 - base_x0 < min_width:
                base_x0, base_x1 = x0, x1

            # 只保留与公式框同栏的正文片段（水平方向相接或重叠）。
            # 双栏排版时另一栏的正文离得很远，会被这条过滤掉。
            h_pad = 8.0
            local = [
                s
                for s in body_spans
                if s["bbox"][2] >= x0 - h_pad and s["bbox"][0] <= x1 + h_pad
            ]
            if not local:
                # 公式左右没有任何正文（例如独占一行、两侧都是空白）时，
                # 退回整页正文行，保证“所有行内公式”都能找到上下边界。
                local = body_spans
            if not local:
                continue

            # 按基线聚成正文行
            local.sort(key=lambda s: s["y"])
            lines = []
            for rec in local:
                target = None
                for ln in lines:
                    if abs(rec["y"] - ln["y"]) <= body_line_tol:
                        target = ln
                        break

                if target is None:
                    lines.append({
                        "ys": [rec["y"]],
                        "y": rec["y"],
                        "bbox": rec["bbox"],
                        "spans": [rec],
                    })
                else:
                    target["ys"].append(rec["y"])
                    target["y"] = sum(target["ys"]) / len(target["ys"])
                    target["bbox"] = union_bbox(target["bbox"], rec["bbox"])
                    target["spans"].append(rec)

            # 定位上一行/下一行：以「同行的文字」为锚点 —— bbox 纵向跨过
            # 公式框中心的那一行就是同行正文。它自身会被下面的两个条件
            # 同时排除，不会被误当成上一行或下一行；这样做也不依赖公式
            # span 的基线（merge_chars 合并后 origin/size 会失真）。
            box_cy = (y0 + y1) / 2.0
            above = [ln for ln in lines if ln["bbox"][3] <= box_cy]
            below = [ln for ln in lines if ln["bbox"][1] >= box_cy]

            prev_line = (
                max(above, key=lambda ln: ln["bbox"][3]) if above else None
            )
            next_line = (
                min(below, key=lambda ln: ln["bbox"][1]) if below else None
            )

            new_y0, new_y1 = y0, y1
            if prev_line is not None:
                # 顶部严格不高于上一行正文的实际底部（含 q/g 等字母的降部）。
                # ∑/∫/√ 的 bbox 顶部是字体框外扩，不是真实墨迹，收边不会裁掉
                # 公式本体（实测 `‖v‖₂ = √∑ᵢ vᵢ²` 收边前后根号、∑ 上下限完全一致，
                # 只是去掉了上一行文字的降部残影）。
                new_y0 = max(new_y0, float(prev_line["bbox"][3]))
            if next_line is not None:
                # 底部只修掉小的字形毛刺（bbox 外溢几个点）；大溢出通常是
                # 公式自身内容（如 #33 的下限/下标），保留原框，避免裁内容。
                limit = float(next_line["bbox"][1])
                max_trim = max(1.5, 0.1 * (y1 - y0))
                if 0.0 < y1 - limit <= max_trim:
                    new_y1 = limit

            # 两侧同时夹逼可能把框压没（如公式框本身就跨越多行），
            # 这时保持原框，避免把公式主体裁掉。
            if new_y1 - new_y0 < min_height:
                new_y0, new_y1 = y0, y1

            # ---- 左右边界：限制在同行正文的外侧 ----
            # “同行”仍然以 bbox 跨过公式框中心的正文行为准；
            # 左侧取这些行里最靠右的字符右边缘，右侧取最靠左的字符左边缘。
            box_cx = (base_x0 + base_x1) / 2.0
            same_row_spans = [
                s
                for ln in lines
                if ln["bbox"][1] <= box_cy <= ln["bbox"][3]
                for s in ln["spans"]
            ]
            left_edge = max(
                (
                    span_ink_right(s)
                    for s in same_row_spans
                    if span_ink_right(s) <= box_cx
                ),
                default=None,
            )
            right_edge = min(
                (
                    span_ink_left(s)
                    for s in same_row_spans
                    if span_ink_left(s) >= box_cx
                ),
                default=None,
            )

            new_x0, new_x1 = base_x0, base_x1
            if left_edge is not None:
                new_x0 = max(new_x0, left_edge)
            if right_edge is not None:
                new_x1 = min(new_x1, right_edge)
            if new_x1 - new_x0 < min_width:
                # 两侧同时夹逼会把框压没（如框本身跨在文字上），保持原框
                new_x0, new_x1 = x0, x1

            # 记录四边到相邻正文的空隙，供裁剪阶段限制 padding：
            # 否则 padding 会把已经贴边的同行文字又带进公式图。
            clearance = (
                None if left_edge is None else max(0.0, new_x0 - left_edge),
                (
                    None
                    if prev_line is None
                    else max(0.0, new_y0 - float(prev_line["bbox"][3]))
                ),
                None if right_edge is None else max(0.0, right_edge - new_x1),
                (
                    None
                    if next_line is None
                    else max(0.0, float(next_line["bbox"][1]) - new_y1)
                ),
            )

            limited_info[box_id] = {
                "bbox": (new_x0, new_y0, new_x1, new_y1),
                "clearance": clearance,
            }

    # ------------------------------------------------------------
    # 3. 写回（同一个 id 的框可能挂在多个 block 上）
    # ------------------------------------------------------------
    if limited_info:
        for block in blocks:
            for box in block.get("formula_boxes", []):
                info = limited_info.get(box.get("id"))
                if info is None:
                    continue
                box["bbox"] = info["bbox"]
                box["crop_clearance"] = info["clearance"]
                box["crop_clearance_bbox"] = info["bbox"]

    return blocks


def collect_formula_boxes(blocks: list) -> list:
    """
    从 merge_formula_boxes() 返回的 blocks 中收集公式框并去重。
    跨 block 的公式框只会返回一次。
    """
    seen = set()
    result = []

    for block in blocks:
        for box in block.get("formula_boxes", []):
            if box["id"] in seen:
                continue
            seen.add(box["id"])
            result.append(box)

    result.sort(key=lambda box: (box["bbox"][1], box["bbox"][0]))
    return result