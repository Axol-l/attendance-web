# config — 项目配置与基础设施

## 文件清单

| 文件 | 功能 |
|------|------|
| `config/__init__.py` | 包初始化：`pymysql.install_as_MySQLdb()` |
| `config/settings.py` | 主配置（开发环境基准，含模板 loaders 补偿） |
| `config/settings_production.py` | 生产覆盖（`from .settings import *`） |
| `config/settings_test.py` | 本地测试配置（内存 SQLite） |
| `config/urls.py` | 主路由 |
| `config/wsgi.py` | WSGI 入口：`DJANGO_SETTINGS_MODULE` 默认 `config.settings`，导出 `application = get_wsgi_application()` |
| `config/asgi.py` | ASGI 入口：`DJANGO_SETTINGS_MODULE` 默认 `config.settings`，导出 `application = get_asgi_application()` |

## settings.py 关键配置

### DEBUG（怎么从环境变量读）

```python
load_dotenv(BASE_DIR / '.env')                                    # 先加载 .env
DEBUG = os.getenv('DEBUG', 'True').lower() in ('true', '1', 'yes')  # 开发默认 True
```

`settings_production.py` 里同一行改为 `os.getenv('DEBUG', 'False')`，即生产默认 False。
项目根 `.env` 由 `python-dotenv` 在导入 settings 时读取。

### TEMPLATES 的 loaders（Django 4.1 的模板缓存坑）

```python
TEMPLATE_LOADERS_CACHED = not DEBUG

_TEMPLATE_LOADER_CHAIN = [
    'django.template.loaders.filesystem.Loader',
    'django.template.loaders.app_directories.Loader',
]

TEMPLATES = [{
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': False,
    'OPTIONS': {
        'loaders': ([('django.template.loaders.cached.Loader', _TEMPLATE_LOADER_CHAIN)]
                    if TEMPLATE_LOADERS_CACHED else _TEMPLATE_LOADER_CHAIN),
        ...
    },
}]
```

必须这么配的原因（代码注释原文要点）：

- Django 4.1 起，只要 `TEMPLATES` 里没有显式配置 `loaders`，引擎就会**无条件**套上 `cached.Loader`（见 `django/template/engine.py` 的 `Engine.__init__`）。
- 这与 `DEBUG` 无关 —— `DEBUG` 只影响报错信息，不影响模板缓存。结果是本地改了模板必须重启进程才生效。
- 因此这里显式声明 `loaders`：`DEBUG=True` 用不带缓存的链（本地热重载）；`DEBUG=False` 才套 `cached.Loader`（生产模板随镜像走，缓存无副作用）。
- 显式给出 `loaders` 时 Django 强制要求 `APP_DIRS` 必须为 False（否则抛 `ImproperlyConfigured: app_dirs must not be set when loaders is defined`），所以 app 级模板目录改为在链里显式写 `app_directories.Loader`。
- `settings_production.py` 里 `template_engine.pop('APP_DIRS', None)` 带了默认值，不受此约束影响。

`context_processors` 除 Django 内置四项外，另有 `accounts.context_processors.user_permissions`，向所有模板注入 `user_perms`。

### 数据库（环境变量、字符集、PyMySQL 注册位置）

| 项 | 取值来源 | 默认值 |
|------|---------|--------|
| `ENGINE` | 硬编码 | `django.db.backends.mysql` |
| `NAME` | `DB_NAME` | `attendance_db` |
| `USER` | `DB_USER` | `att_admin` |
| `PASSWORD` | `DB_PASSWORD` | 空串 |
| `HOST` | `DB_HOST` | `localhost` |
| `PORT` | `DB_PORT` | `3309` |
| `OPTIONS['charset']` | 硬编码 | `utf8mb4` |
| `OPTIONS['init_command']` | 硬编码 | `SET sql_mode='STRICT_TRANS_TABLES'` |

端口 3309 是刻意选择：与同机另一套生产数据系统（3308）错开，便于共存（`.env.example` 注释同此）。

