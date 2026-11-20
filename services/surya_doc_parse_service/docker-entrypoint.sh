#!/usr/bin/env sh
# ============================================================
#  llama-server 启动入口 —— 部署 Surya 2 模型推理服务
#
#  模型文件通过运行时挂载提供(默认 /app/checkpoints, 只读)。
#  可通过环境变量覆盖: LLAMA_MODEL / LLAMA_MMPROJ / LLAMA_HOST /
#  LLAMA_PORT / LLAMA_PARALLEL / LLAMA_CTX / LLAMA_NGL
# ============================================================
set -e

MODEL="${LLAMA_MODEL:-/app/checkpoints/surya_ocr_2/surya-2.gguf}"
MMPROJ="${LLAMA_MMPROJ:-/app/checkpoints/surya_ocr_2/surya-2-mmproj.gguf}"
HOST="${LLAMA_HOST:-0.0.0.0}"
PORT="${LLAMA_PORT:-8060}"
PARALLEL="${LLAMA_PARALLEL:-4}"
CTX="${LLAMA_CTX:-49152}"
# 99 = 全部层卸载到 GPU(CUDA 版); CPU 版该参数会被忽略
NGL="${LLAMA_NGL:-99}"

if [ ! -f "$MODEL" ] || [ ! -f "$MMPROJ" ]; then
  echo "错误: 找不到模型文件。" >&2
  echo "请运行时挂载权重目录:" >&2
  echo "  -v \"./checkpoints:/app/checkpoints:ro\"" >&2
  echo "期望存在: $MODEL 与 $MMPROJ" >&2
  exit 1
fi

# ---- GPU 自检(排查"为什么没用 GPU"的第一现场) ----
# 判据: nvidia-smi 是 NVIDIA Container Toolkit 从宿主机注入容器的, 只有容器真的分到 GPU
#       才存在; 因此"nvidia-smi 不在/失败"== 容器没有 GPU, llama-server 必然退回 CPU。
# 注意: 下面打印的 ngl 只表示"请求卸载层数", CPU 版镜像会忽略它 —— 日志里出现
#       ngl=99 并不能证明用了 GPU, 真正要看的是 llama-server 自己的 ggml_cuda_init /
#       "offloading ... layers to GPU" 输出。
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  echo "[gpu] 容器已拿到 GPU: $(nvidia-smi -L | head -n 1)"
else
  echo "[gpu] 警告: 容器内看不到 GPU(nvidia-smi 缺失或执行失败) -> 将退回 CPU 推理!" >&2
  echo "[gpu] 请用 GPU 覆盖文件启动(镜像需为 surya:gpu):" >&2
  echo "[gpu]   docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d" >&2
fi

# ---- 让 NVIDIA 注入的"宿主驱动库"优先 ----
# 基础镜像 nvidia/cuda:12.4.1 里装了 cuda-compat-12-4(前向兼容驱动库, 在 /usr/local/cuda/compat),
# 它只对特定硬件有效; 一旦被误加载, 在 GeForce(如 RTX 2080 Ti) 上就会报
#   ggml_cuda_init: failed to initialize CUDA: forward compatibility was attempted on non supported HW
# 本镜像的 ENV LD_LIBRARY_PATH 覆盖了基础镜像原值(丢掉了 /usr/local/nvidia/*), 这里补回并置顶。
LD_LIBRARY_PATH="/usr/local/nvidia/lib:/usr/local/nvidia/lib64:${LD_LIBRARY_PATH:-}"
export LD_LIBRARY_PATH

echo "[llama-server] 加载模型: $MODEL"
echo "[llama-server] 视觉投影: $MMPROJ"
echo "[llama-server] 监听 ${HOST}:${PORT} | parallel=${PARALLEL} ctx=${CTX} ngl=${NGL}"
exec llama-server \
  -m "$MODEL" \
  --mmproj "$MMPROJ" \
  -ngl "$NGL" \
  --host "$HOST" \
  --port "$PORT" \
  --parallel "$PARALLEL" \
  --ctx-size "$CTX" \
  --alias datalab-to/surya-ocr-2 \
  --jinja \
  "$@"
