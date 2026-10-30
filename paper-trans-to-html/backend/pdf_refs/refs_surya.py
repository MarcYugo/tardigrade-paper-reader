"""Surya-specific citation extraction.

Surya can return a whole bibliography page as one text block. Unlike the
PyMuPDF strategy, this strategy splits numbered entries, author-year entries
labelled `[Smith et al., 2020]`, and **unlabelled** author-year entries
(`Manan Agarwal, … arXiv preprint arXiv:2606.24773, 2026.`, one per line) within
each block before mapping body citations.
"""
from __future__ import annotations

import re
import math

from .refs import (
    _RE_AY_START,
    _RE_CITE_TOKEN,
    _RE_YEAR,
    _attach_uris,
    _author_words,
    _build_entries,
    _canonical,
    _derive_labels,
    _expand_nums,
    _find_references_start,
    _first_author_surname,
    _map_links,
    _merge_links,
    _region_blocks,
    _scan_author_year_citations,
)

__all__ = ["build_surya_refs", "split_surya_reference_blocks"]

_RE_ENTRY_LINE = re.compile(
    r"(?m)^[ \t]*(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3})\s*[.)])[ \t]*"
)
# 作者-年份制条目开头：`[Smith et al., 2020] 正文…`（AAAI/IJCAI 等模板的写法）
_RE_ENTRY_LINE_AY = re.compile(
    r"(?m)^[ \t]*\[([A-Z][^\[\]\n]{1,120}?,\s*(?:19|20)\d{2}[a-z]?)\][ \t]*(?=\S)"
)
# 上一行“已经写完”的行尾特征：句点 / 右括号 / 右方括号（Surya 常把反向页码
# 标记 `(page 6)` 留在条末，所以 `)` 也算写完了）。
_RE_ENTRY_LINE_DONE = re.compile(r"(?:[.!?]|[)\]])[ \t]*$")
# 以出版信息开头的行（折行条目的续行）：即使上一行“写完了”也不当条目首行，
# 免得把一条文献劈成两条。
_AY_LINE_HEAD_STOP = frozenset({
    "in", "proceedings", "advances", "international", "journal", "transactions",
    "conference", "workshop", "arxiv", "url", "volume", "vol", "pages", "pp",
    "ieee", "acm", "springer", "elsevier", "mit", "oxford", "cambridge",
    "nature", "science", "pmlr", "pmr", "technical", "report", "preprint",
    "abstract", "also", "see", "and", "the", "of", "on", "for", "with",
})


def _looks_like_ay_entry_line(line: str) -> bool:
    """这一行像不像“作者-年份制条目”的首行（行首是作者姓名）。"""
    head = line.lstrip()
    if not _RE_AY_START.match(head):
        return False
    word = re.match(r"[A-Za-z][A-Za-z'’\-]*", head)
    return bool(word) and word.group(0).lower() not in _AY_LINE_HEAD_STOP


def _plain_ay_starts(text: str) -> list[int]:
    """无标签作者-年份条目的行首 offset（ACL/arXiv 风格：条目首行直接是作者名）。

    Surya 会把整页参考文献并成一个 `list` 块、**一条一行**，行内没有几何信息，
    只能按行判断。两个条件同时成立才算条目开头：

      1. 行首像作者姓名（`_RE_AY_START`，且不是 `Machine Learning,` 这种出版信息）；
      2. **上一行已经写完**（行尾是句点/右括号，或上一行里已出现年份）。

    只按第 1 条会把折行条目劈开（`… Advances in Neural Information` +
    `Processing Systems, 35:7796–7809, 2022.` 两行都以大写词开头）。
    """
    starts: list[int] = []
    prev_done = True                 # 块首行总是候选
    pos = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        if line.strip():
            if prev_done and _looks_like_ay_entry_line(line):
                starts.append(pos)
            prev_done = bool(_RE_ENTRY_LINE_DONE.search(line)
                             or _RE_YEAR.search(line))
        pos += len(raw)
    return starts


def _entry_segments(text: str):
    """Yield each reference's span in Surya's newline-preserving text.

    Spans are `(start, end, number)`; `number` is None for author-year entries
    (`[Smith et al., 2020] Title…`, or unlabelled `Manan Agarwal, …`), whose
    label/author name takes the number's place.
    编号条目与作者-年份标签不混排：一段文本里认到编号开头就只按编号切
    （编号列表里的正文引用行不该再把条目切碎）；没有标签时才按“作者姓名开头
    的行”切。
    """
    starts = [(match.start(),
               int(next(group for group in match.groups() if group)))
              for match in _RE_ENTRY_LINE.finditer(text)]
    if not starts:
        starts = [(match.start(), None)
                  for match in _RE_ENTRY_LINE_AY.finditer(text)]
    if not starts:
        starts = [(start, None) for start in _plain_ay_starts(text)]
    if not starts:
        return []
    starts.sort(key=lambda item: item[0])
    return [
        (start, starts[i + 1][0] if i + 1 < len(starts) else len(text), number)
        for i, (start, number) in enumerate(starts)
    ]


