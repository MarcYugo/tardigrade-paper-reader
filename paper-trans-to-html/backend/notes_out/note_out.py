"""模块十 · 笔记导出（机械导出 / Agent 导出）

把一篇论文的**内容与笔记**导出成一份 JSON（`note.md` 功能 16）：

* 两种模式都先把论文拆成 **title / abstract / main text / references / appendix**，
  每一节都同时保留 `text`（拼好的纯文本）与 `blocks`（**原格式的段落集合**，
  逐段带 `x/y/w/h` + `bbox`，以及行内公式区间与表格 HTML）；
* **机械导出**（`mode="mechanical"`）：纯本地、零 LLM 成本，直接给分节内容、
  参考文献条目、句子笔记 / 独立笔记 / 高亮与一份统计报告；
* **Agent 导出**（`mode="agent"`）：**机械导出打底**，再由
  `agents/skills/note_out.py`（技能「笔记导出」）调用只读工具（核验参考文献是否
  真实存在、提取并确认项目代码地址），把「AI 总结 + 笔记整理 + 联网核验结论」
  合并进机械报告（`merge_agent_report()`）—— 不是让大模型重写整份报告。

这里不碰数据库、也不做权限判断：`doc` / `rec` / `anno` 都由调用方（`server/api/export.py` / `server/api/transfer.py`）取好，
本模块只做**纯数据转换**。这样后面接「transfer key 让别的 Agent 拿走笔记」时，
数据对接层可以直接复用同一份导出结构。

对外接口：

| 函数 | 作用 |
| --- | --- |
| `build_sections()` | 把版式文档拆成五个部分（另带 `front_matter`） |
| `build_fulltext()` | 整篇论文的**纯文本**（按分节顺序拼接，**不含 x/y/w/h/bbox**） |
| `build_figures()` | 论文配图：文件信息 + 图注 + **base64 `data_url`** |
| `check_references()` | 参考文献检查：正文引用 vs 列表条目（缺失 / 未被引用 / 重复 / DOI·arXiv·链接） |
| `build_notes()` | 把笔记 / 高亮解析成「页码 + 原句」，并补作者 |
| `merge_agent_report()` | Agent 导出：机械报告 + AI 分析 / 联网核验结论合并 |
| `paper_authors()` | 从首页标题之后猜作者（图片导出第一栏用） |
| `build_ai_summary()` | 取 AI 辅助阅读面板的「整理笔记」（图片导出第三栏用） |
| `export_paper()` | 生成完整导出 JSON（机械 / Agent） |
| `to_json()` / `export_filename()` | 序列化与下载文件名（HTTP 层用，`fmt="image"` 时是 `.png`） |
| `extract_code_links()` | 从文本里提取代码地址（转导出给 Agent 复用） |
"""
from __future__ import annotations

import base64
import datetime
import json
import re
from pathlib import Path
from typing import Any

from .. import records
from ..agents.llm import LlmClient
from ..pdf_refs import build_refs

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
#  整篇纯文本（不含任何版式字段）
# =====================================================================
_SECTION_ORDER = ("title", "front_matter", "abstract", "main_text",
                  "references", "appendix")
_SECTION_LABELS = {
    "title": "标题", "front_matter": "首页信息", "abstract": "摘要",
    "main_text": "正文", "references": "参考文献", "appendix": "附录",
}


def _plain_block(section: str, block: dict) -> dict:
    """段落块的**去版式**视图：只留 分节 / id / 页码 / 类型 / 文本。

    刻意**不带** `x` / `y` / `w` / `h` / `bbox` —— 这是「完整论文文本」的段落口径，
    要几何信息请用 `sections[*].blocks`（原格式块）。
    """
    return {
        "section": section,
        "id": str(block.get("id") or ""),
        "page": block.get("page"),
        "kind": str(block.get("kind") or ""),
        "label": str(block.get("label") or ""),
        "text": _block_content(block),
    }


