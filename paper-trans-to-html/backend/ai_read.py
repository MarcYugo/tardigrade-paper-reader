"""模块八 · AI 辅助阅读（文末：把笔记整理清楚 + 就这篇聊下去）

定位：一篇文章读到结尾（进度 ≥ 95%）时，把它上面的**笔记**整理成结构化摘要，
并在同一屏提供对话 —— 目标是「帮用户想清楚」，不是「替用户读书」。

只依赖 `translate.Translator`（OpenAI 兼容后端）：Google 免费兜底只会逐句翻译，
这类任务会直接抛 `TranslationUnavailable`，前端提示去「⚙ 设置 → LLM 服务」配 Key。

对外函数：

| 函数 | 作用 |
| --- | --- |
| `article_text()` | 把整篇正文（含每页公式 LaTeX）压成对话上下文 |
| `build_summary()` | 把笔记整理成 JSON 摘要（总览 / 主题 / 关联 / 疑问 / 下一步） |
| `reply_chat()` | 带着正文、笔记与摘要聊一轮，返回助手正文（整段） |
| `reply_chat_stream()` | 同上，但**逐段 yield**，给前端做流式显示 |

对话的上下文（`chat_payload()`）**从一开始就带整篇正文**：`article_text()` 会把
每页正文与**该页公式的 LaTeX**（整页 OCR 路线的 `kind="formula"` 块；旧 doc.json 里
还有 `pages[].formula_boxes`）一起拼进去 —— 不然模型只看得见笔记，答不了
「这篇论文讲了什么 / 这个公式是什么」。


`build_summary()` 除了笔记，还能带上**上一版整理**（`previous`，可能被用户手改过）与
**已发生的对话**（`chat`）—— 这就是「重新整理」把讨论成果并进来的方式。
`clean_summary(loose=True)` 给手工编辑用：上限放宽，不会把用户写的内容默默截掉。

`reply_chat_stream()` 与 `reply_chat()` 走同一个提示词（`chat_payload()` 共用），
只是把 `Translator.chat()` 换成 `Translator.chat_stream()`。

摘要为什么是 **JSON** 而不是 Markdown：结构化数据可以直接渲染成卡片；而且模型
偶尔字段缺失/类型不对，`clean_summary()` 能逐字段兜住，不会把页面弄花。
（**对话正文**是 Markdown：前端 `r2-md.js` 自带一个小渲染器负责排版，不引第三方
库；整理结果里的自由文本条目也过它 —— 所以字段内容里的 `**加粗**`、行内代码等
行内写法同样能显示成样式。）

笔记上下文 `[{sent, note, page}]` 由前端提供 —— 只有前端知道「这条笔记挂在哪句话上」
（句子切分在 `reader2.js` 里做），后端负责收敛长度并拼成提示词。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable, Iterator

from .translate import Translator

MAX_NOTES = 200            # 送进模型的笔记条数上限
MAX_SENT_CHARS = 600       # 单条「原句」截断
MAX_NOTE_CHARS = 1500      # 单条笔记截断
MAX_CONTEXT_CHARS = 12000  # 拼给模型的笔记上下文总长上限
MAX_ARTICLE_CHARS = 60000  # 对话上下文里的整篇正文（含公式 LaTeX）总长上限，按页截断
MAX_HISTORY = 20           # 聊天回灌的历史条数（不含本次提问）
MAX_REPLY_CHARS = 6000     # 助手回复保留长度上限（防模型跑飞）
MAX_CHAT_TURNS = 24        # 「重新整理」时把对话当素材：最多带几轮
MAX_CHAT_CHARS = 8000      # 「重新整理」时对话素材的总长上限

# 手工编辑整理结果时的上限（比模型输出宽松，避免把用户写的东西默默截掉）
EDIT_THEMES = 12
EDIT_POINTS = 20
EDIT_ITEMS = 20
EDIT_TITLE_CHARS = 200
EDIT_ITEM_CHARS = 1200
EDIT_OVERVIEW_CHARS = 4000

_SUMMARY_SYSTEM = (
    "你是学术阅读助手。用户读一篇论文时做了若干条笔记，你的任务是**把他的笔记整理清楚**，"
    "让他看清自己记了什么、这些点之间有什么关系、还有什么没想清楚。\n"
    "规则：\n"
    "1. 只依据用户给的「笔记 + 它挂在哪句话上」，**不要引入论文之外的资料**，也不要编造论文内容。\n"
    "2. 用**中文**输出；关键术语可以保留英文原词。\n"
    "3. 忠实于笔记：不要替用户下他还没下的结论；笔记之间若互相矛盾，指出来，而不是抹平。\n"
    "4. `overview`：3~5 句，概括**这些笔记整体在关心什么**（不是概括整篇论文）。\n"
    "5. `themes`：把笔记按主题分成 1~5 组，每组一个短标题 + 若干条要点；"
    "每条要点尽量带上页码（如「P3」）。\n"
    "6. `connections`：笔记之间的关联 / 递进 / 冲突（没有就给空数组）。\n"
    "7. `questions`：从笔记里能看出、但还没被回答的问题（目的是帮他想下去，不是给答案）。\n"
    "8. `next`：接下来可以做的事（回去读哪一节、验证什么、补什么笔记）。\n"
    "9. 没有内容的字段返回空数组，**不要**写「暂无」「无」之类的占位符。\n"
    "10. 如果给了「上一版整理」：以它为基础**增量更新** —— 我自己改过、又没被"
    "笔记或对话推翻的内容要原样保留，别因为换了措辞就把内容丢掉。\n"
    "11. 如果给了「我们之后的对话」：把我已经聊清楚/确认过的结论并进 `themes` 与 `next`，"
    "把新冒出来的疑问并进 `questions`。\n"
    "12. 不要在结果里出现「上一版」「对话中提到」「用户改成」这类元话术，"
    "直接写成结论本身。\n"
    "只输出 JSON：{\"overview\": \"...\", \"themes\": [{\"title\": \"...\", \"points\": [\"...\"]}],"
    " \"connections\": [\"...\"], \"questions\": [\"...\"], \"next\": [\"...\"]}"
)

_CHAT_SYSTEM = (
    "你是学术阅读助手，正在和用户讨论他正在读的一篇论文。下面会给出这篇论文的标题、"
    "**完整正文**（若可读取；解析时公式在正文里被擦成了等长空格，因此每页末尾附有"
    "该页公式的 LaTeX 列表，正文里的空位要对照它来理解）、他做的笔记、以及此前生成的"
    "笔记整理。\n"
    "原则：\n"
    "1. 以**帮他想清楚**为目标：先给结构和思路，再给结论；需要时反问一句帮助他往下想，"
    "但一次最多问一个问题。\n"
    "2. 依据下面提供的论文正文与笔记回答；正文/笔记里没有的就说「论文里没有提到」，"
    "并指出还需要看哪一部分，**不要编造论文内容**。\n"
    "3. 用中文回答，简洁（一般 150~400 字）；必要时用小标题或短列表，不要空话、不要复述问题。\n"
    "4. 引用论文 / 笔记时带上页码（如 P4）。"
)

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
                             "description": "要写进卡片的内容（一句话或一条要点，可含 Markdown）"},
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


def clean_notes(raw) -> list[dict]:
    """收敛前端传来的笔记上下文：条数、单条长度、总长度都设上限。"""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    used = 0
    for r in raw[:MAX_NOTES]:
        if not isinstance(r, dict):
            continue
        note = " ".join(str(r.get("note") or "").split())[:MAX_NOTE_CHARS]
        if not note:
            continue                                  # 空笔记不占位置
        sent = " ".join(str(r.get("sent") or "").split())[:MAX_SENT_CHARS]
        try:
            page = int(r.get("page") or 0)
        except Exception:
            page = 0
        note_id = str(r.get("id") or "")[:100]
        title = " ".join(str(r.get("title") or "").split())[:200]
        size = len(sent) + len(note) + len(note_id) + len(title) + 24
        if used + size > MAX_CONTEXT_CHARS:
            break
        row = {"sent": sent, "note": note, "page": max(0, page)}
        if note_id:
            row["id"] = note_id
        if title:
            row["title"] = title
        out.append(row)
        used += size
    return out


def notes_text(notes: list[dict]) -> str:
    """把笔记拼成给模型看的文本：一条一段，带页码与它挂在哪句话上。"""
    if not notes:
        return "（还没有笔记）"
    blocks: list[str] = []
    for i, n in enumerate(notes, 1):
        lines = [f"{i}. " + (f"【ID: {n['id']}】" if n.get("id") else "")
                 + (f"【第 {n['page']} 页】" if n.get("page") else "")]
        if n.get("title"):
            lines.append(f"   标题：{n['title']}")
        if n.get("sent"):
            lines.append(f"   原文句：{n['sent']}")
        lines.append(f"   我的笔记：{n['note']}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def notes_fingerprint(rows) -> str:
    """笔记指纹：包含句子关联、独立笔记标题与内容，与时间无关。

    用它判断已生成的整理是否过期 —— 比时间戳可靠：同一秒内改笔记也看得出来，
    换台机器/时区不一致也不会误判。`rows` 是存储层的笔记（含 `block_id` 与 `text`）。
    """
    parts = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        block_id = str(row.get("block_id") or "")
        if block_id:
            parts.append(f"{block_id}\x01{text}")
        else:
            parts.append(f"\x03{str(row.get('id') or '')}\x01"
                         f"{str(row.get('title') or '').strip()}\x01{text}")
    parts.sort()
    return hashlib.sha1("\x02".join(parts).encode("utf-8")).hexdigest()[:16]


def _str_list(value, limit: int, item_chars: int) -> list[str]:
    """把「可能是字符串、可能是数组」的字段收敛成干净的字符串列表。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        s = " ".join(str(v or "").split())
        if s:
            out.append(s[:item_chars])
        if len(out) >= limit:
            break
    return out


