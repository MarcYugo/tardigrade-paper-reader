"""通信桥技能（`backend/agents/skills/agent_comm.py`）：用假 Redis / 假存储验证
取消息 → 动作 → 回包 → ACK 的整条链路，不依赖真实 redis / MongoDB。"""
import base64
import gzip
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from backend import records
from backend.agents.skills import agent_comm as comm

TEST_SECRET = "unit-test-stamp-secret"


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeAuth:
    def __init__(self, users=("alice",), keys=None):
        self.users = list(users)
        self.keys = dict(keys or {})

    def list_users(self):
        return [{"username": u} for u in self.users]

    def public_user(self, name):
        return {"username": name} if name in self.users else None

    def resolve_transfer_key(self, token):
        return self.keys.get(token)


class FakeStore:
    def __init__(self, recs, annos=None, docs=None):
        self.recs = {str(r["id"]): r for r in recs}
        self.annos = annos or {}
        self.docs = docs or {}

    def list_for_user(self, username):
        return [r for r in self.recs.values() if records.perm_of(r, username)]

    def get_record(self, doc_id):
        return self.recs.get(doc_id)

    def get_annotations(self, doc_id):
        return self.annos.get(doc_id) or {"highlights": {}, "notes": [], "free_notes": []}

    def read_doc(self, doc_id):
        if doc_id not in self.docs:
            raise FileNotFoundError(doc_id)
        return self.docs[doc_id]


class FakeRedis:
    """只实现 worker 用到的几个命令：消费组 / 读 / ACK / 回包 / 取消标记 / 结果 blob。"""

    def __init__(self, entries=None):
        self.entries = list(entries or [])
        self.added = []
        self.acked = []
        self.cancelled = set()
        self.kv = {}
        self.hashes = {}
        self.ttl = {}

    def xgroup_create(self, *args, **kwargs):
        raise RuntimeError("BUSYGROUP Consumer Group name already exists")

    def xreadgroup(self, group, consumer, streams, count=1, block=None):
        if not self.entries:
            return []
        items, self.entries = self.entries, []
        return [(list(streams)[0], items)]

    def xack(self, stream, group, msg_id):
        self.acked.append(msg_id)
        return 1

    def xadd(self, stream, fields, maxlen=None, approximate=True):
        self.added.append((stream, dict(fields)))
        return f"1-{len(self.added)}"

    def exists(self, key):
        rid = key.rsplit(":", 1)[-1]
        return 1 if rid in self.cancelled else 0

    # --- 完整结果（agent:result:{rid}）---
    def set(self, key, value, ex=None):
        self.kv[key] = value
        if ex:
            self.ttl[key] = ex
        return True

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(mapping or {})
        return len(mapping or {})

    def expire(self, key, ttl):
        self.ttl[key] = ttl
        return 1

    def pipeline(self, transaction=False):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def set(self, key, value, ex=None):
        self.ops.append(("set", (key, value), {"ex": ex}))
        return self

    def hset(self, key, mapping=None, **kwargs):
        self.ops.append(("hset", (key,), {"mapping": mapping}))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", (key, ttl), {}))
        return self

    def execute(self):
        out = [getattr(self.redis, name)(*args, **kw)
               for name, args, kw in self.ops]
        self.ops = []
        return out


def _entry(rid, intent, payload, trace_id="", owner="alice",
           stamp=True, secret=None):
    """构造一条网关下发的消息：默认带盖章（新信任模型的唯一账号来源）。"""
    body = {"user": owner} if owner else {}
    body.update(payload or {})
    envelope = {"request_id": rid, "from_agent": "research-agent-v1",
                "intent": intent, "payload": body, "wait": True}
    if stamp:
        stamped_at = int(time.time())
        envelope["owner"] = owner
        envelope["auth"] = {
            "via": "account_key", "owner": owner, "key_id": "k1",
            "from_agent": "research-agent-v1", "stamped_at": stamped_at,
            "mac": comm.stamp_mac(secret or TEST_SECRET, request_id=rid,
                                  from_agent="research-agent-v1", owner=owner,
                                  key_id="k1", intent=intent,
                                  stamped_at=stamped_at),
        }
    fields = {"request_id": rid, "agent_id": "research-agent-v1",
              "envelope": json.dumps(envelope, ensure_ascii=False)}
    if trace_id:
        fields["trace_id"] = trace_id
    return (f"{rid}-0", fields)


