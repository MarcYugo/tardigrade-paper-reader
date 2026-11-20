# 安装 NVIDIA Container Toolkit
# 注意: Ubuntu 官方源里没有 nvidia-container-toolkit, 必须先添加 NVIDIA 自己的源,
#       否则 apt 报 "E: Unable to locate package nvidia-container-toolkit"。
#       默认用中科大镜像(国内快), 想改用官方源: NVIDIA_REPO=https://nvidia.github.io/libnvidia-container ./docker_install.sh
NVIDIA_REPO="${NVIDIA_REPO:-https://mirrors.ustc.edu.cn/libnvidia-container}"

curl -fsSL "${NVIDIA_REPO}/gpgkey" | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

echo "deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] \
${NVIDIA_REPO}/stable/deb/$(dpkg --print-architecture) /" | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# 注册 nvidia runtime 到 /etc/docker/daemon.json(让 docker 认识 --gpus)
sudo nvidia-ctk runtime configure --runtime=docker
# 重启 docker 使 runtime 生效; WSL 里可能没有 systemd, 用 service 兜底
sudo systemctl restart docker 2>/dev/null || sudo service docker restart

# 验证: Runtimes 里必须能看到 nvidia(只有 runc 就是没配好)
docker info | grep -i runtimes
# 端到端验证(需宿主机 nvidia-smi 正常):
#   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi