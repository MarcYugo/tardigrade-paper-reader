"""Surya 2 解析后端：版式来源 = Surya 整页 OCR 的版面 block（前端独立渲染分支）。

与本地解析方案的区别：

  * **完全使用 Surya 的数据**：文字 / 公式（`<math>` → LaTeX）/ 插图裁图 / 阅读顺序
    全部来自 Surya 的版面 block；不读 PyMuPDF 的文字层、行框与字体。Surya
    不可用时**直接报错**（不会悄悄回退 PyMuPDF，
    免得用户以为看到了 Surya 的效果）。
  * 页面 `texts[]` 是**块级**数据（没有逐行几何）：

        {"id": "p1b2", "kind": "heading"|"text"|"caption"|"footnote"|"list"
                |"formula"|"table"|"header"|"footer",
         "label": "SectionHeader",          # Surya 原始标签（排障 / 样式参考）
         "level": 1,                        # 标题级别（仅 heading）
         "x","y","w","h": 86.6,             # 绝对坐标（PDF pt）
         "size","line_h": 10.0,             # 字号 / 行高估算（pt；前端直接乘缩放比）
         "align": "justify",                # 对齐（left / justify；正文类＝justify）
         "text": "1 Introduction",          # 块文本（数学以 LaTeX 源码内联）
         "lines": [{"runs": [{"t": "1 Introduction", "b": true}]}],
         "math": [{"a": 3, "b": 12, "latex": "…"}],   # text 里的数学区间
         "html": "<h1>1 Introduction</h1>"}           # 服务原始 HTML（表格渲染用）

  * 前端（`frontend/js/reader2.js::buildSuryaItem`，`DOC.parser === "surya"` 时启用）
    走**块级流式排版**：块按 bbox 绝对定位，块内文字正常换行；标题加粗放大、
    图注/脚注小一号 —— 与 PyMuPDF 路径的「逐行绝对定位 + 行宽适配」是两套渲染。
    正文类块（text/list/caption/footnote）按 `align="justify"` **两端对齐**：
    原 PDF（LaTeX 排版）的段落默认 justified、行右缘齐平到栏边界，流式排版若
    保持默认左对齐，右缘会随断行参差不齐；`text-align: justify` 的末行不拉伸，
    与原文一致。标题/页眉页脚保持左对齐（单行，两端对齐无意义）。双栏页面上，
    检测框的膨胀会把两列间隙挤没（实测原 gutter 9.97pt → 检测框 0.96pt），
    解析时由 `_restore_column_gaps` 把间隙恢复回 10pt（见该函数注释）。
    行内公式按 `math` 的**精确字符区间**把原字形**就地换成 KaTeX**（`suryaInlineMath`，
    正文围绕真公式自然重排）；独立公式（`kind="formula"` + `latex`）走 KaTeX 块渲染；
    表格（`kind="table"` + `html`）由前端白名单渲染。
  * 插图（Picture/Figure/Diagram/ChemicalBlock）裁剪落盘到 `images_dir`，
    命名 `p{页:03d}_i{序号:03d}.png`（与 `/api/doc/{id}/img/{fname}` 口径一致），
    写进 `pages[].images`（绝对坐标 pt），前端沿用图片层渲染。

坐标系：Surya 的 bbox / 裁剪框都是**页面图像像素**，这里统一乘 `72/dpi` 换算成
PDF 点；页面 `w/h` 也按渲染图尺寸换算（旋转已在渲染时归一化），全页只有一把尺子。

统计（`parser_info`）：`surya_url` / `surya_backend` / `surya_dpi` / `surya_ms` /
`surya_blocks`（落盘块数）/ `surya_images` / `surya_labels`（原始标签直方图）+
各渲染 kind 的条数（`surya_kind_*`）。

服务端镜像见仓库根 `surya_doc_parse_service/`；客户端见 `../surya_parser.py`。
"""
from __future__ import annotations

import hashlib
import math
import statistics
from pathlib import Path
from typing import Optional

from .. import surya_parser