def _replies(redis):
    """回包流里 (kind, data) 列表。"""
    out = []
    for stream, fields in redis.added:
        if stream != "inside-out-stream":
            continue
        out.append((fields.get("kind"), json.loads(fields.get("data") or "{}")))
    return out


def _worker(redis, store=None, auth=None, **cfg):
    store = store or FakeStore(
        [{"id": "d1", "title": "Tardigrade Paper", "owner": "alice"}],
        {"d1": {"highlights": {"b1": {"color": "yellow"}},
                "notes": [{"id": "n1", "block_id": "b1", "text": "这篇讲缓步动物。",
                           "by": "alice", "created_at": "2026-11-01T10:00:00"}],
                "free_notes": [{"id": "f1", "title": "想法",
                                "text": "可以复现实验。", "by": "alice"}]}},
        {"d1": {"id": "d1", "title": "Tardigrade Paper", "pages": []}},
    )
    conf = comm.CommConfig(block_ms=0, stamp_secret=TEST_SECRET, **cfg)
    return comm.CommWorker(store=store, auth=auth or FakeAuth(), cfg=conf,
                           redis_client=redis)


# --------------------------------------------------------------------------- #
# 测试
# --------------------------------------------------------------------------- #
class ActionMappingTest(unittest.TestCase):
    def test_intent_to_action(self):
        w = _worker(FakeRedis())
        self.assertEqual(w._action_of({"intent": "paper.note.export"}, {}), "export_notes")
        self.assertEqual(w._action_of({"intent": "paper.section.query"}, {}), "answer")
        self.assertEqual(w._action_of({"intent": "paper.insight.validate"}, {}), "answer")
        # payload.action 优先于 intent
        self.assertEqual(w._action_of({"intent": "paper.note.export"},
                                      {"action": "list_notes"}), "list_notes")
        # 未知意图默认回答
        self.assertEqual(w._action_of({"intent": "whatever"}, {}), "answer")


