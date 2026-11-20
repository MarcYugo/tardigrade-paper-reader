"""统一 Agent 运行时：技能声明 + 工具调用循环 + 历史卫生。

`agents/` 目录里只有**一套** Agent 逻辑（本文件）；两个业务能力以「技能」的形式挂在
它下面（`agents/skills/`）：

* `skills/ai_read.py` —— 辅助阅读技能：流式对话 + 写整理卡片 + 定位论文页 / 笔记；
* `skills/note_out.py` —— 笔记导出技能：只读工具（核验参考文献 / 找代码仓库）+
  结构化分析报告。

技能只声明「系统提示词 + 工具 + 工具执行器 + 怎么收尾」，循环本身（轮数 / 调用次数
上限、`tool_calls` 与 `tool` 消息成对、正文截断、页面动作与待确认事件）只在这里实现
一次 —— 免得两套 Agent 各写一遍循环、各自踩不同的坑。

对外接口：

| 名字 | 作用 |
| --- | --- |
| `Skill` | 技能声明：`name / title / description / system_prompt / tools` |
| `run_tool_loop()` | 统一工具循环，yield `delta` / `tool` / `action` / `confirmation` |
| `stream_completion()` | 把流式 `chat_completion_stream` 适配成循环要的事件流 |
| `blocking_completion()` | 把阻塞 `chat_completion` 适配成循环要的事件流 |
| `sanitize_tool_calls()` | 过滤无法回应的 `tool_call`（非对象 / 无 ID / 重号） |
| `balanced_history()` | 修复 `tool_calls` ↔ `tool` 消息成对（上游 400 的兜底） |

事件契约（`run_tool_loop` 的产出，调用方按需转发）：

| 事件 | 含义 |
| --- | --- |
| `{"type":"delta","text":…}` | 助手正文增量 |
| `{"type":"tool","name":…,"status":"running"｜"done"｜"error","message":…}` | 工具状态 |
| `{"type":"action","action":{…}}` | 工具结果里带的前端动作（写卡片 / 跳转…） |
| `{"type":"confirmation","confirmation":{…}}` | 结束时仍待用户确认的动作（技能自行决定怎么发） |
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .llm import LlmUnavailable


# =====================================================================
#  技能声明
# =====================================================================
@dataclass(frozen=True)
class Skill:
    """一个技能：系统提示词 + 工具声明。

    技能本身**不含循环**：循环在 `run_tool_loop()` 里，工具执行器由调用方注入
    （HTTP 层要管权限 / 落库，纯数据层要管状态，两边各管各的）。

    `tools` 用 OpenAI `tools` 参数的原样结构（`[{"type":"function","function":{…}}]`），
    工具名从声明里自动解析，循环据此拒绝技能没声明的工具调用。
    """

    name: str
    title: str
    description: str
    system_prompt: str = ""
    tools: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def tool_names(self) -> frozenset[str]:
        """本技能声明了哪些工具（用于校验模型返回的 `tool_call`）。"""
        names = set()
        for spec in self.tools:
            if not isinstance(spec, dict):
                continue
            fn = spec.get("function")
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str) and name:
                names.add(name)
        return frozenset(names)

    def tool_specs(self) -> list[dict]:
        """给 LLM 的 `tools` 参数（复制一份，避免调用方改到技能声明本身）。"""
        return [dict(spec) for spec in self.tools if isinstance(spec, dict)]


# =====================================================================
#  历史卫生：tool_calls ↔ tool 消息必须严格成对
# =====================================================================
def sanitize_tool_calls(calls: Any) -> list[dict]:
    """过滤掉「无法回应」的工具调用：非对象、没有 ID、同一轮里重号。

    OpenAI 兼容后端要求 assistant 消息里的**每一个** `tool_call_id` 都紧跟一条
    对应的 `tool` 消息；留着一条回应不了的调用，下一轮整包请求就 400
    （`insufficient tool messages following tool_calls message`）。
    """
    if not isinstance(calls, (list, tuple)):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for call in calls:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "").strip()
        if not call_id or call_id in seen:
            continue
        if not isinstance(call.get("function"), dict):
            continue
        seen.add(call_id)
        out.append({**call, "id": call_id,
                    "type": call.get("type") or "function"})
    return out


def balanced_history(msgs: list[dict]) -> list[dict]:
    """兜底修复工具消息历史：`tool_calls` 与 `tool` 消息必须严格成对。

    * 没有 tool 消息回应的 tool_call 直接丢掉（若整条 assistant 因此变空、
      但还有正文，就退化成一条普通 assistant 消息）；
    * 找不到主人的 tool 消息丢掉。
    """
    out: list[dict] = []
    i, total = 0, len(msgs or [])
    while i < total:
        m = msgs[i]
        if not isinstance(m, dict):
            i += 1
            continue
        role = m.get("role")
        if role != "assistant" or not m.get("tool_calls"):
            if role != "tool":                    # 孤儿 tool 消息丢掉
                out.append(m)
            i += 1
            continue
        calls = [c for c in m.get("tool_calls") or []
                 if isinstance(c, dict) and str(c.get("id") or "").strip()]
        answered: dict[str, dict] = {}
        j = i + 1
        while j < total and isinstance(msgs[j], dict) and msgs[j].get("role") == "tool":
            cid = str(msgs[j].get("tool_call_id") or "").strip()
            if cid and cid not in answered:
                answered[cid] = msgs[j]
            j += 1
        kept = [c for c in calls if str(c.get("id")).strip() in answered]
        if kept:
            out.append({"role": "assistant", "content": m.get("content"),
                        "tool_calls": kept})
            out.extend(answered[str(c.get("id")).strip()] for c in kept)
        elif (m.get("content") or "").strip():
            out.append({"role": "assistant", "content": m.get("content")})
        i = j
    return out


# =====================================================================
#  LLM 适配器：把两种客户端折成同一种「事件流」
# =====================================================================
Completion = Callable[[str, list[dict], list[dict], float], Iterator[dict]]


def stream_completion(translator) -> Completion:
    """流式适配器：`Translator.chat_completion_stream` → 循环要的事件流。

    原样透传 `delta` / `completion` 两种事件；校验（没配 Key / 没内容）仍在
    第一次迭代时抛出 `LlmUnavailable`，与拆分前一致。
    """
    def complete(system: str, messages: list[dict], tools: list[dict],
                 temperature: float) -> Iterator[dict]:
        yield from translator.chat_completion_stream(
            system, list(messages), tools=tools or None,
            temperature=temperature)
    return complete


def blocking_completion(llm) -> Completion:
    """阻塞适配器：`LlmClient.chat_completion` → 循环要的事件流（只有 completion）。

    每次请求前都过一遍 `balanced_history()`：这是历史上踩过的 400
    （`insufficient tool messages following tool_calls message`）的兜底。
    """
    def complete(system: str, messages: list[dict], tools: list[dict],
                 temperature: float) -> Iterator[dict]:
        message = llm.chat_completion(
            system, balanced_history(list(messages)), tools=tools or None,
            temperature=temperature)
        yield {"type": "completion", "message": message}
    return complete


# =====================================================================
#  统一工具循环
# =====================================================================
def _call_name(call: dict) -> str:
    fn = call.get("function")
    name = fn.get("name") if isinstance(fn, dict) else ""
    return name if isinstance(name, str) else ""


def _call_args(call: dict) -> dict:
    fn = call.get("function") or {}
    raw = fn.get("arguments") if isinstance(fn, dict) else "{}"
    args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    if not isinstance(args, dict):
        raise ValueError("工具参数必须是对象")
    return args


def run_tool_loop(*, complete: Completion, system: str, messages: list[dict],
                  skill: Skill, execute_tool=None, max_rounds: int = 5,
                  max_calls: int = 12, max_per_round: int = 0,
                  max_chars: int = 0, temperature: float = 0.3,
                  final_reply: bool = False) -> Iterator[dict]:
    """跑一轮（多回合）工具调用，yield 正文增量、工具状态与页面动作。

    参数：

    * `complete`：`stream_completion()` / `blocking_completion()` 产出的适配器；
    * `system` / `messages`：初始上下文（技能自己的提示词 + 业务拼好的消息）；
    * `skill`：用哪个技能（决定 `tools` 与「哪些工具名合法」）；
    * `execute_tool(name, args)`：工具执行器，返回 `{ok, message?, action?, confirmation?}`；
    * `max_rounds` / `max_calls` / `max_per_round`：轮数、总调用次数、单轮并行调用上限
      （0 表示不限制）；
    * `max_chars`：正文保留长度上限（0 表示不限制），防止模型跑飞；
    * `final_reply`：轮数用尽后是否再让模型**不带工具**说一段收尾（否则直接结束）。
    """
    msgs = list(messages or [])
    used = 0
    calls_made = 0
    pending: dict | None = None

    def take(text: Any) -> str:
        """取一段正文，按 `max_chars` 截断并累计。"""
        nonlocal used
        piece = str(text or "")
        if max_chars:
            piece = piece[:max(0, max_chars - used)]
        used += len(piece)
        return piece

    ended_clean = False
    for _ in range(max(1, int(max_rounds))):
        response = None
        parts: list[str] = []
        for event in complete(system, msgs, skill.tool_specs(), temperature):
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "delta":
                piece = take(event.get("text"))
                if piece:
                    parts.append(piece)
                    yield {"type": "delta", "text": piece}
            elif kind == "completion":
                response = event.get("message")
        if not isinstance(response, dict):
            raise LlmUnavailable("模型没有返回有效的对话结果")

        calls = sanitize_tool_calls(response.get("tool_calls"))
        if not calls:
            ended_clean = True
            break
        if max_per_round and len(calls) > int(max_per_round):
            raise LlmUnavailable("模型返回的工具调用数超出限制")

        # 关键：写回历史的 assistant 消息里有几个 tool_call，就必须补几条 tool 消息。
        msgs.append({"role": "assistant",
                     "content": "".join(parts) or None,
                     "tool_calls": calls})
        for call in calls:
            name = _call_name(call)
            if name not in skill.tool_names:
                result = {"ok": False, "message": "不支持的工具"}
            elif calls_made >= int(max_calls):
                result = {"ok": False, "message": "本轮工具调用次数已达上限"}
            else:
                calls_made += 1
                yield {"type": "tool", "name": name, "status": "running"}
                try:
                    args = _call_args(call)
                    result = (execute_tool(name, args) if execute_tool
                              else {"ok": False, "message": "工具不可用"})
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    result = {"ok": False, "message": f"工具参数无效：{exc}"}
                if not isinstance(result, dict):
                    raise LlmUnavailable("工具执行返回了无效结果")
                action = result.get("action")
                if isinstance(action, dict):
                    yield {"type": "action", "action": action}
                confirmation = result.get("confirmation")
                if isinstance(confirmation, dict):
                    pending = confirmation
                yield {"type": "tool", "name": name,
                       "status": "done" if result.get("ok") else "error",
                       "message": str(result.get("message") or "")[:500]}
            msgs.append({"role": "tool", "tool_call_id": call["id"],
                         "content": json.dumps(result, ensure_ascii=False)})
        if calls_made >= int(max_calls):
            break

    if ended_clean:
        # 待确认的动作只在这一轮真正说完之后才发给前端（否则会插在正文中间）
        if pending:
            yield {"type": "confirmation", "confirmation": pending}
        return

    if final_reply:
        # 调用次数耗尽后仍让模型给用户一段自然语言总结，而不是暴露工具协议。
        for event in complete(system, msgs, [], temperature):
            if not isinstance(event, dict) or event.get("type") != "delta":
                continue
            piece = take(event.get("text"))
            if piece:
                yield {"type": "delta", "text": piece}


__all__ = ["Skill", "run_tool_loop", "stream_completion", "blocking_completion",
           "sanitize_tool_calls", "balanced_history"]