def _slice_runs(runs: list[dict], start: int, end: int) -> list[dict]:
    sliced = []
    offset = 0
    for run in runs:
        text = str(run.get("t") or "")
        left = max(start, offset)
        right = min(end, offset + len(text))
        if left < right:
            sliced.append(dict(run, t=text[left - offset:right - offset]))
        offset += len(text)
    return sliced


def _estimated_reference_lines(text: str, width: float, size: float) -> int:
    capacity = max(1.0, width / max(1.0, size * 0.55))
    lines = text.splitlines() or [text]
    return max(1, sum(max(1, math.ceil(len(line) / capacity)) for line in lines))


def split_surya_reference_blocks(pages: list[dict]) -> dict[str, int]:
    """Split bibliography OCR blocks into individually positioned blocks.

    Three entry styles are recognised: numbered (`[1]` / `1.`), `[Author, year]`
    labelled author-year, and unlabelled author-year (each entry starts a line
    with the author names, see `_plain_ay_starts`).
    """
    start = _find_references_start(pages)
    region_ids = {
        str(block.get("id") or "")
        for _, _, block in _region_blocks(pages, start)
    }
    added_by_kind: dict[str, int] = {}

    for page in pages:
        split_texts = []
        for block in page.get("texts") or []:
            block_id = str(block.get("id") or "")
            canonical = _canonical(block, True)
            segments = _entry_segments(canonical) if block_id in region_ids else []
            if len(segments) < 2:
                split_texts.append(block)
                continue

            if canonical[:segments[0][0]].strip():
                segments[0] = (0, segments[0][1], segments[0][2])
            spans = []
            for index, (span_start, span_end, number) in enumerate(segments):
                while span_start < span_end and canonical[span_start] in "\r\n":
                    span_start += 1
                while span_end > span_start and canonical[span_end - 1] in "\r\n":
                    span_end -= 1
                text = canonical[span_start:span_end]
                if text.strip():
                    spans.append((span_start, span_end, number, text, index))
            if len(spans) < 2:
                split_texts.append(block)
                continue

            width = max(1.0, float(block.get("w") or 1.0))
            size = max(1.0, float(block.get("size") or 10.0))
            weights = [
                _estimated_reference_lines(text, width, size)
                for _, _, _, text, _ in spans
            ]
            total_weight = sum(weights)
            y = float(block.get("y") or 0.0)
            height = max(0.0, float(block.get("h") or 0.0))
            cursor = 0
            source_line = ((block.get("lines") or [{}])[0])
            source_runs = source_line.get("runs") or []
            source_math = block.get("math") or []
            parent_id = block_id or f"p{page.get('page', 0)}bref"

            for (span_start, span_end, number, text, index), weight in zip(spans, weights):
                top = y + height * cursor / total_weight
                box_height = height * weight / total_weight
                cursor += weight
                child_id = f"{parent_id}-ref{index + 1}"
                child_runs = _slice_runs(source_runs, span_start, span_end)
                child = dict(block)
                child.update({
                    "id": child_id,
                    "text": text,
                    "math": [
                        dict(item, a=item["a"] - span_start, b=item["b"] - span_start)
                        for item in source_math
                        if span_start <= item.get("a", -1)
                        and item.get("b", 0) <= span_end
                    ],
                    "html": "",
                    "y": round(top, 3),
                    "h": round(box_height, 3),
                    "ref_entry": True,
                    "ref_number": number,
                    "ref_parent": parent_id,
                    "lines": [dict(
                        source_line,
                        x=block.get("x", 0),
                        y=round(top, 3),
                        w=block.get("w", 0),
                        h=round(box_height, 3),
                        runs=child_runs,
                    )],
                })
                split_texts.append(child)
            added_by_kind[block.get("kind") or "text"] = (
                added_by_kind.get(block.get("kind") or "text", 0) + len(spans) - 1
            )

        page["texts"] = split_texts
        page["text"] = "\n\n".join(
            str(block.get("text") or "") for block in split_texts
            if block.get("text")
        )

    return added_by_kind


