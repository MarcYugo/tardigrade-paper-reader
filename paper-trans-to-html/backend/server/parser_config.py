"""PDF 解析后端：账号级选项 + 状态探测缓存。"""
from __future__ import annotations

import datetime

from .. import pdf_parser
from . import runtime
from .config import CONFIG_FILE, PARSER_BACKENDS


# 解析状态（`pdf_parser.status`）里含**真探测**：对 Surya / Unlimited-OCR 的服务
# `GET /models`，服务没起时要一直等到超时（实测 4s+）。首页与阅读页每次打开都会调
# `/api/config`，反复等超时会明显拖慢“打开就能看到内容” ── 这里加一个很短的缓存。
_STATUS_CACHE: dict = {"key": None, "at": 0.0, "value": None}
_STATUS_TTL = 15.0        # 秒：探测结果缓存时间（设置里点了“重试连接”最多等这么久才看到新结果）


def status_cached(username: str) -> dict:
    key = (username, str(CONFIG_FILE))
    now = datetime.datetime.now().timestamp()
    ent = _STATUS_CACHE
    if ent.get("key") == key and ent.get("value") is not None and now - ent.get("at", 0) < _STATUS_TTL:
        return ent["value"]
    value = pdf_parser.status(CONFIG_FILE, account_options=options_for(username))
    _STATUS_CACHE.update({"key": key, "at": now, "value": value})
    return value


def options_for(username: str) -> dict:
    """返回账号专属的解析选项；未设置时沿用 config.json 中的安装级默认值。"""
    parser = runtime.auth.get_prefs(username).get("parser")
    if not isinstance(parser, dict):
        return {}
    return {key: value for key, value in parser.items()
            if key == "backend" and value in PARSER_BACKENDS}