__all__ = ["_parse_with_surya"]

# 页基准字号（pt）的兜底：拿不到任何块估算值时用
_FALLBACK_BODY_SIZE = 10.0
# 标题级别 → 相对页正文的字号倍率（Surya 不给字号，只能按排版惯例估）
_HEADING_MUL = {1: 1.45, 2: 1.22, 3: 1.08, 4: 1.02, 5: 1.0, 6: 1.0}
# 正文类 kind：两端对齐（align="justify"）、参与列间隙恢复（见 _restore_column_gaps）
_JUSTIFY_KINDS = ("text", "list", "caption", "footnote")


# =====================================================================
#  字号估算（Surya 卡里没有字号 —— 按「字宽 0.5em、行距 1.45em」反解）
# =====================================================================
def _estimate_size_pt(block_w: float, block_h: float, chars: int) -> float:
    """单块字号粗估：`h = nlines × 1.45s`、`nlines = chars × 0.5s / w`，解出 s。"""
    chars = max(8, int(chars))
    if block_w <= 0 or block_h <= 0:
        return 0.0
    return math.sqrt(block_h * block_w / (0.725 * chars))


def _document_body_size(blocks: list[dict]) -> float:
    """全 PDF 正文基准字号（pt）：跨所有页面取中位数，避免不同页差异。"""
    ests = [_estimate_size_pt(b["w"], b["h"], len(b["text"]))
            for b in blocks
            if b["kind"] in ("text", "list") and len(b["text"]) >= 60
            and 5.0 <= _estimate_size_pt(b["w"], b["h"], len(b["text"])) <= 22.0]
    if not ests:
        return _FALLBACK_BODY_SIZE
    return min(20.0, max(6.0, statistics.median(ests)))


def _fit_size(size: float, w: float, h: float, text: str) -> float:
    """按块框「缩字适配」：预计排版高度明显超过块框时，把字号按比例收一点。

    Surya 不给字号，正文统一用页基准字号最耐看；但块内的排版字体是浏览器字体
    （Georgia 系），**平均字宽 ≈ 0.62em**，比原论文的排版字体宽不少 —— 同一字号下
    行数会多出一截、块高超出原框，压到下面的插图/文字上（实测摘要/图注/双栏正文
    都能超 30~90px）。这里按「行数 ≈ chars / (w / 0.62s)、行距 1.42s」估高度，
    超出块框 5% 就等比缩（下限 0.70 倍，别把字缩得和旁边差太多）。
    """
    chars = len(text or "")
    if w <= 0 or h <= 0 or chars < 40 or size <= 0:
        return size
    est_h = chars * 0.62 * size / w * 1.42 * size
    if est_h <= h * 1.05:
        return size
    return max(size * 0.70, size * math.sqrt(h * 1.05 / est_h))


def _block_metrics(kind: str, level: Optional[int], w: float, h: float,
                   text: str, body: float) -> tuple[float, float]:
    """块的（字号, 行高）估算，单位 pt。

    字号：标题按级别放大、图注/脚注/页眉页脚小一号，正文/list 用页基准字号；
    块宽高只决定换行与容器位置，不再二次缩放正文字号。行高按类型给固定倍率
    （正文 1.42、标题 1.25、图注 1.32、表格 1.3）。
    """
    if kind == "heading":
        size = body * _HEADING_MUL.get(level or 2, 1.12)
        lh_mul = 1.25
    elif kind == "caption":
        size = body * 0.92
        lh_mul = 1.32
    elif kind in ("footnote", "header", "footer"):
        size = body * 0.88
        lh_mul = 1.32
    elif kind == "table":
        size = body * 0.95
        lh_mul = 1.30
    elif kind == "formula":
        size = body
        lh_mul = 1.30
    else:                        # text / list
        size = body
        lh_mul = 1.42
    size = min(28.0, max(5.5, size))
    # Surya 的块宽高只能决定换行与容器位置，不能决定正文字号。
    # 对正文、caption、footnote、list 等按页统一基准统一缩放，避免
    # 同一份 PDF 的不同段落因块宽/块高反推出不一致字号。
    return round(size, 2), round(size * lh_mul, 2)