def build_fulltext(sections: dict, *, limit_chars: int = 0) -> dict:
    """整篇论文的**纯文本**：按 标题 → 首页信息 → 摘要 → 正文 → 参考文献 → 附录 拼接。

    * `text`：所有段落用空行连起来的完整正文（**没有任何 bbox / 坐标**）；
    * `paragraphs`：逐段 `{section, id, page, kind, label, text}`（同样不含几何）；
    * `sections`：分节目录（页码 / 字数 / 在 `text` 里的 `start`\~`end` 偏移）；
    * `limit_chars > 0` 时只截 `text`（`truncated=True`），`paragraphs` 保持完整。

    几何信息仍在 `build_sections()` 的 `blocks` 里，两者互不干扰。
    """
    chunks: list[str] = []
    paragraphs: list[dict] = []
    outline: list[dict] = []
    offset = 0
    for name in _SECTION_ORDER:
        sec = (sections or {}).get(name)
        if not isinstance(sec, dict):
            continue
        rows = [_plain_block(name, block)
                for block in (sec.get("blocks") or []) if isinstance(block, dict)]
        rows = [row for row in rows if row["text"]]
        body = "\n\n".join(row["text"] for row in rows)
        if not body:
            continue
        outline.append({"name": name, "title": _SECTION_LABELS.get(name, name),
                        "pages": sec.get("pages") or [], "chars": len(body),
                        "paragraphs": len(rows), "start": offset,
                        "end": offset + len(body)})
        chunks.append(body)
        offset += len(body) + 2
        paragraphs.extend(rows)

    text = "\n\n".join(chunks)
    out: dict[str, Any] = {
        "chars": len(text),
        "text": text,
        "sections": outline,
        "paragraphs": paragraphs,
        "plain": True,          # 明确标记：这里不含任何版式 / bbox 字段
        "bbox": False,
    }
    if limit_chars and len(text) > int(limit_chars):
        out["text"] = text[:int(limit_chars)]
        out["truncated"] = True
    return out


# =====================================================================
#  配图（插图 + base64）
# =====================================================================
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}
_CAPTION_KINDS = ("caption", "figure_caption", "image_caption", "table_caption",
                  "figcaption")


def _caption_for(page: dict, image: dict) -> str:
    """给一张插图找最近的图注（同页 caption 类块：横向重叠、纵向最近）。"""
    try:
        ix = float(image.get("x") or 0)
        iy = float(image.get("y") or 0)
        iw = float(image.get("w") or 0)
        ih = float(image.get("h") or 0)
    except (TypeError, ValueError):
        return ""
    best_gap: float | None = None
    best_text = ""
    for block in page.get("texts") or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind") or "").strip().lower()
        label = str(block.get("label") or "").strip().lower()
        if kind not in _CAPTION_KINDS and label not in _CAPTION_KINDS:
            continue
        text = _block_content(block)
        if not text:
            continue
        try:
            bx = float(block.get("x") or 0)
            by = float(block.get("y") or 0)
            bw = float(block.get("w") or 0)
            bh = float(block.get("h") or 0)
        except (TypeError, ValueError):
            continue
        if bx + bw < ix - 8 or bx > ix + iw + 8:      # 横向完全不重叠
            continue
        gap = max(0.0, by - (iy + ih), iy - (by + bh))
        if best_gap is None or gap < best_gap:
            best_gap, best_text = gap, text
    return best_text