**PyMySQL 注册位置**：`config/__init__.py`（包初始化）调用 `pymysql.install_as_MySQLdb()`。注释说明必须放在这里而不是 settings，因为必须在任何 Django 数据库访问之前执行，而 settings 本身就是被本包导入的。

### 业务配置常量

| 常量名 | 默认值 | 说明 |
|--------|--------|------|
| `ALLOWED_EXTENSIONS` | `os.getenv('ALLOWED_EXTENSIONS', 'xlsx').split(',')` | 上传扩展名白名单，`FileStorage.validate_upload` 使用 |
| `IMPORT_BATCH_SIZE` | `500` | `bulk_create` 批大小（`attendance/services.py` 两处使用）；生产可由环境变量覆盖 |
| `IMPORT_MAX_FILE_SIZE` | `os.getenv('MAX_FILE_SIZE', 50MB)` | 单文件上限，上传校验使用。**读环境变量** |
| `IMPORT_MAX_ROWS` | `os.getenv('IMPORT_MAX_ROWS', 100000)` | 单次导入最大行数；`ExcelImporter` 在读表前判定，且**先于 purge** |
| `QUERY_MAX_RESULTS` | `2000` | 查询结果硬上限；`attendance/views.py` 先 `count()`，超限不渲染分页并提示缩小范围 |
| `QUERY_PAGE_SIZE` | `100` | 查询结果分页大小 |
| `BACKUP_RETENTION_DAYS` | `30` | 备份保留天数，`DatabaseBackup.retention_days` 读取 |
| `REMOTE_BACKUP_DIR` | `None` | 异地备份目录；`None` 表示关闭异地同步，`backup.py` 用 `getattr` 读取 |
| `LOGIN_URL` | `/accounts/login/` | 登录页 |
| `FORCE_SCRIPT_NAME` | `os.getenv('FORCE_SCRIPT_NAME', None) or None` | 反向代理路径前缀，本项目生产为 `/attendance` |
| `CSRF_TRUSTED_ORIGINS` | 环境变量 `CSRF_TRUSTED_ORIGINS` 逗号分隔 | 仅在非空时赋值 |
| `STATIC_URL` / `MEDIA_URL` | 环境变量，默认 `/static/` / `/media/` | 生产改为 `/attendance/...` 前缀 |
| `DEFAULT_AUTO_FIELD` | `django.db.models.BigAutoField` | 主键类型 |

> 自动备份（定时任务）**未实现**，不存在对应的开关。

### 日志配置

`LOGS_DIR = BASE_DIR / 'logs'`，模块导入时若不存在则 `mkdir(parents=True, exist_ok=True)`。

Handler：

| Handler | 类 | 级别 | 目标 | 格式 |
|---------|----|------|------|------|
| `console` | `logging.StreamHandler` | DEBUG | 标准输出（带 `require_debug_true` 过滤器，即仅 DEBUG 时输出） | simple |
| `file` | `RotatingFileHandler` | INFO | `logs/app.log`，10MB × 5 | verbose |
| `error_file` | `RotatingFileHandler` | ERROR | `logs/error.log`，10MB × 5 | verbose |

Logger：

| Logger | Handler | 级别 |
|--------|---------|------|
| `django` | console, file | INFO |
| `django.request` | error_file | ERROR |
| `core` / `accounts` / `attendance` / `backups` | console, file, error_file | INFO |
| `root` | console, file | INFO |

四个 app logger 与 `INSTALLED_APPS` 中的自定义应用一一对应；全部 `propagate: False`。

### 静态文件与媒体文件

| 配置 | 值 |
|------|-----|
| `STATIC_URL` | 环境变量 `STATIC_URL`，默认 `/static/` |
| `STATIC_ROOT` | `BASE_DIR / 'staticfiles'`（容器内 `/app/staticfiles`） |
| `STATICFILES_DIRS` | `[BASE_DIR / 'static']` —— 项目自带的前端资源（Bootstrap 与 Bootstrap Icons），**刻意不用 CDN**，因为可能部署在无外网的内网环境 |
| `MEDIA_URL` | 环境变量 `MEDIA_URL`，默认 `/media/` |
| `MEDIA_ROOT` | `BASE_DIR / 'media'`（容器内 `/app/media`） |

