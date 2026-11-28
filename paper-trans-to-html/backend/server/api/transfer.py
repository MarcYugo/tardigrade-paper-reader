"""Transfer Key：账号把只读数据接口授权给其它 Agent（Bearer 校验，不走登录 Cookie）。"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ... import auth as auth_mod, records
from ...agents.llm import LlmUnavailable
from ...notes_out import (export_filename as note_out_filename,
                          export_paper as build_note_out_export,
                          to_json as note_out_json)
from .. import runtime
from ..deps import require_user, transfer_owner
from ..factories import llm_for
from ..views import doc_view, transfer_view
from .settings import scholar_agent_config


router = APIRouter()
log = logging.getLogger("uvicorn.error")


@router.get("/api/transfer-keys")
def api_list_transfer_keys(request: Request):
    """列出**当前账号**名下全部 transfer key（打码串 + 完整 Key 都回，仅本人可见）。"""
    user = require_user(request)
    return transfer_view(user["username"])


@router.post("/api/transfer-keys")
def api_create_transfer_key(request: Request, payload: dict = Body(default=None)):
    """生成一把新的 transfer key；`{"name": "给谁用的"}` 可选，便于区分。"""
    user = require_user(request)
    payload = payload or {}
    try:
        rec = runtime.auth.create_transfer_key(user["username"], payload.get("name") or "")
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    out = transfer_view(user["username"])
    out.update({"ok": True,
                "created": {"id": rec["id"], "name": rec["name"], "key": rec["key"]}})
    log.info("账号 %s 生成 transfer key（%s）", user["username"], rec["id"])
    return out


@router.delete("/api/transfer-keys/{key_id}")
def api_delete_transfer_key(request: Request, key_id: str):
    """吊销一把 transfer key（删除即立刻失效）。"""
    user = require_user(request)
    try:
        found = runtime.auth.delete_transfer_key(user["username"], key_id)
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not found:
        raise HTTPException(404, "该 transfer key 不存在或已删除")
    out = transfer_view(user["username"])
    out["ok"] = True
    log.info("账号 %s 删除 transfer key（%s）", user["username"], key_id)
    return out


@router.get("/api/transfer/whoami")
def api_transfer_whoami(request: Request):
    """用 transfer key 自检：返回该 key 属于哪个账号、能看到多少篇论文。"""
    who = transfer_owner(request)
    owner = who["username"]
    return {"ok": True, "owner": owner, "key_id": who["id"], "key_name": who["name"],
            "papers": len(runtime.store.list_for_user(owner))}


@router.get("/api/transfer/papers")
def api_transfer_papers(request: Request):
    """该 key 所属账号能看到的论文列表（与网站首页列表同源，不含版式数据）。"""
    who = transfer_owner(request)
    owner = who["username"]
    rows = []
    for rec in runtime.store.list_for_user(owner):
        view = doc_view(rec, owner)
        rows.append({k: view.get(k) for k in (
            "id", "title", "source", "num_pages", "parser", "status",
            "progress", "last_page", "last_read_at", "created_at",
            "highlights", "notes", "owner", "perm", "shared_count")})
    return {"ok": True, "owner": owner, "papers": rows}


@router.get("/api/transfer/papers/{doc_id}")
async def api_transfer_paper(request: Request, doc_id: str, mode: str = "mechanical",
                             download: bool = False):
    """单篇论文的笔记数据（与「📤 笔记导出」同一份 JSON 结构）。

    * `mode=mechanical`（默认）：纯本地、零 LLM 成本；
    * `mode=agent`：带上 LLM 分析报告（消耗**该 key 所属账号**的 LLM 配额）。
    `?download=1` 直接带回下载文件名；`format` 固定 JSON（图片导出不开放给 key）。
    """
    who = transfer_owner(request)
    owner = who["username"]
    rec = runtime.store.get_record(doc_id)
    if not rec or not records.perm_of(rec, owner):
        raise HTTPException(404, "论文不存在或已被删除")
    mode = str(mode or "mechanical").strip().lower()
    if mode not in ("mechanical", "agent"):
        raise HTTPException(400, "导出模式只能是 mechanical 或 agent")
    try:
        doc = runtime.store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc
    anno = runtime.store.get_annotations(doc_id)
    llm = None
    if mode == "agent":
        llm = llm_for(owner)
        try:
            llm.validate_chat([{"role": "user", "content": "导出笔记"}])
        except LlmUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
    try:
        export = await run_in_threadpool(
            build_note_out_export, doc=doc, rec=rec, anno=anno, mode=mode,
            llm=llm, username=owner, instructions="",
            scholar=scholar_agent_config(runtime.settings.scholar_merged(owner)),
            # 传 runtime.store/doc_id 才能复用参考文献核验缓存（Agent 模式第二次起快很多）
            store=runtime.store, doc_id=doc_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LlmUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not download:
        return export
    name = note_out_filename(export.get("paper") or {}, export.get("mode") or "mechanical")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "note-export.json"
    return Response(note_out_json(export), media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition": (
                        f"attachment; filename=\"{ascii_name}\"; "
                        f"filename*=UTF-8''{quote(name)}")})
