"""AI 辅助阅读（文末面板）：笔记整理、对话与导航动作。"""
from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ...agents.llm import LlmUnavailable
from ...agents.skills.ai_read import reply_agent_stream
from ...translate import Translator
from .. import ai_chat, runtime
from ..deps import doc_access, require_user
from ..factories import translator_for
from ...ai_read import (article_text, build_summary, clean_notes, clean_summary,
                        notes_fingerprint, summary_empty)


router = APIRouter()
log = logging.getLogger("uvicorn.error")


def _ai_doc_notes(doc_id: str) -> list[dict]:
    """该论文当前的笔记（存储层口径，带 block_id）：用来算指纹。"""
    try:
        anno = runtime.store.get_annotations(doc_id)
        return (anno.get("notes") or []) + (anno.get("free_notes") or [])
    except Exception:
        return []


def _ai_doc_data(doc_id: str) -> dict:
    try:
        return runtime.store.read_doc(doc_id)
    except Exception as exc:
        log.warning("AI 对话：读取 %s 的文档失败（%s）", doc_id, exc)
        return {}


def _ai_stale(rec: dict) -> bool:
    """整理之后笔记又增/删/改过 → 前端可以提示“笔记有更新，可重新整理”。

    用「笔记内容指纹」而不是时间戳：同一秒内的改动也看得出来，
    也不受时区/时钟偏差影响。
    """
    ai = rec.get("ai") or {}
    if not ai.get("summary"):
        return False
    fp = str(ai.get("notes_fp") or "")
    return bool(fp) and fp != notes_fingerprint(_ai_doc_notes(rec["id"]))


def _ai_state(rec: dict, tr: Translator) -> dict:
    """给前端的状态：已生成的整理 + 对话 + 是否可重新整理。"""
    ai = rec.get("ai") or {}
    return {
        "summary": ai.get("summary") or None,
        "summary_at": ai.get("summary_at"),
        "summary_edited_at": ai.get("summary_edited_at"),
        "notes_count": int(ai.get("notes_count") or 0),
        "chat": ai.get("chat") or [],
        "stale": _ai_stale(rec),
        "ready": tr.ready,
        "status_text": tr.status_text,
        "trigger_progress": ai_chat.AI_TRIGGER_PROGRESS,
    }


@router.get("/api/doc/{doc_id}/ai")
def api_get_ai(request: Request, doc_id: str):
    """文末 AI 面板的状态（摘要与对话都存在记录里，刷新/重新登录不会丢）。"""
    rec, _perm = doc_access(request, doc_id)
    user = require_user(request)
    return _ai_state(rec, translator_for(user["username"]))


@router.post("/api/doc/{doc_id}/ai/summary")
async def api_ai_summary(request: Request, doc_id: str, payload: dict = Body(default=None)):
    """把这篇论文的笔记整理成结构化摘要并保存。

    `notes` 由前端给出（`[{sent, note, page}]`——只有前端知道笔记挂在哪句话上）。
    已经整理过且笔记没再改时直接返回缓存；`{"force": true}` 则重新整理。
    重新整理时会带上**上一版整理（可能被用户手改过）+ 已发生的对话**当素材，
    所以是把讨论成果并进来，而不是推倒重来。
    """
    rec, _perm = doc_access(request, doc_id, "write")
    user = require_user(request)
    payload = payload or {}
    tr = translator_for(user["username"])
    notes = clean_notes(payload.get("notes"))
    if not notes:
        raise HTTPException(400, "还没有笔记可以整理：先在正文里选中句子写几条笔记")
    ai = dict(rec.get("ai") or {})
    if ai.get("summary") and not payload.get("force") and not _ai_stale(rec):
        return _ai_state(rec, tr)          # 笔记没变，不重复花 token
    try:
        summary = await run_in_threadpool(
            build_summary, tr, title=rec.get("title") or "", notes=notes,
            pages=int(rec.get("num_pages") or 0),
            progress=float(rec.get("progress") or 0),
            previous=ai.get("summary"), chat=ai.get("chat") or [])
    except LlmUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if summary_empty(summary):
        raise HTTPException(502, "模型没有返回可用的整理结果，请重试")
    ai.update({
        "summary": summary,
        "summary_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "notes_count": len(notes),
        "notes": notes,      # 存一份：对话时即使前端不传笔记也能带上上下文
        "notes_fp": notes_fingerprint(_ai_doc_notes(doc_id)),   # 判断“之后笔记有没有变”
    })
    ai.pop("summary_edited_at", None)      # 这一版是模型重算的，手改时间戳作废
    runtime.store.update_record(doc_id, ai=ai)
    log.info("已为 %s 整理 %d 条笔记，带 %d 条对话素材",
             doc_id, len(notes), len(ai.get("chat") or []))
    return _ai_state(runtime.store.get_record(doc_id) or rec, tr)


