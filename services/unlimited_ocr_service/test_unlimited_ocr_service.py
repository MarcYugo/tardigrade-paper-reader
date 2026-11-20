"""Unlimited-OCR 服务冒烟测试：用 PyMuPDF 打开 PDF，逐页渲染后请求部署好的服务。
NVIDIA A800-SXM4-40GB/ NVIDIA A100-SXM4-40GB
用法:
    python test_unlimited_ocr_service.py paper.pdf
    python test_unlimited_ocr_service.py paper.pdf --base-url http://localhost:9010/v1 --model Unlimited-OCR
"""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

import pymupdf  # PyMuPDF
from openai import OpenAI


def pages_to_base64_png(doc: pymupdf.Document, zoom: float = 300 / 72):
    """把 PDF 每一页渲染成 PNG 并 base64 编码。

    zoom 默认 300/72 ≈ 4.17（300 dpi）：低分辨率下密集页会让模型输出退化
    （回环重复 / 结尾"1. 2. 3."数数），300 dpi 实测最稳。
    """
    matrix = pymupdf.Matrix(zoom, zoom)
    return [
        base64.b64encode(page.get_pixmap(matrix=matrix).tobytes("png")).decode()
        for page in doc
    ]


def parse_layout_items(text: str, pages=None) -> list[dict]:
    """把模型的行级类别、bbox 和正文解析成结构化项。"""
    page_numbers = [page + 1 for page in pages] if pages is not None else []
    page_index = 0
    current_page = page_numbers[0] if page_numbers else 1
    items = []
    current_item = None
    record_pattern = re.compile(
        r"^(?P<kind>[A-Za-z_][\w-]*)\s+\[(?P<bbox>[^\]]+)\]\s*(?P<text>.*)$"
    )
    number_pattern = re.compile(r"-?\d+(?:\.\d+)?")

    # Remove token wrappers but preserve their contents (notably det categories).
    text = re.sub(r"<\|/?(?:ref|det)\|>", "", text)

    for line in text.splitlines():
        for page_part, part in enumerate(line.split("<PAGE>")):
            if page_part:
                page_index += 1
                if page_numbers:
                    current_page = page_numbers[min(page_index, len(page_numbers) - 1)]
                else:
                    current_page += 1

            part = part.strip()
            if not part:
                continue

            match = record_pattern.match(part)
            if match:
                coordinates = number_pattern.findall(match.group("bbox"))
                if len(coordinates) == 4:
                    bbox = [
                        int(value) if float(value).is_integer() else float(value)
                        for value in coordinates
                    ]
                    current_item = {
                        "page": current_page,
                        "type": match.group("kind"),
                        "bbox": bbox,
                        "text": match.group("text").strip(),
                    }
                    items.append(current_item)
                    continue

            if current_item is None or current_item["page"] != current_page:
                current_item = {
                    "page": current_page,
                    "type": "text",
                    "bbox": None,
                    "text": part,
                }
                items.append(current_item)
            else:
                current_item["text"] += ("\n" if current_item["text"] else "") + part

    return items


def parse_pdf(pdf_path: str, client: OpenAI, model: str, pages=None, raw=False) -> list[dict]:
    with pymupdf.open(pdf_path) as doc:
        selected_pages = list(range(len(doc))) if pages is None else pages
        selected_pages = [idx for idx in selected_pages if 0 <= idx < len(doc)]
        if not selected_pages:
            raise ValueError("没有可解析的页面：请检查 --pages 参数是否在 PDF 页范围内")
        results = []
        for page_index in selected_pages:
            image = base64.b64encode(
                # 300 dpi：低分辨率下密集页会触发模型退化（见 pages_to_base64_png）。
                doc[page_index].get_pixmap(matrix=pymupdf.Matrix(300 / 72, 300 / 72)).tobytes("png")
            ).decode()
            # Each request contains exactly one page image; process pages sequentially.
            content = [
                # No "<image>" prefix: the chat template adds the placeholder itself.
                {"type": "text", "text": "document parsing."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
            ]
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=6144,
                    temperature=0.0,
                    extra_body={
                        "skip_special_tokens": False,
                        "vllm_xargs": {"ngram_size": 35, "window_size": 1024},
                    },
                )
            except Exception as exc:
                raise RuntimeError(f"第 {page_index + 1} 页请求失败: {exc}") from exc

            page_text = response.choices[0].message.content
            if not page_text:
                raise ValueError(f"第 {page_index + 1} 页返回为空：检查服务响应和提示词")
            if raw:
                results.append({"page": page_index + 1, "raw": page_text})
            else:
                results.extend(parse_layout_items(page_text, pages=[page_index]))

    return results


def parse_page_list(raw_pages: str | None):
    """解析 --pages 参数，支持 '1,3,5' 或 '1-3,5'。"""
    if raw_pages is None:
        return None
    pages = []
    for part in raw_pages.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            try:
                start = int(start_str)
                end = int(end_str)
            except ValueError as exc:
                raise ValueError(f"页码范围 '{part}' 非法，必须类似 '1-3' 或 '5'") from exc
            if start > end:
                start, end = end, start
            pages.extend(range(start - 1, end))
        else:
            try:
                pages.append(int(part) - 1)
            except ValueError as exc:
                raise ValueError(f"页码 '{part}' 非法，必须是整数或范围") from exc
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description="Unlimited-OCR 服务测试")
    parser.add_argument("pdf", help="待解析的 PDF 文件路径")
    parser.add_argument("--base-url", default="http://localhost:9010/v1")
    parser.add_argument("--model", default="Unlimited-OCR")
    parser.add_argument("--pages", help="指定要解析的页码，例如 '1,3,5' 或 '1-3,5'。默认解析全部页")
    parser.add_argument("--output", help="JSON 结果文件路径，默认与 PDF 同名并使用 .json 后缀")
    parser.add_argument("--raw", action="store_true", help="保留 <|ref|>/<|det|> 标注")
    args = parser.parse_args()

    selected_pages = parse_page_list(args.pages)

    client = OpenAI(
        api_key=os.getenv("VLLM_API_KEY") or "EMPTY",
        base_url=args.base_url,
        timeout=3600,
    )
    try:
        result = parse_pdf(args.pdf, client, args.model, pages=selected_pages, raw=args.raw)
    except Exception as exc:  # noqa: BLE001 - 冒烟测试，直接暴露错误信息
        print(f"[FAIL] 请求失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not result:
        print("[FAIL] 返回为空：检查文案是否重复带了 <image>（chat_template 会自动加）、skip_special_tokens 是否为 False")
        return 1

    output_path = Path(args.output) if args.output else Path(args.pdf).with_suffix(".json")
    result_data = {
        "source_pdf": str(Path(args.pdf)),
        "model": args.model,
        "pages": None if selected_pages is None else [page + 1 for page in selected_pages],
        "raw": args.raw,
        "result": result,
    }
    try:
        output_path.write_text(
            json.dumps(result_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[FAIL] 保存 JSON 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("[OK] 服务返回如下：" if args.raw else "[OK] 解析结果：")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[OK] JSON 结果已保存: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
