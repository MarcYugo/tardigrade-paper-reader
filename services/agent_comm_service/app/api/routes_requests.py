"""对外 API：提交 / 查询 / 长轮询 / 取消 / 签发 transfer_key。"""
from __future__ import annotations

import time
import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from redis.exceptions import TimeoutError as RedisTimeoutError

from env_load import (
    AGENT_SECRETS,
    ALLOW_ENV_TRANSFER_KEYS,
    REQUEST_TTL,
    RESULT_DELETE_ON_FETCH,
    WAITER_GRACE,
)
from ..core import guard, ingest, waiters
from ..core.auth import authenticate, check_quota
from ..core.metrics import METRICS
from ..core.models import RequestEnvelope, SubmitResponse
from ..core.streams import (
    append_reply,
    cancel_key,
    get_result,
    read_reply,
    request_key,
    result_meta,
)

router = APIRouter(prefix="/v1", tags=["requests"])


def _redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis


async def _owned_context(r: aioredis.Redis, rid: str, agent: str) -> dict:
    """读请求上下文，并确认它属于当前 agent（防 IDOR：别人知道 rid 也读不到）。"""
    ctx = await ingest.context(r, rid)
    if ctx is None:
        raise HTTPException(status_code=404, detail="unknown request")
    owner_agent = str(ctx.get("from_agent") or "")
    if AGENT_SECRETS and owner_agent and owner_agent != agent:
        raise HTTPException(status_code=403, detail="request belongs to another agent")
    return ctx


def _epoch(value) -> int | None:
    return int(value.timestamp()) if hasattr(value, "timestamp") else None


def _public_key(doc: dict) -> dict:
    """对外展示的元数据：绝不包含明文，也不包含摘要。"""
    return {
        "key_id": doc.get("_id"),
        "label": doc.get("label") or "",
        "blob_id": doc.get("blob_id"),
        "created_at": _epoch(doc.get("created_at")),
        "expires_at": _epoch(doc.get("exp_at")),
        "revoked": doc.get("revoked_at") is not None,
        "last_used_at": _epoch(doc.get("last_used_at")),
    }


def _reject_status(reason: str) -> int:
    """准入类拒绝统一 403（凭据/归属），其余（意图、结构、大小）仍 422。"""
    return 403 if reason.startswith(
        ("transfer_key", "account_key", "owner_mismatch", "stamp_", "auth")
    ) else 422


@router.post("/requests", response_model=SubmitResponse, status_code=202)
async def submit(request: Request, env: RequestEnvelope) -> SubmitResponse:
    r = _redis(request)
    agent = await authenticate(request, r)
    await check_quota(r, agent)

    if AGENT_SECRETS and env.from_agent != agent:
        raise HTTPException(status_code=403, detail="from_agent mismatch")
    # owner / auth 是服务端盖章字段：客户端填任何值都属于伪造，直接拒
    if env.owner is not None or env.auth is not None:
        raise HTTPException(
            status_code=422,
            detail="owner/auth 由通信服务盖章，客户端不得填写",
        )

    verdict = await guard.fast_check(env)
    if not verdict.ok:
        METRICS.inc(f"ingest.{verdict.level.value}")
        raise HTTPException(status_code=_reject_status(verdict.reason),
                            detail=f"{verdict.reason}: {verdict.detail}".strip(": "))

    # ---- 唯一准入点：账号 transfer key（ptk_）在此校验，并得出归属账号 ----
    rid = (env.request_id or f"req_{uuid.uuid4().hex}").strip()
    grant, gate = await guard.authorize(env)
    if not gate.ok:
        METRICS.inc("ingest.rejected")
        raise HTTPException(status_code=_reject_status(gate.reason),
                            detail=f"{gate.reason}: {gate.detail}".strip(": "))

    # 盖章：抹掉明文 key、固定归属账号、写入防伪 MAC（reader 只读这个结果）
    stamped = guard.stamped_envelope(env, grant, rid)
    check = guard.check_stamp(stamped)
    if not check.ok:
        METRICS.inc("ingest.rejected")
        raise HTTPException(status_code=403,
                            detail=f"{check.reason}: {check.detail}".strip(": "))

    rid, created, status = await ingest.submit_request(r, stamped)
    METRICS.inc("ingest.accepted" if created else "ingest.duplicate")
    return SubmitResponse(
        request_id=rid,
        status=status,
        duplicate=not created,
        deadline=int(time.time()) + env.timeout_s,
        poll_url=f"/v1/requests/{rid}",
        wait_url=f"/v1/requests/{rid}/wait",
    )


@router.get("/requests/{rid}")
async def get_request(
    rid: str,
    request: Request,
    from_id: str = Query("0-0", alias="from", description="上次读到的消息 id"),
) -> dict:
    r = _redis(request)
    agent = await authenticate(request, r)
    ctx = await _owned_context(r, rid, agent)
    items = await read_reply(r, rid, from_id)
    return {
        "request_id": rid,
        **ctx,
        "items": items,
        "next": items[-1]["id"] if items else from_id,
    }


@router.get("/requests/{rid}/wait", response_model=None)
async def wait_request(
    rid: str,
    request: Request,
    timeout: float = Query(30, ge=1, le=120, description="本次长轮询挂起秒数"),
    from_id: str = Query("0-0", alias="from"),
) -> Response | dict:
    r = _redis(request)
    agent = await authenticate(request, r)
    await _owned_context(r, rid, agent)

    # 挂起期间保持 waiter 存活，reader agent 据此判断"还有人在等"
    await waiters.touch(r, rid, int(timeout) + WAITER_GRACE)

    try:
        items = await read_reply(r, rid, from_id, block=int(timeout * 1000))
    except RedisTimeoutError:
        items = []

    if not items:
        return Response(status_code=204)

    ctx = await ingest.context(r, rid) or {}
    return {
        "request_id": rid,
        **ctx,
        "items": items,
        "next": items[-1]["id"],
    }