静态文件的**运行时服务方式**：`MIDDLEWARE` 里紧跟 `SecurityMiddleware` 之后是
`whitenoise.middleware.WhiteNoiseMiddleware`，生产（`DEBUG=False`）下由应用自身
提供静态文件，**不依赖 Nginx 额外配 static location** —— 内网单容器部署时少一个出错点。

上传的考勤原始文件落盘路径（`attendance/services.py` 的 `FileStorage.save`）：

```
MEDIA_ROOT / uploads / <file_kind> / <period 或 unknown> / <YYYYmmdd_HHMMSS>_<sha256 前 12 位>.<ext>
```

`file_kind` 为 `daily`（日考勤汇总表）或 `leave`（请假单据），`period` 形如 `YYYY-MM`。文件落盘后写入 `attendance_uploaded_files` 表（`UploadedFile`）。

月度报表输出到 `MEDIA_ROOT / reports/`（`attendance/report.py` 的 `build_report`，未传 `path` 时）。

`config/urls.py` 在 `DEBUG=True` 时追加 `static(MEDIA_URL, document_root=MEDIA_ROOT)` 与 `static(STATIC_URL, document_root=STATIC_ROOT)`，由开发服务器直接提供媒体/静态文件。

## 生产与开发差异

只列代码中真实存在的差异（左侧 `settings.py`，右侧 `settings_production.py`）。

| 项 | 开发 | 生产 |
|------|------|------|
| `DEBUG` 默认值 | `True` | `False` |
| 模板 loaders | `DEBUG=True` 用无缓存链 | `DEBUG=False` 强制 `cached.Loader`，并设 `OPTIONS['debug']=False`、`pop('APP_DIRS')` |
| 数据库连接 | 仅 `charset` / `init_command` | 追加 `CONN_MAX_AGE=600`、`CONN_HEALTH_CHECKS=True`、`connect_timeout=10`、`read_timeout=30`、`write_timeout=30` |
| 静态文件存储 | 默认 `StaticFilesStorage` | `DEBUG=False` 时 `whitenoise.storage.CompressedManifestStaticFilesStorage`（内容哈希 + gzip/brotli）；并显式声明 `STATICFILES_FINDERS` |
| Cookie 安全 | 未设置（Django 默认） | `SESSION_COOKIE_HTTPONLY=True`；`CSRF_COOKIE_HTTPONLY=False`（必须为 False，前端 JS 通过 `document.cookie` 读 CSRF token）；`SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` 由 `SECURE_COOKIES` 环境变量控制，默认 False |
| 其他安全响应头 | 未设置 | `SECURE_BROWSER_XSS_FILTER=True`、`SECURE_CONTENT_TYPE_NOSNIFF=True`、`X_FRAME_OPTIONS='DENY'` |
| 缓存 / 会话 | 未配置 `CACHES` | `CACHES` 显式 `LocMemCache`（`TIMEOUT=300`、`MAX_ENTRIES=1000`）；`SESSION_ENGINE='django.contrib.sessions.backends.cached_db'` |
| 日志 | `console` 仅 DEBUG 输出、级别 DEBUG | `console` 过滤器清空、级别 INFO；新增 `performance` handler → `logs/performance.log`；新增 `django.db.backends` logger（`SQL_DEBUG=True` 时为 DEBUG） |
| 邮件错误通知 | 未配置 | `EMAIL_BACKEND` 走 SMTP，`ADMINS` / `MANAGERS` 由 `ADMIN_EMAIL` 等环境变量填充 |
| 上传/请求体限制 | 未设置 | `DATA_UPLOAD_MAX_MEMORY_SIZE=10MB`、`FILE_UPLOAD_MAX_MEMORY_SIZE=10MB`、`DATA_UPLOAD_MAX_NUMBER_FIELDS=1000` |
| 可调业务常量 | 硬编码 | `IMPORT_BATCH_SIZE` / `QUERY_MAX_RESULTS` / `QUERY_PAGE_SIZE` 支持环境变量覆盖 |
| `SecurityMiddleware` | 已位于 `MIDDLEWARE` 首位 | 再做一次「不存在则 insert(0)」的兜底（实际不触发） |
| `INTERNAL_IPS` | 未设置 | `['127.0.0.1', 'localhost']` |

