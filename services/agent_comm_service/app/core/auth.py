"""鉴权与配额。

两种模式：
  * 默认（REQUIRE_SIGNATURE=0）：`X-Agent-Id` + `X-Api-Key`（= 该 agent 的密钥）。
  * 强化（REQUIRE_SIGNATURE=1）：额外校验 `X-Timestamp` / `X-Nonce` / `X-Signature`
    （HMAC-SHA256 over "ts\\nMETHOD\\npath\\nnonce\\nsha256(body)"），并做 nonce 防重放。
"""
from __future__ import annotations

import hashlib
import hmac
import time

import redis.asyncio as aioredis
from fastapi import HTTPException, Request

from env_load import AGENT_SECRETS, QUOTA_QPS, REQUIRE_SIGNATURE, TS_WINDOW
from .streams import nonce_key, quota_key


def sign(secret: str, ts: str, method: str, path: str, nonce: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    slot = "\n".join([ts, method.upper(), path, nonce, body_hash])
    return hmac.new(secret.encode(), slot.encode(), hashlib.sha256).hexdigest()


async def authenticate(request: Request, r: aioredis.Redis) -> str:
    if not AGENT_SECRETS:                       # 本地调试：不鉴权
        return request.headers.get("X-Agent-Id") or "anonymous"

    agent = request.headers.get("X-Agent-Id") or ""
    secret = AGENT_SECRETS.get(agent)
    if not agent or not secret:
        raise HTTPException(status_code=401, detail="unknown agent")

    api_key = request.headers.get("X-Api-Key") or ""
    if api_key and hmac.compare_digest(api_key, secret) and not REQUIRE_SIGNATURE:
        return agent

    if not REQUIRE_SIGNATURE:
        raise HTTPException(status_code=401, detail="bad api key")

    ts = request.headers.get("X-Timestamp") or ""
    nonce = request.headers.get("X-Nonce") or ""
    signature = request.headers.get("X-Signature") or ""
    try:
        ts_int = int(ts)
    except ValueError:
        raise HTTPException(status_code=401, detail="bad timestamp")
    if abs(time.time() - ts_int) > TS_WINDOW:
        raise HTTPException(status_code=401, detail="timestamp out of window")

    body = await request.body()
    expected = sign(secret, ts, request.method, request.url.path, nonce, body)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="bad signature")

    if not await r.set(nonce_key(agent, nonce or "-"), "1", nx=True, ex=TS_WINDOW):
        raise HTTPException(status_code=401, detail="replay detected")
    return agent


async def check_quota(r: aioredis.Redis, agent: str, cost: int = 1) -> None:
    """按秒固定窗口限流，够用且成本低。"""
    if QUOTA_QPS <= 0:
        return
    bucket = str(int(time.time()))
    key = quota_key(agent, bucket)
    count = await r.incrby(key, cost)
    if count == cost:
        await r.expire(key, 2)
    if count > QUOTA_QPS:
        raise HTTPException(status_code=429, detail="quota exceeded")
