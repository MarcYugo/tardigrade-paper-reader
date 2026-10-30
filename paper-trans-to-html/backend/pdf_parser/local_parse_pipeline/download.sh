#!/usr/bin/env bash
# 本地解析方案的模型下载入口（公式 / 表格 / 版面分析）。
#
# 公式识别已改为 ONNX Runtime + int8 量化权重（./models/texify-quantized-onnx），
# 只依赖 onnxruntime + tokenizers，**不再**安装 `texify` 第三方包，
# 也不再需要 vikp/texify 的 torch 权重（model.safetensors，600MB+）。
#
# 国内网络慢可以走镜像：HF_ENDPOINT=https://hf-mirror.com ./download.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# ---- 表格结构：SLANet_plus（Paddle 静态图，inference.json + inference.pdiparams）----
modelscope download --model "PaddlePaddle/SLANet_plus" --local_dir "./models/SLANet_plus"

# ---- 版面分析：OXR D-FINE（last.pth + metadata.json）----
# SoMark/OXR-1.0 是 3.16GB 的完整多模态模型，这里只需要里面的 layout_analysis 子目录：
#   --include 只拉取 layout_analysis 下的文件（不会下载 model.safetensors 等大文件），
#   文件会按仓库结构落到 ./models/layout_analysis/，下载后再改名为 oxr_layout_analysis。
OXR_DEST="./models/oxr_layout_analysis"
if [ ! -f "$OXR_DEST/last.pth" ] || [ ! -f "$OXR_DEST/metadata.json" ]; then
    modelscope download --model "SoMark/OXR-1.0" \
        --include 'layout_analysis/*' \
        --local_dir "./models"
    # 仓库目录名是 layout_analysis，本项目的约定目录名是 oxr_layout_analysis
    rm -rf "$OXR_DEST"
    mv "./models/layout_analysis" "$OXR_DEST"
fi

# 兜底校验：下载/改名失败时立刻报错，而不是留给运行时加载模型时才炸
if [ ! -f "$OXR_DEST/last.pth" ] || [ ! -f "$OXR_DEST/metadata.json" ]; then
    echo "!! 版面分析权重缺失：$OXR_DEST 下没有 last.pth / metadata.json" >&2
    exit 1
fi

# ---- 公式识别：texify-quantized-onnx（两个量化图 + tokenizer.json）----
DEST="./models/texify-quantized-onnx"
if [ -f "$DEST/encoder_model_quantized.onnx" ] \
   && [ -f "$DEST/decoder_model_merged_quantized.onnx" ] \
   && [ -f "$DEST/tokenizer.json" ]; then
    echo "OK: $DEST 已存在，跳过下载"
else
    BASE="${HF_ENDPOINT:-https://huggingface.co}/Spedon/texify-quantized-onnx/resolve/main"
    mkdir -p "$DEST"
    for f in config.json generation_config.json preprocessor_config.json \
             special_tokens_map.json tokenizer_config.json tokenizer.json \
             encoder_model_quantized.onnx decoder_model_merged_quantized.onnx; do
        echo "↓ $f"
        # -C - 断点续传：ONNX 图较大，中断后可接着下
        curl -fL --retry 3 -C - -o "$DEST/$f" "$BASE/$f"
    done
fi

echo "OK: 模型目录 $(pwd)/models"
