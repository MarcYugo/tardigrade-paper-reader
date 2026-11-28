"""请求接收：幂等 + 落 raw 流 + 状态登记。"""
from __future__ import annotations

import time
import uuid
from typing import Any

import redis.asyncio as aioredis

from env_load import REQUEST_TTL, STREAM_IN_RAW, STREAM_MAXLEN
from .models import RequestEnvelope, RequestStatus
from .streams import (
    cancel_key,
    request_key,
    submitted_key,
    waiter_key,
)
from . import waiters


async def submit_request(
    r: aioredis.Redis, env: RequestEnvelope
) -> tuple[str, bool, RequestStatus]:
    """返回 (request_id, 是否新建, 当前状态)。重复提交返回已有状态。"""
    rid = (env.request_id or f"req_{uuid.uuid4().hex}").strip()
    first = await r.set(submitted_key(rid), "1", nx=True, ex=REQUEST_TTL)

    if not first:
        existing = await r.hgetall(request_key(rid))
        if existing:
            return rid, False, _as_status(existing.get("status"))
        # 幂等键还在但状态已过期：按新请求继续（刷新幂等键）
        await r.set(submitted_key(rid), "1", ex=REQUEST_TTL)

    now = int(time.time())
    deadline = now + env.timeout_s
    envelope = env.model_copy(update={"request_id": rid}).model_dump_json()

    await r.hset(
        request_key(rid),
        mapping={
            "status": RequestStatus.QUEUED.value,
            "from_agent": env.from_agent,
            "intent": env.intent,
            # 明文 key 已被网关抹掉（见 guard.stamped_envelope），这里不再落任何凭据
            "transfer_key": "",
            "owner": env.owner or "",
            "auth_via": (env.auth.via if env.auth else ""),
            "account_key_id": (env.auth.key_id if env.auth else ""),
            "stamped_at": (env.auth.stamped_at if env.auth else ""),
            "created_at": now,
            "deadline": deadline,
            "reason": "",
            "envelope": envelope,
        },
    )
    await r.expire(request_key(rid), REQUEST_TTL)

    await r.xadd(
        STREAM_IN_RAW,
        {"request_id": rid, "agent_id": env.from_agent, "envelope": envelope},
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )
    return rid, True, RequestStatus.QUEUED


def _as_status(raw: str | None) -> RequestStatus:
    try:
        return RequestStatus(raw or RequestStatus.QUEUED.value)
    except ValueError:
        return RequestStatus.QUEUED


async def context(r: aioredis.Redis, rid: str) -> dict[str, Any] | None:
    """读请求上下文（reader agent 与等待者共用）。"""
    data = await r.hgetall(request_key(rid))
    if not data:
        return None
    deadline = int(data.get("deadline") or 0)
    result_bytes = int(data.get("result_bytes") or 0)
    return {
        "status": data.get("status", RequestStatus.QUEUED.value),
        "from_agent": data.get("from_agent", ""),
        "intent": data.get("intent", ""),
        "transfer_key": data.get("transfer_key") or None,
        # 网关盖章的归属账号（已校验；reader 侧只认它）
        "owner": data.get("owner") or None,
        "auth_via": data.get("auth_via") or None,
        "account_key_id": data.get("account_key_id") or None,
        "deadline": deadline,
        "expired": bool(deadline and deadline < time.time()),
        "reason": data.get("reason", ""),
        "created_at": int(data.get("created_at") or 0),
        "waiting": await waiters.is_waiting(r, rid),
        "cancelled": bool(await r.exists(cancel_key(rid))),
        # 完整结果是否已落 agent:result:{rid}（大结果不在回传消息里）
        "result_available": bool(result_bytes),
        "result_bytes": result_bytes,
        "result_sha256": data.get("result_sha256") or "",
        "result_url": f"/v1/requests/{rid}/result" if result_bytes else None,
    }


async def drop_waiter(r: aioredis.Redis, rid: str) -> None:
    await r.delete(waiter_key(rid))
