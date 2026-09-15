# deployment — 部署与运维

本模块描述考勤管理系统的容器化部署方式：镜像构建、容器启动、Nginx 接入、
数据库备份恢复与升级回滚。

---

## 环境变量

生产变量以 `.env.prod.template` 为唯一模板（复制为 `.env` 后填写，`.env` 已被
`.gitignore` 与 `.dockerignore` 双重排除，不进版本库也不进镜像）。

| 变量 | 用途 | 生产必须修改 |
|---|---|---|
| `SECRET_KEY` | Django 签名密钥；模板值为 `CHANGE-ME-TO-A-RANDOM-50-CHAR-STRING` | **必须改**。`config/settings.py` 有内置开发默认值，不改等于公开密钥 |
| `DEBUG` | 调试开关；`settings_production.py` 读它决定安全项、缓存、静态存储等分支 | 必须保持 `False`（模板已是 `False`） |
| `ALLOWED_HOSTS` | 允许的 Host 头；模板为 `your-domain.com` | **必须改**为真实域名或 IP |
| `DB_NAME` | 数据库名，模板 `attendance_db` | 一般不改 |
| `DB_USER` | 数据库账号，模板 `att_admin` | 一般不改 |
| `DB_PASSWORD` | 数据库口令；模板值为 `CHANGE-ME` | **必须改**（与 `MYSQL_PASSWORD` 同源，改后需清库或同步改 MySQL 用户口令） |
| `DB_HOST` | 数据库主机，生产为 `db`（容器服务名） | 一般不改 |
| `DB_PORT` | 数据库端口，生产为 `3306`（容器内端口，不是宿主 3309） | 一般不改 |
| `FORCE_SCRIPT_NAME` | 子路径部署前缀，生产为 `/attendance` | 必须为 `/attendance`（不带结尾斜杠，与 Nginx 前缀一致） |
| `STATIC_URL` | 静态文件 URL，生产为 `/attendance/static/` | 必须带前缀 |
| `MEDIA_URL` | 媒体文件 URL，生产为 `/attendance/media/` | 必须带前缀 |
| `CSRF_TRUSTED_ORIGINS` | CSRF 信任源，模板为 `https://your-domain.com` | **必须改**为实际访问来源（含域名/协议） |
| `BACKUP_DIR` | 备份目录，生产为 `/app/backup_files`（与 compose 挂载点对齐） | 必须与挂载点一致，否则历史备份列不出来 |
| `QUERY_MAX_RESULTS` | 查询结果硬上限，模板 2000 | 按需要调 |
| `QUERY_PAGE_SIZE` | 分页大小，模板 100 | 按需要调 |
| `DB_ROOT_PASSWORD` | MySQL root 口令，被 `docker-compose.server.yml` 的 `MYSQL_ROOT_PASSWORD` 与 healthcheck 引用 | **必须改**。该编排引用它且**没有默认值**，缺失会导致 MySQL 容器起不来、healthcheck 永久失败；`.env.prod.template` 已列出该项 |

由代码读取、但模板未列出的可选变量：

