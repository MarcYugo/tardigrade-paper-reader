"""翻译：批量块翻译与整篇已缓存译文下发。"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ...agents.llm import LlmUnavailable
from .. import runtime
from ..deps import doc_access, require_user
from ..factories import translator_for
from ...translate import (block_copy_text, cache_key, is_target_language,
                         needs_translation, page_formula_copies)


router = APIRouter()


@router.get("/api/doc/{doc_id}/zh_all")
def api_zh_all(request: Request, doc_id: str):
    """返回本论文所有已缓存译文的块 id -> 译文（按**当前账号的目标语言**过滤）。

    译文缓存是按「目标语言 + 块文本」存键的，换语言后这里自然只回新语言的译文。
    """
    user = require_user(request)
    doc_access(request, doc_id)
    try:
        doc = runtime.store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc
    lang = runtime.settings.target_lang(user["username"])
    cache = runtime.store.get_translations(doc_id)
    result: dict[str, str] = {}
    for page in doc.get("pages", []):
        copies = page_formula_copies(page)
        for blk in page.get("texts", []):
            # ⚠️ 必须用**带公式**的文本取键（前端送翻译的也是这份）：
            # 用裸 `blk["text"]` 会对不上，每次进页面都当没翻译过。
            text = block_copy_text(blk.get("text", ""), copies.get(str(blk.get("id"))))
            zh = cache.get(cache_key(text, lang))
            if zh:
                result[blk["id"]] = zh
    return result


@router.post("/api/translate")
async def api_translate(request: Request, payload: dict = Body(...)):
    """批量翻译块。body: {"doc_id":..., "blocks":[{"id","text"}, ...]}
    命中缓存直接返回，避免重复计费。

    译文语言用**当前账号**在设置里选的目标语言（`users.prefs.target_lang`，默认简体中文）；
    原文语言不用配，交给 LLM 自己识别。译文缓存按「目标语言 + 文本」存键，换语言后不会
    把上一门语言的译文当成本次结果，也不会覆盖它（切回去依然在）。

    返回三个字段，前端必须**区别对待**——把“这次没拿到”当成“不需要翻译”，
    会让这些段落在当前页面里永久变成"本段无译文"、再也点不动（踩过）：
      - `results` 拿到译文的块（含“本来就是目标语言”的块，原样返回原文）
      - `skipped` **本身不需要翻译**的块（空/极短/纯符号），可以不再重试
      - `failed`  这次**没拿到译文**的块（服务返回的条目缺失或为空），可重试
    """
    doc_id = payload.get("doc_id")
    blocks = payload.get("blocks") or []
    if not doc_id:
        raise HTTPException(400, "缺少 doc_id")
    if not blocks:
        return {"results": {}, "skipped": [], "failed": []}
    doc_access(request, doc_id)                # 顺带完成归属校验
    user = require_user(request)               # LLM 配置与目标语言都按账号取
    tr = translator_for(user["username"])      # 用当前账号自己的配置 + 目标语言
    lang = tr.target_lang

    cache = runtime.store.get_translations(doc_id)
    results: dict[str, str] = {}
    skipped: list[str] = []
    todo: list[dict] = []
    for b in blocks:
        bid = b.get("id")
        text = (b.get("text") or "").strip()
        if not needs_translation(text, lang):
            if is_target_language(text, lang):
                results[bid] = text             # 已经是目标语言：原文即译文，不算失败
            else:
                skipped.append(bid)             # 空/极短/纯符号：确实无需翻译
            continue
        key = cache_key(text, lang)
        if key in cache and cache[key]:
            results[bid] = cache[key]
        else:
            todo.append({"id": bid, "text": text, "key": key})

    failed: list[str] = []
    if todo:
        try:
            translated = await run_in_threadpool(
                tr.translate, [t["text"] for t in todo])
        except LlmUnavailable as exc:
            raise HTTPException(503, str(exc))
        updates = {}
        for t, zh in zip(todo, translated):
            if zh:
                updates[t["key"]] = zh
                results[t["id"]] = zh
            else:
                failed.append(t["id"])          # 服务没给结果 → 可重试，别标记 noZh
        if updates:
            runtime.store.set_translations(doc_id, updates)
    return {"results": results, "skipped": skipped, "failed": failed}
