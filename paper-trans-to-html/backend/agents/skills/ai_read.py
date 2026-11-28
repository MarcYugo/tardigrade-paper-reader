"""技能 · 辅助阅读（带受限工具的论文对话）

从原 `agents/ai_read_agent.py` 迁来，现在是挂在统一 Agent 运行时
（`agents/agent.py`）上的一个技能：**只声明**「系统提示词 + 两个工具 + 两个工具
的后端实现」，工具循环交给 `run_tool_loop()`，前端事件契约保持不变。

普通对话（`reply_chat` / `reply_chat_stream`）、笔记整理与上下文拼装仍在
`backend/ai_read.py`；两边共用 `chat_payload()` / `clean_summary()` 等。

对外接口：

| 函数 | 作用 |
| --- | --- |
| `SKILL` | 技能声明（`name="ai_read"`，提示词 + 两个工具） |
| `reply_agent_stream()` | 带工具的流式对话：yield 正文增量、工具状态与页面动作 |
| `add_summary_item()` | 执行 `add_note_to_summary`：把内容并进右侧「笔记整理」卡片 |
| `locate_page()` | 按正文与公式 LaTeX 定位最相关的页面（从 1 开始） |
| `locate_text_block()` | 在指定页内定位相关文本块 ID（供前端点亮） |

工具本身由调用方注入（`execute_tool`）：HTTP 层（`backend/server/api/ai.py`）负责权限、落库与
「先问用户是否跳转、确认后才执行」的状态机，本技能只做提示词与工具后端。
"""
from __future__ import annotations

import re
from typing import Iterator

from ...ai_read import (EDIT_ITEM_CHARS, EDIT_TITLE_CHARS, MAX_REPLY_CHARS,
                        chat_payload, clean_summary, summary_text)
from ...translate import Translator
from ..agent import Skill, run_tool_loop, stream_completion

# 对话循环的上限：最多 4 轮、总共 4 次工具调用、单轮最多 4 个并行调用
MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 4
MAX_TOOL_PARALLEL = 4


_AGENT_SYSTEM = (
    "\n\n你也是论文阅读 Agent，可以调用两个工具：add_note_to_summary 用来把用户要求的内容"
    "**写进右侧的「笔记整理」卡片**（不是正文旁的笔记）；navigate_to_content 用来定位论文页、"
    "指定笔记或打开笔记栏。"
    "只有用户明确要求记录、整理成笔记时才写卡片；不要把普通问答自动写进去。"
    "写卡片时用 section 选分区：overview（总览）/ themes（主题梳理）/ connections（关联）/ "
    "questions（待想清楚）/ next（下一步）；写 themes 要给 title —— 与已有主题同名就追加一条要点，"
    "否则新建一组。一次只写用户要的那点内容，保持和卡片里已有内容一致的措辞与页码标注。"
    "卡片还没生成时也可以写，会在对应分区里先建起来。"
    "不要说「已新建笔记」或「已改笔记栏」——你写的是整理卡片。"
    "**公式一律写成 LaTeX**：行内用 `$…$`（如 `$\\mathrm{MultiHead}(Q,K,V)$`、`$W_i^Q \\in \\mathbb{R}^{d_{model}\\times d_k}$`），"
    "独立成行用 `$$…$$`；**不要**用反引号代码、纯文本或 `\\(…\\)` 写公式 —— 前端只把 `$…$` 渲染成公式。"
    "变量、维度、下标、运算符（如 `$d_k$`、`$h=8$`、`$\\sqrt{d_k}$`）也按公式写。"
    "需要定位内容时调用导航工具确认目标；首次调用会先询问用户，不会立即跳转。"
    "定位论文时，尽量在 query 中提供论文里的相关短语，便于阅读器点亮对应文本框。"
    "用户明确确认后才能再次调用工具执行跳转；不要声称未执行的操作已完成。"
    "笔记 ID 会在笔记上下文中提供，定位笔记时使用对应 ID。"
)

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_note_to_summary",
            "description": (
                "把内容写进右侧的「笔记整理」卡片：按 section 追加到总览 / 主题梳理 / "
                "关联 / 待想清楚 / 下一步，不新建笔记。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string",
                                "enum": ["overview", "themes", "connections",
                                         "questions", "next"],
                                "description": "写到卡片的哪个分区"},
                    "text": {"type": "string", "maxLength": 1200,
                             "description": "要写进卡片的内容（一句话或一条要点，可含 Markdown；"
                                            "公式用 LaTeX $…$（行内）/ $$…$$（独立成行），不要用反引号代码）"},
                    "title": {"type": "string", "maxLength": 200,
                              "description": "section=themes 时的主题标题；同名主题会追加要点"},
                },
                "required": ["section", "text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate_to_content",
            "description": "将阅读器定位到论文页、某条笔记，或打开右侧笔记栏。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["paper", "note", "notes_panel"]},
                    "page": {"type": "integer", "minimum": 1},
                    "query": {"type": "string",
                              "description": "论文中相关的原文短语或主题；定位后用于点亮对应文本框"},
                    "note_id": {"type": "string", "description": "笔记上下文中的笔记 ID"},
                },
                "required": ["target"],
                "additionalProperties": False,
            },
        },
    },
]

SKILL = Skill(
    name="ai_read",
    title="辅助阅读",
    description=("论文阅读对话：把用户要求的内容写进右侧「笔记整理」卡片，"
                 "或把阅读器定位到论文页 / 某条笔记（跳转前先征求用户确认）。"),
    system_prompt=_AGENT_SYSTEM,
    tools=tuple(AGENT_TOOLS),
)


def reply_agent_stream(translator: Translator, *, title: str, notes: list[dict],
                       summary: dict | None, history: list[dict], message: str,
                       article: str = "", execute_tool=None,
                       pending_navigation: dict | None = None,
                       navigation_answer: str | None = None) -> Iterator[dict]:
    """带两个受限工具的 Agent 对话，事件是正文增量、工具状态与页面动作。

    循环本身在 `agents/agent.py::run_tool_loop`；这里只负责：
    拼上下文 + 技能提示词、把「上一轮待确认的导航」写进提示词、把通用的
    `confirmation` 事件转成前端认识的 `confirm_navigation`。
    """
    system, msgs = chat_payload(title=title, notes=notes, summary=summary,
                                history=history, message=message, article=article)
    system += SKILL.system_prompt
    if pending_navigation:
        target = pending_navigation.get("target")
        destination = (f"论文第 {pending_navigation.get('page')} 页"
                       if target == "paper" else "指定笔记")
        system += (
            f"\n\n上一轮你询问是否定位到{destination}。用户当前回答是：{message!r}。"
            + (f"用户通过确认按钮明确选择：{navigation_answer}。"
               if navigation_answer else "")
            + "只有用户明确同意时才调用 navigate_to_content，并且只能使用上一轮确认的同一目标；"
            "如果用户拒绝，就不要调用导航工具；如果回答不明确，就继续询问，不要跳转。"
        )
    for event in run_tool_loop(
            complete=stream_completion(translator), system=system, messages=msgs,
            skill=SKILL, execute_tool=execute_tool, max_rounds=MAX_TOOL_ROUNDS,
            max_calls=MAX_TOOL_CALLS, max_per_round=MAX_TOOL_PARALLEL,
            max_chars=MAX_REPLY_CHARS, temperature=0.5, final_reply=True):
        if event.get("type") == "confirmation":
            # 运行时只认识通用的 confirmation；前端契约保持 confirm_navigation + action
            yield {"type": "confirm_navigation", "action": event.get("confirmation")}
        else:
            yield event


def locate_page(doc: dict, query: str) -> int | None:
    """按页面正文与公式 LaTeX 找到最相关页，返回从 1 开始的页码。"""
    text_query = (query or "").lower()
    words = set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text_query))
    if not words:
        return None
    normalized_query = re.sub(r"[\W_]+", "", text_query, flags=re.UNICODE)
    best_page, best_score = None, 0.0
    for page_no, page in enumerate((doc or {}).get("pages") or [], start=1):
        if not isinstance(page, dict):
            continue
        text = " ".join(
            str(block.get("text") or "") + " " + str(block.get("latex") or "")
            for block in (page.get("texts") or []) if isinstance(block, dict)
        ).lower()
        normalized_text = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
        if len(normalized_query) >= 4 and normalized_query in normalized_text:
            return page_no
        page_words = set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text))
        score = len(words & page_words) / len(words)
        if score > best_score:
            best_page, best_score = page_no, score
    return best_page if best_score >= 0.3 else None