| 变量 | 用途 | 生产必须修改 |
|---|---|---|
| `DJANGO_SUPERUSER_USERNAME` / `DJANGO_SUPERUSER_PASSWORD` / `DJANGO_SUPERUSER_EMAIL` | `create_superuser.py` 建超管时的取值（默认 `admin` / `admin123456` / `admin@example.com`） | 建议设置：默认口令仅用于首次部署，登录后必须立即修改 |
| `GUNICORN_WORKERS` / `GUNICORN_THREADS` / `GUNICORN_TIMEOUT` / `GUNICORN_WORKER_CLASS` / `GUNICORN_MAX_REQUESTS` | `gunicorn.conf.py` 的覆盖开关 | 非必须；服务器核数决定默认 worker 数，本仓库未固定 |
| `SECURE_COOKIES` | 置 `True` 时开启 `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | HTTPS 启用后再开 |
| `SQL_DEBUG`、`IMPORT_BATCH_SIZE`、`EMAIL_*`、`ADMIN_EMAIL` | SQL 日志级别、导入批大小、错误通知邮件 | 非必须 |
| `TZ` | 容器时区，编排中固定为 `Asia/Shanghai` | 已在 compose 中设定 |

---

## 构建镜像

### Dockerfile 做了什么

按 `Dockerfile` 实际内容逐条：

| 步骤 | 内容 |
|---|---|
| 基础镜像 | `python:3.10-slim` |
| 通用环境变量 | `PYTHONUNBUFFERED=1`、`PYTHONDONTWRITEBYTECODE=1`、`PIP_NO_CACHE_DIR=1`、`PIP_DISABLE_PIP_VERSION_CHECK=1` |
| 工作目录 | `/app` |
| apt 源加速 | 用 `sed` 把 `deb.debian.org` / `security.debian.org` 换成 `mirrors.aliyun.com`；同时兼容三种源文件形态：`/etc/apt/sources.list`（Debian ≤ 12）、`/etc/apt/sources.list.d/debian.sources`（Debian ≥ 13 的 deb822 格式）、`debian.sources.docker` |
| 系统依赖 | 仅装 `default-mysql-client`（**只备份功能需要 `mysqldump` / `mysql`**），装完删除 apt 列表 |
| Python 依赖 | `COPY requirements.txt .` 后 `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt` |
| 代码 | `COPY . .`（受 `.dockerignore` 过滤） |
| 目录预建 | `mkdir -p media/uploads media/reports staticfiles logs backup_files` |
| 权限 | `chmod +x docker-entrypoint.sh` |
| 端口声明 | `EXPOSE 8000` |
| 启动命令 | `CMD ["sh", "docker-entrypoint.sh"]` |

需要明确的几点：

- **没有非 root 用户**：Dockerfile 中不存在 `USER` 指令，容器进程以 root 运行。
- **不在构建期收集静态文件**：`collectstatic` 由 `docker-entrypoint.sh` 在容器启动时执行，
  因此运行期需要可写的 `staticfiles` 卷。
- **没有 `HEALTHCHECK` 指令**：Web 容器没有 Docker 级健康检查，探活靠 HTTP 接口
  `/health/`（见下文）。
- **时区**：镜像内不写死时区，由 compose 的 `TZ=Asia/Shanghai` 与
  `settings_production.py` 的 `TIME_ZONE = 'Asia/Shanghai'` 共同决定。
- 依赖清单见 `requirements.txt`：Django 4.1.13、PyMySQL 1.1.2、openpyxl 3.1.5、
  pandas 2.3.3、python-dotenv 1.2.1、gunicorn 21.2.0 等（MySQL 驱动用 PyMySQL，
  由 `config/__init__.py` 注册为 `MySQLdb`）。

### .dockerignore 排除了什么

`Dockerfile` 里的 `COPY . .` 之前会先应用 `.dockerignore` 的规则，目的写在文件头：
缩小构建上下文，并**避免把本地开发产物与敏感的考勤原始数据打进镜像**。

| 排除项 | 原因（文件内注明） |
|---|---|
| `.git/`、`.gitignore` | 版本控制元数据 |
| `venv/`、`env/`、`ENV/`、`.venv/` | 宿主机虚拟环境，容器内由 pip 重新安装 |
| `*.sql`、`**/*.sql`、`**/*.sql.zip`、`**/*.zip` | 数据库备份文件可能含真实考勤数据且体积大 |
| `media/`、`uploads/`、`reports/`、`*.xlsx`、`*.xls`、`**/*.xlsx`、`**/*.xls` | 考勤原始数据与生成的报表，含员工姓名、工号等个人信息，禁止进镜像 |
| `staticfiles/`、`static_collected/` | 运行时由卷挂载 / `collectstatic` 生成 |
| `logs/`、`*.log` | 日志 |
| `.env`、`.env.local`、`.env.production` | 含口令，禁止进镜像 |
| `__pycache__/`、`*.py[cod]`、`.pytest_cache/`、`.coverage`、`htmlcov/` | Python 缓存与覆盖率产物 |
| `.vscode/`、`.idea/`、`.cursor/`、`*.swp`、`*.swo`、`.DS_Store`、`Thumbs.db`、`desktop.ini` | IDE / 操作系统文件 |
| `docs/` | 文档运行时不需要 |
| `tools/`、`**/tests.py`、`**/test_*.py`、`**/conftest.py` | 测试与开发期脚本运行时不依赖 |
| `*.egg-info/`、`dist/`、`build/` | 其他构建产物 |

---

## 启动流程

### docker-entrypoint.sh 逐步说明

脚本以 `set -e` 运行，任一步失败即退出（容器重启循环）。步骤按脚本内的编号：

| 步骤 | 动作 |
|---|---|
| 前置 | 打印 Python 版本、Django 版本、`TZ`、`DEBUG` 环境信息 |
| **[1/6] 等库** | `sleep 10`，固定等待 10 秒 |
| **[2/6] 检查数据库连接** | 内嵌 Python 脚本用 `pymysql` 连接 `DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME`，最多重试 30 次、每次间隔 2 秒，成功打印「数据库连接成功」并退出；30 次仍失败则以非零码退出（容器启动失败） |
| **[3/6] 迁移** | `python manage.py migrate --noinput`；随后 `python manage.py showmigrations 2>/dev/null \| sed -n '1,20p'` 打印前 20 行迁移状态 |
| **[4/6] 超级用户** | 若存在 `create_superuser.py` 则执行（不存在则打印警告跳过）。该脚本：用户名/邮箱/口令取 `DJANGO_SUPERUSER_*` 环境变量，默认 `admin` / `admin@example.com` / `admin123456`；用户已存在则跳过；创建后显式把 `UserProfile.role` 回写为 `super_admin`（否则 `accounts/signals.py` 建 profile 时用的默认 `role='user'` 会被静默忽略） |
| **[5/6] 静态文件** | `collectstatic --noinput --clear`（先清空，输出去向 `/dev/null`），再 `collectstatic --noinput`；用 `find /app/staticfiles -type f \| wc -l` 统计文件数并打印 |
| **[6/6] 系统信息** | 用 Django 连接执行 `SHOW TABLES` 统计表数；`du -sh /app/media` 打印媒体目录大小；`/app/logs` 不存在则创建 |
| 启动 | `exec gunicorn config.wsgi:application --config /app/gunicorn.conf.py --bind 0.0.0.0:8000 --log-level info --access-logfile - --error-logfile -`。用 `exec` 替换当前进程，保证信号正确传递 |

脚本结束时打印的访问地址（`http://localhost:8001`、`/admin`、`/health/`）是
**宿主机映射端口**的口径；容器内 Gunicorn 监听的是 `0.0.0.0:8000`。