class StreamFlowTest(unittest.TestCase):
    def test_export_notes_roundtrip(self):
        redis = FakeRedis([_entry("req1", "paper.note.export",
                                  {"doc_id": "d1"}, trace_id="tr-1")])
        result = _worker(redis).poll_once(block_ms=0)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["action"], "export_notes")

        kinds = [kind for kind, _ in _replies(redis)]
        self.assertEqual(kinds, ["ack", "final"])
        final = _replies(redis)[1][1]
        self.assertTrue(final["ok"])
        self.assertEqual(final["doc_id"], "d1")
        self.assertEqual(final["paper"]["title"], "Tardigrade Paper")
        # 消息里是「摘要」：笔记统计 + 前 3 条预览 + 分节目录（不含正文）
        self.assertEqual(final["notes"]["stats"]["notes_total"], 2)
        self.assertEqual(len(final["notes"]["preview"]["sentence_notes"]), 1)
        self.assertEqual(len(final["notes"]["preview"]["free_notes"]), 1)
        self.assertEqual(final["sections"]["abstract"]["chars"], 0)
        self.assertNotIn("text", final["sections"]["abstract"])
        self.assertIn("hint", final)
        # 完整导出（即使很小）一定已落 Redis，消息只带引用
        self.assertTrue(final["truncated"])
        self.assertEqual(final["result_ref"], "agent:result:req1")
        self.assertEqual(final["result_url"], "/v1/requests/req1/result")
        blob = redis.kv["agent:result:req1"]
        raw = (gzip.decompress(base64.b64decode(blob))
               if final["result_encoding"].startswith("gzip")
               else blob.encode("utf-8"))
        full = json.loads(raw.decode("utf-8"))
        self.assertEqual(len(full["notes"]["sentence_notes"]), 1)
        self.assertEqual(len(full["notes"]["free_notes"]), 1)
        self.assertIn("text", full["sections"]["abstract"])   # 完整导出带正文
        self.assertEqual(full["sections"]["abstract"]["text"], "")
        # ACK 发生在回包之后
        self.assertEqual(redis.acked, ["req1-0"])
        self.assertEqual(redis.added[-1][1].get("trace_id"), "tr-1")

    def test_export_by_gateway_stamp(self):
        """新模型：归属账号只认网关盖章（payload 里不再带任何 key）。"""
        redis = FakeRedis([_entry("req2", "paper.note.export",
                                  {"title": "tardigrade paper"}, owner="alice")])
        result = _worker(redis).poll_once(block_ms=0)
        self.assertTrue(result["items"][0]["ok"])
        final = _replies(redis)[1][1]
        self.assertEqual(final["owner"], "alice")
        self.assertEqual(final["doc_id"], "d1")

    def test_export_agent_without_llm_falls_back(self):
        """payload.mode=agent 但账号没配 LLM：降级 mechanical，并标记 fallback。"""
        redis = FakeRedis([_entry("reqa", "paper.note.export",
                                  {"doc_id": "d1", "mode": "agent"})])
        _worker(redis).poll_once(block_ms=0)
        final = _replies(redis)[1][1]
        self.assertTrue(final["ok"])
        self.assertEqual(final["mode_requested"], "agent")
        self.assertEqual(final["mode"], "mechanical")
        self.assertEqual(final["fallback"], "llm_unavailable")
        self.assertTrue(final["truncated"])
        self.assertEqual(final["result_ref"], "agent:result:reqa")

    def test_export_agent_strict_without_llm_errors(self):
        """allow_fallback=false 时，agent 模式没有 LLM 要直接报错，不能偷偷降级。"""
        redis = FakeRedis([_entry("reqb", "paper.note.export",
                                  {"doc_id": "d1", "mode": "agent",
                                   "allow_fallback": False})])
        _worker(redis).poll_once(block_ms=0)
        kind, data = _replies(redis)[1]
        self.assertEqual(kind, "error")
        self.assertFalse(data["ok"])
        self.assertIn("LLM", data["error"])

    def test_export_agent_uses_llm_when_available(self):
        """有可用 LLM 时走 agent 模式（这里用假 LLM 替换技能，只验证分派）。"""
        class FakeLlm:
            ready = True

            def chat(self, *_a, **_kw):
                return "{}"

        calls = {}

        def fake_export(**kwargs):
            calls.update(kwargs)
            return {"format": "tardigrade-note-export", "version": 1,
                    "mode": kwargs.get("mode"), "paper": {"id": "d1"},
                    "sections": {}, "notes": {}, "report": {}, "ai_summary": None}

        redis = FakeRedis([_entry("reqc", "paper.note.export",
                                  {"doc_id": "d1", "mode": "agent"})])
        worker = _worker(redis)
        worker.llm_factory = lambda user: FakeLlm()
        with patch.object(comm, "build_note_out_export", fake_export):
            worker.poll_once(block_ms=0)
        self.assertEqual(calls.get("mode"), "agent")
        self.assertIsNotNone(calls.get("llm"))
        final = _replies(redis)[1][1]
        self.assertEqual(final["mode"], "agent")
        self.assertEqual(final["mode_requested"], "agent")
        self.assertNotIn("fallback", final)

    def test_list_notes(self):
        redis = FakeRedis([_entry("req3", "paper.note.list", {"action": "list_notes"})])
        _worker(redis).poll_once(block_ms=0)
        final = _replies(redis)[1][1]
        self.assertEqual(final["action"], "list_notes")
        self.assertEqual(final["total"], 2)
        self.assertEqual(len(final["notes"]), 2)
        self.assertEqual(final["papers"][0]["doc_id"], "d1")

    def test_answer_falls_back_to_mechanical(self):
        redis = FakeRedis([_entry("req4", "paper.section.query",
                                  {"query": "缓步动物"})])
        _worker(redis).poll_once(block_ms=0)
        final = _replies(redis)[1][1]
        self.assertEqual(final["action"], "answer")
        self.assertEqual(final["mode"], "mechanical")
        self.assertIn("缓步动物", final["answer"])

    def test_unknown_paper_replies_error(self):
        redis = FakeRedis([_entry("req5", "paper.note.export",
                                  {"title": "完全不存在的标题"})])
        result = _worker(redis).poll_once(block_ms=0)
        self.assertFalse(result["items"][0]["ok"])
        kinds = [kind for kind, _ in _replies(redis)]
        self.assertEqual(kinds, ["ack", "error"])
        self.assertFalse(_replies(redis)[1][1]["ok"])
        self.assertEqual(redis.acked, ["req5-0"])

    def test_cancelled_message_is_skipped(self):
        redis = FakeRedis([_entry("req6", "paper.note.export", {"doc_id": "d1"})])
        redis.cancelled.add("req6")
        result = _worker(redis).poll_once(block_ms=0)
        self.assertTrue(result["items"][0]["ok"])
        self.assertEqual(result["items"][0]["skipped"], "cancelled")
        self.assertEqual(_replies(redis), [])      # 不回包
        self.assertEqual(redis.acked, ["req6-0"])

    def test_unknown_owner_is_reported(self):
        """旧行为（仅 AGENT_COMM_ALLOW_UNSTAMPED=1）：账号不存在要报出来。"""
        redis = FakeRedis([_entry("req7", "paper.note.list",
                                  {"user": "nobody"}, owner=None, stamp=False)])
        result = _worker(redis, allow_unstamped=True).poll_once(block_ms=0)
        self.assertFalse(result["items"][0]["ok"])
        self.assertIn("账号不存在", _replies(redis)[-1][1]["error"])


