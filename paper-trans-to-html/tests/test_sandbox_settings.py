"""绘图沙盒设置：按账号存请求地址 + 回落环境变量 / 内置默认，及技能透传。"""
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from backend.settings import (SANDBOX_DEFAULT_URL, SettingsStore,
                              normalize_sandbox_url)


class _Prefs:
    """只实现 `get_prefs` / `set_prefs`，语义与 `AuthStore` 一致（浅合并）。"""

    def __init__(self):
        self.data: dict = {}

    def get_prefs(self, username):
        return dict(self.data.get(username) or {})

    def set_prefs(self, username, patch):
        cur = dict(self.data.get(username) or {})
        cur.update(patch or {})
        self.data[username] = cur
        return cur


class SandboxSettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_env = os.environ.get("DRAW_SANDBOX_URL")
        os.environ.pop("DRAW_SANDBOX_URL", None)
        # 指到一个不存在的 .env，避免读到开发者本机的地址
        self._old_env_file = os.environ.get("APP_ENV_FILE")
        os.environ["APP_ENV_FILE"] = os.path.join(self._tmp.name, "none.env")
        self.prefs = _Prefs()
        self.store = SettingsStore(self._tmp.name, prefs=self.prefs)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("DRAW_SANDBOX_URL", None)
        else:
            os.environ["DRAW_SANDBOX_URL"] = self._old_env
        if self._old_env_file is None:
            os.environ.pop("APP_ENV_FILE", None)
        else:
            os.environ["APP_ENV_FILE"] = self._old_env_file
        self._tmp.cleanup()

    def test_normalize_sandbox_url(self):
        self.assertEqual(normalize_sandbox_url("http://host:8090"),
                         "http://host:8090/v1/run")
        self.assertEqual(normalize_sandbox_url("http://host:8090/"),
                         "http://host:8090/v1/run")
        self.assertEqual(normalize_sandbox_url("http://host:8090/v1"),
                         "http://host:8090/v1/run")
        self.assertEqual(normalize_sandbox_url("http://host:8090/v1/run"),
                         "http://host:8090/v1/run")
        self.assertEqual(normalize_sandbox_url("  "), "")

    # ---------- 默认设置不变 ----------
    def test_defaults_unchanged(self):
        merged = self.store.sandbox_merged("alice")
        self.assertEqual(merged["url"], SANDBOX_DEFAULT_URL)
        self.assertEqual(merged["raw_url"], "")
        self.assertEqual(merged["source"], "default")
        desc = self.store.sandbox_describe("alice")
        self.assertFalse(desc["from_ui"])
        self.assertEqual(desc["source_text"], "内置默认")

    def test_env_fallback(self):
        os.environ["DRAW_SANDBOX_URL"] = "http://env-sandbox:8090"
        merged = self.store.sandbox_merged("alice")
        self.assertEqual(merged["source"], "env")
        self.assertEqual(merged["url"], "http://env-sandbox:8090/v1/run")
        self.assertEqual(self.store.sandbox_url("alice"),
                         "http://env-sandbox:8090/v1/run")

    # ---------- 本账号优先 ----------
    def test_account_url_wins_and_is_normalized(self):
        os.environ["DRAW_SANDBOX_URL"] = "http://env-sandbox:8090"
        self.store.save_sandbox("alice", "http://draw-sandbox:8091", user="alice")
        desc = self.store.sandbox_describe("alice")
        self.assertTrue(desc["from_ui"])
        self.assertEqual(desc["source"], "account")
        self.assertEqual(desc["raw_url"], "http://draw-sandbox:8091")
        self.assertEqual(desc["url"], "http://draw-sandbox:8091/v1/run")
        self.assertEqual(desc["updated_by"], "alice")

    def test_other_account_unaffected(self):
        self.store.save_sandbox("alice", "http://alice-sandbox:8090/v1/run")
        self.assertEqual(self.store.sandbox_merged("bob")["source"], "default")
        self.assertFalse(self.store.sandbox_describe("bob")["from_ui"])

    def test_save_validates_scheme(self):
        with self.assertRaises(ValueError):
            self.store.save_sandbox("alice", "draw-sandbox:8090")
        with self.assertRaises(ValueError):
            self.store.save_sandbox("alice", "http://host:8090/" + "a" * 400)

    def test_empty_url_and_clear_fall_back(self):
        self.store.save_sandbox("alice", "http://draw-sandbox:8091")
        self.store.save_sandbox("alice", "")
        self.assertEqual(self.store.sandbox_merged("alice")["source"], "default")
        self.store.save_sandbox("alice", "http://draw-sandbox:8091")
        self.store.clear_sandbox("alice")
        self.assertEqual(self.store.sandbox_merged("alice")["source"], "default")


class SandboxRequestTest(unittest.TestCase):
    def _fake_response(self):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True}

        return FakeResponse()

    def test_explicit_url_overrides_env(self):
        from backend.agents.skills.note_out_image import _sandbox_request

        with patch("backend.agents.skills.note_out_image.env_value",
                   return_value="http://env-sandbox:8090/v1/run"):
            with patch("backend.agents.skills.note_out_image.httpx.post",
                       return_value=self._fake_response()) as post:
                _sandbox_request("print(1)", "{}", "http://acc-sandbox:8092")

        self.assertEqual(post.call_args.args[0],
                         "http://acc-sandbox:8092/v1/run")

    def test_no_explicit_url_keeps_env_default(self):
        from backend.agents.skills.note_out_image import _sandbox_request

        with patch("backend.agents.skills.note_out_image.env_value",
                   return_value="http://env-sandbox:8090/v1"):
            with patch("backend.agents.skills.note_out_image.httpx.post",
                       return_value=self._fake_response()) as post:
                _sandbox_request("print(1)", "{}")

        self.assertEqual(post.call_args.args[0], "http://env-sandbox:8090/v1/run")

    def test_agent_passes_sandbox_url_through(self):
        from backend.agents.skills.note_out_image import run_note_out_image_agent

        png = b"\x89PNG\r\n\x1a\nimage"

        class _Llm:
            calls = 0

            def chat_completion(self, system, messages, *, tools=None, temperature=0.3):
                _Llm.calls += 1
                if _Llm.calls == 1:
                    return {"content": "", "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "run_python",
                                     "arguments": json.dumps({"code": "print('draw')"})},
                    }]}
                return {"content": "完成"}

        with patch("backend.agents.skills.note_out_image._sandbox_request",
                   return_value={"ok": True, "outputs": [{
                       "name": "note.png", "encoding": "base64",
                       "data": base64.b64encode(png).decode("ascii"),
                   }]}) as sandbox:
            run_note_out_image_agent(_Llm(), export={"paper": {}},
                                     sandbox_url="http://acc-sandbox:8092/v1/run")

        self.assertEqual(sandbox.call_args.args[2], "http://acc-sandbox:8092/v1/run")


if __name__ == "__main__":
    unittest.main()
