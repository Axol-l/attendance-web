#!/bin/bash
# ==========================================
# 考勤管理系统 - 服务器部署脚本
#
# 与生产数据系统共用一台服务器（your-server-ip），
# 通过共享 Nginx 的 /attendance/ 路径前缀接入。
#
# 用法：
#   ./deploy.sh            构建并重启
#   ./deploy.sh --no-build 仅重启（不重新构建镜像）
# ==========================================
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "=========================================="
echo "考勤管理系统 部署"
echo "目录: $APP_DIR"
echo "=========================================="

if [ ! -f .env ]; then
    echo "❌ 未找到 .env，请先从 .env.prod.template 复制并填写："
    echo "   cp .env.prod.template .env && vi .env"
    exit 1
fi

if [ "$1" != "--no-build" ]; then
    echo "🔨 [1/4] 构建镜像..."
    docker compose -f docker-compose.server.yml build
else
    echo "⏭️  [1/4] 跳过构建"
fi

echo "🚀 [2/4] 启动服务..."
docker compose -f docker-compose.server.yml up -d

echo "⏳ [3/4] 等待服务就绪..."
sleep 15

echo "🏥 [4/4] 健康检查..."
if curl -fsS http://127.0.0.1:8001/health/ ; then
    echo ""
    echo "✅ 部署完成"
    echo "   Nginx 接入路径: /attendance/"
    echo "   直连（仅本机）: http://127.0.0.1:8001/"
else
    echo ""
    echo "❌ 健康检查失败，请查看日志："
    echo "   docker compose -f docker-compose.server.yml logs --tail=100 web"
    exit 1
fi
