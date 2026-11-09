"""极简 `.env` 读取（本项目没装 `python-dotenv`）。

约定：`.env` 里的键**不注入** `os.environ`，由需要的模块按需取，
取值顺序统一为「进程环境变量 → `.env` → 默认值」。这样做的好处：

* 系统 / compose 设的环境变量永远优先，不会出现“改了 `.env` 却被环境变量盖住”的困惑；
* `.env` 文件 mtime 变化会被自动重读，换 key 不用重启进程；
* 只有真正需要这些 key 的模块才读它，别的模块（如存储层）行为完全不变。

> 存储层（`backend/db.py`）有自己的 `.env` 解析（只取 `MONGO_*`），是历史行为，不动它。
"""
from __future__ import annotations

import os
from pathlib import Path

# 按顺序取第一个存在的文件（可用 `APP_ENV_FILE` / `NOTE_OUT_ENV_FILE` 指定别的）
_CANDIDATES = (
    Path(__file__).resolve().parents[1] / ".env",                    # paper-trans-to-html/.env
    Path(__file__).resolve().parents[1] / "docker" / ".env",
    Path(__file__).resolve().parents[2] / "mongo_configuration" / ".env",
)
_CACHE: dict = {"key": None, "data": {}}


def env_file_path() -> Path | None:
    """实际读取的 `.env`（都不存在时返回 None）。"""
    custom = ((os.environ.get("APP_ENV_FILE") or "").strip()
              or (os.environ.get("NOTE_OUT_ENV_FILE") or "").strip())
    if custom:
        path = Path(custom).expanduser()
        return path if path.exists() else None
    return next((p for p in _CANDIDATES if p.exists()), None)


def read_env_file(path: Path | str | None = None) -> dict[str, str]:
    """解析 `.env`（`KEY=VALUE`，支持 `#` 注释与引号）；失败返回空字典。"""
    p = Path(path) if path else env_file_path()
    if p is None or not p.exists():
        return {}
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    cache_key = (str(p), mtime)
    if _CACHE["key"] == cache_key:
        return _CACHE["data"]
    data: dict[str, str] = {}
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            data[key.strip()] = val.strip().strip('"').strip("'")
    except Exception:
        data = {}
    _CACHE["key"] = cache_key
    _CACHE["data"] = data
    return data


def env_value(name: str) -> str:
    """配置值：进程环境变量 → `.env` → 空串。"""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    return str(read_env_file().get(name) or "").strip()


__all__ = ["env_file_path", "read_env_file", "env_value"]