class GatewayStampTest(unittest.TestCase):
    """新信任模型：reader 只读网关盖章，未盖章 / 验章失败 / payload 未归一化都拒收。"""

    def _error(self, redis) -> str:
        kind, data = _replies(redis)[-1]        # 准入失败只回一条 error（不先 ack）
        self.assertEqual(kind, "error")
        return str(data.get("error") or "")

    def test_unstamped_rejected(self):
        redis = FakeRedis([_entry("r1", "paper.note.list", {}, stamp=False)])
        _worker(redis).poll_once(block_ms=0)
        self.assertIn("盖章", self._error(redis))

    def test_bad_mac_rejected(self):
        redis = FakeRedis([_entry("r2", "paper.note.list", {}, secret="another-secret")])
        _worker(redis).poll_once(block_ms=0)
        self.assertIn("bad_stamp_mac", self._error(redis))

    def test_owner_mismatch_rejected(self):
        # payload.user 被改成别人：网关不会这么发，直接拒
        redis = FakeRedis([_entry("r3", "paper.note.list", {"user": "bob"})])
        _worker(redis).poll_once(block_ms=0)
        self.assertIn("payload_owner_mismatch", self._error(redis))

    def test_reinjected_transfer_key_rejected(self):
        redis = FakeRedis([_entry("r4", "paper.note.list",
                                  {"transfer_key": "ptk_abc"})])
        _worker(redis).poll_once(block_ms=0)
        self.assertIn("payload_key_not_stripped", self._error(redis))

    def test_stale_stamp_rejected(self):
        entry = _entry("r5", "paper.note.list", {})
        fields = dict(entry[1])
        env = json.loads(fields["envelope"])
        env["auth"]["stamped_at"] = int(time.time()) - 10 ** 6
        fields["envelope"] = json.dumps(env)
        redis = FakeRedis([(entry[0], fields)])
        _worker(redis).poll_once(block_ms=0)
        self.assertIn("stale_stamp", self._error(redis))

    def test_verify_helper_binds_from_agent(self):
        env = json.loads(_entry("r6", "paper.note.list", {})[1]["envelope"])
        self.assertEqual(comm.verify_gateway_stamp(env, TEST_SECRET)[0], "alice")
        self.assertEqual(comm.verify_gateway_stamp(env, ""), (None, "no_stamp_secret"))
        env["from_agent"] = "another-agent"
        self.assertEqual(comm.verify_gateway_stamp(env, TEST_SECRET),
                         (None, "stamp_agent_mismatch"))


