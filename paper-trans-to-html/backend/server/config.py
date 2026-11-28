"""安装级常量：路径、启动开关、解析后端枚举（不依赖任何运行时对象）。"""
from __future__ import annotations

import os

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent.parent      # backend/server/config.py → 项目根
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"
CONFIG_FILE = BASE_DIR / "config.json"

# 未登录也能白助注册（默认开；`ALLOW_SIGNUP=0` 关闭）。本服务默认只绑 127.0.0.1，
# 若是要暴露到公网，请关掉它，改用“登录后在「账号」里新增”。
ALLOW_SIGNUP = (os.environ.get("ALLOW_SIGNUP", "1") or "").strip().lower() not in (
    "0", "false", "no", "off")
MAX_ACCOUNTS = int(os.environ.get("MAX_ACCOUNTS", "50") or 50)   # 防白助注册无限拉账号

PARSER_BACKENDS = ("auto", "surya", "unlimited_ocr", "pymupdf")
