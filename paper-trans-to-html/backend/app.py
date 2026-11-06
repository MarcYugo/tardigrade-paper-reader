"""FastAPI 应用：登录认证 + 上传/转换/阅读记录/标注/翻译 等 HTTP 接口 + 前端静态托管。

数据默认存 MongoDB（连接参数读 `mongo_configuration/.env`，见 `db.py`）；连不上时按
`APP_STORAGE` 回退本地 JSON，并在 `/api/config` 里给出提示与“重试连接”入口。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

from . import auth as auth_mod
from . import converter
from . import pdf_parser
from . import records
from . import pdf_refs as refs_mod
from .ai_read import (add_summary_item, article_text, build_summary, clean_notes,
                      clean_summary, locate_page, locate_text_block,
                      notes_fingerprint, reply_agent_stream, summary_empty)
from .db import StorageUnavailable, mongo
from .storage import build as build_storage
from .storage import rebuild as rebuild_storage
from .translate import (TranslationUnavailable, Translator, block_copy_text,
                        cache_key, extract_outline, is_target_language,
                        needs_translation, normalize_lang, page_formula_copies,
                        probe_llm, resolve_lang)

log = logging.getLogger("uvicorn.error")

# 未登录也能白助注册（默认开；`ALLOW_SIGNUP=0` 关闭）。本服务默认只绑 127.0.0.1，
# 若是要暴露到公网，请关掉它，改用“登录后在「账号」里新增”。
ALLOW_SIGNUP = (os.environ.get("ALLOW_SIGNUP", "1") or "").strip().lower() not in (
    "0", "false", "no", "off")
MAX_ACCOUNTS = int(os.environ.get("MAX_ACCOUNTS", "50") or 50)   # 防白助注册无限拉账号

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"
CONFIG_FILE = BASE_DIR / "config.json"

# 存储装配：MongoDB 优先（见 storage.py / APP_STORAGE）
store, auth, settings, STORAGE_BACKEND, storage_info = build_storage(DATA_DIR, CONFIG_FILE)
if STORAGE_BACKEND == "mongo":
    log.info("数据存储：MongoDB %s / 库 %s（配置来源 %s）",
             mongo.cfg["host"] + (":" + mongo.cfg["port"] if mongo.cfg.get("port") else ""),
             mongo.cfg["db"], mongo.cfg.get("source"))
else:
    log.warning("数据存储：本地 JSON（%s）", storage_info.get("warning") or "APP_STORAGE=json")


def _translator_for(username: str) -> Translator:
    """按「本账号设置 > 环境变量 > config.json > 默认」装配**这个账号**的翻译服务。

    LLM 配置（地址/Key/模型）与**译文目标语言**都跟账号走：A 存的 Key 只有 A 用得到，
    A 选的目标语言也只影响 A 的翻译（原文语言交给 LLM 识别）。
    """
    return Translator(config_file=settings.translator_config(username))


def _default_translator() -> Translator:
    """未登录场景（登录页提示）用的“安装级默认”翻译服务：只看环境变量/config.json。"""
    return Translator(config_file=settings.translator_config(""))


app = FastAPI(title="论文 PDF → HTML 阅读器", version="1.3")


def _storage_state() -> dict:
    """给前端看的存储状态（含实时探测的 Mongo 连通性）。"""
    info = dict(storage_info)
    info.pop("warning", None)
    info["mongo"] = mongo.status()
    info["backend"] = STORAGE_BACKEND
    try:
        info["papers"] = len(store.list_records())
        info["initialized_users"] = auth.count()
    except Exception as exc:                      # 数据库中途挂了也不该让接口 500
        info["error"] = str(exc)
    want = info.get("preferred", "auto")
    if STORAGE_BACKEND == "json" and want != "json":
        info["warning"] = storage_info.get("warning") or "未连接 MongoDB，正在使用本地 JSON 存储"
    return info


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _current_user(request: Request) -> dict | None:
    token = request.cookies.get(auth_mod.SESSION_COOKIE)
    return auth.user_for_token(token)


def _require_user(request: Request) -> dict:
    """接口层双保险：中间件已拦一层，这里再拦一次（防止@中间件被调整）。"""
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    return user


def _safe_next(value) -> str | None:
    """登录后的回跳地址：只允许站内相对路径，避免被拿去做开放重定向。"""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return None
    return value


def _set_session(resp: JSONResponse, token: str) -> JSONResponse:
    resp.set_cookie(auth_mod.SESSION_COOKIE, token, max_age=auth.cookie_max_age,
                    httponly=True, samesite="lax", path="/")
    return resp


def _login_user(request: Request, user: dict, payload: dict, *, need_llm: bool | None = None) -> JSONResponse:
    """签发会话并返回登录成功的响应（setup/register/login 共用）。

    `need_llm=None` 时按该账号自己的配置判断（没 Key 就引导去配）。
    """
    token = auth.new_session(user["username"], ip=_client_ip(request))
    if need_llm is None:
        need_llm = not bool(_translator_for(user["username"]).key)
    resp = JSONResponse({
        "ok": True, "username": user["username"],
        "need_llm": need_llm,
        "papers": len(store.list_by_owner(user["username"])),
        "next": _safe_next(payload.get("next")),
    })
    return _set_session(resp, token)


# ---------------- 登录门禁 ----------------
_PUBLIC_EXACT = {           # 无需登录即可访问
    "/login.html", "/favicon.ico",
    "/api/health",                       # 容器/负载均衡健康检查
    "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout",
    "/api/auth/register",      # 未登录白助注册（受 ALLOW_SIGNUP 控制）
}
_PUBLIC_PREFIX = ("/css/", "/js/", "/assets/", "/img/",
                  "/logo/",       # 品牌图标(登录页/顶栏都要在未登录状态取到)
                  "/vendor/")     # 登录页要用的静态资源；vendor 是内置的 KaTeX(js/css/字体)
                                  # 放行可避免「未带 Cookie 取字体被 302 掉」这类隐性故障


class AuthGate(BaseHTTPMiddleware):
    """未登录时：接口 → 401(前端跳登录页)；页面 → 302 跳登录页并带上回跳地址。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX):
            return await call_next(request)
        if _current_user(request):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
        nxt = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/login.html?next={quote(nxt, safe='/')}", status_code=302)


app.add_middleware(AuthGate)


