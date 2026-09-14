# ==========================================
# 考勤模块 Web 化 - Docker 镜像
# Python 3.10 + Django 4.1 + MySQL 8.0
# ==========================================

FROM python:3.10-slim

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 配置阿里云 Debian 镜像源（加速 apt-get）
# 兼容两种基础镜像格式：
#   - Debian <= 12 (bookworm)  使用 /etc/apt/sources.list
#   - Debian >= 13 (trixie)    使用 /etc/apt/sources.list.d/debian.sources (deb822)
RUN if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi && \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if [ -f /etc/apt/sources.list.d/debian.sources.docker ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources.docker; \
    fi

# 安装 MySQL 客户端工具（仅备份功能需要 mysqldump / mysql）
RUN apt-get update && \
    apt-get install -y --no-install-recommends default-mysql-client && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装（使用清华大学 PyPI 镜像加速）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 复制项目文件
COPY . .

# 创建必要的目录
RUN mkdir -p media/uploads media/reports staticfiles logs backup_files

# 设置权限
RUN chmod +x docker-entrypoint.sh

EXPOSE 8000

CMD ["sh", "docker-entrypoint.sh"]
