#!/bin/bash
# 掌柜智库：Ubuntu 22.04 虚拟机内一键部署脚本
# 用法：cd /mnt/hgfs/knowledge_base && bash deploy/setup_ubuntu.sh

set -e

echo "==== 1/5 更新系统 ===="
sudo apt update

echo "==== 2/5 安装 Docker 与 Compose ===="
if ! command -v docker >/dev/null 2>&1; then
    sudo apt install -y docker.io docker-compose-v2
fi
sudo usermod -aG docker $USER || true
echo "提示：docker 组要重新登录才生效，本脚本统一用 sudo 代替。"

echo "==== 3/5 配置国内镜像加速（Docker Hub 直连被拒）===="
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://dockerproxy.net"
  ]
}
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker

echo "==== 4/5 启动掌柜智库数据库（Milvus/MinIO/MongoDB/Attu）===="
cd "$(dirname "$0")/.."
sudo docker compose -f deploy/docker-compose.yml up -d

echo "==== 5/5 等待并检查状态 ===="
sleep 20
sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

VM_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "================================================"
echo " 虚拟机 IP：$VM_IP"
echo " 把 Windows 的 knowledge_base/.env 改成："
echo "   MILVUS_URL=http://$VM_IP:19530"
echo "   MINIO_ENDPOINT=$VM_IP:9000"
echo "   MONGO_URL=mongodb://$VM_IP:27017"
echo " Attu 管理页：http://$VM_IP:7000"
echo "================================================"