"""表格检测 + 结构识别 + **单元格文字回填**。

链路：
  1. 版面分析（OXR 的 D-FINE，见 ``utils/load_oxr_layout.py``）在整页上检测出
     ``Table`` / ``Picture`` 等区域框（PDF 点坐标）；本模块既可以直接调用
     ``detect_engine.predict_page(page, dpi)``，也可以用 ``detections=`` 吃到
     已经跑过的版面结果（避免重复推理）；
  2. 按框裁图给 ``SLANet_plus`` 识别**表格结构** —— 它只输出 ``<td>`` 的
     ``colspan`` / ``rowspan`` 骨架，词表里**没有任何文字 token**
     （见 ``models/SLANet_plus/config.json`` 的 ``character_dict``），
     所以结构结果里的每个 ``<td></td>`` 天生是空的；
  3. 单元格文字由本模块用 **PyMuPDF 文字层**回填：``SLANet_plus`` 同时给出
     每个 ``<td>`` 的四边形（裁剪图像的像素坐标，顺序与 HTML 里 ``<td>`` 一致），
     换算成页面点坐标后，把词中心落在该四边形内的词按阅读顺序拼进单元格。

⚠️ 没有文字层的扫描件仍然只能得到空表格（要文字得另外接 OCR）。
"""

import html as _html
import re
from typing import Any

import pymupdf
from utils.crop_op import crop_from_page, image_encode_base64

# 裁剪给 SLANet 的图是 `crop_from_page(...)` 生成的：默认 padding 2pt、四边再补 6px 白边。
# 单元格四边形是**那张图**的像素坐标，换算回页面点坐标时这两项都要减掉。
_CELL_CROP_PADDING = 2.0
_CELL_CROP_MARGIN = 6

