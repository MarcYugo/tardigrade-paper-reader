import json
import unittest

from backend.agents.note_out_agent import (extract_code_links, run_note_out_agent)
from backend.note_out import (build_notes, build_sections, export_filename,
                              export_paper, to_json)


def _block(block_id, text, kind="text"):
    return {"id": block_id, "text": text, "kind": kind}


def _sample_doc():
    return {
        "id": "doc1",
        "title": "A Sample Paper",
        "parser": "pymupdf",
        "pages": [
            {"texts": [
                _block("p0b0", "A Sample Paper", "title"),
                _block("p0b1", "Abstract", "heading"),
                _block("p0b2", "We propose a simple method for testing exports."),
                _block("p0b3", "1 Introduction", "heading"),
                _block("p0b4", "Main content starts here."),
            ]},
            {"texts": [
                _block("p1b0", "References", "heading"),
                _block("p1b1", "[1] A. Smith. First cited paper. 2020."),
                _block("p1b2", "[2] B. Jones. Second cited paper. 2021."),
                _block("p1b3", "Appendix A", "heading"),
                _block("p1b4", "Extra details live in the appendix."),
            ]},
        ],
    }


class NoteOutSectionsTest(unittest.TestCase):
    def test_split_into_five_parts(self):
        sections = build_sections(_sample_doc())

        self.assertEqual(sections["title"]["text"], "A Sample Paper")
        self.assertIn("simple method", sections["abstract"]["text"])
        self.assertIn("Main content", sections["main_text"]["text"])
        self.assertNotIn("References", sections["main_text"]["text"])
        self.assertIn("First cited paper", sections["references"]["text"])
        self.assertGreaterEqual(sections["references"]["count"], 2)
        self.assertEqual(
            [e["index"] for e in sections["references"]["entries"]][:2], [1, 2])
        self.assertEqual(sections["references"]["entries"][0]["page"], 2)
        self.assertIn("Extra details", sections["appendix"]["text"])
        self.assertEqual(sections["main_text"]["pages"], [1])
        self.assertEqual(sections["appendix"]["pages"], [2])

    def test_abstract_inferred_without_abstract_heading(self):
        doc = {
            "title": "No Abstract Heading",
            "pages": [{"texts": [
                _block("p0b0", "No Abstract Heading", "title"),
                _block("p0b1", "This long paragraph is the abstract. " * 8),
                _block("p0b2", "1 Introduction", "heading"),
                _block("p0b3", "Body text."),
            ]}],
        }
        sections = build_sections(doc)
        self.assertIn("long paragraph is the abstract", sections["abstract"]["text"])
        self.assertIn("Body text", sections["main_text"]["text"])
        self.assertNotIn("long paragraph", sections["main_text"]["text"])

    def test_main_text_keeps_original_blocks_with_bbox(self):
        doc = {
            "title": "Layout Paper",
            "pages": [{"w": 600, "h": 800, "texts": [
                {"id": "p0b0", "text": "Layout Paper", "kind": "title",
                 "x": 10, "y": 20, "w": 300, "h": 30},
                {"id": "p0b1", "text": "1 Introduction", "kind": "heading",
                 "x": 10, "y": 60, "w": 120, "h": 18},
                {"id": "p0b2", "text": "Body text one.", "kind": "text",
                 "x": 10, "y": 90, "w": 400, "h": 40,
                 "math": [{"a": 0, "b": 1, "latex": "B"}]},
                {"id": "p0b3", "text": "Body text two.", "kind": "text",
                 "x": 10, "y": 140, "w": 400, "h": 40},
            ]}],
        }
        sections = build_sections(doc)
        main = sections["main_text"]
        self.assertEqual(main["text"],
                         "1 Introduction Body text one. Body text two.")
        self.assertEqual([b["id"] for b in main["blocks"]],
                         ["p0b1", "p0b2", "p0b3"])
        self.assertEqual(main["blocks"][1]["bbox"], [10.0, 90.0, 410.0, 130.0])
        self.assertEqual(main["blocks"][1]["page"], 1)
        self.assertEqual(main["blocks"][1]["text"], "Body text one.")
        self.assertEqual(main["blocks"][1]["math"][0]["latex"], "B")
        # 其它分节同样带 blocks（导出结构保持一致）
        for name in ("title", "abstract", "references", "appendix",
                     "front_matter"):
            self.assertIn("blocks", sections[name])