@app.middleware("http")
async def _static_nocache(request: Request, call_next):
    """页面与前端资源一律 `no-cache`（允许缓存但强制每次条件请求）：
    改完 js/css 刷新即生效，不用再清浏览器缓存——否则很容易出现「代码改了却还是旧行为」
    的假故障。响应仍带 ETag/Last-Modified，文件没变时只是 304，开销可忽略。"""
    resp = await call_next(request)
    path = request.url.path
    if path.endswith(".html") or path.startswith(("/js/", "/css/", "/vendor/")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _require_doc(request: Request, doc_id: str) -> dict:
    """论文必须存在**且属于当前账号**，否则一律 404（不泄露“别人的论文存在”）。

    只用在“只有所有者能做”的事上（删除论文、管理共享名单）；
    看/改笔记这类要区分权限的走 `_doc_access()`。
    """
    user = _require_user(request)
    rec = store.get_record(doc_id)
    if not rec or records.owner_of(rec) != user["username"]:
        raise HTTPException(404, "论文不存在或已被删除")
    return rec


# 权限高低：只读 < 可写 < 所有者
PERM_RANK = {"read": 1, "write": 2, "owner": 3}


def _doc_access(request: Request, doc_id: str, need: str = "read") -> tuple[dict, str]:
    """论文必须存在、且**当前账号权限足够**，否则 404（不泄露别人的论文存在）。

    `need`：`read`（看）/ `write`（改笔记、高亮、整理、对话）/ `owner`（删论文、管共享）。
    返回 `(记录, 权限)`，权限是 `owner` / `write` / `read`。
    """
    user = _require_user(request)
    rec = store.get_record(doc_id)
    perm = records.perm_of(rec, user["username"])
    if not perm:
        raise HTTPException(404, "论文不存在或已被删除")
    if PERM_RANK[perm] < PERM_RANK[need]:
        raise HTTPException(
            403, f"这篇论文是「{'只读' if perm == 'read' else '共享'}」共享给你的，"
                 f"该操作需要更高权限")
    return rec, perm


def _share_state(rec: dict, me: str) -> dict:
    """给前端的共享状态：所有者是谁、我的权限、完整名单。"""
    return {
        "owner": records.owner_of(rec),
        "perm": records.perm_of(rec, me),
        "items": records.shared_entries(rec),
    }


def _doc_view(rec: dict, me: str) -> dict:
    """论文列表里的一行：附上**我的**权限、我的阅读进度与共享信息。"""
    row = dict(rec)
    row.pop("ai", None)                 # 列表不需要 AI 整理正文，减体积
    row.update({k: None for k in records.READING_KEYS})
    row.update(records.reading_of(rec, me))
    row["perm"] = records.perm_of(rec, me)
    row["owner"] = records.owner_of(rec)
    row["shared"] = records.shared_entries(rec)
    row["shared_count"] = len(row["shared"])
    row["mine"] = row["perm"] == "owner"
    return row



def _adopt_orphan_papers() -> int:
    """升级到“按账号隔离”后，把没有 owner 的历史论文归给最早创建的账号（幂等）。"""
    try:
        users = auth.list_users()
        if not users:
            return 0
        n = store.adopt_orphans(users[0]["username"])
        if n:
            log.info("已把 %d 篇历史论文归属给账号 %s（现在论文按账号隔离）", n, users[0]["username"])
        return n
    except Exception as exc:
        log.warning("历史论文归属迁移失败：%s", exc)
        return 0


def _adopt_legacy_llm() -> bool:
    """旧版 LLM 配置是全局一份，升级后归给最早创建的账号（幂等）。"""
    try:
        users = auth.list_users()
        if not users:
            return False
        if settings.adopt_legacy(users[0]["username"]):
            log.info("已把旧版全局 LLM 配置归给账号 %s（现在 LLM 配置按账号隔离）",
                     users[0]["username"])
            return True
    except Exception as exc:
        log.warning("LLM 配置迁移失败：%s", exc)
    return False


def _adopt_legacy_data() -> None:
    """启动/首次建号/换后端后跑一次：历史论文 + 旧版全局 LLM 配置都能“落地”。"""
    _adopt_orphan_papers()
    _adopt_legacy_llm()


_adopt_legacy_data()      # 启动时跑一次：老数据不至于因为加 owner/按账号隔离而“消失”


# ---------------- 健康检查（容器用；无需登录，不碰数据库） ----------------
@app.get("/api/health")
def api_health():
    return {"ok": True, "service": "paper-trans-to-html", "storage": STORAGE_BACKEND}


# ---------------- 身份认证 ----------------
@app.get("/api/auth/status")
def api_auth_status(request: Request):
    """前端进页面前先问这个：要不要先注册、要不要先配 LLM。

    未登录时这里只能报**安装级默认**的翻译服务（环境变量/config.json），
    每个账号自己的 LLM 配置在登录后由 /api/config、/api/settings/llm 给出。
    """
    user = _current_user(request)
    default_tr = _default_translator()
    return {
        "initialized": auth.initialized,
        "authenticated": bool(user),
        "username": user["username"] if user else None,
        "llm_configured": bool(default_tr.key),
        "llm_from_ui": settings.saved_by_ui(user["username"]) if user else False,
        "translator": default_tr.status_text,
        # 登录页需要知道：能不能白助注册、有几个账号
        "signup": ALLOW_SIGNUP,
        "accounts": auth.count(),
        # 登录页就能看到“有没有连上 MongoDB”，不用等登录后再发现
        "storage": STORAGE_BACKEND,
        "storage_warning": (storage_info.get("warning") if STORAGE_BACKEND == "json" else None),
    }


@app.post("/api/auth/setup")
def api_auth_setup(request: Request, payload: dict = Body(...)):
    """首次使用：创建第一个账号（管理员）并直接登录。已初始化则 409，防止被抢注。"""
    if auth.initialized:
        raise HTTPException(409, "已完成初始化，请直接登录（或用「注册新账号」）")
    try:
        user = auth.create_user(payload.get("username", ""), payload.get("password", ""))
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    _adopt_legacy_data()        # 第一个账号接手历史（无 owner）的论文与旧版全局 LLM 配置
    # 首次登录(即创建第一个账号)一律引导配置 LLM：会带上当前生效值，
    # 即使 config.json/环境变量里已经有 Key 也能在页面上改写或直接跳过
    return _login_user(request, user, payload, need_llm=True)


@app.post("/api/auth/register")
def api_auth_register(request: Request, payload: dict = Body(...)):
    """**未登录也能调**：自助注册一个账号，注册完直接登录。

    - 服务还没初始化时请走 /api/auth/setup（第一个账号），这里会 409；
    - `ALLOW_SIGNUP=0` 时关闭，返回 403；
    - 账号数超过 `MAX_ACCOUNTS`（默认 50）也不让再建，避免被刷爆。
    """
    if not auth.initialized:
        raise HTTPException(409, "服务还没初始化，请先创建管理员账号")
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "本服务已关闭自助注册，请让已有账号在「⚙ 设置 → 账号」里添加")
    if auth.count() >= MAX_ACCOUNTS:
        raise HTTPException(400, f"账号数已达上限（{MAX_ACCOUNTS}）")
    try:
        user = auth.create_user(payload.get("username", ""), payload.get("password", ""))
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _login_user(request, user, payload)      # need_llm 按该账号自己的配置判断