def clean_summary(data, *, loose: bool = False) -> dict:
    """把（模型返回或用户手改的）JSON 收敛成固定结构。

    `loose=True` 给**手工编辑**用：条数与字数上限放宽，免得把用户写的内容默默截掉；
    模型输出用默认上限，防它跑飞。字段缺失/类型不对在这两种情况下都能兜住。
    """
    max_themes = EDIT_THEMES if loose else 5
    max_points = EDIT_POINTS if loose else 8
    max_items = EDIT_ITEMS if loose else 8
    max_title = EDIT_TITLE_CHARS if loose else 80
    item_chars = EDIT_ITEM_CHARS if loose else 300
    over_chars = EDIT_OVERVIEW_CHARS if loose else 1500
    data = data if isinstance(data, dict) else {}
    themes: list[dict] = []
    raw_themes = data.get("themes")
    if isinstance(raw_themes, dict):                 # 有的模型会包一层 {1: {...}}
        raw_themes = list(raw_themes.values())
    for t in (raw_themes or [])[:max_themes] if isinstance(raw_themes, list) else []:
        if isinstance(t, dict):
            title = " ".join(str(t.get("title") or "").split())[:max_title]
            points = _str_list(t.get("points"), max_points, item_chars)
        else:
            title = " ".join(str(t or "").split())[:max_title]
            points = []
        if title or points:
            themes.append({"title": title or "要点", "points": points})
    return {
        "overview": " ".join(str(data.get("overview") or "").split())[:over_chars],
        "themes": themes,
        "connections": _str_list(data.get("connections"), max_items, item_chars),
        "questions": _str_list(data.get("questions"), max_items, item_chars),
        "next": _str_list(data.get("next"), max_items, item_chars),
    }


