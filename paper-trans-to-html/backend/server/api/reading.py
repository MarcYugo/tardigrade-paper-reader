"""阅读状态：进度、标注/笔记、所选句译文卡片（笔记写权限在 `doc_access` 里判）。"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, Body, HTTPException, Request

from ... import records
from ...translate import normalize_lang
from .. import runtime
from ..deps import doc_access, require_user


router = APIRouter()


@router.get("/api/doc/{doc_id}/progress")
def api_get_progress(request: Request, doc_id: str):
    rec, perm = doc_access(request, doc_id)
    user = require_user(request)
    out = records.reading_of(rec, user["username"])
    out["progress"] = float(out.get("progress") or 0)
    out["status"] = out.get("status") or "unread"
    out["last_page"] = int(out.get("last_page") or 0)
    # 精确阅读位置（重进阅读器时还原到上次离开处）；老数据没有 → None，前端退回页码
    out["last_pos"] = out.get("last_pos") if isinstance(out.get("last_pos"), dict) else None
    out["perm"] = perm
    out["owner"] = records.owner_of(rec)
    out["shared_count"] = len(records.shared_entries(rec))
    return out


@router.post("/api/doc/{doc_id}/progress")
def api_set_progress(request: Request, doc_id: str, payload: dict = Body(...)):
    """记阅读进度。只读共享也能调 —— 记的是**自己**的进度。"""
    rec, _perm = doc_access(request, doc_id)
    user = require_user(request)
    fields = {}
    if isinstance(payload.get("last_page"), int):
        fields["last_page"] = payload["last_page"]
    if isinstance(payload.get("progress"), (int, float)):
        fields["progress"] = round(max(0.0, min(1.0, float(payload["progress"]))), 4)
    st = payload.get("status")
    if st in ("unread", "reading", "done"):
        fields["status"] = st
    # 精确阅读位置：`{pi, ratio}`（页下标 + 页内比例）或 `{pi:-1, fromBottom}`（文末 AI 面板）。
    # 这是**覆盖写**（往回翻就记往回翻的位置），与只增不减的 progress / last_page 不同。
    pos = payload.get("last_pos")
    if isinstance(pos, dict):
        clean: dict = {}
        if isinstance(pos.get("pi"), (int, float)):
            clean["pi"] = int(pos["pi"])
        if isinstance(pos.get("ratio"), (int, float)):
            clean["ratio"] = round(max(0.0, min(1.0, float(pos["ratio"]))), 5)
        if isinstance(pos.get("fromBottom"), (int, float)):
            clean["fromBottom"] = round(max(0.0, float(pos["fromBottom"])), 1)
        if clean:
            fields["last_pos"] = clean
    fields["last_read_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    updated = runtime.store.update_record(doc_id, **records.reading_patch(rec, user["username"], fields))
    return records.reading_of(updated or rec, user["username"])


@router.get("/api/doc/{doc_id}/anno")
def api_get_anno(request: Request, doc_id: str):
    """标注 + 笔记。

    论文**共享给别人**时，每条笔记会多带一个 `by`（谁写的），前端据此在卡片上标作者；
    没共享时不下发 `by` —— 那种情况下只有自己能看，标了没意义。
    """
    rec, _perm = doc_access(request, doc_id)
    return records.anno_with_authors(runtime.store.get_annotations(doc_id), rec)


@router.post("/api/doc/{doc_id}/anno")
def api_apply_anno(request: Request, doc_id: str, payload: dict = Body(...)):
    rec, _perm = doc_access(request, doc_id, "write")      # 只读共享不能改笔记/高亮
    user = require_user(request)
    action = payload.get("action")
    if action not in ("add_highlight", "remove_highlight", "add_note", "remove_note",
                      "add_free_note", "update_free_note", "remove_free_note"):
        raise HTTPException(400, "不支持的操作")
    title = str(payload.get("title") or "").strip()
    text = payload.get("text")
    if action in ("add_free_note", "update_free_note"):
        if payload.get("title") is not None and not isinstance(payload.get("title"), str):
            raise HTTPException(400, "独立笔记标题格式无效")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "独立笔记内容不能为空")
        if len(text) > 12000:
            raise HTTPException(400, "独立笔记内容不能超过 12000 字")
        if len(title) > 200:
            raise HTTPException(400, "独立笔记标题不能超过 200 字")
        note_id = str(payload.get("note_id") or "").strip() or None
        if action in ("update_free_note", "remove_free_note") and not note_id:
            raise HTTPException(400, "缺少独立笔记 ID")
        if action in ("update_free_note", "remove_free_note") and not any(
                n.get("id") == note_id
                for n in (runtime.store.get_annotations(doc_id).get("free_notes") or [])
                if isinstance(n, dict)):
            raise HTTPException(404, "独立笔记不存在")
    else:
        note_id = payload.get("note_id")
    anno = runtime.store.apply_anno(
        doc_id, action,
        block_id=payload.get("block_id"),
        color=payload.get("color"),
        text=text,
        note_id=note_id,
        title=title,
        by=user["username"],        # 笔记要记下是谁写的（共享论文里会显示）
    )
    return records.anno_with_authors(anno, rec)


@router.get("/api/doc/{doc_id}/seltrans")
def api_get_seltrans(request: Request, doc_id: str):
    user = require_user(request)
    doc_access(request, doc_id)
    lang = runtime.settings.target_lang(user["username"])
    # 老卡片没有 lang 字段，按默认语言（简体中文）算，升级后不至于“卡片全没了”
    return [c for c in runtime.store.get_seltrans(doc_id)
            if normalize_lang(c.get("lang")) == lang]


@router.post("/api/doc/{doc_id}/seltrans")
def api_add_seltrans(request: Request, doc_id: str, payload: dict = Body(...)):
    """保存一张所选句译文卡（同 key 覆盖），并记下它的目标语言。"""
    user = require_user(request)
    doc_access(request, doc_id)
    key = (payload.get("key") or "").strip()
    text = payload.get("text") or ""
    if not key or not text.strip():
        raise HTTPException(400, "缺少 key 或 text")
    return runtime.store.add_seltrans(doc_id, {
        "key": key,
        "item_id": payload.get("item_id") or key.split("#", 1)[0],
        "si_from": int(payload.get("si_from") or 0),
        "text": text,
        "lang": runtime.settings.target_lang(user["username"]),
    })


@router.delete("/api/doc/{doc_id}/seltrans")
def api_del_seltrans(request: Request, doc_id: str, key: str | None = None):
    """传 key 只删一张；不传 key 清空该论文的全部所选句译文卡（需要写权限）。"""
    doc_access(request, doc_id, "read" if key else "write")
    return runtime.store.del_seltrans(doc_id, key) if key else runtime.store.clear_seltrans(doc_id)
