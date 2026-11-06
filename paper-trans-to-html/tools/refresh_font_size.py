"""按原始 PDF 的文字层，刷新 doc.json 里每个块的**字号 / 行距**（不用重新 OCR）

**为什么需要它**：Unlimited-OCR（尤其在线解析）回包不带字号，解析侧只能「块几何 +
字符数」反解，全篇只得到一个正文基准 —— 标题会偏小、小字（Received / 图注）会偏大、
行距按 1.42 估太松。电子版 PDF 的**文字层**本身就带真实字号与基线，这里按块 bbox
把 PDF 的 span 量一遍，直接把 `texts[].size / line_h` 改成实测值：

  * 不动文本、不动块几何、不动笔记 / 高亮 / 译文 / 进度；
  * 不调用任何模型 / 在线接口，秒级完成；
  * 扫描件（没有文字层）量不到 → 该块保持原样。

用法：
    python tools/refresh_font_size.py --list
    python tools/refresh_font_size.py <doc_id> <原PDF路径>
    python tools/refresh_font_size.py <doc_id> <原PDF路径> --dry-run

⚠️ 连的是**应用用的那个库**（与 `tools/reparse_layout.py` 同一套 `backend.storage.build`）。
   Docker 部署时：`docker compose exec app python tools/refresh_font_size.py <id> /app/x.pdf`
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backend import unlimited_ocr_online as uo        # noqa: E402
from backend.storage import build as build_storage    # noqa: E402

DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"

# 行距 / 字号 的合理区间（实测正文多为 1.15～1.35）
_MIN_RATIO, _MAX_RATIO = 1.0, 2.2


def _refresh(doc: dict, pdf_path: Path) -> dict:
    """就地刷新 `pages[].texts[].size / line_h`，返回统计。"""
    import fitz

    stats = {"pages": 0, "blocks": 0, "updated": 0, "no_text_layer": 0,
             "samples": []}
    pdf = fitz.open(str(pdf_path))
    try:
        for pi, pg in enumerate(doc.get("pages") or []):
            if pi >= pdf.page_count:
                break
            stats["pages"] += 1
            page = pdf.load_page(pi)
            spans = uo._page_spans(page)             # noqa: SLF001（同一套量法）
            for item in pg.get("texts") or []:
                stats["blocks"] += 1
                try:
                    rect = (float(item["x"]), float(item["y"]),
                            float(item["x"]) + float(item["w"]),
                            float(item["y"]) + float(item["h"]))
                except (KeyError, TypeError, ValueError):
                    continue
                size, line_h = uo._rect_metrics(spans, rect)   # noqa: SLF001
                if not (4.0 <= size <= 60.0):
                    stats["no_text_layer"] += 1
                    continue
                old_size, old_lh = item.get("size"), item.get("line_h")
                if line_h and not (_MIN_RATIO <= line_h / size <= _MAX_RATIO):
                    line_h = 0.0                    # 量歪了：只改字号
                item["size"] = round(size, 2)
                if line_h:
                    item["line_h"] = round(line_h, 2)
                else:
                    ratio = ((old_lh / old_size)
                             if old_lh and old_size else 1.22)
                    item["line_h"] = round(size * max(1.0, min(2.2, ratio)), 2)
                if item.get("lines"):
                    item["lines"][0]["s"] = item["size"]
                if old_size is not None and abs(old_size - item["size"]) > 0.05:
                    stats["updated"] += 1
                if len(stats["samples"]) < 10:
                    stats["samples"].append(
                        (item.get("id"), item.get("kind"), old_size,
                         item["size"], item.get("line_h")))
    finally:
        pdf.close()
    info = doc.get("parser_info")
    if isinstance(info, dict):
        info["font_refreshed_from_pdf"] = True
    return stats


def _list_docs(store, backend_name: str) -> int:
    rows = store.list_records()
    if not rows:
        print(f"存储后端：{backend_name} —— 库里还没有论文。")
        return 0
    print(f"存储后端：{backend_name}\n")
    print(f"{'doc_id':34s} {'页':>3s}  {'后端':14s} 标题")
    for r in rows:
        doc_id = r.get("id") or r.get("_id")
        try:
            doc = store.read_doc(doc_id)
        except FileNotFoundError:
            print(f"{doc_id:34s}  版式数据缺失")
            continue
        print(f"{doc_id:34s} {len(doc.get('pages') or []):3d}  "
              f"{str(doc.get('parser')):14s} {str(r.get('title'))[:40]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="按原始 PDF 的文字层刷新每块字号/行距（不重新 OCR）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("doc_id", nargs="?", help="论文 id（--list 时不用给）")
    ap.add_argument("pdf", nargs="?", help="原始 PDF 路径")
    ap.add_argument("--list", action="store_true", help="列出库里的论文")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    store = build_storage(DATA_DIR, CONFIG_FILE)[0]
    backend_name = getattr(store, "backend_name", "?")
    if args.list:
        return _list_docs(store, backend_name)
    if not args.doc_id or not args.pdf:
        ap.print_help()
        return 2
    pdf = Path(args.pdf).expanduser()
    if not pdf.exists():
        print(f"❌ 找不到 PDF：{pdf}")
        return 1
    try:
        doc = store.read_doc(args.doc_id)
    except FileNotFoundError:
        print(f"❌ 库里没有这篇论文的版式数据：{args.doc_id}")
        return 1

    print(f"存储后端：{backend_name}")
    print(f"论文    ：{doc.get('title')}（{len(doc.get('pages') or [])} 页，"
          f"后端 {doc.get('parser')}）")
    stats = _refresh(doc, pdf)
    print(f"页 / 块  ：{stats['pages']} / {stats['blocks']}；"
          f"字号变化 {stats['updated']} 块；量不到文字层 {stats['no_text_layer']} 块")
    for did, kind, old, new, lh in stats["samples"]:
        print(f"  {did:<8} {str(kind):<9} {old} → {new}（行距 {lh}）")
    if stats["updated"] == 0:
        print("（没有可更新的块：PDF 无文字层，或字号本来就对）")
    if args.dry_run:
        print("[dry-run] 没有写入任何数据。")
        return 0
    store.save_doc(args.doc_id, doc)
    try:
        store.clear_cache(args.doc_id)             # 让运行中的服务立刻读到新数据
    except Exception:
        pass
    print("\n✅ 已写回 doc.json（文本 / 块位置 / 笔记 / 译文都没动）。刷新阅读器即可看到。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
