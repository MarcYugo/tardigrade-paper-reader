"""模块十 · 笔记导出（机械导出 / Agent 导出）

把一篇论文的**内容与笔记**导出成一份 JSON（`note.md` 功能 16）：

* 两种模式都先把论文拆成 **title / abstract / main text / references / appendix**，
  每一节都同时保留 `text`（拼好的纯文本）与 `blocks`（**原格式的段落集合**，
  逐段带 `x/y/w/h` + `bbox`，以及行内公式区间与表格 HTML）；
* **机械导出**（`mode="mechanical"`）：纯本地、零 LLM 成本，直接给分节内容、
  参考文献条目、句子笔记 / 独立笔记 / 高亮与一份统计报告；
* **Agent 导出**（`mode="agent"`）：在机械导出的基础上，交给
  `agents/note_out_agent.py` 调用只读工具（核验参考文献是否真实存在、提取并确认
  项目代码地址），再形成一份分析报告。

这里不碰数据库、也不做权限判断：`doc` / `rec` / `anno` 都由调用方（`app.py`）取好，
本模块只做**纯数据转换**。这样后面接「transfer key 让别的 Agent 拿走笔记」时，
数据对接层可以直接复用同一份导出结构。

对外接口：

| 函数 | 作用 |
| --- | --- |
| `build_sections()` | 把版式文档拆成五个部分（另带 `front_matter`） |
| `build_notes()` | 把笔记 / 高亮解析成「页码 + 原句」，并补作者 |
| `export_paper()` | 生成完整导出 JSON（机械 / Agent） |
| `to_json()` / `export_filename()` | 序列化与下载文件名（HTTP 层用） |
| `extract_code_links()` | 从文本里提取代码地址（转导出给 Agent 复用） |
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Any

from . import records
from .agents.llm import LlmClient
from .pdf_refs import build_refs

EXPORT_FORMAT = "tardigrade-note-export"
EXPORT_VERSION = 1
MODES = ("mechanical", "agent")


# =====================================================================
#  基础文本工具
# =====================================================================
def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split())


def _block_content(block: dict) -> str:
    """一个文字块的正文：优先 `text`，没有时用公式 `latex`（导出不丢内容）。"""
    text = _norm(block.get("text"))
    if text:
        return text
    latex = _norm(block.get("latex"))
    return f"${latex}$" if latex else ""


_HEADING_KINDS = {"heading", "title"}
_HEADING_LABELS = {
    "title", "heading", "heading1", "heading2", "heading3", "section",
    "section_header", "sectionheader", "paragraph_title", "doc_title",
    "sub_title", "subtitle", "abstract", "reference", "references",
}


def _is_heading_like(block: dict, text: str) -> bool:
    """块是不是标题：先看解析器给的 kind/label，再对短行做保守启发。"""
    kind = str(block.get("kind") or "").strip().lower()
    label = str(block.get("label") or "").strip().lower()
    if kind in _HEADING_KINDS or label in _HEADING_LABELS:
        return True
    if not text or len(text) > 80:
        return False
    if text.endswith((".", ",", ";", "；", "，")):
        return False
    # `1 Introduction` / `2.1 Method` / 全大写短标题 / 多数词首字母大写的短标题
    if re.match(r"^\d+(?:\.\d+)*[.\s]+\S", text):
        return True
    if text.isupper() and len(text.split()) <= 10:
        return True
    words = text.split()
    if len(words) <= 8 and text[:1].isupper():
        capitalized = sum(1 for word in words if word[:1].isupper())
        return capitalized >= max(2, len(words) // 2)
    return False


_RE_ABSTRACT = re.compile(
    r"(?i)^\s*(?:[0-9]{1,2}[.\s]*)?(abstract|summary|摘\s*要)\b"
    r"(?P<sep>\s*[:.\-—]?)\s*(?P<tail>.*)$")
_RE_REFERENCES = re.compile(
    r"(?i)^\s*(?:[0-9]{1,2}[.\s]*)?"
    r"(references|bibliography|works\s+cited|literature\s+cited|参\s*考\s*文\s*献)\b"
    r"(?P<sep>\s*[:.]?)\s*(?P<tail>.*)$")
_RE_APPENDIX = re.compile(
    r"(?i)^\s*(?:(?:[A-Z]|[0-9]{1,2})[.\s]+)?"
    r"(appendix|appendices|supplementary(?:\s+material)?|supporting\s+information|"
    r"附\s*录)\b(?P<sep>\s*[:.]?)\s*(?P<tail>.*)$")
_RE_INTRO = re.compile(
    r"(?i)^\s*(?:[0-9]{1,2}[.\s]*)?(introduction|引\s*言)\b"
    r"(?P<sep>\s*[:.]?)\s*(?P<tail>.*)$")

_SECTION_PATTERNS = (
    ("abstract", _RE_ABSTRACT),
    ("references", _RE_REFERENCES),
    ("appendix", _RE_APPENDIX),
    ("intro", _RE_INTRO),
)


def _section_of(block: dict, text: str) -> tuple[str | None, str]:
    """识别这一块是不是五个部分的**标题块**，返回 `(部分名, 标题后剩下的正文)`。

    本地解析方案的句子级块常把标题和第一条正文并成一块
    （`References [1] Smith …` / `Appendix A Proof …`），所以不能只认“整块 = 标题”；
    但也不能把以 “References to prior work …” 开头的普通句子误判成标题，
    因此对每个部分分别要求“标题样式 / 标题后没有尾巴 / 尾巴像条目”。
    """
    if not text:
        return None, ""
    heading_like = _is_heading_like(block, text)
    for name, pattern in _SECTION_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        tail = (m.group("tail") or "").strip()
        sep = (m.group("sep") or "").strip()
        if name == "abstract":
            # `Abstract` / `Abstract:` / `Abstract. We propose …` 都算；
            # 但纯正文里以 “Abstract reasoning …” 开头的长句不算。
            if heading_like or sep or not tail or len(text) <= 40:
                return name, tail
        elif name == "references":
            if (heading_like or not tail or tail[:1] in "[("
                    or tail[:1].isdigit()):
                return name, tail
        elif name == "appendix":
            if heading_like or sep or not tail or len(text) <= 90:
                return name, tail
        elif name == "intro":
            if heading_like or sep or not tail or len(text) <= 90:
                return name, tail
    return None, ""


def _same_title(a: str, b: str) -> bool:
    """判断两块文字是不是同一个标题（用于把首页的标题块从 front matter 里剔掉）。"""
    ka = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _norm(a).lower())
    kb = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _norm(b).lower())
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    return min(len(ka), len(kb)) >= 12 and (ka in kb or kb in ka)


# 段落块里保留下来的版式字段（`bbox` 之外，方便按原样重建版面）
_BLOCK_STYLE_KEYS = ("size", "line_h")


def _block_view(page: int, block: dict, text: str, *, extras: bool = True) -> dict:
    """一个段落块在导出里的样子：**原格式**（bbox + 段落文本）。

    几何沿用 `doc.json` 的口径（`x` / `y` 是左上角、`w` / `h` 是宽高，单位 pt），
    另外给一份 `bbox = [x0, y0, x1, y1]`，方便直接和 PDF 坐标对照。

    `extras=True` 时还会带上原来的行内公式区间（`math`）与表格 HTML（`html`），
    这样导出后仍能还原公式与表格，而不是只剩被摊平的纯文本。
    """
    def _num(key: str) -> float | None:
        try:
            return round(float(block.get(key)), 3)
        except (TypeError, ValueError):
            return None

    x, y, w, h = (_num(key) for key in ("x", "y", "w", "h"))
    view: dict[str, Any] = {
        "id": str(block.get("id") or ""),
        "page": page,
        "kind": str(block.get("kind") or ""),
        "label": str(block.get("label") or ""),
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "bbox": ([x, y, round(x + w, 3), round(y + h, 3)]
                 if None not in (x, y, w, h) else None),
        "text": text,
    }
    for key in _BLOCK_STYLE_KEYS:
        value = block.get(key)
        if value is not None:
            view[key] = value
    if not extras:
        return view
    math = block.get("math")
    if isinstance(math, list) and math:
        view["math"] = math
    html = _norm(block.get("html"))
    if html:
        view["html"] = html
    return view


def _collect(blocks: list[tuple[int, dict, str]], start: int, end: int, *,
             skip: int | None = None, tail: str = "") -> dict:
    """把 `[start, end)` 的块拼成一个部分：文本 + 页码（1 起）+ **原格式段落块**。

    `text` 仍是被 `strip` 过的正文（给 LLM / 机械报告用），`blocks` 则是逐段的
    `[{id, page, kind, label, x, y, w, h, bbox, text, …}]`，保留原来的 bbox，
    导出后可以按原版面还原每一段的位置。
    """
    parts: list[str] = []
    pages: list[int] = []
    entries: list[dict] = []
    for i in range(max(0, start), min(len(blocks), end)):
        page, blk, text = blocks[i]
        if skip is not None and i == skip:
            if tail:
                parts.append(tail)
                pages.append(page)
                # 标题块后面跟着的正文（`References [1] …`）：几何沿用标题块，
                # 但 math/html 区间是按整块文本算的，这里不能再带过来
                entries.append(_block_view(page, blk, tail, extras=False))
            continue
        if not text:
            continue
        parts.append(text)
        pages.append(page)
        entries.append(_block_view(page, blk, text))
    return {"text": " ".join(parts).strip(), "pages": sorted(set(pages)),
            "blocks": entries}


# =====================================================================
#  分节
# =====================================================================
def _iter_blocks(doc: dict) -> list[tuple[int, dict, str]]:
    blocks: list[tuple[int, dict, str]] = []
    for page_no, page in enumerate((doc or {}).get("pages") or [], start=1):
        if not isinstance(page, dict):
            continue
        for block in page.get("texts") or []:
            if not isinstance(block, dict):
                continue
            text = _block_content(block)
            if text:
                blocks.append((page_no, block, text))
    return blocks


def _reference_entries(doc: dict, blocks: list[tuple[int, dict, str]],
                       ref_start: int | None, ref_end: int,
                       ref_tail: str = "") -> list[dict]:
    """参考文献条目：优先用 `pdf_refs.build_refs()` 的结果（分条更准）。"""
    entries: list[dict] = []
    try:
        info = build_refs(doc) or {}
    except Exception:
        info = {}
    for row in info.get("entries") or []:
        if not isinstance(row, dict):
            continue
        text = _norm(row.get("text"))
        if not text:
            continue
        try:
            page = int(row.get("page"))
        except (TypeError, ValueError):
            page = None
        entries.append({
            "index": int(row.get("n") or len(entries) + 1),
            "page": page + 1 if isinstance(page, int) else None,   # refs 里是 0 起
            "text": text,
            "blocks": [str(b) for b in row.get("blocks") or []],
            "uri": _norm(row.get("uri")),
        })
    if entries:
        return entries

    # 兜底：把参考文献区的块按「编号开头」合并成条目
    if ref_start is None:
        return entries
    texts = [blocks[i][2] for i in range(max(0, ref_start), min(len(blocks), ref_end))]
    if ref_tail:
        texts.insert(0, ref_tail)
    cur: list[str] = []
    for text in texts:
        if re.match(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s*", text) and cur:
            entries.append(_fallback_entry(len(entries) + 1, " ".join(cur)))
            cur = [text]
        else:
            cur.append(text)
    if cur:
        entries.append(_fallback_entry(len(entries) + 1, " ".join(cur)))
    return entries


def _fallback_entry(index: int, text: str) -> dict:
    return {"index": index, "page": None, "text": _norm(text), "blocks": [], "uri": ""}


def build_sections(doc: dict, *, title: str = "") -> dict:
    """把版式文档拆成 title / abstract / main text / references / appendix。

    返回的每个部分都是 `{"text": "...", "pages": [1,2], "blocks": [...]}`：

    * `text`：把这些块拼起来的纯文本（给 LLM / 机械报告用，不含版式）；
    * `blocks`：**原格式的段落集合** —— 逐段 `{id, page, kind, label, x, y, w, h,
      bbox: [x0, y0, x1, y1], text, size, line_h, math?, html?}`，几何沿用
      `doc.json` 的口径（pt、左上原点），导出后可以按原版面还原每段的位置；
    * `title` 是论文标题；`references` 额外带 `entries`（条目化结果）与 `count`；
      `front_matter` 是首页标题/作者/单位等不属于五个部分的内容（保留下来，避免信息丢失）。
    """
    blocks = _iter_blocks(doc)
    n = len(blocks)
    doc_title = _norm(title) or _norm((doc or {}).get("title"))
    if not doc_title and blocks:
        doc_title = blocks[0][2]

    # 1) 标题块位置（第一页里与论文标题一致的块）
    title_idx: int | None = None
    for i, (_page, _block, text) in enumerate(blocks[:24]):
        if _same_title(text, doc_title):
            title_idx = i
            break

    # 2) 收集各个部分的标题位置
    hits: dict[str, list[tuple[int, str]]] = {
        "abstract": [], "references": [], "appendix": [], "intro": []}
    for i, (_page, block, text) in enumerate(blocks):
        name, tail = _section_of(block, text)
        if name:
            hits[name].append((i, tail))

    abstract_idx = hits["abstract"][0][0] if hits["abstract"] else None
    references_idx = next(
        (i for i, _t in hits["references"]
         if abstract_idx is None or i > abstract_idx), None)
    if references_idx is None and hits["references"]:
        references_idx = hits["references"][0][0]
    intro_idx = next(
        (i for i, _t in hits["intro"]
         if abstract_idx is not None and i > abstract_idx), None)
    if intro_idx is None and abstract_idx is None and hits["intro"]:
        intro_idx = hits["intro"][0][0]
    appendix_idx = next(
        (i for i, _t in hits["appendix"]
         if (references_idx is None or i > references_idx)
         and (abstract_idx is None or i > abstract_idx)), None)

    # 3) 各部分的结束位置
    if abstract_idx is not None:
        abstract_end = n
        for j in range(abstract_idx + 1, n):
            if references_idx is not None and j >= references_idx:
                abstract_end = j
                break
            if appendix_idx is not None and j >= appendix_idx:
                abstract_end = j
                break
            name, _tail = _section_of(blocks[j][1], blocks[j][2])
            if name == "intro":
                abstract_end = j
                break
            if name in ("references", "appendix"):
                abstract_end = j
                break
            if _is_heading_like(blocks[j][1], blocks[j][2]):
                abstract_end = j
                break
    else:
        abstract_end = None

    main_start = abstract_end if abstract_end is not None else (
        intro_idx if intro_idx is not None else (
            title_idx + 1 if title_idx is not None else 0))
    main_ends = [x for x in (references_idx, appendix_idx) if x is not None]
    main_end = min(main_ends) if main_ends else n
    if main_end < main_start:
        main_start, main_end = main_end, main_start

    # 4) 摘要：没有 “Abstract” 标题时，用 Introduction 之前的长段落推断
    front_end = abstract_idx if abstract_idx is not None else (
        intro_idx if intro_idx is not None else (title_idx + 1 if title_idx is not None else 0))
    front_parts: list[str] = []
    front_pages: list[int] = []
    front_blocks: list[dict] = []
    abstract_parts: list[str] = []
    abstract_pages: list[int] = []
    abstract_blocks: list[dict] = []
    for i in range(0, max(0, front_end)):
        if i == title_idx:
            continue
        page, _block, text = blocks[i]
        if (abstract_idx is None and intro_idx is not None
                and len(text) >= 120 and not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)):
            abstract_parts.append(text)
            abstract_pages.append(page)
            abstract_blocks.append(_block_view(page, _block, text))
        else:
            front_parts.append(text)
            front_pages.append(page)
            front_blocks.append(_block_view(page, _block, text))

    def _tail_for(name: str, idx: int | None) -> str:
        for i, tail in hits.get(name) or []:
            if i == idx:
                return tail
        return ""

    if abstract_idx is not None:
        abstract = _collect(blocks, abstract_idx, abstract_end or n,
                            skip=abstract_idx,
                            tail=_tail_for("abstract", abstract_idx))
    elif abstract_parts:
        abstract = {"text": " ".join(abstract_parts).strip(),
                    "pages": sorted(set(abstract_pages)),
                    "blocks": abstract_blocks}
    else:
        abstract = {"text": "", "pages": [], "blocks": []}

    # 5) 参考文献（标题到附录之间）
    refs_end = appendix_idx if appendix_idx is not None else n
    if references_idx is not None:
        ref_tail = _tail_for("references", references_idx)
        references = _collect(blocks, references_idx, refs_end,
                              skip=references_idx, tail=ref_tail)
    else:
        ref_tail = ""
        references = {"text": "", "pages": [], "blocks": []}
    entries = _reference_entries(doc, blocks, references_idx, refs_end, ref_tail)
    references["entries"] = entries
    references["count"] = len(entries)

    # 6) 正文与附录（`blocks` 保留每段的原 bbox）
    main_text = _collect(blocks, main_start, main_end)
    appendix = (_collect(blocks, appendix_idx, n, skip=appendix_idx,
                         tail=_tail_for("appendix", appendix_idx))
                if appendix_idx is not None
                else {"text": "", "pages": [], "blocks": []})

    title_page = blocks[title_idx][0] if title_idx is not None else (1 if blocks else None)
    title_block = (_block_view(blocks[title_idx][0], blocks[title_idx][1],
                               blocks[title_idx][2])
                   if title_idx is not None else None)
    return {
        "title": {"text": doc_title, "pages": [title_page] if title_page else [],
                  "blocks": [title_block] if title_block else []},
        "abstract": abstract,
        "main_text": main_text,
        "references": references,
        "appendix": appendix,
        "front_matter": {"text": " ".join(front_parts).strip(),
                         "pages": sorted(set(front_pages)),
                         "blocks": front_blocks},
    }


# =====================================================================
#  笔记 / 高亮
# =====================================================================
def _block_map(doc: dict) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for page_no, page in enumerate((doc or {}).get("pages") or [], start=1):
        if not isinstance(page, dict):
            continue
        for block in page.get("texts") or []:
            if not isinstance(block, dict):
                continue
            block_id = str(block.get("id") or "")
            if block_id:
                out[block_id] = (page_no, _block_content(block))
    return out


def _split_sentence_key(key: str) -> tuple[str, int]:
    """`"p0b3#1"` → `("p0b3", 1)`；不是句子 key 时 `si=-1`（与前端 `keyParts` 同口径）。"""
    text = str(key or "")
    pos = text.rfind("#")
    if pos > 0:
        suffix = text[pos + 1:]
        if suffix.isdigit():
            return text[:pos], int(suffix)
    return text, -1


def _sentence_at(text: str, si: int) -> str:
    """取块里第 `si` 句；切句口径与 `pdf_parser.sentences.split_sentences` 一致。"""
    text = _norm(text)
    if si < 0 or not text:
        return text
    try:
        from .pdf_parser.sentences import split_sentences      # 延迟导入，免得拖重依赖
        spans = split_sentences(text)
    except Exception:
        return text
    if 0 <= si < len(spans):
        start, end = spans[si]
        piece = _norm(text[start:end])
        return piece or text
    return text


def build_notes(anno: dict | None, doc: dict, rec: dict | None = None) -> dict:
    """把存储层的标注解析成导出用的笔记视图（带页码与原句）。

    * `sentence_notes`：句子笔记，`block_id` 里的 `#si` 会解析成页码 + 原句；
    * `free_notes`：不绑定正文的独立笔记；
    * `highlights`：高亮（块 id → 颜色），同样补页码与原句；
    * `stats`：条数 / 作者分布 / 颜色分布，机械报告直接用。
    """
    block_map = _block_map(doc)
    anno = anno or {}

    def resolve(block_id: str) -> tuple[str, int, int, str]:
        base, si = _split_sentence_key(block_id)
        page, text = block_map.get(base, (0, ""))
        return base, si, page, _sentence_at(text, si)

    sentence_notes: list[dict] = []
    for note in anno.get("notes") or []:
        if not isinstance(note, dict):
            continue
        block_id = str(note.get("block_id") or "")
        base, si, page, sentence = resolve(block_id)
        sentence_notes.append({
            "id": str(note.get("id") or ""),
            "block_id": block_id,
            "block": base,
            "sentence_index": si,
            "page": page,
            "sentence": sentence,
            "note": _norm(note.get("text")),
            "author": (records.note_author(rec, note) if rec else
                       _norm(note.get("by"))),
            "created_at": note.get("created_at"),
            "updated_at": note.get("updated_at"),
        })

    free_notes: list[dict] = []
    for note in anno.get("free_notes") or []:
        if not isinstance(note, dict):
            continue
        free_notes.append({
            "id": str(note.get("id") or ""),
            "title": _norm(note.get("title")),
            "text": _norm(note.get("text")),
            "author": (records.note_author(rec, note) if rec else
                       _norm(note.get("by"))),
            "created_at": note.get("created_at"),
            "updated_at": note.get("updated_at"),
        })

    highlights: list[dict] = []
    for block_id, row in (anno.get("highlights") or {}).items():
        if not isinstance(row, dict):
            row = {"color": row}
        base, si, page, sentence = resolve(str(block_id))
        highlights.append({
            "block_id": str(block_id),
            "block": base,
            "sentence_index": si,
            "page": page,
            "sentence": sentence,
            "color": _norm(row.get("color")) or "yellow",
            "at": row.get("at"),
        })
    highlights.sort(key=lambda h: (h["page"] or 10 ** 9, h["block_id"]))

    by_author: dict[str, int] = {}
    for row in sentence_notes + free_notes:
        author = row["author"] or "（未标注）"
        by_author[author] = by_author.get(author, 0) + 1
    by_color: dict[str, int] = {}
    for row in highlights:
        by_color[row["color"]] = by_color.get(row["color"], 0) + 1

    return {
        "sentence_notes": sentence_notes,
        "free_notes": free_notes,
        "highlights": highlights,
        "stats": {
            "sentence_notes": len(sentence_notes),
            "free_notes": len(free_notes),
            "notes_total": len(sentence_notes) + len(free_notes),
            "highlights": len(highlights),
            "pages_with_notes": len({n["page"] for n in sentence_notes if n["page"]}),
            "by_author": by_author,
            "highlights_by_color": by_color,
        },
    }


# =====================================================================
#  机械报告
# =====================================================================
def build_mechanical_report(paper: dict, sections: dict, notes_data: dict) -> dict:
    """机械导出的「分析报告」：不调用 LLM，只做结构与统计。"""
    stats = notes_data.get("stats") or {}
    refs = (sections.get("references") or {})
    all_text = "\n".join(
        str((sections.get(key) or {}).get("text") or "")
        for key in ("title", "abstract", "main_text", "references", "appendix"))
    all_text += "\n" + "\n".join(
        f"{n.get('title')} {n.get('text')}" for n in notes_data.get("free_notes") or [])
    code_links = _code_links(all_text)

    has = {
        "abstract": bool((sections.get("abstract") or {}).get("text")),
        "references": bool(refs.get("text") or refs.get("entries")),
        "appendix": bool((sections.get("appendix") or {}).get("text")),
    }
    structure = [
        "标题", "摘要" if has["abstract"] else "摘要（未识别）",
        "正文", "参考文献" if has["references"] else "参考文献（未识别）",
        "附录" if has["appendix"] else "附录（未识别）",
    ]
    summary = (
        f"《{paper.get('title') or '未命名论文'}》共 {paper.get('num_pages') or '?'} 页；"
        f"按 {' / '.join(structure)} 分节；"
        f"参考文献 {refs.get('count') or 0} 条；"
        f"句子笔记 {stats.get('sentence_notes', 0)} 条、"
        f"独立笔记 {stats.get('free_notes', 0)} 条、"
        f"高亮 {stats.get('highlights', 0)} 处"
        + (f"；正文/笔记里提到 {len(code_links)} 个代码地址" if code_links else "")
        + "。本报告为机械导出（未调用 LLM）。"
    )
    return {
        "mode": "mechanical",
        "summary": summary,
        "section_overview": {
            "has_abstract": has["abstract"],
            "has_references": has["references"],
            "has_appendix": has["appendix"],
            "section_pages": {
                key: (sections.get(key) or {}).get("pages") or []
                for key in ("abstract", "main_text", "references", "appendix")
            },
        },
        "stats": {
            **stats,
            "references": refs.get("count") or 0,
            "pages": paper.get("num_pages") or 0,
        },
        "references": {
            "count": refs.get("count") or 0,
            "numbers": [e.get("index") for e in refs.get("entries") or []
                        if isinstance(e, dict)],
            "pages": (sections.get("references") or {}).get("pages") or [],
        },
        "code_links": code_links,
        "notes_by_page": _notes_by_page(notes_data),
    }


def _notes_by_page(notes_data: dict) -> list[dict]:
    rows: dict[int, int] = {}
    for note in notes_data.get("sentence_notes") or []:
        page = int(note.get("page") or 0)
        if page > 0:
            rows[page] = rows.get(page, 0) + 1
    return [{"page": page, "notes": rows[page]} for page in sorted(rows)]


def _code_links(text: str) -> list[str]:
    # 从 Agent 模块复用同一套提取规则（机械导出与工具候选口径一致）
    from .agents.note_out_agent import extract_code_links
    return extract_code_links(text)


# =====================================================================
#  导出入口
# =====================================================================
def _paper_meta(doc: dict, rec: dict | None, *, username: str = "") -> dict:
    rec = rec or {}
    reading: dict = {}
    if rec:
        reading = (records.reading_of(rec, username) if username else
                   {key: rec.get(key) for key in records.READING_KEYS})
        reading = {key: value for key, value in reading.items() if value is not None}
    return {
        "id": str(rec.get("id") or (doc or {}).get("id") or ""),
        "title": _norm(rec.get("title") or (doc or {}).get("title")),
        "source": _norm(rec.get("source") or (doc or {}).get("source")),
        "num_pages": int(rec.get("num_pages") or len((doc or {}).get("pages") or []) or 0),
        "parser": _norm(rec.get("parser") or (doc or {}).get("parser")),
        "owner": (records.owner_of(rec) if rec else ""),
        "reading": reading,
        "created_at": rec.get("created_at"),
    }


def export_paper(*, doc: dict, rec: dict | None = None, anno: dict | None = None,
                 mode: str = "mechanical", llm: LlmClient | None = None,
                 username: str = "", instructions: str = "",
                 scholar: dict | None = None) -> dict:
    """生成完整导出 JSON。

    * `mode="mechanical"`：纯本地，不需要 LLM；
    * `mode="agent"`：`llm` 必须是可用的 `agents/llm.py::LlmClient`
      （Google 免费兜底做不了工具调用与结构化报告）。

    返回结构：

    ```json
    {
      "format": "tardigrade-note-export",
      "version": 1,
      "exported_at": "2026-…",
      "mode": "mechanical" | "agent",
      "paper": {...},
      "sections": {
        "title": {"text": "...", "pages": [1], "blocks": [...]},
        "abstract": {...}, "main_text": {...},
        "references": {"text": "...", "blocks": [...],
                       "entries": [...], "count": 12},
        "appendix": {...}, "front_matter": {...}
      },
      "notes": {"sentence_notes": [...], "free_notes": [...],
                "highlights": [...], "stats": {...}},
      "report": {...}
    }
    ```
    """
    mode = str(mode or "mechanical").strip().lower()
    if mode not in MODES:
        raise ValueError("导出模式只能是 mechanical 或 agent")
    paper = _paper_meta(doc, rec, username=username)
    sections = build_sections(doc, title=paper["title"])
    notes_data = build_notes(anno, doc, rec)
    if mode == "agent":
        if llm is None:
            raise ValueError("Agent 导出需要可用的 LLM（请在设置里配置 OpenAI 兼容服务）")
        from .agents.note_out_agent import run_note_out_agent
        report = run_note_out_agent(llm, paper=paper, sections=sections,
                                    notes=notes_data, instructions=instructions,
                                    scholar=scholar)
    else:
        report = build_mechanical_report(paper, sections, notes_data)
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": _now(),
        "mode": mode,
        "paper": paper,
        "sections": sections,
        "notes": notes_data,
        "report": report,
    }


def to_json(export: dict) -> str:
    """导出 JSON 文本（UTF-8、中文不转义、缩进 2）。"""
    return json.dumps(export, ensure_ascii=False, indent=2)


def export_filename(paper: dict | None) -> str:
    """下载文件名：`<论文标题>-note-export.json`（非法字符替换成 `_`）。"""
    title = _norm((paper or {}).get("title")) or "paper"
    safe = re.sub(r'[\\/:*?"<>|]+', "_", title).strip(" ._")[:60] or "paper"
    return f"{safe}-note-export.json"


__all__ = [
    "EXPORT_FORMAT", "EXPORT_VERSION", "MODES",
    "build_sections", "build_notes", "build_mechanical_report",
    "export_paper", "to_json", "export_filename",
]
