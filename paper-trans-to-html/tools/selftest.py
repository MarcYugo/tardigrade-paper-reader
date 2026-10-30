"""解析自检：不启动服务，直接验证“模块一 · 文档解析”。

运行:
    python tools/selftest.py [pdf路径] [--backend auto|detection_service_group|surya|unlimited_ocr|pymupdf] [--dpi 150]
默认用 samples/sample.pdf；backend 留空时按 config.json / 环境变量(默认 auto)。
输出: 实际使用的后端、页数、文字块/图片数、版面标签统计，
      并把图片落盘到 data/selftest_images/。

`pymupdf` / `auto` / `detection_service_group` 走**本地解析方案**：PyMuPDF 文字层 +
`local_parse_pipeline.process_pages()`（texify 公式 / OXR 版面 + SLANet 表格 / 插图），
需要本机模型权重与依赖（见 `backend/pdf_parser/local_parse_pipeline/README.md`）：
    python tools/selftest.py samples/latex_sample.pdf --backend pymupdf
    python tools/selftest.py samples/latex_sample.pdf --backend surya      # 需 Surya 服务
    python tools/selftest.py samples/latex_sample.pdf --backend unlimited_ocr
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from backend import pdf_parser  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="文档解析模块自检")
    ap.add_argument("pdf", nargs="?", default=None,
                    help="PDF 路径(默认 samples/sample.pdf)")
    ap.add_argument("--backend", default=None,
                    choices=["auto", "detection_service_group", "surya",
                             "unlimited_ocr", "pymupdf"],
                    help="解析后端；不传则按 config.json / 环境变量(默认 auto)")
    ap.add_argument("--dpi", type=int, default=None, help="公式检测/识别渲染 DPI")
    args = ap.parse_args()

    pdf = Path(args.pdf) if args.pdf else BASE / "samples" / "sample.pdf"
    if not pdf.is_absolute():
        pdf = (Path.cwd() / pdf).resolve()
    if not pdf.exists():
        print(f"找不到 PDF：{pdf}\n请先运行：python tools/make_sample_pdf.py")
        sys.exit(1)

    img_dir = BASE / "data" / "selftest_images"
    options = {"dpi": args.dpi} if args.dpi else None
    res = pdf_parser.parse_pdf(pdf, img_dir, backend=args.backend, options=options)

    print(f"文件      : {pdf}")
    print(f"解析后端  : {res.get('parser')}"
          + (f"  {res.get('parser_info')}" if res.get("parser_info") else ""))
    for warn in res.get("warnings") or []:
        print(f"[警告]    : {warn}")
    print(f"页数      : {res['num_pages']}")
    print(f"页面尺寸  : {res['page_w']:.1f} x {res['page_h']:.1f} pt")
    for i, pg in enumerate(res["pages"]):
        n_txt = sum(1 for t in pg["texts"] if any(ln["runs"] for ln in t["lines"]))
        extra = f", 版面标签 {pg['block_labels']}" if pg.get("block_labels") else ""
        print(f"  第{i+1}页: 文字块 {len(pg['texts'])}(非空 {n_txt}) 个, "
              f"图片 {len(pg['images'])} 张{extra}")
        for t in pg["texts"][:3]:
            print(f"      [{t.get('label') or '-'}] {t['text'][:60]!r}  "
                  f"@({t['x']:.0f},{t['y']:.0f})")
    imgs = (list(img_dir.glob("*.png")) + list(img_dir.glob("*.jp*g"))
            + list(img_dir.glob("*.webp")))
    print(f"落盘图片  : {len(imgs)} 张 -> {img_dir}")


if __name__ == "__main__":
    main()
