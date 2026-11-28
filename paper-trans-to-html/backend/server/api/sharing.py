"""共享：所有者把论文 + 笔记共享给他人并决定读写权限，共享者也能自行退出。"""
from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Body, HTTPException, Request

from ... import records
from .. import runtime
from ..deps import doc_access, require_doc, require_user
from ..views import share_state


router = APIRouter()
log = logging.getLogger("uvicorn.error")


def _account_names() -> set[str]:
    return {str(u.get("username") or "").strip() for u in runtime.auth.list_users()}


@router.get("/api/doc/{doc_id}/share")
def api_get_share(request: Request, doc_id: str):
    """这篇论文的共享名单（**任何有权限的人都能看**，但只有所有者能改）。"""
    rec, _perm = doc_access(request, doc_id)
    me = require_user(request)["username"]
    return share_state(rec, me)


@router.post("/api/doc/{doc_id}/share")
def api_set_share(request: Request, doc_id: str, payload: dict = Body(...)):
    """新增或修改一条共享：`{user, perm}`，`perm` 是 `read`（只读）/ `write`（可写）。

    只能由论文所有者操作（不能共享给自己，目标账号必须存在）。
    """
    rec = require_doc(request, doc_id)
    me = require_user(request)["username"]
    target = str(payload.get("user") or "").strip()
    perm = str(payload.get("perm") or "read").strip().lower()
    if perm not in records.SHARE_PERMS:
        raise HTTPException(400, "权限只能是 read 或 write")
    if not target:
        raise HTTPException(400, "请填写要共享给的账号")
    if target == me:
        raise HTTPException(400, "这就是你自己的论文，不用共享给自己")
    if target not in _account_names():
        raise HTTPException(400, f"账号「{target}」不存在（对方需要先注册/被创建）")
    items = [s for s in records.shared_entries(rec) if s["user"] != target]
    items.append({"user": target, "perm": perm, "by": me,
                  "at": datetime.datetime.now().isoformat(timespec="seconds")})
    runtime.store.update_record(doc_id, shared=items)
    log.info("%s 把论文 %s 共享给 %s（%s）", me, doc_id, target, perm)
    return share_state(runtime.store.get_record(doc_id) or rec, me)


@router.delete("/api/doc/{doc_id}/share/{username}")
def api_del_share(request: Request, doc_id: str, username: str):
    """取消某条共享（只有所有者能操作）。顺手清掉那个人的阅读状态。"""
    rec = require_doc(request, doc_id)
    me = require_user(request)["username"]
    target = username.strip()
    items = [s for s in records.shared_entries(rec) if s["user"] != target]
    if len(items) == len(records.shared_entries(rec)):
        raise HTTPException(404, "这条共享不存在")
    runtime.store.update_record(doc_id, shared=items,
                        **records.drop_reading(rec, target))
    log.info("%s 取消了 %s 对论文 %s 的共享", me, target, doc_id)
    return share_state(runtime.store.get_record(doc_id) or rec, me)


@router.post("/api/doc/{doc_id}/share/leave")
def api_leave_share(request: Request, doc_id: str):
    """被共享的人自己退出（不需要所有者权限；所有者不能“退出”自己的论文）。"""
    rec, perm = doc_access(request, doc_id)
    me = require_user(request)["username"]
    if perm == "owner":
        raise HTTPException(400, "这是你自己的论文，只能删除或取消别人的共享")
    items = [s for s in records.shared_entries(rec) if s["user"] != me]
    runtime.store.update_record(doc_id, shared=items, **records.drop_reading(rec, me))
    log.info("%s 退出了论文 %s 的共享", me, doc_id)
    return {"ok": True}
