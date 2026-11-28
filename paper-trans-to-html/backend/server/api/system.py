"""系统级接口：健康检查、前端配置、存储热切换、通信桥状态。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ...db import StorageUnavailable
from .. import lifecycle, migrate, runtime
from ..deps import current_user, require_user
from ..factories import translator_for
from ..parser_config import status_cached


router = APIRouter()
log = logging.getLogger("uvicorn.error")


@router.get("/api/health")
def api_health():
    return {"ok": True, "service": "paper-trans-to-html", "storage": runtime.storage_backend}


@router.get("/api/config")
def api_config(request: Request):
    """当前账号的翻译/解析/存储状态（翻译服务按账号隔离，所以必须登录后看）。"""
    user = require_user(request)
    tr = translator_for(user["username"])
    return {
        "translator": tr.status_text,
        "ready": tr.ready,
        "backend": tr.backend,
        "llm_owner": user["username"],
        # PDF 解析后端：
        #   auto / pymupdf → 本地解析方案
        #     （PyMuPDF 版式 + texify 公式 + OXR 版面 / SLANet 表格，见 backend_pymupdf.py）
        #   surya → 整页 OCR：文字/公式/插图全用 Surya 数据
        #   unlimited_ocr → 整页 OCR：文字/公式/表格/插图全用 Unlimited-OCR 数据
        "parser": status_cached(user["username"]),
        # 数据存储后端：mongo(MongoDB) / json(本地文件回退)
        "storage": runtime.storage_state(),
    }


@router.get("/api/storage/status")
def api_storage_status(request: Request):
    require_user(request)
    return runtime.storage_state()


@router.get("/api/agent-comm/status")
def api_agent_comm_status(request: Request):
    """通信桥状态：后台线程是否在跑 + 通信服务 / Redis 的实时体检。"""
    require_user(request)
    return lifecycle.comm_status()


@router.post("/api/storage/reconnect")
def api_storage_reconnect(request: Request):
    """重新探测 MongoDB 并热切换存储后端（不用重启服务）。

    切到 MongoDB 后，当前会话（存在旧后端里）可能不再有效，
    响应里的 `relogin` 会告诉前端去重新登录。
    """
    require_user(request)
    before = runtime.storage_backend
    try:
        runtime.rebuild()
    except StorageUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    switched = before != runtime.storage_backend
    migrate.adopt_legacy_data()            # 换后端后也把无归属的历史论文/旧版 LLM 配置接过来
    lifecycle.restart_agent_comm()           # 通信桥持有 runtime.store/runtime.auth 引用，换库后要用新的
    out = runtime.storage_state()
    # 换后端后旧会话未必存在（比如旧的是 json 会话、新库里没这个 token）
    out["switched"] = switched
    out["relogin"] = switched and not current_user(request)
    log.info("数据存储已切换：%s → %s", before, runtime.storage_backend)
    return out
