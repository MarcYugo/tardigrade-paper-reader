"""三条文字版式后端（同一 `parse_pdf()` 契约下的可切换实现）。

| 模块 | 后端取值 | 作用 |
| --- | --- | --- |
| `backend_pymupdf` | `pymupdf` / `auto` | 本地解析方案：PyMuPDF 文字层 + texify 公式 + OXR 版面 / SLANet 表格 |
| `backend_surya` | `surya` | Surya 2 整页 OCR（块级数据，前端独立渲染） |
| `backend_unlimited_ocr` | `unlimited_ocr` | Unlimited-OCR 整页 OCR（本地推理 / 在线解析两条来源） |

上层的调度与公共工具仍在 `pdf_parser/` 包根（`entry.py` / `options.py` /
`text_layer.py` / `sentences.py` / `math_*` 等）；三客户端（`surya_parser` /
`unlimited_ocr` / `unlimited_ocr_online`）也在包根，这里通过 `..` 引用。
"""