## 三套 settings 的用途

| 配置模块 | 定位 | 启用方式 |
|----------|------|---------|
| `config.settings` | 开发基准配置；`wsgi.py` / `asgi.py` 用 `setdefault` 指定为默认值 | 无 `DJANGO_SETTINGS_MODULE` 时生效 |
| `config.settings_production` | 生产覆盖，`from .settings import *` 后覆盖安全项、连接池、缓存、模板、日志、邮件、上传限制 | `docker-compose.server.yml` 中 `DJANGO_SETTINGS_MODULE=config.settings_production` |
| `config.settings_test` | 本地测试/自检，`from .settings import *` 后换成内存 SQLite | `python manage.py test --settings=config.settings_test` |

切换方式统一是环境变量 `DJANGO_SETTINGS_MODULE`，settings 文件末尾不做 `import` 判断。

`settings_test.py` 的具体覆盖：

- `DATABASES` 整体替换为 `django.db.backends.sqlite3` + `NAME=':memory:'`，不碰 MySQL。
- `LOGGING['handlers']['file']` 与 `error_file` 整体替换为 `{'class': 'logging.NullHandler'}`，避免测试写文件污染 `logs/`。
- `MEDIA_ROOT = tempfile.mkdtemp(prefix='attendance-test-media-')`。
- `PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']`（加速测试）。
- 文件头注释明确：仅用于本地测试与自检；生产与开发一律用 MySQL，因为报表/汇总依赖 MySQL 的 `STRICT_TRANS_TABLES` 与 `utf8mb4` 行为。

## 主路由

`config/urls.py`：

| URL 前缀 | 模块 | 说明 |
|----------|------|------|
| `/admin/` | `admin.site.urls` | Django 后台 |
| `/health/` | `core.views.health_check` | 健康检查，无认证，供 Nginx / 监控探活 |
| `/` | `core.views.home` | 首页（`@login_required`） |
| `/accounts/` | `include('accounts.urls')` | 用户、权限、审计日志 |
| `/attendance/` | `include('attendance.urls')` | 考勤模块（上传、查询、汇总、导出、规则） |
| `/backups/` | `include('backups.urls')` | 数据库备份 |

`DEBUG=True` 时额外追加 `static()` 提供的 `MEDIA_URL` 与 `STATIC_URL` 直出路由。

`core/urls.py` 定义了 `app_name = 'core'` 和 `path('health/', views.health_check)`，但根路由**未 include `core.urls`**，实际的 `/health/` 是 `config/urls.py` 直接挂载 `core.views.health_check` 得到的。

## 核心模块（core）

`core` 应用刻意保持极薄：不放业务模型，考勤相关的一切都在 `attendance` 应用（`core/views.py` 文件头注释）。

| 视图 | 认证 | 行为 |
|------|------|------|
| `health_check` | 无 | 执行 `SELECT 1` 探测数据库连通性 |
| `home` | `@login_required` | 统计概览，渲染 `home.html` |

`health_check` 返回结构：

- 数据库连通：`{"status": "ok", "database": true}`，HTTP 200。
- 数据库失败：`logger.exception('健康检查：数据库连接失败')` 记录后返回 `{"status": "degraded", "database": false}`，HTTP 503。
- 注释说明：因探活方通常无会话，所以不要求认证；也正因如此，不返回任何业务数据。

`home` 的模板上下文：

| 变量 | 取值 |
|------|------|
| `daily_count` | `AttendanceDaily.objects.count()` |
| `leave_count` | `LeaveRecord.objects.count()` |
| `file_count` | `UploadedFile.objects.count()` |
| `recent_files` | `UploadedFile.objects.order_by('-uploaded_at')[:5]`（最近 5 条上传记录） |

`templates/home.html` 用三张数字卡片展示上述三个计数，并在有 `recent_files` 时渲染最近上传文件列表。

## 备份模块（backups）

