"""给已入库的 Surya 论文补跑「参考文献分块」（解析期派生步骤，不重新 OCR）

**为什么需要它**：`refs_surya.split_surya_reference_blocks` 是**解析期**跑的派生
步骤（把整页一条的参考文献 OCR 块按条目拆成独立块，见 `backend/pdf_parser/entry.py`）。
解析器升级后（例如新支持作者-年份制 `[Smith et al., 2020]` 条目、以及行首直接是
作者名的无标签作者-年份制条目的拆分），
**已经上传过**的论文库里存的还是旧版式数据，不会自动跟着变 —— 不重新 OCR、
不重新上传，用这个工具把分块补齐：

  * 只动 `pages[].texts`（拆分产生的新块）与 `parser_info` 里的统计键
    （`surya_blocks` / `surya_kind_*`，口径与解析时一致）；
  * 文字、公式、插图、笔记、高亮、译文、阅读进度**一个不动**；
  * **幂等**：已经拆好的文档再跑一遍不会重复拆。

用法：
    python tools/split_ref_blocks.py --list               # 看每篇会补出多少块
    python tools/split_ref_blocks.py <doc_id>             # 补跑一篇
    python tools/split_ref_blocks.py <doc_id> --dry-run   # 只看差异，不写库
    python tools/split_ref_blocks.py --all                # 有增量的全补

⚠️ 连的是**应用用的那个库**（参数按 `backend/db.py` 的顺序解析；Docker 部署时
   可 `docker compose exec app python tools/split_ref_blocks.py <doc_id>`）。
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backend.pdf_refs.refs_surya import split_surya_reference_blocks  # noqa: E402
from backend.storage import build as build_storage  # noqa: E402

DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"


def _block_count(pages) -> int:
    return sum(len(pg.get("texts") or []) for pg in pages or [])


def _apply(doc: dict) -> tuple[dict, dict]:
    """返回 `(补跑后的 doc 副本, added)`；`added` 为空 = 无需改动。"""
    patched = copy.deepcopy(doc)
    pages = patched.get("pages") or []
    added = split_surya_reference_blocks(pages)
    if not added:
        return doc, {}
    info = patched.setdefault("parser_info", {})
    info["surya_blocks"] = sum(
        len(page.get("texts") or []) + len(page.get("images") or [])
        for page in pages
    )
    for kind, count in added.items():
        key = f"surya_kind_{kind}"
        info[key] = int(info.get(key) or 0) + count
    return patched, added


def _list_docs(store) -> int:
    rows = store.list_records()
    if not rows:
        print("库里还没有论文。")
        return 0
    print(f"共 {len(rows)} 篇：\n")
    for r in rows:
        doc_id = r.get("id") or r.get("_id")
        try:
            doc = store.read_doc(doc_id)
        except FileNotFoundError:
            print(f"{doc_id:34s} 版式数据缺失")
            continue
        parser = doc.get("parser") or "?"
        _, added = _apply(doc)
        flag = "" if added else "   （无需补跑）"
        if parser != "surya":
            flag = "   （非 surya 后端，跳过）" if flag else flag
        print(f"{doc_id:34s} parser={parser:8s} "
              f"块 {_block_count(doc.get('pages'))} → "
              f"{_block_count(doc.get('pages')) + sum(added.values())}"
              f"  added={added or '{}'}  {str(r.get('title'))[:36]}{flag}")
    return 0


def _patch_one(store, doc_id: str, *, dry_run: bool) -> int:
    try:
        doc = store.read_doc(doc_id)
    except FileNotFoundError:
        print(f"❌ 库里没有这篇论文：{doc_id}")
        return 1
    if (doc.get("parser") or "") != "surya":
        print(f"这篇论文的解析后端是 {doc.get('parser')!r}，分块只对 surya 有意义。")
        return 0
    patched, added = _apply(doc)
    before = _block_count(doc.get("pages"))
    after = _block_count(patched.get("pages"))
    print(f"论文    ：{doc.get('title')}")
    print(f"文字块  ：{before} → {after}（新增拆分块 {sum(added.values())}，"
          f"added={added or '{}'}）")
    if not added:
        print("没有可补的分块，不动。")
        return 0
    if dry_run:
        print("--dry-run：不写库。")
        return 0
    store.save_doc(doc_id, patched)
    print("✅ 已写回（阅读器刷新后生效；笔记/译文/进度不受影响）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="给已入库论文补跑参考文献分块")
    ap.add_argument("doc_id", nargs="?", help="论文 id（--list / --all 时不用给）")
    ap.add_argument("--list", action="store_true", help="列出所有论文及补跑增量")
    ap.add_argument("--all", action="store_true", help="把所有有增量的论文都补上")
    ap.add_argument("--dry-run", action="store_true", help="只看差异，不写库")
    args = ap.parse_args()

    store = build_storage(DATA_DIR, CONFIG_FILE)[0]
    backend = getattr(store, "backend_name", "?")
    print(f"存储后端：{backend}\n")
    if args.list:
        return _list_docs(store)
    if args.all:
        rows = store.list_records()
        if not rows:
            print("库里还没有论文。")
            return 0
        rc = 0
        for r in rows:
            print("-" * 60)
            rc |= _patch_one(store, r.get("id") or r.get("_id"),
                             dry_run=args.dry_run)
        return rc
    if not args.doc_id:
        ap.print_help()
        return 2
    return _patch_one(store, args.doc_id, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