_TD_RE = re.compile(r"(<td\b[^>]*>)(.*?)(</td>)", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TCELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _cell_points(cell: Any) -> list[tuple[float, float]]:
    """把 ``SLANet`` 的单元格坐标统一成 ``[(x, y), ...]``。

    可能是 8 个数的扁平序列（``x0,y0,x1,y1,...``），也可能是 ``[[x, y], ...]``，
    还可能带着 numpy 类型（Paddle 直接返回 ndarray 时）。
    """
    if hasattr(cell, "tolist"):
        cell = cell.tolist()
    if not isinstance(cell, (list, tuple)) or not cell:
        return []
    if isinstance(cell[0], (list, tuple)) or hasattr(cell[0], "tolist"):
        pts: list[tuple[float, float]] = []
        for p in cell:
            if hasattr(p, "tolist"):
                p = p.tolist()
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                pts.append((float(p[0]), float(p[1])))
        return pts
    if len(cell) >= 4 and len(cell) % 2 == 0:
        return [(float(cell[i]), float(cell[i + 1])) for i in range(0, len(cell), 2)]
    return []


def _table_crop_rect(page: pymupdf.Page, bbox) -> pymupdf.Rect:
    """与 ``crop_from_page`` 完全一致的裁剪矩形（点坐标）。"""
    rect = pymupdf.Rect(bbox)
    rect = pymupdf.Rect(
        rect.x0 - _CELL_CROP_PADDING,
        rect.y0 - _CELL_CROP_PADDING,
        rect.x1 + _CELL_CROP_PADDING,
        rect.y1 + _CELL_CROP_PADDING,
    )
    return rect & page.rect


def _cell_rect(cell: Any, crop_rect: pymupdf.Rect, dpi: float) -> pymupdf.Rect | None:
    """单元格四边形（裁剪图像素坐标）→ 页面点坐标的外接矩形。"""
    pts = _cell_points(cell)
    if not pts:
        return None
    scale = 72.0 / max(1.0, float(dpi))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return pymupdf.Rect(
        crop_rect.x0 + (min(xs) - _CELL_CROP_MARGIN) * scale,
        crop_rect.y0 + (min(ys) - _CELL_CROP_MARGIN) * scale,
        crop_rect.x0 + (max(xs) - _CELL_CROP_MARGIN) * scale,
        crop_rect.y0 + (max(ys) - _CELL_CROP_MARGIN) * scale,
    )


def extract_cell_texts(page: pymupdf.Page, bbox, cells, dpi: float) -> list[str]:
    """按单元格四边形从页面文字层取文本（顺序与 ``cells`` 一致）。

    判定用**词中心点**而不是 ``get_textbox``：SLANet 预测的单元格四边形相邻行
    会互相压一点（实测上下各溢 1~2pt），``get_textbox`` 会把隔壁行整行词带进来；
    词中心只会落在一个单元格里（重叠带里没有字形），再按 block/line/word 序号
    还原阅读顺序（每个单元格一个字符串，多行用 ``\\n`` 分隔；换行标签由
    :func:`fill_table_html` 在转义之后补，免得 ``<br>`` 被当成正文转义掉）。
    """
    crop_rect = _table_crop_rect(page, bbox)
    rects: list[pymupdf.Rect] = []
    for cell in cells or []:
        rect = _cell_rect(cell, crop_rect, dpi)
        if rect is None:
            return []
        rects.append(rect)
    if not rects:
        return []

    areas = [max(1e-6, r.width * r.height) for r in rects]
    buckets: list[list[tuple]] = [[] for _ in rects]
    try:
        words = page.get_text("words") or []
    except Exception:
        return ["" for _ in rects]

    for word in words:
        cx = (word[0] + word[2]) / 2.0
        cy = (word[1] + word[3]) / 2.0
        best, best_area = -1, 0.0
        for i, rect in enumerate(rects):
            if rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1:
                if best < 0 or areas[i] < best_area:
                    best, best_area = i, areas[i]
        if best >= 0:
            buckets[best].append(word)

    out: list[str] = []
    for bucket in buckets:
        out.append("\n".join(_words_to_lines(bucket)))
    return out


def _words_to_lines(words: list[tuple]) -> list[str]:
    """把同一单元格里的词按**视觉行**（而不是 block/line 号）拼成文本行。

    ``get_text("words")`` 的 block/line 号反映的是 PyMuPDF 的文本块划分：同一行
    表格里隔得远的两段（``0.82`` 与 ``0.80``）可能落在不同 block，直接按 block 号
    分组会把它们竖着排成两行，单元格高度翻倍、整表超出 PDF 框。这里按**词中心 y**
    的接近程度分行（容差 = 半个字高）。
    """
    if not words:
        return []
    heights = sorted(w[3] - w[1] for w in words)
    tol = max(1.5, 0.5 * heights[len(heights) // 2])
    rows: list[tuple[float, list[tuple]]] = []
    for word in sorted(words, key=lambda w: ((w[1] + w[3]) / 2.0, w[0])):
        cy = (word[1] + word[3]) / 2.0
        if not rows or abs(cy - rows[-1][0]) > tol:
            rows.append((cy, []))
        rows[-1][1].append(word)
    return [" ".join(str(w[4]) for w in sorted(row, key=lambda w: w[0]))
            for _cy, row in rows]


def table_font_size(page: pymupdf.Page, bbox) -> float:
    """表格区域内的实际字号（按字符数加权取众数），量不到返回 0。

    前端把表格字号当 ``font-size``：字号偏大时整张表会比 PDF 框高出一截，
    压到后面的正文（本地方案没有“块高增长”的向下避让）。
    """
    try:
        data = page.get_text("dict", clip=pymupdf.Rect(bbox)) or {}
    except Exception:
        return 0.0
    counter: dict[float, int] = {}
    for block in data.get("blocks") or []:
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                text = str(span.get("text") or "")
                if not text.strip():
                    continue
                try:
                    size = round(float(span.get("size") or 0.0), 2)
                except (TypeError, ValueError):
                    continue
                if size > 0:
                    counter[size] = counter.get(size, 0) + len(text)
    if not counter:
        return 0.0
    return max(counter.items(), key=lambda kv: kv[1])[0]


def fill_table_html(html: str, texts: list[str]) -> str:
    """把文本按顺序填进 ``<td>``；已经有内容的 ``<td>`` 不覆盖。"""
    if not html or not texts:
        return html
    state = {"i": 0}

    def repl(match: re.Match) -> str:
        i = state["i"]
        state["i"] = i + 1
        if i >= len(texts) or not texts[i]:
            return match.group(0)
        if match.group(2).strip():
            return match.group(0)
        # 先转义再补换行标签：反过来的话 `<br>` 会被 escape 成正文
        body = _html.escape(texts[i], quote=False).replace("\n", "<br>")
        return match.group(1) + body + match.group(3)

    return _TD_RE.sub(repl, html)


def _row_is_empty(row_inner: str) -> bool:
    """该 ``<tr>`` 的**所有**单元格都没有文字（没有单元格也算空）。"""
    cells = _TCELL_RE.findall(row_inner)
    if not cells:
        return True
    for cell in cells:
        if _TAG_RE.sub("", cell).replace("&nbsp;", " ").strip():
            return False
    return True


def prune_empty_rows(html: str) -> str:
    """剔除表格**首尾**整行全空的 ``<tr>``。

    ``SLANet_plus`` 对没有竖线的小表（三线表）常在裁剪图多出来的空白处多吐一条
    空行（实测 `latex_sample` 第 4 页 Table 3 多出一条空表头行）；文字回填是按
    单元格四边形找词，空行分不到任何词，于是留下 ``<tr><td></td></tr>``。这条
    空行会让前端把「表头线」画到它下面（顶线下 6~7px 又出现一条 1px 线、真正的
    表头既没有下划线也不加粗），还多占 3+3px padding 把整表撑高，触发前端的
    表格缩字号。

    只删**首尾**：中间的空行可能是 ``rowspan`` 合并留下的占位，删掉会错位；
    整表都没有文字（没回填到）时原样返回。
    """
    if not html:
        return html
    rows = list(_TR_RE.finditer(html))
    if not rows:
        return html
    flags = [_row_is_empty(m.group(1)) for m in rows]
    keep = [i for i, empty in enumerate(flags) if not empty]
    if not keep:                      # 一个字都没填进来 → 不动
        return html
    first, last = keep[0], keep[-1]
    if first == 0 and last == len(rows) - 1:
        return html
    out: list[str] = []
    pos = 0
    for i, m in enumerate(rows):
        out.append(html[pos:m.start()])
        if first <= i <= last:
            out.append(m.group(0))
        pos = m.end()
    out.append(html[pos:])
    return "".join(out)


def extract_tables(page: pymupdf, detect_engine: Any, transfer_engine: Any, dpi: int = 150,
                   detections: Any = None):
    """表格（结构 + 文字）与插图（裁图）提取。

    Args:
        page: PyMuPDF 页面。
        detect_engine: 版面检测引擎（需要 ``predict_page(page, dpi)``）；
            ``detections`` 已给定时可为 ``None``。
        transfer_engine: 表格结构识别模型（``SLANet_plus``）。
        dpi: 裁剪/渲染 dpi（也是「单元格四边形像素坐标」换算回点的基准）。
        detections: 可选的版面检测结果（元素需带 ``label`` / ``score`` /
            ``bbox``（PDF 点坐标）），给了就不再调用 ``detect_engine``。
    """
    if detections is not None:
        dets = list(detections)
    else:
        dets = detect_engine.predict_page(page=page, dpi=dpi)
    tables = []
    pictures = []
    for item in dets:
        if item.label == "Table":
            # dpi 必须与检测时一致：单元格四边形是这张裁剪图的像素坐标
            tab_pic = crop_from_page(page, bbox=item.bbox, dpi=dpi)
            # 增加将表格图片转化为html的模型
            html_item = transfer_engine.predict_one(tab_pic)
            # SLANet 只给结构（词表里没有文字），单元格文字用 PDF 文字层回填
            if isinstance(html_item, dict):
                cells = html_item.get("cells") or []
                html = html_item.get("html") or ""
                if html:
                    html_item["size"] = table_font_size(page, item.bbox)
                if cells and html:
                    texts = extract_cell_texts(page, item.bbox, cells, dpi)
                    html_item["html"] = fill_table_html(html, texts)
                # 首尾的空行是结构模型的假行（表头线会画到它下面、整表还会被撑高）
                if html_item.get("html"):
                    html_item["html"] = prune_empty_rows(html_item["html"])
            tables.append({
                "det_score": item.score,
                "label": "table",
                "bbox": item.bbox,
                "image": image_encode_base64(tab_pic),
                "html_table": html_item
            })
        elif item.label in ("Figure", "Picture"):
            fig = crop_from_page(page, bbox=item.bbox, dpi=dpi)
            item = {
                "bbox": item.bbox,
                "label": "picture",
                "dpi": dpi,
                "image": image_encode_base64(image=fig)
            }
            pictures.append(item)
    return tables, pictures