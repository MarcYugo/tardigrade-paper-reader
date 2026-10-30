"""插图（已停用）：原 PyMuPDF 去重影 / 点阵图 / 矢量图实现已整体移除。

本模块只保留函数名 / 签名与导出常量（`backend/pdf_parser/__init__.py` 仍会把它们
再导出一次），函数体全部为空壳，方便之后用新的实现替换。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:                      # 仅用于类型标注；运行时不依赖 PyMuPDF
    import fitz


_IMG_OK = {"png", "jpeg", "jpg", "webp", "gif"}

# 矢量图形判据（原实现使用；保留为导出常量）。
_FIG_MIN_PARTS = 3        # 成图至少要有几个绘图元素
_FIG_MIN_W = 30.0         # 图形区域最小宽（磅）
_FIG_MIN_H = 20.0         # 图形区域最小高（磅）
_FIG_GAP = 5.0            # 聚类间隙（磅）
_FIG_PAD = 2.0            # 栅格化外扩（磅）


def _image_rect(im: dict) -> fitz.Rect:
    """[已停用] 插图字典 → 矩形。"""
    return None


def _merge_rects(rects, gap: float = 1.0) -> list[fitz.Rect]:
    """[已停用] 合并相交(或相距小于 gap)的矩形。"""
    return []


def _covered_ratio(rect: fitz.Rect, covers: list[fitz.Rect]) -> float:
    """[已停用] rect 被 covers 覆盖的面积比例。"""
    return 0.0


def _invisible_text_rects(page) -> list[fitz.Rect]:
    """[已停用] 收集「不可见文字」的 span 外框。"""
    return []


def _suppress_ghost_text(texts: list[dict], images: list[dict],
                         invisible: list[fitz.Rect], opts: dict
                         ) -> tuple[list[dict], int]:
    """[已停用] 删掉“已经烘焙进插图”的文字行。"""
    return texts, 0


def _extract_raster(page, doc, images_dir, pno, idx0: int = 0) -> list[dict]:
    """[已停用] 抽取内嵌点阵图。"""
    return []


def _cluster_rects(rects, gap: float = 6.0) -> list[fitz.Rect]:
    """[已停用] 把彼此靠近的图形外框合并成若干“图形区域”。"""
    return []


def _overlap_area(a: fitz.Rect, b: fitz.Rect) -> float:
    """[已停用] 两个矩形的交集面积。"""
    return 0.0


def _render_region_png(page, rect: fitz.Rect, images_dir, pno, idx) -> Optional[str]:
    """[已停用] 把页面区域栅格化成 PNG。"""
    return None


def _extract_vector_figures(page, images_dir, pno, texts, placed_raster,
                            idx0: int = 0) -> list[dict]:
    """[已停用] 识别矢量线条/色块绘制的图表。"""
    return []


def _carve_images(images: list[dict], page, images_dir: Optional[Path],
                  pno: int, hole: fitz.Rect, idx: int) -> int:
    """[已停用] 把盖住 `hole` 的「烘焙图」挖个洞。"""
    return idx
