import unittest

from backend.pdf_refs import build_refs
from backend.pdf_refs.refs_surya import split_surya_reference_blocks


def _text(block_id, text, y):
    return {
        "id": block_id,
        "text": text,
        "x": 10,
        "y": y,
        "w": 500,
        "h": 12,
        "lines": [{"x": 10, "y": y, "w": 500, "h": 12,
                   "runs": [{"t": text}]}],
    }


def _text_at(block_id, text, y, x):
    """同 `_text`，但可以指定左侧 x（参考文献区的悬挂缩进靠它）。"""
    block = _text(block_id, text, y)
    block["x"] = x
    block["lines"][0]["x"] = x
    block["lines"][0]["w"] = 500 - (x - 10)
    return block


class ReferenceFallbackTest(unittest.TestCase):
    def test_author_year_tokens_match_entries_without_links(self):
        doc = {
            "parser": "pymupdf",
            "parser_info": {"links_total": 0},
            "pages": [{
                "texts": [
                    _text("p0b0", "See (Smith et al., 2020) and (e.g., [Wu et al., 2021]). "
                          "Not (Wilson and Clark, 2020).", 20),
                    _text("p0b1", "References", 10),
                    _text("p0b2", "[Smith and Jones, 2020] First paper.", 30),
                    _text("p0b3", "[Wu et al., 2021] Second paper.", 50),
                ],
            }],
        }

        result = build_refs(doc)

        # 方括号嵌在圆括号里时只标里层；作者对不上的令牌不标。
        self.assertEqual(
            [(link["token"], link["refs"]) for link in result["links"]],
            [("(Smith et al., 2020)", [0]), ("[Wu et al., 2021]", [1])],
        )

    def test_numeric_citations_map_when_pdf_has_no_goto_links(self):
        doc = {
            "parser": "pymupdf",
            "parser_info": {"links_total": 2},
            "pages": [
                {"texts": [_text("p0b0", "Earlier work [1] and [2, 3] is relevant.", 20)]},
                {"texts": [
                    _text("p1b0", "References", 10),
                    _text("p1b1", "[1] First cited paper, 2020.", 30),
                    _text("p1b2", "[2] Second cited paper, 2021.", 50),
                    _text("p1b3", "[3] Third cited paper, 2022.", 70),
                ]},
            ],
        }

        result = build_refs(doc)

        self.assertEqual(result["counts"]["entries"], 3)
        self.assertEqual(
            [(link["token"], link["nums"], link["refs"]) for link in result["links"]],
            [("[1]", [1], [0]), ("[2, 3]", [2, 3], [1, 2])],
        )

    def test_reference_entries_are_not_marked_as_body_citations(self):
        doc = {
            "parser": "pymupdf",
            "parser_info": {"links_total": 0},
            "pages": [{
                "texts": [
                    _text("p0b0", "References", 10),
                    _text("p0b1", "[1] This entry mentions [2] in its title.", 30),
                    _text("p0b2", "[2] Another cited paper, 2021.", 50),
                ],
            }],
        }

        result = build_refs(doc)

        self.assertEqual(result["links"], [])

    def test_heading_merged_with_first_author_year_entry(self):
        """本地解析方案会把标题和第一条并进同一个块（`References [Smith…]`）。"""
        doc = {
            "parser": "pymupdf",
            "parser_info": {"links_total": 0},
            "pages": [
                {"texts": [
                    _text("p0b0", "See [Smith, 2020] and [Wu et al., 2021].", 20),
                ]},
                {"texts": [
                    _text("p1b0",
                          "References [Smith and Jones, 2020] First paper. "
                          "ACM, 2020. [Wu et al., 2021] Second paper.", 30),
                ]},
            ],
        }

        result = build_refs(doc)

        self.assertEqual(result["section_page"], 1)
        self.assertEqual([entry["n"] for entry in result["entries"]], [None, None])
        self.assertEqual(
            [entry["label"] for entry in result["entries"]],
            ["[Smith and Jones, 2020]", "[Wu et al., 2021]"],
        )
        # 块中间的标签也要切开：上一条的结尾（ACM, 2020.）不能并进下一条。
        self.assertTrue(result["entries"][0]["text"].endswith("ACM, 2020."))
        self.assertTrue(result["entries"][1]["text"].startswith("[Wu et al., 2021]"))
        self.assertEqual(
            [(link["token"], link["refs"]) for link in result["links"]],
            [("[Smith, 2020]", [0]), ("[Wu et al., 2021]", [1])],
        )

    def test_author_year_scan_is_merged_with_goto_links(self):
        """PDF 只给部分引用做了 hyperref 时，其余作者-年份引用也要标出来。"""
        doc = {
            "parser": "pymupdf",
            "parser_info": {"links_total": 1},
            "pages": [
                {
                    "texts": [_text(
                        "p0b0",
                        "Prior [Smith et al., 2020] and [Wu et al., 2021].",
                        20,
                    )],
                    "links": [{"kind": "goto", "rect": [10, 20, 70, 32],
                               "page": 1, "to": [10, 30]}],
                },
                {"texts": [
                    _text("p1b0", "References", 10),
                    _text("p1b1", "[Smith and Jones, 2020] First paper.", 30),
                    _text("p1b2", "[Wu et al., 2021] Second paper.", 60),
                ]},
            ],
        }

        result = build_refs(doc)

        self.assertEqual(
            [(link["token"], link["refs"]) for link in result["links"]],
            [("[Smith et al., 2020]", [0]), ("[Wu et al., 2021]", [1])],
        )

    def test_goto_target_above_entry_top_still_maps_to_that_entry(self):
        """hyperref 目标点比条目行顶高几个 pt、条目块又整行宽（OCR 路线的常态）时也要对上。

        实测 `unlimited_ocr` 的参考文献条目块宽 414pt、`to` 贴在页面左边距
        （x≈95.14，y 比条目行顶高 4~5pt）：旧判据拿“到块中心”的距离兜底 →
        每条都差 200pt+，199 条链接只有 6 条能映射上。
        """
        doc = {
            "parser": "unlimited_ocr",
            "parser_info": {"links_total": 2},
            "pages": [
                {
                    "texts": [_text("p0b0", "Earlier work [1] and [2].", 20)],
                    "links": [
                        # 目标点比条目块顶（100 / 140）高 5pt，x 在页面左边距
                        {"kind": "goto", "rect": [10, 20, 26, 32],
                         "page": 1, "to": [10, 95]},
                        {"kind": "goto", "rect": [40, 20, 56, 32],
                         "page": 1, "to": [10, 135]},
                    ],
                },
                {"texts": [
                    _text("p1b0", "References", 60),
                    _text("p1b1", "[1] First cited paper, 2020.", 100),
                    _text("p1b2", "[2] Second cited paper, 2021.", 140),
                ]},
            ],
        }

        result = build_refs(doc)

        self.assertEqual(
            [(link["token"], link["refs"]) for link in result["links"]],
            [("[1]", [0]), ("[2]", [1])],
        )

    def test_unlabelled_author_year_list_is_split_by_hanging_indent(self):
        """ACL/arXiv 风格的参考文献没有 `[作者, 年份]` 标签：条目行首直接是作者名。

        PyMuPDF 句子级块会把一条文献切成「作者 / 标题+年份 / 出处」几块，标题块
        **含年份**——旧规则“含年份的块起条目”会让每条变成「标题+年份 + 下一条作者」，
        作者与年份永远不在同一条目里，作者-年份令牌全部标不出来。
        """
        doc = {
            "parser": "pymupdf",
            "parser_info": {"links_total": 0},
            "pages": [{
                "texts": [
                    _text("p0b0", "We build on (Smith et al., 2020) and (Lee et al., 2020).", 20),
                    _text("p0b1", "References", 10),
                    # 条目首行齐左边缘（x=10），标题/出处缩进 10pt
                    _text_at("p0b2", "Alan Smith, Bo Chen, and Cy Doe.", 30, 10),
                    _text_at("p0b3", "A study of flows. arXiv preprint arXiv:2001.01234, 2020. "
                                    "(page 6)", 42, 20),
                    _text_at("p0b4", "Chanhyuk Lee, Dana Wu, and Eli Kim.", 60, 10),
                    _text_at("p0b5", "Looped language models. PMLR, 2020. (pages 1, 3)", 72, 20),
                ],
            }],
        }

        result = build_refs(doc)

        self.assertEqual(result["counts"]["entries"], 2)
        # 一条 = 作者 + 标题 + 出处；反向页码标记不该留在条目文字里
        self.assertTrue(result["entries"][0]["text"].startswith("Alan Smith, Bo Chen"))
        self.assertTrue(result["entries"][0]["text"].endswith("2020."))
        self.assertNotIn("(page", result["entries"][0]["text"])
        # 无标签条目也要有人看得懂的标签（前端卡片徽标）
        self.assertEqual([e["label"] for e in result["entries"]],
                         ["[Smith, 2020]", "[Lee, 2020]"])
        self.assertEqual(
            [(link["token"], link["refs"]) for link in result["links"]],
            [("(Smith et al., 2020)", [0]), ("(Lee et al., 2020)", [1])],
        )

    def test_first_author_beats_coauthor_for_author_year_tokens(self):
        """共同作者不该让同年的其他文献也挂到同一个引用上。"""
        doc = {
            "parser": "pymupdf",
            "parser_info": {"links_total": 0},
            "pages": [{
                "texts": [
                    _text("p0b0", "Compared with (Wu et al., 2021).", 20),
                    _text("p0b1", "References", 10),
                    _text_at("p0b2", "Chanhyuk Lee, Dana Wu, and Eli Kim.", 30, 10),
                    _text_at("p0b3", "First paper. PMLR, 2021.", 42, 20),
                    _text_at("p0b4", "Dana Wu and Alan Smith.", 60, 10),
                    _text_at("p0b5", "Second paper. PMLR, 2021.", 72, 20),
                ],
            }],
        }

        result = build_refs(doc)

        # Wu 是第一条的共同作者、第二条的第一作者 → 只认第二条
        self.assertEqual(
            [(link["token"], link["refs"]) for link in result["links"]],
            [("(Wu et al., 2021)", [1])],
        )