| 文件 | 职责 |
|------|------|
| `backups/models.py` | `BackupRecord`（表 `backup_records`）：`file_path` / `file_size` / `status`(success,failed) / `error_message` / `created_by`(SET_NULL) / `created_at`；`ordering = ['-created_at']` |
| `backups/backup.py` | `DatabaseBackup` 服务类（服务层在这里，不叫 `services.py`），模块末尾导出单例 `backup_service = DatabaseBackup()` |
| `backups/views.py` | 列表 / 创建 / 下载 / 删除四个函数视图 |
| `backups/urls.py` | `app_name = 'backups'`，4 条路由 |
| `backups/admin.py` | 注册 `BackupRecord` 到 Django 后台 |
| `backups/tests.py` | 文件层与边界条件测试（不真的调 `mysqldump`，测试环境是 SQLite） |

**实现方式**：调用外部命令 `mysqldump` / `mysql`，不是 Django 的 `dumpdata`。`Dockerfile` 为此安装 `default-mysql-client`（注释：仅备份功能需要 `mysqldump` / `mysql`）。

`create_backup()` 流程：

1. 文件名默认 `backup_YYYYmmdd_HHMMSS.sql`。
2. 组装 `mysqldump` 命令：`--host` / `--port` / `--user` / `--password`（均取自 `settings.DATABASES['default']`）、`--skip-ssl`、`--single-transaction`、`--routines`、`--triggers`、库名。
3. `subprocess.run(cmd, stdout=PIPE, stderr=PIPE, text=True, timeout=SUBPROCESS_TIMEOUT)`，把 `result.stdout` 自己写进 `.sql` 文件。
4. 超时（`TimeoutExpired`）→ 删除半截文件，提示改用服务器 crontab 执行；`returncode != 0` → 删除文件并返回 `stderr`。
5. 成功 → `zipfile.ZipFile(..., ZIP_DEFLATED)` 打包为 `<原名>.zip`，删除原始 `.sql`；配置了 `REMOTE_BACKUP_DIR` 时再 `shutil.copy2` 同步异地。
6. 返回 `(success, message, filename)`。

其他方法：

| 方法 | 行为 |
|------|------|
| `list_backups()` | 扫描备份目录，只取 `.zip` / `.sql`，按修改时间倒序，附 `size_display` |
| `download_backup(name)` | 返回 `FileResponse`，`Content-Type` 按后缀给 `application/zip` / `application/sql`，`Content-Disposition` 用 `filename*=UTF-8''` 编码中文名 |
| `delete_backup(name)` | 删本地文件；配了异地目录时一并删除异地副本 |
| `restore_backup(name)` | zip 先解出临时 `_temp_*.sql`，再用 `mysql` 命令以 `stdin` 导入，`finally` 中删除临时文件 |
| `sync_to_remote(name)` | `shutil.copy2` 到异地目录 |
| `cleanup_old_backups()` | 按 mtime 删除超过 `retention_days` 的 `.zip` / `.sql`（含异地），返回 `(True, 删除数, 消息)` |
| `get_backup_stats()` | `total_count` / `total_size_mb` / `today_count` / `week_count` |

**备份目录**：优先读环境变量 `BACKUP_DIR`，否则 `BASE_DIR / 'backups'`。生产 `docker-compose.server.yml` 挂载 `./backup_files:/app/backup_files`，`.env.prod.template` 中 `BACKUP_DIR=/app/backup_files`，两者对齐。

**保留策略**：`BACKUP_RETENTION_DAYS = 30`（默认）+ `cleanup_old_backups()` 手动/程序化清理。该方法目前只被 `backups/tests.py` 调用，页面未提供触发入口，项目内也没有对应的 management command。

**页面**（`/backups/`，全部要求登录 + 管理员）：

| 路由 | 视图 | 说明 |
|------|------|------|
| `/backups/` | `backup_list` | 渲染 `backups/backup_list.html`，`BackupRecord` 每页 20 条 |
| `/backups/create/` | `create_backup` | 仅 POST，调 `backup_service.create_backup()`，成功/失败都写 `BackupRecord`，返回 `JsonResponse` |
| `/backups/<pk>/download/` | `download_backup` | 文件不存在时 404 |
| `/backups/<pk>/delete/` | `delete_backup` | 仅 POST，删物理文件 + 删记录，返回 `JsonResponse` |

