"""给前端的视图拼装：论文行、共享状态、按页切片、transfer key 列表。"""
from __future__ import annotations

import re

from .. import auth as auth_mod, records
from ..settings import mask_key
from . import runtime


def share_state(rec: dict, me: str) -> dict:
    """给前端的共享状态：所有者是谁、我的权限、完整名单。"""
    return {
        "owner": records.owner_of(rec),
        "perm": records.perm_of(rec, me),
        "items": records.shared_entries(rec),
    }


def doc_view(rec: dict, me: str) -> dict:
    """论文列表里的一行：附上**我的**权限、我的阅读进度与共享信息。"""
    row = dict(rec)
    row.pop("ai", None)                 # 列表不需要 AI 整理正文，减体积
    row.update({k: None for k in records.READING_KEYS})
    row.update(records.reading_of(rec, me))
    row["perm"] = records.perm_of(rec, me)
    row["owner"] = records.owner_of(rec)
    row["shared"] = records.shared_entries(rec)
    row["shared_count"] = len(row["shared"])
    row["mine"] = row["perm"] == "owner"
    return row


def transfer_view(username: str) -> dict:
    """给前端/接口用的 key 列表（含完整 Key —— 只有所有者本人能看到，方便随时复制）。"""
    rows = []
    for k in runtime.auth.list_transfer_keys(username):
        raw = str(k.get("key") or "")
        rows.append({
            "id": k.get("id") or "",
            "name": k.get("name") or "",
            "key": raw,
            "key_masked": mask_key(raw),
            "created_at": k.get("created_at"),
            "last_used_at": k.get("last_used_at"),
            "uses": int(k.get("uses") or 0),
        })
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"owner": username, "keys": rows, "limit": auth_mod.TRANSFER_MAX,
            "prefix": auth_mod.TRANSFER_PREFIX}


_MATH_CMD_RE = re.compile(r"\\[A-Za-z]{2,}")


def doc_needs_math(doc: dict) -> bool:
    """全文有没有需要 KaTeX 排版的东西（与前端 `r2-math.js::docNeedsMath` 同口径）。

    逐页加载时前端只看得到首页那几页，靠它才知道要不要提前把 KaTeX 拉起来。
    宁可多判（多加载一次 KaTeX 只是慢一点），不能漏判（漏了后加载的页里公式
    会先显示成 LaTeX 源码，要等一次整篇重绘才出图）。
    """
    for pg in doc.get("pages") or []:
        if not isinstance(pg, dict):
            continue
        for box in pg.get("formula_boxes") or []:
            if isinstance(box, dict) and str(box.get("latex") or "").strip():
                return True
        for t in pg.get("texts") or []:
            if not isinstance(t, dict):
                continue
            if str(t.get("latex") or "").strip():
                return True
            if t.get("math"):
                return True
            html = t.get("html") or ""
            if "<math" in html:
                return True
            if "<table" in html and _MATH_CMD_RE.search(html):
                return True
            text = t.get("text") or ""
            if "\\(" in text or "$$" in text or "$" in text:
                return True
    return False


def doc_slice(doc: dict, page_from: int | None, page_to: int | None) -> dict:
    """把版式文档按页切片返回（阅读页逐页加载用）。

    除 `pages` 之外的原字段（title / parser / parser_info / audit …）全部照旧下发，
    另外补上：
      · `page_total`  总页数（前端据此给还没到数据的页留占位高度）
      · `page_from` / `page_to`  本次切片的半开区间 `[page_from, page_to)`
      · `page_sizes`  所有页的 `{w, h}`（很轻，用来给占位页算宽高）
      · `needs_math`  全文是否需要 KaTeX（只看首页会漏判，见 `_doc_needs_math`）
    `page_from` 可以是负数，表示“从倒数第 |page_from| 页开始”（停在文末时用）。
    """
    pages = doc.get("pages") or []
    total = len(pages)
    if page_from is None:
        start = 0
    elif page_from < 0:
        start = max(0, total + page_from)
    else:
        start = min(page_from, max(0, total - 1)) if total else 0
    if page_to is None:
        end = total
    elif page_to < 0:
        end = max(0, total + page_to)
    else:
        end = min(page_to, total)
    end = max(start, end)
    out = {k: v for k, v in doc.items() if k != "pages"}
    out["pages"] = pages[start:end]
    out["page_total"] = total
    out["page_from"] = start
    out["page_to"] = end
    out["page_sizes"] = [{"w": p.get("w"), "h": p.get("h")}
                         for p in pages if isinstance(p, dict)]
    out["needs_math"] = doc_needs_math(doc)
    return out
