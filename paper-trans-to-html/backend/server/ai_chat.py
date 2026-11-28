"""论文阅读 Agent 的对话管道：导航目标校验、工具执行、SSE 流式输出与落库。

HTTP 层（`server/api/ai.py`）只负责权限、参数读取与响应封装；这里不碰 FastAPI，
校验失败统一抛 `ValueError`（由路由转成 400），LLM 不可用仍抛 `LlmUnavailable`。
"""
from __future__ import annotations

import datetime
import json
import logging

from ..agents.llm import LlmUnavailable
from ..agents.skills.ai_read import (add_summary_item, locate_page,
                                    locate_text_block)
from ..ai_read import clean_notes
from ..translate import Translator
from . import runtime


log = logging.getLogger("uvicorn.error")

AI_KEEP_TURNS = 40          # 每篇保留的对话条数（user+assistant 合计）
AI_MAX_MESSAGE = 4000       # 单条提问长度上限
AI_TRIGGER_PROGRESS = 0.95  # 读到这个进度就自动整理笔记（前端用它决定何时触发）


def sse(obj: dict) -> str:
    """SSE 帧：一行 `data: {json}` + 空行分隔（JSON 里不会出现裸换行）。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def normalize_pending_navigation(doc: dict, anno: dict, pending_navigation,
                                 navigation_answer) -> dict | None:
    """校验并规整前端回传的“待确认导航目标”（只允许用户刚看到过的那一页/那条笔记）。"""
    if navigation_answer not in (None, "yes", "no"):
        raise ValueError("导航确认选项无效")
    if pending_navigation is None:
        if navigation_answer is not None:
            raise ValueError("缺少待确认的导航目标")
        return None
    if not isinstance(pending_navigation, dict):
        raise ValueError("待确认的导航目标无效")
    target = pending_navigation.get("target")
    if target == "paper":
        page = pending_navigation.get("page")
        if (isinstance(page, bool) or not isinstance(page, int)
                or page < 1 or page > len(doc.get("pages") or [])):
            raise ValueError("待确认的论文页码无效")
        block_id = pending_navigation.get("block_id")
        page_texts = (doc.get("pages") or [])[page - 1].get("texts") or []
        if block_id is not None and (
                not isinstance(block_id, str)
                or not any(isinstance(block, dict) and block.get("id") == block_id
                           for block in page_texts)):
            raise ValueError("待确认的相关文本块无效")
        clean = {"target": "paper", "page": page}
        if block_id:
            clean["block_id"] = block_id
        return clean
    if target == "note":
        note_id = pending_navigation.get("note_id")
        all_notes = (anno.get("notes") or []) + (anno.get("free_notes") or [])
        if (not isinstance(note_id, str)
                or not any(n.get("id") == note_id for n in all_notes if isinstance(n, dict))):
            raise ValueError("待确认的笔记不存在")
        return {"target": "note", "note_id": note_id}
    raise ValueError("待确认的导航类型无效")


def merge_notes(notes: list[dict], anno: dict) -> list[dict]:
    """把独立笔记（自由笔记）并进对话素材，按 id 去重（独立笔记以存储为准）。"""
    free_note_rows = []
    free_note_ids = set()
    for note in anno.get("free_notes") or []:
        if isinstance(note, dict) and note.get("id") and note.get("text"):
            free_note_ids.add(note["id"])
            free_note_rows.append({
                "id": note["id"], "title": note.get("title") or "",
                "note": note.get("text") or "", "page": 0,
            })
    return clean_notes(free_note_rows + [
        note for note in notes if note.get("id") not in free_note_ids
    ])


def build_tool_executor(*, doc: dict, anno: dict, ai: dict, doc_id: str,
                        pending_navigation: dict | None, navigation_answer):
    """注入给 Agent 的工具实现：写整理卡片 / 定位论文或笔记。

    导航先回 `confirmation` 让前端征求用户同意，只有用户确认过的那次跳转才真正执行。
    """
    def execute_tool(name: str, args: dict) -> dict:
        if name == "add_note_to_summary":
            # 写进右侧整理卡片（不新建笔记）：卡片为空时按分区先建起来
            result, summary = add_summary_item(ai.get("summary"), args)
            if result.get("ok"):
                ai["summary"] = summary
                ai["summary_edited_at"] = datetime.datetime.now().isoformat(
                    timespec="seconds")
                runtime.store.update_record(doc_id, ai=ai)
                result["action"] = {"type": "summary_changed", "summary": summary}
            return result

        target = args.get("target")
        if not isinstance(target, str):
            return {"ok": False, "message": "定位目标格式无效"}
        if target == "notes_panel":
            return {"ok": True, "message": "已打开笔记栏",
                    "action": {"type": "navigate", "target": "notes_panel"}}
        if target == "note":
            note_id = args.get("note_id")
            if not isinstance(note_id, str):
                return {"ok": False, "message": "笔记 ID 格式无效"}
            all_notes = (anno.get("notes") or []) + (anno.get("free_notes") or [])
            if not note_id or not any(n.get("id") == note_id for n in all_notes if isinstance(n, dict)):
                return {"ok": False, "message": "没有找到这条笔记"}
            action = {"type": "navigate", "target": "note", "note_id": note_id}
            label = "指定笔记"
        if target == "paper":
            page = args.get("page")
            total = len(doc.get("pages") or [])
            if page is not None and (isinstance(page, bool) or not isinstance(page, int)
                                     or page < 1 or page > total):
                return {"ok": False, "message": f"页码无效，本文共 {total} 页"}
            if page is None:
                query = args.get("query") or ""
                if not isinstance(query, str):
                    return {"ok": False, "message": "论文检索内容格式无效"}
                query = query.strip()[:500]
                page = locate_page(doc, query)
            else:
                query = args.get("query") or ""
                if not isinstance(query, str):
                    return {"ok": False, "message": "论文检索内容格式无效"}
                query = query.strip()[:500]
            if not page:
                return {"ok": False, "message": "无法从论文内容中确定相关页"}
            action = {"type": "navigate", "target": "paper", "page": page}
            block_id = locate_text_block(doc, page, query)
            if block_id:
                action["block_id"] = block_id
            label = f"论文第 {page} 页"
        if target in ("paper", "note"):
            if pending_navigation:
                same_destination = (
                    pending_navigation.get("target") == action.get("target")
                    and pending_navigation.get("page") == action.get("page")
                    and pending_navigation.get("note_id") == action.get("note_id")
                )
                if navigation_answer == "no":
                    return {"ok": False, "message": "用户拒绝了跳转，不要导航"}
                if navigation_answer != "yes":
                    return {"ok": False, "message": "用户尚未确认跳转，不要导航"}
                if not same_destination:
                    return {"ok": False, "message": "只能执行用户刚确认的导航目标"}
                if target == "paper" and pending_navigation.get("block_id"):
                    action["block_id"] = pending_navigation["block_id"]
                return {"ok": True, "message": f"用户已回答，正在定位到{label}",
                        "action": action}
            return {"ok": True,
                    "message": f"已找到{label}。先询问用户是否定位；确认后才能执行跳转。",
                    "confirmation": action}
        return {"ok": False, "message": "未知的定位目标"}

    return execute_tool


def sse_stream(chunks, *, ai: dict, history: list[dict], message: str,
               doc_id: str, tr: Translator):
    """把 Agent 增量流式转成 SSE 帧，结束时把本轮问答落库。"""
    parts: list[str] = []
    try:
        for event in chunks:
            if event.get("type") == "delta":
                parts.append(event.get("text") or "")
            yield sse(event)
    except LlmUnavailable as exc:
        yield sse({"type": "error", "message": str(exc)})
        return
    except Exception as exc:                 # 客户端断开 / 上游抽风：别把连接吊死
        log.warning("AI 对话中断：%s", exc)
        yield sse({"type": "error", "message": f"对话中断：{exc}"})
        return
    reply = "".join(parts).strip()
    if not reply:
        yield sse({"type": "error", "message": "模型没有返回内容，请重试"})
        return
    now = datetime.datetime.now().isoformat(timespec="seconds")
    latest = dict(ai)
    latest["chat"] = (history + [
        {"role": "user", "content": message, "at": now},
        {"role": "assistant", "content": reply, "at": now},
    ])[-AI_KEEP_TURNS:]
    runtime.store.update_record(doc_id, ai=latest)
    yield sse({"type": "done", "chat": latest["chat"],
               "ready": tr.ready, "status_text": tr.status_text})
