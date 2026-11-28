"""应用生命周期：托管通信桥后台线程，并给出 `/api/agent-comm/status` 需要的数据。"""
from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import runtime
from .factories import llm_for


log = logging.getLogger("uvicorn.error")


# ---------------- 通信桥（agent_comm_service reader 侧，自动托管） ----------------
# 启动时在后台线程里探测通信服务：**存在才消费**它的 Redis Streams，不在就一直等；
# 不想让应用自己消费队列时设 `AGENT_COMM_AUTO=0`（详见 agents/skills/agent_comm.py）。
def start_agent_comm() -> None:
    """起后台通信桥（不阻塞启动、不抛异常；探测在子线程里循环做）。"""
    try:
        from ..agents.skills.agent_comm import start_auto
        runtime.comm = start_auto(store=runtime.store, auth=runtime.auth,
                                  settings=runtime.settings, llm_factory=llm_for)
    except Exception as exc:              # 通信桥坏了也不能连带主服务起不来
        log.warning("通信桥自动启动失败（不影响主服务）：%s", exc)


def stop_agent_comm() -> None:
    if runtime.comm is not None:
        try:
            runtime.comm.stop()
        except Exception as exc:
            log.warning("通信桥停止失败：%s", exc)


def restart_agent_comm() -> None:
    """存储热切换后让通信桥用上新的 store / auth / settings。"""
    stop_agent_comm()
    start_agent_comm()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期：启动时托管通信桥，关闭时回收后台线程。"""
    start_agent_comm()
    try:
        yield
    finally:
        stop_agent_comm()


def comm_status() -> dict:
    """通信桥状态：后台线程是否在跑 + 通信服务 / Redis 的实时体检。"""
    from ..agents.skills.agent_comm import service_status
    if runtime.comm is not None:
        state = runtime.comm.state()
        link = service_status(runtime.comm.worker.cfg)
    else:
        state = {"running": False,
                 "reason": "未启动（AGENT_COMM_AUTO=0，或依赖缺失导致启动失败）"}
        link = service_status()
    return {"supervisor": state, "link": link}