def summary_empty(summary: dict | None) -> bool:
    """摘要里一条可用内容都没有（模型偶尔会返回空壳）。"""
    if not isinstance(summary, dict):
        return True
    if (summary.get("overview") or "").strip():
        return False
    return not any(summary.get(k) for k in ("themes", "connections", "questions", "next"))


def summary_text(summary: dict | None) -> str:
    """把摘要压成文本，给对话当上下文（也用于回灌给模型）。"""
    if not isinstance(summary, dict):
        return ""
    lines: list[str] = []
    if summary.get("overview"):
        lines.append("【总览】" + summary["overview"])
    for t in summary.get("themes") or []:
        lines.append("【主题】" + (t.get("title") or ""))
        lines.extend("  - " + p for p in (t.get("points") or []))
    for key, label in (("connections", "关联"), ("questions", "待想清楚"),
                       ("next", "下一步")):
        rows = summary.get(key) or []
        if rows:
            lines.append(f"【{label}】")
            lines.extend("  - " + r for r in rows)
    return "\n".join(lines)


def history_text(chat) -> str:
    """把已发生的对话压成给模型看的文本（「重新整理」时当素材用）。

    只保留 `role` + `content`，并按轮数与总长度收敛 —— 整理任务不需要逐字精确。
    """
    rows: list[str] = []
    used = 0
    for m in (chat or [])[-MAX_CHAT_TURNS:]:
        if not isinstance(m, dict):
            continue
        content = " ".join(str(m.get("content") or "").split())
        if not content:
            continue
        line = ("我：" if m.get("role") == "user" else "AI：") + content[:MAX_NOTE_CHARS]
        if used + len(line) > MAX_CHAT_CHARS:
            break
        rows.append(line)
        used += len(line)
    return "\n".join(rows)


