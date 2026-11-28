"""FastAPI 应用入口：装配中间件、业务路由与前端静态托管。

各业务接口见 `backend/server/api/`（按域拆分）；请求级依赖见 `deps.py`，视图拼装见 `views.py`，
存储三件套与热切换见 `runtime.py`，应用生命周期与通信桥见 `lifecycle.py`。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import lifecycle, migrate, runtime, security
from .api import api_router
from .config import FRONTEND_DIR


log = logging.getLogger("uvicorn.error")

# 存储装配：MongoDB 优先（见 storage.py / APP_STORAGE）；热切换见 runtime.rebuild()
runtime.load()
migrate.adopt_legacy_data()      # 启动时跑一次：老数据不至于因为加 owner/按账号隔离而“消失”

app = FastAPI(title="论文 PDF → HTML 阅读器", version="1.3", lifespan=lifecycle.lifespan)
security.install(app)
app.include_router(api_router)


# ---------------- 前端静态资源（必须最后挂：兜底所有未匹配路径） ----------------
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
