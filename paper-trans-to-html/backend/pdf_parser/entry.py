"""入口：`parse_pdf`。

见包文档 `backend/pdf_parser/__init__.py`。三条解析链并列：

  * `backend` = `pymupdf` / `auto` / `detection_service_group`：**本地解析方案**
    （`backend_pymupdf._parse_with_pymupdf`）—— PyMuPDF 文字层 + `local_parse_pipeline`
    的 texify 公式识别、OXR 版面分析（D-FINE）定位表格/插图、SLANet_plus 表格结构识别与插图提取。
    页面下发**逐行几何**（`pages[].texts[].lines[].runs[]`）与页面级公式框
    （`pages[].formula_boxes[]`，LaTeX + 裁图），前端走逐行绝对定位渲染
    （见 `frontend/js/reader2.js::buildTextItem` 与 `r2-math.js::buildFormulaBoxes`）。
    依赖/模型缺失时直接报错（`backend_pymupdf.LocalParseUnavailable`）。
    `detection_service_group` 仅作为历史取值保留，本地链路不再调用该服务。
  * `backend` = `surya`：整页 OCR 走 Surya 2（`backend_surya._parse_with_surya`），
    文字/公式（`<math>`→LaTeX）/插图裁图/阅读顺序全部来自 Surya 的版面 block，
    前端按 `DOC.parser === "surya"` 走独立的块级渲染
    （见 `frontend/js/reader2.js::buildSuryaItem`）。Surya 不可用时直接报错
    （`backend/surya_parser.SuryaUnavailable`，经转换接口映成页面上的失败提示）。
  * `backend` = `unlimited_ocr`：整页 OCR 走 Unlimited-OCR
    （`backend_unlimited_ocr._parse_with_unlimited_ocr`），与 `surya` 同级：文字/公式
    （LaTeX）/表格（HTML）/插图裁图/阅读顺序全部来自 Unlimited-OCR 回包，前端按
    `DOC.parser === "unlimited_ocr"` 走它**自己那套**独立的块级渲染
    （见 `frontend/js/reader2.js::buildUnlimitedItem`，与 Surya 那套各自维护）。
    服务不可用时直接报错（`backend/unlimited_ocr.UnlimitedOcrUnavailable`）。

对外签名与返回结构**保持不变**(仍是 `parser` / `parser_info` / `toc` /
`warnings`)，所以 `converter.py` / `app.py` / `tools/*` 都不用改；
每个页面**新增** `pages[].links`（PDF 链接注释，后端统一采集，见 `links.py`）
—— 它是“引用跳转”（正文引用标记 → 参考文献条目，`backend/pdf_refs/refs.py`）的数据源。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .backend_pymupdf import (_parse_with_pymupdf)
from .backend_surya import (_parse_with_surya)
from .backend_unlimited_ocr import (_parse_with_unlimited_ocr)
from .links import collect_links
from .options import (load_options)
from ..pdf_refs.refs_surya import split_surya_reference_blocks


# =====================================================================
#  公式后处理挂载点（detection-service-group）
# =====================================================================
def _formula_postprocess(result: dict, pdf_path: Path, images_dir: Optional[Path],
                         opts: dict, warnings: list[str]) -> dict:
    """本地解析**之后**的增强钩子（当前无事可做）。

    公式识别与插图/表格提取已经由 `local_parse_pipeline.process_pages` 一并完成
    （`backend_pymupdf`），原 detection-service-group 后处理不再参与；函数保留
    挂载点，方便以后加“公式二次审核”之类的步骤。
    """
    return result


def pop_private(result: dict) -> dict:
    """摘掉只给内部用的字段(页级 `_` 前缀键) —— **不能进 doc.json**。

    当前有 `_formula_slots`(公式空位；detection-service-group 后处理用它挑「哪些页要送
    检测」，见 `backend_pymupdf`)—— 体积不小，且前端与接口层都不该看见；
    必须在公式后处理**之后**调用(后处理正是它的使用者)。
    """
    for pg in result.get("pages") or []:
        for key in [k for k in pg if k.startswith("_")]:
            pg.pop(key, None)
    return result


# =====================================================================
#  链接注释采集（三个后端统一：引用跳转功能的数据源）
# =====================================================================
def _attach_links(result: dict, pdf_path: Path, warnings: list[str]) -> None:
    """把 PDF 链接注释（内部跳转 / URI）写进 `pages[].links`（见 `links.py`）。

    三条解析分支（本地解析方案 / surya / unlimited_ocr）都在返回前调用
    —— 链接来自 PDF 注释本身，与“文字来自哪个后端”无关，所以采一套、大家共用。
    前端“点引用弹参考文献卡片”（`backend/pdf_refs/refs.py` + `frontend/js/r2-refs.js`）
    与 `GET /api/doc/{id}/refs` 都建立在这份数据上。
    采集失败只记一条 warning，**绝不影响解析主流程**。
    """
    try:
        links = collect_links(pdf_path)
    except Exception as exc:                     # 兜底：任何异常都不拖垮解析
        warnings.append(f"链接注释采集失败：{exc}")
        links = {}
    total = 0
    for pi, pg in enumerate(result.get("pages") or []):
        got = links.get(pi)
        if got:
            pg["links"] = got
            total += len(got)
    info = result.get("parser_info")
    if not isinstance(info, dict):
        info = {}
        result["parser_info"] = info
    info["links_total"] = total


# =====================================================================
#  入口
# =====================================================================
def parse_pdf(pdf_path: Path, images_dir: Optional[Path] = None, *,
              backend: Optional[str] = None, options: Optional[dict] = None,
              config_file: Optional[Path] = None) -> dict:
    """解析 PDF → 版式数据(即 doc.json 的 pages 结构)。

    `backend` / `options` / `config_file` 三个参数为**兼容旧调用方保留**
    (`converter.reparse(..., backend=…)`、`tools/selftest.py --backend …`)：

      * `pymupdf` / `auto` / `detection_service_group`：**本地解析方案** —— PyMuPDF
        文字层 + `local_parse_pipeline` 的 texify（公式）/ OXR 版面 + SLANet（表格）/
        矢量图与位图提取；前端逐行绝对定位渲染，公式走页面级 KaTeX 覆盖层。
        依赖/模型缺失时直接抛 `LocalParseUnavailable`（不回退）；
      * `surya`：整页 OCR 走 Surya 2 —— 文字、公式（`<math>`→LaTeX）、插图都来自
        Surya，**不使用 PyMuPDF 版式**；失败直接抛 `SuryaUnavailable`
        （不会悄悄回退，免得看起来像解析成功了）；
      * `unlimited_ocr`：整页 OCR 走 Unlimited-OCR —— 文字、公式（LaTeX）、表格
        （HTML）、插图都来自它的回包，**不使用 PyMuPDF 版式**；失败直接抛
        `UnlimitedOcrUnavailable`（同样不回退）。

    返回值：
      * parser      `"pymupdf"` / `"surya"` / `"unlimited_ocr"`
      * parser_info 解析统计（本地方案：公式/表格/插图条数与耗时；OCR 后端：块数等）
      * toc         PDF 自带书签目录 `[{level,title,page}]`（可能为空）
      * warnings    说明性信息(列表，可能为空)
    """
    opts, warnings = load_options(config_file=config_file, overrides=options,
                                  backend=backend)
    if images_dir is not None:
        # `_parse_with_pymupdf` / `_parse_with_surya` 里都要 images_dir.mkdir()，
        # str 会 AttributeError
        images_dir = Path(images_dir)

    if opts["backend"] == "surya":
        # Surya 是**独立解析链**：文字 / 公式 / 插图全部用它自己的数据，
        # 前端按 `parser === "surya"` 走块级渲染（见 backend_surya 模块文档）。
        warnings.append("解析后端 'surya'：文字版式、公式（<math>→LaTeX）与插图都由 "
                        "Surya 2 整页 OCR 提供；不使用 PyMuPDF 版式，"
                        "也不跑 detection-service-group")
        result = _parse_with_surya(pdf_path, images_dir, opts)
        split_counts = split_surya_reference_blocks(result.get("pages") or [])
        if split_counts:
          info = result.setdefault("parser_info", {})
          info["surya_blocks"] = sum(
            len(page.get("texts") or []) + len(page.get("images") or [])
            for page in result.get("pages") or []
          )
          for kind, added in split_counts.items():
            key = f"surya_kind_{kind}"
            info[key] = int(info.get(key) or 0) + added
        _attach_links(result, pdf_path, warnings)
        result["warnings"] = warnings
        return result

    if opts["backend"] == "unlimited_ocr":
        # Unlimited-OCR 也是**独立解析链**（与 surya 同级、各自维护）：文字 / 公式 /
        # 表格 / 插图全部用它自己的回包，前端按 `parser === "unlimited_ocr"`
        # 走它自己那套块级渲染（见 backend_unlimited_ocr 模块文档）。
        warnings.append("解析后端 'unlimited_ocr'：文字、公式（LaTeX）、表格与插图都由 "
                        "Unlimited-OCR 整页 OCR 提供；不使用 PyMuPDF 版式，"
                        "也不跑 detection-service-group")
        result = _parse_with_unlimited_ocr(pdf_path, images_dir, opts)
        _attach_links(result, pdf_path, warnings)
        result["warnings"] = warnings
        return result

    # ---- 本地解析方案：PyMuPDF 版式 + local_parse_pipeline 小模型链路 ----
    # `auto` / `detection_service_group` / `pymupdf` 都落到这里。
    if opts["backend"] == "detection_service_group":
        warnings.append("解析后端 'detection_service_group' 现在使用本地解析方案"
                        "（公式/表格/插图都由本地模型完成，不再调用 detection-service-group）")
    else:
        warnings.append("解析后端 " + repr(opts["backend"])
                        + " → 本地解析方案：PyMuPDF 版式 + texify 公式 + "
                        "OXR 版面分析 + SLANet 表格；公式以页面覆盖层渲染")
    result = _parse_with_pymupdf(pdf_path, images_dir, opts)
    # 后端自己的告警（例如“有 N 个公式框没有拿到 LaTeX”）要一并保留
    warnings.extend(result.get("warnings") or [])
    result = _formula_postprocess(result, pdf_path, images_dir, opts, warnings)
    # 公式最后是怎么落地的：
    #   "boxes" = 页面上叠了公式框覆盖层（pages[].formula_boxes，前端按 bbox_norm
    #             在页面上渲染 KaTeX，见 frontend/js/r2-math.js）；
    #   "none"  = 一个公式框都没拿到（文字层保留原字形）；
    #   "space" = 早期 PyMuPDF 链路把公式字形擦成等长空白的取值（旧 doc.json 兼容）。
    # "surya" / "unlimited_ocr"（公式来自整页 OCR 回包）走的是函数开头那两条独立
    # 分支，不会到这里。
    if any(pg.get("formula_boxes") for pg in result.get("pages") or []):
        result["formula_action"] = "boxes"
    _attach_links(result, pdf_path, warnings)    # 链接注释（引用跳转用，见 links.py）
    pop_private(result)          # 去掉页级 `_` 前缀的内部字段
    result["warnings"] = warnings
    return result
