"""竖排/倒排（旋转）文字的解析回归测试。

背景：PDF 里旋转 90° 的页边水印（arXiv 侧边章）在 PyMuPDF 里是
`dir=(0,-1)`、bbox 为「窄而高」的竖条、每个字的 x 完全相同、origin 在竖条**底部**。
旧实现把它当横排行处理 → 行宽只有 1pt、文字横向溢出压住正文、还会被句号切成两块。

本测试不加载任何模型（直接调 `_local_text_blocks`），覆盖 rot = -90 / 90 / 180 与横排。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.pdf_parser.backend.backend_pymupdf import (  # noqa: E402
    _build_text_items, _local_text_blocks)
from backend.pdf_parser.text_layer import _line_rot  # noqa: E402

TEXT = "ABC def"
STAMP = "arXiv:2002.08245v2  [q-fin.CP]  4 Apr 2020"


def _make_pdf(tmp_path: Path) -> Path:
    import pymupdf

    path = tmp_path / "rot.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    for i, rot in enumerate((0, 90, 180, 270)):
        page.insert_text((100 + i * 100, 300), TEXT, fontsize=12,
                         fontname="tiro", rotate=rot)
    # 侧边章：带句号，用来验证「不被按句切碎」
    page.insert_text((20, 560), STAMP, fontsize=20, fontname="tiro", rotate=90)
    doc.save(str(path))
    doc.close()
    return path


def _close(a, b, tol=0.6):
    return abs(float(a) - float(b)) <= tol


def test_line_rot_math():
    """`_line_rot` 的四个象限：ox/oy 必须落在页面上的正确角。"""
    bbox = [187.36, 257.0, 203.37, 300.0]          # 竖排（自下而上）
    got = _line_rot({"dir": (0.0, -1.0)}, bbox)
    assert got["rot"] == -90
    assert _close(got["ox"], 187.36) and _close(got["oy"], 300.0)
    assert _close(got["rw"], 43.0) and _close(got["rh"], 16.01)

    got = _line_rot({"dir": (-1.0, 0.0)}, [257.0, 296.63, 300.0, 312.64])
    assert got["rot"] == 180
    assert _close(got["ox"], 300.0) and _close(got["oy"], 312.64)
    assert _close(got["rw"], 43.0) and _close(got["rh"], 16.01)

    got = _line_rot({"dir": (0.0, 1.0)}, [396.63, 300.0, 412.64, 343.0])
    assert got["rot"] == 90
    assert _close(got["ox"], 412.64) and _close(got["oy"], 300.0)
    assert _close(got["rw"], 43.0) and _close(got["rh"], 16.01)

    # 横排 / 斜排 / 缺 dir：都不算旋转行
    assert _line_rot({"dir": (1.0, 0.0)}, bbox) is None
    assert _line_rot({"dir": (0.7, 0.7)}, bbox) is None
    assert _line_rot({}, bbox) is None


def test_rotated_lines_keep_geometry(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    path = _make_pdf(tmp_path)
    doc = pymupdf.open(str(path))
    try:
        texts, _stats = _local_text_blocks(doc[0], [], page_no=1,
                                           table_rects=[], image_rects=[],
                                           box_map={})
    finally:
        doc.close()

    # 短文本（ABC def）本身没有句号，会各自成块；水印必须保持 1 块 + 全文
    stamps = [t for t in texts if STAMP in (t.get("text") or "")]
    assert len(stamps) == 1, [t.get("text") for t in texts]
    st = stamps[0]
    assert st.get("vertical") is True
    ln = st["lines"][0]
    assert ln.get("rot") == -90
    # 行宽 / 块宽必须是「字高」，不是被 clamp 出来的 1pt
    assert ln["w"] > 20.0
    assert _close(ln["rw"], ln["h"], 1.0)     # 未旋转时的行宽 = 竖条高度
    assert _close(ln["rh"], ln["w"], 1.0)     # 未旋转时的高 = 竖条宽度
    assert _close(ln["oy"], ln["y"] + ln["h"], 1.0)   # 局部左上角在竖条底部
    assert _close(ln["ox"], ln["x"], 1.0)

    rots = sorted(ln.get("rot") for t in texts for ln in t["lines"] if ln.get("rot"))
    # 三条短文本（-90 / 180 / 90）+ 侧边章（-90）；横排那条没有 rot
    assert rots == [-90, -90, 90, 180], rots


def test_rotated_block_is_not_sentence_split(tmp_path):
    """带句号的竖排文本不能被按句切成两块（旧实现切成 `…[q-fin.` + `CP] …`）。"""
    pymupdf = pytest.importorskip("pymupdf")
    path = _make_pdf(tmp_path)
    doc = pymupdf.open(str(path))
    try:
        texts, stats = _local_text_blocks(doc[0], [], page_no=1,
                                          table_rects=[], image_rects=[],
                                          box_map={})
    finally:
        doc.close()
    stamps = [t for t in texts if "arXiv:2002.08245v2" in (t.get("text") or "")]
    assert len(stamps) == 1
    assert stamps[0]["text"] == STAMP
    assert stats.get("sentence_line_splits", 0) == 0


def test_build_text_items_marks_vertical(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    path = _make_pdf(tmp_path)
    doc = pymupdf.open(str(path))
    try:
        items, _copy = _build_text_items({"blocks": []}, 1, {}, [], [], page=doc[0])
    finally:
        doc.close()
    vert = [i for i in items if i.get("vertical")]
    assert len(vert) == 4                     # 三条短旋转文本 + 侧边章
    stamps = [i for i in vert if "arXiv:2002.08245v2" in (i.get("text") or "")]
    assert len(stamps) == 1
    assert stamps[0]["lines"][0]["rot"] == -90
    # 透传到前端的关键字段
    for k in ("rot", "ox", "oy", "rw", "rh"):
        assert k in stamps[0]["lines"][0]