视图用 `@login_required` + `@admin_permission_required`（`accounts/decorators.py`：`super_admin` / `admin` / Django superuser 放行，其余重定向），并通过 `accounts.services.log_action` 写审计（动作码 `BACKUP_CREATE`、`BACKUP_DELETE`）。

## 部署相关文件

| 文件 | 职责 |
|------|------|
| `Dockerfile` | 基于 `python:3.10-slim`；替换 Debian apt 源为阿里云镜像（同时兼容 `sources.list` 与 Debian 13 的 deb822 `debian.sources`）；安装 `default-mysql-client`（仅备份需要）；用清华 PyPI 源装 `requirements.txt`；`COPY . .`；预建 `media/uploads media/reports staticfiles logs backup_files`；`chmod +x docker-entrypoint.sh`；`EXPOSE 8000`；`CMD ["sh", "docker-entrypoint.sh"]` |
| `docker-compose.yml` | 本地开发：`db`（`mysql:8.0`，容器名 `attendance_db`，映射 `3309:3306`，`utf8mb4` / `utf8mb4_unicode_ci`，`--default-time-zone='+08:00'`，`mysqladmin ping` 健康检查，卷 `mysql_data`）+ `web`（`build: .`，映射 `8001:8000`，`DEBUG=True`，`DB_HOST=db`、`DB_PORT=3306`，`GUNICORN_WORKERS=4`，挂载 `logs`/`templates`/静态/媒体/备份卷，`depends_on: db healthy`） |
| `docker-compose.server.yml` | 生产：`env_file: .env`，`DJANGO_SETTINGS_MODULE=config.settings_production`，镜像 `attendance-web:latest`，端口 `127.0.0.1:8001:8000`（仅本机，由共享 Nginx 反代），**刻意不挂载代码与模板**（随镜像走以保证可回滚），只挂 `./logs`、静态卷、媒体卷、`./backup_files` |
| `docker-entrypoint.sh` | 容器启动脚本，见下 |
| `gunicorn.conf.py` | `bind = "0.0.0.0:8000"`；`workers` 默认 `2 × CPU + 1`（`GUNICORN_WORKERS` 可覆盖）；`worker_class = sync`；`threads = 2`；`max_requests = 1000` + `max_requests_jitter = 50`（防内存泄漏）；`timeout = 120`、`keepalive = 5`、`graceful_timeout = 30`；access/error log 写 `/app/logs/gunicorn-access.log`、`gunicorn-error.log`；`proc_name = 'attendance_system'`；请求行/字段上限；含 `on_starting` / `when_ready` / `post_fork` 等日志钩子。注：`docker-entrypoint.sh` 的启动命令用 `--access-logfile -` / `--error-logfile -` 把访问与错误日志再覆盖到标准输出 |
| `deploy.sh` | 服务器部署：校验 `.env` 存在 → `docker compose -f docker-compose.server.yml build`（`--no-build` 可跳过）→ `up -d` → `sleep 15` → `curl -fsS http://127.0.0.1:8001/health/` 校验，失败则提示查看 `logs` |
| `requirements.txt` | `Django==4.1.13`、`PyMySQL==1.1.2`、`openpyxl==3.1.5`、`pandas==2.3.3`、`numpy==2.2.6`、`python-dotenv==1.2.1`、`gunicorn==21.2.0` 等（Python 3.10+） |

**`docker-entrypoint.sh` 启动时自动做的事**（`set -e`，共 6 步）：

