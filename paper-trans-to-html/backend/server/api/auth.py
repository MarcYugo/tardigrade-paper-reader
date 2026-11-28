"""身份认证、账号偏好、多账号管理与注销。"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from ... import auth as auth_mod
from .. import migrate, runtime
from ..config import ALLOW_SIGNUP, MAX_ACCOUNTS
from ..deps import client_ip, current_user, login_user, require_user
from ..factories import default_translator


PREFS_HL_MAX = 3          # 自定义高亮色槽位数（与服务端存的高亮色键 c1/c2/c3 对应）
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

router = APIRouter()
log = logging.getLogger("uvicorn.error")


@router.get("/api/auth/status")
def api_auth_status(request: Request):
    """前端进页面前先问这个：要不要先注册、要不要先配 LLM。

    未登录时这里只能报**安装级默认**的翻译服务（环境变量/config.json），
    每个账号自己的 LLM 配置在登录后由 /api/config、/api/settings/llm 给出。
    """
    user = current_user(request)
    default_tr = default_translator()
    return {
        "initialized": runtime.auth.initialized,
        "authenticated": bool(user),
        "username": user["username"] if user else None,
        "llm_configured": bool(default_tr.key),
        "llm_from_ui": runtime.settings.saved_by_ui(user["username"]) if user else False,
        "translator": default_tr.status_text,
        # 登录页需要知道：能不能白助注册、有几个账号
        "signup": ALLOW_SIGNUP,
        "accounts": runtime.auth.count(),
        # 登录页就能看到“有没有连上 MongoDB”，不用等登录后再发现
        "storage": runtime.storage_backend,
        "storage_warning": (runtime.storage_info.get("warning") if runtime.storage_backend == "json" else None),
    }


@router.post("/api/auth/setup")
def api_auth_setup(request: Request, payload: dict = Body(...)):
    """首次使用：创建第一个账号（管理员）并直接登录。已初始化则 409，防止被抢注。"""
    if runtime.auth.initialized:
        raise HTTPException(409, "已完成初始化，请直接登录（或用「注册新账号」）")
    try:
        user = runtime.auth.create_user(payload.get("username", ""), payload.get("password", ""))
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    migrate.adopt_legacy_data()        # 第一个账号接手历史（无 owner）的论文与旧版全局 LLM 配置
    # 首次登录(即创建第一个账号)一律引导配置 LLM：会带上当前生效值，
    # 即使 config.json/环境变量里已经有 Key 也能在页面上改写或直接跳过
    return login_user(request, user, payload, need_llm=True)


@router.post("/api/auth/register")
def api_auth_register(request: Request, payload: dict = Body(...)):
    """**未登录也能调**：自助注册一个账号，注册完直接登录。

    - 服务还没初始化时请走 /api/auth/setup（第一个账号），这里会 409；
    - `ALLOW_SIGNUP=0` 时关闭，返回 403；
    - 账号数超过 `MAX_ACCOUNTS`（默认 50）也不让再建，避免被刷爆。
    """
    if not runtime.auth.initialized:
        raise HTTPException(409, "服务还没初始化，请先创建管理员账号")
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "本服务已关闭自助注册，请让已有账号在「⚙ 设置 → 账号」里添加")
    if runtime.auth.count() >= MAX_ACCOUNTS:
        raise HTTPException(400, f"账号数已达上限（{MAX_ACCOUNTS}）")
    try:
        user = runtime.auth.create_user(payload.get("username", ""), payload.get("password", ""))
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return login_user(request, user, payload)      # need_llm 按该账号自己的配置判断


@router.post("/api/auth/login")
def api_auth_login(request: Request, payload: dict = Body(...)):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    ip = client_ip(request)
    left = runtime.auth.locked_seconds(username, ip)
    if left:
        raise HTTPException(429, f"失败次数过多，请 {left} 秒后再试")
    user = runtime.auth.authenticate(username, password)
    if not user:
        runtime.auth.note_failure(username, ip)
        raise HTTPException(401, "用户名或密码不正确")
    runtime.auth.clear_failures(username, ip)
    return login_user(request, user, payload)      # need_llm 按该账号自己的配置判断


@router.post("/api/auth/logout")
def api_auth_logout(request: Request):
    runtime.auth.drop_session(request.cookies.get(auth_mod.SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth_mod.SESSION_COOKIE, path="/")
    return resp


@router.post("/api/auth/password")
def api_auth_password(request: Request, payload: dict = Body(...)):
    user = require_user(request)
    try:
        runtime.auth.change_password(user["username"], payload.get("old_password") or "",
                             payload.get("new_password") or "",
                             keep_token=request.cookies.get(auth_mod.SESSION_COOKIE))
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.get("/api/prefs")
def api_get_prefs(request: Request):
    """当前账号的偏好。注意：**随账号存**，不同账号互不影响。"""
    user = require_user(request)
    prefs = runtime.auth.get_prefs(user["username"])
    return {"hl_colors": prefs.get("hl_colors") or [],
            "show_zh": prefs.get("show_zh"),
            "theme": prefs.get("theme")}


@router.post("/api/prefs")
def api_set_prefs(request: Request, payload: dict = Body(...)):
    """保存当前账号的偏好：3 个自定义高亮色 + “显示译文”开关 + 明/暗主题。"""
    user = require_user(request)
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
        prefs = runtime.auth.set_prefs(user["username"], patch)
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "hl_colors": prefs.get("hl_colors") or [],
            "show_zh": prefs.get("show_zh"), "theme": prefs.get("theme")}


@router.post("/api/auth/deactivate")
def api_auth_deactivate(request: Request, payload: dict = Body(...)):
    """注销当前登录的账号。

    需要：当前口令 + 重输用户名确认（服务端也校验）；
    效果：该账号名下**全部论文数据**（版式数据/标注/笔记/译文/图片）与账号、会话一并删除，
    不可恢复；注销后前端回登录页（若已无账号则回到“首次使用”初始化流程）。
    """
    user = require_user(request)
    try:
        # 注意：必须传 confirm（缺省用空串，不能是 None，否则会被当成“跳过确认”）
        closed = runtime.auth.close_account(user["username"], payload.get("password") or "",
                                   payload.get("confirm") or "")
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    removed = runtime.store.purge_owner(closed["username"])
    log.warning("账号 %s 已注销：同时清除 %d 篇论文及其数据", closed["username"], removed)
    resp = JSONResponse({
        "ok": True, "username": closed["username"], "removed_papers": removed,
        "initialized": runtime.auth.initialized,
    })
    resp.delete_cookie(auth_mod.SESSION_COOKIE, path="/")
    return resp


@router.get("/api/auth/users")
def api_list_users(request: Request):
    user = require_user(request)
    users = runtime.auth.list_users()
    for u in users:                     # 带上各自论文数，删除前心里有数
        u["papers"] = len(runtime.store.list_by_owner(u["username"]))
    return {"users": users, "current": user["username"],
            "password_min": auth_mod.MIN_PASSWORD}


@router.post("/api/auth/users")
def api_add_user(request: Request, payload: dict = Body(...)):
    """新增一个账号（需已登录）。用于“切换账号”前先建好另一个账号。"""
    user = require_user(request)
    try:
        runtime.auth.create_user(payload.get("username", ""), payload.get("password", ""))
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "users": runtime.auth.list_users(), "current": user["username"]}


@router.delete("/api/auth/users/{username}")
def api_del_user(request: Request, username: str, purge: bool = True):
    """删除账号。`purge=true`（默认）连它名下的论文数据一起删。"""
    user = require_user(request)
    target = runtime.auth.public_user(username)      # 取规范大小写的用户名
    try:
        runtime.auth.delete_user(username, by=user["username"])
    except auth_mod.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    removed = runtime.store.purge_owner(target["username"]) if (purge and target) else 0
    if removed:
        log.info("删除账号 %s 时一并删除 %d 篇论文", username, removed)
    return {"ok": True, "removed_papers": removed, "users": runtime.auth.list_users(),
            "current": user["username"]}