@app.post("/api/auth/login")
def api_auth_login(request: Request, payload: dict = Body(...)):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    ip = _client_ip(request)
    left = auth.locked_seconds(username, ip)
    if left:
        raise HTTPException(429, f"失败次数过多，请 {left} 秒后再试")
    user = auth.authenticate(username, password)
    if not user:
        auth.note_failure(username, ip)
        raise HTTPException(401, "用户名或密码不正确")
    auth.clear_failures(username, ip)
    return _login_user(request, user, payload)      # need_llm 按该账号自己的配置判断


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    auth.drop_session(request.cookies.get(auth_mod.SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth_mod.SESSION_COOKIE, path="/")
    return resp


@app.post("/api/auth/password")
def api_auth_password(request: Request, payload: dict = Body(...)):
    user = _require_user(request)
    try:
        auth.change_password(user["username"], payload.get("old_password") or "",
                             payload.get("new_password") or "",
                             keep_token=request.cookies.get(auth_mod.SESSION_COOKIE))
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


# ---------------- 账号偏好（随账号存储，如自定义高亮色） ----------------
PREFS_HL_MAX = 3          # 自定义高亮色槽位数（与服务端存的高亮色键 c1/c2/c3 对应）
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@app.get("/api/prefs")
def api_get_prefs(request: Request):
    """当前账号的偏好。注意：**随账号存**，不同账号互不影响。"""
    user = _require_user(request)
    prefs = auth.get_prefs(user["username"])
    return {"hl_colors": prefs.get("hl_colors") or [],
            "show_zh": prefs.get("show_zh"),
            "theme": prefs.get("theme")}


@app.post("/api/prefs")
def api_set_prefs(request: Request, payload: dict = Body(...)):
    """保存当前账号的偏好：3 个自定义高亮色 + “显示译文”开关 + 明/暗主题。"""
    user = _require_user(request)
    patch: dict = {}
    if "hl_colors" in payload:
        colors = payload.get("hl_colors")
        if not isinstance(colors, list) or not all(isinstance(c, str) for c in colors):
            raise HTTPException(400, "hl_colors 需为颜色数组")
        colors = colors[:PREFS_HL_MAX]
        for c in colors:
            if not _HEX_RE.match(c):
                raise HTTPException(400, f"颜色格式应为 #rrggbb：{c}")
        patch["hl_colors"] = colors
    if "show_zh" in payload:
        patch["show_zh"] = bool(payload.get("show_zh"))
    if "theme" in payload:
        theme = str(payload.get("theme") or "").strip().lower()
        if theme not in ("light", "dark"):
            raise HTTPException(400, "theme 只能是 light 或 dark")
        patch["theme"] = theme
    if not patch:
        raise HTTPException(400, "没有可保存的偏好字段")
    try:
        prefs = auth.set_prefs(user["username"], patch)
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "hl_colors": prefs.get("hl_colors") or [],
            "show_zh": prefs.get("show_zh"), "theme": prefs.get("theme")}


# ---------------- 注销账号（自助删除，数据一并清除） ----------------
@app.post("/api/auth/deactivate")
def api_auth_deactivate(request: Request, payload: dict = Body(...)):
    """注销当前登录的账号。

    需要：当前口令 + 重输用户名确认（服务端也校验）；
    效果：该账号名下**全部论文数据**（版式数据/标注/笔记/译文/图片）与账号、会话一并删除，
    不可恢复；注销后前端回登录页（若已无账号则回到“首次使用”初始化流程）。
    """
    user = _require_user(request)
    try:
        # 注意：必须传 confirm（缺省用空串，不能是 None，否则会被当成“跳过确认”）
        closed = auth.close_account(user["username"], payload.get("password") or "",
                                   payload.get("confirm") or "")
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    removed = store.purge_owner(closed["username"])
    log.warning("账号 %s 已注销：同时清除 %d 篇论文及其数据", closed["username"], removed)
    resp = JSONResponse({
        "ok": True, "username": closed["username"], "removed_papers": removed,
        "initialized": auth.initialized,
    })
    resp.delete_cookie(auth_mod.SESSION_COOKIE, path="/")
    return resp


# ---------------- 账号管理（多账号 / 切换账号） ----------------
@app.get("/api/auth/users")
def api_list_users(request: Request):
    user = _require_user(request)
    users = auth.list_users()
    for u in users:                     # 带上各自论文数，删除前心里有数
        u["papers"] = len(store.list_by_owner(u["username"]))
    return {"users": users, "current": user["username"],
            "password_min": auth_mod.MIN_PASSWORD}


@app.post("/api/auth/users")
def api_add_user(request: Request, payload: dict = Body(...)):
    """新增一个账号（需已登录）。用于“切换账号”前先建好另一个账号。"""
    user = _require_user(request)
    try:
        auth.create_user(payload.get("username", ""), payload.get("password", ""))
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "users": auth.list_users(), "current": user["username"]}


@app.delete("/api/auth/users/{username}")
def api_del_user(request: Request, username: str, purge: bool = True):
    """删除账号。`purge=true`（默认）连它名下的论文数据一起删。"""
    user = _require_user(request)
    target = auth.public_user(username)      # 取规范大小写的用户名
    try:
        auth.delete_user(username, by=user["username"])
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    removed = store.purge_owner(target["username"]) if (purge and target) else 0
    if removed:
        log.info("删除账号 %s 时一并删除 %d 篇论文", username, removed)
    return {"ok": True, "removed_papers": removed, "users": auth.list_users(),
            "current": user["username"]}


# ---------------- 设置：LLM 服务（按账号） ----------------
@app.get("/api/settings/llm")
def api_get_llm(request: Request):
    """返回**当前账号**的 LLM 配置；API Key 只回打码串，明文不出服务端。"""
    user = _require_user(request)
    tr = _translator_for(user["username"])
    return settings.describe(user["username"],
                             active_key=tr.key, status_text=tr.status_text)