1. 打印环境信息：Python 版本、Django 版本、`TZ`、`DEBUG`。
2. `sleep 10`；再用内嵌 Python + PyMySQL 重试连接数据库，最多 30 次、每次间隔 2 秒，仍失败则 `sys.exit(1)` 终止启动。
3. `python manage.py migrate --noinput`；随后 `showmigrations` 只打印前 20 行。
4. 若存在 `create_superuser.py` 则执行它（**会自动建超级用户**）：用户名/邮箱/口令取自 `DJANGO_SUPERUSER_USERNAME`（默认 `admin`）、`DJANGO_SUPERUSER_EMAIL`（默认 `admin@example.com`）、`DJANGO_SUPERUSER_PASSWORD`；用户已存在则跳过；创建后把 `UserProfile.role` 回写为 `super_admin`。
5. `collectstatic --noinput --clear` 清旧文件后重新收集，并统计 `/app/staticfiles` 下文件数。
6. 打印数据库表数量、`/app/media` 大小，必要时创建 `/app/logs`；最后 `exec gunicorn config.wsgi:application --config /app/gunicorn.conf.py --bind 0.0.0.0:8000 ...`（用 `exec` 替换进程以保证信号传递）。

**Nginx 路径前缀 `/attendance/`**：与同机另一套系统共享同一个 Nginx 实例，本项目不占根路径。对应配置为 `FORCE_SCRIPT_NAME=/attendance`、`STATIC_URL=/attendance/static/`、`MEDIA_URL=/attendance/media/`、`CSRF_TRUSTED_ORIGINS` 填外部访问地址；Nginx 侧 `location /attendance/ { proxy_pass http://127.0.0.1:8001/; ... proxy_set_header SCRIPT_NAME /attendance; client_max_body_size 60m; }`（上传文件上限 50MB，留出余量）。

<!-- ## 已避开的坑