### Gunicorn 配置

`gunicorn.conf.py`（均可由环境变量覆盖）：

| 配置项 | 值 | 说明 |
|---|---|---|
| `bind` | `0.0.0.0:8000` | 与 entrypoint 的 `--bind` 一致 |
| `daemon` | `False` | 前台运行，由容器托管 |
| `workers` | `GUNICORN_WORKERS`，默认 `multiprocessing.cpu_count() * 2 + 1` | 本地开发 compose 显式设为 4；**生产未设置该变量，因此用公式默认值**，实际数量取决于服务器核数 |
| `worker_class` | `GUNICORN_WORKER_CLASS`，默认 `sync` | |
| `threads` | `GUNICORN_THREADS`，默认 `2` | 多进程 + 多线程 |
| `max_requests` | `GUNICORN_MAX_REQUESTS`，默认 `1000` | 防内存泄漏；`max_requests_jitter` 默认 50 |
| `timeout` | `GUNICORN_TIMEOUT`，默认 `120` 秒 | **必须大于 `backups/backup.py` 的 `SUBPROCESS_TIMEOUT = 90`**，否则备份未完成 worker 就被杀掉，留下半截文件且无记录 |
| `keepalive` | 默认 `5` 秒 | |
| `graceful_timeout` | 默认 `30` 秒 | 优雅重启 |
| `accesslog` / `errorlog` | `/app/logs/gunicorn-access.log` / `/app/logs/gunicorn-error.log` | 但 entrypoint 用 `--access-logfile -`、`--error-logfile -` 覆盖为 **stdout / stderr**，实际落点是 `docker compose logs` |
| `loglevel` | 默认 `info` | |
| `access_log_format` | 含 `%(D)s`（请求耗时微秒） | |
| `proc_name` | `attendance_system` | |
| 安全限制 | `limit_request_line=4096`、`limit_request_fields=100`、`limit_request_field_size=8190` | |
| 生命周期钩子 | `on_starting` / `when_ready` / `post_fork` / `worker_int` / `worker_abort` / `on_exit` | 仅打日志 |

