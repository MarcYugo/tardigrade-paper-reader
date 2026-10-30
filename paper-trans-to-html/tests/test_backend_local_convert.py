"""本地解析方案的输出转换测试（不加载模型，直接用 out_samples 里的流水线结果）。

覆盖 `backend_pymupdf` 的关键步骤：
  1. 跨 block 的 formula_boxes 去重时优先取“识别成功”的那份；
  2. texify 的 list 输出与 `$$…$$` 定界符被规整成可直接给 KaTeX 的 LaTeX；
  3. 公式字形在文字块里被替换成 `gap/gw` 占位，且 copy 区间指向正确的块；
  4. `bbox_norm` 是“归一化中心 xywh”。

正文字符流程（与 v11.3 的 PyMuPDF 后端一致）：
  5. 相邻 span 之间「有空隙没空格」→ 补一个**真空格**，整行合成一个正文 run；
  6. 按句子重切块（一个块 = 恰好一句）；
  7. 假上标（flags bit0）按“行正文基线”剔除；
  8. 行内公式占位落在「前一段正文」所在的那条行（不是字形最多的碎片行），
     宽度 = 真实空档；删过公式字形的行按剩下的 run 重算行左边/行宽。
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.pdf_parser import backend_pymupdf as bp

SAMPLE = (Path(__file__).resolve().parent.parent
          / "backend" / "pdf_parser" / "local_parse_pipeline"
          / "out_samples" / "latex_sample.json")


def _page_zero() -> dict:
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    return raw["pages"]["0"]["contents"]


def test_normalize_latex():
    assert bp._normalize_latex(["$$a^{2}+b^{2}=c^{2}$$"]) == "a^{2}+b^{2}=c^{2}"
    assert bp._normalize_latex([""]) == ""
    assert bp._normalize_latex(None) == ""


def test_same_row_fragments_join_one_block():
    """同一视觉行被 PyMuPDF 拆进两个 block 时，两块要归到**同一个句子单元**里。

    实测 latex_sample 第 2 页 “For sequences, the arithmetic series ∑… can be
    expressed as …” 就跨了 block。新版流程**不再合并视觉行**（行几何各自保留），
    而是在 `sentences.cut_blocks_by_sentence` 里按「同一视觉行 + 横向相接」把它们
    并成一句话一块 —— 前端 `splitSentences` 是按块切句的，块不并会出现半句碎片。
    """
    def line(x0, x1, base, text):
        return {
            "bbox": [x0, base - 8.0, x1, base + 2.0],
            "spans": [{
                "size": 10.0, "flags": 4, "font": "Times-Roman", "text": text,
                "origin": [x0, base],
                "bbox": [x0, base - 8.0, x1, base + 2.0],
            }],
        }

    page = {"blocks": [
        {"type": 0, "number": 0, "lines": [line(10, 100, 100, "left part")]},
        {"type": 0, "number": 1, "lines": [line(106, 200, 100, "right part")]},
    ]}
    texts, _copies = bp._build_text_items(page, 1, {}, [])
    assert len(texts) == 1                        # 两句碎片并成一句话一块
    rows = texts[0]["lines"]
    assert len(rows) == 2                         # 行几何各自保留（不合并视觉行）
    assert texts[0]["text"] == "left part right part"
    assert all(abs(ln["base"] - 100.0) < 0.6 for ln in rows)
    # 每行一个正文 run（公式占位除外），字符没有增删
    assert all(len(ln["runs"]) == 1 for ln in rows)
    assert [ln["runs"][0]["t"] for ln in rows] == ["left part", "right part"]


def test_gap_between_spans_becomes_real_space():
    """相邻 span 之间有视觉空隙、却没有空格字符 → 补一个**真空格**。

    表格把 `Method` / `Accuracy` 并进同一行最常见：PyMuPDF 按 span 切字，
    中间那 20pt 空隙在字符流里完全不存在，合并成一行后会粘成 `MethodAccuracy`。
    """
    def span(x0, x1, text):
        return {"size": 10.0, "flags": 4, "font": "Times-Roman", "text": text,
                "origin": [x0, 108.0], "bbox": [x0, 100.0, x1, 112.0]}

    line = {"bbox": [10.0, 100.0, 130.0, 112.0],
            "spans": [span(10, 50, "Method"), span(70, 120, "Accuracy")]}
    page = {"blocks": [{"type": 0, "number": 0, "lines": [line]}]}
    texts, _copies = bp._build_text_items(page, 1, {}, [])
    runs = texts[0]["lines"][0]["runs"]
    assert len(runs) == 1                      # 一行一个正文 run
    assert runs[0]["t"] == "Method Accuracy"   # 空隙变成了真空格
    assert texts[0]["text"] == "Method Accuracy"
    # 前端的空隙占位（旧版的 `sp` run）不再产出
    assert "sp" not in runs[0]


def test_block_is_one_sentence():
    """按句重切块：一段话里的两句 → 两个块（前端是按块切句的）。"""
    def span(x0, x1, text):
        return {"size": 10.0, "flags": 4, "font": "Times-Roman", "text": text,
                "origin": [x0, 108.0], "bbox": [x0, 100.0, x1, 112.0]}

    line = {"bbox": [10.0, 100.0, 260.0, 112.0],
            "spans": [span(10, 250, "First sentence. Second sentence.")]}
    page = {"blocks": [{"type": 0, "number": 0, "lines": [line]}]}
    texts, _copies = bp._build_text_items(page, 1, {}, [])
    assert [t["text"] for t in texts] == ["First sentence.", "Second sentence."]
    # 拆出来的行沿用原行（同一个 y/base），宽度按逐字符几何落在句末
    first, second = (t["lines"][0] for t in texts)
    assert abs(first["y"] - second["y"]) < 0.01
    assert first["x"] < second["x"]
    assert first["w"] + second["w"] <= line["bbox"][2] - line["bbox"][0] + 0.5


def test_inline_formula_box_reserves_full_bbox():
    """行内公式按**整框**占位：没打 formula_box_id 的残余字形也要吞掉。

    texify 的检测框常只标到公式的一部分字形；如果只按带 id 的 span 算宽度，
    占位会比公式框窄，前端只能把公式缩得很小，后面的正文还会压在公式上。
    """
    def span(x0, x1, text, bid=None):
        out = {"size": 10.0, "flags": 4, "font": "Times-Roman", "text": text,
               "origin": [x0, 108.0], "bbox": [x0, 100.0, x1, 112.0]}
        if bid is not None:
            out["formula_box_id"] = bid
        return out

    line = {
        "bbox": [10.0, 100.0, 210.0, 112.0],
        "spans": [span(10, 40, "before "), span(40, 60, "x", 1),
                  span(60, 100, "residual"), span(100, 200, " after")],
    }
    page = {"blocks": [{
        "type": 0, "number": 0, "lines": [line],
        "formula_boxes": [{"id": 1, "role": "inline", "bbox": [40.0, 98.0, 100.0, 114.0],
                           "latex": "x"}],
    }]}
    box_map = bp._collect_formula_boxes(page["blocks"])
    for box in box_map.values():
        box["_drawable"] = True
    texts, copies = bp._build_text_items(page, 1, box_map, [])
    runs = texts[0]["lines"][0]["runs"]
    gaps = [r for r in runs if r.get("gap") == 1]
    assert len(gaps) == 1
    assert abs(gaps[0]["gw"] - 60.0) < 0.5      # 整框宽 = 100-40，不是 id span 的 20
    assert "residual" not in "".join(r["t"] for r in runs)
    assert copies.get(1)                        # copy 区间仍指向合并后的 item


def test_inline_box_placeholder_goes_to_its_glyph_line():
    """行内公式的占位必须落在**框内字形所在的那条行**上。

    实测双栏页：左栏有一行正文与公式框的 y 完全一样，按「纵向重叠最多」挑归属行
    会挑到左栏那行，占位就跑到别的块里去了（公式框找不到 `.rngap` 锚点）。
    """
    def span(x0, x1, text, bid=None):
        out = {"size": 10.0, "flags": 4, "font": "Times-Roman", "text": text,
               "origin": [x0, 108.0], "bbox": [x0, 100.0, x1, 112.0]}
        if bid is not None:
            out["formula_box_id"] = bid
        return out

    left = {"bbox": [10.0, 100.0, 100.0, 112.0],
            "spans": [span(10, 100, "left column text")]}
    right = {"bbox": [120.0, 100.0, 260.0, 112.0],
             "spans": [span(120, 150, "before "), span(150, 190, "x", 1),
                       span(190, 260, " after")]}
    page = {"blocks": [
        {"type": 0, "number": 0, "lines": [left]},
        {"type": 0, "number": 1, "lines": [right],
         "formula_boxes": [{"id": 1, "role": "inline",
                            "bbox": [150.0, 98.0, 190.0, 114.0], "latex": "x"}]},
    ]}
    box_map = bp._collect_formula_boxes(page["blocks"])
    for box in box_map.values():
        box["_drawable"] = True
    texts, copies = bp._build_text_items(page, 1, box_map, [])

    holders = [t for t in texts
               if any(r.get("gap") == 1 for ln in t["lines"] for r in ln["runs"])]
    assert len(holders) == 1
    assert "before" in holders[0]["text"]        # 占位在右侧那行，而不是左栏那行
    gap = next(r for ln in holders[0]["lines"] for r in ln["runs"] if r.get("gap") == 1)
    assert abs(gap["gw"] - 40.0) < 0.5           # 整框宽（150→190），不是 id span 的 40
    assert copies.get(1)


def test_inline_placeholder_goes_to_the_line_with_preceding_text():
    """行内公式跨行碎片时，占位要落在**前一段正文所在的那条行**，宽度 = 真实空档。

    实测 `x = (-b±√…)/2a` 被 PyMuPDF 拆成两条行碎片：前文 “the formula ” 在前一条，
    分式那一截在后一条（字形更多）。按「字形最多」挑归属行会把占位放进后一条
    （行从公式中间开始），公式整体右移 40pt 并压住后面的 “where”；而且那条行只有
    25pt 宽却塞了 69pt 占位 → 前端整行被 scaleX 压到 0.72。
    """
    def span(x0, x1, text, bid=None):
        out = {"size": 10.0, "flags": 4, "font": "Times-Roman", "text": text,
               "origin": [x0, 108.0], "bbox": [x0, 100.0, x1, 112.0]}
        if bid is not None:
            out["formula_box_id"] = bid
        return out

    line_a = {"bbox": [10.0, 100.0, 90.0, 112.0],
              "spans": [span(10, 70, "the formula "), span(70, 90, "x=", 1)]}
    line_b = {"bbox": [90.0, 100.0, 130.0, 112.0],
              "spans": [span(90, 130, "frac", 1)]}
    page = {"blocks": [{
        "type": 0, "number": 0, "lines": [line_a, line_b],
        "formula_boxes": [{"id": 1, "role": "inline",
                           "bbox": [70.0, 98.0, 130.0, 114.0], "latex": "x"}],
    }]}
    box_map = bp._collect_formula_boxes(page["blocks"])
    for box in box_map.values():
        box["_drawable"] = True
    texts, copies = bp._build_text_items(page, 1, box_map, [])

    holder = [t for t in texts
              if any(r.get("gap") == 1 for ln in t["lines"] for r in ln["runs"])]
    assert len(holder) == 1
    ln = holder[0]["lines"][0]
    gap = next(r for ln2 in holder[0]["lines"] for r in ln2["runs"] if r.get("gap") == 1)
    assert "the formula" in holder[0]["text"]
    # 占位排在正文 run 之后（宽度 = 前文右边界 70 → 框右边 130）
    assert ln["runs"][0]["t"].strip() == "the formula"
    assert ln["runs"][-1] is gap
    assert abs(gap["gw"] - 60.0) < 0.5           # 不是框宽之外再加一整框
    # 行左边/宽度按留下的 run 重算：x=10，宽度覆盖到占位右边（130-10=120）
    assert abs(ln["x"] - 10.0) < 0.5
    assert abs(ln["w"] - 120.0) < 0.5
    assert copies.get(1)


def test_line_geometry_ignores_dropped_formula_glyphs():
    """行首的公式字形被删掉后，行左边要挪到**剩下的文字**上。

    实测 `q(xᵢ) measures…`：q(xᵢ) 属于行内公式（占位落在前一条行碎片上），
    这一条行碎片只剩 “measures”。行框若仍按原 bbox 定位，“measures” 会被拉回
    左边 23pt、压住公式；行宽也会偏窄 → 前端误判超宽、整行 scaleX 压扁。
    """
    def span(x0, x1, text, bid=None):
        out = {"size": 10.0, "flags": 4, "font": "Times-Roman", "text": text,
               "origin": [x0, 108.0], "bbox": [x0, 100.0, x1, 112.0]}
        if bid is not None:
            out["formula_box_id"] = bid
        return out

    line_a = {"bbox": [10.0, 100.0, 80.0, 112.0],
              "spans": [span(10, 60, "the formula "), span(60, 80, "x", 1)]}
    line_b = {"bbox": [80.0, 100.0, 200.0, 112.0],
              "spans": [span(80, 120, "frac", 1), span(120, 200, " measures")]}
    page = {"blocks": [{
        "type": 0, "number": 0, "lines": [line_a, line_b],
        "formula_boxes": [{"id": 1, "role": "inline",
                           "bbox": [60.0, 98.0, 120.0, 114.0], "latex": "x"}],
    }]}
    box_map = bp._collect_formula_boxes(page["blocks"])
    for box in box_map.values():
        box["_drawable"] = True
    texts, _copies = bp._build_text_items(page, 1, box_map, [])
    item = texts[0]
    assert "measures" in item["text"]
    second = [ln for ln in item["lines"] if abs(ln["y"] - 100.0) < 0.5][-1]
    assert second["x"] > 110.0                   # 不再从 80 开始
    assert second["w"] <= 85.0                   # 只剩 “ measures”


def test_display_formula_fragment_keeps_fragment_width():
    """独立公式的行碎片：占位宽度 = 该行 span 的并集（不是整框宽），行几何不变。"""
    def span(x0, x1, text, bid=None):
        out = {"size": 10.0, "flags": 4, "font": "Times-Roman", "text": text,
               "origin": [x0, 108.0], "bbox": [x0, 100.0, x1, 112.0]}
        if bid is not None:
            out["formula_box_id"] = bid
        return out

    line = {"bbox": [80.0, 100.0, 140.0, 112.0],
            "spans": [span(80, 110, "abc", 1), span(115, 140, "de", 1)]}
    page = {"blocks": [{
        "type": 0, "number": 0, "lines": [line],
        "formula_boxes": [{"id": 1, "role": "display",
                           "bbox": [80.0, 90.0, 200.0, 130.0], "latex": "x"}],
    }]}
    box_map = bp._collect_formula_boxes(page["blocks"])
    for box in box_map.values():
        box["_drawable"] = True
    texts, _copies = bp._build_text_items(page, 1, box_map, [])
    ln = texts[0]["lines"][0]
    gaps = [r for r in ln["runs"] if r.get("gap") == 1]
    assert len(gaps) == 1
    assert abs(gaps[0]["gw"] - 60.0) < 0.5      # 80→140（本行并集），不是整框 120
    assert abs(ln["x"] - 80.0) < 0.5 and abs(ln["w"] - 60.0) < 0.5


def test_false_superscript_is_dropped():
    """行首公式下标会带偏 PyMuPDF 的“上标”标记，正文必须被纠正回来。

    真实 PDF 里（latex_sample 第 2/3 页）：行内公式以 `i`（6.97pt，基线更低）开头，
    紧跟其后的正文 `gives the Euclidean…`（9.96pt）被 PyMuPDF 标成上标（flags bit0）。
    前端若照做会把正文缩到 0.7×，所以这里在数据层就先剔除这类假上标
    （判据是 `text_layer._is_super`：与正文同字号 + 压在本行基线上 → 不是上标）。
    """
    from backend.pdf_parser.text_layer import _is_super

    body = {"size": 9.96, "flags": 5, "font": "LMRoman10-Regular",
            "text": " gives the Euclidean", "origin": [102.0, 108.0],
            "bbox": [102.0, 100.0, 220.0, 112.0]}
    sup = {"size": 6.97, "flags": 5, "font": "LMRoman10-Regular", "text": "2",
           "origin": [221.0, 104.0], "bbox": [221.0, 100.0, 224.0, 106.0]}
    assert _is_super(body, 5, 108.0) is False      # 与正文同基线 → 假上标
    assert _is_super(sup, 5, 108.0) is True        # 更小且明显抬升 → 真上标

    line = {
        "bbox": [100.0, 100.0, 300.0, 114.0],
        "spans": [
            {"size": 6.97, "flags": 6, "font": "CMMI7", "text": "i",
             "origin": [100.0, 111.0], "bbox": [100.0, 106.0, 102.0, 112.0]},
            body, sup,
        ],
    }
    page = {"blocks": [{"type": 0, "number": 1, "lines": [line]}]}
    texts, _copies = bp._build_text_items(page, 1, {}, [])

    # 一行一个正文 run：样式取本行字符数最多的 run（正文），up 一律 false
    # —— 上标/下标与正文同号显示是「一行一个 run」的已知代价（见 text_layer）。
    run = texts[0]["lines"][0]["runs"][0]
    assert run["t"] == "i gives the Euclidean2"
    assert run["up"] is False
    assert abs(run["s"] - 9.96) < 0.05             # 字号保留 1 位小数（9.96 → 10.0）


def test_formula_boxes_and_gap_runs():
    page = _page_zero()
    box_map = bp._collect_formula_boxes(page["blocks"])
    assert box_map
    # 识别结果优先：至少有一条公式拿到了 LaTeX
    assert any(box.get("latex") for box in box_map.values())

    for box in box_map.values():
        box["_drawable"] = bp._mark_drawable(box, page["width"], page["height"])

    texts, copies = bp._build_text_items(page, 1, box_map, [])
    boxes = bp._build_formula_box_list(box_map, copies, page["width"], page["height"])

    assert texts
    assert boxes
    gap_runs = [run for t in texts for ln in t["lines"]
                for run in ln["runs"] if run.get("gap") is not None]
    assert gap_runs
    assert all(run["t"] == " " and run["gw"] > 0 for run in gap_runs)

    # 每条公式的 copy 区间都能在对应文字块的 canonical 里找到那个占位空格
    by_id = {t["id"]: t for t in texts}
    for box in boxes:
        assert len(box["bbox_norm"]) == 4
        assert 0 < box["bbox_norm"][2] <= 1
        assert 0 < box["bbox_norm"][3] <= 1
        for row in box["copy"]:
            item = by_id[row["id"]]
            canonical = "".join(
                "".join(str(r["t"]) for r in ln["runs"]) + "\n"
                for ln in item["lines"]
            )
            assert canonical[row["start"]:row["end"]] == " "


def test_blank_formula_row_stays_in_its_own_column():
    """「整行公式占位」的乘客归属必须看**同原块/同栏**，不能只比纵向距离。

    实测双栏页：右栏公式行到左栏句子的纵向距离（6.8pt）比到它自己那一段
    （7.2pt）还小，旧逻辑「只比 y 距离 + `above` 优先」把它挂进左栏句子 → 块矩形
    被撑到跨栏（前端 `.blk.sel` 的 outline 整块跨栏、逐行背景盖住另一栏）。
    现在：同原块优先 → 同栏（跨分栏走廊一律拒绝）→ 最近。

    同时块框只按**正文行**量，乘客行（`.rngap` 锚点）不参与 —— 即使挂上了，
    块矩形也不会跨栏。
    """
    from backend.pdf_parser.sentences import cut_blocks_by_sentence

    def tline(x, y, text):
        return {"x": float(x), "y": float(y), "w": 90.0, "h": 10.0,
                "runs": [{"t": text, "s": 10.0}]}

    def gline(x, y, bid):
        return {"x": float(x), "y": float(y), "w": 90.0, "h": 12.0,
                "runs": [{"t": " ", "s": 10.0, "gap": bid, "gw": 90.0}]}

    texts = [
        {"id": "p1b0", "lines": [tline(10, 100, "Left one."),
                                 tline(10, 112, "Left two."),
                                 tline(10, 124, "Left three."),
                                 tline(10, 136, "Left four.")]},
        {"id": "p1b1", "lines": [tline(130, 100, "Right one."),
                                 tline(130, 112, "Right two."),
                                 gline(130, 130, 7),
                                 tline(130, 136, "Right three.")]},
    ]
    out, _stats = cut_blocks_by_sentence(texts)

    left = [b for b in out if (b.get("text") or "").startswith("Left")]
    assert len(left) == 4
    # 左栏每块都只覆盖本栏（10..100），没有被右栏公式行撑开
    assert all(b["x"] + b["w"] <= 100.5 for b in left)

    holders = [b for b in out
               if any(r.get("gap") == 7 for ln in b["lines"] for r in ln["runs"])]
    assert len(holders) == 1
    assert (holders[0]["text"] or "").startswith("Right")   # 挂在右栏那句上
    assert holders[0]["x"] + holders[0]["w"] <= 220.5       # 块框仍只按正文行


def test_table_formula_box_does_not_hijack_other_column_line():
    """表格里的公式框（表头 `Top-k` 的 `k`）不许给别的栏的行挂占位。

    实测 AutoAlpha 第 5 页：左栏表头 `Top-` + `k`(CMMI9) + ` Alpha` 被判成行内公式，
    但该行整条落在表格框里、被 `_covered_ratio >= 0.55` 过滤掉 → 归属行只剩
    「纵向重叠最多」的兜底，右栏 `quency Memory recurrent network)…` 抢走了它。
    占位被插到行首（x=73.95、gw=3.83）后整行左边从 325 拉到 73.95、行宽 233→484，
    前端就把这句画到左栏表格上了。
    """
    def span(x0, y0, x1, y1, text, size=10.0, bid=None):
        out = {"size": size, "flags": 4, "font": "Times-Roman", "text": text,
               "origin": [x0, y1], "bbox": [x0, y0, x1, y1]}
        if bid is not None:
            out["formula_box_id"] = bid
        return out

    # 左栏表头（整条在表格框里，会被过滤）
    table_line = {
        "bbox": [60.6, 100.7, 97.3, 108.0],
        "spans": [span(60.6, 100.8, 73.9, 108.0, "Top-", 7.17),
                  span(73.9, 100.7, 77.8, 107.9, "k", 7.17, bid=7),
                  span(77.8, 100.8, 97.3, 108.0, " Alpha", 7.17)],
    }
    # 右栏正文行：y 带与公式框重叠 3.6pt（比同一句的前一行更多）
    head = {"bbox": [315.0, 92.4, 558.0, 103.3],
            "spans": [span(315.0, 92.4, 558.0, 103.3, "SFM: proposed SFM (the State Fre-", 9.96)]}
    cont = {"bbox": [325.0, 104.3, 558.0, 114.2],
            "spans": [span(325.0, 104.3, 558.0, 114.2,
                           "quency Memory recurrent network) which is an end-to-end", 9.96)]}
    page = {"blocks": [
        {"type": 0, "number": 0, "lines": [table_line]},
        {"type": 0, "number": 1, "lines": [head, cont],
         "formula_boxes": [{"id": 7, "role": "inline",
                            "bbox": [73.9, 100.7, 77.8, 107.9], "latex": "k"}]},
    ]}
    box_map = bp._collect_formula_boxes(page["blocks"])
    for box in box_map.values():
        box["_drawable"] = True

    texts, copies = bp._build_text_items(page, 5, box_map,
                                         [[55.5, 94.1, 307.5, 161.6]])

    cont_block = next(t for t in texts if "quency" in (t.get("text") or ""))
    cont_line = next(ln for ln in cont_block["lines"]
                     if "quency" in "".join(str(r.get("t") or "") for r in ln["runs"]))
    assert abs(cont_line["x"] - 325.0) < 0.5          # 行左边没被拉到左栏
    assert abs(cont_line["w"] - 233.0) < 0.5          # 行宽仍是右栏宽
    assert not [r for ln in cont_block["lines"] for r in ln["runs"] if r.get("gap") == 7]
    assert not copies.get(7)                          # 也不再指向右栏那句
    # 表格里的文字照旧不进正文流
    assert not any("Top-" in (t.get("text") or "") for t in texts)


def test_same_column_rejects_far_apart_bands():
    """兜底找归属行时的同栏判据：横向间隙超过 `_BOX_COL_TOL` 就不算同一栏。"""
    assert bp._same_column([325.0, 104.3, 558.0, 114.2], [73.9, 100.7, 77.8, 107.9]) is False
    assert bp._same_column([60.6, 100.7, 97.3, 108.0], [73.9, 100.7, 77.8, 107.9]) is True
    # 公式紧贴行首（bb 右边挨着行左边）仍然算同一栏
    assert bp._same_column([325.0, 104.3, 558.0, 114.2], [300.0, 100.7, 325.0, 107.9]) is True


def test_formula_boxes_inside_table_or_figure_are_not_drawn():
    """表格/插图里的公式框不下发：内容由表格 HTML 与插图本身呈现，再画就是重影。

    实测 AutoAlpha：表头 `Top-k` 的 `k`（+`Top1..Top5` 标签列、`h=1`/`avgIC` 表头）
    和图里的 `h=1`/`h=5` 图例都被检成公式，覆盖层把它们画在表格/图上是双层重影。
    """
    def box(bid, rect, role="inline", latex="x"):
        return {"id": bid, "role": role, "bbox": rect, "latex": latex,
                "size": 8.0, "_file": "", "_cache": "", "pdf_text": ""}

    box_map = {
        1: box(1, [73.9, 100.7, 77.8, 107.9], latex="k"),          # 表头里
        2: box(2, [70.8, 120.5, 87.1, 159.5], "display", "Top1"),  # 表格标签列
        3: box(3, [198.4, 559.2, 220.6, 568.2], latex="h=5"),      # 图里（只重合 0.49）
        4: box(4, [400.0, 300.0, 440.0, 315.0], latex="x^2"),      # 正文里的真公式
    }
    for b in box_map.values():
        b["_drawable"] = True
    table_rects = [[55.5, 94.1, 307.5, 161.6]]
    image_rects = [[62.1, 469.6, 288.8, 563.6]]

    boxes = bp._build_formula_box_list(box_map, {}, 612.0, 792.0)
    assert len(boxes) == 4
    kept, hidden = bp._hide_graphic_formula_boxes(boxes, table_rects, image_rects)
    assert hidden == 3
    assert [b["id"] for b in kept] == [4]        # 正文里的公式照常下发


def test_text_line_inside_figure_is_dropped():
    """图内文字（图表刻度/子图注）只要中心点落在插图里就不画，避免与插图叠字。"""
    def span(rect, text):
        return {"size": 10.0, "flags": 4, "font": "Times-Roman", "text": text,
                "origin": [rect[0], rect[3]], "bbox": rect}

    inside = {"bbox": [150.0, 160.0, 250.0, 230.0],       # 与图框重合 0.57（<0.6）
              "spans": [span([150.0, 160.0, 250.0, 230.0], "(c) holding period")]}
    outside = {"bbox": [10.0, 300.0, 200.0, 312.0],
               "spans": [span([10.0, 300.0, 200.0, 312.0], "body text")]}
    page = {"blocks": [{"type": 0, "number": 0, "lines": [inside, outside]}]}
    texts, _copies = bp._build_text_items(page, 1, {}, [], [[100.0, 100.0, 300.0, 200.0]])
    joined = " ".join(t.get("text") or "" for t in texts)
    assert "holding period" not in joined
    assert "body text" in joined
