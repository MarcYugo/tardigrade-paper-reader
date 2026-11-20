"""笔记导出 · 图片（Matplotlib + XKCD 三栏版式）的冒烟测试。

没装 matplotlib 时整组跳过（JSON 导出不受影响）。
"""
import base64
import json
import unittest
from unittest.mock import patch

from backend.notes_out import export_filename, export_paper

try:
    import matplotlib  # noqa: F401
    HAVE_MPL = True
except Exception:                       # pragma: no cover - 环境没装 matplotlib
    HAVE_MPL = False


def _sample_export():
    doc = {
        "id": "doc1",
        "title": "A Sample Paper",
        "pages": [{"texts": [
            {"id": "p0b0", "text": "A Sample Paper", "kind": "title", "y": 20},
            {"id": "p0b1", "text": "Alice Smith\nUniversity of Testing\nalice@example.com", "y": 60},
            {"id": "p0b2", "text": "Abstract", "kind": "heading", "y": 120},
            {"id": "p0b3", "text": "We propose a simple method for testing exports."},
            {"id": "p0b4", "text": "1 Introduction", "kind": "heading"},
            {"id": "p0b5", "text": "Main content starts here."},
        ]}],
    }
    anno = {"notes": [{"id": "n1", "block_id": "p0b5#0", "text": "my note"}],
            "free_notes": [], "highlights": {}}
    rec = {"id": "doc1", "title": "A Sample Paper", "owner": "alice",
           "num_pages": 1,
           "ai": {"summary": {"overview": "总览：这篇文章讲了一个简单方法。",
                              "themes": [{"title": "方法", "points": ["要点一"]}],
                              "connections": ["与 X 相关"],
                              "questions": [], "next": ["读 X"]},
                  "summary_at": "2026-11-19T10:00:00"}}
    return export_paper(doc=doc, rec=rec, anno=anno, mode="mechanical",
                        username="alice")