def build_figures(doc: dict, *, store=None, doc_id: str = "",
                  images_dir: str | Path | None = None,
                  include_data: bool = True, max_images: int = 60,
                  max_image_kb: int = 4096, max_total_kb: int = 24576) -> dict:
    """论文配图：逐张给出文件信息、图注和 **base64 `data_url`**。

    * 图片来源：`pages[].images[]`（解析器裁好落盘的插图）；
    * 字节来源：优先 `store.get_image(doc_id, file)`（MongoDB 走 GridFS），
      没有 `store` 时退回 `images_dir/file` 磁盘文件；
    * `data_url` 形如 `data:image/png;base64,…`，可直接被 Agent / 前端当图片用；
    * 单张超过 `max_image_kb`、或总数超过 `max_total_kb` 的图**不带 base64**
      （仍然列出文件与图注，`skipped` 里写明原因），避免导出体积失控。
    """
    def _read(fname: str) -> bytes | None:
        if store is not None and doc_id:
            try:
                data = store.get_image(doc_id, fname)
            except Exception:
                data = None
            if data:
                return data
        if images_dir:
            try:
                return (Path(images_dir) / fname).read_bytes()
            except OSError:
                return None
        return None

    items: list[dict] = []
    skipped: list[dict] = []
    total_bytes = 0
    with_data = 0
    for page_no, page in enumerate((doc or {}).get("pages") or [], start=1):
        if not isinstance(page, dict):
            continue
        for image in page.get("images") or []:
            if not isinstance(image, dict):
                continue
            fname = str(image.get("file") or "").strip()
            if not fname:
                continue
            entry: dict[str, Any] = {
                "page": page_no,
                "file": fname,
                "kind": str(image.get("class_name") or "Figure"),
                "source": str(image.get("source") or ""),
                "cache_key": str(image.get("cache_key") or ""),
                "size": {"w": image.get("w"), "h": image.get("h")},
                "caption": _caption_for(page, image),
            }
            if not include_data:
                items.append(entry)
                continue
            if len(items) >= max(1, int(max_images)):
                skipped.append({"page": page_no, "file": fname,
                                "reason": "max_images"})
                continue
            raw = _read(fname)
            if not raw:
                entry["bytes"] = 0
                skipped.append({"page": page_no, "file": fname, "reason": "missing"})
                items.append(entry)
                continue
            entry["bytes"] = len(raw)
            if len(raw) > max(1, int(max_image_kb)) * 1024:
                skipped.append({"page": page_no, "file": fname,
                                "reason": "too_large", "bytes": len(raw)})
                items.append(entry)
                continue
            if total_bytes + len(raw) > max(1, int(max_total_kb)) * 1024:
                skipped.append({"page": page_no, "file": fname,
                                "reason": "total_budget", "bytes": len(raw)})
                items.append(entry)
                continue
            mime = _IMAGE_MIME.get(Path(fname).suffix.lower(), "image/png")
            entry["mime"] = mime
            entry["data_url"] = (f"data:{mime};base64,"
                                 + base64.b64encode(raw).decode("ascii"))
            total_bytes += len(raw)
            with_data += 1
            items.append(entry)

    return {"count": len(items), "with_data": with_data,
            "bytes": total_bytes, "items": items, "skipped": skipped,
            "truncated": bool(skipped)}


# =====================================================================
#  参考文献检查
# =====================================================================
_RE_BRACKET_CITE = re.compile(
    r"\[(\d{1,3}(?:\s*[,;，、]\s*\d{1,3}|\s*[–—-]\s*\d{1,3})*)\]")
_RE_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+", re.I)
_RE_ARXIV = re.compile(r"\barxiv\s*[:：]?\s*(\d{4}\.\d{4,5}(?:v\d+)?)", re.I)
_RE_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)


def _expand_cite(group: str) -> list[int]:
    """`"3, 5-7"` → `[3, 5, 6, 7]`（只在括号内展开，避免误伤普通数字）。"""
    out: list[int] = []
    for part in re.split(r"[,;，、]", group or ""):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d{1,3})\s*[–—-]\s*(\d{1,3})$", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if 0 < lo <= hi <= 999 and hi - lo <= 60:
                out.extend(range(lo, hi + 1))
            continue
        if part.isdigit():
            out.append(int(part))
    return out


def _cited_numbers(text: str) -> list[int]:
    """正文里出现过的引用编号（`[12]` / `[3, 5]` / `[7–9]`）。"""
    found: list[int] = []
    for m in _RE_BRACKET_CITE.finditer(str(text or "")):
        found.extend(_expand_cite(m.group(1)))
    return sorted(set(found))