@app.post("/api/settings/llm")
def api_save_llm(request: Request, payload: dict = Body(...)):
    """保存**当前账号**的一套 LLM 配置（最多 3 套，按 `profile_id` 定位）。

    - `api_key` 不传/传 null = 沿用该套已保存的 Key，传空串 = 清空；
    - `profile_id` 不传/传 `"new"` = 新建一套（超过上限报 400），传已存在的 id = 改写那一套。
    保存的这套会**立即成为当前生效配置**（相当于保存即切换）。

    另外可选带 `target_lang`（译文目标语言，如 `zh-CN` / `en` / `ja`）：它存在
    `users.prefs.target_lang`，与上面几套配置槽无关，改语言不会动 Key/地址/模型。
    """
    user = _require_user(request)
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
            have = api_key or settings.profile_key(name, profile_id)
            if have is None:
                have = settings.merged(name)["api_key"]
            if not have:
                raise HTTPException(400, "请填写 API Key")

    try:
        settings.save_llm(name, provider=provider, base_url=base_url, model=model,
                          api_key=api_key, user=name,
                          profile_id=profile_id, profile_name=profile_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if target_lang:
        settings.set_target_lang(name, target_lang)
    tr = _translator_for(name)          # 无需重启：配置存在账号里，每次用都重新装配
    out = settings.describe(name, active_key=tr.key, status_text=tr.status_text)
    out.update({"ok": True, "ready": tr.ready})
    return out


@app.post("/api/settings/llm/activate")
def api_activate_llm(request: Request, payload: dict = Body(...)):
    """切换到本账号已保存的另一套 LLM 配置（`{"profile_id": "..."}`）。"""
    user = _require_user(request)
    name = user["username"]
    try:
        settings.activate_profile(name, (payload.get("profile_id") or "").strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    tr = _translator_for(name)          # 切换后立刻按新配置装配
    out = settings.describe(name, active_key=tr.key, status_text=tr.status_text)
    out.update({"ok": True, "ready": tr.ready})
    return out


@app.post("/api/settings/llm/delete")
def api_delete_llm(request: Request, payload: dict = Body(...)):
    """删除本账号已保存的一套 LLM 配置（删的若是当前生效那套，会自动切到剩下的第一套）。"""
    user = _require_user(request)
    name = user["username"]
    try:
        settings.delete_profile(name, (payload.get("profile_id") or "").strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    tr = _translator_for(name)
    out = settings.describe(name, active_key=tr.key, status_text=tr.status_text)
    out.update({"ok": True, "ready": tr.ready})
    return out


@app.post("/api/settings/llm/test")
async def api_test_llm(request: Request, payload: dict = Body(...)):
    """用一次极小的请求验证 地址/Key/模型 三件套，失败给出可读原因。

    表单里现填的值优先；`api_key` 没填（页面只显示打码串）时，用**所选配置槽已保存的 Key**，
    这样“切换过去但不改 Key”也能直接测。
    """
    user = _require_user(request)
    saved = settings.merged(user["username"])
    pid = (payload.get("profile_id") or "").strip()
    prof = next((p for p in settings.profile_list(user["username"]) if p["id"] == pid), None)
    raw_key = payload.get("api_key")
    key = raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else None
    if key is None:
        key = settings.profile_key(user["username"], pid)
    if not key:
        key = saved["api_key"]
    base = (payload.get("base_url") or (prof or {}).get("base_url") or saved["base_url"]).strip()
    model = (payload.get("model") or (prof or {}).get("model") or saved["model"]).strip()
    try:
        msg = await run_in_threadpool(probe_llm, base, key, model)
    except TranslationUnavailable as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "message": msg}


# ---------------- 转换 ----------------
@app.post("/api/convert")
async def api_convert(request: Request, file: UploadFile = File(...)):
    user = _require_user(request)
    name = file.filename or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "请上传 PDF 文件")
    doc_id = uuid.uuid4().hex
    tmp_dir = DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{doc_id}.pdf"
    tmp.write_bytes(await file.read())
    try:
        meta = await run_in_threadpool(converter.convert, store, doc_id, tmp, name,
                                       user["username"],
                                       _parser_options_for(user["username"]))
    except Exception as exc:
        store.delete(doc_id)
        raise HTTPException(400, f"转换失败：{exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return meta


# ---------------- 原地重解析版式（解析器升级后用） ----------------
@app.post("/api/doc/{doc_id}/reparse")
async def api_doc_reparse(request: Request, doc_id: str,
                          file: UploadFile = File(...)):
    """用**这一篇的原始 PDF** 原地重解析版式数据。

    解析器升级（换后端 / 补公式 / 调 DPI）后，库里已存的论文不会自动跟着变；
    重新上传会生成新论文、笔记译文全丢，所以给一个原地重解析：只换
    `pages`/`parser`/`parser_info`/插图，笔记、高亮、译文、进度一个不动。
    解析失败不改动已有数据。
    """
    user = _require_user(request)
    _doc_access(request, doc_id, "write")
    name = file.filename or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "请上传这一篇的原始 PDF 文件")
    tmp_dir = DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"reparse-{doc_id}.pdf"
    tmp.write_bytes(await file.read())
    try:
        summary = await run_in_threadpool(
            converter.reparse, store, doc_id, tmp,
            parser_options=_parser_options_for(user["username"]))
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc
    except Exception as exc:
        raise HTTPException(400, f"重新解析失败：{exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    # 版式已重写：清掉本进程的缓存条目兜底（其它进程/实例由 read_doc 的
    # rev 校验自动失效），下个请求从库重读，刷新页面即可见新版式。
    store.clear_cache(doc_id)
    log.info("版式已重新解析：%s → %s（带 latex 的块 %s）", doc_id,
             summary.get("parser"), summary.get("latex_blocks"))
    return {"ok": True, **summary}


# ---------------- 阅读记录 ----------------
@app.get("/api/docs")
def api_docs(request: Request):
    """当前账号能看到的论文：**自己的 + 别人共享给我的**。

    每行都带上 `perm`（owner/write/read）、`owner`、`shared`，以及**我自己的**阅读进度
    （共享论文的进度跟人走，不会覆盖所有者的）。
    """
    user = _require_user(request)
    me = user["username"]
    return [_doc_view(rec, me) for rec in store.list_for_user(me)]


@app.get("/api/doc/{doc_id}")
def api_doc(request: Request, doc_id: str):
    _doc_access(request, doc_id)
    try:
        return store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc


@app.post("/api/doc/{doc_id}/title")
def api_doc_title(request: Request, doc_id: str, payload: dict = Body(...)):
    """重命名论文（阅读页顶栏的标题可以直接改）。

    标题在库里存了**两份**，必须一起改，否则首页与阅读页会各显示一个名字：
      · 记录（records）的 `title`：首页列表、共享信息、AI 整理的上下文都用它；
      · 版式文档（doc.json）的 `title`：阅读页顶栏、分享弹窗的文案用它。
    只动名字、不碰解析数据，所以不用走重解析流程；笔记/译文/进度都不受影响。
    有「可写」权限就能改名（只读者不行），与原文件名的 `source` 字段无关。
    """
    _doc_access(request, doc_id, need="write")
    title = " ".join(str(payload.get("title") or "").split())[:200].strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    store.update_record(doc_id, title=title)
    try:
        doc = store.read_doc(doc_id)
    except FileNotFoundError:
        doc = None
    if isinstance(doc, dict):
        doc["title"] = title
        store.save_doc(doc_id, doc)
    log.info("论文改名：%s → %s", doc_id, title)
    return {"ok": True, "title": title}


@app.get("/api/doc/{doc_id}/refs")
def api_doc_refs(request: Request, doc_id: str):
    """引用跳转：PDF 内部链接（正文引用标记 → 参考文献条目）算好的映射。

    纯本地、零 LLM 成本（毫秒级），所以**不缓存、每次现算** —— 重解析后自然就是
    新数据；PDF 本身没有内部链接（绝大多数扫描件）时返回空映射，前端静默。
    数据结构见 `backend/pdf_refs/refs.py`。
    """
    _doc_access(request, doc_id)
    try:
        doc = store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc
    return refs_mod.build_refs(doc)


_IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".gif": "image/gif"}


@app.get("/api/doc/{doc_id}/img/{fname}")
def api_img(request: Request, doc_id: str, fname: str):
    """论文插图。本体存在存储后端里（MongoDB 走 GridFS，回退模式读磁盘），
    所以服务重启/重建容器都不会再出现“图打不开”的情况。"""
    if not re.fullmatch(r"p\d+_i\d+\.(png|jpe?g|webp|gif)", fname, re.I):
        raise HTTPException(400, "非法的图片名")
    _doc_access(request, doc_id)
    data = store.get_image(doc_id, fname)
    if not data:
        raise HTTPException(404, "图片不存在")
    # 图片跟 doc_id 绑定、内容不会变，交给浏览器缓存，翻页/来回滚动不重复下载
    mime = _IMG_MIME.get(Path(fname).suffix.lower(), "application/octet-stream")
    return Response(data, media_type=mime,
                    headers={"Cache-Control": "private, max-age=604800"})


@app.delete("/api/doc/{doc_id}")
def api_delete_doc(request: Request, doc_id: str):
    _require_doc(request, doc_id)          # 只能删自己的论文（共享者不能删）
    store.delete(doc_id)
    return {"ok": True}


# ---------------- 共享（用户 A 把论文 + 笔记共享给 B，并决定读写权限） ----------------
def _account_names() -> set[str]:
    return {str(u.get("username") or "").strip() for u in auth.list_users()}


@app.get("/api/doc/{doc_id}/share")
def api_get_share(request: Request, doc_id: str):
    """这篇论文的共享名单（**任何有权限的人都能看**，但只有所有者能改）。"""
    rec, _perm = _doc_access(request, doc_id)
    me = _require_user(request)["username"]
    return _share_state(rec, me)


@app.post("/api/doc/{doc_id}/share")
def api_set_share(request: Request, doc_id: str, payload: dict = Body(...)):
    """新增或修改一条共享：`{user, perm}`，`perm` 是 `read`（只读）/ `write`（可写）。

    只能由论文所有者操作（不能共享给自己，目标账号必须存在）。
    """
    rec = _require_doc(request, doc_id)
    me = _require_user(request)["username"]
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
    store.update_record(doc_id, shared=items)
    log.info("%s 把论文 %s 共享给 %s（%s）", me, doc_id, target, perm)
    return _share_state(store.get_record(doc_id) or rec, me)


@app.delete("/api/doc/{doc_id}/share/{username}")
def api_del_share(request: Request, doc_id: str, username: str):
    """取消某条共享（只有所有者能操作）。顺手清掉那个人的阅读状态。"""
    rec = _require_doc(request, doc_id)
    me = _require_user(request)["username"]
    target = username.strip()
    items = [s for s in records.shared_entries(rec) if s["user"] != target]
    if len(items) == len(records.shared_entries(rec)):
        raise HTTPException(404, "这条共享不存在")
    store.update_record(doc_id, shared=items,
                        **records.drop_reading(rec, target))
    log.info("%s 取消了 %s 对论文 %s 的共享", me, target, doc_id)
    return _share_state(store.get_record(doc_id) or rec, me)


@app.post("/api/doc/{doc_id}/share/leave")
def api_leave_share(request: Request, doc_id: str):
    """被共享的人自己退出（不需要所有者权限；所有者不能“退出”自己的论文）。"""
    rec, perm = _doc_access(request, doc_id)
    me = _require_user(request)["username"]
    if perm == "owner":
        raise HTTPException(400, "这是你自己的论文，只能删除或取消别人的共享")
    items = [s for s in records.shared_entries(rec) if s["user"] != me]
    store.update_record(doc_id, shared=items, **records.drop_reading(rec, me))
    log.info("%s 退出了论文 %s 的共享", me, doc_id)
    return {"ok": True}


# ---------------- 进度（**跟人走**：共享论文里每个人记自己的） ----------------
@app.get("/api/doc/{doc_id}/progress")
def api_get_progress(request: Request, doc_id: str):
    rec, perm = _doc_access(request, doc_id)
    user = _require_user(request)
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


@app.post("/api/doc/{doc_id}/progress")
def api_set_progress(request: Request, doc_id: str, payload: dict = Body(...)):
    """记阅读进度。只读共享也能调 —— 记的是**自己**的进度。"""
    rec, _perm = _doc_access(request, doc_id)
    user = _require_user(request)
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
    updated = store.update_record(doc_id, **records.reading_patch(rec, user["username"], fields))
    return records.reading_of(updated or rec, user["username"])


# ---------------- 标注 / 笔记 ----------------
@app.get("/api/doc/{doc_id}/anno")
def api_get_anno(request: Request, doc_id: str):
    """标注 + 笔记。

    论文**共享给别人**时，每条笔记会多带一个 `by`（谁写的），前端据此在卡片上标作者；
    没共享时不下发 `by` —— 那种情况下只有自己能看，标了没意义。
    """
    rec, _perm = _doc_access(request, doc_id)
    return records.anno_with_authors(store.get_annotations(doc_id), rec)


@app.post("/api/doc/{doc_id}/anno")
def api_apply_anno(request: Request, doc_id: str, payload: dict = Body(...)):
    rec, _perm = _doc_access(request, doc_id, "write")      # 只读共享不能改笔记/高亮
    user = _require_user(request)
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
                for n in (store.get_annotations(doc_id).get("free_notes") or [])
                if isinstance(n, dict)):
            raise HTTPException(404, "独立笔记不存在")
    else:
        note_id = payload.get("note_id")
    anno = store.apply_anno(
        doc_id, action,
        block_id=payload.get("block_id"),
        color=payload.get("color"),
        text=text,
        note_id=note_id,
        title=title,
        by=user["username"],        # 笔记要记下是谁写的（共享论文里会显示）
    )
    return records.anno_with_authors(anno, rec)


