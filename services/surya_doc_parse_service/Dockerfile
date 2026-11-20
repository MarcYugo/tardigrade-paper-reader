# syntax=docker/dockerfile:1

# ============================================================
#  Surya 2 (surya-ocr) 部署镜像
#
#  基础:   Ubuntu 22.04 + Python3 + llama.cpp (llama-server)
#         (GPU 版以 nvidia/cuda:*-devel-ubuntu22.04 为基础镜像, 自带 nvcc)
#  角色:   镜像 = 推理服务端, 主进程 llama-server 常驻加载 surya-2 模型
#  权重:  不进镜像, 运行时把宿主机 checkpoints/ 挂载到容器 /app/checkpoints
#         (surya 客户端默认从该路径找权重)
#
#  构建:
#    CPU:  docker build -t surya:latest .
#    GPU:  docker build \
#            --build-arg BASE_IMAGE=nvidia/cuda:12.2.2-devel-ubuntu22.04 \
#            --build-arg ENABLE_CUDA=1 -t surya:gpu .
#    国内可加:  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#
#  ⚠ 重要 1: ENABLE_CUDA=1 必须配 BASE_IMAGE=nvidia/cuda:<ver>-devel-ubuntu22.04。
#    只用 ubuntu:22.04 是没装 nvcc 的, 会在 CMake 阶段报
#    "CUDA Toolkit not found"。构建前会做一次快速检查直接报错提示。
#
#  ⚠ 重要 2: 这里 <ver> 必须 <= 宿主机驱动支持的 CUDA 版本(`nvidia-smi` 右上角 "CUDA Version")。
#    CUDA 版本高于驱动时, 运行期 llama.cpp 会报
#      ggml_cuda_init: failed to initialize CUDA: forward compatibility was attempted on non supported HW
#    然后 "no usable GPU found" 退回 CPU —— GeForce 不支持前向兼容, 靠 CUDA 镜像兼容旧驱动行不通。
#    对照: 驱动 525.x->12.0 | 535.x->12.2 | >=550.54.14->12.4。
#
#  运行 (启动 llama-server 推理服务, 主进程常驻):
#    CPU:
#      docker run -d --name surya-server \
#        -p 8060:8060 \
#        -v "<checkpoints目录>:/app/checkpoints:ro" \
#        surya:latest
#    GPU:
#      docker run -d --name surya-server --gpus all \
#        -p 8060:8060 \
#        -v "<checkpoints目录>:/app/checkpoints:ro" \
#        surya:gpu
#  验证:  curl http://127.0.0.1:8060/health
#  客户端(Surya OCR, 需已装 surya-ocr 0.22.x, 例如宿主机):
#      python test_surya_ocr.py --url http://127.0.0.1:8060/v1 --input 你的.pdf
#
#  提示:
#    - GPU 运行需要宿主机装好 NVIDIA Container Toolkit (Windows 上配合 WSL2)。
#    - CPU 版能跑但很慢(Surya2 在 CPU 上约 0.1 页/秒), 生产建议 GPU。
#    - 客户端需单独装 surya-ocr 0.22.x, 用 --url 连本服务(见上)。
#    - 想改端口/并发/上下文大小, 用环境变量 LLAMA_PORT/LLAMA_PARALLEL/LLAMA_CTX。
# ============================================================

# 基础镜像: CPU = ubuntu; GPU = NVIDIA CUDA devel(自带 nvcc, 不依赖 conda)
ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

# ---- 构建参数 ----
# 重新声明 BASE_IMAGE, 让下面的 RUN 里也能读到(ARG 在 FROM 之前声明时, 阶段内需重新声明)
ARG BASE_IMAGE=ubuntu:22.04
# ENABLE_CUDA: 1 = 编译 CUDA 版 llama-server(GPU 加速); 0 = CPU 版
# ARG ENABLE_CUDA=0
ARG ENABLE_CUDA=1
# CUDA_ARCHITECTURES: 目标 GPU 架构(仅 CUDA 构建使用), 分号分隔
#   不传 = CMake 为编译器支持的所有架构编译, 构建时间会非常长
#   ⚠ 必须覆盖实际显卡的计算能力(compute_cap), 漏掉会报
#       CUDA error: no kernel image is available for execution on the device
#   常见值: 70(V100) 75(Turing) 80(A100) 86(RTX30) 89(RTX40) 90(H100)
ARG CUDA_ARCHITECTURES=70;75;80;86;89
# surya-ocr 版本(Surya 2)
ARG SURYA_VERSION=0.22.1
# PyTorch CPU wheels (the later Surya install uses the regular PyPI index)
# ARG TORCH_PIP_INDEX_URL=https://download.pytorch.org/whl/cpu
# ARG TORCH_PIP_INDEX_URL=https://download.pytorch.org/whl/cu124
# pip 源(国内可换镜像)
ARG PIP_INDEX_URL=https://pypi.org/simple

# ---- 环境 ----
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/root \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    LD_LIBRARY_PATH=/usr/local/bin:/usr/local/cuda/lib64

# ---- 系统工具 + Python3 ----
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git cmake build-essential ca-certificates curl \
      python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

