"""表格单元格文字回填测试（不加载 SLANet/版面模型，用桩引擎复用 out_samples 的结构结果）。

背景：`SLANet_plus` 只输出表格**结构**（词表里没有文字 token），
`<td></td>` 天生是空的 —— 之前前端表格没有数值就是这个原因。
`local_parse_pipeline.extract_tables` 现在用 PDF 文字层按单元格四边形回填，
这里覆盖：
  1. 单元格坐标的三种形态（扁平 8 元组 / 点对 / numpy 风格）；
  2. 文本按顺序填进空 `<td>`，非空 `<td>` 不覆盖，HTML 转义 + 多行 `<br>`；
  3. 端到端（`samples/latex_sample.pdf` 第 4 页的 3 张表）每个单元格都有文字。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = ROOT / "backend" / "pdf_parser" / "local_parse_pipeline"
SAMPLE = LOCAL_DIR / "out_samples" / "latex_sample.json"
SAMPLE_PDF = ROOT / "samples" / "latex_sample.pdf"

# 流水线里的模块用的是 `from utils.crop_op import …` 这种顶层导入
if str(LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(LOCAL_DIR))

from extract_tables import (  # noqa: E402
    _cell_points,
    _words_to_lines,
    extract_cell_texts,
    fill_table_html,
    prune_empty_rows,
    table_font_size,
)


class _NpLike:
    """模拟 numpy 数组（只有 `tolist` 的序列）。"""

    def __init__(self, data):
        self._data = data

    def tolist(self):
        return self._data


def test_cell_points_shapes():
    assert _cell_points([1, 2, 3, 4, 5, 6, 7, 8]) == [
        (1.0, 2.0), (3.0, 4.0), (5.0, 6.0), (7.0, 8.0)]
    assert _cell_points([[1, 2], [3, 4], [5, 6], [7, 8]]) == [
        (1.0, 2.0), (3.0, 4.0), (5.0, 6.0), (7.0, 8.0)]
    assert _cell_points(_NpLike([_NpLike([1, 2]), _NpLike([3, 4])])) == [
        (1.0, 2.0), (3.0, 4.0)]
    assert _cell_points([]) == []
    assert _cell_points(None) == []


def test_fill_table_html_in_order_and_escape():
    html = ("<table><tr><td></td><td></td></tr>"
            "<tr><td></td><td rowspan=\"2\"></td></tr></table>")
    texts = ["Method", "Acc.\nF1", "A < B", ""]
    out = fill_table_html(html, texts)
    assert "<td>Method</td>" in out
    assert "<td>Acc.<br>F1</td>" in out
    assert "<td>A &lt; B</td>" in out
    # 空文本不写（保持原样），属性不丢
    assert "<td rowspan=\"2\"></td>" in out


def test_fill_table_html_keeps_existing_content():
    html = "<table><tr><td>已有</td><td></td></tr></table>"
    out = fill_table_html(html, ["新", "补"])
    assert "<td>已有</td>" in out
    assert "<td>补</td>" in out


def test_prune_empty_rows_drops_fake_header_row():
    """SLANet 对无竖线小表会多吐一条空表头行（latex_sample Table 3 实测）。

    空行留着会让前端把「表头线」画在它下面（顶线下再出一条线），真正的表头
    也没下划线/不加粗。
    """
    html = ("<html><body><table>"
            "<tr><td></td></tr>"
            "<tr><td>Item Value</td></tr>"
            "<tr><td>A 1.23</td></tr>"
            "<tr><td></td></tr>"
            "</table></body></html>")
    out = prune_empty_rows(html)
    assert "<tr><td></td></tr>" not in out
    assert out.count("<tr") == 2
    assert "<td>Item Value</td>" in out
    # 首尾以外的结构一律不动
    assert out.startswith("<html><body><table>")
    assert out.endswith("</table></body></html>")


def test_prune_empty_rows_keeps_middle_and_all_empty():
    # 中间的空行可能是 rowspan 占位，不能删
    html = ("<table><tr><td>a</td></tr>"
            "<tr><td></td></tr>"
            "<tr><td>b</td></tr></table>")
    assert prune_empty_rows(html) == html
    # 整表都没回填到文字 → 原样返回（不要删成空表）
    empty = "<table><tr><td></td><td></td></tr><tr><td></td><td></td></tr></table>"
    assert prune_empty_rows(empty) == empty
    # 没有表格行 / 空串：直接返回
    assert prune_empty_rows("") == ""
    assert prune_empty_rows("<table></table>") == "<table></table>"


class _Det:
    label = "Table"

    def __init__(self, bbox, score):
        self.bbox = tuple(bbox)
        self.score = score


class _DetectEngine:
    def __init__(self, tables):
        self._tables = tables

    def predict_page(self, page, dpi=200, **kwargs):
        return [_Det(t["bbox"], t["det_score"]) for t in self._tables]


class _TransferEngine:
    """回放 out_samples 里 SLANet 的结构结果（含 cells 四边形）。"""

    def __init__(self, tables):
        self._tables = tables
        self._i = 0

    def predict_one(self, image):
        table = self._tables[self._i]
        self._i += 1
        return dict(table["html_table"])


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="缺少 samples/latex_sample.pdf")
def test_sample_tables_get_cell_text():
    pymupdf = pytest.importorskip("pymupdf")
    pytest.importorskip("PIL")
    from extract_tables import extract_tables

    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    page_tables = raw["pages"]["3"]["contents"]["tables"]
    assert page_tables, "样例第 4 页应当有表格"

    doc = pymupdf.open(str(SAMPLE_PDF))
    try:
        page = doc.load_page(3)
        tables = extract_tables(page, _DetectEngine(page_tables),
                                _TransferEngine(page_tables), dpi=300)
    finally:
        doc.close()

    assert len(tables) == len(page_tables)
    expected = [
        ["Method", "Accuracy", "F1 Score", "Baseline", "0.85", "0.83",
         "Proposed", "0.92", "0.91"],
    ]
    cells = [re.findall(r"<td\b[^>]*>(.*?)</td>", t["html_table"]["html"],
                        re.S | re.I) for t in tables]
    # 每个单元格都要有文字（结构模型本身只给骨架）
    assert all(x.strip() for group in cells for x in group)
    assert cells[0] == expected[0]
    assert cells[1][0] == "Method"
    assert cells[2][:4] == ["Item", "Value", "A", "1.23"]
    # 表格字号由 PDF 文字层量出（样例是 10pt 正文号），前端用它当 font-size
    assert tables[0]["html_table"]["size"] == pytest.approx(9.96, abs=0.05)


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="缺少 samples/latex_sample.pdf")
def test_extract_cell_texts_same_line_merged():
    """同一视觉行的两段（`0.82` 与 `0.80`）必须拼成一行，不能竖成两行。

    PyMuPDF 的 block/line 号会把表格里隔得远的词分成不同 line；按 line 号分组会
    让单元格变成两行文本、整张表比 PDF 框高出一截。
    """
    pymupdf = pytest.importorskip("pymupdf")
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    table = raw["pages"]["3"]["contents"]["tables"][1]
    cells = table["html_table"]["cells"]

    doc = pymupdf.open(str(SAMPLE_PDF))
    try:
        page = doc.load_page(3)
        texts = extract_cell_texts(page, table["bbox"], cells, 300)
    finally:
        doc.close()

    assert len(texts) == len(cells)
    assert texts[1] == "Acc. F1"        # 表头两个词同一行
    assert texts[3] == "0.82 0.80"
    assert all("\n" not in t for t in texts)


def test_words_to_lines_by_y_proximity():
    # (x0, y0, x1, y1, word, block, line, word_no)：block/line 号故意写成不同
    words = [
        (10, 100, 30, 110, "0.82", 1, 1, 0),
        (60, 100.5, 80, 110.5, "0.80", 2, 1, 0),   # 同一视觉行
        (10, 130, 30, 140, "next", 1, 2, 0),       # 下一行
    ]
    assert _words_to_lines(words) == ["0.82 0.80", "next"]
    assert _words_to_lines([]) == []


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="缺少 samples/latex_sample.pdf")
def test_table_font_size_from_text_layer():
    pymupdf = pytest.importorskip("pymupdf")
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    table = raw["pages"]["3"]["contents"]["tables"][0]
    doc = pymupdf.open(str(SAMPLE_PDF))
    try:
        page = doc.load_page(3)
        size = table_font_size(page, table["bbox"])
        empty = table_font_size(page, (0, 0, 5, 5))
    finally:
        doc.close()
    assert size == pytest.approx(9.96, abs=0.05)
    assert empty == 0.0
