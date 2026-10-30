"""PDF 解析主流程：版面分析 → 公式 LaTeX → 表格结构 → 插图裁剪 → JSON。

版面检测由 **OXR 的 D-FINE 版面分析模型** 负责（``utils/load_oxr_layout.py``，
权重在 ``models/oxr_layout_analysis/``）：一次前向拿到整页 12 类区域
（Text / Title / Formula / Table / Picture / Caption / ...），
用来定位表格与插图，取代了原先单独接入的 YOLOv13 检测器；
表格结构仍由 ``SLANet_plus`` 识别，公式仍由 ``texify`` 转 LaTeX。
"""

import json
import pymupdf
from typing import Any, List, Dict

# from utils.load_pp_formulanet_s import load_pp_formulanet_s
from utils.load_texify import load_texify
# 版面分析（表格 / 插图定位）：OXR D-FINE，替代原来的 YOLOv13 检测器
from utils.load_oxr_layout import load_oxr_layout
from utils.load_slanet import load_slanet

from extract_images import extrac_figures_pictures
from extract_tables import extract_tables
from formula_trans_to_latex import fix_formula_box_trans_latex


def process_one_page(
    page: pymupdf.Page,
    layout_engine: Any,
    slanet_engine: Any,
    texify_engine: Any,
    dpi: int,
    padding: float,
    temperature: float,
    max_tokens: int,
    confidence: float = 0.3,
) -> Dict[str, Any]:
    """处理单页：版面分析 → 公式识别 → 表格结构识别 → 插图裁剪。

    版面检测只跑一次：``Table`` 区域交给 ``SLANet_plus`` 识别结构，
    ``Picture`` 区域直接裁图；其余类别留在 ``page_dict["layout"]`` 里备用。
    """
    page_dict = fix_formula_box_trans_latex(page=page, engine=texify_engine,
                                            dpi=dpi, padding=padding, 
                                            temperature=temperature, max_tokens=max_tokens)
    # 版面分析：页级结果与 out_samples/attention_layout.json 同构（像素坐标），
    # 返回的 detections 已换算成 PDF 点坐标并去重，可直接用于裁剪
    layout_page, detections = layout_engine.predict_page_detail(
        page=page, dpi=dpi, confidence=confidence)
    page_dict["layout"] = layout_page
    tables, pictures = extract_tables(page=page, detect_engine=None,
                                      transfer_engine=slanet_engine, dpi=dpi,
                                      detections=detections)
    page_dict["tables"] = tables
    # figures = extrac_figures_pictures(page=page)
    # page_dict["figures"] = figures
    page_dict["pictures"] = pictures
    return page_dict

def process_pages(doc: pymupdf.Document,
                  layout_engine: Any, slanet_engine: Any, texify_engine: Any,
                  dpi: int, padding: float,
                  temperature: float, max_tokens: int,
                  confidence: float = 0.3) -> Dict[str, Dict]:
    pages = {}
    for i, page in enumerate(doc):
        content = process_one_page(page=page, 
                                    layout_engine=layout_engine, slanet_engine=slanet_engine, texify_engine=texify_engine, 
                                    dpi=dpi, padding=padding,
                                    temperature=temperature, max_tokens=max_tokens,
                                    confidence=confidence)
        pages[i] = {"page_index": i, "contents": content}
    doc_parse = {"document": doc.name, "pages": pages}
    return doc_parse

if __name__ == "__main__":
    doc_path = "../latex_sample.pdf"
    save_path  = "./out_samples/latex_sample.json"
    # formulanet_model_dir = "./models/pp-formulanet-plus-s-onnx"
    texify_model_dir = "./models/texify-quantized-onnx"
    # 版面分析（D-FINE）权重：models/oxr_layout_analysis/{last.pth, metadata.json}
    layout_checkpoint = "./models/oxr_layout_analysis/last.pth"
    slanet_model_dir = "./models/SLANet_plus/"
    texify_engine = load_texify(model_dir=texify_model_dir, device="cpu")
    layout_engine = load_oxr_layout(checkpoint=layout_checkpoint, device="cpu")
    slanet_engine = load_slanet(model_dir=slanet_model_dir, device="cpu")
    info = layout_engine.info()
    print(f"版面分析模型已加载：version={info['model_version']} class={info['num_classes']} "
          f"size={info['image_size']} device={info['device']}")

    doc = pymupdf.open(filename=doc_path)
    # page_dict = process_one_page(page=doc[3], 
    #                              layout_engine=layout_engine, slanet_engine=slanet_engine, texify_engine=texify_engine, 
    #                              dpi=300, padding=2.0, temperature=0.0, max_tokens=384)
    doc_parse = process_pages(doc=doc, layout_engine=layout_engine, slanet_engine=slanet_engine, texify_engine=texify_engine,
                               dpi=300, padding=2.0, temperature=0.0, max_tokens=384)
    with open(save_path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(doc_parse, ensure_ascii=False, indent=2))
    print(f"Result of parsed pdf file has been saved to {save_path}")