def _formula_row(latex: str, class_name: str, pos) -> dict:
    """一条公式记录：`{latex, class_name, pos}`，`pos` 是归一化的 `(cy, cx)`。"""
    return {"latex": latex, "class_name": class_name, "pos": pos}


def _page_formulas(page: dict) -> list[dict]:
    r"""页面里的公式（LaTeX + 位置），两个来源合并：

      * `pages[].texts[]` 里 `kind="formula"` 且带 `latex` 的块 —— **当前流程**
        （Surya 2 / Unlimited-OCR 整页 OCR 下发的独立公式块），位置由块框 x/y/w/h 折算；
      * `pages[].formula_boxes` —— 本地 PyMuPDF 公式覆盖框
        字段（该链路已移除），旧 doc.json 里仍可能存在；识别失败（`error`）或没有
        LaTeX 的条目跳过。`bbox_norm = [cx, cy, w, h]`。
    """
    rows: list[dict] = []
    for fb in page.get("formula_boxes") or []:
        if not isinstance(fb, dict) or fb.get("error"):
            continue
        latex = str(fb.get("latex") or "").strip()
        if not latex:
            continue
        pos = None
        bbox = fb.get("bbox_norm")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:                           # YOLO 口径：cx, cy 在前
                pos = (float(bbox[1]), float(bbox[0]))
            except (TypeError, ValueError):
                pos = None
        rows.append(_formula_row(latex, str(fb.get("class_name") or ""), pos))
    try:
        pw = float(page.get("w") or 0.0)
        ph = float(page.get("h") or 0.0)
    except (TypeError, ValueError):
        pw = ph = 0.0
    for blk in page.get("texts") or []:
        if not isinstance(blk, dict) or blk.get("kind") != "formula":
            continue
        latex = str(blk.get("latex") or "").strip()
        if not latex:
            continue
        pos = None
        if pw > 0 and ph > 0:
            try:
                pos = ((float(blk.get("y") or 0.0) + float(blk.get("h") or 0.0) / 2) / ph,
                       (float(blk.get("x") or 0.0) + float(blk.get("w") or 0.0) / 2) / pw)
            except (TypeError, ValueError):
                pos = None
        rows.append(_formula_row(latex, "formula_block", pos))
    return rows


