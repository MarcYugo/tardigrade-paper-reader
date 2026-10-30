"""公式框合并（`merge_modules.merge_blocks`）的回归测试。

背景（2026-10-29）：`merge_formula_boxes` 的「一个簇不跨两条正文行」闸门用
`body_line` 判，而 `body_line` 是按**跨栏并集的正文行**算出来的，碎片 bbox 上下
差 0.8~4pt（下标、CMEX 大括号）就可能落到隔壁那条行上 → 同一条公式的左右两半
连候选边都生成不出来。实测 AutoAlpha 第 5 页 `…log(S_T/S_0)}−1` 的尾段 `0)}`
因此自成一框、且 98% 落在大框里，前端渲染成「大框里套一张裁图小框」。

修法：新增 `same_row_touching()` —— 只对**行内**公式、**基线几乎一致**
（≤0.15 字号）且**横向几乎贴着**（≤0.3 字号）的碎片绕过该闸门（预合并一遍）。

这里的用例守的是修复的**风险方向**：判据一放宽，竖直相叠的两条独立公式就会被
焊成一条（实测 latex_sample 第 2 页 `lim(…)/h` 与 `∫ᵃᵇf(x)dx` 的基线只差
3.38pt、横向只差 3.74pt）。目标用例（AutoAlpha 第 5 页 `0)}` 并回大框）在真实
PDF 上验证：`merge_formula_boxes` 输出 42 → 41 个框、三份文档「嵌套框 = 0」，
而 latex_sample 与 Looped_Flows 的框**逐个一致**；那份 PDF 不在仓库里，故不写进
测试。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = ROOT / "backend" / "pdf_parser" / "local_parse_pipeline"
SAMPLE_PDF = ROOT / "samples" / "latex_sample.pdf"

if str(LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(LOCAL_DIR))

from merge_modules.merge_blocks import (  # noqa: E402
    limit_inline_formula_box_height,
    merge_formula_boxes,
)


def _span(x0, y0, x1, y1, text, font="CMMI10", size=9.96):
    return {"size": size, "flags": 4, "font": font, "text": text,
            "origin": [x0, y1], "bbox": [x0, y0, x1, y1]}


def _line(spans):
    return {
        "bbox": [min(s["bbox"][0] for s in spans), min(s["bbox"][1] for s in spans),
                 max(s["bbox"][2] for s in spans), max(s["bbox"][3] for s in spans)],
        "spans": spans,
    }


def _boxes_of(blocks):
    seen: dict = {}
    for blk in limit_inline_formula_box_height(blocks=merge_formula_boxes(blocks=blocks)):
        for box in blk.get("formula_boxes") or []:
            seen.setdefault(box["id"], box)
    return list(seen.values())


def _page_boxes(page_no: int):
    import pymupdf

    doc = pymupdf.open(str(SAMPLE_PDF))
    try:
        page = doc.load_page(page_no)
        return _boxes_of(page.get_text("dict")["blocks"])
    finally:
        doc.close()


def test_single_row_of_fragments_stays_separate_without_body_line():
    """没有正文行可归（body_line=None）时，逐个碎片保持独立 —— 略过闸门的宽松合并
    只针对「行内 + 同一基线 + 横向紧贴」，不会把任意相邻碎片焊成一团。

    这条守的是 `same_row_touching()` 的 role 条件：这里所有碎片都是独立公式
    （display），走既有规则，不能被新增的宽松边连起来。
    """
    formula = _line([
        _span(249.56, 424.14, 262.22, 435.60, " /S", "CMMI10", 9.96),
        _span(262.22, 427.97, 266.20, 434.95, "0", "CMR7", 6.97),
        _span(266.69, 424.14, 270.57, 434.11, ")", "CMR10", 9.96),
    ])
    boxes = _boxes_of([{"type": 0, "number": 0, "lines": [formula]}])
    texts = "".join(str(b.get("text") or "") for b in boxes)
    assert "0" in texts and ")" in texts        # 碎片一个都没丢
    assert all(b.get("role") != "inline" for b in boxes)


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="缺少 samples/latex_sample.pdf")
def test_stacked_display_formulas_stay_separate():
    """上下相叠的两条独立公式不能被「同视觉行」规则焊成一条。

    第 2 页实测：`f'(x)=lim_{h→0}(f(x+h)−f(x))/h`（y 522.9~538.3）与
    `∫_a^b f(x) dx`（y 535.3~553.2）bbox 只重叠 3pt，而分母 `h` 的基线
    （538.3）只比 `∫` 的基线（542.1）低 3.38pt、横向只差 3.74pt ——
    判据放松到 0.35 字号就会把两条公式焊成一条。
    """
    texts = [str(b.get("text") or "") for b in _page_boxes(1)]
    assert any("lim" in t for t in texts)
    assert any("dx" in t for t in texts)
    assert not [t for t in texts if "lim" in t and "dx" in t]
