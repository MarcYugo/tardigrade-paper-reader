"""SLANet_plus 表格结构识别（本地权重：``models/SLANet_plus``）。

为什么用 ``paddleocr`` 包，而不是裸 ``paddle.inference``：
权重是 Paddle 3.x 的静态图（``inference.json`` + ``inference.pdiparams``），
缩放 / padding / 归一化和 TableLabelDecode 后处理全写在 ``inference.yml`` 里，
用 ``paddleocr.TableStructureRecognition(model_dir=...)`` 可以直接复用，
不必手抄 token 表和 bbox 还原逻辑。

安装（CPU 版；GPU 把 cpu 换成对应 cu118 / cu126 的源）::

    pip install "paddlepaddle>=3.0.0" -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
    pip install paddleocr

注意：Python 3.13 需要 paddlepaddle 3.3.x（3.0.0 没有 cp313 轮子）。

用法::

    from load_slanet import load_slanet

    engine = load_slanet()                      # 默认 models/SLANet_plus
    tables = engine.predict([pil_img1, pil_img2])
    # [{"html": "<html>...", "cells": [[x0, y0, ..., y3], ...], "score": 0.99}, ...]

    table = engine.predict_one(pil_img1)

命令行自测::

    python load_slanet.py table1.png table2.png
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence, Union

import numpy as np
from PIL import Image

# local_parse_pipeline/models/SLANet_plus（本文件在 utils/ 下，需要往上退一层）
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = PROJECT_DIR / "models" / "SLANet_plus"
DEFAULT_BATCH_SIZE = 1  # 动态 shape 只导出了 batch=1，调大可能报错
MODEL_FILES = ("inference.json", "inference.pdiparams", "inference.yml", "config.json")

_INSTALL_HINT = (
    "无法导入 paddleocr。请先安装 PaddlePaddle 3.x 与 PaddleOCR：\n"
    '    pip install "paddlepaddle>=3.0.0" -i https://www.paddlepaddle.org.cn/packages/stable/cpu/\n'
    "    pip install paddleocr\n"
    "GPU 环境请把 cpu 换成 cu118 / cu126 对应的包源；"
    "Python 3.13 需要 paddlepaddle 3.3.x。"
)

ImageLike = Union[Image.Image, str, Path]


def _ascii_model_dir(model_dir: Path) -> Path:
    """Paddle Inference 在 Windows 上打不开含中文的绝对路径（JSON 读到空流），
    遇到这种情况就把权重镜像到临时 ASCII 目录再加载。"""
    try:
        str(model_dir).encode("ascii")
        return model_dir
    except UnicodeEncodeError:
        pass

    digest = hashlib.md5(str(model_dir).encode("utf-8")).hexdigest()[:8]
    target = Path(tempfile.gettempdir()) / f"slanet_plus_{digest}"
    target.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        src = model_dir / name
        if src.is_file():
            shutil.copy2(src, target / name)
    return target


def _normalize_device(device: str | None) -> str | None:
    """``cuda`` / ``cuda:1`` → ``gpu`` / ``gpu:1``；其余原样交给 PaddleOCR。"""
    if device and device.lower().startswith("cuda"):
        return "gpu" + device[4:]
    return device


@contextlib.contextmanager
def _quiet_paddle_probe() -> Iterator[None]:
    """屏蔽 ``import paddle`` 时的 ccache 探测噪音。

    ``paddle/utils/cpp_extension/cpp_extension.py`` 在模块级执行
    ``CCACHE_HOME = find_ccache_home()``，其中用
    ``subprocess.check_output(['where', 'ccache'])`` 找 ccache：

    * 没有重定向 stderr，中文 Windows 下 ``where`` 会把
      “信息: 用提供的模式无法找到文件。” 直接漏到控制台；
    * 找不到再 ``warnings.warn("No ccache found. ...")``。

    ccache 只用于 JIT 编译自定义 C++/CUDA 算子，纯推理用不到，
    所以这里在导入窗口内临时把 fd 2 指向 devnull，并过滤这条告警。
    """
    stderr_fd: int | None
    try:
        stderr_fd = sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        stderr_fd = None

    saved_fd: int | None = None
    devnull_fd: int | None = None
    if stderr_fd is not None:
        try:
            sys.stderr.flush()
            saved_fd = os.dup(stderr_fd)
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull_fd, stderr_fd)
        except OSError:  # pragma: no cover - 极端环境
            if saved_fd is not None:
                os.close(saved_fd)
                saved_fd = None
            if devnull_fd is not None:
                os.close(devnull_fd)
                devnull_fd = None

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r".*ccache.*")
            yield
    finally:
        if saved_fd is not None and stderr_fd is not None:
            try:
                sys.stderr.flush()
            except Exception:
                pass
            os.dup2(saved_fd, stderr_fd)
            os.close(saved_fd)
        if devnull_fd is not None:
            os.close(devnull_fd)


class SLANetEngine:
    """一次加载、多次推理。构造后直接调 :meth:`predict`。

    Args:
        model_dir: 权重目录（含 inference.json / inference.pdiparams / inference.yml）。
        device: ``"cpu"`` / ``"gpu"`` / ``"gpu:0"``；None 交给 PaddleOCR 自动探测。
    """

    def __init__(
        self,
        model_dir: str | Path | None = None,
        *,
        device: str | None = None,
    ) -> None:
        resolved = Path(model_dir or DEFAULT_MODEL_DIR).expanduser().resolve()
        if not (resolved / "inference.json").is_file():
            raise FileNotFoundError(
                f"SLANet_plus 权重目录不完整（缺少 inference.json）：{resolved}"
            )

        # Windows 下 paddle 先于 torch 导入会让 torch 的 shm.dll 加载失败，
        # 先把 torch 拉起来，避免同进程后续加载 texify / OXR 版面模型时报错。
        try:
            import torch  # noqa: F401
        except ImportError:
            pass

        # paddle 是 lazy import 的：真的把 paddle 拉起来是在实例化
        # TableStructureRecognition 的时候，所以 ccache 探测也发生在这里。
        self.model_dir = resolved
        self.runtime_dir = _ascii_model_dir(resolved)
        with _quiet_paddle_probe():
            try:
                from paddleocr import TableStructureRecognition
            except ImportError as exc:  # pragma: no cover - 环境问题
                raise ImportError(_INSTALL_HINT) from exc

            self.model = TableStructureRecognition(
                model_name="SLANet_plus",
                model_dir=str(self.runtime_dir),
                device=_normalize_device(device),
            )

    # ---------------------------------------------------------------- 推理

    def predict(
        self,
        images: Sequence[ImageLike],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> List[Dict[str, Any]]:
        """批量识别表格结构，返回与入参同序的结果列表。

        Returns:
            每项为 ``{"html": 完整 HTML, "cells": 单元格八点坐标, "score": 结构置信度}``。
            坐标是输入图片的像素坐标。
        """
        pil_images = [self._to_rgb(im) for im in images]
        if not pil_images:
            return []

        # PaddleOCR 的 ndarray 输入按 BGR 解释（与 cv2.imread 一致）
        batch = [np.ascontiguousarray(np.asarray(im)[:, :, ::-1]) for im in pil_images]
        results = self.model.predict(input=batch, batch_size=max(1, int(batch_size)))

        tables: List[Dict[str, Any]] = []
        for res in results:
            data = res.json["res"]
            tables.append(
                {
                    "html": "".join(data.get("structure", [])),
                    "cells": data.get("bbox", []),
                    "score": float(data.get("structure_score") or 0.0),
                }
            )
        return tables

    def predict_one(self, image: ImageLike, **kwargs) -> Dict[str, Any]:
        """单张图片识别。"""
        return self.predict([image], **kwargs)[0]

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _to_rgb(image: ImageLike) -> Image.Image:
        if isinstance(image, (str, Path)):
            with Image.open(image) as im:
                im.load()
                return im.convert("RGB")
        if isinstance(image, Image.Image):
            return image.convert("RGB")
        raise TypeError(f"不支持的图片类型：{type(image)!r}")

    def info(self) -> dict:
        return {
            "model_dir": str(self.model_dir),
            "runtime_dir": str(self.runtime_dir),
            "model_name": "SLANet_plus",
        }


_ENGINE: SLANetEngine | None = None


def load_slanet(
    model_dir: str | Path | None = None,
    *,
    device: str | None = None,
    reload: bool = False,
) -> SLANetEngine:
    """加载（或复用进程内已加载的）SLANet_plus 推理引擎。

    Args:
        reload: 强制重新加载权重。
    """
    global _ENGINE
    if reload or _ENGINE is None:
        _ENGINE = SLANetEngine(model_dir, device=device)
    return _ENGINE


# -------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    try:  # Windows 控制台 GBK，避免 HTML 里的特殊字符触发 UnicodeEncodeError
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="用本地 SLANet_plus 权重识别表格结构")
    parser.add_argument("images", nargs="+", help="表格图片路径（可多个）")
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument("--device", default=None, help="cpu / gpu / gpu:0，留空自动")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    engine = load_slanet(args.model_dir, device=args.device)
    print(f"模型：{engine.info()}")

    results = engine.predict(args.images, batch_size=args.batch_size)
    for path, table in zip(args.images, results):
        print(f"{Path(path).name}\t{json.dumps(table, ensure_ascii=False)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