class ReplySignatureTest(unittest.TestCase):
    """回包必须带签名（dispatcher 会验），否则任何人写 Redis 都能伪造 final。"""

    def _replies_fields(self, redis):
        return [f for stream, f in redis.added if stream == "inside-out-stream"]

    def test_replies_carry_owner_and_sig(self):
        redis = FakeRedis([_entry("rs1", "paper.note.list", {})])
        _worker(redis).poll_once(block_ms=0)
        fields = self._replies_fields(redis)
        self.assertTrue(fields)
        for field in fields:
            self.assertEqual(field.get("owner"), "alice")
            expected = comm.reply_sig(TEST_SECRET,
                                      request_id=field["request_id"],
                                      kind=field["kind"], owner=field["owner"],
                                      data=field["data"])
            self.assertEqual(field.get("sig"), expected)

    def test_error_reply_is_signed_without_owner(self):
        redis = FakeRedis([_entry("rs2", "paper.note.list", {}, stamp=False)])
        _worker(redis).poll_once(block_ms=0)
        field = self._replies_fields(redis)[-1]
        self.assertEqual(field["kind"], "error")
        self.assertNotIn("owner", field)      # 账号都没确定，不能盖章
        self.assertIn("sig", field)
        self.assertEqual(
            field["sig"],
            comm.reply_sig(TEST_SECRET, request_id="rs2", kind="error",
                           owner="", data=field["data"]))


class FitTest(unittest.TestCase):
    def _big_store(self):
        return FakeStore(
            [{"id": "d1", "title": "Big", "owner": "alice"}],
            {"d1": {"highlights": {},
                    "notes": [{"id": f"n{i}", "block_id": f"b{i}",
                               "text": "x" * 200} for i in range(200)],
                    "free_notes": []}},
            {"d1": {"id": "d1", "title": "Big", "pages": []}},
        )

    def test_oversized_reply_spills_to_redis(self):
        """超过单条上限：完整结果进 agent:result:{rid}，消息只带摘要 + 引用。"""
        redis = FakeRedis([_entry("req8", "paper.note.export", {"doc_id": "d1"})])
        w = _worker(redis, store=self._big_store(), inline_kb=8)
        result = w.poll_once(block_ms=0)
        final = _replies(redis)[1][1]

        self.assertTrue(final["truncated"])
        self.assertEqual(final["result_ref"], "agent:result:req8")
        self.assertEqual(final["result_url"], "/v1/requests/req8/result")
        self.assertLessEqual(len(json.dumps(final, ensure_ascii=False).encode()), 8 * 1024)
        # 摘要里保留统计与少量预览，正文全部省略
        self.assertEqual(final["notes"]["stats"]["sentence_notes"], 200)
        self.assertEqual(len(final["notes"]["preview"]["sentence_notes"]), 3)
        self.assertNotIn("text", final["sections"]["abstract"])

        # Redis 里存的是完整内容（一条笔记都不少）
        blob = redis.kv["agent:result:req8"]
        raw = (gzip.decompress(base64.b64decode(blob))
               if final["result_encoding"].startswith("gzip")
               else blob.encode("utf-8"))
        full = json.loads(raw.decode("utf-8"))
        self.assertEqual(len(full["notes"]["sentence_notes"]), 200)
        for note in full["notes"]["sentence_notes"]:
            self.assertEqual(len(note["note"]), 200)
        # 元信息写进 agent:req:{rid}，通信服务 context() 能直接看到
        req = redis.hashes["agent:req:req8"]
        self.assertEqual(req["result_bytes"], str(final["result_bytes"]))
        self.assertEqual(req["result_sha256"], final["result_sha256"])
        self.assertEqual(result["items"][0]["result_ref"], "agent:result:req8")

    def test_oversized_reply_falls_back_to_trimming(self):
        """关掉 blob（或 Redis 写不进去）时，退回原来的裁剪行为，不会丢消息。"""
        redis = FakeRedis([_entry("req8b", "paper.note.export", {"doc_id": "d1"})])
        w = _worker(redis, store=self._big_store(), inline_kb=8, result_blob=False)
        w.poll_once(block_ms=0)
        final = _replies(redis)[1][1]
        self.assertTrue(final["trimmed"])
        self.assertLessEqual(len(json.dumps(final, ensure_ascii=False).encode()), 8 * 1024)
        self.assertEqual(final["action"], "export_notes")
        self.assertEqual(redis.kv, {})