@router.get("/requests/{rid}/waiter")
async def get_waiter(rid: str, request: Request) -> dict:
    r = _redis(request)
    agent = await authenticate(request, r)
    await _owned_context(r, rid, agent)
    return {"request_id": rid, "waiting": await waiters.is_waiting(r, rid)}


@router.get("/requests/{rid}/result", response_model=None)
async def request_result(
    rid: str,
    request: Request,
    meta: bool = Query(False, description="true 时只返回元信息，不取正文"),
    purge: bool | None = Query(
        None,
        description=("取完就删除完整结果（不传则用 RESULT_DELETE_ON_FETCH，默认 false）"),
    ),
) -> Response | dict:
    """取回**完整结果**（reader 回包超出 64KB 时，回传消息里只带摘要 + 引用）。

    - 完整内容存于 `agent:result:{rid}`（默认 TTL 1 小时，`RESULT_TTL` 可调）；
    - `?purge=true`：本次取回后**立即删除**；不传时按 `RESULT_DELETE_ON_FETCH`
      （默认 false，仍然只靠 TTL 过期）；
    - `?meta=true` 只看大小 / 摘要 / 编码，不传正文（**不会触发 purge**）；
    - 结果不存在时 404（可能已过期或已被 purge，也可能该请求的结果本来就在消息里）。
    """
    r = _redis(request)
    agent = await authenticate(request, r)
    await _owned_context(r, rid, agent)

    info = await result_meta(r, rid)
    if meta:
        return {"request_id": rid, **info}

    data, info = await get_result(
        r, rid, purge=RESULT_DELETE_ON_FETCH if purge is None else bool(purge)
    )
    if data is None:
        raise HTTPException(
            status_code=404,
            detail="result not found（可能已过期或已被取回后删除，或该请求的结果直接在回传消息里）",
        )
    headers = {
        "X-Result-Bytes": str(info.get("bytes") or 0),
        "X-Result-Sha256": info.get("sha256") or "",
        "X-Result-Encoding": info.get("encoding") or "json",
        "X-Result-Purged": "true" if info.get("purged") else "false",
    }
    return JSONResponse(
        content={"request_id": rid, "meta": info, "result": data},
        headers=headers,
    )


@router.delete("/requests/{rid}")
async def cancel_request(rid: str, request: Request) -> dict:
    r = _redis(request)
    agent = await authenticate(request, r)
    await _owned_context(r, rid, agent)      # 只有提交方能取消（防别人替你取消）

    # 旗标供 reader agent 在步骤间自查；同时立刻回一条 cancelled 唤醒等待者
    await r.set(cancel_key(rid), "1", ex=REQUEST_TTL)
    await r.hset(request_key(rid), mapping={"status": "cancelled"})
    await append_reply(r, rid, "cancelled", {"by": agent})
    METRICS.inc("ingest.cancelled")
    return {"request_id": rid, "status": "cancelled"}


@router.post("/transfer-keys")
async def create_transfer_key(request: Request, body: dict) -> dict:
    """签发旧的 `tk:v2` key（pepper 摘要，与账号无关）。

    新模型下默认关闭（`ALLOW_ENV_TRANSFER_KEYS=0`）：访问权只来自账号 transfer key
    （`ptk_…`，在 tardigrade 的「转交 Key」里签发），不再由本服务按启动环境变量
    里的 pepper 派生。要兼容旧脚本可显式打开该环境变量。
    """
    if not ALLOW_ENV_TRANSFER_KEYS:
        raise HTTPException(
            status_code=403,
            detail="env_transfer_keys_disabled: 请使用账号 transfer key（ptk_…）；"
                   "如需兼容旧流程请设 ALLOW_ENV_TRANSFER_KEYS=1",
        )
    r = _redis(request)
    agent = await authenticate(request, r)
    body = body or {}
    blob_id = str(body.get("blob_id") or "").strip() or None
    label = str(body.get("label") or "").strip()
    try:
        ttl = int(body["ttl"]) if body.get("ttl") is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="ttl must be integer seconds")

    key, doc = await guard.issue_transfer_key(agent, blob_id, ttl, label)
    return {
        "transfer_key": key,
        "key_id": doc["_id"],
        "blob_id": blob_id,
        "label": label,
        "expires_at": int(doc["exp_at"].timestamp()),
        "notice": "明文仅返回这一次，服务端只保存不可逆摘要；遗失后只能重新签发",
    }


@router.get("/transfer-keys")
async def list_transfer_keys(request: Request) -> dict:
    """列出自己名下的 key（不含密文、不含摘要）。"""
    r = _redis(request)
    agent = await authenticate(request, r)
    docs = await guard.get_store().list_owner(agent)
    return {"items": [_public_key(doc) for doc in docs]}


@router.delete("/transfer-keys/{key_id}")
async def delete_transfer_key(
    key_id: str,
    request: Request,
    purge: bool = Query(False, description="true 时物理删除，默认软吊销以保留审计"),
) -> dict:
    """吊销（默认）或删除自己的 key；下一次使用即失败。"""
    r = _redis(request)
    agent = await authenticate(request, r)
    ok = await guard.get_store().revoke(key_id, agent, purge=purge)
    if not ok:
        raise HTTPException(status_code=404, detail="unknown key")
    METRICS.inc("transfer_key.purged" if purge else "transfer_key.revoked")
    return {"key_id": key_id, "deleted": purge, "revoked": True}