def _ordered_formulas(rows: list[dict]) -> list[dict]:
    """公式按阅读顺序排（先上下、再左右），同一位置的重复项去重。

    服务组偶尔把一条公式同时标成 `DisplayedFormulaLine` 与 `InlineFormula`（实测有，
    两条 LaTeX 相同、位置几乎重合），只保留先出现的那条。
    """
    ordered = sorted(rows, key=lambda r: (r["pos"] is None, r["pos"] or (0.0, 0.0)))
    out: list[dict] = []
    for row in ordered:
        if row["pos"]:
            dup = any(o["pos"] and o["latex"] == row["latex"]
                      and abs(o["pos"][0] - row["pos"][0]) < 0.01
                      and abs(o["pos"][1] - row["pos"][1]) < 0.01 for o in out)
            if dup:
                continue
        out.append(row)
    return out


def article_text(doc: dict, *, max_chars: int = MAX_ARTICLE_CHARS) -> str:
    """把整篇正文压成给对话模型看的文本：**每页正文 + 该页公式的 LaTeX**。

    为什么公式要单独列在页尾：解析时公式字形被擦成等长空格（见 `pdf_parser` 包文档），
    正文里那些空位看不出公式内容；把该页公式按阅读顺序列出来，模型就能把两者对上。

    `doc` 是存储层的版式文档（`store.read_doc()` 的返回值），这里只读 `pages`；
    没有可读内容时返回空串，调用方据此省略整个正文段。超过 `max_chars` 时**按页
    截断**（不会截半页就断供），并附一句说明页码总数的提示。
    """
    pages = (doc or {}).get("pages") or []
    chunks: list[str] = []
    used = 0
    kept = 0
    for pi, pg in enumerate(pages, start=1):
        if not isinstance(pg, dict):
            continue
        lines: list[str] = []
        for blk in pg.get("texts") or []:
            if not isinstance(blk, dict):
                continue
            if blk.get("kind") == "formula" and str(blk.get("latex") or "").strip():
                continue                   # 旧数据的公式块：内容并入页尾公式列表
            t = str(blk.get("text") or "").strip()
            if t:
                lines.append(t)
        formulas = _ordered_formulas(_page_formulas(pg))
        if not lines and not formulas:
            continue
        block = [f"【第 {pi} 页】", *lines]
        if formulas:
            block.append("〔本页公式 LaTeX（按阅读顺序）〕")
            block.extend(
                f"{i}. " + ("（行内）" if "inline" in f["class_name"].lower() else "")
                + "$" + f["latex"] + "$"
                for i, f in enumerate(formulas, 1))
        text = "\n".join(block)
        size = len(text) + 2
        if used + size > max_chars:
            if kept == 0:                  # 第一页就超长：至少给出截断后的开头
                chunks.append(text[:max_chars] + "…")
                chunks.append(f"…（本页起内容已截断；全文共 {len(pages)} 页）")
            else:
                chunks.append(f"…（全文共 {len(pages)} 页，超出输入上限；"
                              f"第 {pi} 页起已省略）")
            break
        chunks.append(text)
        used += size
        kept += 1
    return "\n\n".join(chunks)