# ---------------- “所选句译文”卡片（跨刷新/重新登录保留） ----------------
# 它们属于**阅读辅助**而不是共享内容：只读共享也能加/删自己的卡片。
# 卡片上记着它是哪个目标语言的译文：换语言后旧语言的卡不再返回（切回原语言还在）。
@app.get("/api/doc/{doc_id}/seltrans")
def api_get_seltrans(request: Request, doc_id: str):
    user = _require_user(request)
    _doc_access(request, doc_id)
    lang = settings.target_lang(user["username"])
    # 老卡片没有 lang 字段，按默认语言（简体中文）算，升级后不至于“卡片全没了”
    return [c for c in store.get_seltrans(doc_id)
            if normalize_lang(c.get("lang")) == lang]


@app.post("/api/doc/{doc_id}/seltrans")
def api_add_seltrans(request: Request, doc_id: str, payload: dict = Body(...)):
    """保存一张所选句译文卡（同 key 覆盖），并记下它的目标语言。"""
    user = _require_user(request)
    _doc_access(request, doc_id)
    key = (payload.get("key") or "").strip()
    text = payload.get("text") or ""
    if not key or not text.strip():
        raise HTTPException(400, "缺少 key 或 text")
    return store.add_seltrans(doc_id, {
        "key": key,
        "item_id": payload.get("item_id") or key.split("#", 1)[0],
        "si_from": int(payload.get("si_from") or 0),
        "text": text,
        "lang": settings.target_lang(user["username"]),
    })