class FakeLlm:
    """最小的 `LlmClient` 替身：第一轮返回给定 tool_calls，之后返回一段最终正文。"""

    ready = True

    def __init__(self, calls=None, final="完成"):
        self._calls = list(calls or [])
        self._final = final

    def chat_completion(self, system, messages, tools=None, temperature=0.3):
        if self._calls:
            return {"role": "assistant", "content": None,
                    "tool_calls": [self._calls.pop(0)]}
        return {"role": "assistant", "content": self._final, "tool_calls": []}


class StatusTest(unittest.TestCase):
    def test_status_never_raises(self):
        # 两个地址都故意指向不可达端口：结果里只记录失败，不抛异常
        out = comm.service_status(comm.CommConfig(service_url="http://127.0.0.1:1",
                                                  redis_url="redis://127.0.0.1:1/0"))
        self.assertFalse(out["service"]["ok"])
        self.assertIn("redis", out)
        self.assertFalse(out["redis"]["ok"])
        self.assertFalse(out["ok"])

    def test_mask_url(self):
        self.assertEqual(comm.mask_url("redis://u:pw@h:6379/0"), "redis://***@h:6379/0")


class SkillTest(unittest.TestCase):
    def test_tool_loop_polls_and_reports(self):
        redis = FakeRedis([_entry("req9", "paper.note.list", {"action": "list_notes"})])
        w = _worker(redis)
        llm = FakeLlm([
            {"id": "c1", "type": "function",
             "function": {"name": "comm_poll",
                          "arguments": json.dumps({"limit": 1, "block_ms": 0})}},
        ], final="已处理队列中的消息。")
        out = comm.run_comm_agent(worker=w, llm=llm)
        self.assertIn("已处理", out["text"])
        self.assertTrue(any(e.get("type") == "tool" and e.get("name") == "comm_poll"
                            and e.get("status") == "done" for e in out["events"]))
        self.assertEqual(redis.acked, ["req9-0"])


class AutoStartTest(unittest.TestCase):
    def test_auto_start_can_be_disabled(self):
        with patch.dict(os.environ, {"AGENT_COMM_AUTO": "0"}):
            self.assertIsNone(comm.start_auto(store=object()))

    def test_run_forever_waits_when_service_missing(self):
        """服务不在（Redis 也不在）时不取消息，只等停止信号。"""
        w = _worker(FakeRedis([_entry("r", "paper.note.list", {"action": "list_notes"})]))
        w.cfg.service_url = "http://127.0.0.1:1"     # 不可达
        w.cfg.redis_url = "redis://127.0.0.1:1/0"    # 不可达
        stop = threading.Event()
        threading.Timer(0.15, stop.set).start()
        started = time.monotonic()
        code = w.run_forever(stop_event=stop, probe_interval=0.05)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(w._redis.acked, [])          # 没消费任何消息

    def test_supervisor_start_stop(self):
        class StubWorker:
            consumer = "stub-consumer"
            cfg = comm.CommConfig()

            def __init__(self):
                self.calls = 0

            def run_forever(self, *, stop_event=None, probe_interval=None,
                            require_service=True):
                self.calls += 1
                stop_event.wait(1.0)
                return 0

        worker = StubWorker()
        sup = comm.CommSupervisor(worker, probe_interval=0.01).start()
        self.assertTrue(sup.running)
        self.assertEqual(sup.state()["consumer"], "stub-consumer")
        self.assertTrue(sup.stop(timeout=2))
        self.assertGreaterEqual(worker.calls, 1)
        self.assertFalse(sup.running)


if __name__ == "__main__":
    unittest.main()