def build_summary(translator: Translator, *, title: str, notes: list[dict],
                  pages: int = 0, progress: float = 0.0,
                  previous: dict | None = None, chat=None) -> dict:
    """把笔记整理成结构化摘要。LLM 不可用时抛 `TranslationUnavailable`。

    `previous` 是上一版整理（**可能被用户手改过**），`chat` 是已发生的对话：
    两者都作为素材一起喂进去 —— 所以「重新整理」是把讨论成果并进来，而不是推倒重来。
    """
    if not notes:
        raise TranslationUnavailable("还没有笔记可以整理：先在正文里选中句子写几条笔记")
    head = [f"论文标题：{title or '（未知）'}"]
    if pages:
        pct = round(max(0.0, min(1.0, float(progress or 0))) * 100)
        head.append(f"论文共 {pages} 页，我读到 {pct}%")
    parts = ["\n".join(head) + f"\n\n我的笔记（共 {len(notes)} 条）：\n" + notes_text(notes)]
    prev = summary_text(previous)
    if prev:
        parts.append("===== 上一版整理（可能被我改过） =====\n" + prev)
    convo = history_text(chat)
    if convo:
        parts.append("===== 我们之后的对话 =====\n" + convo)
    parts.append("请按要求输出这一版的 JSON。")
    return clean_summary(translator.chat_json(_SUMMARY_SYSTEM, "\n\n".join(parts),
                                              temperature=0.2))


def reply_chat(translator: Translator, *, title: str, notes: list[dict],
               summary: dict | None, history: list[dict], message: str,
               article: str = "") -> str:
    """带着这篇的正文、笔记与摘要回答一轮，返回助手正文。

    `article` 是整篇正文（含每页公式 LaTeX，`article_text()` 生成）；不传也照常能聊，
    只是模型看不到全文。
    """
    system, msgs = chat_payload(title=title, notes=notes, summary=summary,
                                history=history, message=message, article=article)
    reply = (translator.chat(system, msgs, temperature=0.5) or "").strip()
    if not reply:
        raise TranslationUnavailable("模型没有返回内容，请重试")
    return reply[:MAX_REPLY_CHARS]


def reply_chat_stream(translator: Translator, *, title: str, notes: list[dict],
                      summary: dict | None, history: list[dict], message: str,
                      article: str = "") -> Iterator[str]:
    """流式版 `reply_chat()`：返回一个**逐段 yield 正文增量**的生成器。

    与 `reply_chat()` 一样，参数不对/没配 LLM 会在**调用时**立即抛
    `TranslationUnavailable`（方便接口层先回 503）；真正的网络请求等开始迭代才发。
    总长仍然招在 `MAX_REPLY_CHARS` 以内 —— 超了就停，免得模型跑飞。
    """
    system, msgs = chat_payload(title=title, notes=notes, summary=summary,
                                history=history, message=message, article=article)
    return _cap_chunks(translator.chat_stream(system, msgs, temperature=0.5),
                       MAX_REPLY_CHARS)