# ---- 快速失败检查: 想编 CUDA 版就必须有 nvcc ----
# 否则会在很久之后的 CMake 配置阶段才报 "CUDA Toolkit not found", 白等半天。
RUN set -eux; \
    if [ "${ENABLE_CUDA}" = "1" ]; then \
      if ! command -v nvcc >/dev/null 2>&1 && [ ! -x /usr/local/cuda/bin/nvcc ]; then \
        echo "" >&2; \
        echo "=====================================================================" >&2; \
        echo "ERROR: ENABLE_CUDA=1, 但基础镜像 (${BASE_IMAGE}) 里没有 CUDA Toolkit(nvcc)。" >&2; \
        echo "GPU 构建必须使用 devel 版镜像, 例如(版本需 <= 驱动支持的 CUDA):" >&2; \
        echo "  BASE_IMAGE=nvidia/cuda:12.2.2-devel-ubuntu22.04" >&2; \
        echo "compose 构建请直接: docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build" >&2; \
        echo "=====================================================================" >&2; \
        echo "" >&2; \
        exit 1; \
      fi; \
      nvcc --version | tail -n 1; \
    fi

# ---- torch(CPU 版) + surya-ocr (Surya 2) ----
# 关键点:
#   1) 必须用 /opt/venv/bin/python -m pip, 不能用裸 pip(那是系统 python, 会装错环境)
#   2) torch 用 --index-url 独占 CPU 索引; 用 --extra-index-url 会让 PyPI 上更新的
#      CUDA 版 torch 胜出, 从而"被替换成 GPU 版"
#   3) 把已装好的 torch 写入约束文件, 防止后续依赖解析把它升级成 CUDA 版
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/python -m pip install --no-cache-dir -U pip setuptools wheel \
 && /opt/venv/bin/python -m pip install --no-cache-dir \
      # --index-url ${TORCH_PIP_INDEX_URL} \
      torch==2.7 torchvision \
 && (/opt/venv/bin/python -m pip freeze | grep -iE '^(torch|torchvision|torchaudio)' \
       > /tmp/torch-constraints.txt || true) \
 && /opt/venv/bin/python -m pip install --no-cache-dir --retries 5 --timeout 60 \
      -c /tmp/torch-constraints.txt \
      surya-ocr==${SURYA_VERSION}
# 注意: 不能写 $PATH(Dockerfile 里取不到 base 镜像的 PATH, 会变空导致 llama-server/nvcc 找不到)
ENV PATH=/opt/venv/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ---- 从源码编译 llama-server (CPU 或 CUDA) ----
# 注意: 新版 llama.cpp 把 llama-server 拆成了可执行文件 + libllama-server-impl.so,
#       只 install 可执行文件会在运行时报
#       "libllama-server-impl.so: cannot open shared object file".
#       因此: 1) 尽量静态构建  2) 把 build/bin 下的二进制与 .so 一并拷到 /usr/local/bin
#             3) 嵌入 $ORIGIN rpath, 让可执行文件在自身目录找依赖库
RUN git clone --depth 1 https://github.com/ggml-org/llama.cpp.git /tmp/llama.cpp
WORKDIR /tmp/llama.cpp
# 只有 CUDA 构建才传 CMAKE_CUDA_ARCHITECTURES, 避免 CPU 构建被无谓污染
#
# ⚠ 变量展开陷阱: RUN 是 shell form, Docker 不对它做变量替换, 命令原样交给 /bin/sh。
#    所以这里必须写 shell 变量 $arch_arg, 不能写 $$arch_arg ——
#    $$ 在 shell 里是"当前进程号(PID)", 构建容器里为 1, 于是 $${arch_arg} 会变成
#    "1{arch_arg}", cmake 便把它当成源码目录并报 source directory not exist。
#    同理 $ORIGIN 用单引号 '$ORIGIN' 保护即可, 不要写成 '$$ORIGIN'。
RUN set -eux; \
    arch_arg=""; \
    if [ "${ENABLE_CUDA}" = "1" ]; then \
      arch_arg="-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHITECTURES}"; \
    fi; \
    cmake -B build -DCMAKE_BUILD_TYPE=Release \
      -DGGML_CUDA=${ENABLE_CUDA} \
      $arch_arg \
      -DBUILD_SHARED_LIBS=OFF \
      -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
      -DCMAKE_INSTALL_RPATH='$ORIGIN' \
 && cmake --build build --target llama-server -j"$(nproc)" \
 && cp -a build/bin/. /usr/local/bin/ \
 && cd / && rm -rf /tmp/llama.cpp

# ============================================================
#  推理服务入口: 容器主进程 = llama-server(部署模型, 常驻)
#  权重通过 -v 挂载到 /app/checkpoints(只读), 不进镜像
# ============================================================
WORKDIR /app

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 服务运行参数(可被 docker run -e / compose environment 覆盖)
ENV SURYA_GGUF_LOCAL_MODEL_PATH=/app/checkpoints/surya_ocr_2/surya-2.gguf \
    SURYA_GGUF_LOCAL_MMPROJ_PATH=/app/checkpoints/surya_ocr_2/surya-2-mmproj.gguf \
    SURYA_INFERENCE_BACKEND=llamacpp \
    LLAMA_HOST=0.0.0.0 \
    LLAMA_PORT=8060 \
    LLAMA_PARALLEL=4 \
    LLAMA_CTX=49152

EXPOSE 8060
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${LLAMA_PORT}/health" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD []
