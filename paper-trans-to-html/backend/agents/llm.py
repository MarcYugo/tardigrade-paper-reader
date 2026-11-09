"""LLM 接入层：OpenAI 兼容 chat/completions 的客户端（对话 / 结构化 JSON / 流式 / 工具调用）。

只管「把一次请求发出去、把响应解析回来」：配置解析、能力校验（没 Key 或不是
OpenAI 兼容后端就拒绝）、流式增量拼接、`tool_calls` 片段合并、错误归一成
`LlmUnavailable`。

它**不决定**问什么、要不要调工具、要不要再来一轮 —— 那是
`agents/ai_read_agent.py` 与 `agents/note_out_agent.py` 的事；翻译业务的提示词与
批量流程在 `translate.py`，整理/对话的提示词在 `ai_read.py`。

`translate.Translator` 把对话相关方法原样转发到 `LlmClient`（签名与报错保持历史形状），
翻译自身（`translate()` + Google 兜底）仍留在 `Translator` 里；因此本模块不依赖任何
业务模块，也不会与它们形成循环导入。

对外接口：

| 名字 | 作用 |
| --- | --- |
| `LlmClient` | 一次 HTTP 往返的客户端（无状态：历史每次由调用方传入） |
| `probe_llm()` | 「测试连接」用：验证 地址 + Key + 模型 是否可用 |
| `iter_sse_deltas()` | OpenAI 兼容流式响应的逐行解析（正文增量） |
| `extract_json()` | 从模型输出里抠出 JSON（容忍 ``` 围栏与前后废话） |
| `LlmUnavailable` | LLM 侧不可用（没配 Key / 非 OpenAI 兼容 / 上游报错 / 返回无法解析） |
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Iterator

import httpx


class LlmUnavailable(Exception):
    """LLM 侧不可用：没配 Key / 不是 OpenAI 兼容后端 / 上游报错 / 返回无法解析。

    这个异常不只属于翻译：对话、笔记整理、工具调用、结构化 JSON 都共用它。
    接口层据此回 503 或 SSE `{"type":"error"}`。

    旧名 `TranslationUnavailable` 保留为别名，避免老代码/外部脚本 import 失败。
    """


# 兼容旧名：早期只有翻译用它，外部代码可能仍在 import。
TranslationUnavailable = LlmUnavailable


def extract_json(content: str):
    """从模型输出里挖出 JSON：容忍 ``` 围栏、前后废话，失败返回 `None`。"""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\[.*\]", content, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def _delta_text(value) -> str:
    """`delta.content` 既可能是字符串，也可能是分片数组（有的服务这么返回）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for p in value:
            if isinstance(p, dict):
                if isinstance(p.get("text"), str):
                    parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return ""


def iter_sse_deltas(lines: Iterable[str]) -> Iterator[str]:
    """把 OpenAI 兼容的**流式**响应行解析成正文增量。

    - 逐行 `data: {...}`（SSE），`data: [DONE]` 结束；
    - 服务端忽略 `stream` 直接回整包 JSON 时也能拿到整段正文（只 yield 一次）；
    - 空行 / 注释 / `event:` / 半截 JSON 一律跳过，不让它们打断流。
    """
    for raw in lines:
        line = (raw or "").strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line:
            continue
        if line == "[DONE]":
            return
        try:
            obj = json.loads(line)
        except Exception:
            continue                    # 可能是不完整的一行，跳过就好
        if not isinstance(obj, dict):
            continue
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        ch = choices[0] if isinstance(choices[0], dict) else {}
        delta = ch.get("delta")
        text = _delta_text(delta.get("content")) if isinstance(delta, dict) else ""
        if not text:
            msg = ch.get("message")      # 非流式的整包响应
            text = _delta_text(msg.get("content")) if isinstance(msg, dict) else ""
        if text:
            # 注意：`reasoning_content`(思维链) 故意丢弃，只把正文流给前端
            yield text


class LlmClient:
    """一次 HTTP 往返的 LLM 客户端。

    无状态：既没有对话历史，也没有「下一步做什么」的决策 —— 历史与系统提示词
    每次由调用方传入，工具调用的循环在 `agents/ai_read_agent.py`。
    """

    def __init__(self, config_file: Path | str | dict | None = None,
                 overrides: dict | None = None):
        """`config_file` 可以是配置文件路径，也可以直接是配置字典(便于叠加运行时设置)。"""
        cfg: dict = {}
        if isinstance(config_file, dict):
            cfg = dict(config_file)
        elif config_file and Path(config_file).exists():
            try:
                data = json.loads(Path(config_file).read_text(encoding="utf-8"))
                cfg = data if isinstance(data, dict) else {}
            except Exception:
                cfg = {}
        if overrides:
            cfg.update(overrides)
        self.config = cfg

        key = (cfg.get("api_key") or cfg.get("deepseek_api_key") or "").strip()
        self.provider = (cfg.get("backend") or cfg.get("provider") or
                         ("deepseek" if key else "google")).strip().lower()
        self.key = key
        self.base = (cfg.get("base_url") or cfg.get("deepseek_base_url")
                     or "https://api.deepseek.com").rstrip("/")
        self.model = cfg.get("model") or cfg.get("deepseek_model") or "deepseek-chat"
        self.timeout = float(cfg.get("timeout") or 120)
        # 有 Key 且不是显式选 Google → 一律按 OpenAI 兼容接口调用；否则退免费兜底
        self.backend = "google" if (self.provider == "google" or not key) else "openai"

    @property
    def provider_label(self) -> str:
        return {"deepseek": "DeepSeek", "openai": "OpenAI",
                "anthropic": "Anthropic (Claude)",
                "custom": "OpenAI 兼容服务", "google": "Google(免费兜底)"}.get(
                    self.provider, self.provider or "自定义")

    @property
    def ready(self) -> bool:
        """能不能发 chat/completions（Google 兜底只能逐句翻译，不算就绪）。"""
        return self.backend == "openai" and bool(self.key)

    @property
    def status_text(self) -> str:
        return f"{self.provider_label} · {self.model}" if self.ready else "未配置"

    # ---------------- OpenAI 兼容接口(DeepSeek / OpenAI / 自建中转) ----------------
    def post_chat(self, body: dict) -> str:
        """POST 一次 chat/completions，返回助手正文。"""
        data = self.post_chat_payload(body)
        try:
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise LlmUnavailable(f"模型响应解析失败：{exc}") from exc

    def post_chat_payload(self, body: dict) -> dict:
        """POST 一次 chat/completions，返回完整的 OpenAI 兼容响应。"""
        try:
            resp = httpx.post(
                f"{self.base}/chat/completions",
                headers={"Authorization": "Bearer " + self.key,
                         "Content-Type": "application/json"},
                json=body, timeout=self.timeout)
        except Exception as exc:
            raise LlmUnavailable(f"请求模型服务失败：{exc}") from exc

        if resp.status_code != 200:
            raise LlmUnavailable(
                f"模型服务返回 {resp.status_code}：{resp.text[:200]}")

        try:
            return resp.json()
        except Exception as exc:
            raise LlmUnavailable(f"模型响应解析失败：{exc}") from exc

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        """做一次结构化对话，返回解析出来的 JSON 对象（提取目录这类任务用）。

        只有 OpenAI 兼容后端能做（Google 兑底只能逐句翻译），不能用就抛
        `LlmUnavailable`，前端会提示去配置 LLM。
        """
        if not self.ready or self.backend != "openai":
            raise LlmUnavailable(
                f"当前 LLM 服务（{self.status_text}）不可用，"
                f"请到「⚙ 设置 → LLM 服务」配置 OpenAI 兼容服务与 API Key")
        content = self.post_chat({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        })
        data = extract_json(content)
        if not isinstance(data, dict):
            raise LlmUnavailable("返回结果不是合法 JSON，请重试")
        return data

    def chat(self, system: str, messages: list[dict], *, temperature: float = 0.3) -> str:
        """普通多轮对话，返回助手正文文本（AI 辅助阅读的聊天用）。

        `messages` 形如 `[{"role": "user"|"assistant", "content": "..."}]`，
        会拼在 `system` 之后。非 OpenAI 兼容后端同样抛 `LlmUnavailable`。
        """
        convo = self._convo(system, messages)
        return (self.post_chat({
            "model": self.model,
            "messages": convo,
            "temperature": temperature,
        }) or "").strip()

    def chat_completion(self, system: str, messages: list[dict], *,
                        tools: list[dict] | None = None,
                        temperature: float = 0.3) -> dict:
        """执行一次 OpenAI 兼容聊天请求，返回 assistant message（可包含 tool_calls）。"""
        convo = self._convo(system, messages)
        body = {"model": self.model, "messages": convo, "temperature": temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data = self.post_chat_payload(body)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmUnavailable(f"模型响应解析失败：{exc}") from exc
        if not isinstance(message, dict):
            raise LlmUnavailable("模型响应格式不符")
        return message

    def chat_completion_stream(self, system: str, messages: list[dict], *,
                               tools: list[dict] | None = None,
                               temperature: float = 0.3) -> Iterator[dict]:
        """流式输出正文增量，并在末尾返回完整的 assistant message/tool_calls。"""
        convo = self._convo(system, messages)
        body = {"model": self.model, "messages": convo,
                "temperature": temperature, "stream": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return self._stream_completion(body)

    def _stream_completion(self, body: dict) -> Iterator[dict]:
        content: list[str] = []
        calls: dict[int, dict] = {}
        try:
            with httpx.stream(
                    "POST", f"{self.base}/chat/completions",
                    headers={"Authorization": "Bearer " + self.key,
                             "Content-Type": "application/json",
                             "Accept": "text/event-stream"},
                    json=body, timeout=self.timeout) as resp:
                if resp.status_code != 200:
                    detail = resp.read().decode("utf-8", "replace")[:200]
                    raise LlmUnavailable(
                        f"模型服务返回 {resp.status_code}：{detail}")
                for raw in resp.iter_lines():
                    line = (raw or "").strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line:
                        continue
                    if line == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    choices = chunk.get("choices") if isinstance(chunk, dict) else None
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        message = choice.get("message")
                        delta = message if isinstance(message, dict) else {}
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        content.append(text)
                        yield {"type": "delta", "text": text}
                    for call in delta.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        try:
                            index = int(call.get("index", 0))
                        except (TypeError, ValueError):
                            continue
                        row = calls.setdefault(index, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if call.get("id"):
                            row["id"] = str(call["id"])
                        function = call.get("function")
                        if isinstance(function, dict):
                            if isinstance(function.get("name"), str):
                                row["function"]["name"] += function["name"]
                            arguments = function.get("arguments")
                            if isinstance(arguments, str):
                                row["function"]["arguments"] += arguments
                            elif isinstance(arguments, dict):
                                row["function"]["arguments"] = arguments
            message = {"content": "".join(content)}
            if calls:
                message["tool_calls"] = [calls[i] for i in sorted(calls)]
            yield {"type": "completion", "message": message}
        except LlmUnavailable:
            raise
        except Exception as exc:
            raise LlmUnavailable(f"请求模型服务失败：{exc}") from exc

    def validate_chat(self, messages: list[dict]) -> None:
        """在开启流式响应前校验当前账号是否可用对话模型。"""
        self._convo("", messages)

    def _convo(self, system: str, messages: list[dict]) -> list[dict]:
        """把 `system` + 多轮消息拼成 OpenAI 的 `messages`，顺便做能力校验。"""
        if not self.ready or self.backend != "openai":
            raise LlmUnavailable(
                f"当前 LLM 服务（{self.status_text}）不可用，"
                f"请到「⚙ 设置 → LLM 服务」配置 OpenAI 兼容服务与 API Key")
        convo = [{"role": "system", "content": system}]
        for m in messages or []:
            role = m.get("role")
            if role in ("user", "assistant"):
                content = m.get("content")
                if isinstance(content, str):
                    content = content.strip() or None
                if content or (role == "assistant" and m.get("tool_calls")):
                    row = {"role": role, "content": content}
                    if role == "assistant" and m.get("tool_calls"):
                        row["tool_calls"] = m["tool_calls"]
                    convo.append(row)
            elif role == "tool" and m.get("tool_call_id"):
                convo.append({"role": "tool", "tool_call_id": str(m["tool_call_id"]),
                              "content": str(m.get("content") or "")})
        if len(convo) < 2:                       # 只有 system，没什么可答的
            raise LlmUnavailable("没有可发送的内容")
        return convo

    def chat_stream(self, system: str, messages: list[dict], *,
                    temperature: float = 0.3) -> Iterator[str]:
        """流式版 `chat()`：返回一个**逐段 yield 正文增量**的生成器。

        校验（没配 Key / 不是 OpenAI 兼容后端 / 没内容）在**调用时**就抛出，
        网络请求要等真正开始迭代才发 —— 这样接口层能先把错误变成 503，
        而不是等头已经发出去了才发现连不上。
        """
        convo = self._convo(system, messages)
        return self._stream_chat({"model": self.model, "messages": convo,
                                  "temperature": temperature, "stream": True})

    def _stream_chat(self, body: dict) -> Iterator[str]:
        try:
            with httpx.stream(
                    "POST", f"{self.base}/chat/completions",
                    headers={"Authorization": "Bearer " + self.key,
                             "Content-Type": "application/json",
                             "Accept": "text/event-stream"},
                    json=body, timeout=self.timeout) as resp:
                if resp.status_code != 200:
                    detail = resp.read().decode("utf-8", "replace")[:200]
                    raise LlmUnavailable(
                        f"模型服务返回 {resp.status_code}：{detail}")
                yield from iter_sse_deltas(resp.iter_lines())
        except LlmUnavailable:
            raise
        except Exception as exc:                     # 连接中断 / 超时 / 解码失败
            raise LlmUnavailable(f"请求模型服务失败：{exc}") from exc


# =====================================================================
#  连通性探测：给“首次登录配置 LLM”页面的「测试连接」用
# =====================================================================
def probe_llm(base_url: str, api_key: str, model: str, timeout: float = 30) -> str:
    """用一次极小的 chat/completions 请求验证 地址+Key+模型 是否可用。

    成功返回一句可展示的说明；失败抛 `LlmUnavailable`，消息尽量给出可操作的原因。
    """
    base = (base_url or "").strip().rstrip("/")
    key = (api_key or "").strip()
    model = (model or "").strip()
    if not base.startswith(("http://", "https://")):
        raise LlmUnavailable("服务地址需以 http:// 或 https:// 开头")
    if not key:
        raise LlmUnavailable("请先填写 API Key")
    if not model:
        raise LlmUnavailable("请先填写模型名")
    try:
        resp = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 1},
            timeout=timeout)
    except Exception as exc:
        raise LlmUnavailable(f"无法连接 {base}：{exc}") from exc

    if resp.status_code == 200:
        return f"连接成功：{base} / {model}"
    detail = resp.text[:300].replace("\n", " ")
    if resp.status_code in (401, 403):
        raise LlmUnavailable(f"认证失败({resp.status_code})：API Key 无效或无权限。{detail}")
    if resp.status_code == 404:
        raise LlmUnavailable(f"接口不存在(404)：请确认服务地址是 OpenAI 兼容根路径，"
                             f"当前为 {base}。{detail}")
    if resp.status_code == 429:
        raise LlmUnavailable(f"触发限流(429)：Key 可用但额度/频率受限。{detail}")
    raise LlmUnavailable(f"服务返回 {resp.status_code}：{detail}")
