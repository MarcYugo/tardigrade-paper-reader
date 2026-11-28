"""HTTP 路由清单回归：`app.py` 拆成 `backend/server/api/*` 后，路径与响应结构必须保持不变。

这里冻结 `/api` 路由集合（路径 + 方法）。任何接口的新增、删除或改名都会让本测试失败，
提醒同步前端调用方、文档与依赖这些接口的其它 Agent。
"""
import pathlib
import shutil
import tempfile
import unittest

import backend.server.config as config

# 用一个临时数据目录跑应用装配，绝不碰仓库里的 data/（app 导入时会 runtime.load()）
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="app-routes-test-"))
config.DATA_DIR = _TMP
config.CONFIG_FILE = _TMP / "config.json"

from starlette.testclient import TestClient  # noqa: E402

from backend.app import app  # noqa: E402

EXPECTED_API_ROUTES = {
    ("/api/agent-comm/status", ("GET",)),
    ("/api/auth/deactivate", ("POST",)),
    ("/api/auth/login", ("POST",)),
    ("/api/auth/logout", ("POST",)),
    ("/api/auth/password", ("POST",)),
    ("/api/auth/register", ("POST",)),
    ("/api/auth/setup", ("POST",)),
    ("/api/auth/status", ("GET",)),
    ("/api/auth/users", ("GET",)),
    ("/api/auth/users", ("POST",)),
    ("/api/auth/users/{username}", ("DELETE",)),
    ("/api/config", ("GET",)),
    ("/api/convert", ("POST",)),
    ("/api/doc/{doc_id}", ("DELETE",)),
    ("/api/doc/{doc_id}", ("GET",)),
    ("/api/doc/{doc_id}/ai", ("GET",)),
    ("/api/doc/{doc_id}/ai/chat", ("POST",)),
    ("/api/doc/{doc_id}/ai/chat/reset", ("POST",)),
    ("/api/doc/{doc_id}/ai/summary", ("POST",)),
    ("/api/doc/{doc_id}/ai/summary/save", ("POST",)),
    ("/api/doc/{doc_id}/anno", ("GET",)),
    ("/api/doc/{doc_id}/anno", ("POST",)),
    ("/api/doc/{doc_id}/img/{fname}", ("GET",)),
    ("/api/doc/{doc_id}/note-out", ("POST",)),
    ("/api/doc/{doc_id}/progress", ("GET",)),
    ("/api/doc/{doc_id}/progress", ("POST",)),
    ("/api/doc/{doc_id}/refs", ("GET",)),
    ("/api/doc/{doc_id}/reparse", ("POST",)),
    ("/api/doc/{doc_id}/seltrans", ("DELETE",)),
    ("/api/doc/{doc_id}/seltrans", ("GET",)),
    ("/api/doc/{doc_id}/seltrans", ("POST",)),
    ("/api/doc/{doc_id}/share", ("GET",)),
    ("/api/doc/{doc_id}/share", ("POST",)),
    ("/api/doc/{doc_id}/share/leave", ("POST",)),
    ("/api/doc/{doc_id}/share/{username}", ("DELETE",)),
    ("/api/doc/{doc_id}/title", ("POST",)),
    ("/api/doc/{doc_id}/toc", ("GET",)),
    ("/api/doc/{doc_id}/toc", ("POST",)),
    ("/api/doc/{doc_id}/zh_all", ("GET",)),
    ("/api/docs", ("GET",)),
    ("/api/health", ("GET",)),
    ("/api/prefs", ("GET",)),
    ("/api/prefs", ("POST",)),
    ("/api/settings/llm", ("GET",)),
    ("/api/settings/llm", ("POST",)),
    ("/api/settings/llm/activate", ("POST",)),
    ("/api/settings/llm/delete", ("POST",)),
    ("/api/settings/llm/test", ("POST",)),
    ("/api/settings/parser", ("GET",)),
    ("/api/settings/parser", ("POST",)),
    ("/api/settings/sandbox", ("GET",)),
    ("/api/settings/sandbox", ("POST",)),
    ("/api/settings/scholar", ("GET",)),
    ("/api/settings/scholar", ("POST",)),
    ("/api/settings/scholar/test", ("POST",)),
    ("/api/storage/reconnect", ("POST",)),
    ("/api/storage/status", ("GET",)),
    ("/api/transfer-keys", ("GET",)),
    ("/api/transfer-keys", ("POST",)),
    ("/api/transfer-keys/{key_id}", ("DELETE",)),
    ("/api/transfer/papers", ("GET",)),
    ("/api/transfer/papers/{doc_id}", ("GET",)),
    ("/api/transfer/whoami", ("GET",)),
    ("/api/translate", ("POST",)),
}


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class AppRoutesTest(unittest.TestCase):
    def test_api_route_inventory(self):
        actual = {(route.path, tuple(sorted(route.methods)))
                  for route in app.routes
                  if getattr(route, "path", "").startswith("/api/")}
        self.assertEqual(EXPECTED_API_ROUTES, actual)

    def test_static_mount_comes_last(self):
        # 兜底挂载必须在所有接口之后，否则未匹配的 /api 请求会被静态托管吞掉
        self.assertEqual("", app.routes[-1].path)
        self.assertEqual("static", app.routes[-1].name)

    def test_unauthenticated_requests(self):
        with TestClient(app) as client:
            self.assertEqual(200, client.get("/api/health").status_code)
            self.assertEqual(401, client.get("/api/docs").status_code)
            page = client.get("/login.html")
            self.assertEqual(200, page.status_code)
            self.assertEqual("no-cache", page.headers.get("cache-control"))
