"""进程级运行时状态：存储三件套 + 通信桥托管句柄。

`/api/storage/reconnect` 会**原地替换**这里的属性（见 `rebuild()`），所以使用者必须写
`runtime.store` / `runtime.auth` / `runtime.settings`，不能 `from .runtime import store`
—— 后者拿到的是旧引用，热切换后还在往已经作废的存储后端里写。

`runtime.settings` 是存储层暴露的 settings 门面，与同名模块 `backend/settings.py`
（统一以 `settings_mod` 引用）不是一回事；`runtime.auth` 与 `backend/auth.py` 同理。
"""
from __future__ import annotations

import logging

from ..db import mongo
from ..storage import build as build_storage, rebuild as rebuild_storage
from .config import CONFIG_FILE, DATA_DIR


log = logging.getLogger("uvicorn.error")

store = None
auth = None
settings = None
storage_backend = "json"
storage_info: dict = {}
comm = None                 # 通信桥 supervisor（启停见 lifecycle.py）


def load() -> None:
    """按 `APP_STORAGE` / MongoDB 连通性装配存储三件套（进程启动时一次）。"""
    global store, auth, settings, storage_backend, storage_info
    store, auth, settings, storage_backend, storage_info = build_storage(DATA_DIR, CONFIG_FILE)
    if storage_backend == "mongo":
        log.info("数据存储：MongoDB %s / 库 %s（配置来源 %s）",
                 mongo.cfg["host"] + (":" + mongo.cfg["port"] if mongo.cfg.get("port") else ""),
                 mongo.cfg["db"], mongo.cfg.get("source"))
    else:
        log.warning("数据存储：本地 JSON（%s）",
                    storage_info.get("warning") or "APP_STORAGE=json")


def rebuild() -> str:
    """重新探测并热切换存储后端，返回切换后的后端名（失败时抛 StorageUnavailable）。"""
    global store, auth, settings, storage_backend, storage_info
    before = storage_backend
    store, auth, settings, storage_backend, storage_info = rebuild_storage(DATA_DIR, CONFIG_FILE)
    log.info("数据存储已切换：%s → %s", before, storage_backend)
    return storage_backend


def storage_state() -> dict:
    """给前端看的存储状态（含实时探测的 Mongo 连通性）。"""
    info = dict(storage_info)
    info.pop("warning", None)
    info["mongo"] = mongo.status()
    info["backend"] = storage_backend
    try:
        info["papers"] = len(store.list_records())
        info["initialized_users"] = auth.count()
    except Exception as exc:                      # 数据库中途挂了也不该让接口 500
        info["error"] = str(exc)
    want = info.get("preferred", "auto")
    if storage_backend == "json" and want != "json":
        info["warning"] = storage_info.get("warning") or "未连接 MongoDB，正在使用本地 JSON 存储"
    return info
