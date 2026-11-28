"""回包签名（reader → 服务）与请求归属校验。

`python tests/test_reply_sig.py` 或 pytest；不依赖 Redis / MongoDB。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.api import routes_requests  # noqa: E402
from app.core import dispatcher, stamp  # noqa: E402

SECRET = "unit-test-stamp-secret"
stamp.set_secret(SECRET)
# 归属校验只在开了 agent 鉴权时生效；测试里显式给一个
routes_requests.AGENT_SECRETS = {"research-agent-v1": "x"}


def _raised(fn):
    """跑一下，返回它抛出的异常（没抛就算测试失败）。"""
    try:
        fn()
    except Exception as exc:            # noqa: BLE001 - 这里就是要抓 HTTPException
        return exc
    raise AssertionError("应当抛出异常")


def _reply(rid="req_1", kind="final", owner="test123", data='{"ok": true}', **extra):
    fields = {"request_id": rid, "kind": kind, "data": data}
    if owner:
        fields["owner"] = owner
    if "sig" not in extra:
        fields["sig"] = stamp.sign_reply(request_id=rid, kind=kind, owner=owner, data=data)
    fields.update(extra)
    return fields


# --------------------------- 回包签名 --------------------------- #
def test_valid_reply_passes() -> None:
    assert dispatcher.check_reply(_reply(), "test123") == (True, "")


def test_missing_sig_rejected() -> None:
    fields = _reply()
    fields.pop("sig")
    assert dispatcher.check_reply(fields, "test123") == (False, "missing_reply_sig")


def test_tampered_data_rejected() -> None:
    fields = _reply()
    fields["data"] = '{"ok": true, "poisoned": true}'
    assert dispatcher.check_reply(fields, "test123") == (False, "bad_reply_sig")


def test_sig_bound_to_rid_and_kind() -> None:
    fields = _reply()
    fields["request_id"] = "req_other"
    assert dispatcher.check_reply(fields, "test123")[1] == "bad_reply_sig"
    fields = _reply()
    fields["kind"] = "error"
    assert dispatcher.check_reply(fields, "test123")[1] == "bad_reply_sig"


def test_owner_must_match_request_context() -> None:
    assert dispatcher.check_reply(_reply(owner="alice"), "test123") == (
        False, "reply_owner_mismatch")


def test_no_secret_configured_rejects() -> None:
    fields = _reply()
    stamp.set_secret("")
    try:
        assert dispatcher.check_reply(fields, "test123") == (False, "no_stamp_secret")
    finally:
        stamp.set_secret(SECRET)


def test_flag_off_accepts_unsigned() -> None:
    fields = _reply()
    fields.pop("sig")
    dispatcher.REQUIRE_READER_SIG = False
    try:
        assert dispatcher.check_reply(fields, "test123") == (True, "")
    finally:
        dispatcher.REQUIRE_READER_SIG = True


# --------------------------- 请求归属（IDOR） --------------------------- #
class FakeRedis:
    def __init__(self, ctx: dict | None) -> None:
        self.ctx = ctx

    async def hgetall(self, key):                     # noqa: ANN001
        return dict(self.ctx or {})

    async def exists(self, key):                      # noqa: ANN001
        return 0


def _ctx(from_agent="research-agent-v1", owner="test123") -> dict:
    return {"status": "done", "from_agent": from_agent, "intent": "paper.note.list",
            "owner": owner, "deadline": "0", "created_at": "0", "result_bytes": "0",
            "transfer_key": "", "reason": "", "auth_via": "account_key"}


def _owned(rid: str, agent: str, ctx: dict | None):
    return asyncio.run(routes_requests._owned_context(FakeRedis(ctx), rid, agent))


def test_owner_agent_can_read() -> None:
    assert _owned("req_1", "research-agent-v1", _ctx())["owner"] == "test123"


def test_other_agent_forbidden() -> None:
    exc = _raised(lambda: _owned("req_1", "some-other-agent", _ctx()))
    assert getattr(exc, "status_code", None) == 403


def test_unknown_request_404() -> None:
    exc = _raised(lambda: _owned("req_missing", "research-agent-v1", None))
    assert getattr(exc, "status_code", None) == 404


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("reply-sig ok")
