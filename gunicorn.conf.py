"""
Gunicorn 配置文件 - 考勤管理系统
适用于生产环境和本地生产模式测试

配置说明：
- 多进程 + 多线程模式
- 自动重启机制（max_requests 防内存泄漏）
- 优雅重启，避免请求中断

⚠️ timeout 与其他超时的关系（必须保持这个大小顺序）：
     backups/backup.py 的 SUBPROCESS_TIMEOUT (90s)
       < gunicorn timeout (120s)
    否则备份还没跑完，worker 就被 gunicorn 杀掉，留下半截文件且无记录。
"""

import multiprocessing
import os

# ==================== 服务器配置 ====================

bind = "0.0.0.0:8000"
daemon = False

# 推荐公式：(2 * CPU核心数) + 1
workers = int(os.getenv('GUNICORN_WORKERS', multiprocessing.cpu_count() * 2 + 1))
worker_class = os.getenv('GUNICORN_WORKER_CLASS', 'sync')
threads = int(os.getenv('GUNICORN_THREADS', 2))
max_requests = int(os.getenv('GUNICORN_MAX_REQUESTS', 1000))
max_requests_jitter = int(os.getenv('GUNICORN_MAX_REQUESTS_JITTER', 50))

# ==================== 超时配置 ====================

timeout = int(os.getenv('GUNICORN_TIMEOUT', 120))
keepalive = int(os.getenv('GUNICORN_KEEPALIVE', 5))
graceful_timeout = int(os.getenv('GUNICORN_GRACEFUL_TIMEOUT', 30))

# ==================== 日志配置 ====================

accesslog = os.getenv('GUNICORN_ACCESS_LOG', '/app/logs/gunicorn-access.log')
errorlog = os.getenv('GUNICORN_ERROR_LOG', '/app/logs/gunicorn-error.log')
loglevel = os.getenv('GUNICORN_LOG_LEVEL', 'info')
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# ==================== 进程命名 ====================

proc_name = 'attendance_system'

# ==================== 安全配置 ====================

limit_request_line = 4096
limit_request_fields = 100
limit_request_field_size = 8190


# ==================== 服务器钩子 ====================

def on_starting(server):
    server.log.info("=" * 50)
    server.log.info("考勤管理系统 - Gunicorn 启动中...")
    server.log.info(f"工作进程数: {workers}")
    server.log.info(f"每进程线程数: {threads}")
    server.log.info(f"监听地址: {bind}")
    server.log.info("=" * 50)


def on_reload(server):
    server.log.info("配置已重载")


def when_ready(server):
    server.log.info("✅ Gunicorn 服务器已就绪，开始接受请求")


def worker_int(worker):
    worker.log.info(f"工作进程 {worker.pid} 收到中断信号")


def worker_abort(worker):
    worker.log.error(f"工作进程 {worker.pid} 异常终止")


def post_fork(server, worker):
    worker.log.info(f"工作进程 {worker.pid} 已启动")


def on_exit(server):
    server.log.info("=" * 50)
    server.log.info("Gunicorn 服务器已关闭")
    server.log.info("=" * 50)
