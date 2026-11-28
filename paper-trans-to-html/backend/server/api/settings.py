"""按账号的设置：LLM 服务、学术库检索、绘图沙盒、PDF 解析后端。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ... import auth as auth_mod, pdf_parser
from ...agents.llm import LlmUnavailable, probe_llm
from ...translate import resolve_lang
from .. import runtime
from ..config import CONFIG_FILE, PARSER_BACKENDS
from ..deps import require_user
from ..factories import translator_for
from ..parser_config import options_for


router = APIRouter()
log = logging.getLogger("uvicorn.error")


def scholar_agent_config(conf: dict) -> dict:
    """`runtime.settings.scholar_merged()` → 笔记导出技能认识的配置键名。"""
    conf = conf or {}
    return {
        "OPENALEX_API_KEY": str(conf.get("openalex_key") or "").strip(),
        "SEMANTIC_SCHOLAR_API_KEY": str(conf.get("semantic_scholar_key") or "").strip(),
        "SEMANTIC_SCHOLAR_BASE_URL": str(conf.get("s2_base_url") or "").strip(),
        "SCHOLARLY_MAILTO": str(conf.get("mailto") or "").strip(),
    }


@router.get("/api/settings/llm")
def api_get_llm(request: Request):
    """返回**当前账号**的 LLM 配置；API Key 只回打码串，明文不出服务端。"""
    user = require_user(request)
    tr = translator_for(user["username"])
    return runtime.settings.describe(user["username"],
                             active_key=tr.key, status_text=tr.status_text)


@router.post("/api/settings/llm")
def api_save_llm(request: Request, payload: dict = Body(...)):
    """保存**当前账号**的一套 LLM 配置（最多 3 套，按 `profile_id` 定位）。

    - `api_key` 不传/传 null = 沿用该套已保存的 Key，传空串 = 清空；
    - `profile_id` 不传/传 `"new"` = 新建一套（超过上限报 400），传已存在的 id = 改写那一套。
    保存的这套会**立即成为当前生效配置**（相当于保存即切换）。

    另外可选带 `target_lang`（译文目标语言，如 `zh-CN` / `en` / `ja`）：它存在
    `users.prefs.target_lang`，与上面几套配置槽无关，改语言不会动 Key/地址/模型。
    """
    user = require_user(request)
    name = user["username"]
    provider = (payload.get("provider") or "deepseek").strip().lower()
    base_url = (payload.get("base_url") or "").strip()
    model = (payload.get("model") or "").strip()
    raw_key = payload.get("api_key")
    api_key = raw_key.strip() if isinstance(raw_key, str) else None
    # 传空串 = 用户明确要求清除本账号的 Key（允许，清除后会回落到环境变量/config.json/免费兜底）
    explicit_clear = raw_key == ""

    # 目标语言先在写配置之前校验，免得“Key 写进去了、语言却报错”
    raw_lang = payload.get("target_lang")
    target_lang = None
    if isinstance(raw_lang, str) and raw_lang.strip():
        try:
            target_lang = resolve_lang(raw_lang)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    raw_pid = payload.get("profile_id")
    profile_id = raw_pid.strip() if isinstance(raw_pid, str) else None
    if profile_id == "":
        profile_id = None
    raw_name = payload.get("profile_name")
    profile_name = raw_name.strip() if isinstance(raw_name, str) else None

    if provider != "google":
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(400, "服务地址需以 http:// 或 https:// 开头")
        if not model:
            raise HTTPException(400, "请填写模型名")
        if not explicit_clear:
            # 新建这套时没有历史 Key，就沿用本账号当前生效的 Key
            have = api_key or runtime.settings.profile_key(name, profile_id)
            if have is None:
                have = runtime.settings.merged(name)["api_key"]
            if not have:
                raise HTTPException(400, "请填写 API Key")

    try:
        runtime.settings.save_llm(name, provider=provider, base_url=base_url, model=model,
                          api_key=api_key, user=name,
                          profile_id=profile_id, profile_name=profile_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if target_lang:
        runtime.settings.set_target_lang(name, target_lang)
    tr = translator_for(name)          # 无需重启：配置存在账号里，每次用都重新装配
    out = runtime.settings.describe(name, active_key=tr.key, status_text=tr.status_text)
    out.update({"ok": True, "ready": tr.ready})
    return out


@router.post("/api/settings/llm/activate")
def api_activate_llm(request: Request, payload: dict = Body(...)):
    """切换到本账号已保存的另一套 LLM 配置（`{"profile_id": "..."}`）。"""
    user = require_user(request)
    name = user["username"]
    try:
        runtime.settings.activate_profile(name, (payload.get("profile_id") or "").strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    tr = translator_for(name)          # 切换后立刻按新配置装配
    out = runtime.settings.describe(name, active_key=tr.key, status_text=tr.status_text)
    out.update({"ok": True, "ready": tr.ready})
    return out


@router.post("/api/settings/llm/delete")
def api_delete_llm(request: Request, payload: dict = Body(...)):
    """删除本账号已保存的一套 LLM 配置（删的若是当前生效那套，会自动切到剩下的第一套）。"""
    user = require_user(request)
    name = user["username"]
    try:
        runtime.settings.delete_profile(name, (payload.get("profile_id") or "").strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    tr = translator_for(name)
    out = runtime.settings.describe(name, active_key=tr.key, status_text=tr.status_text)
    out.update({"ok": True, "ready": tr.ready})
    return out


@router.post("/api/settings/llm/test")
async def api_test_llm(request: Request, payload: dict = Body(...)):
    """用一次极小的请求验证 地址/Key/模型 三件套，失败给出可读原因。

    表单里现填的值优先；`api_key` 没填（页面只显示打码串）时，用**所选配置槽已保存的 Key**，
    这样“切换过去但不改 Key”也能直接测。
    """
    user = require_user(request)
    saved = runtime.settings.merged(user["username"])
    pid = (payload.get("profile_id") or "").strip()
    prof = next((p for p in runtime.settings.profile_list(user["username"]) if p["id"] == pid), None)
    raw_key = payload.get("api_key")
    key = raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else None
    if key is None:
        key = runtime.settings.profile_key(user["username"], pid)
    if not key:
        key = saved["api_key"]
    base = (payload.get("base_url") or (prof or {}).get("base_url") or saved["base_url"]).strip()
    model = (payload.get("model") or (prof or {}).get("model") or saved["model"]).strip()
    try:
        msg = await run_in_threadpool(probe_llm, base, key, model)
    except LlmUnavailable as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "message": msg}


@router.get("/api/settings/scholar")
def api_get_scholar(request: Request):
    """返回**当前账号**的学术库（OpenAlex / Semantic Scholar）配置。

    与 LLM 配置一样按账号隔离：Key 只回打码串，并标明每项生效来源
    （本账号配置 / 环境变量 / `.env`）。
    """
    user = require_user(request)
    return runtime.settings.scholar_describe(user["username"])


@router.post("/api/settings/scholar")
def api_save_scholar(request: Request, payload: dict = Body(default=None)):
    """保存**当前账号**的学术库配置（用于笔记导出时的参考文献核验）。

    - 三个字段（`openalex_key` / `semantic_scholar_key` / `mailto`）都可选：
      不传 = 不改，传空串 = 清空；
    - `{"reset": true}` = 删掉本账号设置，回落到环境变量 / `.env`。
    """
    user = require_user(request)
    name = user["username"]
    payload = payload or {}
    if payload.get("reset"):
        runtime.settings.clear_scholar(name)
        return runtime.settings.scholar_describe(name)

    def _field(key: str, limit: int = 300) -> str | None:
        raw = payload.get(key)
        if raw is None:
            return None
        value = str(raw).strip()
        if len(value) > limit:
            raise HTTPException(400, f"{key} 太长（最多 {limit} 个字符）")
        return value

    try:
        runtime.settings.save_scholar(name, openalex_key=_field("openalex_key"),
                              semantic_scholar_key=_field("semantic_scholar_key"),
                              s2_base_url=_field("s2_base_url", 200),
                              mailto=_field("mailto", 200), user=name)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return runtime.settings.scholar_describe(name)


@router.post("/api/settings/scholar/test")
def api_test_scholar(request: Request, payload: dict = Body(default=None)):
    """用当前账号（或表单里还没保存的）Key 试一下各学术库能不能用。"""
    user = require_user(request)
    conf = runtime.settings.scholar_merged(user["username"])
    payload = payload or {}
    for field in ("openalex_key", "semantic_scholar_key", "s2_base_url", "mailto"):
        raw = payload.get(field)
        if isinstance(raw, str) and raw.strip():
            conf[field] = raw.strip()
    from .agents.skills.note_out import probe_scholar
    try:
        return probe_scholar(scholar_agent_config(conf))
    except Exception as exc:                  # 网络异常不应 500，按“不可用”回前端
        return {"ok": False, "message": f"⚠️ 测试失败：{exc}", "checks": []}


@router.get("/api/settings/sandbox")
def api_get_sandbox(request: Request):
    """返回**当前账号**的绘图沙盒请求地址（Agent 笔记图片用）。

    默认设置不变：没填时沿用安装级配置（环境变量 `DRAW_SANDBOX_URL` → 内置本地地址）；
    填了就按账号存在 `users.prefs.sandbox` 里，只影响本账号的 Agent 图片导出。
    """
    user = require_user(request)
    return runtime.settings.sandbox_describe(user["username"])


@router.post("/api/settings/sandbox")
def api_save_sandbox(request: Request, payload: dict = Body(default=None)):
    """保存**当前账号**的沙盒请求地址。

    - `{"url": "http://host:8090"}`：允许只填到端口，后端会补成 `…/v1/run`；
    - `{"url": ""}` 或 `{"reset": true}`：清掉本账号设置，回落到环境变量 / 内置默认。
    """
    user = require_user(request)
    name = user["username"]
    payload = payload or {}
    if payload.get("reset"):
        runtime.settings.clear_sandbox(name)
        return runtime.settings.sandbox_describe(name)
    raw = payload.get("url")
    if raw is None:
        raise HTTPException(400, "缺少 url（传空串表示恢复默认）")
    try:
        runtime.settings.save_sandbox(name, str(raw), user=name)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return runtime.settings.sandbox_describe(name)


@router.get("/api/settings/parser")
def api_settings_parser_get(request: Request):
    """当前 PDF 解析(OCR)后端状态。"""
    user = require_user(request)
    return pdf_parser.status(
        CONFIG_FILE, account_options=options_for(user["username"]))


@router.post("/api/settings/parser")
def api_settings_parser_set(request: Request, payload: dict = Body(...)):
    """切换 PDF 解析后端：auto / surya / unlimited_ocr / pymupdf。

    保存到当前账号的 users.prefs.parser.backend；解析服务参数仍来自安装级配置。
    若环境变量 PDF_PARSER_BACKEND 已设置，它会覆盖账号选择
    (status 里的 env_override 会告诉前端)。
    """
    user = require_user(request)
    backend = str((payload or {}).get("backend") or "").strip().lower()
    if backend not in PARSER_BACKENDS:
        raise HTTPException(400, f"未知的解析后端：{backend or '(空)'}；"
                                 f"可选 {'/'.join(PARSER_BACKENDS)}")
    parser_prefs = runtime.auth.get_prefs(user["username"]).get("parser")
    parser_prefs = dict(parser_prefs) if isinstance(parser_prefs, dict) else {}
    parser_prefs["backend"] = backend
    try:
        runtime.auth.set_prefs(user["username"], {"parser": parser_prefs})
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("账号 %s 的 PDF 解析(OCR)后端已切换为 %s",
             user["username"], backend)
    parser_status = pdf_parser.status(
        CONFIG_FILE, account_options=options_for(user["username"]))
    return {"ok": True, "backend": backend, "parser": parser_status}