class NoteOutImageTest(unittest.TestCase):
    def test_image_filename_is_png(self):
        self.assertEqual(
            export_filename({"title": "A Sample Paper"}, "mechanical", "image"),
            "A Sample Paper-note-export.png")

    def test_authors_and_ai_summary_in_export(self):
        export = _sample_export()
        self.assertEqual(export["paper"]["authors"], ["Alice Smith"])
        self.assertIn("简单方法", (export["ai_summary"] or {})["summary"]["overview"])

    def test_image_context_carries_notes_organization(self):
        """第三栏「AI 整理笔记」的数据源：面板整理 → Agent 报告 → 原样笔记。"""
        from backend.agents.skills.note_out_image import _image_context

        export = _sample_export()
        export["report"] = {"notes_analysis": {"overview": "笔记总览",
                                               "themes": [], "connections": [],
                                               "questions": [], "next": []}}
        ctx = json.loads(_image_context(export, "", "light"))
        self.assertEqual(ctx["notes_analysis_source"], "ai_summary")
        self.assertIn("简单方法", ctx["ai_summary"]["overview"])

        export["ai_summary"] = None                      # 面板没整理过
        ctx = json.loads(_image_context(export, "", "light"))
        self.assertEqual(ctx["notes_analysis_source"], "report.notes_analysis")
        self.assertEqual(ctx["notes_analysis"]["overview"], "笔记总览")
        self.assertEqual(ctx["notes_rows"], [])

        export["report"] = {}                            # 两份整理都没有
        ctx = json.loads(_image_context(export, "", "light"))
        self.assertEqual(ctx["notes_analysis_source"], "notes")
        self.assertTrue(ctx["notes_rows"])               # 给原样笔记兜底

    def test_image_context_pairs_notes_with_sentences(self):
        """第二栏：原句与笔记必须成对给出（左原文 / 右笔记），并带上同页要点。"""
        from backend.agents.skills.note_out_image import _image_context

        export = _sample_export()
        export["notes"]["free_notes"] = [{"title": "想法", "text": "独立笔记内容"}]
        export["report"] = {"notes_analysis": {"themes": [
            {"title": "方法", "points": ["P1：要点一", "P2：另一页的要点"]}]}}

        ctx = json.loads(_image_context(export, "", "light"))
        pairs = ctx["note_pairs"]
        self.assertEqual(pairs[0]["kind"], "sentence")
        self.assertTrue(pairs[0]["label"].startswith("P1"))
        self.assertEqual(pairs[0]["sentence"], "Main content starts here.")
        self.assertEqual(pairs[0]["note"], "my note")
        self.assertEqual(pairs[-1]["kind"], "free")
        self.assertEqual(pairs[-1]["note"], "独立笔记内容")
        # 页内要点按页归组，第二栏只引用同一页的分析
        self.assertEqual(ctx["page_points"]["P1"], ["P1：要点一"])
        self.assertEqual(ctx["page_points"]["P2"], ["P2：另一页的要点"])
        # 配色直接用给定色板，避免暗色下字看不见
        self.assertEqual(ctx["theme"], "light")
        self.assertEqual(ctx["theme_colors"]["ink"], "#1f2430")
        dark = json.loads(_image_context(export, "", "dark"))
        self.assertEqual(dark["theme_colors"]["bg"], "#22262d")
        # 页眉时间与机械导出同一口径（YYYY-MM-DD HH:MM）
        self.assertEqual(ctx["generated_at_text"],
                         str(export["exported_at"]).replace("T", " ")[:16])
        self.assertEqual(ctx["export_mode_text"], "机械导出")
        self.assertIn("references_count", ctx)

    def test_agent_info_column_includes_report_summary_and_reference_stats(self):
        from backend.notes_out.note_out_image import _build_info_col, _content_cells

        export = _sample_export()
        export["mode"] = "agent"
        export["report"] = {
            "summary": "两条参考文献已核查，其中一条需要复核。",
            "references_stats": {"total": 2, "checked": 2, "matched": 1,
                                 "not_found": 1, "unverified": 0},
        }
        col = _build_info_col(export, 52)
        rendered_text = " ".join(
            str(value) for row in col.rows for value in row.values())

        self.assertIn("两条参考文献已核查", rendered_text)
        self.assertIn("文献核验", rendered_text)
        self.assertGreater(_content_cells(export)[0],
                           _content_cells({**export, "mode": "mechanical"})[0])

    def test_segments_split_math_and_ignore_currency(self):
        from backend.notes_out.note_out_image import _segments

        self.assertEqual(_segments("a $x^2$ b"),
                         [(False, "a "), (True, "$x^2$"), (False, " b")])
        # 货币 / 无数学符号：不当公式
        self.assertEqual(_segments("cost $5 and $10"), [(False, "cost $5 and $10")])

    @unittest.skipUnless(HAVE_MPL, "未安装 matplotlib")
    def test_math_formula_and_bad_formula_do_not_crash(self):
        from backend.notes_out.note_out_image import render

        export = _sample_export()
        export["notes"]["sentence_notes"][0]["note"] = (
            "公式 $\\alpha_{i} = \\mathrm{softmax}(QK^{T}/\\sqrt{d_k})$ 以及 “$5 和 $10”")
        export["ai_summary"]["summary"]["themes"][0]["points"] = [
            "$\\begin{matrix}a&b\\\\c&d\\end{matrix}$ 不支持时应回退原文"]
        png = render(export, theme="dark", dpi=96)
        self.assertTrue(png.startswith(b"\x89PNG"))

    @unittest.skipUnless(HAVE_MPL, "未安装 matplotlib")
    def test_render_png_for_both_themes(self):
        from backend.notes_out.note_out_image import render

        export = _sample_export()
        for theme in ("light", "dark"):
            png = render(export, theme=theme, dpi=96)
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(png), 10000)

    @unittest.skipUnless(HAVE_MPL, "未安装 matplotlib")
    def test_canvas_keeps_16_9(self):
        """内容多少都要保持 16:9（靠加宽，不是拉高）。"""
        import io as _io

        from PIL import Image

        from backend.notes_out.note_out_image import render

        export = _sample_export()
        export["notes"]["sentence_notes"] = [
            {"id": f"n{i}", "block_id": "p0b5#0", "page": 1, "sentence_index": 0,
             "sentence": "Original sentence %d. " % i * 4,
             "note": "笔记内容 %d。" % i * 6}
            for i in range(12)]
        export["ai_summary"] = {"summary": {
            "overview": "很长的总览。" * 200,
            "themes": [{"title": "主题", "points": ["要点。" * 60] * 5}],
            "connections": [], "questions": [], "next": []}, "at": "2026-11-19T10:00:00"}

        im = Image.open(_io.BytesIO(render(export, theme="light", dpi=96)))
        ratio = im.size[0] / im.size[1]
        self.assertAlmostEqual(ratio, 16 / 9, delta=0.02)


