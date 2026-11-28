import unittest

from backend.agents.llm import (DEFAULT_JSON_MAX_TOKENS, LlmClient, LlmUnavailable,
                               extract_json)


def _client(handler):
    """一个不发 HTTP 的 `LlmClient`：用 `handler(body)` 造响应。"""
    client = LlmClient({"api_key": "k", "model": "m"})
    client.post_chat_payload = handler
    return client


def _payload(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


class ExtractJsonTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_object(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_prose_around_object(self):
        self.assertEqual(extract_json('好的，报告如下：\n{"a": {"b": 2}}\n以上。'),
                         {"a": {"b": 2}})

    def test_trailing_comma_and_raw_newline(self):
        self.assertEqual(extract_json('{"a": "line1\nline2", "b": [1, 2,],}'),
                         {"a": "line1\nline2", "b": [1, 2]})

    def test_array_still_supported(self):
        self.assertEqual(extract_json('说明：\n[{"id": 1}]'), [{"id": 1}])

    def test_truncated_json_is_none(self):
        self.assertIsNone(extract_json('{"summary": "被截断的报告'))
        self.assertIsNone(extract_json(""))


class ChatJsonTest(unittest.TestCase):
    def test_uses_explicit_max_tokens(self):
        seen = {}

        def handler(body):
            seen.update(body)
            return _payload('{"ok": true}')

        self.assertEqual(_client(handler).chat_json("sys", "user"), {"ok": True})
        self.assertEqual(seen["max_tokens"], DEFAULT_JSON_MAX_TOKENS)
        self.assertEqual(seen["response_format"], {"type": "json_object"})

    def test_retries_after_truncation(self):
        calls = []

        def handler(body):
            calls.append(body)
            if len(calls) == 1:
                return _payload('{"summary": "被截断', finish="length")
            return _payload('{"summary": "ok"}')

        self.assertEqual(_client(handler).chat_json("sys", "user"),
                         {"summary": "ok"})
        self.assertEqual(len(calls), 2)
        # 第二次把「输出被截断」的提示补进对话，且 JSON 模式仍在
        self.assertEqual(calls[1]["messages"][-1]["role"], "user")
        self.assertIn("截断", calls[1]["messages"][-1]["content"])

    def test_raises_after_two_bad_attempts(self):
        calls = []

        def handler(body):
            calls.append(body)
            return _payload("这不是 JSON")

        with self.assertRaises(LlmUnavailable) as ctx:
            _client(handler).chat_json("sys", "user")
        self.assertIn("不是合法 JSON", str(ctx.exception))
        self.assertEqual(len(calls), 2)

    def test_drops_response_format_when_server_rejects(self):
        calls = []

        def handler(body):
            calls.append(dict(body))
            if "response_format" in body:
                raise LlmUnavailable("模型服务返回 400：response_format unsupported")
            return _payload('{"ok": 1}')

        self.assertEqual(_client(handler).chat_json("sys", "user"), {"ok": 1})
        self.assertIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])


if __name__ == "__main__":
    unittest.main()