def check_references(sections: dict) -> dict:
    """参考文献检查（**纯本地**，不联网）：正文引用与列表条目是否对得上。

    * `count` / `numbers`：列表条目数与编号；
    * `cited`：正文里出现过的编号；`missing`：引用了但列表里没有；
    * `uncited`：列表里有但正文没引用；
    * `duplicates`：疑似重复条目（按归一化文本比对）；
    * `links`：条目里带 DOI / arXiv / URL 的；`with_link` 是条数；
    * `ok`：没有 `missing` 也没有 `duplicates` 时为 True。

    联网核验（Crossref / OpenAlex / S2 / arXiv）在 Agent 模式下由
    `agents/skills/note_out.py` 完成，结论写在 `report.references_stats` 里；
    本函数只给「结构层面的检查」。
    """
    refs = (sections or {}).get("references") or {}
    entries = [e for e in (refs.get("entries") or []) if isinstance(e, dict)]

    numbers: list[int] = []
    for pos, entry in enumerate(entries, start=1):
        try:
            num = int(entry.get("index") or pos)
        except (TypeError, ValueError):
            num = pos
        numbers.append(num if num > 0 else pos)

    body_parts: list[str] = []
    for key in ("abstract", "main_text", "appendix", "front_matter", "title"):
        sec = (sections or {}).get(key)
        if isinstance(sec, dict):
            body_parts.append(str(sec.get("text") or ""))
    body = "\n".join(body_parts)
    cited = _cited_numbers(body)
    known = set(numbers)
    missing = [num for num in cited if num not in known]
    uncited = [num for num in numbers if num not in set(cited)]

    seen: dict[str, int] = {}
    duplicates: list[dict] = []
    links: list[dict] = []
    for num, entry in zip(numbers, entries):
        text = str(entry.get("text") or "")
        key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text.lower())[:120]
        if len(key) >= 20:
            if key in seen:
                duplicates.append({"index": num, "same_as": seen[key]})
            else:
                seen[key] = num
        uri = str(entry.get("uri") or "").strip()
        doi = _RE_DOI.search(text)
        arxiv = _RE_ARXIV.search(text)
        url = uri or (doi.group(0) if doi else "")
        if arxiv and not url:
            url = "https://arxiv.org/abs/" + arxiv.group(1)
        if not url:
            hit = _RE_URL.search(text)
            url = hit.group(0).rstrip(".,;)") if hit else ""
        if url:
            links.append({"index": num, "url": url,
                          "kind": ("arxiv" if arxiv else
                                   "doi" if doi else "url")})

    ok = not missing and not duplicates
    style = "numeric" if cited else "unknown"
    if not cited:
        # 作者-年份制（或扫描 PDF 丢了引用括号）：不做「未被引用」对账，避免误报
        uncited = []
        note = "正文里没找到 [n] 形式的引用编号（可能是作者-年份制），未做引用对账"
    else:
        note = ""
    summary = (f"参考文献 {len(entries)} 条；正文引用 {len(cited)} 个编号；"
               f"缺失 {len(missing)} 条、未被引用 {len(uncited)} 条、"
               f"疑似重复 {len(duplicates)} 条；带 DOI/arXiv/链接 {len(links)} 条。")
    return {"ok": ok, "summary": summary, "style": style, "note": note,
            "count": len(entries), "numbers": numbers,
            "cited": cited, "cited_count": len(cited),
            "missing": missing, "uncited": uncited,
            "duplicates": duplicates, "links": links,
            "with_link": len(links)}


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
        from ..pdf_parser.sentences import split_sentences      # 延迟导入，免得拖重依赖
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
def build_mechanical_report(paper: dict, sections: dict, notes_data: dict, *,
                           figures: dict | None = None,
                           reference_check: dict | None = None,
                           code_links: list[str] | None = None) -> dict:
    """机械导出的「分析报告」：不调用 LLM，只做结构与统计。"""
    stats = notes_data.get("stats") or {}
    refs = (sections.get("references") or {})
    all_text = "\n".join(
        str((sections.get(key) or {}).get("text") or "")
        for key in ("title", "abstract", "main_text", "references", "appendix"))
    all_text += "\n" + "\n".join(
        f"{n.get('title')} {n.get('text')}" for n in notes_data.get("free_notes") or [])
    links = list(code_links) if code_links is not None else _code_links(all_text)
    figures = figures or {}
    ref_check = reference_check or {}

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
        + (f"{ref_check.get('summary').rstrip('。')}；"
           if ref_check.get("summary") else f"参考文献 {refs.get('count') or 0} 条；")
        + f"句子笔记 {stats.get('sentence_notes', 0)} 条、"
        f"独立笔记 {stats.get('free_notes', 0)} 条、"
        f"高亮 {stats.get('highlights', 0)} 处"
        + (f"；配图 {figures.get('count')} 张（{figures.get('with_data', 0)} 张带 base64）"
           if figures.get("count") else "")
        + (f"；正文/笔记里提到 {len(links)} 个代码地址" if links else "")
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
        "code_links": links,
        "figures": {"count": figures.get("count") or 0,
                    "with_data": figures.get("with_data") or 0,
                    "bytes": figures.get("bytes") or 0},
        "reference_check": {
            "ok": ref_check.get("ok"),
            "style": ref_check.get("style") or "unknown",
            "count": ref_check.get("count") or 0,
            "cited_count": ref_check.get("cited_count") or 0,
            "missing": ref_check.get("missing") or [],
            "uncited": ref_check.get("uncited") or [],
            "duplicates": ref_check.get("duplicates") or [],
            "with_link": ref_check.get("with_link") or 0,
        },
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
    # 从笔记导出技能复用同一套提取规则（机械导出与工具候选口径一致）
    from ..agents.skills.note_out import extract_code_links
    return extract_code_links(text)