@app.delete("/api/doc/{doc_id}/seltrans")
def api_del_seltrans(request: Request, doc_id: str, key: str | None = None):
    """传 key 只删一张；不传 key 清空该论文的全部所选句译文卡（需要写权限）。"""
    _doc_access(request, doc_id, "read" if key else "write")
    return store.del_seltrans(doc_id, key) if key else store.clear_seltrans(doc_id)


# ---------------- 侧栏目录 ----------------
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


@app.get("/api/doc/{doc_id}/toc")
def api_get_toc(request: Request, doc_id: str):
    """论文目录。PDF 自带书签在建论文时就存下了；没有书签则返回空，
    由前端按需调 POST 让 LLM 从全文提取（提取结果也会存起来，不再重复花 token）。"""
    rec, _perm = _doc_access(request, doc_id)
    return {"items": rec.get("toc") or [], "source": rec.get("toc_source") or ""}


@app.post("/api/doc/{doc_id}/toc")
async def api_build_toc(request: Request, doc_id: str, payload: dict = Body(default=None)):
    """用 LLM 从全文提取目录。`{"force": true}` = 忽略已存的、重新提取。

    目录存在记录里、大家共用，所以需要写权限。
    """
    rec, _perm = _doc_access(request, doc_id, "write")
    user = _require_user(request)
    if rec.get("toc") and not (payload or {}).get("force"):
        return {"items": rec["toc"], "source": rec.get("toc_source") or ""}
    try:
        doc = store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc

    context = _outline_context(doc)
    if not context.strip():
        raise HTTPException(400, "这篇论文没有可用来提取目录的文本")
    try:
        items = await run_in_threadpool(
            extract_outline, _translator_for(user["username"]),
            doc.get("title") or "", context)
    except TranslationUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not items:
        raise HTTPException(400, "没能从全文里识别出章节标题")

    total = max(1, int(doc.get("num_pages") or 0))
    for it in items:                      # LLM 偶尔给越界页码，夹一下
        it["page"] = min(max(1, it.get("page") or 1), total)
    store.update_record(doc_id, toc=items, toc_source="llm",
                        toc_at=datetime.datetime.now().isoformat(timespec="seconds"))
    log.info("已用 LLM 为 %s 提取目录：%d 条", doc_id, len(items))
    return {"items": items, "source": "llm"}


# ---------------- AI 辅助阅读（文末：笔记整理 + 对话） ----------------
AI_KEEP_TURNS = 40          # 每篇保留的对话条数（user+assistant 合计）
AI_MAX_MESSAGE = 4000       # 单条提问长度上限
AI_TRIGGER_PROGRESS = 0.95  # 读到这个进度就自动整理笔记（前端用它决定何时触发）


