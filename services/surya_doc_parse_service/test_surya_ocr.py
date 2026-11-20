"""
Surya 2 (surya-ocr 0.22.x) 解析 PDF —— 使用本地已下载模型权重

模型文件(本脚本同级 checkpoints/ 下):
    checkpoints/surya_ocr_2/surya-2.gguf            OCR/版面/表格 VLM 主权重
    checkpoints/surya_ocr_2/surya-2-mmproj.gguf     视觉投影 (mmproj)
    (可选) checkpoints/surya_layout2/                rf-detr 快速版面
    (可选) checkpoints/ocr_error_detection/          OCR 纠错

运行前提:
    1) surya-ocr 已升级到 0.22.x (Surya 2 API):  pip install -U surya-ocr
    2) 推理后端 (SuryaInferenceManager 会自动拉起):
         - CPU/本机  : llama.cpp 的 llama-server
           从 https://github.com/ggml-org/llama.cpp/releases 下载 Windows
           版本并加入 PATH (或用 CUDA 版走 GPU)
         - NVIDIA GPU: vllm + Docker (Linux/WSL)
    3) 放好 test.pdf 后运行:
           python test_surya_ocr.py
       其它示例:
           python test_surya_ocr.py --input xx.pdf --pages 0-2 --dpi 96
           python test_surya_ocr.py --backend vllm
           # 连接手动启动好的 llama-server / vllm (不再自动拉起):
           python test_surya_ocr.py --url http://127.0.0.1:8080/v1

要点:
    * 通过环境变量 SURYA_GGUF_LOCAL_MODEL_PATH / SURYA_GGUF_LOCAL_MMPROJ_PATH
      指向本地 gguf, 避免再次联网下载。
    * 本示例用"整页 OCR"(full-page): 每页一次 VLM 调用, 返回按阅读顺序
      排列的 block(每个 block 含 label + html, 表格为 <table>, 公式为 <math>)。
    * 图片类 block(Picture/Figure/Diagram/...)会按 bbox 从页面上裁剪出来,
      单独存为 PNG(JSON 里记录相对路径)。
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CKPT_DIR = HERE / "checkpoints"

# 版面标签里属于"图片类"的 canonical 名称(见 surya.layout.label.LAYOUT_PRED_RELABEL):
#   Image -> Picture, Complex-Block/Figure -> Figure, Diagram, Chemical-Block -> ChemicalBlock
# 这些 block 只会返回一段(几乎不含文字的)HTML, 真正的图需要按 bbox 从页面上裁剪
DEFAULT_IMAGE_LABELS = ("Picture", "Figure", "Diagram", "ChemicalBlock")


def resolve_image_labels(spec: str | None) -> set[str]:
    """'Picture,Figure' -> {'Picture','Figure'}; None = 用默认图片类标签"""
    if not spec:
        return set(DEFAULT_IMAGE_LABELS)
    return {s.strip() for s in spec.split(",") if s.strip()}


def safe_name(label: str) -> str:
    """标签转成可用作文件名的片段"""
    return re.sub(r"[^\w.-]+", "_", label or "block").strip("_") or "block"


def crop_block(image, polygon, pad: int = 4):
    """按 block 的 polygon(已是页面图像素坐标)裁剪。

    返回 (bbox, crop): bbox = [x0, y0, x1, y1]; 区域退化/越界时 crop 为 None
    """
    if not polygon:
        return None, None
    xs = [float(p[0]) for p in polygon]
    ys = [float(p[1]) for p in polygon]
    x0 = max(0, int(min(xs)) - pad)
    y0 = max(0, int(min(ys)) - pad)
    x1 = min(int(image.size[0]), int(max(xs)) + pad)
    y1 = min(int(image.size[1]), int(max(ys)) + pad)
    bbox = [x0, y0, x1, y1]
    if x1 <= x0 or y1 <= y0:
        return bbox, None
    return bbox, image.crop((x0, y0, x1, y1))


def save_block_image(crop, image_dir: Path, stem: str, page: int,
                     order, label: str) -> str:
    """把插图裁剪存成 PNG, 返回文件名(相对插图目录)"""
    image_dir.mkdir(parents=True, exist_ok=True)
    order_tag = f"{order:03d}" if isinstance(order, int) else "na"
    name = f"{stem}_p{page:03d}_b{order_tag}_{safe_name(label)}.png"
    crop.save(image_dir / name)
    return name


def parse_pages(spec: str | None) -> list[int] | None:
    """'0,2-5' -> [0,2,3,4,5]; 返回 None 表示全部页"""
    if not spec:
        return None
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return pages


def html_to_text(html: str | None) -> str:
    """把 block 的 html 粗略转成纯文本(保留段落/表格单元格/换行)"""
    if not html:
        return ""
    h = html
    h = re.sub(r"</(p|div|tr|li)>", "\n", h, flags=re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"</(td|th)>", " | ", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    h = (h.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'"))
    h = re.sub(r"[ \t]+\n", "\n", h)
    h = re.sub(r"\n{2,}", "\n", h)
    return h.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Surya 2 本地模型解析 PDF")
    parser.add_argument("--input", default="test.pdf", help="PDF 路径, 默认 test.pdf")
    parser.add_argument("--pages", default=None, help="页码(从0起), 如 '0,2-5'; 默认全部")
    parser.add_argument("--dpi", type=int, default=192, help="渲染 DPI, 96~192")
    parser.add_argument("--backend", default="llamacpp",
                        help="推理后端: llamacpp(默认) / vllm")
    parser.add_argument("--url", default=None,
                        help="连接手动启动的推理服务器(OpenAI 兼容), 如 "
                             "http://127.0.0.1:8080/v1; 设置后不再自动拉起后端")
    parser.add_argument("--output", default=None, help="结果 JSON 输出路径")
    parser.add_argument("--image-dir", default=None,
                        help="插图输出目录, 默认 <输入名>_images(与 JSON 同目录)")
    parser.add_argument("--no-images", action="store_true",
                        help="不裁剪/保存图片类 block 的插图")
    parser.add_argument("--image-labels", default=None,
                        help="视为图片的 block 标签(逗号分隔), 默认 "
                             + ",".join(DEFAULT_IMAGE_LABELS))
    parser.add_argument("--image-pad", type=int, default=4,
                        help="插图裁剪四周外扩像素, 默认 4")
    parser.add_argument("--min-image-size", type=int, default=32,
                        help="宽或高小于该值(px)的插图跳过, 默认 32")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = HERE / input_path
    if not input_path.exists():
        sys.exit(f"[错误] 找不到输入 PDF: {input_path}")

    # ---------- 关键: 必须在 import surya 之前设环境变量 ----------
    os.environ["SURYA_INFERENCE_BACKEND"] = args.backend

    if args.url:
        # 连接外部已启动的 llama-server / vllm: 不需要本地 gguf, 也不自动拉起
        os.environ["SURYA_INFERENCE_URL"] = args.url
        model_desc = f"连接外部服务器 {args.url}"
    else:
        # 让 Surya 自动拉起后端: 需指向本地模型文件
        ocr_ckpt = CKPT_DIR / "surya_ocr_2"
        model_file = ocr_ckpt / "surya-2.gguf"
        mmproj_file = ocr_ckpt / "surya-2-mmproj.gguf"
        missing = [str(f) for f in (model_file, mmproj_file) if not f.exists()]
        if missing:
            sys.exit("[错误] 缺少本地模型文件:\n    " + "\n    ".join(missing)
                     + "\n请把 surya_ocr_2 权重放到: " + str(ocr_ckpt))
        os.environ["SURYA_GGUF_LOCAL_MODEL_PATH"] = str(model_file)
        os.environ["SURYA_GGUF_LOCAL_MMPROJ_PATH"] = str(mmproj_file)
        model_desc = f"自动拉起后端(本地 {model_file.name} + {mmproj_file.name})"
        # 若还要用本地快速版面 surya_layout2(rf-detr), 打开下面两行:
        # os.environ["FAST_LAYOUT_MODEL_CHECKPOINT"] = str(CKPT_DIR / "surya_layout2")
        # os.environ["FAST_ORDER_MODEL_CHECKPOINT"] = str(CKPT_DIR / "surya_layout2" / "order")
        if args.backend == "llamacpp" and shutil.which("llama-server") is None:
            print("[警告] 未找到 llama-server。请先安装 llama.cpp 并把 llama-server 加入 PATH;\n"
                  "        或改用 --backend vllm, 或直接 --url 连接已启动的服务器。")

    print(f"[信息] 解析目标: {input_path} | 后端: {args.backend} | DPI: {args.dpi}")
    print(f"[信息] 模型来源: {model_desc}")

    # ---------- 导入 Surya 2 (必须在环境变量设置之后) ----------
    try:
        from surya.input.load import load_from_file  # PDF -> 页面 PIL 图片
        from surya.inference import SuryaInferenceManager
        from surya.recognition import RecognitionPredictor
    except ImportError as e:
        sys.exit(f"[错误] 导入 surya 失败, 请确认已升级到 0.22.x: pip install -U surya-ocr\n({e})")

    # ---------- 渲染 PDF 页面 ----------
    page_range = parse_pages(args.pages)
    page_images, names = load_from_file(str(input_path), page_range=page_range, dpi=args.dpi)
    print(f"[信息] 共加载 {len(page_images)} 页")
    if not page_images:
        sys.exit("[错误] 没有可处理的页面")

    # ---------- 输出路径(JSON + 插图目录) ----------
    output_path = (Path(args.output) if args.output
                   else input_path.with_name(input_path.stem + "_surya2.json"))
    image_dir = (Path(args.image_dir) if args.image_dir
                 else output_path.parent / f"{input_path.stem}_images")
    image_labels = resolve_image_labels(args.image_labels)
    if not args.no_images:
        print(f"[信息] 插图输出目录: {image_dir} | 图片类标签: "
              + ",".join(sorted(image_labels)))

    # ---------- 启动推理后端 + OCR ----------
    if args.url:
        print(f"[信息] 连接外部推理服务器: {args.url} ...")
    else:
        print("[信息] 启动推理后端/加载模型 (首次会自动拉起 llama-server 或 vllm) ...")
    manager = SuryaInferenceManager(method=args.backend)
    rec_predictor = RecognitionPredictor(manager)

    print("[信息] 整页 OCR ...")
    ocr_results = rec_predictor(page_images)  # -> List[PageOCRResult]

    # ---------- 汇总: 每页按阅读顺序输出 block, 并裁剪图片类 block ----------
    pages_out = []
    image_count = 0
    for idx, (image, page_res) in enumerate(zip(page_images, ocr_results)):
        blocks = []
        page_images_out = []
        for b in (page_res.blocks or []):
            html = b.html or ""
            bbox, crop = crop_block(image, b.polygon, pad=args.image_pad)
            rel_path = None
            # 只对图片类 block 落盘: 文本/表格/公式已在 html 里, 无需再存图
            if (not args.no_images and crop is not None
                    and b.label in image_labels and not b.skipped and not b.error
                    and min(crop.size) >= args.min_image_size):
                rel_path = save_block_image(crop, image_dir, input_path.stem,
                                            idx + 1, b.reading_order, b.label)
                image_count += 1
                page_images_out.append({
                    "label": b.label,
                    "reading_order": b.reading_order,
                    "bbox": bbox,
                    "size": list(crop.size),
                    "file": rel_path,
                })
            blocks.append({
                "label": b.label,
                "raw_label": b.raw_label,
                "reading_order": b.reading_order,
                "confidence": round(float(b.confidence), 4) if b.confidence is not None else None,
                "skipped": b.skipped,
                "error": b.error,
                "bbox": bbox,
                "image": rel_path,
                "html": html,
                "text": html_to_text(html),
            })
        blocks.sort(key=lambda x: x["reading_order"] if x["reading_order"] is not None else 0)
        page_text = "\n\n".join(b["text"] for b in blocks if b["text"])
        pages_out.append({
            "page": idx + 1,
            "image_size": list(image.size),
            "text": page_text,
            "images": page_images_out,
            "blocks": blocks,
        })

        print(f"\n========== 第 {idx + 1} 页 ==========")
        for b in blocks:
            print(f"  [序{b['reading_order']}] {b['label']} conf={b['confidence']}"
                  + (" (skipped)" if b["skipped"] else "") + (" (error)" if b["error"] else "")
                  + (f" -> {b['image']}" if b["image"] else ""))
        if page_images_out:
            print(f"--- 本页提取插图 {len(page_images_out)} 张 ---")
        print("--- 提取文本 ---")
        print(page_text if page_text else "(无可识别文本)")

    # ---------- 保存 ----------
    result = {"input": str(input_path), "backend": args.backend,
              "image_dir": None if args.no_images else str(image_dir),
              "pages": pages_out}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[完成] 结果已保存: {output_path}")
    if not args.no_images:
        print(f"[完成] 共提取插图 {image_count} 张: {image_dir}")

    manager.stop()  # 停掉本次自动拉起的推理后端


if __name__ == "__main__":
    main()
