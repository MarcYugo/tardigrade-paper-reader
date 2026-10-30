'''
按公式框裁剪 → texify 识别
流程：
    1. 根据pymupdf解析出的字符判断哪些是公式字符，并使用算法聚合得到完整的公式框
    2. 根据公式框周围的字符和上下行的位置，对行内公式框进行限制和修正，并更新
    3. 按照公式框使用pymupdf裁剪渲染后的pdf图片，得到公式截图
    4. 使用texify识别公式截图中的公式，转换为latex公式，给公式span增加latex公式代码和公式截图
'''

from __future__ import annotations

from typing import Any, Dict, List, Sequence

import pymupdf
# from PIL import Image, ImageOps

from merge_modules.merge_blocks import merge_formula_boxes, limit_inline_formula_box_height
from merge_modules.merge_chars import merge_unformula_chars_to_string

from utils.crop_op import crop_formula_image, image_encode_base64

MIN_BOX_SIZE = 1.0  # 小于 1pt 的框基本是碎片噪声，直接跳过


def collect_page_formula_boxes(page_dict: dict) -> List[dict]:
    """跑一遍 merge_blocks，拿到本页所有公式框。

    ``merge_formula_boxes()`` 返回的是整页 blocks（每个 block 上挂着
    ``formula_boxes``），必须再用 ``collect_formula_boxes()`` 跨 block 去重
    收集，得到的才是带 ``id`` / ``bbox`` / ``role`` 的公式框列表。
    """
    blocks = merge_formula_boxes(blocks=page_dict["blocks"])
    # blocks = merge_unformula_chars_to_string(blocks=blocks)
    blocks = limit_inline_formula_box_height(blocks=blocks)
    return blocks

def recognize_one_boxe(
    engine: Any,
    box: dict,
    page: pymupdf.Page,
    dpi: int,
    padding: float,
    batch_size: int,
    temperature: float,
    max_tokens: int,
    all_boxes: Sequence[dict] | None = None,
) -> Dict[str, Any]:
    """裁剪 + 批量识别，返回每条的裁剪图与 LaTeX。"""
    pending: Dict[str, Any] = None

    siblings = list(all_boxes) if all_boxes is not None else [box]
    foreign_frags = [
                        (other.get("id"), rb)
                        for other in siblings
                        for rb in (other.get("fragment_bboxes") or ())
                    ]
    
    bbox = box["bbox"]
    if bbox[2] - bbox[0] < MIN_BOX_SIZE or bbox[3] - bbox[1] < MIN_BOX_SIZE:
        return pending
    keep_rects = [
                    tuple(float(v) for v in rb)
                    for rb in (box.get("fragment_bboxes") or ())
                ]
    mask_rects = [rb for oid, rb in foreign_frags if oid != box["id"]]

    image, rect = crop_formula_image(
        page,
        bbox,
        dpi=dpi,
        padding=padding,
        clearance=(
            box.get("crop_clearance")
            if tuple(bbox) == tuple(box.get("crop_clearance_bbox", ()))
            else None
        ),
        mask_rects=mask_rects,
        keep_rects=keep_rects
    )
    
    if image is None:
        return pending

    pending = {
                "id": box["id"],
                "role": box.get("role"),
                "bbox": [round(float(v), 2) for v in bbox],
                "pdf_text": box.get("text", ""),
                "image": image,
                "rect": rect,
            }

    latex_code = engine.predict(
        [pending["image"]],
        temperature=temperature,
        max_tokens=max_tokens,
        batch_size=batch_size,
    )
    pending["latex"] = latex_code
    # image 编码为base64
    pending["image"] = image_encode_base64(pending["image"])
    pending["rect"] = tuple(pending["rect"])
    return pending

def drop_src_image_blocks(blocks: list):
    i = 0
    while i < len(blocks):
        if blocks[i]["type"] == 1:
            blocks.pop(i)
        else:
            i += 1
    return blocks

def fix_formula_box_trans_latex(page: pymupdf.Page, engine: Any, dpi: int, padding: float, 
                                temperature: float, max_tokens: int):
    page_dict = page.get_text("dict")
    blocks = collect_page_formula_boxes(page_dict)
    # 记住已处理的公式，避免重复识别
    mem_id = set()
    for block in blocks:
        for i in range(len(block.get("formula_boxes", []))):
            item = block["formula_boxes"][i]
            if item["id"] in mem_id:
                continue
            ret = recognize_one_boxe(engine=engine, box=item, page=page,
                                    dpi=dpi, padding=padding, batch_size=1, 
                                    temperature=temperature, max_tokens=max_tokens)
            mem_id.add(item["id"])
            block["formula_boxes"][i] = ret
    blocks = drop_src_image_blocks(blocks=blocks)
    page_dict["blocks"] = blocks
    return page_dict