容器内日志目录 `/app/logs` 挂到宿主机 `./logs`（两个编排文件都挂），
Django 应用日志另有 `logs/app.log`、`logs/error.log`（`RotatingFileHandler`，
10MB × 5）；`logs/performance.log` 由 `settings_production.py` 在 `DEBUG=False`
时追加，用于 `django.db.backends` 的 SQL 日志。

### 健康检查

接口：`GET /health/`（`core/views.py::health_check`，路由注册在 `core/urls.py`
与 `config/urls.py`）。**不需要认证**，刻意不返回任何业务数据。

| 情况 | HTTP 状态 | JSON |
|---|---|---|
| 进程存活且 `SELECT 1` 成功 | `200` | `{"status": "ok", "database": true}` |
| 数据库连接失败 | `503` | `{"status": "degraded", "database": false}` |

返回体只有 `status` 与 `database` 两个字段，数据库失败时会在 `core` logger 记录异常。

手工验证：

```bash
# 1) 宿主机直连（容器内 Gunicorn 监听 8000，映射到宿主 8001）
curl -fsS http://127.0.0.1:8001/health/

# 2) 查看启动日志（部署失败时的第一步）
docker compose -f docker-compose.server.yml logs --tail=100 web
```

`deploy.sh` 内部就是 `curl -fsS http://127.0.0.1:8001/health/`，
失败时提示查看上述日志命令并以非零码退出。

---

## Nginx 配置（共享实例）

`/attendance/` 的 location 片段（与 `README.md` 第 7 节一致）：

```nginx
location /attendance/ {
    proxy_pass http://127.0.0.1:8001/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header SCRIPT_NAME /attendance;
    client_max_body_size 60m;      # 上传的考勤文件最大 50MB
}
```

要点：

- `proxy_pass http://127.0.0.1:8001/;` —— 对应生产编排里 Web 只监听
  `127.0.0.1:8001`。**结尾的斜杠会剥掉 `/attendance/` 前缀**，
  容器内收到的路径是 `/health/`、`/attendance/...`（Django 自身路由）。
- `client_max_body_size 60m` 与 Django 侧的上限配套：
  `MAX_FILE_SIZE` / `IMPORT_MAX_FILE_SIZE` 均为 50MB（`.env.example` 的
  `MAX_FILE_SIZE=52428800`、`settings.py` 的 `IMPORT_MAX_FILE_SIZE = 50 * 1024 * 1024`），
  `settings_production.py` 的 `DATA_UPLOAD_MAX_MEMORY_SIZE`、
  `FILE_UPLOAD_MAX_MEMORY_SIZE` 为 10MB（超过则落临时文件）。
