"""模块一 · 文档解析

把 PDF 解析为与原文版式一致的布局数据：
  - 文字块 texts：绝对坐标 bbox；
  - 图片   images：绝对坐标 bbox + 落盘文件名。

当前可用的文字版式后端有三个：

  * **本地解析方案**（`backend_pymupdf.py` + `local_parse_pipeline/`，`backend` =
    `pymupdf` / `auto`）：PyMuPDF 文字层 + texify
    公式识别 + OXR 版面分析（D-FINE）/ SLANet 表格识别 + 插图提取；页面下发逐行几何与页面级公式框，
    前端按 `doc.parser === "pymupdf"` 走逐行绝对定位渲染
    （`frontend/js/reader2.js::buildTextItem`）。依赖/模型缺失时直接报错
    （`backend_pymupdf.LocalParseUnavailable`）。
  * **Surya 2**（`backend_surya.py` + 客户端 `../surya_parser.py`，`backend` = `surya`）：
    文字 / 公式（`<math>`→LaTeX）/ 插图裁图 / 阅读顺序全部来自 Surya 的版面 block；
    前端按 `doc.parser === "surya"` 走独立的块级流式渲染
    （`frontend/js/reader2.js::buildSuryaItem`）。Surya 不可用时直接报错。
  * **Unlimited-OCR**（`backend_unlimited_ocr.py` + 客户端 `../unlimited_ocr.py`，
    `backend` = `unlimited_ocr`）：文字 / 公式（LaTeX）/ 表格（HTML）/ 插图裁图 /
    阅读顺序全部来自 Unlimited-OCR 回包；前端按 `doc.parser === "unlimited_ocr"`
    走它**自己那套**独立块级渲染（`frontend/js/reader2.js::buildUnlimitedItem`）。
    数据来源两条（**设置项不变**）：本地推理服务 → **没配置 / 没起来时自动切在线解析**
    （`../unlimited_ocr_online.py`，百度智能云「文档解析（Unlimited-OCR）」）；
    两条都不可用时直接报错。走了哪条看 `parser_info["unlimited_ocr_route"]`。

对外接口：`parse_pdf(...)`(见 `entry.py`)与 `status(...)`(见 `options.py`)。
`parse_pdf` 的签名与返回结构**保持不变** —— `converter.py`、`app.py` 与 `tools/*`
都不用改：`backend` / `options` / `config_file` 参数仍在。
顶层 `formula_action` 与页级 `formula_boxes` 是**增量**字段(旧调用方忽略即可)；
`formula_action` 取值：`"boxes"`（本地方案：页面级公式框覆盖层）/ `"surya"` /
`"unlimited_ocr"`（公式来自整页 OCR 回包的 LaTeX）/ `"none"`（本地方案没拿到公式框）；
旧 doc.json 里还可能见到 `"space"` / `"inline"`（早期 PyMuPDF 链路的历史取值，
前端仍兼容读取）。

配置契约：解析服务参数从 `config.json` 的 `parser` 段读取；解析后端选择可通过
`/api/settings/parser` 按账号保存到 `users.prefs.parser.backend`，未单独设置时沿用
配置文件默认值。网页「⚙ 设置 → 存储 / OCR」面板使用 `surya_*` / `unlimited_ocr_*` 配置；
`options.status()` 返回 `surya_*` / `unlimited_ocr_*`。`surya_ready` 与
`unlimited_ocr_ready` 都是**真探测**
（`GET /health` / `GET /models`；`surya_ready` 还要求本机 surya-ocr 客户端依赖就绪，
细分字段 `surya_service_ready` / `surya_client_ready`）。

Unlimited-OCR 多两个**配置文件中才有的**键（不上设置面板）：
`parser.unlimited_ocr_route`（`auto`/`local`/`online`）与在线解析凭据
`parser.unlimited_ocr_online_api_key` / `unlimited_ocr_online_secret_key`
（也接受环境变量 `UNLIMITED_OCR_ROUTE` / `UNLIMITED_OCR_ONLINE_API_KEY` /
`UNLIMITED_OCR_ONLINE_SECRET_KEY`）。`status()` 额外返回 `unlimited_ocr_route` /
`unlimited_ocr_online_ready` / `unlimited_ocr_online_message`。

# ---------------------------------------------------------------------
#  本包的模块划分：
#    options.py          解析配置 / 环境变量覆盖 / status()
#    text_layer.py       文字层：正文字符抽取 / 补间距空格 / 每行合并成一个正文 run
#    sentences.py        按句子重切文字块（一个块 = 恰好一句）
#    images.py           [历史空壳] 早期插图检测/去重影
#    math_text.py        [历史空壳] 早期公式文本工具
#    math_font.py        公式字体判据（供补间距/探针文本用；字形擦除链路已停用）
#    backend_pymupdf.py  版式后端 · 本地解析方案（PyMuPDF + local_parse_pipeline）
#    backend_surya.py    版式后端 · Surya 2 整页 OCR（块级数据）
#    backend_unlimited_ocr.py 版式后端 · Unlimited-OCR 整页 OCR（块级数据）
#        本地推理 / 在线解析两条来源（见 ../unlimited_ocr_online.py），前端同一套渲染
#    links.py            PDF 链接注释采集（引用跳转的数据源）
#    entry.py            入口 parse_pdf(+ pop_private)
#  下面仍把内部名字再导出一次，保证 `pdf_parser.<原名字>` 照旧可用
#  (新代码建议直接从对应子模块 import)。
# ---------------------------------------------------------------------
"""

