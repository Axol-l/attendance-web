#!/bin/sh
# ==========================================
# 考勤管理系统 - Docker 容器启动脚本
# 适用于生产环境和本地生产模式测试
# ==========================================

set -e

echo "=========================================="
echo "🚀 考勤管理系统启动中..."
echo "=========================================="

# ==================== 环境信息 ====================
echo "📋 环境信息："
echo "  - Python版本: $(python --version)"
echo "  - Django版本: $(python -c 'import django; print(django.get_version())')"
echo "  - 时区: ${TZ:-Asia/Shanghai}"
echo "  - DEBUG模式: ${DEBUG:-False}"
echo ""

# ==================== 等待数据库启动 ====================
echo "⏳ [1/6] 等待数据库服务启动..."
sleep 10

# ==================== 检查数据库连接 ====================
echo "🔍 [2/6] 检查数据库连接..."

python << END
import os
import sys
import time

import pymysql

db_config = {
    'host': os.getenv('DB_HOST', 'db'),
    'port': int(os.getenv('DB_PORT', '3306')),
    'user': os.getenv('DB_USER', 'att_admin'),
    'password': os.getenv('DB_PASSWORD', ''),
    'database': os.getenv('DB_NAME', 'attendance_db'),
}

print(f"📡 连接信息: {db_config['user']}@{db_config['host']}:{db_config['port']}/{db_config['database']}")

max_retries = 30
retry_count = 0

while retry_count < max_retries:
    try:
        connection = pymysql.connect(**db_config)
        connection.close()
        print("✅ 数据库连接成功!")
        sys.exit(0)
    except Exception as e:
        retry_count += 1
        print(f"⏳ 等待数据库... ({retry_count}/{max_retries})")
        if retry_count >= max_retries:
            print(f"❌ 数据库连接失败: {str(e)}")
            sys.exit(1)
        time.sleep(2)
END

echo ""

# ==================== 执行数据库迁移 ====================
echo "📦 [3/6] 执行数据库迁移..."
echo "  - 应用迁移..."
python manage.py migrate --noinput

echo "  - 检查迁移状态..."
# ⚠️ 不要写成 `showmigrations | head -20`：head 读够 20 行就关闭管道，
#    Django 继续往已关闭的 stdout 写会抛 BrokenPipeError（实测在启动日志里
#    出现过一长串吓人的 traceback）。用 sed 只取前 20 行、不提前关闭管道。
python manage.py showmigrations 2>/dev/null | sed -n '1,20p' || true

echo "✅ 数据库迁移完成"
echo ""

# ==================== 创建超级用户 ====================
echo "👤 [4/6] 创建超级用户..."

if [ -f "create_superuser.py" ]; then
    python create_superuser.py
    echo "✅ 超级用户检查完成"
else
    echo "⚠️  未找到 create_superuser.py，跳过"
fi

echo ""

# ==================== 收集静态文件 ====================
echo "📁 [5/6] 收集静态文件..."

echo "  - 清理旧文件..."
python manage.py collectstatic --noinput --clear > /dev/null 2>&1

echo "  - 收集新文件..."
python manage.py collectstatic --noinput

STATIC_COUNT=$(find /app/staticfiles -type f 2>/dev/null | wc -l)
echo "  - 静态文件数量: ${STATIC_COUNT}"

echo "✅ 静态文件收集完成"
echo ""

# ==================== 系统信息 ====================
echo "📊 [6/6] 系统信息："

TABLE_COUNT=$(python -c "
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute('SHOW TABLES')
    print(len(cursor.fetchall()))
" 2>/dev/null || echo "0")
echo "  - 数据库表数量: ${TABLE_COUNT}"

if [ -d "/app/media" ]; then
    MEDIA_SIZE=$(du -sh /app/media 2>/dev/null | cut -f1)
    echo "  - 媒体文件大小: ${MEDIA_SIZE}"
fi

if [ ! -d "/app/logs" ]; then
    mkdir -p /app/logs
    echo "  - 创建日志目录: /app/logs"
fi

echo ""

# ==================== 启动完成 ====================
echo "=========================================="
echo "✅ 系统启动完成！"
echo "=========================================="
echo "📍 访问地址: http://localhost:8001"
echo "📝 管理后台: http://localhost:8001/admin"
echo "🏥 健康检查: http://localhost:8001/health/"
echo "=========================================="
echo ""

# ==================== 启动 Gunicorn ====================
echo "🚀 启动 Gunicorn 服务器..."
echo "  - 配置文件: /app/gunicorn.conf.py"
echo "  - 监听地址: 0.0.0.0:8000"
echo "  - 工作进程: ${GUNICORN_WORKERS:-auto}"
echo "  - 日志目录: /app/logs"
echo ""

# 使用 exec 替换当前进程，确保信号正确传递
exec gunicorn config.wsgi:application \
    --config /app/gunicorn.conf.py \
    --bind 0.0.0.0:8000 \
    --log-level info \
    --access-logfile - \
    --error-logfile -
