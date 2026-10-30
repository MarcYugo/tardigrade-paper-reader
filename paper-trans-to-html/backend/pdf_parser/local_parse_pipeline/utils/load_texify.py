"""texify 公式识别（ONNX Runtime + int8 量化权重：``models/texify-quantized-onnx``）。

为什么换成 ONNX
--------------
原来走官方 ``texify`` 包 + torch，得钉死 transformers 4.48.1 才能跑；换成
Xenova/optimum 导出的量化 ONNX 后，推理只依赖 ``onnxruntime`` + ``numpy``
+ ``Pillow``（tokenizer 用 ``tokenizers``），不再需要 torch / texify / transformers。
实测 5 张公式图与 fp32 torch 版**逐字一致**，耗时 16.9s vs 36.6s（约 2.1×）。

两个子图（batch 都固定为 1，多图在 Python 侧循环）::

    encoder_model_quantized.onnx
        pixel_values (1,3,420,420) -> last_hidden_state (1,196,1024)

    decoder_model_merged_quantized.onnx
        input_ids (1,1) + encoder_hidden_states (1,196,1024)
        + past_key_values.{i}.{decoder,encoder}.{key,value} + use_cache_branch
        -> logits (1,1,50000) + present.*

改之前必读的坑（都是实测踩出来的）
--------------------------------
1. **cross-attn 的 K/V 只能用首步那一份。**
   ``use_cache_branch=True`` 那一支输出的 ``present.*.encoder.*`` 是空壳
   （实测形状 ``(0,16,1,64)``）。若每步都拿它覆盖，第 4 个 token 起
   cross-attn 就丢了图像上下文，输出会退化成 ``\\Gamma_0^T`` 无限重复。
   正确做法：encoder K/V 首步算一次，之后反复喂同一份。

2. **必须复刻 texify 的预处理（白底 pad）。**
   目录自带的 ``preprocessor_config.json`` 是 transformers 版
   ``DonutImageProcessor``，pad 值是 0（黑底），与训练分布不一致
   （实测与 texify 预处理 ``max|diff| ≈ 4.5``）；texify 用 255（白底）居中填充。
   见 :func:`preprocess_image`。

3. **图像尺寸固定 420。** encoder 输出长度恒为 196（420/4 的窗口数），
   换尺寸必须重新导出，本模块会直接报错。

4. tokenizer 不要用 ``AutoTokenizer`` 读本目录：它的
   ``preprocessor_config.json`` 会让 transformers 建出 stock ``DonutProcessor``，
   连预处理都跟着变成黑底 pad。直接用 ``tokenizers`` 读 ``tokenizer.json``。

用法::

    from load_texify import load_texify

    engine = load_texify()                       # 默认 models/texify-quantized-onnx
    latex_list = engine.predict([pil_img1, pil_img2])
    latex = engine.predict_one(pil_img1)

命令行自测::

    python load_texify.py crop1.png crop2.png
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Sequence, Union

import numpy as np
from PIL import Image

# local_parse_pipeline/models/texify-quantized-onnx（本文件在 utils/ 下，需要往上退一层）
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = PROJECT_DIR / "models" / "texify-quantized-onnx"
ENCODER_FILE = "encoder_model_quantized.onnx"
DECODER_FILE = "decoder_model_merged_quantized.onnx"
DEFAULT_MAX_TOKENS = 384
DEFAULT_BATCH_SIZE = 8  # 仅为兼容旧接口保留，ONNX 版是逐张推理
DEFAULT_IMAGE_SIZE = 420  # 与 encoder 图绑定，不能改
IMAGE_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGE_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

ImageLike = Union[Image.Image, str, Path]


# ------------------------------------------------------------------ 预处理


def preprocess_image(image: Image.Image, size: int = DEFAULT_IMAGE_SIZE) -> np.ndarray:
    """复刻 texify 的 ``VariableDonutImageProcessor``，返回 ``(1,3,size,size)`` float32。

    与 transformers 版 ``DonutImageProcessor`` 的差别只有 pad 值（但很致命）：
    texify 用 255（白底）、居中填充；transformers 默认 0（黑底）。
    顺序也与 texify 一致：先按短边缩放进 ``size×size``，再 thumbnail，再 pad。
    """
    im = image.convert("RGB")
    width, height = im.size
    if width != size or height != size:
        scale = min(size / width, size / height)
        im = im.resize(
            (min(int(width * scale), size), min(int(height * scale), size)),
            Image.BILINEAR,
        )
    im.thumbnail((size, size), Image.BILINEAR)

    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))

    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    arr = (arr - IMAGE_MEAN) / IMAGE_STD
    return arr.transpose(2, 0, 1)[None, ...]


# ------------------------------------------------------------------ 后处理

try:  # ftfy 是 texify 的依赖；缺失时退化为原样返回，不影响主流程
    from ftfy import fix_text as _fix_text
except ImportError:  # pragma: no cover

    def _fix_text(text: str) -> str:
        return text


def postprocess(text: str) -> str:
    """等价于 ``texify.output.postprocess``：修乱码 + 去掉 \\label/\\ref/\\pageref。"""
    text = _fix_text(text)
    text = re.sub(r"\\label\{[^}]*\}", "", text)
    text = re.sub(r"\\ref\{[^}]*\}", "", text)
    text = re.sub(r"\\pageref\{[^}]*\}", "", text)
    return text


def replace_katex_invalid(text: str) -> str:
    """等价于 ``texify.output.replace_katex_invalid``：抹掉 KaTeX 不认的写法。"""
    text = re.sub(r"\\tag\{.*?\}", "", text)
    text = re.sub(r"\\(?:Bigg?|bigg?)\{(.*?)\}", r"\1", text)
    text = re.sub(r"\\quad\\mbox\{(.*?)\}", r"\1", text)
    text = re.sub(r"\\mbox\{(.*?)\}", r"\1", text)

    def _keep_inner(match: "re.Match[str]") -> str:
        # $$...$$ 内部不允许再出现单个 $，否则 KaTeX 解析失败
        return "$$" + match.group(1).replace("$", "") + "$$"

    return re.sub(r"\$\$(.*?)\$\$", _keep_inner, text, flags=re.DOTALL)


class TexifyEngine:
    """一次加载、多次推理。构造后直接调 :meth:`predict`。

    Args:
        model_dir: ONNX 权重目录（两个 .onnx + tokenizer.json + 若干 json）。
        device: ``"cpu"`` / ``"cuda"``；None 走 CPU。只有装了
            ``onnxruntime-gpu`` 且 CUDA EP 可用时才会真的上 GPU。
        max_image_size: 只接受 ``{"height": 420, "width": 420}``；
            传别的值会报错（encoder 图按固定尺寸导出）。
        threads: ONNX Runtime 的 intra-op 线程数，0 = 交给 ORT 默认。

    Note:
        量化图（QOperator int8）在 CUDA EP 上支持有限，跑不动会自动回退 CPU。
    """

    def __init__(
        self,
        model_dir: str | Path | None = None,
        *,
        device: str | None = None,
        max_image_size: dict | None = None,
        threads: int = 0,
    ) -> None:
        resolved = Path(model_dir or DEFAULT_MODEL_DIR).expanduser().resolve()
        for name in (ENCODER_FILE, DECODER_FILE):
            if not (resolved / name).is_file():
                raise FileNotFoundError(
                    f"ONNX 权重目录不完整（缺少 {name}）：{resolved}\n"
                    f"默认目录：{DEFAULT_MODEL_DIR}"
                )

        if max_image_size is not None:
            height = int(max_image_size.get("height") or 0)
            width = int(max_image_size.get("width") or 0)
            if (height, width) != (DEFAULT_IMAGE_SIZE, DEFAULT_IMAGE_SIZE):
                raise ValueError(
                    f"ONNX encoder 图固定 {DEFAULT_IMAGE_SIZE}x{DEFAULT_IMAGE_SIZE}，"
                    f"不支持 max_image_size={max_image_size}"
                )

        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise ImportError("缺少 onnxruntime：pip install onnxruntime") from exc

        try:
            import tokenizers
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise ImportError("缺少 tokenizers：pip install tokenizers") from exc

        self.model_dir = resolved
        self.image_size = DEFAULT_IMAGE_SIZE
        self.max_tokens = DEFAULT_MAX_TOKENS
        self._rng = np.random.default_rng()

        # 直接用 tokenizers 读 tokenizer.json：不导入 transformers，
        # 也就不会被目录里的 stock preprocessor_config.json 带偏（见文件头第 4 条）
        self.tokenizer = tokenizers.Tokenizer.from_file(str(resolved / "tokenizer.json"))

        gen_cfg: dict = {}
        gen_path = resolved / "generation_config.json"
        if gen_path.is_file():
            gen_cfg = json.loads(gen_path.read_text(encoding="utf-8"))
        self.bos_id = int(gen_cfg.get("decoder_start_token_id", gen_cfg.get("bos_token_id", 0)))
        self.eos_id = int(gen_cfg.get("eos_token_id", 2))
        self.pad_id = int(gen_cfg.get("pad_token_id", 1))

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            so.intra_op_num_threads = int(threads)

        self.providers = ["CPUExecutionProvider"]
        if (
            device
            and str(device).startswith("cuda")
            and "CUDAExecutionProvider" in ort.get_available_providers()
        ):
            self.providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        self.encoder = ort.InferenceSession(
            str(resolved / ENCODER_FILE), so, providers=self.providers
        )
        self.decoder = ort.InferenceSession(
            str(resolved / DECODER_FILE), so, providers=self.providers
        )
        self.device = self.encoder.get_providers()[0]

        # 层数/头数从图输入形状里读，不信 config.json
        # （那里 num_hidden_layers=12，实际 decoder 只有 8 层）
        self.num_layers = 0
        self.num_heads, self.head_dim = 16, 64
        for inp in self.decoder.get_inputs():
            matched = re.fullmatch(r"past_key_values\.(\d+)\.decoder\.key", inp.name)
            if not matched:
                continue
            self.num_layers = max(self.num_layers, int(matched.group(1)) + 1)
            shape = list(inp.shape)
            if len(shape) == 4 and all(isinstance(v, int) for v in shape[1:]):
                self.num_heads, self.head_dim = int(shape[1]), int(shape[3])
        if self.num_layers == 0:
            raise ValueError(
                f"{DECODER_FILE} 里找不到 past_key_values.*.decoder.key，"
                "不是 optimum 导出的 merged 图？"
            )

    # ---------------------------------------------------------------- 推理

    def _pick(self, logits_row: np.ndarray, temperature: float) -> int:
        """贪心或 top_p=0.95 采样（与 texify 的 do_sample 参数对齐）。"""
        if temperature <= 0:
            return int(np.argmax(logits_row))

        scores = logits_row.astype(np.float64) / float(temperature)
        scores -= scores.max()
        probs = np.exp(scores)
        probs /= probs.sum()
        order = np.argsort(-probs)
        cutoff = int(np.searchsorted(np.cumsum(probs[order]), 0.95)) + 1
        keep = order[:cutoff]
        weights = probs[keep]
        return int(self._rng.choice(keep, p=weights / weights.sum()))

    def _generate(
        self,
        pixel_values: np.ndarray,
        *,
        max_tokens: int,
        temperature: float,
    ) -> List[int]:
        """单张图的解码：self-attn 用增量 cache，cross K/V 只算一次。"""
        num_layers, heads, head_dim = self.num_layers, self.num_heads, self.head_dim
        hidden = self.encoder.run(["last_hidden_state"], {"pixel_values": pixel_values})[0]

        empty = np.zeros((1, heads, 0, head_dim), dtype=np.float32)
        feeds = {
            "input_ids": np.array([[self.bos_id]], dtype=np.int64),
            "encoder_hidden_states": hidden,
            "use_cache_branch": np.array([False]),
        }
        for i in range(num_layers):
            feeds[f"past_key_values.{i}.decoder.key"] = empty
            feeds[f"past_key_values.{i}.decoder.value"] = empty
            feeds[f"past_key_values.{i}.encoder.key"] = empty
            feeds[f"past_key_values.{i}.encoder.value"] = empty

        outs = self.decoder.run(None, feeds)
        logits = outs[0]
        self_k = [outs[1 + 4 * i] for i in range(num_layers)]
        self_v = [outs[2 + 4 * i] for i in range(num_layers)]
        # 见文件头第 1 条：cross K/V 只认首步这一份，后面不再更新
        cross_k = [outs[3 + 4 * i] for i in range(num_layers)]
        cross_v = [outs[4 + 4 * i] for i in range(num_layers)]

        tokens: List[int] = []
        for _ in range(max(0, int(max_tokens))):
            nxt = self._pick(logits[0, -1], temperature)
            if nxt == self.eos_id:
                break
            tokens.append(nxt)

            feeds["input_ids"] = np.array([[nxt]], dtype=np.int64)
            feeds["use_cache_branch"] = np.array([True])
            for i in range(num_layers):
                feeds[f"past_key_values.{i}.decoder.key"] = self_k[i]
                feeds[f"past_key_values.{i}.decoder.value"] = self_v[i]
                feeds[f"past_key_values.{i}.encoder.key"] = cross_k[i]
                feeds[f"past_key_values.{i}.encoder.value"] = cross_v[i]

            outs = self.decoder.run(None, feeds)
            logits = outs[0]
            self_k = [outs[1 + 4 * i] for i in range(num_layers)]
            self_v = [outs[2 + 4 * i] for i in range(num_layers)]

        return tokens

    def predict(
        self,
        images: Sequence[ImageLike],
        *,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        katex_compatible: bool = False,
    ) -> List[str]:
        """逐张识别公式图片，返回与入参同序的 LaTeX 字符串列表。

        Args:
            images: PIL 图片或图片路径。
            temperature: 0 = 贪心解码（推荐，结果稳定）；>0 走 top_p=0.95 采样。
            max_tokens: 单条公式最大 token 数，384 以上收益很小。
            batch_size: **仅为兼容旧接口保留**，ONNX 图 batch 固定为 1，逐张推理。
            katex_compatible: 用 :func:`replace_katex_invalid` 处理 KaTeX 不支持的写法。
        """
        del batch_size  # 图里 batch 维固定为 1

        pil_images = [self._to_rgb(im) for im in images]
        texts: List[str] = []
        for pil in pil_images:
            pixels = preprocess_image(pil, self.image_size)
            tokens = self._generate(
                pixels,
                max_tokens=int(max_tokens),
                temperature=float(temperature),
            )
            text = postprocess(self.tokenizer.decode(tokens, skip_special_tokens=True))
            if katex_compatible:
                text = replace_katex_invalid(text)
            texts.append(text)
        return texts

    def predict_one(self, image: ImageLike, **kwargs) -> str:
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
            "backend": "onnxruntime",
            "device": self.device,
            "dtype": "int8",
            "image_size": self.image_size,
            "num_layers": self.num_layers,
            "max_tokens": self.max_tokens,
        }


_ENGINE: TexifyEngine | None = None


def load_texify(
    model_dir: str | Path | None = None,
    *,
    device: str | None = None,
    max_image_size: dict | None = None,
    threads: int = 0,
    reload: bool = False,
) -> TexifyEngine:
    """加载（或复用进程内已加载的）texify ONNX 推理引擎。

    Args:
        reload: 强制重新加载权重。
    """
    global _ENGINE
    if reload or _ENGINE is None:
        _ENGINE = TexifyEngine(
            model_dir,
            device=device,
            max_image_size=max_image_size,
            threads=threads,
        )
    return _ENGINE


# -------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    try:  # Windows 控制台 GBK，避免 LaTeX 里的特殊字符触发 UnicodeEncodeError
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="用本地 texify ONNX 权重识别公式图片")
    parser.add_argument("images", nargs="+", help="公式图片路径（可多个）")
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument("--device", default=None, help="cpu / cuda，留空走 CPU")
    parser.add_argument("--threads", type=int, default=0, help="ORT intra-op 线程数，0=默认")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="仅为兼容旧参数，ONNX 版逐张推理",
    )
    parser.add_argument("--katex", action="store_true", help="替换 KaTeX 不支持的写法")
    args = parser.parse_args(argv)

    engine = load_texify(
        args.model_dir,
        device=args.device,
        threads=args.threads,
    )
    print(f"模型：{engine.info()}")

    results = engine.predict(
        args.images,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        batch_size=args.batch_size,
        katex_compatible=args.katex,
    )

    for path, latex in zip(args.images, results):
        print(f"{Path(path).name}\t{latex}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())