# =====================================================================
#  Agent 导出：机械报告打底 + AI 分析补充
# =====================================================================
# AI 只负责「文字分析与联网核验结论」；分节结构、统计、参考文献结构检查、
# 配图、代码链接一律用机械报告的结果 —— 模型输出的数字不会污染这些字段。
_AI_REPORT_KEYS = (
    "summary", "paper_overview", "notes_analysis",
    "references_stats", "references_review", "references_review_summary",
    "code_repositories", "conclusion", "next_steps", "evidence",
)


def merge_agent_report(mechanical: dict, ai: dict | None) -> dict:
    """把 Agent 的 AI 分析合并进机械报告（Agent 导出 = 机械导出 + AI 总结/核验）。

    * 机械部分（`build_mechanical_report()` 的结果）**全部保留**：`section_overview`
      / `stats` / `references` / `reference_check` / `code_links` / `figures` /
      `notes_by_page`；它那句结构化的句子摘要改放在 `mechanical_summary`（不丢）；
    * AI 部分只覆盖 `_AI_REPORT_KEYS`，而且**只有非空值才覆盖**：模型没给的字段
      继续用机械结果（不会变成 null / 空对象）；
    * `mode` 标成 `agent`，图片导出 / 前端据此判断「本轮有大模型参与」。
    """
    report = dict(mechanical or {})
    ai = ai if isinstance(ai, dict) else {}
    report["mechanical_summary"] = report.get("summary") or ""
    report["mode"] = "agent"
    for key in _AI_REPORT_KEYS:
        value = ai.get(key)
        if value not in (None, "", [], {}):
            report[key] = value
    if not report.get("summary"):
        report["summary"] = report["mechanical_summary"]
    return report


# =====================================================================
#  导出入口
# =====================================================================
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_AFFIL_RE = re.compile(
    r"(?i)\b(university|institute|department|college|school|laboratory|lab|"
    r"research|google|microsoft|meta|amazon|inc\.?|ltd\.?|corp\.?)\b")
_AUTHOR_MARK_RE = re.compile(r"[\*†‡§¶∗⋆]|\^\{[^}]*\}|[_^]\{[^}]*\}|\d+\s*[,;]\s*\d+")
_FRONT_NOISE_RE = re.compile(
    r"(?i)\b(arxiv|doi|issn|isbn|copyright|©|preprint|under review|accepted|published)\b")


def paper_authors(doc: dict, title: str = "", limit: int = 10) -> list[str]:
    """从首页版式里猜作者（导出图片第一栏用）。

    只取「标题块之后、摘要/引言之前」的文字块，每条只留**首行**
    （作者块常是 `Name^{*}\\nAffiliation\\nemail` 的形态），去掉上标与邮箱；
    识别不到就返回空列表 —— 不猜比猜错好。
    """
    pages = (doc or {}).get("pages") or []
    if not pages or not isinstance(pages[0], dict):
        return []
    blocks = [b for b in pages[0].get("texts") or [] if isinstance(b, dict)]

    def _y(block: dict) -> float | None:
        try:
            return float(block.get("y"))
        except (TypeError, ValueError):
            return None

    title_y = None
    for block in blocks:
        if _same_title(_norm(block.get("text")), title):
            title_y = _y(block)
            break
    if title_y is None:
        return []

    stop_y = title_y + 280.0
    for block in blocks:
        y = _y(block)
        if y is None or y <= title_y:
            continue
        name, _tail = _section_of(block, _norm(block.get("text")))
        if name in ("abstract", "intro") and y < stop_y:
            stop_y = y

    found: list[tuple[float, str]] = []
    for block in blocks:
        y = _y(block)
        if y is None or y <= title_y + 2 or y >= stop_y:
            continue
        raw = str(block.get("text") or "")
        if not raw:
            continue
        first = _norm(raw.split("\n")[0])
        if not first or len(first) > 120 or _AFFIL_RE.search(first):
            continue
        if _EMAIL_RE.search(first):            # 作者块首行不带邮箱；带了就不是名字
            continue
        if _FRONT_NOISE_RE.search(first):      # 「arXiv:… / DOI 10.… / © 2024 …」这类页眉
            continue
        first = _norm(_AUTHOR_MARK_RE.sub(" ", first)).strip(" ,;:")
        if not first or not re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", first):
            continue
        found.append((y, first))

    out: list[str] = []
    for _y, name in sorted(found, key=lambda item: item[0]):
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def build_ai_summary(rec: dict | None) -> dict | None:
    """AI 辅助阅读面板里的「整理笔记」（图片导出第三栏用）。

    返回 `{"summary": {...}, "at": 整理时间, "edited_at": 手改时间}`，
    从没整理过时返回 `None`。
    """
    ai = (rec or {}).get("ai") or {}
    summary = ai.get("summary")
    if not isinstance(summary, dict):
        return None
    from ..ai_read import clean_summary      # 延迟导入：只做结构收敛，不引入额外依赖
    return {
        "summary": clean_summary(summary, loose=True),
        "at": ai.get("summary_at"),
        "edited_at": ai.get("summary_edited_at"),
    }


