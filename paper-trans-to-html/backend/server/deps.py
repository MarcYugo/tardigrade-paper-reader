"""请求级依赖：当前账号、论文权限、transfer key 校验、会话签发。"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .. import auth as auth_mod, records
from . import runtime
from .factories import translator_for


# 给「其它 Agent」用 transfer key 调取的接口：不走登录 Cookie，
# 由 `transfer_owner()` 做 `Authorization: Bearer <key>` 校验（见导出弹窗「转交 Key」）。
# 注意末尾的 `/`：`/api/transfer-keys` 是**登录后**管理 key 的接口，不在放行范围内。
TRANSFER_PREFIX = "/api/transfer/"

# 权限高低：只读 < 可写 < 所有者
PERM_RANK = {"read": 1, "write": 2, "owner": 3}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(auth_mod.SESSION_COOKIE)
    return runtime.auth.user_for_token(token)


def require_user(request: Request) -> dict:
    """接口层双保险：中间件已拦一层，这里再拦一次（防止@中间件被调整）。"""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    return user


def safe_next(value) -> str | None:
    """登录后的回跳地址：只允许站内相对路径，避免被拿去做开放重定向。"""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return None
    return value


def set_session(resp: JSONResponse, token: str) -> JSONResponse:
    resp.set_cookie(auth_mod.SESSION_COOKIE, token, max_age=runtime.auth.cookie_max_age,
                    httponly=True, samesite="lax", path="/")
    return resp


def login_user(request: Request, user: dict, payload: dict, *, need_llm: bool | None = None) -> JSONResponse:
    """签发会话并返回登录成功的响应（setup/register/login 共用）。

    `need_llm=None` 时按该账号自己的配置判断（没 Key 就引导去配）。
    """
    token = runtime.auth.new_session(user["username"], ip=client_ip(request))
    if need_llm is None:
        need_llm = not bool(translator_for(user["username"]).key)
    resp = JSONResponse({
        "ok": True, "username": user["username"],
        "need_llm": need_llm,
        "papers": len(runtime.store.list_by_owner(user["username"])),
        "next": safe_next(payload.get("next")),
    })
    return set_session(resp, token)


def require_doc(request: Request, doc_id: str) -> dict:
    """论文必须存在**且属于当前账号**，否则一律 404（不泄露“别人的论文存在”）。

    只用在“只有所有者能做”的事上（删除论文、管理共享名单）；
    看/改笔记这类要区分权限的走 `doc_access()`。
    """
    user = require_user(request)
    rec = runtime.store.get_record(doc_id)
    if not rec or records.owner_of(rec) != user["username"]:
        raise HTTPException(404, "论文不存在或已被删除")
    return rec


# 权限高低：只读 < 可写 < 所有者
PERM_RANK = {"read": 1, "write": 2, "owner": 3}


def doc_access(request: Request, doc_id: str, need: str = "read") -> tuple[dict, str]:
    """论文必须存在、且**当前账号权限足够**，否则 404（不泄露别人的论文存在）。

    `need`：`read`（看）/ `write`（改笔记、高亮、整理、对话）/ `owner`（删论文、管共享）。
    返回 `(记录, 权限)`，权限是 `owner` / `write` / `read`。
    """
    user = require_user(request)
    rec = runtime.store.get_record(doc_id)
    perm = records.perm_of(rec, user["username"])
    if not perm:
        raise HTTPException(404, "论文不存在或已被删除")
    if PERM_RANK[perm] < PERM_RANK[need]:
        raise HTTPException(
            403, f"这篇论文是「{'只读' if perm == 'read' else '共享'}」共享给你的，"
                 f"该操作需要更高权限")
    return rec, perm


def transfer_owner(request: Request) -> dict:
    """校验请求携带的 transfer key，返回 `{username, id, name}`（无效则 401）。

    取值顺序：`Authorization: Bearer <key>` → `X-Transfer-Key: <key>` → `?key=<key>`。
    """
    raw = ""
    authz = request.headers.get("authorization") or ""
    if authz.lower().startswith("bearer "):
        raw = authz[7:]
    if not raw.strip():
        raw = request.headers.get("x-transfer-key") or ""
    if not raw.strip():
        raw = request.query_params.get("key") or ""
    who = runtime.auth.resolve_transfer_key(raw.strip())
    if not who:
        raise HTTPException(401, "transfer key 无效或已被删除")
    return who
