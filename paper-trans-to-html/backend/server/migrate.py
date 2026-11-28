"""启动/换后端时的数据迁移（把历史论文与旧版全局 LLM 配置落到账号上，幂等）。"""
from __future__ import annotations

import logging

from . import runtime


log = logging.getLogger("uvicorn.error")


def _adopt_orphan_papers() -> int:
    """升级到“按账号隔离”后，把没有 owner 的历史论文归给最早创建的账号（幂等）。"""
    try:
        users = runtime.auth.list_users()
        if not users:
            return 0
        n = runtime.store.adopt_orphans(users[0]["username"])
        if n:
            log.info("已把 %d 篇历史论文归属给账号 %s（现在论文按账号隔离）", n, users[0]["username"])
        return n
    except Exception as exc:
        log.warning("历史论文归属迁移失败：%s", exc)
        return 0


def _adopt_legacy_llm() -> bool:
    """旧版 LLM 配置是全局一份，升级后归给最早创建的账号（幂等）。"""
    try:
        users = runtime.auth.list_users()
        if not users:
            return False
        if runtime.settings.adopt_legacy(users[0]["username"]):
            log.info("已把旧版全局 LLM 配置归给账号 %s（现在 LLM 配置按账号隔离）",
                     users[0]["username"])
            return True
    except Exception as exc:
        log.warning("LLM 配置迁移失败：%s", exc)
    return False


def adopt_legacy_data() -> None:
    """启动/首次建号/换后端后跑一次：历史论文 + 旧版全局 LLM 配置都能“落地”。"""
    _adopt_orphan_papers()
    _adopt_legacy_llm()