def _sse(obj: dict) -> str:
    """SSE 帧：一行 `data: {json}` + 空行分隔（JSON 里不会出现裸换行）。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _ai_doc_notes(doc_id: str) -> list[dict]:
    """该论文当前的笔记（存储层口径，带 block_id）：用来算指纹。"""
    try:
        anno = store.get_annotations(doc_id)
        return (anno.get("notes") or []) + (anno.get("free_notes") or [])
    except Exception:
        return []


def _ai_doc_data(doc_id: str) -> dict:
    try:
        return store.read_doc(doc_id)
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
        "trigger_progress": AI_TRIGGER_PROGRESS,
    }


@app.get("/api/doc/{doc_id}/ai")
def api_get_ai(request: Request, doc_id: str):
    """文末 AI 面板的状态（摘要与对话都存在记录里，刷新/重新登录不会丢）。"""
    rec, _perm = _doc_access(request, doc_id)
    user = _require_user(request)
    return _ai_state(rec, _translator_for(user["username"]))


@app.post("/api/doc/{doc_id}/ai/summary")
async def api_ai_summary(request: Request, doc_id: str, payload: dict = Body(default=None)):
    """把这篇论文的笔记整理成结构化摘要并保存。

    `notes` 由前端给出（`[{sent, note, page}]`——只有前端知道笔记挂在哪句话上）。
    已经整理过且笔记没再改时直接返回缓存；`{"force": true}` 则重新整理。
    重新整理时会带上**上一版整理（可能被用户手改过）+ 已发生的对话**当素材，
    所以是把讨论成果并进来，而不是推倒重来。
    """
    rec, _perm = _doc_access(request, doc_id, "write")
    user = _require_user(request)
    payload = payload or {}
    tr = _translator_for(user["username"])
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
    except TranslationUnavailable as exc:
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
    store.update_record(doc_id, ai=ai)
    log.info("已为 %s 整理 %d 条笔记，带 %d 条对话素材",
             doc_id, len(notes), len(ai.get("chat") or []))
    return _ai_state(store.get_record(doc_id) or rec, tr)


@app.post("/api/doc/{doc_id}/ai/summary/save")
def api_ai_summary_save(request: Request, doc_id: str, payload: dict = Body(...)):
    """保存**手工编辑**后的笔记整理（整份覆盖）。

    前端把改完的完整结构传回来（`{"summary": {...}}`）；只在已经有整理结果时可用。
    清洗用宽松上限，不会把用户写的长句子默默截掉。
    """
    rec, _perm = _doc_access(request, doc_id, "write")
    user = _require_user(request)
    ai = dict(rec.get("ai") or {})
    if not ai.get("summary"):
        raise HTTPException(400, "还没有整理结果可以编辑，先生成一份")
    summary = clean_summary(payload.get("summary"), loose=True)
    if summary_empty(summary):
        raise HTTPException(400, "整理内容不能全为空")
    ai["summary"] = summary
    ai["summary_edited_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    store.update_record(doc_id, ai=ai)
    return _ai_state(store.get_record(doc_id) or rec, _translator_for(user["username"]))


@app.post("/api/doc/{doc_id}/ai/chat")
async def api_ai_chat(request: Request, doc_id: str, payload: dict = Body(...)):
    """论文阅读 Agent：带着整篇正文、公式、笔记与整理回答，可把内容写进右侧整理卡片并定位内容。

    正文由服务端从文档数据里现拼；公式位置在正文里是空格，对应的 LaTeX 附在每页末尾。
    Agent 写内容时走 `add_note_to_summary`：按分区追加到**右侧「笔记整理」卡片**，
    不新建笔记。

    SSE 同时传输回复增量、工具状态与页面动作，结束时给出落库后的完整对话。
    """
    rec, _perm = _doc_access(request, doc_id, "write")
    user = _require_user(request)
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "请输入内容")
    if len(message) > AI_MAX_MESSAGE:
        raise HTTPException(400, f"消息太长了（上限 {AI_MAX_MESSAGE} 字）")
    tr = _translator_for(user["username"])
    try:
        tr.validate_chat([{"role": "user", "content": message}])
    except TranslationUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    ai = dict(rec.get("ai") or {})
    notes = clean_notes(payload.get("notes")) or (ai.get("notes") or [])
    history = ai.get("chat") or []
    doc = await run_in_threadpool(_ai_doc_data, doc_id)
    article = article_text(doc)
    stored_anno = await run_in_threadpool(store.get_annotations, doc_id)
    pending_navigation = payload.get("pending_navigation")
    navigation_answer = payload.get("navigation_answer")
    if navigation_answer not in (None, "yes", "no"):
        raise HTTPException(400, "导航确认选项无效")
    if pending_navigation is not None:
        if not isinstance(pending_navigation, dict):
            raise HTTPException(400, "待确认的导航目标无效")
        target = pending_navigation.get("target")
        if target == "paper":
            page = pending_navigation.get("page")
            if (isinstance(page, bool) or not isinstance(page, int)
                    or page < 1 or page > len(doc.get("pages") or [])):
                raise HTTPException(400, "待确认的论文页码无效")
            block_id = pending_navigation.get("block_id")
            page_texts = (doc.get("pages") or [])[page - 1].get("texts") or []
            if block_id is not None and (
                    not isinstance(block_id, str)
                    or not any(isinstance(block, dict) and block.get("id") == block_id
                               for block in page_texts)):
                raise HTTPException(400, "待确认的相关文本块无效")
            pending_navigation = {"target": "paper", "page": page}
            if block_id:
                pending_navigation["block_id"] = block_id
        elif target == "note":
            note_id = pending_navigation.get("note_id")
            all_notes = (stored_anno.get("notes") or []) + (stored_anno.get("free_notes") or [])
            if (not isinstance(note_id, str)
                    or not any(n.get("id") == note_id for n in all_notes if isinstance(n, dict))):
                raise HTTPException(400, "待确认的笔记不存在")
            pending_navigation = {"target": "note", "note_id": note_id}
        else:
            raise HTTPException(400, "待确认的导航类型无效")
    elif navigation_answer is not None:
        raise HTTPException(400, "缺少待确认的导航目标")

    free_note_rows = []
    free_note_ids = set()
    for note in stored_anno.get("free_notes") or []:
        if isinstance(note, dict) and note.get("id") and note.get("text"):
            free_note_ids.add(note["id"])
            free_note_rows.append({
                "id": note["id"], "title": note.get("title") or "",
                "note": note.get("text") or "", "page": 0,
            })
    notes = clean_notes(free_note_rows + [
        note for note in notes if note.get("id") not in free_note_ids
    ])

    def execute_tool(name: str, args: dict) -> dict:
        nonlocal stored_anno
        if name == "add_note_to_summary":
            # 写进右侧整理卡片（不新建笔记）：卡片为空时按分区先建起来
            result, summary = add_summary_item(ai.get("summary"), args)
            if result.get("ok"):
                ai["summary"] = summary
                ai["summary_edited_at"] = datetime.datetime.now().isoformat(
                    timespec="seconds")
                store.update_record(doc_id, ai=ai)
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
            all_notes = (stored_anno.get("notes") or []) + (stored_anno.get("free_notes") or [])
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

    try:
        # 这一步就把能提前发现的错误（没配 LLM / 没内容 / 参数不对）抛出来，
        # 而不是等 SSE 头已经发出去了才报错
        chunks = reply_agent_stream(tr, title=rec.get("title") or "", notes=notes,
                                    summary=ai.get("summary"), history=history,
                                    message=message, article=article,
                                    execute_tool=execute_tool,
                                    pending_navigation=pending_navigation,
                                    navigation_answer=navigation_answer)
    except TranslationUnavailable as exc:
        # 只给原因：前端会自己加上「对话失败：」前缀（不然会重复）
        raise HTTPException(503, str(exc)) from exc

    def gen():
        parts: list[str] = []
        try:
            for event in chunks:
                if event.get("type") == "delta":
                    parts.append(event.get("text") or "")
                yield _sse(event)
        except TranslationUnavailable as exc:
            yield _sse({"type": "error", "message": str(exc)})
            return
        except Exception as exc:                 # 客户端断开 / 上游抽风：别把连接吊死
            log.warning("AI 对话中断：%s", exc)
            yield _sse({"type": "error", "message": f"对话中断：{exc}"})
            return
        reply = "".join(parts).strip()
        if not reply:
            yield _sse({"type": "error", "message": "模型没有返回内容，请重试"})
            return
        now = datetime.datetime.now().isoformat(timespec="seconds")
        latest = dict(ai)
        latest["chat"] = (history + [
            {"role": "user", "content": message, "at": now},
            {"role": "assistant", "content": reply, "at": now},
        ])[-AI_KEEP_TURNS:]
        store.update_record(doc_id, ai=latest)
        yield _sse({"type": "done", "chat": latest["chat"],
                    "ready": tr.ready, "status_text": tr.status_text})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",           # 别让中间层缓存/攒够再发
        "X-Accel-Buffering": "no",            # nginx 后面也一样边收边发
    })


@app.post("/api/doc/{doc_id}/ai/chat/reset")
def api_ai_chat_reset(request: Request, doc_id: str):
    """清空这篇的对话记录（已生成的笔记整理保留）。对话是共享内容，所以需要写权限。"""
    rec, _perm = _doc_access(request, doc_id, "write")
    ai = dict(rec.get("ai") or {})
    ai["chat"] = []
    store.update_record(doc_id, ai=ai)
    return {"ok": True, "chat": []}


# ---------------- 翻译 ----------------
@app.get("/api/config")
def api_config(request: Request):
    """当前账号的翻译/解析/存储状态（翻译服务按账号隔离，所以必须登录后看）。"""
    user = _require_user(request)
    tr = _translator_for(user["username"])
    return {
        "translator": tr.status_text,
        "ready": tr.ready,
        "backend": tr.backend,
        "llm_owner": user["username"],
        # PDF 解析后端：
        #   auto / pymupdf → 本地解析方案
        #     （PyMuPDF 版式 + texify 公式 + OXR 版面 / SLANet 表格，见 backend_pymupdf.py）
        #   surya → 整页 OCR：文字/公式/插图全用 Surya 数据
        #   unlimited_ocr → 整页 OCR：文字/公式/表格/插图全用 Unlimited-OCR 数据
        "parser": pdf_parser.status(
            CONFIG_FILE, account_options=_parser_options_for(user["username"])),
        # 数据存储后端：mongo(MongoDB) / json(本地文件回退)
        "storage": _storage_state(),
    }


# ---------------- PDF 解析后端切换 ----------------
PARSER_BACKENDS = ("auto", "surya", "unlimited_ocr", "pymupdf")


def _parser_options_for(username: str) -> dict:
    """返回账号专属的解析选项；未设置时沿用 config.json 中的安装级默认值。"""
    parser = auth.get_prefs(username).get("parser")
    if not isinstance(parser, dict):
        return {}
    return {key: value for key, value in parser.items()
            if key == "backend" and value in PARSER_BACKENDS}


@app.get("/api/settings/parser")
def api_settings_parser_get(request: Request):
    """当前 PDF 解析(OCR)后端状态。"""
    user = _require_user(request)
    return pdf_parser.status(
        CONFIG_FILE, account_options=_parser_options_for(user["username"]))


@app.post("/api/settings/parser")
def api_settings_parser_set(request: Request, payload: dict = Body(...)):
    """切换 PDF 解析后端：auto / surya / unlimited_ocr / pymupdf。

    保存到当前账号的 users.prefs.parser.backend；解析服务参数仍来自安装级配置。
    若环境变量 PDF_PARSER_BACKEND 已设置，它会覆盖账号选择
    (status 里的 env_override 会告诉前端)。
    """
    user = _require_user(request)
    backend = str((payload or {}).get("backend") or "").strip().lower()
    if backend not in PARSER_BACKENDS:
        raise HTTPException(400, f"未知的解析后端：{backend or '(空)'}；"
                                 f"可选 {'/'.join(PARSER_BACKENDS)}")
    parser_prefs = auth.get_prefs(user["username"]).get("parser")
    parser_prefs = dict(parser_prefs) if isinstance(parser_prefs, dict) else {}
    parser_prefs["backend"] = backend
    try:
        auth.set_prefs(user["username"], {"parser": parser_prefs})
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("账号 %s 的 PDF 解析(OCR)后端已切换为 %s",
             user["username"], backend)
    parser_status = pdf_parser.status(
        CONFIG_FILE, account_options=_parser_options_for(user["username"]))
    return {"ok": True, "backend": backend, "parser": parser_status}


# ---------------- 存储状态 / 热切换 ----------------
@app.get("/api/storage/status")
def api_storage_status(request: Request):
    _require_user(request)
    return _storage_state()


@app.post("/api/storage/reconnect")
def api_storage_reconnect(request: Request):
    """重新探测 MongoDB 并热切换存储后端（不用重启服务）。

    切到 MongoDB 后，当前会话（存在旧后端里）可能不再有效，
    响应里的 `relogin` 会告诉前端去重新登录。
    """
    global store, auth, settings, STORAGE_BACKEND, storage_info
    _require_user(request)
    before = STORAGE_BACKEND
    try:
        store, auth, settings, STORAGE_BACKEND, storage_info = rebuild_storage(DATA_DIR, CONFIG_FILE)
    except StorageUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    switched = before != STORAGE_BACKEND
    _adopt_legacy_data()            # 换后端后也把无归属的历史论文/旧版 LLM 配置接过来
    out = _storage_state()
    # 换后端后旧会话未必存在（比如旧的是 json 会话、新库里没这个 token）
    out["switched"] = switched
    out["relogin"] = switched and not _current_user(request)
    log.info("数据存储已切换：%s → %s", before, STORAGE_BACKEND)
    return out


@app.get("/api/doc/{doc_id}/zh_all")
def api_zh_all(request: Request, doc_id: str):
    """返回本论文所有已缓存译文的块 id -> 译文（按**当前账号的目标语言**过滤）。

    译文缓存是按「目标语言 + 块文本」存键的，换语言后这里自然只回新语言的译文。
    """
    user = _require_user(request)
    _doc_access(request, doc_id)
    try:
        doc = store.read_doc(doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "文档数据缺失") from exc
    lang = settings.target_lang(user["username"])
    cache = store.get_translations(doc_id)
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


@app.post("/api/translate")
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
    _doc_access(request, doc_id)                # 顺带完成归属校验
    user = _require_user(request)               # LLM 配置与目标语言都按账号取
    tr = _translator_for(user["username"])      # 用当前账号自己的配置 + 目标语言
    lang = tr.target_lang

    cache = store.get_translations(doc_id)
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
        except TranslationUnavailable as exc:
            raise HTTPException(503, str(exc))
        updates = {}
        for t, zh in zip(todo, translated):
            if zh:
                updates[t["key"]] = zh
                results[t["id"]] = zh
            else:
                failed.append(t["id"])          # 服务没给结果 → 可重试，别标记 noZh
        if updates:
            store.set_translations(doc_id, updates)
    return {"results": results, "skipped": skipped, "failed": failed}


# ---------------- 前端静态资源 ----------------
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
