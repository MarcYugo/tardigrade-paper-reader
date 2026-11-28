"""按账号装配 LLM / 翻译服务（配置口径统一走 `settings.translator_config`）。"""
from __future__ import annotations

from ..agents.llm import LlmClient
from ..translate import Translator
from . import runtime


def translator_for(username: str) -> Translator:
    """按「本账号设置 > 环境变量 > config.json > 默认」装配**这个账号**的翻译服务。

    LLM 配置（地址/Key/模型）与**译文目标语言**都跟账号走：A 存的 Key 只有 A 用得到，
    A 选的目标语言也只影响 A 的翻译（原文语言交给 LLM 识别）。
    """
    return Translator(config_file=runtime.settings.translator_config(username))


def default_translator() -> Translator:
    """未登录场景（登录页提示）用的“安装级默认”翻译服务：只看环境变量/config.json。"""
    return Translator(config_file=runtime.settings.translator_config(""))


def llm_for(username: str) -> LlmClient:
    """按账号装配 **LLM 客户端**（直接用 `agents/llm.py`，不经 `Translator`）。

    配置解析与 `translator_for()` 完全同源（`runtime.settings.translator_config`），
    只是这里只取对话/工具调用需要的那一层，不包翻译业务。
    """
    return LlmClient(config_file=runtime.settings.translator_config(username))
