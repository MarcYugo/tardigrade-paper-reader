"""兼容入口：应用本体已移到 `backend/server/app.py`（uvicorn 目标 `backend.server.app:app`）。

保留本文件只为让旧命令 `uvicorn backend.app:app` 与既有部署脚本继续可用。
"""
from .server.app import app

__all__ = ["app"]