class NoteOutNotesTest(unittest.TestCase):
    def test_notes_resolve_page_and_sentence(self):
        anno = {
            "highlights": {"p0b4#0": {"color": "yellow", "at": "now"}},
            "notes": [{"id": "n1", "block_id": "p0b4#0", "text": "my note",
                       "by": "alice"}],
            "free_notes": [{"id": "f1", "title": "idea", "text": "free note",
                            "by": "alice"}],
        }
        notes = build_notes(anno, _sample_doc())

        self.assertEqual(notes["sentence_notes"][0]["page"], 1)
        self.assertEqual(notes["sentence_notes"][0]["sentence"],
                         "Main content starts here.")
        self.assertEqual(notes["sentence_notes"][0]["note"], "my note")
        self.assertEqual(notes["highlights"][0]["page"], 1)
        self.assertEqual(notes["stats"]["notes_total"], 2)
        self.assertEqual(notes["stats"]["highlights"], 1)


class NoteOutExportTest(unittest.TestCase):
    def test_mechanical_export_is_json_serializable(self):
        export = export_paper(
            doc=_sample_doc(),
            rec={"id": "doc1", "title": "A Sample Paper", "owner": "alice",
                 "num_pages": 2},
            anno={"notes": [{"id": "n1", "block_id": "p0b4#0", "text": "my note"}],
                  "free_notes": [], "highlights": {}},
            mode="mechanical",
            username="alice",
        )

        self.assertEqual(export["format"], "tardigrade-note-export")
        self.assertEqual(export["mode"], "mechanical")
        self.assertEqual(export["paper"]["title"], "A Sample Paper")
        self.assertIn("summary", export["report"])
        self.assertEqual(export["report"]["stats"]["sentence_notes"], 1)
        json.loads(to_json(export))          # 必须能序列化
        self.assertIn("note-export.json", export_filename(export["paper"]))

    def test_agent_mode_requires_llm(self):
        with self.assertRaises(ValueError):
            export_paper(doc=_sample_doc(), mode="agent")


class _FakeTranslator:
    """只模拟 Agent 需要的两个方法：工具循环 + 结构化报告。"""

    def __init__(self):
        self.tool_calls_made = 0

    def chat_completion(self, system, messages, *, tools=None, temperature=0.3):
        if self.tool_calls_made:
            return {"content": "已完成核查。"}
        self.tool_calls_made += 1
        return {"content": None, "tool_calls": [{
            "id": "call-1",
            "function": {
                "name": "find_code_repository",
                # 关掉 GitHub 搜索：测试环境不联网
                "arguments": json.dumps({"search_github": False}),
            },
        }]}

    def chat_json(self, system, user, *, temperature=0.1):
        return {
            "summary": "这是一份测试报告。",
            "paper_overview": {"problem": "问题", "method": "方法",
                               "contributions": ["贡献"], "findings": [],
                               "limitations": []},
            "notes_analysis": {"overview": "笔记总览",
                               "themes": [{"title": "主题", "points": ["要点"]}],
                               "connections": [], "questions": [], "next": []},
            "references_review": [],
            "code_repositories": [],
            "conclusion": "结论",
            "next_steps": ["下一步"],
        }