@router.post("/api/doc/{doc_id}/ai/summary/save")
def api_ai_summary_save(request: Request, doc_id: str, payload: dict = Body(...)):
    """保存**手工编辑**后的笔记整理（整份覆盖）。

    前端把改完的完整结构传回来（`{"summary": {...}}`）；只在已经有整理结果时可用。
    清洗用宽松上限，不会把用户写的长句子默默截掉。
    """
    rec, _perm = doc_access(request, doc_id, "write")
    user = require_user(request)
    ai = dict(rec.get("ai") or {})
    if not ai.get("summary"):
        raise HTTPException(400, "还没有整理结果可以编辑，先生成一份")
    summary = clean_summary(payload.get("summary"), loose=True)
    if summary_empty(summary):
        raise HTTPException(400, "整理内容不能全为空")
    ai["summary"] = summary
    ai["summary_edited_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    runtime.store.update_record(doc_id, ai=ai)
    return _ai_state(runtime.store.get_record(doc_id) or rec, translator_for(user["username"]))


@router.post("/api/doc/{doc_id}/ai/chat")
async def api_ai_chat(request: Request, doc_id: str, payload: dict = Body(...)):
    """论文阅读 Agent：带着整篇正文、公式、笔记与整理回答，可把内容写进右侧整理卡片并定位内容。

    正文由服务端从文档数据里现拼；公式位置在正文里是空格，对应的 LaTeX 附在每页末尾。
    Agent 写内容时走 `add_note_to_summary`：按分区追加到**右侧「笔记整理」卡片**，
    不新建笔记。

    SSE 同时传输回复增量、工具状态与页面动作，结束时给出落库后的完整对话。
    """
    rec, _perm = doc_access(request, doc_id, "write")
    user = require_user(request)
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "请输入内容")
    if len(message) > ai_chat.AI_MAX_MESSAGE:
        raise HTTPException(400, f"消息太长了（上限 {ai_chat.AI_MAX_MESSAGE} 字）")
    tr = translator_for(user["username"])
    try:
        tr.validate_chat([{"role": "user", "content": message}])
    except LlmUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    ai = dict(rec.get("ai") or {})
    history = ai.get("chat") or []
    doc = await run_in_threadpool(_ai_doc_data, doc_id)
    article = article_text(doc)
    stored_anno = await run_in_threadpool(runtime.store.get_annotations, doc_id)
    try:
        pending_navigation = ai_chat.normalize_pending_navigation(
            doc, stored_anno, payload.get("pending_navigation"),
            payload.get("navigation_answer"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    navigation_answer = payload.get("navigation_answer")
    notes = ai_chat.merge_notes(
        clean_notes(payload.get("notes")) or (ai.get("notes") or []), stored_anno)
    execute_tool = ai_chat.build_tool_executor(
        doc=doc, anno=stored_anno, ai=ai, doc_id=doc_id,
        pending_navigation=pending_navigation, navigation_answer=navigation_answer)
    try:
        # 这一步就把能提前发现的错误（没配 LLM / 没内容 / 参数不对）抛出来，
        # 而不是等 SSE 头已经发出去了才报错
        chunks = reply_agent_stream(tr, title=rec.get("title") or "", notes=notes,
                                    summary=ai.get("summary"), history=history,
                                    message=message, article=article,
                                    execute_tool=execute_tool,
                                    pending_navigation=pending_navigation,
                                    navigation_answer=navigation_answer)
    except LlmUnavailable as exc:
        # 只给原因：前端会自己加上「对话失败：」前缀（不然会重复）
        raise HTTPException(503, str(exc)) from exc

    return StreamingResponse(
        ai_chat.sse_stream(chunks, ai=ai, history=history, message=message,
                           doc_id=doc_id, tr=tr),
        media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",           # 别让中间层缓存/攒够再发
            "X-Accel-Buffering": "no",            # nginx 后面也一样边收边发
        })


@router.post("/api/doc/{doc_id}/ai/chat/reset")
def api_ai_chat_reset(request: Request, doc_id: str):
    """清空这篇的对话记录（已生成的笔记整理保留）。对话是共享内容，所以需要写权限。"""
    rec, _perm = doc_access(request, doc_id, "write")
    ai = dict(rec.get("ai") or {})
    ai["chat"] = []
    runtime.store.update_record(doc_id, ai=ai)
    return {"ok": True, "chat": []}