def _build_surya_entries(region):
    entries: list[dict] = []
    block_entries: dict[str, list[int]] = {}
    region_block_ids: set[str] = set()

    def append_text(entry, text):
        text = " ".join(text.split())
        if text and len(entry["text"]) < 1500:
            entry["text"] = (entry["text"] + " " + text).strip()[:1500]

    for pi, _, block in region:
        bid = str(block.get("id") or f"p{pi}b{len(entries)}")
        region_block_ids.add(bid)
        canonical = _canonical(block, True)
        if not canonical.strip() or canonical.strip().isdigit() or len(canonical.strip()) < 4:
            continue
        segments = _entry_segments(canonical)

        if not segments:
            # 认不出条目开头：解析期已按条目拆过块的（`ref_entry`）**整块就是一条**；
            # 其余（跨页续写、碎片）并进上一条。以前一律并入上一条，未拆分的
            # 作者-年份制块会整块丢掉。
            if entries and not block.get("ref_entry"):
                entry = entries[-1]
                if bid not in entry["blocks"]:
                    entry["blocks"].append(bid)
                append_text(entry, canonical)
                block_entries.setdefault(bid, []).append(entry["i"])
                continue
            segments = [(0, len(canonical), None)]

        prefix = canonical[:segments[0][0]]
        if prefix.strip() and entries:
            entry = entries[-1]
            if bid not in entry["blocks"]:
                entry["blocks"].append(bid)
            append_text(entry, prefix)
            block_entries.setdefault(bid, []).append(entry["i"])

        for start, end, number in segments:
            text = " ".join(canonical[start:end].split())
            if len(text) < 4:
                continue
            entry = {
                "i": len(entries),
                "n": number,
                "page": pi,
                "blocks": [bid],
                "text": text[:1500],
                "uri": "",
                # 与通用实现同口径（见 `refs._build_entries`）：`label` 给前端卡片徽标
                # （`Agarwal 2026`），`author_words`/`first_author` 给作者-年份匹配
                # 的“强匹配”档 —— 缺了它们只能走弱匹配，共同作者会把同年的其他文献
                # 一起挂到引用上。
                "label": "",
                "author_words": _author_words(text),
                "first_author": _first_author_surname(text),
            }
            entries.append(entry)
            block_entries.setdefault(bid, []).append(entry["i"])

    _derive_labels(entries)      # 无标签条目补 `[Agarwal, 2026]`，前端才好显示
    return entries, block_entries, region_block_ids


def _map_surya_citations(pages, entries, region_block_ids):
    num_to_entry: dict[int, int] = {}
    for entry in entries:
        number = entry.get("n")
        if number is not None:
            num_to_entry.setdefault(number, entry["i"])
    if not num_to_entry:
        return []

    links = {}
    for page_index, page in enumerate(pages):
        for block in page.get("texts") or []:
            block_id = str(block.get("id") or "")
            if not block_id or block_id in region_block_ids:
                continue
            canonical = _canonical(block, True)
            for match in _RE_CITE_TOKEN.finditer(canonical):
                numbers = sorted(
                    number for number in _expand_nums(match.group(1))
                    if number in num_to_entry
                )
                if not numbers:
                    continue
                refs = sorted(
                    {num_to_entry[number] for number in numbers},
                    key=lambda i: (entries[i].get("n") or 0, i),
                )
                links[(block_id, match.start(), match.end())] = {
                    "page": page_index,
                    "block": block_id,
                    "start": match.start(),
                    "end": match.end(),
                    "token": match.group(0),
                    "refs": refs,
                    "nums": numbers,
                }
    return sorted(links.values(),
                  key=lambda link: (link["page"], link["block"], link["start"]))


def build_surya_refs(doc: dict) -> dict:
    pages = doc.get("pages") or []
    info = {
        "ok": True,
        "scan": "ok",
        "section_page": None,
        "entries": [],
        "links": [],
        "counts": {},
    }
    if not isinstance((doc.get("parser_info") or {}).get("links_total"), int):
        info["scan"] = "no-links-data"

    start = _find_references_start(pages)
    region = _region_blocks(pages, start)
    if not region:
        info["counts"] = {"entries": 0, "links": 0, "skipped": 0}
        return info

    entries, block_entries, region_block_ids = _build_surya_entries(region)
    if not entries:
        entries, by_block = _build_entries(region)
        block_entries = {block_id: [index] for block_id, index in by_block.items()}
        region_block_ids.update(block_id for _, _, block in region
                                if (block_id := str(block.get("id") or "")))
    info["section_page"] = start[0] if start is not None else (
        entries[0]["page"] if entries else None
    )
    # A multi-entry OCR block cannot identify which entry a PDF target or URI
    # belongs to geometrically. Preserve goto mapping for unambiguous blocks;
    # numeric and author-year citations are also scanned independently so mixed
    # PDFs do not lose unlinked citations.
    unique_block_entries = {
        block_id: indices[0]
        for block_id, indices in block_entries.items()
        if len(set(indices)) == 1
    }
    linked, skipped = _map_links(
        pages, True, entries, unique_block_entries,
        exclude_source_blocks=region_block_ids,
    )
    numeric = _map_surya_citations(pages, entries, region_block_ids)
    author_year = _scan_author_year_citations(pages, True, entries, region_block_ids)
    links = _merge_links(linked, numeric, author_year)

    _attach_uris(pages, entries, unique_block_entries)

    info["entries"] = entries
    info["links"] = links
    info["counts"] = {
        "entries": len(entries),
        "links": len(links),
        "skipped": skipped,
    }
    return info
