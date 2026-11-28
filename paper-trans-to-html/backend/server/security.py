"""登录门禁与静态资源缓存策略（不含任何路由）。"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

from .deps import TRANSFER_PREFIX, current_user


PUBLIC_EXACT = {           # 无需登录即可访问
    "/login.html", "/favicon.ico",
    "/api/health",                       # 容器/负载均衡健康检查
    "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout",
    "/api/auth/register",      # 未登录白助注册（受 ALLOW_SIGNUP 控制）
}
PUBLIC_PREFIX = ("/css/", "/js/", "/assets/", "/img/",
                  "/logo/",       # 品牌图标(登录页/顶栏都要在未登录状态取到)
                  "/vendor/")     # 登录页要用的静态资源；vendor 是内置的 KaTeX(js/css/字体)
                                  # 放行可避免「未带 Cookie 取字体被 302 掉」这类隐性故障

class AuthGate(BaseHTTPMiddleware):
    """未登录时：接口 → 401(前端跳登录页)；页面 → 302 跳登录页并带上回跳地址。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX):
            return await call_next(request)
        if path.startswith(TRANSFER_PREFIX):       # 持 transfer key 的 Agent
            return await call_next(request)
        if current_user(request):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
        nxt = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/login.html?next={quote(nxt, safe='/')}", status_code=302)




async def static_nocache(request: Request, call_next):
    """页面与前端资源一律 `no-cache`（允许缓存但强制每次条件请求）：
    改完 js/css 刷新即生效，不用再清浏览器缓存——否则很容易出现「代码改了却还是旧行为」
    的假故障。响应仍带 ETag/Last-Modified，文件没变时只是 304，开销可忽略。"""
    resp = await call_next(request)
    path = request.url.path
    if path.endswith(".html") or path.startswith(("/js/", "/css/", "/vendor/")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def install(app: FastAPI) -> None:
    """挂上门禁与静态资源 no-cache 两道中间件（顺序与原 app.py 一致）。"""
    app.add_middleware(AuthGate)
    app.middleware("http")(static_nocache)