# =====================================================================
#  列间隙恢复（双栏页面：检测框膨胀把列间空当挤没了）
# =====================================================================
# Surya 的 block bbox 来自渲染图上的目标检测，框比文字本身向外**膨胀**
# （实测本仓库样本 dpi=150 每边 3.5~5.5pt）。单栏页面无感；双栏页面上，
# 左栏右缘与右栏左缘的检测框几乎贴在一起 —— 原 PDF 的 gutter 实测 9.97pt
# （LaTeX 默认 \columnsep=10pt），检测框只剩 0.96pt；流式排版（尤其是两端
# 对齐）后两栏文字就挤到一处，与原文观感明显不符。
# 修复：把「垂直重叠、水平相邻且间隙 < _MIN_COL_GAP」的正文类块对之间的
# 空隙恢复到 _MIN_COL_GAP（两侧各让一半：左块右缘左移、右块左缘右移），
# 每个块至多修一次。只认正文类块，且要求**至少一侧是多行块**（单行×单行
# 的紧邻更可能是检测器把一行拆成两块，不碰）；标题/公式/表格/页眉页脚
# 不参与。块是绝对定位的，只改内容区，页面/栏的外侧边距不动。
_MIN_COL_GAP = 10.0        # pt：相邻列之间应保留的空隙（LaTeX 默认 \columnsep）
_COL_EDGE_TOL = 1.5        # pt：检测框允许的轻微重叠（负间隙）容差


def _restore_column_gaps(items: list[dict]) -> None:
    """把双栏页被检测框膨胀吃掉的列间空隙恢复回 _MIN_COL_GAP（就地修改）。

    调用时机：块字号/行高估算**之后**（判「多行」要用估出的 line_h）。
    """
    cand = [it for it in items if it.get("kind") in _JUSTIFY_KINDS]
    if len(cand) < 2:
        return

    def _multiline(it: dict) -> bool:
        # 检测框纵向也有膨胀（h 略大于实际），阈值取 2 行而不是 1 行：
        # 单行块 h/line_h 实测约 1.2~1.6，两行以上的块约 ≥ 2.5。
        lh = float(it.get("line_h") or 0)
        return lh > 0 and it.get("h", 0) >= lh * 2.0

    used = set()
    for a in cand:
        if a["id"] in used:
            continue
        for b in cand:
            if b is a or b["id"] in used or b["x"] <= a["x"]:
                continue
            gap = b["x"] - (a["x"] + a["w"])
            if not (-_COL_EDGE_TOL <= gap < _MIN_COL_GAP):
                continue
            overlap = (min(a["y"] + a["h"], b["y"] + b["h"])
                       - max(a["y"], b["y"]))
            if overlap <= 0 or overlap < min(a["h"], b["h"]) * 0.5:
                continue
            if not (_multiline(a) or _multiline(b)):
                continue
            d = (_MIN_COL_GAP - gap) / 2.0
            if a["w"] - d < 24 or b["w"] - d < 24:
                continue        # 防御：别把块挤没了
            a["w"] = round(a["w"] - d, 3)
            b["x"] = round(b["x"] + d, 3)
            b["w"] = round(b["w"] - d, 3)
            # lines[0] 是块宽/位置的镜像（前端主要用 item 的 x/w，保持一致）
            if a.get("lines"):
                a["lines"][0]["w"] = a["w"]
            if b.get("lines"):
                b["lines"][0]["x"] = b["x"]
                b["lines"][0]["w"] = b["w"]
            used.add(a["id"])
            used.add(b["id"])
            break


