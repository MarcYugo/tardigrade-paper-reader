"""公式框与 Figure 图片增强（已停用）。

原实现（detection-service-group 全页检测 → 公式框覆盖层 + Figure 裁图）依赖本地
PyMuPDF 版式与 `pages[]._formula_slots`，已随本地解析链路一起移除。本模块只保留
函数名 / 签名，函数体为空壳，方便之后用新的实现替换。
"""

from __future__ import annotations

from pathlib import Path


def _figure_image(detection: dict, page: dict, page_index: int,
                  image_index: int, images_dir: Path | None) -> dict | None:
    """[已停用] 服务组 Figure 裁图 → 本地图片条目。"""
    return None


def apply_formula_boxes(result: dict, pdf_path: Path, opts: dict,
                        warnings: list[str], *, images_dir: Path | None = None,
                        predict=None) -> dict:
    """[已停用] 对解析结果补充服务组公式框和 Figure 裁图。

    本地 PyMuPDF 解析链路已移除，这里不再做任何增强，原样返回 `result`。
    """
    return result
