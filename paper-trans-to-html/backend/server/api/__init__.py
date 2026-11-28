"""全部 /api 路由：按业务域拆分，路径与响应结构与拆分前保持一致。"""
from fastapi import APIRouter

from . import (ai, auth, export, papers, reading, settings, sharing, system,
               toc, transfer, translate)

api_router = APIRouter()
for _module in (system, auth, settings, papers, reading, sharing, toc,
                ai, export, transfer, translate):
    api_router.include_router(_module.router)

__all__ = ["api_router"]