| 坑 | 本项目做法 ||----|-----------|
| Django 4.1 起 `TEMPLATES` 未显式配 `loaders` 会**无条件**缓存模板（与 `DEBUG` 无关），改模板必须重启进程 | `settings.py` 显式声明 `loaders`：`DEBUG=True` 用不带缓存的链；同时因显式 loaders 要求 `APP_DIRS=False`，把 `app_directories.Loader` 写进链里。`docker-compose.yml` 之所以敢挂载 `templates/`，前提就是这个配置 |
| Docker 用 Go `filepath.Match` 语义，`*.sql` 的 `*` 不跨 `/`，只匹配根目录 | `.dockerignore` 用 `**/*.sql`，并同时排除 `**/*.sql.zip`、`**/*.zip` |
| 排除整个 `backups/` 目录会连带排掉 Django app（`models.py`、`migrations/`），而它同时又是运行时备份目录 | `.dockerignore` 只排除文件类型（`**/*.sql` 等），不排除 `backups/` 目录本身 |
| `.dockerignore` 的 `!` 例外无法把已被排除的父目录下的文件捞回来 | 只排除文件类型而不排除 `attendance/` 目录，才能用 `!attendance/templates_xlsx/*.xlsx` 把报表模板（代码资产）保留在镜像里 |
| 考勤原始数据/报表含员工个人信息、测试脚本含真实姓名作夹具 | `.dockerignore` 排除 `media/`、`*.xlsx`、`**/*.xlsx`、`tools/`、`**/tests.py`、`**/test*.py`、`**/*_tests.py`、`**/conftest.py`（注释记载：实测曾把它们打进镜像，导致员工姓名随镜像分发出去） |
| 排除测试文件的模式写窄了，导致 `attendance/tests_report.py` **被静默打进镜像** | 模式由 `**/test_*.py` 改为 `**/test*.py`（前者要求前缀 `test_`，匹配不到 `tests_` 开头的文件）。`DockerignoreCoverageTests` 扫出仓库里所有测试/工具脚本并断言每个都被排除，防止同类疏漏 |
| `subprocess.run` 的 `timeout` 只有在「捕获输出（PIPE）」时才会真正杀掉子进程 | `backups/backup.py` 用 `stdout=PIPE` 捕获后自己写 `.sql` 文件，保证超时后 `mysqldump` 被终止、不残留半截文件与僵尸进程 |
| 备份子进程超时若大于 gunicorn 超时，worker 会被杀在备份中途 | `SUBPROCESS_TIMEOUT = 90` < gunicorn `timeout = 120`，两者在 `gunicorn.conf.py` 注释里明确要求保持该大小顺序 |
| 钉钉导出的 xlsx 缺少 `dimension` 元数据，`openpyxl` 的 `read_only=True` 下 `max_row` / `max_column` 恒为 1，整张表被读空 | `attendance/services.py` 的 `load_workbook_plain()` 强制 `read_only=False`；模块文件头也再次声明所有解析都不使用 `read_only=True` |
| `settings_test.py` 里只把 handler 的 `class` 改成 `NullHandler`，`filename` 仍会传进去，抛 `Handler.__init__() got an unexpected keyword argument 'filename'` | 整体替换 handler 定义（`{'class': 'logging.NullHandler'}`），而不是只改 `class` 键 |
| 备份页面读的目录与 compose 挂载点不一致，历史备份永远列不出来 | 备份目录改为优先读环境变量 `BACKUP_DIR`，与 `docker-compose.server.yml` 的 `/app/backup_files` 对齐 |
| `cleanup_old_backups()` 在备份目录不存在时 `os.listdir` 抛 `FileNotFoundError`，而不是「没有可清理的备份」 | 先 `os.path.isdir(self.backup_dir)` 判断，不存在时返回 `(True, 0, '备份目录不存在，无需清理')` |
| `list_backups()` 返回 naive datetime，`timezone.now()` 是 aware，`USE_TZ=True` 下直接比较抛 `can't compare offset-naive and offset-aware datetimes` | `get_backup_stats()` 用 `timezone.make_aware()` 先本地化再比较，并用 `timezone.localtime()` 判定「今天」 |
| 入口脚本用 `showmigrations \| head -20`：`head` 读够就关闭管道，Django 继续写已关闭的 stdout 抛 `BrokenPipeError`（启动日志里出现一长串 traceback） | 改用 `sed -n '1,20p'`，只取前 20 行且不提前关闭管道 |
| `accounts/signals.py` 的 `post_save` 已在 User 创建瞬间用 `role='user'` 建好 profile，`get_or_create` 的 `defaults` 被静默忽略 | `create_superuser.py` 显式回写 `profile.role = 'super_admin'` 并 `save(update_fields=['role'])`，否则超管在业务层的角色仍是普通用户 |
| PyMySQL 必须在任何 Django 数据库访问之前 `install_as_MySQLdb()` | 放在 `config/__init__.py` 包初始化里，而不是 settings（settings 是被本包导入的） |
| 查询结果全量内联进 HTML（另一套系统实测 3000 条命中 ≈ 5MB HTML，10 万条约 170MB） | `QUERY_MAX_RESULTS = 2000` 硬上限，超限时只提示「结果过多，请缩小范围」而不渲染分页 |
| 对上传文件缺少扩展名/大小/非空校验 | `FileStorage.validate_upload()` 校验扩展名（`ALLOWED_EXTENSIONS`）、空文件、超过 `IMPORT_MAX_FILE_SIZE`，都抛带中文提示的业务异常 |
| `MAX_FILE_SIZE` 环境变量调大后上传上限不生效 —— 它喂给了一个**无人引用**的 `MAX_UPLOAD_SIZE`，真正校验的 `IMPORT_MAX_FILE_SIZE` 写死 50MB，且不会有任何报错 | 两者合并：`IMPORT_MAX_FILE_SIZE` 直接读 `MAX_FILE_SIZE`；死配置已删除，`UploadSizeLimitTests` 阻止它回来 |
| `IMPORT_MAX_ROWS` 只是 settings 里的一句声明、无任何代码引用，等于"文档写了上限但实际无上限" | `ExcelImporter.import_daily` / `import_leave` 在读表前按各 Sheet `max_row` 之和判定，超限直接拒绝 |
| 行数上限若在"清空账期"**之后**判定，一个超限文件会先把当月数据删光再报错 | 上限判定**先于 purge**；`ImportLimitTests.test_row_cap_is_checked_before_purge` 用「先导入 6 行 → 再传超限文件」的方式守住这个顺序（把检查挪到 purge 之后该用例立刻变红） |
| 用 `innerHTML` 拼接 Excel 数据造成存储型 XSS | `templates/base.html` 提供 `escapeHtml()`，模板全部走 Django 自动转义 | -->