def reply_agent_stream(translator: Translator, *, title: str, notes: list[dict],
                       summary: dict | None, history: list[dict], message: str,
                       article: str = "", execute_tool=None,
                       pending_navigation: dict | None = None,
                       navigation_answer: str | None = None) -> Iterator[dict]:
    """带两个受限工具的 Agent 对话，事件是正文增量、工具状态与页面动作。"""
    system, msgs = chat_payload(title=title, notes=notes, summary=summary,
                                history=history, message=message, article=article)
    system += _AGENT_SYSTEM
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
    pending_confirmation = None
    used_chars = 0
    tool_count = 0
    for _ in range(4):
        response = None
        response_text = []
        for event in translator.chat_completion_stream(
                system, msgs, tools=AGENT_TOOLS, temperature=0.5):
            if event.get("type") == "delta":
                piece = str(event.get("text") or "")[:max(0, MAX_REPLY_CHARS - used_chars)]
                if piece:
                    used_chars += len(piece)
                    response_text.append(piece)
                    yield {"type": "delta", "text": piece}
            elif event.get("type") == "completion":
                response = event.get("message")
        if not isinstance(response, dict):
            raise TranslationUnavailable("模型没有返回有效的对话结果")
        calls = response.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            if pending_confirmation:
                yield {"type": "confirm_navigation", "action": pending_confirmation}
            return
        if len(calls) > 4:
            raise TranslationUnavailable("模型返回的工具调用数超出限制")
        msgs.append({"role": "assistant", "content": "".join(response_text) or None,
                     "tool_calls": calls})
        for call in calls:
            if not isinstance(call, dict):
                raise TranslationUnavailable("模型返回了无效的工具调用")
            call_id = str(call.get("id") or "")
            fn = call.get("function") if isinstance(call, dict) else {}
            name = fn.get("name") if isinstance(fn, dict) else ""
            if not call_id:
                raise TranslationUnavailable("模型返回了缺少 ID 的工具调用")
            if not isinstance(name, str) or name not in {
                    "add_note_to_summary", "navigate_to_content"}:
                result = {"ok": False, "message": "不支持的工具"}
            elif tool_count >= 4:
                result = {"ok": False, "message": "本轮工具调用次数已达上限"}
            else:
                tool_count += 1
                yield {"type": "tool", "name": name, "status": "running"}
                try:
                    raw_args = fn.get("arguments") or "{}"
                    args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
                    if not isinstance(args, dict):
                        raise ValueError("工具参数必须是对象")
                    result = execute_tool(name, args) if execute_tool else {
                        "ok": False, "message": "工具不可用"}
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    result = {"ok": False, "message": f"工具参数无效：{exc}"}
                if not isinstance(result, dict):
                    raise TranslationUnavailable("工具执行返回了无效结果")
                action = result.get("action")
                if isinstance(action, dict):
                    yield {"type": "action", "action": action}
                confirmation = result.get("confirmation")
                if isinstance(confirmation, dict):
                    pending_confirmation = confirmation
                yield {"type": "tool", "name": name,
                       "status": "done" if result.get("ok") else "error",
                       "message": str(result.get("message") or "")[:500]}
            msgs.append({"role": "tool", "tool_call_id": call_id,
                         "content": json.dumps(result, ensure_ascii=False)})

    # 让模型在调用次数耗尽后仍能给用户自然语言总结，而不是暴露工具协议。
    for event in translator.chat_completion_stream(system, msgs, temperature=0.5):
        if event.get("type") != "delta":
            continue
        piece = str(event.get("text") or "")[:max(0, MAX_REPLY_CHARS - used_chars)]
        if piece:
            used_chars += len(piece)
            yield {"type": "delta", "text": piece}


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



def _cap_chunks(chunks: Iterable[str], limit: int) -> Iterator[str]:
    """把增量流的总长度招到 `limit`：到顶就结束，不会多吐一个字。"""
    used = 0
    for c in chunks:
        text = c or ""
        if not text:
            continue
        room = limit - used
        if room <= 0:
            return
        text = text[:room]
        used += len(text)
        yield text


def chat_payload(*, title: str, notes: list[dict], summary: dict | None,
                 history: list[dict], message: str,
                 article: str = "") -> tuple[str, list[dict]]:
    """拼出对话要发给模型的 `(system, messages)`。两个对话入口共用。

    `article`（`article_text()` 生成的整篇正文 + 每页公式 LaTeX）放在**历史最前**
    （标题之后）—— 模型从第一轮起就带着全文语境，而不是只看到笔记。
    """
    message = (message or "").strip()
    if not message:
        raise TranslationUnavailable("请输入内容")
    ctx = [f"===== 论文标题 =====\n{title or '（未知）'}"]
    article = (article or "").strip()
    if article:
        ctx.append("===== 论文正文（公式位置在正文里是空格；每页末尾附有该页公式的"
                   " LaTeX 列表）=====\n" + article)
    else:
        ctx.append("（本次没拿到论文正文，只能依据笔记与整理回答；"
                   "涉及正文内容时请说明这一点）")
    ctx.extend([
        f"===== 我的笔记（共 {len(notes)} 条）=====\n{notes_text(notes)}",
        "===== 已生成的笔记整理 =====\n" + (summary_text(summary) or "（还没生成整理）"),
    ])
    msgs: list[dict] = []
    for m in (history or [])[-MAX_HISTORY:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": message})
    return _CHAT_SYSTEM + "\n\n" + "\n\n".join(ctx), msgs