def _paper_meta(doc: dict, rec: dict | None, *, username: str = "") -> dict:
    rec = rec or {}
    reading: dict = {}
    if rec:
        reading = (records.reading_of(rec, username) if username else
                   {key: rec.get(key) for key in records.READING_KEYS})
        reading = {key: value for key, value in reading.items() if value is not None}
    title = _norm(rec.get("title") or (doc or {}).get("title"))
    return {
        "id": str(rec.get("id") or (doc or {}).get("id") or ""),
        "title": title,
        "authors": paper_authors(doc, title),
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
                 scholar: dict | None = None,
                 store=None, doc_id: str = "",
                 images_dir: str | Path | None = None,
                 with_fulltext: bool = True, with_images: bool = False,
                 fulltext_limit: int = 0,
                 image_budget_mb: float = 24.0, image_max_mb: float = 4.0,
                 max_images: int = 60,
                 refresh_references: bool = False) -> dict:
    """生成完整导出 JSON。

    * `mode="mechanical"`：纯本地，不需要 LLM；
    * `mode="agent"`：`llm` 必须是可用的 `agents/llm.py::LlmClient`
      （Google 免费兜底做不了工具调用与结构化报告）。

    `store` / `doc_id` 用来取**配图字节**（`store.get_image(doc_id, file)`，
    MongoDB 走 GridFS）；不传 `store` 时只能拿到图片文件名，没有 base64。

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
      "fulltext": {"chars": 42000, "text": "标题…摘要…正文…",
                   "sections": [...], "paragraphs": [...]},
      "figures": {"count": 12, "with_data": 12, "bytes": 2345678,
                  "items": [{"page": 2, "file": "p002_i000.png",
                             "caption": "Figure 1: …",
                             "data_url": "data:image/png;base64,…"}]},
      "code_links": ["https://github.com/owner/repo"],
      "reference_check": {"ok": true, "count": 37, "cited": [1, 2],
                          "missing": [], "uncited": [9], "links": [...]},
      "notes": {"sentence_notes": [...], "free_notes": [...],
                "highlights": [...], "stats": {...}},
      "ai_summary": {"summary": {...}, "at": "…"} | null,
      "report": {...}
    }
    ```

    * `fulltext` 是**整篇论文的纯文本**（不含 `x/y/w/h/bbox`，几何仍在
      `sections[*].blocks` 里）；`with_fulltext=False` 时不给；
    * `figures` 是**配图**：文件信息 + 图注 + base64 `data_url`（默认关闭，
      传输给外部 Agent 时再打开）；
    * `code_links` 是从正文 / 笔记里提取的代码仓地址（确定性提取，不联网）；
    * `reference_check` 是参考文献的**结构检查**结果（引用编号对账、重复、DOI/arXiv）。

    Agent 模式会把整张参考文献列表联网核验一遍，结果按「论文 + 参考文献指纹」缓存在
    `store`（`get_refcheck` / `set_refcheck`）里：**同一篇论文只全量核验一次**，
    之后的导出直接复用（`refresh_references=True` 可强制重查）。

    `report` 在两种模式下的构成（关键）：

    * `mechanical`：`build_mechanical_report()` 的确定性结果 —— `summary`（结构化一句话）、
      `section_overview` / `stats` / `references` / `reference_check` / `code_links` /
      `figures` / `notes_by_page`；
    * `agent`：**在同一份机械报告上合并 AI 分析**（`merge_agent_report()`，`mode="agent"`）。
      机械字段一个不丢（原 `summary` 移到 `mechanical_summary`），AI 只补
      `summary` / `paper_overview` / `notes_analysis` / `references_stats` /
      `references_review` / `code_repositories` / `conclusion` / `next_steps` —— 大模型
      **不重写整份报告**，也不决定统计数字。

    `paper.authors` 是首页上猜到（也可能为空）的作者；`ai_summary` 是 AI 辅助阅读
    面板里的「整理笔记」，图片导出第三栏直接用它。
    """
    mode = str(mode or "mechanical").strip().lower()
    if mode not in MODES:
        raise ValueError("导出模式只能是 mechanical 或 agent")
    paper = _paper_meta(doc, rec, username=username)
    sections = build_sections(doc, title=paper["title"])
    notes_data = build_notes(anno, doc, rec)
    fulltext = (build_fulltext(sections, limit_chars=fulltext_limit)
                if with_fulltext else None)
    figures = (build_figures(doc, store=store,
                             doc_id=doc_id or paper.get("id") or "",
                             images_dir=images_dir,
                             include_data=True, max_images=max_images,
                             max_image_kb=int(max(0.1, image_max_mb) * 1024),
                             max_total_kb=int(max(1.0, image_budget_mb) * 1024))
               if with_images else None)
    reference_check = check_references(sections)
    code_links = _code_links("\n".join(
        [str((fulltext or {}).get("text") or "")]
        + [f"{n.get('title')} {n.get('text')} {n.get('note')} "
           for n in (notes_data.get("free_notes") or [])
           + (notes_data.get("sentence_notes") or [])]))

    # 机械报告永远先建：Agent 模式也只是在它上面补「AI 分析 / 联网核验结论」，
    # 分节、统计、参考文献结构检查、配图、代码链接这些确定性结果不会因为模型
    # 少给字段或幻觉而丢失。
    report = build_mechanical_report(paper, sections, notes_data,
                                     figures=figures,
                                     reference_check=reference_check,
                                     code_links=code_links)
    if mode == "agent":
        if llm is None:
            raise ValueError("Agent 导出需要可用的 LLM（请在设置里配置 OpenAI 兼容服务）")
        from ..agents.skills.note_out import run_note_out_agent
        # 参考文献核验很慢（整张列表联网查）：把核验结论按论文落库缓存，
        # 下次导出直接复用，只补查没结论的那几条。
        ai_report = run_note_out_agent(
            llm, paper=paper, sections=sections, notes=notes_data,
            instructions=instructions, scholar=scholar,
            store=store, doc_id=doc_id or str(paper.get("id") or ""),
            refresh_ref_cache=refresh_references)
        report = merge_agent_report(report, ai_report)
    out = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": _now(),
        "mode": mode,
        "paper": paper,
        "sections": sections,
        "code_links": code_links,
        "reference_check": reference_check,
        "notes": notes_data,
        "ai_summary": build_ai_summary(rec),
        "report": report,
    }
    if fulltext is not None:
        out["fulltext"] = fulltext
    if figures is not None:
        out["figures"] = figures
    return out