# =====================================================================
#  插图 / 元数据
# =====================================================================
def _figure_image(block: dict, images_dir: Optional[Path], scale: float) -> Optional[dict]:
    """图片类 block → `pages[].images` 条目（pt 坐标；图片由 surya_parser 已落盘）。"""
    info = block.get("image")
    if not isinstance(info, dict) or images_dir is None:
        return None
    fname = str(info.get("file") or "").strip()
    bounds = info.get("bbox") or block.get("bbox")
    if not fname or not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bounds)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    try:
        digest = hashlib.sha1((Path(images_dir) / fname).read_bytes()).hexdigest()[:12]
    except OSError:
        return None
    return {
        "x": round(x0 * scale, 3), "y": round(y0 * scale, 3),
        "w": round((x1 - x0) * scale, 3), "h": round((y1 - y0) * scale, 3),
        "file": fname, "source": "surya",
        "class_name": str(block.get("label") or "Figure"),
        "cache_key": digest,
    }


def _pdf_meta(pdf_path: Path) -> tuple[str, list[dict], int]:
    """PDF 标题 / 自带书签 / 页数（只读元数据，不参与版式渲染）。"""
    try:
        import fitz
    except ImportError:
        return "", [], 0
    doc = fitz.open(str(pdf_path))
    try:
        title = str((doc.metadata or {}).get("title") or "").strip()
        toc: list[dict] = []
        try:
            raw = doc.get_toc(simple=True) or []
        except Exception:
            raw = []
        for row in raw:
            try:
                level, name, page = int(row[0]), str(row[1] or "").strip(), int(row[2])
            except Exception:
                continue
            if name:
                toc.append({"level": max(1, min(level, 4)),
                            "title": name[:200], "page": max(1, page)})
        return title, toc, int(doc.page_count)
    finally:
        doc.close()


