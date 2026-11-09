"""译文服务：论文原文 → 目标语言（默认简体中文，可在「⚙ 设置」里改）。

**原文语言不配、也不猜**：交给 LLM 自己识别（论文可能是英/日/德…）；这里只决定
**译文用哪种语言**（见 `TARGET_LANGS`），以及按语言分开缓存译文（`cache_key`）。

后端优先走 **OpenAI 兼容的 chat/completions**(DeepSeek / OpenAI / 自建中转都行，
批量 + JSON 输出)，未配置 Key 时若安装了 deep-translator 则退化为免费 Google 翻译；
两者都不可用时 `ready=False`，前端会给出配置提示。

配置项(见 `settings.py` 的优先级说明)：
  backend/provider 服务类型(deepseek/openai/anthropic/custom/google)
  base_url         服务地址，如 https://api.deepseek.com
  api_key          密钥(必需，缺失则退化 Google)
  model            模型名
  target_lang      译文语言(target_lang，默认 zh-CN)，按账号存在 `users.prefs.target_lang`

网页里首次登录填写的配置存在 `data/llm.json`，优先于环境变量与 config.json。

**LLM 接入（HTTP、流式解析、tool_calls、测试连接）在 `agents/llm.py` 的 `LlmClient`**：
本模块只保留翻译本身（提示词、批量、Google 兜底、译文语言的判定与缓存键），对话类
方法（`chat` / `chat_json` / `chat_stream` / `chat_completion*` / `validate_chat`）直接
转发过去，签名与提示文案保持历史形状，调用方无需改动。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterator

from .agents.llm import LlmClient, LlmUnavailable, extract_json

# 旧名兼容：外部代码/脚本可能仍在 from .translate import TranslationUnavailable
TranslationUnavailable = LlmUnavailable

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
_JUNK = re.compile(r"^[\s\d\W_]*$", re.UNICODE)  # 纯数字/标点/空白
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")        # 日文假名
_HANGUL = re.compile(r"[\u1100-\u11ff\uac00-\ud7af]")      # 韩文谚文
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")                 # 俄文（西里尔）
_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f]")      # 阿拉伯文
_DEVANAGARI = re.compile(r"[\u0900-\u097f]")               # 印地文（天城文）

# 翻译目标语言：键 -> 名称（也是提示词里对 LLM 的称呼）。前端下拉就照这个表生成，
# 想加语言在这里加一行即可（Google 免费兜底也直接用它当目标语言代码）。
TARGET_LANGS: dict[str, str] = {
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "pt": "Português",
    "ru": "Русский",
    "ar": "العربية",
    "hi": "हिन्दी",
}
DEFAULT_TARGET_LANG = "zh-CN"

# 常见别名（旧配置 / 浏览器地区码里可能出现 zh、zh-Hans、en-US 这类写法）
_LANG_ALIAS = {
    "zh": "zh-CN", "zh-hans": "zh-CN", "zh-sg": "zh-CN", "cn": "zh-CN",
    "zh-hant": "zh-TW", "zh-hk": "zh-TW", "zh-mo": "zh-TW",
}


def resolve_lang(lang) -> str:
    """把语言键解析成 `TARGET_LANGS` 里的规范键；不认识就抛 `ValueError`。"""
    v = str(lang or "").strip()
    if not v:
        return DEFAULT_TARGET_LANG
    low = v.lower()
    if low in _LANG_ALIAS:
        return _LANG_ALIAS[low]
    base = low.split("-")[0]
    for key in TARGET_LANGS:
        if key.lower() in (low, base):
            return key
    raise ValueError(f"不支持的目标语言：{v}")


def normalize_lang(lang) -> str:
    """同 `resolve_lang`，但**认不出就退回默认**（读旧配置/脏数据时不至于报错）。"""
    try:
        return resolve_lang(lang)
    except ValueError:
        return DEFAULT_TARGET_LANG


def lang_name(lang=None) -> str:
    """目标语言的显示名（与提示词里对 LLM 的称呼一致）。"""
    return TARGET_LANGS[normalize_lang(lang)]


def cache_key(text: str, lang=None) -> str:
    """译文缓存的键：**同一段文、不同目标语言互不覆盖**。

    默认目标语言（简体中文）沿用原来的键（纯文本 sha1），历史缓存继续可用；其它语言
    把语言键混进哈希里，换语言后不会命中上一门语言的旧译文，切回去也还在。
    """
    key = normalize_lang(lang)
    raw = text if key == DEFAULT_TARGET_LANG else f"{key}\x00{text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------- 块文本 → 「复制 / 翻译」用的文本（带公式） ----------------
_WS = re.compile(r"\s+")


def page_formula_copies(page: dict) -> dict[str, list[tuple[int, int, str]]]:
    """`page["formula_boxes"]` → `{块id: [(start, end, latex), ...]}`。

    `formula_box["copy"]` 是解析侧记下的「这个公式占位落在哪个块的哪个字符区间」，
    坐标按块 canonical（含行末 `\\n`）计字符数。
    """
    out: dict[str, list[tuple[int, int, str]]] = {}
    for meta in page.get("formula_boxes") or []:
        latex = str(meta.get("latex") or "").strip()
        if not latex:
            continue
        for c in meta.get("copy") or []:
            bid = c.get("id")
            try:
                a, b = int(c.get("start")), int(c.get("end"))
            except (TypeError, ValueError):
                continue
            if bid and b > a:
                out.setdefault(str(bid), []).append((a, b, latex))
    return out


def block_copy_text(text: str, copies: list[tuple[int, int, str]] | None = None) -> str:
    """块文本 → **「复制」和「送翻译」用的同一份文本**：公式占位换成 `$latex$`，
    所有空白压成一个空格。

    ⚠️ 与前端 `reader2.js` 的 `copyRangeText` / `itemCopyText` 必须**同口径**：
    译文缓存的键就是这段文本的 sha1，两边算法不一致的话，每次进页面都会判定成
    “这段没翻译过”，白白重复计费。
    """
    t = text or ""
    for start, end, latex in sorted(copies or [], key=lambda c: -c[0]):
        if 0 <= start < end <= len(t):
            t = f"{t[:start]}${latex}${t[end:]}"
    return _WS.sub(" ", t).strip()


def _ratio(pat: re.Pattern, text: str) -> float:
    return len(pat.findall(text)) / max(len(text), 1)


def looks_chinese(text: str) -> bool:
    """像不像中文：汉字占比够高，且假名很少（日文里汉字也多，别把它当成中文）。"""
    if not text:
        return False
    return _ratio(_CJK, text) > 0.12 and _ratio(_KANA, text) < 0.05


def is_target_language(text: str, lang=None) -> bool:
    """粗判这段文字**已经是目标语言**（那就不用翻，免得把译文再翻一遍）。

    只按字符脚本判断：中文 / 日文 / 韩文 / 俄文 / 阿拉伯文 / 印地文 能可靠区分；
    英、法、德、西、葡同属拉丁字母，光看文本分不出来，一律返回 False —— 交给 LLM
    自己判断（提示词里已要求“段落若已是目标语言就原样返回”）。
    """
    t = (text or "").strip()
    if not t:
        return False
    key = normalize_lang(lang)
    if key.startswith("zh"):
        return looks_chinese(t)
    if key == "ja":
        return _ratio(_KANA, t) > 0.05
    if key == "ko":
        return _ratio(_HANGUL, t) > 0.05
    if key == "ru":
        return _ratio(_CYRILLIC, t) > 0.12
    if key == "ar":
        return _ratio(_ARABIC, t) > 0.12
    if key == "hi":
        return _ratio(_DEVANAGARI, t) > 0.12
    return False


def needs_translation(text: str, lang=None) -> bool:
    """这段文本**需不需要**送去翻译。

    不需要的三种：空/极短、纯数字标点符号、本来就是目标语言。
    接口层要靠它区分两种“没有译文”：
      - 不需要翻译 → 可以打上“无需翻译”的标记，以后不用再试；
      - 需要但没拿到（服务少返/返回空）→ 只是这次失败，**必须可重试**。
    """
    t = (text or "").strip()
    if not t or len(t) < 2 or _JUNK.fullmatch(t):
        return False
    return not is_target_language(t, lang)


def translation_prompt(lang=None) -> str:
    """翻译提示词：**目标语言可配**，原文语言由 LLM 自行识别。"""
    target = lang_name(lang)
    return (
        f"你是一名严谨的学术论文翻译助手，负责把论文原文翻译成{target}；"
        "原文是什么语言你自己识别，不用问用户。\n"
        "规则：\n"
        "1. 忠实原文、术语准确、语句通顺，保持原有段落结构。\n"
        "2. 数字、公式、LaTeX、变量名、引用标记、URL 等原样保留。"
        "输入里的公式是形如 $...$ 的 LaTeX，译文必须在**对应位置**原样带出同样的 "
        "$...$ 公式（不要翻译、不要省略、不要改写成中文文字或纯符号）。\n"
        f"3. 段落若已经是{target}，原样返回，不要改写、也不要另作翻译。\n"
        "4. 只做翻译，不要添加解释或额外说明。\n"
        "5. 每个输入段落输出**一个连续段落**：不要在句与句之间插入空行或换行。\n"
        "用户会发送一个 JSON 对象：{\"texts\": [\"段落1\", \"段落2\", ...]}。\n"
        "请返回一个 JSON 对象：{\"translations\": [\"译文1\", \"译文2\", ...]}，"
        "数量与顺序必须与输入一一对应。只输出 JSON。"
    )


# ---------------- 目录提取（PDF 没有自带书签时用全文让 LLM 排一份） ----------------
_TOC_PROMPT = (
    "你是学术论文的排版助手，任务是从论文全文里**识别章节标题并整理成目录**。\n"
    "用户给你的文本按页给出：每一页以【第N页】开头，后面是该页的候选标题行。\n"
    "规则：\n"
    "1. 只输出真正的章节/小节标题（如 Abstract、1 Introduction、2.1 Method、"
    "Experiments、References），**不要**输出正文句子、图表标题（Figure/Table 开头）、"
    "公式编号、页眉页脚、作者与单位、参考文献条目。\n"
    "2. 标题**保留原文语言与编号**，不要翻译、不要改写、不要加序号。\n"
    "3. `page` 取该标题所在页的页码（按【第N页】标记），`level` 从 1 开始、子标题更深。\n"
    "4. 按文中出现顺序输出，宁少勿多；整篇没有可识别的章节就返回空数组。\n"
    "只输出 JSON：{\"outline\": [{\"level\": 1, \"title\": \"...\", \"page\": 1}]}"
)


def extract_outline(translator: "Translator", title: str, context: str,
                    max_items: int = 120) -> list[dict]:
    """让 LLM 从全文里提取目录，返回 `[{level, title, page}]`（已清洗/夹紧）。"""
    user = (f"论文标题：{title or '（未知）'}\n\n"
            f"论文正文（按页给出候选标题行）：\n{context}\n\n"
            f"请输出目录 JSON。")
    data = translator.chat_json(_TOC_PROMPT, user)
    rows = data.get("outline") if isinstance(data, dict) else None
    if isinstance(rows, dict):                       # 有的模型会包一层 {数字: {...}}
        rows = list(rows.values())
    if not isinstance(rows, list):
        raise LlmUnavailable("返回结果里没有 outline 列表，请重试")

    out: list[dict] = []
    for row in rows[:max_items]:
        if not isinstance(row, dict):
            continue
        t = " ".join(str(row.get("title") or "").split())
        if not t:
            continue
        try:
            page = int(row.get("page") or 0)
        except Exception:
            page = 0
        try:
            level = int(row.get("level") or 1)
        except Exception:
            level = 1
        out.append({"level": max(1, min(level, 4)), "title": t[:200], "page": max(0, page)})
    return out


class Translator:
    """翻译服务：把一次批量翻译交给 LLM（或 Google 兜底）。

    对话能力（`chat*` / `validate_chat`）只是**转发给 `LlmClient`**，签名与报错沿用
    历史形状，调用方（`ai_read` / `agents` / 接口层）不需要知道内部换了实现。
    """

    def __init__(self, config_file: Path | str | dict | None = None,
                 overrides: dict | None = None):
        """`config_file` 可以是配置文件路径，也可以直接是配置字典(便于叠加运行时设置)。"""
        self.llm = LlmClient(config_file, overrides)
        self.provider = self.llm.provider
        self.key = self.llm.key
        self.base = self.llm.base
        self.model = self.llm.model
        self.timeout = self.llm.timeout
        self.backend = self.llm.backend

        # 译文目标语言（原文语言交给 LLM 识别）：提示词与译文缓存的键都用它
        cfg = self.llm.config
        self.target_lang = normalize_lang(cfg.get("target_lang") or cfg.get("lang"))
        self.target_name = lang_name(self.target_lang)

    @property
    def provider_label(self) -> str:
        return self.llm.provider_label

    @property
    def ready(self) -> bool:
        if self.llm.ready:
            return True
        if self.backend == "google":
            try:
                import deep_translator  # noqa: F401
                return True
            except Exception:
                return False
        return False

    @property
    def status_text(self) -> str:
        if self.backend == "google":
            return "Google(免费兜底)"
        return self.llm.status_text

    # ---------------- 批量翻译 ----------------
    def translate(self, texts: list[str]) -> list[str]:
        """输入与输出等长；无需翻译的项(已是目标语言/空/纯符号)原样或留空返回。"""
        jobs: list[tuple[int, str]] = []
        out: list[str] = [""] * len(texts)
        for i, t in enumerate(texts):
            t = (t or "").strip()
            if not needs_translation(t, self.target_lang):
                # 无需翻译：空/极短/纯符号留空；已是目标语言的原样返回
                out[i] = t if is_target_language(t, self.target_lang) else ""
                continue
            jobs.append((i, t))
        if not jobs:
            return out
        if not self.ready:
            raise LlmUnavailable(self.status_text)

        if self.backend == "google":
            self._google(jobs, out)
        else:
            self._openai_compat(jobs, out)
        return out

    # ---------------- 对话能力：转发给 LlmClient ----------------
    def _require_llm(self, what: str) -> None:
        """没有可用的 OpenAI 兼容后端就抛错（Google 兜底做不了对话/结构化任务）。

        提前在**调用时**抛出，接口层才能先回 503 再开流。
        """
        if self.ready and self.backend == "openai":
            return
        raise LlmUnavailable(
            f"当前翻译服务（{self.status_text}）不支持{what}，"
            f"请到「⚙ 设置 → LLM 服务」配置 OpenAI 兼容服务与 API Key")

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        """做一次结构化对话，返回解析出来的 JSON 对象（提取目录这类任务用）。

        只有 OpenAI 兼容后端能做（Google 兜底只能逐句翻译），不能用就抛
        `LlmUnavailable`，前端会提示去配置 LLM。
        """
        self._require_llm("这类任务")
        return self.llm.chat_json(system, user, temperature=temperature)

    def chat(self, system: str, messages: list[dict], *, temperature: float = 0.3) -> str:
        """普通多轮对话，返回助手正文文本（AI 辅助阅读的聊天用）。

        `messages` 形如 `[{"role": "user"|"assistant", "content": "..."}]`，
        会拼在 `system` 之后。非 OpenAI 兼容后端同样抛 `LlmUnavailable`。
        """
        self._require_llm("对话")
        return self.llm.chat(system, messages, temperature=temperature)

    def chat_completion(self, system: str, messages: list[dict], *,
                        tools: list[dict] | None = None,
                        temperature: float = 0.3) -> dict:
        """执行一次 OpenAI 兼容聊天请求，返回 assistant message（可包含 tool_calls）。"""
        self._require_llm("对话")
        return self.llm.chat_completion(system, messages, tools=tools,
                                        temperature=temperature)

    def chat_completion_stream(self, system: str, messages: list[dict], *,
                               tools: list[dict] | None = None,
                               temperature: float = 0.3) -> Iterator[dict]:
        """流式输出正文增量，并在末尾返回完整的 assistant message/tool_calls。"""
        self._require_llm("对话")
        return self.llm.chat_completion_stream(system, messages, tools=tools,
                                               temperature=temperature)

    def validate_chat(self, messages: list[dict]) -> None:
        """在开启流式响应前校验当前账号是否可用对话模型。"""
        self._require_llm("对话")
        self.llm.validate_chat(messages)

    def chat_stream(self, system: str, messages: list[dict], *,
                    temperature: float = 0.3) -> Iterator[str]:
        """流式版 `chat()`：返回一个**逐段 yield 正文增量**的生成器。

        校验（没配 Key / 不是 OpenAI 兼容后端 / 没内容）在**调用时**就抛出，
        网络请求要等真正开始迭代才发 —— 这样接口层能先把错误变成 503，
        而不是等头已经发出去了才发现连不上。
        """
        self._require_llm("对话")
        return self.llm.chat_stream(system, messages, temperature=temperature)

    def _openai_compat(self, jobs: list[tuple[int, str]], out: list[str]) -> None:
        ordered = [t for _, t in jobs]
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": translation_prompt(self.target_lang)},
                {"role": "user", "content": json.dumps({"texts": ordered}, ensure_ascii=False)},
            ],
            "temperature": 0.2,
        }
        # Anthropic 的 OpenAI 兼容层不支持 response_format（带上会报错）；
        # 提示词已明确要求只输出 JSON，解析端 (`extract_json`) 也能从文本里兜住。
        if self.provider != "anthropic":
            body["response_format"] = {"type": "json_object"}
        content = self.llm.post_chat(body)

        data = extract_json(content) or {}
        arr = data.get("translations") if isinstance(data, dict) else data
        if isinstance(arr, list):
            for idx, (pos, _t) in enumerate(jobs):
                zh = arr[idx] if idx < len(arr) else ""
                out[pos] = (zh or "").strip()
        else:
            raise LlmUnavailable("翻译结果格式不符，请重试")

    # ---------------- Google 兜底 ----------------
    def _google(self, jobs: list[tuple[int, str]], out: list[str]) -> None:
        """无需 Key，但翻译质量与稳定性都不如 LLM，仅作兜底（源语言同样交给它自动识别）。"""
        try:
            from deep_translator import GoogleTranslator
        except Exception as exc:
            raise LlmUnavailable("未安装 deep-translator，无法使用免费翻译") from exc
        tr = GoogleTranslator(source="auto", target=self.target_lang)
        for pos, t in jobs:
            try:
                out[pos] = (tr.translate(t) or "").strip()
            except Exception:
                out[pos] = ""