class _FakeImageLlm:
    def __init__(self):
        self.calls = 0

    def chat_completion(self, system, messages, *, tools=None, temperature=0.3):
        self.calls += 1
        if self.calls == 1:
            return {"content": None, "tool_calls": [{
                "id": "draw-1",
                "function": {
                    "name": "run_python",
                    "arguments": json.dumps({"code": "print('draw')"}),
                },
            }]}
        return {"content": "图片已生成。"}


def _load_math_helper():
    """执行沙盒辅助模块（用假的 mathtext，本机没装 matplotlib 也能测）。"""
    import sys
    import types

    from backend.agents.skills.note_out_image import _SANDBOX_UTILS

    fake_mathtext = types.ModuleType("matplotlib.mathtext")

    class _Parser:
        def parse(self, s, dpi=72):
            return (0, 0, 0, [], None)

    fake_mathtext.MathTextParser = lambda backend: _Parser()
    fake_mpl = types.ModuleType("matplotlib")
    fake_mpl.mathtext = fake_mathtext

    namespace = {}
    with patch.dict(sys.modules, {"matplotlib": fake_mpl,
                                  "matplotlib.mathtext": fake_mathtext}):
        exec(compile(_SANDBOX_UTILS, "note_image_utils.py", "exec"), namespace)
    return namespace