from .sentences import cut_blocks_by_sentence, split_sentences
from .text_layer import (_CHAR_X, _GAP_SPACE_MIN, _MONO, _SANS, _SERIF, _family,
                         _insert_gap_spaces, _is_super, _join_lines,
                         _line_baseline, _line_rot, _merge_line_runs,
                         _parse_text_blocks, _row_top, _span_char_x, _span_text,
                         _text_line_rects, _union_line_rect)
from .images import (_FIG_GAP, _FIG_MIN_H, _FIG_MIN_PARTS, _FIG_MIN_W, _FIG_PAD,
                     _IMG_OK, _carve_images, _cluster_rects, _covered_ratio,
                     _extract_raster, _extract_vector_figures, _image_rect,
                     _invisible_text_rects, _merge_rects, _overlap_area,
                     _render_region_png, _suppress_ghost_text)
from .options import (CONFIG_FILE, DEFAULT_IMAGE_LABELS, DEFAULT_OPTIONS,
                      PARSER_MODES, _ENV_KEYS, _to_bool, load_options, status)
from .backend_pymupdf import (_blank_math_and_prune, _local_page,
                              _parse_with_pymupdf, _pdf_outline,
                              _strip_font_keys)
from .backend_surya import _parse_with_surya
from .backend_unlimited_ocr import _parse_with_unlimited_ocr
from .math_text import (_ANCHOR_LENS, _ANCHOR_MIN_TOTAL, _ANCHOR_RANK_GAP,
                        _ANCHOR_RANK_MIN, _ANCHOR_SPAN_MAX, _DISPLAY_MATH_RE,
                        _GLYPH_CLS, _GLYPH_RE, _LIG_EXPAND,
                        _LINE_MATH_TOKEN_RE, _NON_GLYPH_PREFIX_RE,
                        _NORM_CHAR_RE, _SPAN_MAX_LINES, _STYLE_KEYS,
                        _all_pos, _anchor_of, _apply_replacements,
                        _bag_ratio, _body_run, _count_chars, _gap_ok,
                        _has_glyphs, _lines_text, _looks_math_body,
                        _math_glyphs, _math_ish, _meta_at, _norm_keep,
                        _norm_keep_idx, _ocr_math_lines, _page_index,
                        _plain_math, _same_style, _split_line_math, _tighten)
from .math_font import (_CM_TEXT_RE, _LINE_MATH_PURE, _MATH_FONT_STRONG,
                        _MATH_FONT_WEAK, _MERGE_GAP_CHARS, _SYMBOL_FONT,
                        _accumulate_stats, _blank_math_level, _blank_math_spans,
                        _cm_text_math_on, _font_base, _font_math_index,
                        _font_math_level, _font_math_line, _group_math_pieces,
                        _has_symbol_font, _line_math_spans, _line_text,
                        _mask_out, _retighten, _weak_math_enabled, _widen_cut,
                        _widen_with_cand)
from .entry import parse_pdf, pop_private
