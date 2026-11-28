"""笔记导出：论文分节 + 笔记/高亮 + 分析报告（JSON 或 PNG）。"""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ...agents.llm import LlmUnavailable
from ...agents.skills.note_out_image import run_note_out_image_agent
from ...notes_out import (export_filename as note_out_filename,
                          export_paper as build_note_out_export,
                          render_image as render_note_out_image,
                          to_json as note_out_json)
from .. import runtime
from ..deps import doc_access, require_user
from ..factories import llm_for
from .settings import scholar_agent_config


router = APIRouter()


@router.post("/api/doc/{doc_id}/note-out")
async def api_note_out(request: Request, doc_id: str, download: bool = False,
                       payload: dict = Body(default=None)):
    """笔记导出：论文内容分节 + 笔记/高亮 + 分析报告（JSON 或 PNG 图片）。

    * `{"mode": "mechanical"}`（默认）= 机械导出，纯本地、零 LLM 成本；
    * `{"mode": "agent"}` = Agent 导出：核验参考文献、提取代码地址并形成报告
      （需要当前账号配置了 OpenAI 兼容的 LLM）；Agent 图片会继续生成绘图代码并交给
      `draw-sandbox` 执行（需配置 `DRAW_SANDBOX_URL`，可选 `DRAW_SANDBOX_API_KEY`）。

    另有 `{"format": "json" | "image"}`（默认 JSON）与 `{"theme": "light" | "dark"}`
    （图片导出的配色，跟阅读页当前主题一致）。`?download=1` 直接带回下载文件名。

    机械图片导出使用三栏固定版式（概览 / 笔记与原文 / AI 整理笔记），
    用 Matplotlib 的 XKCD 手绘风绘制；Agent 图片默认沿用此风格并在第一栏增加
    文献核查摘要，可通过 `instructions` 改变风格或栏目。

    只读操作，有「只读」权限就能导出；给其它 Agent 取走的接口见
    `GET /api/transfer/papers/{doc_id}`（凭账号生成的 transfer key 调用，
    复用同一份 `notes_out.note_out.export_paper()` 结构）。
    """
    rec, _perm = doc_access(request, doc_id)          # 导出只读，不被权限挡住
    user = require_user(request)
    payload = payload or {}
    mode = str(payload.get("mode") or "mechanical").strip().lower()
    if mode not in ("mechanical", "agent"):
        raise HTTPException(400, "导出模式只能是 mechanical 或 agent")
    fmt = str(payload.get("format") or "json").strip().lower()
    if fmt not in ("json", "image"):
        raise HTTPException(400, "导出格式只能是 json 或 image")
    theme = "dark" if str(payload.get("theme") or "").strip().lower() == "dark" else "light"
    try:
        doc = runtime.store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc
    anno = runtime.store.get_annotations(doc_id)
    llm = None
    if mode == "agent":
        llm = llm_for(user["username"])
        try:
            # 先把「没配 LLM」这类问题变成 503，而不是等工具循环里才报错
            llm.validate_chat([{"role": "user", "content": "导出笔记"}])
        except LlmUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
    try:
        export = await run_in_threadpool(
            build_note_out_export, doc=doc, rec=rec, anno=anno, mode=mode,
            llm=llm, username=user["username"],
            instructions=str(payload.get("instructions") or ""),
            scholar=scholar_agent_config(
                runtime.settings.scholar_merged(user["username"])),
            # 整篇纯文本（不含 bbox）默认给；配图 base64 体积大，要显式开
            store=runtime.store, doc_id=doc_id,
            with_fulltext=bool(payload.get("include_fulltext", True)),
            with_images=bool(payload.get("include_images", False)),
            image_budget_mb=float(payload.get("image_budget_mb") or 24),
            # 参考文献核验结果默认复用（含上次的缓存）；传 true 强制重新联网核验
            refresh_references=bool(payload.get("refresh_references", False)))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LlmUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    if fmt == "image":
        try:
            if mode == "agent":
                data = await run_in_threadpool(
                    run_note_out_image_agent, llm, export=export,
                    instructions=str(payload.get("instructions") or ""),
                    theme=theme,
                    # 沙盒地址按账号取：没配过就用环境变量 / 内置默认，行为不变
                    sandbox_url=runtime.settings.sandbox_url(user["username"]))
            else:
                data = await run_in_threadpool(
                    render_note_out_image, export, theme=theme)
        except RuntimeError as exc:            # 没装 matplotlib 时给出可读的提示
            raise HTTPException(503, str(exc)) from exc
        except LlmUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        name = note_out_filename(export.get("paper") or {},
                                 export.get("mode") or "mechanical", "image")
        ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "note-export.png"
        return Response(data, media_type="image/png", headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{quote(name)}"),
        })
    body = note_out_json(export)
    if not download:
        return JSONResponse(export)
    name = note_out_filename(export.get("paper") or {}, export.get("mode") or "mechanical")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "note-export.json"
    return Response(body, media_type="application/json; charset=utf-8", headers={
        "Content-Disposition": (
            f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(name)}"),
    })