class SuryaReferenceTest(unittest.TestCase):
    def test_numbered_ocr_references_become_independent_layout_blocks(self):
        references = _text(
            "p1b1",
            "[1] First paper, 2020.\n[2] Second paper, 2021.",
            30,
        )
        references.update({"h": 40, "size": 10, "kind": "text"})
        references["lines"][0].update({"h": 40, "runs": [
            {"t": "[1] ", "b": True},
            {"t": "First paper, 2020.\n[2] Second paper, 2021."},
        ]})
        pages = [{"texts": [_text("p0b0", "References", 10), references]}]

        added = split_surya_reference_blocks(pages)
        blocks = pages[0]["texts"][1:]

        self.assertEqual(added, {"text": 1})
        self.assertEqual([block["text"] for block in blocks], [
            "[1] First paper, 2020.", "[2] Second paper, 2021.",
        ])
        self.assertEqual(len({block["id"] for block in blocks}), 2)
        self.assertLess(blocks[0]["y"], blocks[1]["y"])
        self.assertTrue(blocks[0]["lines"][0]["runs"][0]["b"])

        result = build_refs({
            "parser": "surya",
            "parser_info": {"links_total": 0},
            "pages": pages,
        })
        self.assertEqual([entry["n"] for entry in result["entries"]], [1, 2])

    def test_splits_multiple_references_from_one_ocr_block(self):
        body = _text("p0b0", "Prior work [35, 2, 5] is relevant.", 20)
        references = _text(
            "p1b1",
            "[1] First paper, 2020.\n"
            "[2] Second paper, 2021.\n"
            "[5] Fifth paper, 2022.\n"
            "[35] Thirty-fifth paper, 2023.",
            30,
        )
        doc = {
            "parser": "surya",
            "parser_info": {"links_total": 18},
            "pages": [
                {"texts": [body]},
                {"texts": [_text("p1b0", "References", 10), references]},
            ],
        }

        result = build_refs(doc)

        self.assertEqual(
            [(entry["n"], entry["text"]) for entry in result["entries"]],
            [
                (1, "[1] First paper, 2020."),
                (2, "[2] Second paper, 2021."),
                (5, "[5] Fifth paper, 2022."),
                (35, "[35] Thirty-fifth paper, 2023."),
            ],
        )
        self.assertEqual(
            [(link["token"], link["nums"], link["refs"])
             for link in result["links"]],
            [("[35, 2, 5]", [2, 5, 35], [1, 2, 3])],
        )

    def test_does_not_mark_citations_inside_surya_reference_blocks(self):
        doc = {
            "parser": "surya",
            "parser_info": {"links_total": 0},
            "pages": [{
                "texts": [
                    _text("p0b0", "References", 10),
                    _text("p0b1", "[1] A paper whose title mentions [2].\n"
                          "[2] Another paper.", 30),
                ],
            }],
        }

        result = build_refs(doc)

        self.assertEqual([entry["n"] for entry in result["entries"]], [1, 2])
        self.assertEqual(result["links"], [])

    def test_surya_keeps_author_year_entries_as_a_separate_fallback(self):
        doc = {
            "parser": "surya",
            "parser_info": {"links_total": 0},
            "pages": [{
                "texts": [
                    _text("p0b0", "References", 10),
                    _text("p0b1", "Smith et al. (2020). First paper.", 30),
                    _text("p0b2", "Jones (2021). Second paper.", 50),
                ],
            }],
        }

        result = build_refs(doc)

        self.assertEqual(result["counts"]["entries"], 2)
        self.assertEqual([entry["n"] for entry in result["entries"]], [None, None])

    def test_author_year_bracket_entries_split_and_map_without_links(self):
        references = _text(
            "p1b1",
            "[Smith and Jones, 2020] First paper.\n"
            "[Wu et al., 2021] Second paper.",
            30,
        )
        references.update({"h": 40, "size": 10, "kind": "text"})
        pages = [{"texts": [
            _text("p0b0", "Prior work [Smith et al., 2020] and [Wu et al., 2021].",
                  20),
            _text("p1b0", "References", 10),
            references,
        ]}]

        added = split_surya_reference_blocks(pages)
        blocks = pages[0]["texts"][2:]

        self.assertEqual(added, {"text": 1})
        self.assertEqual(
            [block["text"] for block in blocks],
            ["[Smith and Jones, 2020] First paper.",
             "[Wu et al., 2021] Second paper."],
        )
        self.assertEqual([block["ref_number"] for block in blocks], [None, None])

        result = build_refs({
            "parser": "surya",
            "parser_info": {"links_total": 0},
            "pages": pages,
        })

        self.assertEqual([entry["n"] for entry in result["entries"]], [None, None])
        self.assertEqual(
            [(link["token"], link["refs"]) for link in result["links"]],
            [("[Smith et al., 2020]", [0]), ("[Wu et al., 2021]", [1])],
        )

    def test_unlabelled_author_year_references_split_by_line(self):
        """ACL/arXiv 风格（条目首行直接是作者名）也要按行拆成独立块。"""
        references = _text(
            "p1b1",
            "Manan Agarwal, Sheel Shah, and Jinwoo Kim. Posterior refinement: "
            "Fast language generation via any-order flow maps. arXiv preprint "
            "arXiv:2606.24773, 2026. (page 6)\n"
            # 姓名缩写不带句点（`S`）：以前这一行认不出条目开头，整条被并进上一条
            "Michael S Albergo and Eric Vanden-Eijnden. Building normalizing flows "
            "with stochastic interpolants. arXiv preprint arXiv:2209.15571, 2022. "
            "(page 6)\n"
            # 折行条目：上一行没有句末标点，续行不能当新条目
            "Cem Anil, Shaojie Bai, and J Zico Kolter. Path independent equilibrium "
            "models can better exploit test-time computation. Advances in Neural "
            "Information\n"
            "Processing Systems, 35:7796–7809, 2022. (page 6)\n"
            "Alan Baddeley. Working memory. Science, 255(5044):556–559, 1992. (page 1)",
            30,
        )
        references.update({"h": 80, "size": 10, "kind": "list"})
        references["lines"][0]["h"] = 80
        pages = [{"texts": [
            _text("p0b0", "Prior work (Albergo and Vanden-Eijnden, 2022) and "
                  "(Baddeley, 1992).", 20),
            _text("p1b0", "References", 10),
            references,
        ]}]

        added = split_surya_reference_blocks(pages)
        blocks = pages[0]["texts"][2:]

        self.assertEqual(added, {"list": 3})
        self.assertTrue(all(block.get("ref_entry") for block in blocks))
        self.assertEqual([block["ref_number"] for block in blocks],
                         [None, None, None, None])
        self.assertTrue(blocks[0]["text"].startswith("Manan Agarwal,"))
        self.assertTrue(blocks[1]["text"].startswith("Michael S Albergo and"))
        # 折行条目仍是**一条**（`Processing Systems, …` 不另起）
        self.assertEqual(" ".join(blocks[2]["text"].split()),
                         "Cem Anil, Shaojie Bai, and J Zico Kolter. Path independent "
                         "equilibrium models can better exploit test-time computation. "
                         "Advances in Neural Information Processing Systems, "
                         "35:7796–7809, 2022. (page 6)")
        self.assertEqual(blocks[3]["text"],
                         "Alan Baddeley. Working memory. Science, "
                         "255(5044):556–559, 1992. (page 1)")

        result = build_refs({
            "parser": "surya",
            "parser_info": {"links_total": 0},
            "pages": pages,
        })

        self.assertEqual(result["counts"]["entries"], 4)
        # 无标签条目也要有卡片徽标（第一作者姓氏 + 年份）
        self.assertEqual([entry["label"] for entry in result["entries"]],
                         ["[Agarwal, 2026]", "[Albergo, 2022]", "[Anil, 2022]",
                          "[Baddeley, 1992]"])
        self.assertEqual(
            [(link["token"], link["refs"]) for link in result["links"]],
            [("(Albergo and Vanden-Eijnden, 2022)", [1]), ("(Baddeley, 1992)", [3])],
        )

    def test_surya_scans_unlinked_numeric_citations_alongside_goto_links(self):
        body = _text("p0b0", "Citations [1] and [2].", 20)
        first_ref = _text("p1b1", "[1] First paper, 2020.", 30)
        second_ref = _text("p1b2", "[2] Second paper, 2021.", 60)
        doc = {
            "parser": "surya",
            "parser_info": {"links_total": 1},
            "pages": [
                {
                    "texts": [body],
                    "links": [{
                        "kind": "goto",
                        "rect": [50, 20, 70, 32],
                        "page": 1,
                        "to": [20, 30],
                    }],
                },
                {"texts": [_text("p1b0", "References", 10), first_ref, second_ref]},
            ],
        }

        result = build_refs(doc)

        self.assertEqual(
            [(link["token"], link["nums"]) for link in result["links"]],
            [("[1]", [1]), ("[2]", [2])],
        )


class UnlimitedOcrReferenceTest(unittest.TestCase):
    def test_reference_kind_fallback_stays_on_unlimited_ocr_strategy(self):
        doc = {
            "parser": "unlimited_ocr",
            "parser_info": {"links_total": 0},
            "pages": [{
                "texts": [
                    _text("p0b0", "Prior work [2].", 10),
                    dict(_text("p0b1", "[1] First paper, 2020.", 30),
                         kind="reference"),
                    dict(_text("p0b2", "[2] Second paper, 2021.", 50),
                         kind="reference"),
                ],
            }],
        }

        result = build_refs(doc)

        self.assertEqual([entry["n"] for entry in result["entries"]], [1, 2])
        self.assertEqual([link["nums"] for link in result["links"]], [[2]])


if __name__ == "__main__":
    unittest.main()
