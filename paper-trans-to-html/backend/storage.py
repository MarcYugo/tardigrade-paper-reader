"""存储装配：按 `APP_STORAGE` 选择 MongoDB 或本地 JSON

`app.py` 只调用一次 `build()`，拿到 `(store, auth, settings, backend_name)`：

| `APP_STORAGE` | 行为 |
| --- | --- |
| `auto`（默认） | 能连上 MongoDB 就用 MongoDB；连不上**回退本地 JSON**并在前端提示 |
| `mongo` | 强制 MongoDB，连不上直接抛 `StorageUnavailable`（启动即报错，避免“以为在写库其实写了文件”） |
| `json` | 强制本地 JSON（不需要 MongoDB 的轻量场景） |

回退时会打印一条醒目的告警，并把原因放进 `/api/config` 的 `storage` 字段，
前端据此在页面上提示“未连接 MongoDB + 重试连接”，不会静默降级。
"""
from __future__ import annotations

from pathlib import Path

from .auth import AuthStore, JsonAuthBackend
from .db import StorageUnavailable, auth_hint, mongo, preferred_backend
from .records import Store
from .settings import JsonSettingsBackend, SettingsStore


def build(data_dir: Path | str, config_file: Path | str | None = None) -> tuple:
    """返回 `(store, auth, settings, backend_name, storage_info)`。"""
    data_dir = Path(data_dir)
    want = preferred_backend()

    use_mongo = False
    if want == "json":
        use_mongo = False
    else:
        use_mongo = mongo.available
        if want == "mongo" and not use_mongo:
            msg = (f"APP_STORAGE=mongo 但连不上 MongoDB：{mongo.error}（"
                   f"检查 {mongo.cfg['host']}:{mongo.cfg['port']} 是否已启动，"
                   f"可执行 mongo_configuration/ 下的 docker compose up -d）")
            hint = auth_hint(mongo.error, mongo.cfg)
            raise StorageUnavailable(f"{msg} {hint}" if hint else msg)

    if use_mongo:
        # 延迟导入：没装 pymongo 时也能走 JSON 回退
        from .mongo_store import MongoAuthBackend, MongoSettingsBackend, MongoStore
        store = MongoStore(data_dir, mongo)
        auth = AuthStore(data_dir, backend=MongoAuthBackend(mongo))
        # LLM 配置按账号存（users.prefs.llm）；legacy 是旧版“全局一份”，仅供迁移读取
        settings = SettingsStore(data_dir, config_file, prefs=auth,
                                 legacy=MongoSettingsBackend(mongo))
        backend_name = "mongo"
    else:
        store = Store(data_dir)
        auth = AuthStore(data_dir, backend=JsonAuthBackend(data_dir))
        settings = SettingsStore(data_dir, config_file, prefs=auth,
                                 legacy=JsonSettingsBackend(data_dir / "llm.json"))
        backend_name = "json"

    info = {
        "backend": backend_name,
        "preferred": want,
        "mongo": mongo.status(),
        "data_dir": str(data_dir),
    }
    if backend_name == "json" and want != "json":
        warning = (
            "未连接 MongoDB，已退回本地 JSON。"
            f"原因：{mongo.error or '未知'}。"
            "启动 mongo_configuration 后点“重试连接”即可切回数据库。")
        # 认证失败很常见（账号没建出来/口令不符），把“怎么修”一并写进日志与前端提示
        hint = auth_hint(mongo.error, mongo.cfg)
        info["warning"] = f"{warning} {hint}" if hint else warning
    return store, auth, settings, backend_name, info


def rebuild(data_dir: Path | str, config_file: Path | str | None = None) -> tuple:
    """重新探测 MongoDB 并重新装配（前端「重试连接」用）。"""
    mongo.refresh()
    return build(data_dir, config_file)
