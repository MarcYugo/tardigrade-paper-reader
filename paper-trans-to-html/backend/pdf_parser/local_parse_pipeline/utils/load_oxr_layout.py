"""加载 OXR 版面分析（layout analysis）模型并跑推理的独立脚本。

模型：D-FINE Stage-2 B2，12 类，960x960 输入，输出**原图像素坐标**的检测框，
不提供阅读顺序（阅读顺序由 OXR 主模型负责）。
权重与 metadata 位于 `local_parse_pipeline/models/oxr_layout_analysis/`：
  - `last.pth`       D-FINE 权重；
  - `metadata.json`  模型版本 / 类别 / checkpoint 大小与 SHA-256，加载前强校验。

推理流程直接复用服务端实现 `local_parse_pipeline/utils/dfine_la`（进程内调用，不经过 HTTP）：
  1. `Settings.validate_checkpoint()`：读 metadata，校验架构/类别/尺寸，
     并核对 checkpoint 的文件大小与 SHA-256；
  2. `ModelRuntime.__init__`：构建 D-FINE（backbone+encoder+decoder+postprocessor），
     `torch.load` 载入 `ema.module`（无则 `model`），CUDA 下包一层 CUDA Graph 并 warmup；
  3. `ModelRuntime.prepare()`：OpenCV 解码 -> BGR 转 RGB -> 等比缩放 + 114 灰边填充到 960，
     转成 0-1 的 float CHW 张量，同时记录每张图原始 [w, h]；
  4. `ModelRuntime.infer()`：`no_grad` 前向（CUDA 走 FP16 autocast），
     postprocessor 做 topk 选 300 个 query 并把 letterbox 坐标还原到原图；
  5. `format_predictions()`：按全局置信度与按类别阈值过滤，保持分数降序。

作为模块使用（只需拿到 engine，后续可反复调用 predict）：
    from load_oxr_layout import load_oxr_layout

    engine = load_oxr_layout(device="cuda")        # 只加载一次
    result = engine.predict([png_bytes], confidence=0.3)
    print(result["pages"][0]["regions"])

命令行用法：
  python load_oxr_layout.py input.pdf
  python load_oxr_layout.py page.png --confidence=0.3 --save-dir=drawn
  python load_oxr_layout.py input.pdf --pages=1-3 --out=layout.json

选项（`--key=value` 或 `--key value` 均可）：
  --checkpoint=PATH          last.pth 路径，默认 models/oxr-1.0/layout_analysis/last.pth
  --metadata=PATH            metadata.json 路径，默认与 checkpoint 同目录
  --device=DEVICE            auto / cuda / cpu，默认 auto（有 GPU 则用 GPU）
  --confidence=F             全局置信度阈值，默认 0.3
  --class-confidences=JSON   按类别覆盖阈值，如 '{"8":0.5}'（键是类别 id）
  --dpi=N                    PDF 页面渲染 dpi，默认 200
  --pages=SPEC               只处理这些 PDF 页，如 `1,3-5`；默认全部
  --out=PATH                 结果 JSON 路径，默认 <首个输入>.layout.json
  --save-dir=DIR             把画好检测框的图存到该目录（默认不画）
  --no-cuda-graph            关闭 CUDA Graph（显存紧张或出问题时可关）
  --raw                      额外输出未展开的原始 labels/boxes/scores 数组
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent   # local_parse_pipeline/utils
PROJECT_DIR = SCRIPT_DIR.parent                # local_parse_pipeline
# `dfine_la` 包就在本文件同级目录，把它加进 sys.path 即可 import（无需 OXR 服务端目录）
SERVICE_DIR = SCRIPT_DIR
DEFAULT_MODEL_DIR = PROJECT_DIR / "models" / "oxr_layout_analysis"
DEFAULT_CHECKPOINT = DEFAULT_MODEL_DIR / "last.pth"
DEFAULT_METADATA = DEFAULT_MODEL_DIR / "metadata.json"
DEFAULT_CONFIDENCE = 0.3
DEFAULT_DPI = 200
DEFAULT_IOU_DEDUPE = 0.9
DRAW_COLORS = [  # BGR，按类别 id 取模
    (86, 86, 255), (60, 160, 255), (60, 220, 240), (110, 220, 120),
    (190, 200, 60), (255, 160, 80), (255, 120, 150), (220, 110, 230),
    (140, 140, 140), (140, 200, 0), (60, 60, 60), (80, 120, 200),
]


# ---------------------------------------------------------------- 后端加载


def _load_backend():
    """延迟 import dfine_la：让 --help 与参数错误在缺少 torch 时也能用。"""
    if str(SERVICE_DIR) not in sys.path:
        sys.path.insert(0, str(SERVICE_DIR))
    try:
        from dfine_la.confidence import format_predictions, parse_confidence_policy
        from dfine_la.config import Settings
        from dfine_la.runtime import ModelRuntime
    except ImportError as exc:  # torch / opencv 缺失时给出可操作的提示
        raise RuntimeError(
            f"无法 import dfine_la（{exc}）；请先安装 "
            f"{SERVICE_DIR / 'requirements.txt'} 中的依赖（含 torch、opencv-python-headless）"
        ) from exc
    return Settings, ModelRuntime, format_predictions, parse_confidence_policy


class OxrLayoutEngine:
    """版面分析 engine：一个进程内只加载一次，之后可反复调用 predict。"""

    def __init__(
        self,
        checkpoint: str | Path | None = None,
        metadata: str | Path | None = None,
        device: str = "auto",
        cuda_graph: bool = True,
        parallel_preprocess: bool = False,
    ) -> None:
        Settings, ModelRuntime, format_predictions, parse_confidence_policy = _load_backend()

        checkpoint = Path(
            checkpoint or os.getenv("OXR_LAYOUT_CHECKPOINT") or DEFAULT_CHECKPOINT
        )
        if metadata:
            metadata_path = Path(metadata)
        elif checkpoint == DEFAULT_CHECKPOINT:
            metadata_path = DEFAULT_METADATA
        else:
            metadata_path = checkpoint.with_name("metadata.json")
        if os.getenv("OXR_LAYOUT_METADATA"):
            metadata_path = Path(os.environ["OXR_LAYOUT_METADATA"])

        device = (device or os.getenv("OXR_LAYOUT_DEVICE") or "auto").strip().lower()
        if device not in {"auto", "cpu", "cuda"}:
            raise ValueError(f"device 只能是 auto / cpu / cuda，收到 {device!r}")
        if not metadata_path.is_file():
            raise RuntimeError(
                f"metadata.json 不存在：{metadata_path}（先用 download.sh 下载模型）"
            )

        settings = Settings(
            model_path=checkpoint,
            metadata_path=metadata_path,
            device=device,
            cuda_graph_enabled=cuda_graph,
            parallel_preprocess=parallel_preprocess,
        )
        # 构造 ModelRuntime 内部会做 validate_checkpoint + 载入权重 + CUDA Graph warmup
        self.runtime = ModelRuntime(settings)
        self.settings = self.runtime.settings  # 校验后已填好 model_version / id2label 等
        self.id2label = self.settings.id2label
        self._format_predictions = format_predictions
        self._parse_confidence_policy = parse_confidence_policy

    # ------------------------------------------------------------ 信息

    @property
    def device(self):
        return self.runtime.device

    @property
    def model(self):
        """底层 torch 模型（D-FINE + postprocessor，CUDA 下可能是 CUDA Graph 包装）。"""
        model = self.runtime._engines.get(block=True)
        try:
            return model
        finally:
            self.runtime._engines.put(model)

    def info(self) -> dict:
        return {
            "model_version": self.settings.model_version,
            "architecture": self.settings.architecture,
            "num_classes": self.settings.num_classes,
            "image_size": self.settings.image_size,
            "color_order": self.settings.color_order,
            "resize_backend": self.settings.resize_backend,
            "postprocess": self.settings.postprocess,
            "provides_reading_order": False,
            "device": str(self.runtime.device),
            "cuda_graph": self.runtime.cuda_graph_ready,
            "id2label": dict(self.id2label),
        }

    # ------------------------------------------------------------ 推理

    def predict(
        self,
        payloads: list[bytes],
        confidence: float | None = DEFAULT_CONFIDENCE,
        class_confidences: str | None = None,
        include_raw: bool = False,
    ) -> dict:
        """对一组图片字节做版面分析，返回按页展开的结果。

        payloads 顺序即返回 pages 顺序；boxes 为原图像素坐标 [x1, y1, x2, y2]。
        """
        if not payloads:
            raise ValueError("payloads 不能为空")

        global_confidence, overrides, params = self._parse_confidence_policy(
            confidence, class_confidences, self.settings.num_classes
        )

        started = time.perf_counter()
        ready, decode_ms, preprocess_ms = self.runtime.prepare(payloads)
        sizes = ready[1].tolist()  # [[w, h], ...]
        outputs, timings = self.runtime.infer(ready)
        total_ms = (time.perf_counter() - started) * 1000

        predictions = self._format_predictions(
            outputs, global_confidence, overrides, params
        )

        pages = []
        for index, (width, height) in enumerate(sizes):
            regions = []
            for label, box, score in zip(
                predictions["data"]["labels"][index],
                predictions["data"]["boxes"][index],
                predictions["data"]["scores"][index],
            ):
                label_id = int(label)
                regions.append(
                    {
                        "label_id": label_id,
                        "label": self.id2label.get(str(label_id), str(label_id)),
                        "score": round(float(score), 6),
                        "bbox": [round(float(value), 2) for value in box],
                    }
                )
            pages.append(
                {
                    "source": None,  # 由调用方填文件名/页码
                    "width": int(width),
                    "height": int(height),
                    "regions": regions,
                }
            )

        result = {
            "status": "success",
            "model": self.info(),
            "params": params,
            "timings_ms": {
                "decode": round(decode_ms, 3),
                "preprocess": round(preprocess_ms, 3),
                "queue": round(timings["queue"], 3),
                "inference": round(timings["inference"], 3),
                "total": round(total_ms, 3),
            },
            "pages": pages,
        }
        if include_raw:
            result["raw"] = {
                "labels": predictions["data"]["labels"],
                "boxes": predictions["data"]["boxes"],
                "scores": predictions["data"]["scores"],
            }
        return result

    def predict_paths(
        self,
        paths: list[str | Path],
        dpi: int = DEFAULT_DPI,
        pages: str | None = None,
        confidence: float | None = DEFAULT_CONFIDENCE,
        class_confidences: str | None = None,
    ) -> dict:
        """按文件路径推理（自动展开 PDF 页面），返回结果字典。"""
        items = load_inputs([Path(path) for path in paths], dpi=dpi, pages=pages)
        if not items:
            raise ValueError("没有可处理的页面/图片")
        result = self.predict(
            [png for _, png in items],
            confidence=confidence,
            class_confidences=class_confidences,
        )
        for page, (name, _) in zip(result["pages"], items):
            page["source"] = name
        result["input"] = [str(path) for path in paths]
        return result

    # ------------------------------------------------------ 页面级适配

    def predict_page_detail(
        self,
        page,
        dpi: int = DEFAULT_DPI,
        confidence: float | None = DEFAULT_CONFIDENCE,
        class_confidences: str | None = None,
        iou_threshold: float = DEFAULT_IOU_DEDUPE,
    ) -> tuple[dict, list["LayoutDetection"]]:
        """对单个 PyMuPDF 页面跑版面分析，返回 ``(页级结果, 检测框列表)``。

        - 页级结果与 ``out_samples/attention_layout.json`` 的 ``pages[i]`` 同构：
          ``{source, width, height, regions[{label_id, label, score, bbox}]}``，
          其中 ``width`` / ``height`` 是渲染图像素尺寸，``bbox`` 是**像素坐标**；
        - 检测框列表把 bbox 换算成 **PDF 点坐标**（并与页面矩形求交）、去掉重复框，
          可直接交给 ``crop_from_page()`` / ``crop_formula_image()``。
        """
        result = self.predict(
            [render_page_png(page, dpi=dpi)],
            confidence=confidence,
            class_confidences=class_confidences,
        )
        layout_page = result["pages"][0]
        layout_page["source"] = page_source(page)
        detections = regions_to_detections(layout_page, page, iou_threshold=iou_threshold)
        return layout_page, detections

    def predict_page(
        self,
        page,
        dpi: int = DEFAULT_DPI,
        confidence: float | None = DEFAULT_CONFIDENCE,
        class_confidences: str | None = None,
        iou_threshold: float = DEFAULT_IOU_DEDUPE,
    ) -> list["LayoutDetection"]:
        """只返回检测框（与 YOLOv13 engine 的 ``predict_page`` 接口兼容）。"""
        _layout_page, detections = self.predict_page_detail(
            page,
            dpi=dpi,
            confidence=confidence,
            class_confidences=class_confidences,
            iou_threshold=iou_threshold,
        )
        return detections


# 兼容旧名字
OxrLayoutAnalyzer = OxrLayoutEngine


# ---------------------------------------------------------------- 版面结果适配

#: OXR 类别名 → 下游沿用的 YOLOv13 类别名（``extract_tables()`` 只认 ``Table`` / ``Figure``）
LABEL_ALIASES = {"Picture": "Figure"}

#: 类别名 → 角色（与 ``load_yolov13_arxivformula.CLASS_ROLES`` 对齐）
CLASS_ROLES = {
    "Table": "table",
    "Figure": "figure",
    "Formula": "display",
    "Caption": "caption",
    "Title": "title",
    "Text": "text",
    "Footnote": "footnote",
    "Page-header": "header",
    "Page-footer": "footer",
    "Code Block": "code",
    "Stamp": "stamp",
    "Chemical Structure": "chemical",
}


@dataclass
class LayoutDetection:
    """一条版面检测结果，字段与 YOLOv13 引擎的 ``Detection`` 兼容。"""

    label: str
    score: float
    bbox: tuple[float, float, float, float]  # PDF 点坐标
    label_id: int = -1
    role: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "label_id": self.label_id,
            "score": self.score,
            "role": self.role,
            "bbox": list(self.bbox),
        }


def _iou(a, b) -> float:
    x0, y0 = max(float(a[0]), float(b[0])), max(float(a[1]), float(b[1]))
    x1, y1 = min(float(a[2]), float(b[2])), min(float(a[3]), float(b[3]))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = max(0.0, float(a[2]) - float(a[0])) * max(0.0, float(a[3]) - float(a[1]))
    area_b = max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def dedupe_detections(
    detections: list["LayoutDetection"],
    iou_threshold: float = DEFAULT_IOU_DEDUPE,
) -> list["LayoutDetection"]:
    """同类别、同位置（IoU ≥ 阈值）的重复框只保留分数最高的那条。

    D-FINE 偶尔会把同一张插图输出两次（``attention_layout.json`` 里两条 Picture
    的 IoU≈0.99），不去重会让同一张图被裁两遍、前端重复渲染。
    """
    kept: list[LayoutDetection] = []
    for det in sorted(detections, key=lambda item: -item.score):
        if any(
            det.label == other.label and _iou(det.bbox, other.bbox) >= iou_threshold
            for other in kept
        ):
            continue
        kept.append(det)
    return kept


def render_page_png(page, dpi: int = DEFAULT_DPI) -> bytes:
    """整页渲染成 PNG 字节（覆盖 ``page.rect``，便于像素 ↔ 点坐标换算）。"""
    try:
        import pymupdf
    except ImportError:  # 兼容 PyMuPDF < 1.24
        import fitz as pymupdf

    pixmap = page.get_pixmap(dpi=int(dpi), colorspace=pymupdf.csRGB, alpha=False)
    return pixmap.tobytes("png")


def page_source(page) -> str:
    """页名，格式与 ``load_pdf_pages()`` 一致：``<pdf 名>_p<页码>``。"""
    number = int(getattr(page, "number", 0)) + 1
    name = getattr(getattr(page, "parent", None), "name", "") or ""
    stem = Path(name).stem
    return f"{stem}_p{number}" if stem else f"p{number}"


def regions_to_detections(
    layout_page: dict,
    page,
    iou_threshold: float = DEFAULT_IOU_DEDUPE,
) -> list["LayoutDetection"]:
    """页级结果（像素坐标）→ ``LayoutDetection`` 列表（PDF 点坐标，已去重）。"""
    page_rect = page.rect
    width = float(layout_page.get("width") or 0.0)
    height = float(layout_page.get("height") or 0.0)
    scale_x = page_rect.width / width if width > 0 else 1.0
    scale_y = page_rect.height / height if height > 0 else 1.0

    def to_point(value: float, origin: float, size: float, scale: float) -> float:
        return round(min(max(origin + value * scale, origin), origin + size), 2)

    detections: list[LayoutDetection] = []
    for region in layout_page.get("regions") or []:
        bbox = list(region.get("bbox") or [])
        if len(bbox) != 4:
            continue
        x0, y0, x1, y1 = (float(value) for value in bbox)
        box = (
            to_point(x0, page_rect.x0, page_rect.width, scale_x),
            to_point(y0, page_rect.y0, page_rect.height, scale_y),
            to_point(x1, page_rect.x0, page_rect.width, scale_x),
            to_point(y1, page_rect.y0, page_rect.height, scale_y),
        )
        if box[2] - box[0] < 1.0 or box[3] - box[1] < 1.0:
            continue  # 退化框（单像素噪声）裁不出内容
        raw_label = str(region.get("label") or "")
        label = LABEL_ALIASES.get(raw_label, raw_label)
        detections.append(
            LayoutDetection(
                label=label,
                score=float(region.get("score") or 0.0),
                bbox=box,
                label_id=int(region.get("label_id", -1)),
                role=CLASS_ROLES.get(label, "unknown"),
            )
        )
    return dedupe_detections(detections, iou_threshold)


def load_oxr_layout(
    checkpoint: str | Path | None = None,
    metadata: str | Path | None = None,
    device: str = "auto",
    cuda_graph: bool = True,
) -> OxrLayoutEngine:
    """加载版面分析模型并返回可复用的 engine。

    外部调用示例：
        from load_oxr_layout import load_oxr_layout

        engine = load_oxr_layout(device="cuda")
        engine.info()                            # 模型版本 / 类别 / 设备等
        engine.predict([png_bytes], confidence=0.3)
        engine.predict_paths(["paper.pdf"], pages="1-3")

    参数默认值可用环境变量覆盖：OXR_LAYOUT_CHECKPOINT / OXR_LAYOUT_METADATA /
    OXR_LAYOUT_DEVICE。
    """
    return OxrLayoutEngine(
        checkpoint=checkpoint,
        metadata=metadata,
        device=device,
        cuda_graph=cuda_graph,
    )


# ---------------------------------------------------------------- 输入读取


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """`1,3-5` -> 页号列表；空 spec 表示全部。"""
    if not spec:
        return list(range(1, page_count + 1))
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = part.split("-", 1)
                pages.update(range(int(start), int(end) + 1))
            else:
                pages.add(int(part))
        except ValueError:
            raise ValueError(f"--pages 无法解析：{part!r}（应形如 1,3-5）") from None
    return [page for page in sorted(pages) if 1 <= page <= page_count]


def load_pdf_pages(pdf_path: Path, dpi: int = DEFAULT_DPI, pages: str | None = None):
    """PDF 逐页渲染成 PNG 字节，返回 [(页名, png)]。"""
    try:
        import pymupdf
    except ImportError:  # 兼容 PyMuPDF < 1.24
        import fitz as pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        items = []
        for number in parse_pages(pages, doc.page_count):
            page = doc.load_page(number - 1)
            pixmap = page.get_pixmap(
                matrix=matrix, colorspace=pymupdf.csRGB, alpha=False
            )
            items.append((f"{pdf_path.stem}_p{number}", pixmap.tobytes("png")))
        return items
    finally:
        doc.close()


def load_inputs(paths: list[Path], dpi: int, pages: str | None):
    """把输入统一转成 [(页名, png 字节)]。"""
    items = []
    for path in paths:
        if path.suffix.lower() == ".pdf":
            items.extend(load_pdf_pages(path, dpi=dpi, pages=pages))
        else:
            items.append((path.stem, path.read_bytes()))
    return items


# ---------------------------------------------------------------- 可视化


def draw_regions(png: bytes, regions: list[dict]) -> np.ndarray:
    """在原始渲染图上画框并标注「类别 分数」，返回 BGR 图。"""
    import cv2

    image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("OpenCV 无法解码该图片")
    for region in regions:
        color = DRAW_COLORS[region["label_id"] % len(DRAW_COLORS)]
        x1, y1, x2, y2 = (int(round(value)) for value in region["bbox"])
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
        text = f'{region["label"]} {region["score"]:.2f}'
        (text_w, text_h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        top = max(y1 - text_h - 4, 0)
        cv2.rectangle(image, (x1, top), (x1 + text_w + 4, top + text_h + 4), color, -1)
        cv2.putText(
            image,
            text,
            (x1 + 2, top + text_h + 1),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    return image


# ---------------------------------------------------------------- 命令行


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="加载 OXR 版面分析模型（D-FINE Stage-2 B2）并对图片/PDF 跑推理"
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="图片或 PDF 路径")
    parser.add_argument("--checkpoint", type=Path, default=None, help="last.pth 路径")
    parser.add_argument("--metadata", type=Path, default=None, help="metadata.json 路径")
    parser.add_argument(
        "--device",
        default=os.getenv("OXR_LAYOUT_DEVICE", "auto"),
        help="auto / cuda / cpu，默认 auto",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=DEFAULT_CONFIDENCE,
        help=f"全局置信度阈值，默认 {DEFAULT_CONFIDENCE}",
    )
    parser.add_argument(
        "--class-confidences",
        default=None,
        help='按类别覆盖阈值，如 \'{"8":0.5}\'',
    )
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="PDF 渲染 dpi")
    parser.add_argument("--pages", default=None, help="只处理这些 PDF 页，如 1,3-5")
    parser.add_argument("--out", type=Path, default=None, help="结果 JSON 路径")
    parser.add_argument("--save-dir", type=Path, default=None, help="画框图输出目录")
    parser.add_argument(
        "--no-cuda-graph",
        dest="cuda_graph",
        action="store_false",
        help="关闭 CUDA Graph",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="额外输出原始 labels/boxes/scores 数组",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 下 stdout 被重定向时默认 GBK，输出中文/特殊字符可能报错
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    args = build_parser().parse_args(argv)
    inputs = args.inputs
    for path in inputs:
        if not path.is_file():
            sys.exit(f"输入不存在：{path}")

    engine = load_oxr_layout(
        checkpoint=args.checkpoint,
        metadata=args.metadata,
        device=args.device,
        cuda_graph=args.cuda_graph,
    )
    model = engine.info()
    print(
        f"模型已加载：version={model['model_version']} "
        f"device={model['device']} cuda_graph={model['cuda_graph']} "
        f"classes={model['num_classes']} size={model['image_size']}"
    )

    items = load_inputs(inputs, dpi=args.dpi, pages=args.pages)
    if not items:
        sys.exit("没有可处理的页面/图片")

    started = time.perf_counter()
    result = engine.predict(
        [png for _, png in items],
        confidence=args.confidence,
        class_confidences=args.class_confidences,
        include_raw=args.raw,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    for page, (name, _) in zip(result["pages"], items):
        page["source"] = name
    result["input"] = [str(path) for path in inputs]
    result["elapsed_ms"] = round(elapsed_ms, 3)

    if args.save_dir:
        import cv2

        args.save_dir.mkdir(parents=True, exist_ok=True)
        for page, (_, png) in zip(result["pages"], items):
            drawn = draw_regions(png, page["regions"])
            target = args.save_dir / f"{page['source']}.png"
            cv2.imwrite(str(target), drawn)
        print(f"检测框图已写入：{args.save_dir}")

    out_path = args.out or inputs[0].with_suffix(".layout.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"结果已写入：{out_path}")
    print(
        f"耗时 {elapsed_ms:.0f} ms"
        f"（推理 {result['timings_ms']['inference']:.0f} ms）"
    )
    for page in result["pages"]:
        counts: dict[str, int] = {}
        for region in page["regions"]:
            counts[region["label"]] = counts.get(region["label"], 0) + 1
        summary = "，".join(f"{label}×{count}" for label, count in counts.items()) or "无"
        print(
            f"  {page['source']}（{page['width']}x{page['height']}）："
            f"{len(page['regions'])} 个区域 —— {summary}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
