"""
Surya 2 推理服务 —— 客户端使用样例

前提: 已用 Docker 把 llama-server 服务端跑起来(部署 surya-2 模型):
      docker run -d --name surya-server --gpus all \
        -p 8080:8080 \
        -v "<checkpoints目录>:/app/checkpoints:ro" \
        surya:gpu
      # 验证: curl http://127.0.0.1:8080/v1/models

本脚本作为"客户端", 通过 OpenAI 兼容接口连接该服务, 对 PDF 做整页 OCR:
  1) 设置 SURYA_INFERENCE_URL 指向服务(不自动拉起后端)
  2) 渲染 PDF 页面
  3) RecognitionPredictor 整页 OCR(每页一次 VLM 调用)
  4) 按阅读顺序输出 block(label + html), 保存 JSON
  5) 把"图片类"block(Picture/Figure/Diagram/...)按 bbox 从页面上裁剪出来,
     单独存为 PNG(JSON 里记录相对路径), 便于后续入库/展示

使用(需本机已装 surya-ocr 0.22.x):
    python test_surya_service.py --url http://127.0.0.1:8080/v1 --input 你的.pdf
    python test_surya_service.py --url http://127.0.0.1:8080/v1 --input xx.pdf --pages 0-2
    # 指定插图输出目录 / 只要文本不要图:
    python test_surya_service.py --url http://127.0.0.1:8080/v1 --input xx.pdf --image-dir out/images
    python test_surya_service.py --url http://127.0.0.1:8080/v1 --input xx.pdf --no-images
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 版面标签里属于"图片类"的 canonical 名称(见 surya.layout.label.LAYOUT_PRED_RELABEL):
#   Image -> Picture, Complex-Block/Figure -> Figure, Diagram, Chemical-Block -> ChemicalBlock
# 关键: 这些标签同时也在 SKIP_OCR_LABELS(surya.inference.prompts)里 —— 模型不会对它们跑
#       OCR, 所以这些 block 一定是 skipped=True / html="", 这是"它本来就是图"的正常表现,
#       绝不能用 skipped 当过滤条件(那样会把要提取的图全部丢掉)。
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


def extract_page(image, page_res, *, page_no: int, stem: str, image_dir: Path,
                 image_labels: set[str], pad: int = 4, min_size: int = 32,
                 save_images: bool = True):
    """把一页的 PageOCRResult 整理成输出 dict, 并把图片类 block 裁剪落盘。

    返回 (page_out, label_counter)。注意: 图片类 block 一定是
    skipped=True / html=""(模型本就不对图做 OCR), 所以只能用 label 和
    裁剪区域来判断, 不能拿 skipped/空 html 当过滤条件。
    """
    blocks = []
    images_out = []
    counter: dict[str, int] = {}
    for b in (page_res.blocks or []):
        html = b.html or ""
        counter[b.label] = counter.get(b.label, 0) + 1
        bbox, crop = crop_block(image, b.polygon, pad=pad)
        rel_path = None
        if (save_images and crop is not None and b.label in image_labels
                and not b.error and min(crop.size) >= min_size):
            rel_path = save_block_image(crop, image_dir, stem, page_no,
                                        b.reading_order, b.label)
            images_out.append({
                "label": b.label,
                "raw_label": b.raw_label,
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
    return {
        "page": page_no,
        "image_size": list(image.size),
        "text": page_text,
        "images": images_out,
        "blocks": blocks,
    }, counter


def parse_pages(spec: str | None) -> list[int] | None:
    """'0,2-5' -> [0,2,3,4,5]; None = 全部页"""
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


def check_server(url: str) -> None:
    """(可选) 探测服务端是否就绪, 并确认模型名"""
    base = url.rstrip("/")
    if base.endswith("/v1"):
        models_url = base + "/models"
    else:
        models_url = base
    try:
        with urllib.request.urlopen(models_url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = [m.get("id") for m in data.get("data", [])]
        print(f"[信息] 服务就绪, 可用模型: {names}")
        if names and "datalab-to/surya-ocr-2" not in names:
            print("[警告] 未看到模型 datalab-to/surya-ocr-2, 请确认服务端用 "
                  "--alias datalab-to/surya-ocr-2 启动(Surya 连接时会校验模型名)。")
    except Exception as e:
        print(f"[警告] 服务健康检查失败: {e}")
        print("        请确认服务端已启动: docker ps / docker logs surya-server")


def main() -> None:
    parser = argparse.ArgumentParser(description="Surya 2 推理服务客户端(整页 OCR)")
    parser.add_argument("--url", default="http://127.0.0.1:8060/v1",
                        help="llama-server/vllm 的 OpenAI 兼容地址, 默认 "
                             "http://127.0.0.1:8060/v1")
    parser.add_argument("--backend", default="llamacpp",
                        help="服务端类型: llamacpp(默认) / vllm")
    parser.add_argument("--input", default="test.pdf", help="要解析的 PDF 路径")
    parser.add_argument("--pages", default=None, help="页码(从0起), 如 '0,2-5'; 默认全部")
    parser.add_argument("--dpi", type=int, default=192, help="渲染 DPI, 96~192")
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
    parser.add_argument("--check", action="store_true",
                        help="先探测服务端 /v1/models 再解析")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = HERE / input_path
    if not input_path.exists():
        sys.exit(f"[错误] 找不到输入 PDF: {input_path}")

    if args.check:
        check_server(args.url)

    # ---------- 关键: import surya 之前设环境变量, 指向外部服务 ----------
    os.environ["SURYA_INFERENCE_URL"] = args.url
    os.environ["SURYA_INFERENCE_BACKEND"] = args.backend

    print(f"[信息] 连接推理服务: {args.url} | 后端: {args.backend}")
    print(f"[信息] 解析目标: {input_path} | DPI: {args.dpi}")

    # ---------- 导入 surya (客户端, 本机需已装 surya-ocr 0.22.x) ----------
    try:
        from surya.input.load import load_from_file  # PDF -> 页面 PIL 图片
        from surya.inference import SuryaInferenceManager
        from surya.recognition import RecognitionPredictor
    except ImportError as e:
        sys.exit(f"[错误] 本机缺少 surya-ocr 0.22.x: pip install -U surya-ocr\n({e})")

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

    # ---------- 连接服务并整页 OCR ----------
    manager = SuryaInferenceManager(method=args.backend)
    rec_predictor = RecognitionPredictor(manager)
    print("[信息] 整页 OCR ...")
    ocr_results = rec_predictor(page_images)  # -> List[PageOCRResult]

    # ---------- 汇总: 每页按阅读顺序输出 block, 并裁剪图片类 block ----------
    pages_out = []
    image_count = 0
    label_counter: dict[str, int] = {}
    for idx, (image, page_res) in enumerate(zip(page_images, ocr_results)):
        page_out, counter = extract_page(
            image, page_res,
            page_no=idx + 1, stem=input_path.stem, image_dir=image_dir,
            image_labels=image_labels, pad=args.image_pad,
            min_size=args.min_image_size, save_images=not args.no_images,
        )
        pages_out.append(page_out)
        image_count += len(page_out["images"])
        for lbl, cnt in counter.items():
            label_counter[lbl] = label_counter.get(lbl, 0) + cnt

        print(f"\n========== 第 {idx + 1} 页 ==========")
        for b in page_out["blocks"]:
            print(f"  [序{b['reading_order']}] {b['label']} conf={b['confidence']}"
                  + (" (skipped)" if b["skipped"] else "") + (" (error)" if b["error"] else "")
                  + (f" -> {b['image']}" if b["image"] else ""))
        if page_out["images"]:
            print(f"--- 本页提取插图 {len(page_out['images'])} 张 ---")
        print("--- 提取文本 ---")
        print(page_out["text"] if page_out["text"] else "(无可识别文本)")

    # ---------- 保存 ----------
    result = {"server_url": args.url, "backend": args.backend,
              "input": str(input_path),
              "image_dir": None if args.no_images else str(image_dir),
              "pages": pages_out}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[完成] 结果已保存: {output_path}")
    if not args.no_images:
        print(f"[完成] 共提取插图 {image_count} 张: {image_dir}")
        if image_count == 0:
            print("[警告] 没有匹配到图片类 block。本次实际出现的 block 标签统计"
                  f"(用 --image-labels 把真实标签加进去):")
            for lbl, cnt in sorted(label_counter.items(), key=lambda kv: -kv[1]):
                tags = " (已在图片类中)" if lbl in image_labels else ""
                print(f"    {lbl}: {cnt}{tags}")


if __name__ == "__main__":
    main()
