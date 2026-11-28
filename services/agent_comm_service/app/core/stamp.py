"""网关盖章（auth stamp）：把「账号校验结果」变成消息里**不可伪造**的字段。

背景：reader 侧（tardigrade 的通信桥）过去是自己从 `payload.transfer_key` / `payload.user`
里“猜”归属账号的，而 `payload` 是自由格式 —— 谁能写进流，谁就能自报身份。
新模型把决策点收敛到网关：网关校验 `ptk_` → 反查账号 → 在信封上盖 `owner` + `auth`
（带 HMAC），并把 `payload` 归一化成“只指向这个账号”。reader 只需读这个结果。

两个关键点：

* **盖章 = 结果 + 防伪**。只写 `owner` 等于自报姓名，能连 Redis 的人照样能伪造；
  所以带 `mac = HMAC(secret, request_id|from_agent|owner|key_id|intent|stamped_at)`。
* **secret 不取自容器启动环境变量**。它优先用 `AUTH_STAMP_SECRET`（显式配置时才用），
  否则由本服务在 MongoDB 的 `agent_gateway_meta` 里 get-or-create 一次并长期复用，
  这样重启 / 多副本都稳定，也不会像 `KEY_HASH_PEPPER` 那样一换就让所有凭据失效。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from env_load import AUTH_STAMP_SECRET, STAMP_WINDOW

_META_ID = "auth_stamp"

_lock = threading.Lock()
_secret: str = ""
_warned = False


def set_secret(value: str | None) -> None:
    """设置盖章密钥（启动时由 `ensure_secret()` 调用；测试里可直接设）。"""
    global _secret
    with _lock:
        _secret = (value or "").strip()


def configured() -> bool:
    return bool(_secret)


def _key() -> bytes:
    """拿密钥；没配置时退化成进程内随机值（只保证单进程自洽，并打一次警告）。"""
    global _secret, _warned
    if _secret:
        return _secret.encode()
    with _lock:
        if not _secret:
            _secret = secrets.token_hex(32)
            if not _warned:
                _warned = True
                print("[warn] 盖章密钥未初始化：临时用进程内随机值（多副本会互不认可）",
                      flush=True)
        return _secret.encode()


def _slot(request_id: str, from_agent: str, owner: str, key_id: str,
          intent: str, stamped_at: int) -> str:
    return "\n".join([request_id or "", from_agent or "", owner or "",
                      key_id or "", intent or "", str(int(stamped_at))])


def sign(*, request_id: str, from_agent: str, owner: str, key_id: str,
         intent: str, stamped_at: int) -> str:
    return hmac.new(
        _key(),
        _slot(request_id, from_agent, owner, key_id, intent, stamped_at).encode(),
        hashlib.sha256,
    ).hexdigest()


def verify(*, request_id: str, from_agent: str, owner: str, key_id: str,
           intent: str, stamped_at: int, mac: str) -> tuple[bool, str]:
    """校验一条盖章：时间窗 → MAC。返回 `(ok, 原因)`。"""
    now = int(time.time())
    if not stamped_at or abs(now - int(stamped_at)) > STAMP_WINDOW:
        return False, "stale_stamp"
    expected = sign(request_id=request_id, from_agent=from_agent, owner=owner,
                    key_id=key_id, intent=intent, stamped_at=int(stamped_at))
    if not hmac.compare_digest(expected, (mac or "").strip()):
        return False, "bad_stamp_mac"
    return True, ""


# --------------------------------------------------------------------------- #
# 回包签名（reader → 服务）：挡住“能写 Redis 就能伪造 final”
#   slot = "reply" | request_id | kind | owner | sha256(data)
# --------------------------------------------------------------------------- #
def _reply_slot(request_id: str, kind: str, owner: str, data: str) -> str:
    body_hash = hashlib.sha256((data or "").encode()).hexdigest()
    return "\n".join(["reply", request_id or "", kind or "",
                      owner or "", body_hash])


def sign_reply(*, request_id: str, kind: str, owner: str, data: str) -> str:
    return hmac.new(_key(), _reply_slot(request_id, kind, owner, data).encode(),
                    hashlib.sha256).hexdigest()


def verify_reply(*, request_id: str, kind: str, owner: str, data: str,
                 mac: str) -> tuple[bool, str]:
    expected = sign_reply(request_id=request_id, kind=kind, owner=owner, data=data)
    if not hmac.compare_digest(expected, (mac or "").strip()):
        return False, "bad_reply_sig"
    return True, ""


async def ensure_secret(store) -> str:
    """启动时调用：确定盖章密钥（env 显式配置 > Mongo 持久化 > 进程内随机）。"""
    if (AUTH_STAMP_SECRET or "").strip():
        set_secret(AUTH_STAMP_SECRET)
        return AUTH_STAMP_SECRET
    doc = await store.get_meta(
        _META_ID,
        lambda: {"secret": secrets.token_hex(32),
                 "created_at": time.time(),
                 "note": "agent_comm_service 网关盖章密钥（自动生成，勿删）"},
    )
    set_secret(str(doc.get("secret") or ""))
    return _secret
