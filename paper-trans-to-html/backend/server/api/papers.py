"""论文本身：上传转换、原地重解析、阅读数据、改名、插图、引用与删除。"""
from __future__ import annotations

import logging
import re
import uuid

from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ... import converter, pdf_refs as refs_mod
from .. import parser_config, runtime
from ..config import DATA_DIR
from ..deps import doc_access, require_doc, require_user
from ..views import doc_slice, doc_view


_IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".gif": "image/gif"}

router = APIRouter()
log = logging.getLogger("uvicorn.error")


@router.post("/api/convert")
async def api_convert(request: Request, file: UploadFile = File(...)):
    user = require_user(request)
    name = file.filename or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "请上传 PDF 文件")
    doc_id = uuid.uuid4().hex
    tmp_dir = DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{doc_id}.pdf"
    tmp.write_bytes(await file.read())
    try:
        meta = await run_in_threadpool(converter.convert, runtime.store, doc_id, tmp, name,
                                       user["username"],
                                       parser_config.options_for(user["username"]))
    except Exception as exc:
        runtime.store.delete(doc_id)
        raise HTTPException(400, f"转换失败：{exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return meta


@router.post("/api/doc/{doc_id}/reparse")
async def api_doc_reparse(request: Request, doc_id: str,
                          file: UploadFile = File(...)):
    """用**这一篇的原始 PDF** 原地重解析版式数据。

    解析器升级（换后端 / 补公式 / 调 DPI）后，库里已存的论文不会自动跟着变；
    重新上传会生成新论文、笔记译文全丢，所以给一个原地重解析：只换
    `pages`/`parser`/`parser_info`/插图，笔记、高亮、译文、进度一个不动。
    解析失败不改动已有数据。
    """
    user = require_user(request)
    doc_access(request, doc_id, "write")
    name = file.filename or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "请上传这一篇的原始 PDF 文件")
    tmp_dir = DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"reparse-{doc_id}.pdf"
    tmp.write_bytes(await file.read())
    try:
        summary = await run_in_threadpool(
            converter.reparse, runtime.store, doc_id, tmp,
            parser_options=parser_config.options_for(user["username"]))
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc
    except Exception as exc:
        raise HTTPException(400, f"重新解析失败：{exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    # 版式已重写：清掉本进程的缓存条目兜底（其它进程/实例由 read_doc 的
    # rev 校验自动失效），下个请求从库重读，刷新页面即可见新版式。
    runtime.store.clear_cache(doc_id)
    log.info("版式已重新解析：%s → %s（带 latex 的块 %s）", doc_id,
             summary.get("parser"), summary.get("latex_blocks"))
    return {"ok": True, **summary}


@router.get("/api/docs")
def api_docs(request: Request):
    """当前账号能看到的论文：**自己的 + 别人共享给我的**。

    每行都带上 `perm`（owner/write/read）、`owner`、`shared`，以及**我自己的**阅读进度
    （共享论文的进度跟人走，不会覆盖所有者的）。
    """
    user = require_user(request)
    me = user["username"]
    return [doc_view(rec, me) for rec in runtime.store.list_for_user(me)]


@router.get("/api/doc/{doc_id}")
def api_doc(request: Request, doc_id: str,
            page_from: int | None = None, page_to: int | None = None):
    """版式文档。

    不带 `page_from` / `page_to` 时与以前完全一致（整篇下发，老调用方不受影响）；
    带参数时只下发这一段页（见 `_doc_slice`），供阅读页**逐页加载**：
    首屏只要看得见的那几页，剩下的在后台一批批补，等待时间与论文总页数解耦。
    """
    doc_access(request, doc_id)
    try:
        doc = runtime.store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc
    if page_from is None and page_to is None:
        return doc
    return doc_slice(doc, page_from, page_to)


@router.post("/api/doc/{doc_id}/title")
def api_doc_title(request: Request, doc_id: str, payload: dict = Body(...)):
    """重命名论文（阅读页顶栏的标题可以直接改）。

    标题在库里存了**两份**，必须一起改，否则首页与阅读页会各显示一个名字：
      · 记录（records）的 `title`：首页列表、共享信息、AI 整理的上下文都用它；
      · 版式文档（doc.json）的 `title`：阅读页顶栏、分享弹窗的文案用它。
    只动名字、不碰解析数据，所以不用走重解析流程；笔记/译文/进度都不受影响。
    有「可写」权限就能改名（只读者不行），与原文件名的 `source` 字段无关。
    """
    doc_access(request, doc_id, need="write")
    title = " ".join(str(payload.get("title") or "").split())[:200].strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    runtime.store.update_record(doc_id, title=title)
    try:
        doc = runtime.store.read_doc(doc_id)
    except FileNotFoundError:
        doc = None
    if isinstance(doc, dict):
        doc["title"] = title
        runtime.store.save_doc(doc_id, doc)
    log.info("论文改名：%s → %s", doc_id, title)
    return {"ok": True, "title": title}


@router.get("/api/doc/{doc_id}/refs")
def api_doc_refs(request: Request, doc_id: str):
    """引用跳转：PDF 内部链接（正文引用标记 → 参考文献条目）算好的映射。

    纯本地、零 LLM 成本（毫秒级），所以**不缓存、每次现算** —— 重解析后自然就是
    新数据；PDF 本身没有内部链接（绝大多数扫描件）时返回空映射，前端静默。
    数据结构见 `backend/pdf_refs/refs.py`。
    """
    doc_access(request, doc_id)
    try:
        doc = runtime.store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc
    return refs_mod.build_refs(doc)


@router.get("/api/doc/{doc_id}/img/{fname}")
def api_img(request: Request, doc_id: str, fname: str):
    """论文插图。本体存在存储后端里（MongoDB 走 GridFS，回退模式读磁盘），
    所以服务重启/重建容器都不会再出现“图打不开”的情况。"""
    if not re.fullmatch(r"p\d+_i\d+\.(png|jpe?g|webp|gif)", fname, re.I):
        raise HTTPException(400, "非法的图片名")
    doc_access(request, doc_id)
    data = runtime.store.get_image(doc_id, fname)
    if not data:
        raise HTTPException(404, "图片不存在")
    # 图片跟 doc_id 绑定、内容不会变，交给浏览器缓存，翻页/来回滚动不重复下载
    mime = _IMG_MIME.get(Path(fname).suffix.lower(), "application/octet-stream")
    return Response(data, media_type=mime,
                    headers={"Cache-Control": "private, max-age=604800"})


@router.delete("/api/doc/{doc_id}")
def api_delete_doc(request: Request, doc_id: str):
    require_doc(request, doc_id)          # 只能删自己的论文（共享者不能删）
    runtime.store.delete(doc_id)
    return {"ok": True}