class NoteOutAgentTest(unittest.TestCase):
    def test_agent_loop_and_report(self):
        doc = _sample_doc()
        sections = build_sections(doc)
        notes = build_notes({"notes": [], "free_notes": [], "highlights": {}}, doc)
        report = run_note_out_agent(
            _FakeTranslator(),
            paper={"title": "A Sample Paper", "num_pages": 2},
            sections=sections,
            notes=notes,
            verify_all=False,      # 测试环境不联网
        )

        self.assertEqual(report["summary"], "这是一份测试报告。")
        self.assertEqual(report["paper_overview"]["contributions"], ["贡献"])
        self.assertEqual(report["notes_analysis"]["themes"][0]["title"], "主题")
        self.assertIn("references_stats", report)
        json.loads(json.dumps(report, ensure_ascii=False))   # 可序列化


class _QuietTranslator:
    """不调工具、只给报告的假 LLM（用于只测核验链路）。"""

    def chat_completion(self, system, messages, *, tools=None, temperature=0.3):
        return {"content": "已完成核查。"}

    def chat_json(self, system, user, *, temperature=0.1):
        return {"summary": "报告", "conclusion": "结论",
                "references_review": [], "code_repositories": []}


class NoteOutFullVerifyTest(unittest.TestCase):
    def test_all_references_verified_and_counted(self):
        import backend.agents.note_out_agent as agent

        called = []

        def fake_verify(rid, *, title, authors, year, doi, arxiv, query, cfg=None):
            called.append(rid)
            ok = rid == 1
            return {
                "ok": True, "reference_id": rid,
                "status": "verified" if ok else "not_found",
                "message": "stub",
                "best": {"title": title, "year": year,
                         "similarity": 0.95 if ok else 0.2},
                "candidates": [], "errors": [],
            }

        doc = _sample_doc()
        sections = build_sections(doc)
        notes = build_notes({"notes": [], "free_notes": [], "highlights": {}}, doc)
        original = agent._verify_reference
        agent._verify_reference = fake_verify
        try:
            report = run_note_out_agent(
                _QuietTranslator(),
                paper={"title": "A Sample Paper", "num_pages": 2},
                sections=sections, notes=notes, verify_all=True)
        finally:
            agent._verify_reference = original

        self.assertEqual(sorted(called), [1, 2])       # 两条都查了，不是只查一部分
        stats = report["references_stats"]
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["checked"], 2)
        self.assertEqual(stats["matched"], 1)          # 检索到且对得上
        self.assertEqual(stats["verified"], 1)
        self.assertEqual(stats["not_found"], 1)
        self.assertEqual(stats["not_checked"], 0)
        self.assertEqual([(r["id"], r["status"]) for r in report["references_review"]],
                         [(1, "verified"), (2, "not_found")])


class CodeLinkTest(unittest.TestCase):
    def test_extract_and_normalize_code_links(self):
        text = ("Code is at https://github.com/acme/paper-code. "
                "Mirror: github.com/foo/bar, thanks.")
        self.assertEqual(extract_code_links(text), [
            "https://github.com/acme/paper-code",
            "https://github.com/foo/bar",
        ])

    def test_ignore_site_pages(self):
        self.assertEqual(extract_code_links("https://github.com/features/copilot"), [])


class RefReviewConsistencyTest(unittest.TestCase):
    def test_evidence_status_wins_over_model(self):
        from backend.agents.note_out_agent import _clean_ref_review

        evidence = {"references": {
            1: {"status": "verified", "message": "ok"},
            2: {"status": "not_found", "message": "no"},
        }}
        rows = _clean_ref_review(
            [{"id": 1, "status": "not_found", "note": "模型说没查到"},
             {"id": 2, "status": "verified", "note": "模型说存在"}],
            evidence)
        # 状态一律以证据为准，保证与 references_stats 的数字一致
        self.assertEqual([(r["id"], r["status"]) for r in rows],
                         [(1, "verified"), (2, "not_found")])
        self.assertEqual(rows[0]["note"], "模型说没查到")   # 说明文字保留模型写法


if __name__ == "__main__":
    unittest.main()
