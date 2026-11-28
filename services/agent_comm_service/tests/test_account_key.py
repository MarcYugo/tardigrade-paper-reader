"""账号 transfer key（ptk_）+ 网关盖章：`python tests/test_account_key.py` 或 pytest。"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import accounts, guard, keystore, stamp  # noqa: E402
from app.core.models import RequestEnvelope  # noqa: E402

# 单测不依赖 MongoDB
keystore.set_store(keystore.MemoryKeyStore())
ACCOUNTS = accounts.MemoryAccountKeyStore()
ACCOUNTS.add("test123", "ptk_demo_key_abcdef", key_id="k1", name="obsidian")
accounts.set_store(ACCOUNTS)
stamp.set_secret("unit-test-stamp-secret")


def run(coro):
    return asyncio.run(coro)


def env(**kwargs) -> RequestEnvelope:
    base = {
        "from_agent": "research-agent-v1",
        "intent": "paper.note.export",
        "payload": {},
    }
    base.update(kwargs)
    return RequestEnvelope(**base)


def grant_of(env_obj):
    return run(guard.authorize(env_obj))


# --------------------------- 准入 --------------------------- #
def test_missing_key_is_rejected() -> None:
    grant, verdict = grant_of(env())
    assert not verdict.ok
    assert verdict.reason == "account_key_required"
    assert grant.owner is None


def test_account_key_resolves_owner() -> None:
    grant, verdict = grant_of(env(transfer_key="ptk_demo_key_abcdef"))
    assert verdict.ok, verdict
    assert grant.owner == "test123" and grant.key_id == "k1"


def test_key_from_payload_still_works() -> None:
    """旧客户端把 key 放 payload 里也认，但校验后会被抹掉（见 stamped_envelope）。"""
    grant, verdict = grant_of(env(payload={"transfer_key": "ptk_demo_key_abcdef"}))
    assert verdict.ok, verdict
    assert grant.owner == "test123"


def test_unknown_key_rejected() -> None:
    _grant, verdict = grant_of(env(transfer_key="ptk_nope"))
    assert not verdict.ok and verdict.reason == "transfer_key_unknown"


def test_deleted_key_rejected() -> None:
    ACCOUNTS.add("bob", "ptk_short_lived", key_id="k2")
    assert grant_of(env(transfer_key="ptk_short_lived"))[1].ok
    ACCOUNTS.remove("ptk_short_lived")           # 模拟应用里“删除 key”
    _grant, verdict = grant_of(env(transfer_key="ptk_short_lived"))
    assert not verdict.ok and verdict.reason == "transfer_key_unknown"


def test_self_claimed_owner_mismatch() -> None:
    _grant, verdict = grant_of(env(transfer_key="ptk_demo_key_abcdef",
                                   payload={"user": "someone-else"}))
    assert not verdict.ok and verdict.reason == "owner_mismatch"


def test_bad_prefix_rejected() -> None:
    _grant, verdict = grant_of(env(transfer_key="sk_live_abc"))
    assert not verdict.ok and verdict.reason == "transfer_key_bad_format"


def test_env_derived_key_rejected_by_default() -> None:
    """tk:v2 是 pepper 派生、与账号无关的旧凭据，默认必须拒。"""
    key, _doc = run(guard.issue_transfer_key("research-agent-v1", ttl=60))
    _grant, verdict = grant_of(env(transfer_key=key))
    assert not verdict.ok
    assert verdict.reason == "transfer_key_env_key_rejected"


# --------------------------- 盖章 --------------------------- #
def _stamped() -> RequestEnvelope:
    grant, verdict = grant_of(env(transfer_key="ptk_demo_key_abcdef",
                                  payload={"action": "export_notes"}))
    assert verdict.ok
    return guard.stamped_envelope(env(transfer_key="ptk_demo_key_abcdef",
                                      payload={"action": "export_notes"}), grant,
                                  "req_test")


def test_stamp_normalizes_payload() -> None:
    stamped = _stamped()
    assert stamped.transfer_key is None, "明文 key 必须被抹掉"
    assert stamped.owner == "test123"
    assert stamped.auth and stamped.auth.owner == "test123"
    assert stamped.auth.key_id == "k1"
    assert stamped.payload.get("user") == "test123"
    assert "transfer_key" not in stamped.payload


def test_stamp_passes_deep_check() -> None:
    assert run(guard.deep_check(_stamped())).ok


def test_stamp_detects_tampering() -> None:
    stamped = _stamped()
    bad = stamped.model_copy(update={"owner": "bob"})
    assert guard.check_stamp(bad).reason == "stamp_owner_mismatch"

    auth = stamped.auth.model_copy(update={"mac": "0" * 64})
    assert guard.check_stamp(stamped.model_copy(update={"auth": auth})).reason == "bad_stamp_mac"


def test_stamp_requires_owner_binding() -> None:
    """reader 读的是 payload.user：盖章后它必须等于 auth.owner，不许自报。"""
    stamped = _stamped()
    swapped = stamped.model_copy(
        update={"payload": {**stamped.payload, "user": "bob"}})
    assert guard.check_stamp(swapped).reason == "stamp_owner_not_bound"


def test_stamp_rejects_reinjected_key() -> None:
    stamped = _stamped()
    again = stamped.model_copy(
        update={"payload": {**stamped.payload, "transfer_key": "ptk_demo_key_abcdef"}})
    assert guard.check_stamp(again).reason == "stamp_key_not_stripped"


def test_stamp_expires() -> None:
    old = int(time.time()) - 10 ** 6
    auth = stamp.sign(request_id="req_test", from_agent="research-agent-v1",
                      owner="test123", key_id="k1", intent="paper.note.export",
                      stamped_at=old)
    envelope = RequestEnvelope(
        request_id="req_test", from_agent="research-agent-v1", intent="paper.note.export",
        owner="test123",
        auth={"via": "account_key", "owner": "test123", "key_id": "k1",
              "from_agent": "research-agent-v1", "stamped_at": old, "mac": auth},
        payload={"user": "test123"},
    )
    assert guard.check_stamp(envelope).reason == "stale_stamp"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("account-key ok")
