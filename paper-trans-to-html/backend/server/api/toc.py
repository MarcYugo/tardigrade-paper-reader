"""侧栏目录：PDF 书签优先，没有书签时用 LLM 从全文提取。"""
from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...agents.llm import LlmUnavailable
from ...translate import extract_outline
from .. import runtime
from ..deps import doc_access, require_user
from ..factories import translator_for


router = APIRouter()
log = logging.getLogger("uvicorn.error")


def _outline_context(doc: dict, max_chars: int = 14000) -> str:
    """把正文压成“按页给出候选标题行”的文本，呭给 LLM 排目录。

    标题几乎都短且不以句号结尾，所以只收短行（解析器标为 heading 的块优先保留），
    这样 token 花在刀刃上、正文长句不会淹没标题。
    """
    lines: list[str] = []
    used = 0
    all_pages = doc.get("pages") or []
    for pi, pg in enumerate(all_pages, start=1):
        picked: list[str] = []
        for blk in pg.get("texts") or []:
            t = " ".join((blk.get("text") or "").split())
            if not t:
                continue
            label = (blk.get("label") or "").lower()
            # Surya 分支的块自带归一化 kind（"heading"）；其它后端的老口径按标签名猜
            heading_like = (blk.get("kind") == "heading"
                            or label in ("title", "heading", "section", "section_header",
                                         "sectionheader", "paragraph_title",
                                         "doc_title", "sub_title"))
            if not heading_like and (len(t) > 120 or t.endswith((".", "。", ";", "；", ","))):
                continue
            picked.append(t[:160])
            if len(picked) >= 40:          # 一页最多 40 行
                break
        if not picked:
            continue
        chunk = [f"【第{pi}页】", *picked]
        size = sum(len(x) + 1 for x in chunk)
        if used + size > max_chars:
            lines.append(f"…（后面还有 {len(all_pages) - pi + 1} 页，已省略）")
            break
        lines.extend(chunk)
        used += size
    return "\n".join(lines)


@router.get("/api/doc/{doc_id}/toc")
def api_get_toc(request: Request, doc_id: str):
    """论文目录。PDF 自带书签在建论文时就存下了；没有书签则返回空，
    由前端按需调 POST 让 LLM 从全文提取（提取结果也会存起来，不再重复花 token）。"""
    rec, _perm = doc_access(request, doc_id)
    return {"items": rec.get("toc") or [], "source": rec.get("toc_source") or ""}


@router.post("/api/doc/{doc_id}/toc")
async def api_build_toc(request: Request, doc_id: str, payload: dict = Body(default=None)):
    """用 LLM 从全文提取目录。`{"force": true}` = 忽略已存的、重新提取。

    目录存在记录里、大家共用，所以需要写权限。
    """
    rec, _perm = doc_access(request, doc_id, "write")
    user = require_user(request)
    if rec.get("toc") and not (payload or {}).get("force"):
        return {"items": rec["toc"], "source": rec.get("toc_source") or ""}
    try:
        doc = runtime.store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc

    context = _outline_context(doc)
    if not context.strip():
        raise HTTPException(400, "这篇论文没有可用来提取目录的文本")
    try:
        items = await run_in_threadpool(
            extract_outline, translator_for(user["username"]),
            doc.get("title") or "", context)
    except LlmUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not items:
        raise HTTPException(400, "没能从全文里识别出章节标题")

    total = max(1, int(doc.get("num_pages") or 0))
    for it in items:                      # LLM 偶尔给越界页码，夹一下
        it["page"] = min(max(1, it.get("page") or 1), total)
    runtime.store.update_record(doc_id, toc=items, toc_source="llm",
                        toc_at=datetime.datetime.now().isoformat(timespec="seconds"))
    log.info("已用 LLM 为 %s 提取目录：%d 条", doc_id, len(items))
    return {"items": items, "source": "llm"}
