"""agent_comm_service 入口：HTTP API + 两个消费者 + 巡检。"""
from __future__ import annotations

import asyncio
import os
import socket
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from env_load import (
    ACCOUNT_KEY_REQUIRED,
    AGENT_SECRETS,
    ALLOW_ENV_TRANSFER_KEYS,
    GROUP_DISPATCH,
    GROUP_GUARD,
    REDIS_URL,
    STREAM_IN_RAW,
    STREAM_OUT,
)
from .api import routes_admin, routes_requests
from .core import accounts, dispatcher, keystore, stamp
from .core.streams import ensure_group


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis.ping()
    app.state.redis = redis

    store = keystore.build_store()
    await store.connect()
    keystore.set_store(store)
    app.state.keystore = store

    # 账号 transfer key（ptk_）的校验源：直读应用那套 transfer_keys / users
    acct_store = accounts.build_store()
    await acct_store.connect()
    accounts.set_store(acct_store)
    app.state.account_store = acct_store
    # 网关盖章密钥：优先 AUTH_STAMP_SECRET，否则 Mongo 里 get-or-create（跨重启稳定）
    await stamp.ensure_secret(acct_store)

    if not AGENT_SECRETS:
        print("[warn] AGENT_SECRETS 为空：传输层鉴权已关闭，仅可用于本地调试", flush=True)
    if ACCOUNT_KEY_REQUIRED:
        print("[info] 准入模式：只接受账号 transfer key（ptk_…），"
              f"env transfer key={'允许' if ALLOW_ENV_TRANSFER_KEYS else '已拒绝'}",
              flush=True)
    else:
        print("[warn] ACCOUNT_KEY_REQUIRED=0：缺账号 key 也放行（owner 由 reader 推断）",
              flush=True)

    tag = f"comm-{socket.gethostname()}-{os.getpid()}"
    for stream, group in ((STREAM_IN_RAW, GROUP_GUARD), (STREAM_OUT, GROUP_DISPATCH)):
        await ensure_group(redis, stream, group)

    tasks = [
        asyncio.create_task(
            dispatcher.run_consumer(
                redis,
                stream=STREAM_IN_RAW,
                group=GROUP_GUARD,
                consumer=f"{tag}-guard",
                handler=dispatcher.handle_raw,
            ),
            name="guard",
        ),
        asyncio.create_task(
            dispatcher.run_consumer(
                redis,
                stream=STREAM_OUT,
                group=GROUP_DISPATCH,
                consumer=f"{tag}-dispatch",
                handler=dispatcher.handle_out,
            ),
            name="dispatch",
        ),
        asyncio.create_task(
            dispatcher.run_reclaimer(
                redis,
                stream=STREAM_IN_RAW,
                group=GROUP_GUARD,
                consumer=f"{tag}-guard-reclaim",
                handler=dispatcher.handle_raw,
            ),
            name="reclaim-in",
        ),
        asyncio.create_task(
            dispatcher.run_reclaimer(
                redis,
                stream=STREAM_OUT,
                group=GROUP_DISPATCH,
                consumer=f"{tag}-dispatch-reclaim",
                handler=dispatcher.handle_out,
            ),
            name="reclaim-out",
        ),
    ]
    print(f"[{tag}] agent_comm_service started", flush=True)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await redis.aclose()
        await store.close()
        await acct_store.close()
        print(f"[{tag}] agent_comm_service stopped", flush=True)


app = FastAPI(title="agent-comm-service", version="0.1.0", lifespan=lifespan)
app.include_router(routes_requests.router)
app.include_router(routes_admin.router)