# =====================================================================
#  入口：Surya 版面数据 → doc.json 的 pages 结构
# =====================================================================
def _parse_with_surya(pdf_path: Path, images_dir: Optional[Path], opts: dict) -> dict:
    """整份 PDF 走 Surya 2 整页 OCR，转成阅读器的版式数据。

    失败一律抛 `SuryaUnavailable`（由 `entry.parse_pdf` 向上抛给转换接口，
    页面上会显示具体原因）—— **不回退 PyMuPDF**，见模块文档。
    """
    raw = surya_parser.ocr_pdf(pdf_path, opts, images_dir=images_dir)
    scale = 72.0 / float(raw.get("dpi") or 192)
    pdf_title, toc, pdf_pages = _pdf_meta(pdf_path)

    pages_out: list[dict] = []
    kind_counts: dict[str, int] = {}
    total_images = 0
    document_blocks: list[dict] = []
    for pg in raw.get("pages") or []:
        mids: list[dict] = []
        images: list[dict] = []
        for order, b in enumerate(pg.get("blocks") or []):
            text, runs, math = surya_parser.html_to_text_runs(b.get("html"))
            kind = surya_parser.label_kind(b.get("label"), b.get("raw_label"),
                                           b.get("html"), text)
            if kind is None:
                continue
            bounds = b.get("bbox") or [0, 0, 0, 0]
            try:
                x0, y0, x1, y1 = (float(v) * scale for v in bounds[:4])
            except (TypeError, ValueError):
                continue
            if x1 - x0 <= 0.5 or y1 - y0 <= 0.5:
                continue

            if kind == "figure":
                image = _figure_image(b, images_dir, scale)
                if image is None:
                    continue        # 裁图缺失/过小：没有可显示的东西
                images.append(image)
                total_images += 1
                kind_counts["figure"] = kind_counts.get("figure", 0) + 1
                continue

            item: dict = {
                "id": f"p{pg.get('page')}b{order}",
                "kind": kind,
                "label": b.get("label") or "",
                "raw_label": b.get("raw_label") or "",
                "x": round(x0, 3), "y": round(y0, 3),
                "w": round(x1 - x0, 3), "h": round(y1 - y0, 3),
                "text": text,
                "math": math,
                "html": b.get("html") or "",
                "surya": True,
                "lines": [],        # 文本块下面填入唯一的"伪行"；公式/表格为空表
            }
            # 对齐方式：原 PDF（LaTeX 排版）的正文段落默认**两端对齐**（justified），
            # 行右缘齐平到栏边界；流式排版若保持默认左对齐，右缘会随断行参差不齐
            # （与原文观感明显不符）。正文类块下发 "justify"（前端 `text-align:
            # justify`，末行不拉伸 —— 与原 PDF 的末行一样自然收尾）；标题/页眉页脚
            # 保持 "left"（标题多为单行，两端对齐无意义）；公式/表格各自渲染，
            # 不消费该字段。前端对旧数据（无 align）会按同一规则兜底推断。
            item["align"] = "justify" if kind in _JUSTIFY_KINDS else "left"
            if kind == "heading":
                item["level"] = surya_parser.heading_level(b.get("html"), text)
            if kind == "formula":
                latex = surya_parser.latex_from_html(b.get("html"))
                if not latex:
                    # 拿不到 LaTeX 的"公式块"：没有可渲染的内容，纯浪费位置
                    continue
                item["latex"] = latex
                item["text"] = ""           # 公式块不参与翻译/句子（前端只渲 KaTeX）
                item["math"] = []
            elif kind in ("text", "caption", "footnote", "list",
                          "header", "footer") and not text:
                continue                    # 空文本块：没有可显示/可选中的内容
            if runs and kind != "formula":
                # 一行"伪行"装 runs：前端 buildSuryaItem 只取 lines[0]，
                # 但保留 lines 结构可以让 selftest 等现有工具不用改口径。
                item["lines"] = [{
                    "x": item["x"], "y": item["y"],
                    "w": item["w"], "h": item["h"],
                    "runs": runs,
                }]
            mids.append(item)
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            document_blocks.append({
                "kind": kind,
                "w": item["w"],
                "h": item["h"],
                "text": item["text"],
            })

        page = {
            "w": round(pg.get("w", 0) * scale, 3),
            "h": round(pg.get("h", 0) * scale, 3),
            "texts": mids,
            "images": images,
            "text": "\n\n".join(t["text"] for t in mids if t.get("text")),
        }
        pages_out.append(page)

    # 全 PDF 统一正文基准；caption、table、footnote 等仅按类型相对缩放。
    body = _document_body_size(document_blocks)
    for page in pages_out:
        for item in page["texts"]:
            size, line_h = _block_metrics(item["kind"], item.get("level"),
                                          item["w"], item["h"], item["text"], body)
            item["size"] = size
            item["line_h"] = line_h
            if item.get("lines"):
                item["lines"][0]["s"] = size

    # 列间隙恢复：双栏页面上检测框膨胀会把两列之间的空当挤没（两端对齐后
    # 两栏文字贴到一起）—— 用估好的 line_h 判多行，把间隙修回 _MIN_COL_GAP。
    for page in pages_out:
        _restore_column_gaps(page["texts"])

    stats = dict(raw.get("stats") or {})
    return {
        "num_pages": len(pages_out),
        "page_w": pages_out[0]["w"] if pages_out else 0,
        "page_h": pages_out[0]["h"] if pages_out else 0,
        "pdf_title": pdf_title,
        "pages": pages_out,
        "toc": toc,                    # PDF 自带书签（没有就是空列表）
        "parser": "surya",
        # 公式怎么落地的：surya = 公式来自 `<math>` 的 LaTeX（行内覆盖层 + 独立公式块），
        # 与 "space"（PyMuPDF 擦成空格）、"boxes"（服务组公式框覆盖层）并列。
        "formula_action": "surya",
        "parser_info": {
            "surya_url": raw.get("url"),
            "surya_backend": raw.get("backend"),
            "surya_dpi": raw.get("dpi"),
            "surya_ms": stats.get("surya_ms", 0),
            "surya_blocks": sum(len(p["texts"]) + len(p["images"]) for p in pages_out),
            "surya_pdf_pages": pdf_pages,
            "surya_images": total_images,
            "surya_labels": stats.get("surya_labels") or {},
            **{f"surya_kind_{k}": v for k, v in sorted(kind_counts.items())},
        },
    }
