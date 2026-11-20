# syntax=docker/dockerfile:1
# ============================================================
#  draw-sandbox —— 画图沙盒（Python 3.11 轻量级基础镜像）
#
#  构建：docker build -t draw-sandbox:1.0.0 .
#  运行：docker run --rm -p 127.0.0.1:8090:8090 --memory=2g --cpus=2 \
#            --read-only --tmpfs /tmp:rw,nosuid,noexec,size=256m \
#            --cap-drop=ALL --security-opt no-new-privileges draw-sandbox:1.0.0
# ============================================================
FROM python:3.11-slim

LABEL org.opencontainers.image.title="draw-sandbox" \
      org.opencontainers.image.description="画图沙盒：接收 Python 代码，执行并回传产出文件" \
      org.opencontainers.image.version="1.0.0"

# 国内网络可加 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

# PYTHONDONTWRITEBYTECODE：运行时根文件系统只读，不写 .pyc
# MPLBACKEND=Agg：容器里没有 Tk/Qt 显示
# MPLCONFIGDIR：字体缓存落到镜像里（运行时只读，缓存必须提前建好）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/opt/mplcache \
    SANDBOX_BASE_DIR=/tmp/sandbox

# 中文字体：模型生成的图常带中文标题/坐标轴标签，缺字体会显示成方框。
# wqy-microhei 只有约 2MB，比 noto-cjk 轻很多。
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-wqy-microhei \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用分层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷业务代码
COPY app ./app

# 预热 matplotlib 字体缓存：不预热的话每个新容器第一次画图都要扫一遍系统字体
RUN mkdir -p "$MPLCONFIGDIR" \
 && python -c "import matplotlib; matplotlib.use('Agg'); from matplotlib import pyplot as plt; plt.plot([0,1],[0,1]); plt.title('warmup'); plt.savefig('/tmp/warmup.png'); plt.close('all')" \
 && rm -f /tmp/warmup.png

# 非 root 运行（uid 10001）
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sandbox \
 && mkdir -p /tmp/sandbox \
 && chown -R sandbox:sandbox /opt/mplcache /tmp/sandbox
USER sandbox

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3).status == 200 else 1)"

# workers 必须是 1：并发由进程内的 Semaphore 控制，多 worker 会让实际并发翻倍
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8090", \
     "--workers", "1", \
     "--no-server-header", \
     "--no-access-log"]