- **`SCRIPT_NAME` 为什么必要**：应用挂在子路径下，Django 必须知道这段前缀，
  否则 `{% url %}` 反解、`HttpResponseRedirect`、静态文件前缀、后台登录跳转
  都会生成不带 `/attendance/` 的绝对路径，浏览器会打到根路径上的另一个项目。
  两条途径可提供该值：
  1. `.env.prod.template` 的 `FORCE_SCRIPT_NAME=/attendance`
     （`config/settings.py` 读入，Django 的 `get_script_name()` 优先使用它）；
  2. Nginx 传的 `SCRIPT_NAME` 请求头（仅在未设 `FORCE_SCRIPT_NAME` 时生效）。
     本项目两者都给，取值一致。
  模板侧也依赖这个值：`templates/base.html` 中
  `const SCRIPT_PREFIX = '{{ request.META.SCRIPT_NAME|default:"" }}';`，
  所有 `fetch()` 的 URL 都要拼上它 —— 文件注释写明「否则在子路径部署下
  会打到别的项目上」。
- **不需要为静态文件单独配 location**：应用内挂了 WhiteNoise
  （`whitenoise.middleware.WhiteNoiseMiddleware` + `CompressedManifestStaticFilesStorage`），
  生产（`DEBUG=False`）下 `/attendance/static/` 由 Gunicorn 直接提供。
  媒体文件（上传的原始考勤表、生成的报表）**本来就不需要对外暴露** ——
  页面通过各自的视图读取或下载它们，不靠 URL 直链。
  这样内网单容器部署时，除了上面的反代片段外没有额外配置项。
- **Nginx 配置文件本身不在本仓库内**，由服务器上的共享实例维护（与同机其它项目
  的 location 共存于同一份配置）。

---


## 部署步骤（生产）

`deploy.sh` 是仓库内唯一的部署脚本（`#!/bin/bash`，需在服务器上执行）：

```bash
cd /path/to/attendance-web

# 1) 准备 .env（脚本会先检查，不存在则直接退出并打印下一步）
cp .env.prod.template .env
vi .env      # 至少填 SECRET_KEY、DB_PASSWORD、DB_ROOT_PASSWORD、
             # ALLOWED_HOSTS、CSRF_TRUSTED_ORIGINS

# 2) 构建 + 启动 + 健康检查
./deploy.sh

# 仅重启、不重新构建镜像
./deploy.sh --no-build
```

`deploy.sh` 内部依次做四件事（不做别的）：

1. `[1/4]` 检查 `.env` 是否存在；缺失则打印
   `cp .env.prod.template .env && vi .env` 并以非零码退出。
   随后 `docker compose -f docker-compose.server.yml build`
   （传 `--no-build` 时跳过）。
2. `[2/4]` `docker compose -f docker-compose.server.yml up -d`。
3. `[3/4]` `sleep 15` 等待服务就绪
   （容器内 entrypoint 自身还有 10 秒等库与 30 次 × 2 秒重连）。
4. `[4/4]` `curl -fsS http://127.0.0.1:8001/health/`；成功打印
   「Nginx 接入路径: /attendance/」「直连（仅本机）: http://127.0.0.1:8001/」，
   失败打印 `docker compose -f docker-compose.server.yml logs --tail=100 web`
   并以非零码退出。

首次部署后的收尾（依据 `create_superuser.py` 与 `README.md` 第 1.3 节）：

- 首启会自建超级用户，默认 `admin` / `admin123456`，**登录后立即修改**；
  生产建议先在 `.env` 里设置 `DJANGO_SUPERUSER_PASSWORD` 再启动。
- `collectstatic` 与 `migrate` 由 entrypoint 自动完成，无需手工执行。
- Nginx 侧追加 `/attendance/` 的 location，然后 `nginx -t && nginx -s reload`。
  **静态文件不需要额外 location**（应用内的 WhiteNoise 会提供）。

---

## 数据库备份与恢复

### 实现

`backups` 应用（`backups/backup.py` 的 `DatabaseBackup` 类，模块级单例
`backup_service`）：

| 能力 | 说明 |
|---|---|
| 备份 | `create_backup()`：调用容器内的 `mysqldump`（由 Dockerfile 安装的 `default-mysql-client` 提供），参数含 `--skip-ssl --single-transaction --routines --triggers`，连接信息直接取 `settings.DATABASES['default']`。SQL 先写到 `BACKUP_DIR`，再压成同名 `.zip`（`ZIP_DEFLATED`）并删除中间的 `.sql`，返回 `backup_YYYYmmdd_HHMMSS.sql.zip` |
| 超时 | `SUBPROCESS_TIMEOUT = 90` 秒；超时会删除半截文件并提示「请改用服务器 crontab 执行 mysqldump」 |
| 列表 | `list_backups()` 扫描 `BACKUP_DIR` 下的 `.zip` / `.sql`，按修改时间倒序 |
| 下载 | `download_backup()` 返回 `FileResponse`，MIME 为 `application/zip` 或 `application/sql` |
| 删除 | `delete_backup()` 删物理文件（若配置了异地目录则一并删） |
| 恢复 | `restore_backup()`：`.zip` 先解出临时 `.sql`，再用 `mysql` 客户端导入，`finally` 中删除临时文件 |
| 清理 | `cleanup_old_backups()` 删除早于 `BACKUP_RETENTION_DAYS` 的文件；先判目录存在，避免 `FileNotFoundError` |
| 统计 | `get_backup_stats()`：总数、总大小、今日/近 7 天数量 |
| 异地同步 | `sync_to_remote()`：拷贝到 `settings.REMOTE_BACKUP_DIR` |

配置来源：
`BACKUP_DIR`（环境变量，生产 `/app/backup_files`；未设置时回落
`settings.BASE_DIR/backups`）、`BACKUP_RETENTION_DAYS = 30`、
`REMOTE_BACKUP_DIR = None`（**默认未配置，异地同步实际处于关闭状态**）。
**没有定时自动备份** —— 不存在自动备份开关，备份需在 `/backups/` 页面手工创建。

页面入口：`/backups/`（`backups/urls.py` 只有列表、创建、下载、删除四个路由，
均要求 `@login_required` + `@admin_permission_required`，操作写审计日志
`BACKUP_CREATE` / `BACKUP_DELETE`，并在 `BackupRecord` 表留记录）。
**`restore_backup()` 未暴露任何页面或 URL**，恢复只能手工触发。

### 备份文件在哪

| 环境 | 位置 |
|---|---|
| 生产 | 容器内 `/app/backup_files`，即宿主机项目目录下的 `./backup_files/`（`docker-compose.server.yml` 的绑定挂载），备份文件在宿主机可见 |
| 本地开发 | 命名卷 `backup_volume`，容器内 `/app/backup_files` |
| 列表记录 | 数据库表 `backup_records`（`backups/models.py` 的 `BackupRecord`：路径、大小、状态、失败原因、操作人、时间） |

### 恢复

在服务器上（备份文件已通过 `./backup_files/` 可见）：

```bash
# 方式一：调用应用自带的恢复方法（无页面入口，需手工执行）
docker compose -f docker-compose.server.yml exec web python -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings_production')
django.setup()
from backups.backup import backup_service
print(backup_service.restore_backup('backup_YYYYmmdd_HHMMSS.sql.zip'))"

# 方式二：用镜像内的 mysql 客户端导入（web 容器内置客户端，
#         且容器内已注入 DB_HOST/DB_USER/DB_PASSWORD/DB_NAME）
unzip -p backup_YYYYmmdd_HHMMSS.sql.zip | \
  docker compose -f docker-compose.server.yml exec -T web \
  sh -c 'mysql -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME"'
```

注意事项：

- 两种方式都会**覆盖同名表**（`mysqldump` 输出默认带 `DROP TABLE IF EXISTS`），
  执行前先另存当前库的备份。
- 恢复同样受 `SUBPROCESS_TIMEOUT = 90` 秒限制，超时报
  「恢复超时（超过 90 秒）」。
- 备份/恢复依赖容器内的 MySQL 客户端；本地不用 Docker 跑时不具备该二进制。
- `backup.py` 的注释给出替代方案：数据量大导致超时，改用服务器 `crontab`
  直接执行 `mysqldump`。

---

## 升级与回滚

### 常规升级

```bash
cd /path/to/attendance-web
git pull                          # 或上传新的代码
vi .env                           # 如有新增变量（如 DB_ROOT_PASSWORD）先补齐
./deploy.sh                       # 重建镜像 + 重启 + 健康检查
docker compose -f docker-compose.server.yml logs --tail=100 web
```

升级要点：

- 生产**不挂载代码与模板**（`docker-compose.server.yml` 注释：刻意如此，
  「随镜像走，保证可回滚」），所以升级必须重建镜像，改文件不生效。
- 每个容器启动都会自动执行 `migrate --noinput`，**新迁移文件随镜像一起生效**。
- 静态文件在启动时 `collectstatic --clear` 后重新收集；新静态资源无需手工处理。

### 迁移注意事项

- 容器只执行 `migrate`（应用已生成的迁移文件），**不会生成迁移**。
  代码里若有模型改动而没有对应迁移文件，启动不会自动建表 → 升级前先确认。
- `makemigrations` 后**必须人工审阅**：
  确认生成的是 `CreateModel` / `AddField`，而不是 `RemoveField`。
- 审阅命令：

```bash
python manage.py makemigrations --dry-run --verbosity 3 --settings=config.settings_test
```

- 迁移前先做一次数据库备份（`/backups/` 页面或上文命令）。

### 回滚

仓库中没有版本化的镜像标签或回滚脚本，`docker-compose.server.yml` 固定使用
`image: attendance-web:latest`，因此回滚只能靠**旧镜像**：

```bash
# 升级前：给当前可用镜像打一个带日期的标签（建议纳入流程）
docker tag attendance-web:latest attendance-web:backup-YYYYmmdd

# 回滚：把旧镜像重新指回 latest，再用 --no-build 启动（避免重新构建覆盖）
docker tag attendance-web:backup-YYYYmmdd attendance-web:latest
./deploy.sh --no-build
```

> 上述 `docker tag` 流程是**操作建议**，仓库内没有对应的自动化脚本；
> 若服务器上已有其他镜像管理方式，以其为准。

回滚还要注意：数据库迁移**不会自动回退**。若升级引入了破坏性迁移
（`RemoveField` / 改列），回滚镜像后需同时恢复升级前的数据库备份。

---

## 已避开的坑

| 坑 | 本项目的做法 |
|---|---|
| `.dockerignore` 里 `*.sql` 不跨 `/`（Go `filepath.Match` 语义），匹配不到 `backups/` 下的备份 | 同时写 `*.sql` 与 `**/*.sql`（另排除 `**/*.sql.zip`、`**/*.zip`） |
| 排除 `**/*.xlsx` 会连带排除报表模板，导致容器内找不到 `考勤表模板.xlsx`、报表生成失败 | 文件末尾用否定规则 `!attendance/templates_xlsx/*.xlsx` 把模板放回来；且不排除 `attendance/` 父目录（父目录被排除则无法再包含子文件） |
| 排除整个 `backups/` 目录会让 Django app（`models.py`、`migrations/`）缺失 | 只排除备份文件类型，不排除目录本身 |
| `tools/`、`**/tests.py` 等测试脚本含真实姓名作为夹具，曾被打进镜像 | `.dockerignore` 排除 `tools/`、`**/tests.py`、`**/test*.py`、`**/*_tests.py`、`**/conftest.py`；`.gitignore` 亦排除 `*.xlsx` / `*.xls` |
| 排除模式写窄了不会报错，只会**静默**把文件打进镜像 —— `**/test_*.py` 匹配不到 `attendance/tests_report.py`（前缀是 `tests_`），该文件一直躺在镜像里 | 模式改为 `**/test*.py`；`DockerignoreCoverageTests` 断言仓库里每个测试/工具脚本都能被排除规则命中。**改完需重新 `docker compose build` 并复核 `docker exec attendance_web ls /app/<app>`** |
| Django 4.1 起 `TEMPLATES` 未显式配 `loaders` 会**无条件**缓存模板（与 `DEBUG` 无关），本地改模板必须重启进程 | `settings.py` 显式声明 loaders：`DEBUG=True` 用不带缓存的加载器（配合 `docker-compose.yml` 挂载 `./templates` 实现即时生效）；`settings_production.py` 在 `DEBUG=False` 时套 `cached.Loader`，生产模板随镜像走 |
| `subprocess.run` 的 `timeout` **仅在捕获输出（`stdout=PIPE`）时**才真正杀掉子进程 | `backups/backup.py` 用 `stdout=PIPE` 捕获后自行写文件，超时能删除半截文件并返回明确错误 |
| 备份子进程超时会先于 Gunicorn 超时 | `SUBPROCESS_TIMEOUT = 90` 秒 < Gunicorn `timeout = 120` 秒，`gunicorn.conf.py` 注释明确要求保持这个大小顺序 |
| `showmigrations \| head -20` 提前关闭管道，Django 继续写 stdout 抛 `BrokenPipeError`（启动日志出现长 traceback） | entrypoint 改用 `showmigrations 2>/dev/null \| sed -n '1,20p'`，不提前关闭管道 |
| 备份页面读的目录与挂载点不一致，历史备份永远列不出来 | 备份目录优先读环境变量 `BACKUP_DIR`，生产固定 `/app/backup_files`，与 compose 挂载点对齐 |
| 备份目录不存在时 `os.listdir` 直接抛 `FileNotFoundError` | `cleanup_old_backups()` 先 `os.path.isdir` 判断，返回「备份目录不存在，无需清理」 |
| `USE_TZ=True` 下 naive 与 aware datetime 比较抛异常 | `get_backup_stats()` 先用 `timezone.make_aware` 本地化再比较 |
| 查询结果全量内联进 HTML（实测 3000 条命中 = 5MB HTML） | `QUERY_MAX_RESULTS=2000` 硬上限，超限只提示不渲染；`QUERY_PAGE_SIZE=100` |
| `MAX_FILE_SIZE` 环境变量调大后上传上限不生效（喂给了一个无人引用的常量） | `IMPORT_MAX_FILE_SIZE` 直接读 `MAX_FILE_SIZE`；死配置 `MAX_UPLOAD_SIZE` 已删除，并有测试禁止它回来 |
| 导入行数上限若在"清空账期"之后再判定，超限文件会先删光当月数据再报错 | 行数上限在 `purge` **之前**判定并直接拒绝；`ImportLimitTests.test_row_cap_is_checked_before_purge` 守住顺序 |
| 生产挂代码导致无法回滚 | `docker-compose.server.yml` 刻意不挂 `templates` 与代码，只挂 `logs`、`staticfiles`、`media`、`backup_files` |
| 前端资源走公网 CDN，内网环境直接掉样式与图标 | Bootstrap 与图标随仓库分发到 `static/vendor/`，模板用 `{% static %}` 引用；`OfflineAssetsTests` 断言模板里不出现任何 http(s) 外链 |
| `bootstrap.min.css` 末尾的 `sourceMappingURL` 指向未分发的 `.map`，生产 `CompressedManifestStaticFilesStorage` 后处理时抛 `MissingFileError`，**整个 collectstatic 失败**（开发模式看不出来） | 用 `tools/_strip_sourcemap.py` 去掉该注释；`OfflineAssetsTests.test_vendored_assets_have_no_dangling_sourcemap` 守住 |
| 只下图标 CSS、忘下 `fonts/` 目录，页面不报错但图标静默变空白方块 | 资源随仓库分发，测试按 CSS 里实际的 `url()` 逐个核对字体文件存在 |
| 在同一卷里先用 `DEBUG=True` 收过静态文件、再切 `DEBUG=False`，`{% static %}` 会抛 `Missing staticfiles manifest entry` | 两种模式各自把静态文件收全：生产启动时 entrypoint 会用生产配置重跑 `collectstatic`；本地切模式后手工跑一次 `collectstatic` 即可 |
