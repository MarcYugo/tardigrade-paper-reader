"""按原 PDF 重新解析「版式数据」，原地写回同一个 doc_id（笔记 / 译文 / 进度全保留）

**为什么需要它**：解析器升级后（比如换公式增强服务、调 DPI），
**已经上传过**的论文在库里存的是**旧**版式数据（`documents.pages`），不会自动跟着变 ——
所以「改了代码却看不到效果」十有八九是没重解析。重新上传虽然也行，但那会生成新论文，
笔记与译文全丢。这个工具用原 PDF 原地重解析：

  * `pages`（文字块/公式框/插图）+ `parser` / `parser_info` / `warnings` 全部换新；
  * 笔记、高亮、译文、阅读进度、分享状态**一个不动**；
  * 译文是按「块文本的 sha1」缓存的，解析升级可能改块文本（重切句等）→ 这里会自动把
    老译文的键迁到新文本上（对得上就不用重译）。

用法：
    python tools/reparse_layout.py --list                        # 看有哪些论文、什么后端
    python tools/reparse_layout.py <doc_id> 原PDF.pdf             # 原地重解析
    python tools/reparse_layout.py <doc_id> 原PDF.pdf --dry-run   # 只看会变什么，不写库

⚠️ 连的是**应用用的那个库**：直连 MongoDB 需要环境变量（`MONGO_HOST` / `MONGO_URI` 等），
   没配就会回退到本地 JSON（看不到你库里的论文）。用 Docker 部署时更省事：

       docker compose exec app python tools/reparse_layout.py <doc_id> /app/xxx.pdf

   或在网页里用阅读器右上角「🔄 重新解析版式」（同一套逻辑，上传同一个 PDF 即可）。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backend import converter                      # noqa: E402
from backend import pdf_parser                     # noqa: E402
from backend.storage import build as build_storage  # noqa: E402

DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"


def _formula_box_count(pages) -> int:
    """统计页级公式覆盖框。"""
    return sum(len(pg.get("formula_boxes") or []) for pg in pages or [])


def _compare(old_pages: list, new_pages: list, parser: str | None,
             parser_info: dict, warnings: list) -> dict:
    """把「新旧版式」的差异整成一份摘要(打印/接口都用它)。"""
    old_texts = converter._block_texts(old_pages)       # noqa: SLF001
    new_texts = converter._block_texts(new_pages)       # noqa: SLF001
    return {
        "parser": parser,
        "parser_info": parser_info or {},
        "warnings": warnings or [],
        "pages": [len(old_pages), len(new_pages)],
        "blocks": [len(old_texts), len(new_texts)],
        "changed": sum(1 for k, v in new_texts.items()
                       if k in old_texts and old_texts[k] != v),
        "added": sum(1 for k in new_texts if k not in old_texts),
        "gone": sum(1 for k in old_texts if k not in new_texts),
        "latex_blocks": [converter._latex_count(old_pages),      # noqa: SLF001
                         converter._latex_count(new_pages)],     # noqa: SLF001
        "formula_boxes": [_formula_box_count(old_pages),
                          _formula_box_count(new_pages)],
    }


def _print_summary(summary: dict) -> None:
    info = summary.get("parser_info") or {}
    pages = summary.get("pages") or [0, 0]
    blocks = summary.get("blocks") or [0, 0]
    latex = summary.get("latex_blocks") or [0, 0]
    print(f"解析后端  ：{summary.get('parser')}  {info}")
    print(f"页数      ：{pages[0]} → {pages[1]}")
    print(f"文字块    ：{blocks[0]} → {blocks[1]}"
          f"（内容变了 {summary.get('changed', 0)}，新增 {summary.get('added', 0)}，"
          f"不再存在 {summary.get('gone', 0)}）")
    print(f"公式擦除  ：{info.get('formula_spans', 0)} 段 run / "
          f"{info.get('formula_chars', 0)} 字 / {info.get('formula_lines', 0)} 行"
          + (f"；带 latex 的块 {latex[0]} → {latex[1]}" if latex[1] else ""))
    if info.get("surya_dpi") or info.get("surya_blocks"):
        # Surya 整页 OCR 后端（见 pdf_parser/backend_surya.py）：块/图片/耗时
        print(f"Surya 2   ：{info.get('surya_blocks', 0)} 块（插图 "
              f"{info.get('surya_images', 0)} 张），dpi {info.get('surya_dpi')}，"
              f"{info.get('surya_ms', 0)} ms")
    if info.get("unlimited_ocr_dpi") or info.get("unlimited_ocr_blocks"):
        # Unlimited-OCR 整页 OCR 后端（见 pdf_parser/backend_unlimited_ocr.py）：
        # route=local 本地推理服务 / route=online 百度智能云在线解析
        print(f"Unlimited ：{info.get('unlimited_ocr_blocks', 0)} 块（插图 "
              f"{info.get('unlimited_ocr_images', 0)} 张），route "
              f"{info.get('unlimited_ocr_route', '?')}，dpi "
              f"{info.get('unlimited_ocr_dpi')}，{info.get('unlimited_ocr_ms', 0)} ms")
    if info.get("formula_slots"):
        print(f"公式空位  ：{info['formula_slots']} 处")
    if info.get("ghost_text_removed"):
        print(f"去重影    ：按插图剔除 {info['ghost_text_removed']} 行文字")
    for w in summary.get("warnings") or []:
        print(f"[警告]    ：{w}")


def _list_docs(store, backend_name: str) -> int:
    rows = store.list_records()
    if not rows:
        print(f"存储后端：{backend_name} —— 库里还没有论文。")
        print("（论文若其实在 MongoDB 里，请带上 MONGO_* 环境变量再跑，"
              "或用 docker compose exec app 在容器里跑）")
        return 0
    print(f"存储后端：{backend_name}\n")
    print(f"{'doc_id':34s} {'页':>3s}  {'后端':10s} {'带公式的块':>10s}  标题")
    for r in rows:
        doc_id = r.get("id") or r.get("_id")
        try:
            doc = store.read_doc(doc_id)
        except FileNotFoundError:
            print(f"{doc_id:34s}  版式数据缺失")
            continue
        pages = doc.get("pages") or []
        print(f"{doc_id:34s} {len(pages):3d}  {str(doc.get('parser')):10s} "
              f"{converter._latex_count(pages):10d}  "      # noqa: SLF001
              f"{str(r.get('title'))[:40]}")
    print("\n重解析：python tools/reparse_layout.py <doc_id> <原PDF路径>")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="用原 PDF 原地重解析版式数据（保留笔记/译文/进度）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("doc_id", nargs="?", help="论文 id（--list 时不用给）")
    ap.add_argument("pdf", nargs="?", help="原始 PDF 路径")
    ap.add_argument("--list", action="store_true", help="列出所有论文与现用解析后端")
    ap.add_argument("--dry-run", action="store_true", help="只解析对比，不写库")
    ap.add_argument("--backend", default=None,
                    choices=["auto", "surya", "unlimited_ocr", "pymupdf"],
                    help="指定解析后端；不传则按 config.json / 环境变量(默认 auto)")
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
        old = store.read_doc(args.doc_id)
    except FileNotFoundError:
        print(f"❌ 库里没有这篇论文的版式数据：{args.doc_id}")
        return 1
    old_pages = old.get("pages") or []
    print(f"存储后端  ：{backend_name}")
    print(f"论文      ：{old.get('title')}  （{len(old_pages)} 页，"
          f"当前后端 {old.get('parser')}）")
    print(f"重新解析  ：{pdf.name} …")

    if args.dry_run:
        # 同样的解析，但不写库、不碰插图
        with tempfile.TemporaryDirectory(prefix="reparse-") as staging:
            res = pdf_parser.parse_pdf(pdf, Path(staging), backend=args.backend)
        if not res.get("pages"):
            print("❌ 没能解析出任何页面。")
            return 1
        _print_summary(_compare(old_pages, res["pages"], res.get("parser"),
                                res.get("parser_info"), res.get("warnings")))
        print("\n[dry-run] 没有写入任何数据。")
        return 0

    try:
        summary = converter.reparse(store, args.doc_id, pdf, backend=args.backend)
    except ValueError as exc:
        print(f"❌ {exc}\n   （库里的数据没有改动）")
        return 1
    _print_summary(summary)
    print("\n✅ 已写回版式数据（doc_id 不变，笔记 / 高亮 / 进度都保留）")
    print(f"   插图：{summary.get('images', 0)} 张")
    print(f"   译文：迁移 {summary.get('translations_moved', 0)} 条到新块文本上")
    print("   到浏览器里刷新该论文即可看到新解析结果（公式由 KaTeX 渲染）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