class NoteOutImageAgentTest(unittest.TestCase):
    def test_agent_runs_generated_code_and_returns_png(self):
        from backend.agents.skills.note_out_image import run_note_out_image_agent

        export = _sample_export()
        export["mode"] = "agent"
        export["report"] = {"summary": "文献核查摘要"}
        png = b"\x89PNG\r\n\x1a\nimage"
        llm = _FakeImageLlm()
        with patch("backend.agents.skills.note_out_image._sandbox_request",
                   return_value={"ok": True, "outputs": [{
                       "name": "note.png", "encoding": "base64",
                       "data": base64.b64encode(png).decode("ascii"),
                   }]}) as sandbox:
            result = run_note_out_image_agent(
                llm, export=export, instructions="改用深色主题")

        self.assertEqual(result, png)
        self.assertEqual(llm.calls, 2)
        payload = sandbox.call_args.args[1]
        self.assertIn("文献核查摘要", payload)
        self.assertIn("my note", payload)
        # 第二栏成对数据也写进了沙盒文件，代码可直接读
        sandbox_json = json.loads(payload)
        pairs = sandbox_json["image_hints"]["note_pairs"]
        self.assertEqual(pairs[0]["note"], "my note")
        self.assertEqual(pairs[0]["sentence"], "Main content starts here.")
        # 配色 / 时间也可从沙盒文件读到（代码里不用猜顶层键）
        hints = sandbox_json["image_hints"]
        self.assertEqual(hints["theme"], "light")
        self.assertIn("ink", hints["theme_colors"])
        self.assertIn("generated_at_text", hints)
        self.assertIn("references_count", hints)

    def test_agent_rejects_success_without_note_png(self):
        from backend.agents.skills.note_out_image import run_note_out_image_agent
        from backend.agents.llm import LlmUnavailable

        llm = _FakeImageLlm()
        with patch("backend.agents.skills.note_out_image._sandbox_request",
                   return_value={"ok": True, "outputs": []}):
            with self.assertRaisesRegex(LlmUnavailable, "没有生成"):
                run_note_out_image_agent(llm, export=_sample_export())

    def test_read_notes_tool_returns_paired_notes(self):
        """第二栏数据由 read_notes 工具提供（原句与笔记成对，含同页分析）。"""
        from backend.agents.skills.note_out_image import run_note_out_image_agent

        png = b"\x89PNG\r\n\x1a\nimage"
        seen = {}

        class _Llm:
            def __init__(self):
                self.calls = 0

            def chat_completion(self, system, messages, *, tools=None, temperature=0.3):
                self.calls += 1
                if self.calls == 1:
                    return {"content": None, "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "read_notes",
                                     "arguments": json.dumps({"page": 1})}}]}
                if self.calls == 2:
                    seen["tool"] = [m for m in messages if m.get("role") == "tool"]
                    return {"content": None, "tool_calls": [{
                        "id": "c2", "type": "function",
                        "function": {"name": "run_python",
                                     "arguments": json.dumps({"code": "print('draw')"})}}]}
                return {"content": "完成"}

        with patch("backend.agents.skills.note_out_image._sandbox_request",
                   return_value={"ok": True, "outputs": [{
                       "name": "note.png", "encoding": "base64",
                       "data": base64.b64encode(png).decode("ascii"),
                   }]}):
            result = run_note_out_image_agent(_Llm(), export=_sample_export())

        self.assertEqual(result, png)
        payload = json.loads(seen["tool"][0]["content"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["notes"][0]["sentence"], "Main content starts here.")
        self.assertEqual(payload["notes"][0]["note"], "my note")

    def test_rejects_path_effects_in_generated_code(self):
        """白字 + 白描边会把字形糊成白块：带 path_effects 的代码直接打回、不进沙盒。"""
        from backend.agents.llm import LlmUnavailable
        from backend.agents.skills.note_out_image import run_note_out_image_agent

        seen = {}
        code = ("import matplotlib.patheffects as pe\n"
                "ax.text(0, 0, 'x', path_effects=[pe.withStroke(linewidth=3)])")

        class _Llm:
            def __init__(self):
                self.calls = 0

            def chat_completion(self, system, messages, *, tools=None, temperature=0.3):
                self.calls += 1
                if self.calls == 1:
                    return {"content": None, "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "run_python",
                                     "arguments": json.dumps({"code": code})}}]}
                seen["tool"] = [m for m in messages if m.get("role") == "tool"]
                return {"content": "完成"}

        with patch("backend.agents.skills.note_out_image._sandbox_request") as sandbox:
            with self.assertRaises(LlmUnavailable):
                run_note_out_image_agent(_Llm(), export=_sample_export())

        sandbox.assert_not_called()
        feedback = json.loads(seen["tool"][0]["content"])
        self.assertFalse(feedback["ok"])
        self.assertIn("path_effects", feedback["message"])

    def test_flat_png_check_is_safe(self):
        """对比度校验不能因为非法数据 / 缺依赖而报错（只当“正常”放行）。"""
        from backend.agents.skills.note_out_image import _is_flat_png

        self.assertFalse(_is_flat_png(b"not-a-png"))
        self.assertFalse(_is_flat_png(b"\x89PNG\r\n\x1a\nbroken"))

    def test_sandbox_request_sends_input_file_and_optional_key(self):
        from backend.agents.skills.note_out_image import _sandbox_request

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True}

        with patch("backend.agents.skills.note_out_image.env_value",
                   side_effect=lambda key: {
                       "DRAW_SANDBOX_URL": "http://sandbox:8090/v1",
                       "DRAW_SANDBOX_API_KEY": "sandbox-key",
                   }[key]):
            with patch("backend.agents.skills.note_out_image.httpx.post",
                       return_value=FakeResponse()) as post:
                self.assertEqual(_sandbox_request("print(1)", '{"x":1}'),
                                 {"ok": True})

        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://sandbox:8090/v1/run")
        self.assertEqual(kwargs["headers"]["X-API-Key"], "sandbox-key")
        files = kwargs["json"]["files"]
        self.assertEqual([f["name"] for f in files],
                         ["note_export.json", "note_image_utils.py"])
        self.assertEqual(files[0]["content"], '{"x":1}')
        self.assertIn("def fix", files[1]["content"])

    def test_math_helper_normalizes_double_dollars(self):
        """mathtext 不认 $$…$$：辅助模块必须转成行内 $…$。"""
        fix = _load_math_helper()["fix"]
        self.assertEqual(fix("a $$x^2$$ b"), "a $x^2$ b")
        self.assertEqual(fix("$$a$$ 与 $$b$$"), "$a$ 与 $b$")
        self.assertEqual(fix(r"行内 \(y\) 与 \[z\]"), "行内 $y$ 与 $z$")
        self.assertEqual(fix("没有公式"), "没有公式")

    def test_math_helper_text_of_handles_summary_objects(self):
        """ai_summary / notes_analysis 是对象：text_of 拼成文本，不能直接 .strip()。"""
        helper = _load_math_helper()
        text_of, fix = helper["text_of"], helper["fix"]

        obj = {"overview": "总览一句",
               "themes": [{"title": "主题一", "points": ["要点 A", "要点 B"]}],
               "connections": ["关联一"], "questions": ["疑问一"],
               "next": ["下一步"]}
        out = text_of(obj)
        self.assertIn("总览一句", out)
        self.assertIn("主题一：要点 A；要点 B", out)
        self.assertIn("关联一", out)
        self.assertIn("疑问一", out)
        self.assertIn("下一步", out)
        self.assertIn("\n", out)                     # 多段之间是真换行，不是字面 \n
        self.assertEqual(text_of("纯字符串"), "纯字符串")
        self.assertEqual(text_of(None), "")
        self.assertIn("总览一句", fix(obj))          # fix 也接受对象



if __name__ == "__main__":
    unittest.main()
