import hashlib
import tempfile
import unittest
from unittest.mock import patch

from backend.ai_read import (add_summary_item, locate_page, locate_text_block,
                             notes_fingerprint, reply_agent_stream)
from backend.records import Store, anno_with_authors
from backend.translate import Translator, probe_llm


class AiAgentNotesTest(unittest.TestCase):
    def test_add_summary_item_appends_point_to_matching_theme(self):
        summary = {"overview": "总览一句", "themes": [
            {"title": "多头注意力", "points": ["P5 公式 (1)"]},
            {"title": "消融", "points": []},
        ], "connections": [], "questions": [], "next": []}

        result, merged = add_summary_item(
            summary, {"section": "themes", "title": "多头注意力",
                      "text": "P5：h=8、d_k=d_v=64"})

        self.assertTrue(result["ok"])
        self.assertEqual(len(merged["themes"]), 2)          # 没有多出一组主题
        self.assertEqual(merged["themes"][0]["points"],
                         ["P5 公式 (1)", "P5：h=8、d_k=d_v=64"])
        self.assertEqual(summary["themes"][0]["points"], ["P5 公式 (1)"])  # 不改入参

    def test_add_summary_item_creates_theme_and_appends_list_items(self):
        result, merged = add_summary_item(
            None, {"section": "themes", "title": "梳理卡片",
                   "text": "先记一条"})
        self.assertTrue(result["ok"])
        self.assertEqual(merged["themes"], [{"title": "梳理卡片",
                                             "points": ["先记一条"]}])

        for key in ("connections", "questions", "next"):
            result, merged = add_summary_item(
                merged, {"section": key, "text": f"{key} 一条"})
            self.assertTrue(result["ok"])
            self.assertEqual(merged[key], [f"{key} 一条"])

    def test_add_summary_item_appends_overview(self):
        result, merged = add_summary_item(
            {"overview": "原有的总览。", "themes": [], "connections": [],
             "questions": [], "next": []},
            {"section": "overview", "text": "补一句新的总结。"})

        self.assertTrue(result["ok"])
        self.assertEqual(merged["overview"], "原有的总览。 补一句新的总结。")

    def test_add_summary_item_rejects_bad_section_and_empty_text(self):
        summary = {"overview": "总览", "themes": [], "connections": [],
                   "questions": [], "next": []}

        bad_section, _ = add_summary_item(summary, {"section": "notes", "text": "x"})
        self.assertFalse(bad_section["ok"])

        empty, _ = add_summary_item(summary, {"section": "next", "text": "   "})
        self.assertFalse(empty["ok"])

    def test_add_summary_item_reports_when_section_is_full(self):
        summary = {"overview": "", "themes": [],
                   "connections": [f"第 {i} 条" for i in range(20)],
                   "questions": [], "next": []}

        result, merged = add_summary_item(summary, {"section": "connections",
                                                    "text": "写不进去的一条"})

        self.assertFalse(result["ok"])
        self.assertIn("已满", result["message"])
        self.assertEqual(len(merged["connections"]), 20)    # 旧内容也没被破坏

    def test_json_store_persists_independent_notes_and_counts(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.ensure_record({"id": "paper", "owner": "alice"})

            created = store.apply_anno("paper", "add_free_note", title="Idea",
                                       text="A standalone note", by="alice")
            note = created["free_notes"][0]
            self.assertEqual(note["title"], "Idea")
            self.assertEqual(note["text"], "A standalone note")
            self.assertEqual(store.get_record("paper")["notes"], 1)

            store.apply_anno("paper", "update_free_note", note_id=note["id"],
                             title="Updated", text="Edited note", by="bob")
            reopened = Store(root).get_annotations("paper")
            self.assertEqual(reopened["free_notes"][0]["title"], "Updated")
            self.assertEqual(reopened["free_notes"][0]["text"], "Edited note")
            self.assertEqual(anno_with_authors(
                reopened, {"shared": [{"user": "bob"}]})["free_notes"][0]["by"], "bob")

            store.apply_anno("paper", "remove_free_note", note_id=note["id"])
            self.assertEqual(store.get_annotations("paper")["free_notes"], [])
            self.assertEqual(store.get_record("paper")["notes"], 0)

    def test_agent_executes_tool_then_sends_tool_result_back_to_model(self):
        class FakeTranslator:
            def __init__(self):
                self.messages_seen = []

            def chat_completion(self, system, messages, *, tools=None, temperature=0.3):
                self.messages_seen.append(list(messages))
                if len(self.messages_seen) == 1:
                    return {
                        "content": None,
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "add_note_to_summary",
                                "arguments": '{"section":"next","text":"Remember this"}',
                            },
                        }],
                    }
                return {"content": "笔记已添加。"}

            def chat_completion_stream(self, system, messages, *, tools=None, temperature=0.3):
                result = self.chat_completion(system, messages, tools=tools,
                                              temperature=temperature)
                if result.get("content"):
                    yield {"type": "delta", "text": result["content"]}
                yield {"type": "completion", "message": result}

        translator = FakeTranslator()
        executed = []
        events = list(reply_agent_stream(
            translator, title="Paper", notes=[], summary=None, history=[],
            message="把这个记下来", execute_tool=lambda name, args: (
                executed.append((name, args)) or {
                    "ok": True, "message": "saved",
                    "action": {"type": "notes_changed"},
                }),
        ))

        self.assertEqual(executed, [("add_note_to_summary",
                                     {"section": "next", "text": "Remember this"})])
        self.assertTrue(any(event["type"] == "action" for event in events))
        self.assertEqual("".join(event.get("text", "") for event in events), "笔记已添加。")
        self.assertTrue(any(row["role"] == "tool" for row in translator.messages_seen[1]))

    def test_agent_requests_confirmation_before_navigating(self):
        class FakeTranslator:
            def __init__(self):
                self.responses = iter([
                    {
                        "content": None,
                        "tool_calls": [{
                            "id": "navigate-1",
                            "type": "function",
                            "function": {
                                "name": "navigate_to_content",
                                "arguments": '{"target":"paper","page":19}',
                            },
                        }],
                    },
                    {"content": "要定位到 P19 吗？"},
                ])

            def chat_completion_stream(self, *_args, **_kwargs):
                response = next(self.responses)
                if response.get("content"):
                    yield {"type": "delta", "text": response["content"]}
                yield {"type": "completion", "message": response}

        events = list(reply_agent_stream(
            FakeTranslator(), title="Paper", notes=[], summary=None, history=[],
            message="这部分在第几页？",
            execute_tool=lambda *_: {
                "ok": True, "message": "已找到论文第 19 页，等待用户确认",
                "confirmation": {"type": "navigate", "target": "paper", "page": 19},
            },
        ))

        self.assertEqual([event["type"] for event in events],
                         ["tool", "tool", "delta", "confirm_navigation"])
        self.assertNotIn("action", [event["type"] for event in events])
        self.assertEqual(events[-1]["action"]["page"], 19)

    def test_agent_navigates_only_after_confirming_turn_calls_tool(self):
        class FakeTranslator:
            def __init__(self):
                self.responses = iter([
                    {
                        "content": None,
                        "tool_calls": [{
                            "id": "navigate-2",
                            "type": "function",
                            "function": {
                                "name": "navigate_to_content",
                                "arguments": '{"target":"paper","page":19}',
                            },
                        }],
                    },
                    {"content": "好的，已定位到 P19。"},
                ])
                self.system = ""

            def chat_completion_stream(self, system, *_args, **_kwargs):
                self.system = system
                response = next(self.responses)
                if response.get("content"):
                    yield {"type": "delta", "text": response["content"]}
                yield {"type": "completion", "message": response}

        translator = FakeTranslator()
        events = list(reply_agent_stream(
            translator, title="Paper", notes=[], summary=None, history=[],
            message="是的，请跳转。",
            pending_navigation={"target": "paper", "page": 19},
            navigation_answer="yes",
            execute_tool=lambda _name, _args: {
                "ok": True, "message": "用户已回答，正在定位",
                "action": {"type": "navigate", "target": "paper", "page": 19},
            },
        ))

        self.assertIn("用户通过确认按钮明确选择：yes", translator.system)
        self.assertTrue(any(event.get("action", {}).get("page") == 19
                            for event in events if event["type"] == "action"))

    def test_locate_page_matches_page_content(self):
        doc = {"pages": [
            {"texts": [{"text": "Introduction and prior work"}]},
            {"texts": [{"text": "We evaluate the proposed method on ImageNet"}]},
        ]}

        self.assertEqual(locate_page(doc, "evaluate proposed method ImageNet"), 2)
        self.assertIsNone(locate_page(doc, "no matching subject"))

    def test_locate_text_block_matches_relevant_text_on_page(self):
        doc = {"pages": [
            {"texts": [
                {"id": "p1b0", "text": "Introduction and prior work"},
                {"id": "p1b1", "text": "We evaluate the proposed method on ImageNet"},
            ]},
        ]}

        self.assertEqual(locate_text_block(doc, 1, "proposed method ImageNet"), "p1b1")
        self.assertIsNone(locate_text_block(doc, 1, "unrelated subject"))

    def test_note_fingerprint_keeps_old_linked_note_format_and_tracks_free_notes(self):
        linked = [{"block_id": "b1#0", "text": "note"}]
        old_fingerprint = hashlib.sha1("b1#0\x01note".encode()).hexdigest()[:16]
        self.assertEqual(notes_fingerprint(linked), old_fingerprint)

        free = [{"id": "n1", "title": "Idea", "text": "note"}]
        self.assertNotEqual(notes_fingerprint(free), notes_fingerprint([
            {"id": "n1", "title": "Edited", "text": "note"},
        ]))

    def test_translator_preserves_tool_protocol_messages(self):
        translator = Translator({"backend": "openai", "api_key": "test-key"})
        requests = []

        def fake_post(body):
            requests.append(body)
            return {"choices": [{"message": {"content": "Done"}}]}

        translator._post_chat_payload = fake_post
        translator.chat_completion("system", [
            {"role": "user", "content": "save a note"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function",
                 "function": {"name": "add_note_to_summary", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
        ], tools=[{"type": "function"}])

        self.assertEqual(
            [message["role"] for message in requests[0]["messages"]],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(requests[0]["messages"][2]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(requests[0]["messages"][3]["tool_call_id"], "call-1")
        self.assertIn("tools", requests[0])

    def test_chat_completion_uses_bearer_authentication_header(self):
        translator = Translator({"backend": "openai", "api_key": "test-key"})

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "ok"}}]}

        with patch("backend.translate.httpx.post", return_value=FakeResponse()) as post:
            translator._post_chat_payload({"model": "test"})

        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"],
                         "Bearer test-key")

    def test_llm_connection_probe_uses_bearer_authentication_header(self):
        class FakeResponse:
            status_code = 200

        with patch("backend.translate.httpx.post", return_value=FakeResponse()) as post:
            self.assertIn("连接成功", probe_llm("https://llm.example", "test-key", "test-model"))

        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"],
                         "Bearer test-key")

    def test_translator_streams_text_and_reassembles_tool_call_arguments(self):
        translator = Translator({"backend": "openai", "api_key": "test-key"})
        stream_calls = []

        class FakeResponse:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"Saving ","tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"add_note_to_summary","arguments":"{\\"text\\":"}}]}}]}'
                yield 'data: {"choices":[{"delta":{"content":"note","tool_calls":[{"index":0,"function":{"arguments":"\\"hello\\"}"}}]}}]}'
                yield "data: [DONE]"

        def fake_stream(*args, **kwargs):
            stream_calls.append(kwargs)
            return FakeResponse()

        with patch("backend.translate.httpx.stream", side_effect=fake_stream):
            events = list(translator.chat_completion_stream(
                "system", [{"role": "user", "content": "save"}], tools=[{"type": "function"}]))

        self.assertEqual(stream_calls[0]["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta"), "Saving note")
        completion = next(e["message"] for e in events if e["type"] == "completion")
        self.assertEqual(completion["tool_calls"][0]["id"], "call-1")
        self.assertEqual(completion["tool_calls"][0]["function"]["arguments"],
                         '{"text":"hello"}')


if __name__ == "__main__":
    unittest.main()
