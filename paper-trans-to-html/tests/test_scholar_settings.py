"""学术库（OpenAlex / Semantic Scholar）配置：按账号存 + 回落环境变量，及 Agent 透传。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from backend.agents import note_out_agent as agent
from backend.settings import SettingsStore

_ENV_NAMES = ("OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "SCHOLARLY_MAILTO")


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


class _QuietLlm:
    def chat_completion(self, system, messages, *, tools=None, temperature=0.3):
        return {"content": "完成"}

    def chat_json(self, system, user, *, temperature=0.1):
        return {"summary": "报告", "conclusion": "结论",
                "references_review": [], "code_repositories": []}


class ScholarSettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_env = {k: os.environ.get(k) for k in _ENV_NAMES}
        for name in _ENV_NAMES:
            os.environ.pop(name, None)
        # 指到一个不存在的 .env，避免读到开发者本机的 key
        self._old_env_file = os.environ.get("APP_ENV_FILE")
        os.environ["APP_ENV_FILE"] = os.path.join(self._tmp.name, "none.env")
        self.prefs = _Prefs()
        self.store = SettingsStore(self._tmp.name, prefs=self.prefs)

    def tearDown(self):
        for name, value in self._old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if self._old_env_file is None:
            os.environ.pop("APP_ENV_FILE", None)
        else:
            os.environ["APP_ENV_FILE"] = self._old_env_file
        self._tmp.cleanup()

    # ---------- 未配置 ----------
    def test_defaults_are_empty(self):
        merged = self.store.scholar_merged("alice")
        self.assertEqual(merged, {"openalex_key": "", "semantic_scholar_key": "",
                                  "s2_base_url": "https://s2api.ominiai.cn/s2",
                                  "mailto": ""})
        desc = self.store.scholar_describe("alice")
        self.assertFalse(desc["openalex_key_set"])
        self.assertEqual(desc["sources"]["openalex_key"], "none")

    # ---------- 环境变量回落 ----------
    def test_env_fallback_and_source(self):
        os.environ["OPENALEX_API_KEY"] = "env-openalex"
        os.environ["SCHOLARLY_MAILTO"] = "me@example.com"
        merged = self.store.scholar_merged("alice")
        self.assertEqual(merged["openalex_key"], "env-openalex")
        self.assertEqual(merged["mailto"], "me@example.com")
        desc = self.store.scholar_describe("alice")
        self.assertEqual(desc["sources"]["openalex_key"], "env")
        self.assertEqual(desc["sources"]["semantic_scholar_key"], "none")
        self.assertTrue(desc["openalex_key_set"])
        self.assertEqual(desc["openalex_key_masked"], "env-****alex")

    # ---------- 本账号优先 + 打码 ----------
    def test_account_key_wins_and_is_masked(self):
        os.environ["OPENALEX_API_KEY"] = "env-openalex"
        self.store.save_scholar("alice", openalex_key="ziytsI8JZXTeLZlBzyaU88",
                                semantic_scholar_key="sk-abcdefghijkl", user="alice")
        desc = self.store.scholar_describe("alice")
        self.assertEqual(desc["sources"]["openalex_key"], "account")
        self.assertTrue(desc["from_ui"])
        self.assertNotIn("ziytsI8JZXTeLZlBzyaU88", json.dumps(desc, ensure_ascii=False))
        self.assertEqual(desc["openalex_key_masked"],
                         "ziytsI8JZXTeLZlBzyaU88"[:4] + "****" +
                         "ziytsI8JZXTeLZlBzyaU88"[-4:])
        self.assertEqual(self.store.scholar_merged("alice")["openalex_key"],
                         "ziytsI8JZXTeLZlBzyaU88")

    def test_other_account_unaffected(self):
        self.store.save_scholar("alice", openalex_key="alice-key")
        self.assertEqual(self.store.scholar_merged("bob")["openalex_key"], "")
        self.assertFalse(self.store.scholar_describe("bob")["openalex_key_set"])

    # ---------- 只改一项不会把别的清掉 ----------
    def test_save_keeps_untouched_fields(self):
        self.store.save_scholar("alice", openalex_key="k1", mailto="a@b.c")
        self.store.save_scholar("alice", semantic_scholar_key="k2")
        merged = self.store.scholar_merged("alice")
        self.assertEqual(merged["openalex_key"], "k1")
        self.assertEqual(merged["mailto"], "a@b.c")
        self.assertEqual(merged["semantic_scholar_key"], "k2")

    def test_s2_base_url_saved_and_validated(self):
        self.store.save_scholar("alice", s2_base_url="https://proxy.example/s2")
        self.assertEqual(self.store.scholar_describe("alice")["s2_base_url"],
                         "https://proxy.example/s2")
        with self.assertRaises(ValueError):
            self.store.save_scholar("alice", s2_base_url="proxy.example/s2")

    # ---------- 清空 → 回落环境变量 ----------
    def test_clear_falls_back_to_env(self):
        os.environ["OPENALEX_API_KEY"] = "env-openalex"
        self.store.save_scholar("alice", openalex_key="account-key")
        self.store.clear_scholar("alice")
        self.assertEqual(self.store.scholar_merged("alice")["openalex_key"],
                         "env-openalex")
        self.assertFalse(self.store.scholar_describe("alice")["from_ui"])


class ScholarAgentConfigTest(unittest.TestCase):
    """Agent 侧：账号配置优先于环境变量，并真的传到检索函数。"""

    def setUp(self):
        self._old_env = {k: os.environ.get(k) for k in _ENV_NAMES}
        for name in _ENV_NAMES:
            os.environ.pop(name, None)
        self._old_env_file = os.environ.get("APP_ENV_FILE")
        os.environ["APP_ENV_FILE"] = os.path.join(tempfile.gettempdir(), "none.env")

    def tearDown(self):
        for name, value in self._old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if self._old_env_file is None:
            os.environ.pop("APP_ENV_FILE", None)
        else:
            os.environ["APP_ENV_FILE"] = self._old_env_file

    def test_account_cfg_beats_env(self):
        os.environ[agent.CFG_OPENALEX] = "env-key"
        self.assertEqual(agent._cfg_value(agent.CFG_OPENALEX, None), "env-key")
        self.assertEqual(
            agent._cfg_value(agent.CFG_OPENALEX, {agent.CFG_OPENALEX: "account-key"}),
            "account-key")

    def test_scholar_cfg_reaches_verify_reference(self):
        seen = {}

        def fake_verify(rid, **kwargs):
            seen["cfg"] = kwargs.get("cfg")
            return {"ok": True, "reference_id": rid, "status": "verified",
                    "message": "stub", "best": {"title": "t", "similarity": 0.99},
                    "candidates": [], "errors": []}

        doc = {"references": {"entries": [
            {"index": 1, "text": "[1] A. Smith. First cited paper. 2020."}]}}
        scholar = {agent.CFG_OPENALEX: "account-key"}
        with mock.patch.object(agent, "_verify_reference", fake_verify):
            report = agent.run_note_out_agent(
                _QuietLlm(), paper={"title": "P", "num_pages": 1},
                sections=doc, notes={}, verify_all=True, scholar=scholar)
        self.assertEqual(seen["cfg"], scholar)
        self.assertEqual(report["references_stats"]["matched"], 1)

    def test_probe_scholar_shape(self):
        ok = lambda q, rows=3, cfg=None: ([{"title": "Attention is all you need"}], "")
        with mock.patch.object(agent, "_openalex_search", ok), \
                mock.patch.object(agent, "_semantic_search", ok), \
                mock.patch.object(agent, "_arxiv_search", ok), \
                mock.patch.object(agent, "_crossref_search", ok):
            result = agent.probe_scholar({agent.CFG_OPENALEX: "k"})
        self.assertTrue(result["ok"])
        self.assertEqual([c["name"] for c in result["checks"]],
                         ["openalex", "semantic_scholar", "arxiv", "crossref"])
        self.assertTrue(result["checks"][0]["keyed"])
        self.assertIsNone(result["checks"][2]["keyed"])      # arXiv 不需要 key
        self.assertEqual(result["checks"][1]["base"], agent.S2_DEFAULT_BASE)

    def test_s2_default_base_consistent_with_settings(self):
        from backend import settings as st
        self.assertEqual(agent.S2_DEFAULT_BASE, st.S2_DEFAULT_BASE)

    def _s2_headers(self, base: str, key: str = "sk-x"):
        seen = {}

        def fake_json(url, params=None, timeout=8.0, *, headers=None, retries=1,
                      source=""):
            seen["url"] = url
            seen["headers"] = dict(headers or {})
            return {"data": []}, ""

        cfg = {agent.CFG_S2: key, agent.CFG_S2_BASE: base}
        with mock.patch.object(agent, "_s2_gate"), \
                mock.patch.object(agent, "_http_json", fake_json):
            agent._semantic_search("attention", 1, cfg)
        return seen

    def test_s2_proxy_uses_bearer_token(self):
        seen = self._s2_headers("https://proxy.example/s2/")
        self.assertEqual(seen["url"],
                         "https://proxy.example/s2/graph/v1/paper/search")
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer sk-x")
        self.assertNotIn("x-api-key", seen["headers"])

    def test_s2_official_uses_x_api_key(self):
        seen = self._s2_headers("https://api.semanticscholar.org")
        self.assertEqual(seen["url"],
                         "https://api.semanticscholar.org/graph/v1/paper/search")
        self.assertEqual(seen["headers"].get("x-api-key"), "sk-x")
        self.assertNotIn("Authorization", seen["headers"])


if __name__ == "__main__":
    unittest.main()