def to_json(export: dict) -> str:
    """导出 JSON 文本（UTF-8、中文不转义、缩进 2）。"""
    return json.dumps(export, ensure_ascii=False, indent=2)


def export_filename(paper: dict | None, mode: str = "mechanical",
                    fmt: str = "json") -> str:
    """下载文件名：`<论文标题>-note-export.json` / `.png`（非法字符替换成 `_`）。

    Agent 导出会在文件名里带上 `-agent`：`<论文标题>-agent-note-export.json`；
    `fmt="image"` 时扩展名是 `.png`。
    """
    title = _norm((paper or {}).get("title")) or "paper"
    safe = re.sub(r'[\\/:*?"<>|]+', "_", title).strip(" ._")[:60] or "paper"
    tag = "-agent" if str(mode or "").strip().lower() == "agent" else ""
    ext = "png" if str(fmt or "").strip().lower() == "image" else "json"
    return f"{safe}{tag}-note-export.{ext}"


__all__ = [
    "EXPORT_FORMAT", "EXPORT_VERSION", "MODES",
    "build_sections", "build_fulltext", "build_figures", "check_references",
    "build_notes", "build_mechanical_report", "merge_agent_report",
    "build_ai_summary", "paper_authors",
    "export_paper", "to_json", "export_filename",
]
