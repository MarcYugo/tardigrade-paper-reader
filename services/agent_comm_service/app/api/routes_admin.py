"""运维接口：健康检查、指标、死信查看。"""
from __future__ import annotations

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Query, Request

from env_load import STREAM_DLQ_IN, STREAM_DLQ_OUT, STREAM_IN, STREAM_IN_RAW, STREAM_OUT
from ..core.auth import authenticate
from ..core.metrics import METRICS

router = APIRouter()


def _redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    r = _redis(request)
    return {"ok": bool(await r.ping())}


@router.get("/metrics")
async def metrics(request: Request) -> dict:
    r = _redis(request)
    queues = {}
    for name in (STREAM_IN_RAW, STREAM_IN, STREAM_OUT, STREAM_DLQ_IN, STREAM_DLQ_OUT):
        queues[name] = await r.xlen(name)
    return {"counters": METRICS.snapshot(), "queues": queues}


@router.get("/v1/dlq")
async def dlq(request: Request, which: str = Query("in"), count: int = Query(20, ge=1, le=200)) -> list:
    r = _redis(request)
    # 死信里可能含信封（owner / doc_id / payload），不能匿名可读
    await authenticate(request, r)
    stream = STREAM_DLQ_IN if which == "in" else STREAM_DLQ_OUT
    entries = await r.xrevrange(stream, count=count)
    return [{"id": entry_id, **fields} for entry_id, fields in entries]
