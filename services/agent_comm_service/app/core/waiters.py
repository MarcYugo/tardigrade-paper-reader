"""等待者登记：记录"外部 agent 是否还在等这个请求的结果"。"""
from __future__ import annotations

import redis.asyncio as aioredis

from env_load import WAITER_GRACE
from .streams import waiter_key


async def touch(r: aioredis.Redis, rid: str, ttl: int) -> None:
    """等待者每轮询一次就续期；断连后自然过期。"""
    await r.set(waiter_key(rid), "1", ex=max(int(ttl), WAITER_GRACE))


async def is_waiting(r: aioredis.Redis, rid: str) -> bool:
    return bool(await r.exists(waiter_key(rid)))


async def drop(r: aioredis.Redis, rid: str) -> None:
    await r.delete(waiter_key(rid))