def locate_text_block(doc: dict, page_no: int, query: str) -> str | None:
    """在指定页内按关键词匹配相关文本块，返回其前端 block ID。"""
    pages = (doc or {}).get("pages") or []
    if page_no < 1 or page_no > len(pages) or not isinstance(pages[page_no - 1], dict):
        return None
    text_query = (query or "").lower()
    words = set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text_query))
    if not words:
        return None
    normalized_query = re.sub(r"[\W_]+", "", text_query, flags=re.UNICODE)
    best_id, best_score = None, 0.0
    for block in pages[page_no - 1].get("texts") or []:
        if not isinstance(block, dict) or not isinstance(block.get("id"), str):
            continue
        text = (str(block.get("text") or "") + " " + str(block.get("latex") or "")).lower()
        normalized_text = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
        if len(normalized_query) >= 4 and normalized_query in normalized_text:
            return block["id"]
        block_words = set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text))
        score = len(words & block_words) / len(words)
        if score > best_score:
            best_id, best_score = block["id"], score
    return best_id if best_score >= 0.3 else None


SUMMARY_SECTIONS = {
    "overview": "总览",
    "themes": "主题梳理",
    "connections": "笔记之间的关联",
    "questions": "还没想清楚",
    "next": "下一步",
}


def add_summary_item(summary: dict | None, args: dict) -> tuple[dict, dict]:
    """执行 `add_note_to_summary` 工具：把内容并进右侧「笔记整理」卡片。

    **不新建笔记**，只改整理卡片里的分区：

    * `overview`：接在总览后面；
    * `themes`：`title` 与已有主题同名就追加一条要点，否则新建一组；
    * `connections` / `questions` / `next`：各追加一条。

    返回 `(工具结果, 新整理)`。卡片还没生成时按空卡片新建 —— 只写用户要的那一块，
    不替模型编造其它内容。结果过 `clean_summary(loose=True)` 收敛（条数/字数上限
    与手工编辑一致），写完再核一遍内容确实落地，避免超上限时被默默截掉。
    """
    section = args.get("section")
    text = args.get("text")
    title = args.get("title")
    title = "" if title is None else title
    base = clean_summary(summary, loose=True)
    if not isinstance(section, str) or section not in SUMMARY_SECTIONS:
        return {"ok": False,
                "message": "section 只能是 " + " / ".join(SUMMARY_SECTIONS)}, base
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "message": "写入内容不能为空"}, base
    if not isinstance(title, str):
        return {"ok": False, "message": "主题标题格式无效"}, base
    text = " ".join(text.split())[:EDIT_ITEM_CHARS]
    title = " ".join(title.split())[:EDIT_TITLE_CHARS]

    if section == "overview":
        base["overview"] = (base["overview"] + " " + text).strip()
    elif section == "themes":
        theme = next((t for t in base["themes"] if title and t["title"] == title), None)
        if theme is None:
            theme = {"title": title or "要点", "points": []}
            base["themes"].append(theme)
        theme["points"].append(text)
    else:
        base[section] = list(base.get(section) or []) + [text]

    merged = clean_summary(base, loose=True)
    if text not in " ".join(summary_text(merged).split()):
        where = SUMMARY_SECTIONS[section]
        message = ("「总览」太长了，先精简原文再写" if section == "overview"
                   else f"「{where}」条目已满，先删掉几条旧的再写")
        return {"ok": False, "message": message}, clean_summary(summary, loose=True)
    return {"ok": True, "message": f"已写入右侧整理卡片的「{SUMMARY_SECTIONS[section]}」",
            "section": section}, merged


__all__ = ["SKILL", "AGENT_TOOLS", "reply_agent_stream", "add_summary_item",
           "locate_page", "locate_text_block", "SUMMARY_SECTIONS"]
