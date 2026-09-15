"""
考勤模块 Web 化 —— Django 配置

工程约定（沿用生产数据系统，见项目计划书第 2 节）：
  - Django 4.1.13 + Python 3.10
  - MySQL 8.0 + PyMySQL
  - USE_TZ=True / Asia/Shanghai
  - 函数视图 + 装饰器链，禁止 CBV
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# 加载环境变量
load_dotenv(BASE_DIR / '.env')


# ==================== 基础配置 ====================

SECRET_KEY = os.getenv(
    'SECRET_KEY',
    'django-insecure-attendance-dev-key-change-me-in-production',
)

DEBUG = os.getenv('DEBUG', 'True').lower() in ('true', '1', 'yes')

# 模板加载器是否缓存（Django 4.1 起的行为补偿）
#
# 背景：Django 4.1 起，只要 TEMPLATES 里没有显式配置 loaders，引擎就会
# **无条件**套上 cached.Loader（见 django/template/engine.py 的 Engine.__init__），
# 这与 DEBUG 无关 —— DEBUG 只影响报错信息，不影响模板缓存。
# 结果是：本地开发改了模板必须重启进程才生效，"改完刷新浏览器"不成立。
#
# 因此这里显式声明 loaders：DEBUG=True 用不带缓存的加载器（本地热重载），
# DEBUG=False 沿用 cached.Loader（生产模板随镜像走，缓存无副作用）。
#
# 注意：显式给出 loaders 时 Django 强制要求 APP_DIRS 必须为 False，
# 所以下面把 app_directories.Loader 显式写进了 loaders 列表。
# settings_production.py 里 `pop('APP_DIRS', None)` 带默认值，不受影响。
TEMPLATE_LOADERS_CACHED = not DEBUG

ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')


# ==================== 应用 ====================

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # 自定义应用
    'core',
    'accounts',
    'attendance',
    'backups',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise 必须紧跟 SecurityMiddleware。
    # 作用：生产（DEBUG=False）下由应用自己服务静态文件，
    # 无需 Nginx 额外配 static location —— 内网单容器部署时少一个出错点。
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

# ── 模板加载器列表 ──
# 注意：显式给出 loaders 时，Django 强制要求 APP_DIRS 必须为 False
# （否则抛 ImproperlyConfigured: app_dirs must not be set when loaders is defined），
# 因此 app 级模板目录改为在列表里显式声明 app_directories.Loader。
_TEMPLATE_LOADER_CHAIN = [
    'django.template.loaders.filesystem.Loader',
    'django.template.loaders.app_directories.Loader',
]

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': False,
        'OPTIONS': {
            # DEBUG=True  → 不带缓存的加载器，改模板刷新浏览器即生效
            # DEBUG=False → 套 cached.Loader，避免每次请求读磁盘
            'loaders': (
                [('django.template.loaders.cached.Loader', _TEMPLATE_LOADER_CHAIN)]
                if TEMPLATE_LOADERS_CACHED
                else _TEMPLATE_LOADER_CHAIN
            ),
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounts.context_processors.user_permissions',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# ==================== 数据库 ====================

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('DB_NAME', 'attendance_db'),
        'USER': os.getenv('DB_USER', 'att_admin'),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', 'localhost'),
        # 端口刻意与生产数据系统（3308）错开，便于同机共存
        'PORT': os.getenv('DB_PORT', '3309'),
        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        }
    }
}


# ==================== 密码校验 ====================

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# ==================== 国际化 ====================

LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True


# ==================== 静态与媒体文件 ====================

STATIC_URL = os.getenv('STATIC_URL', '/static/')
STATIC_ROOT = BASE_DIR / 'staticfiles'

# 项目自带的前端资源（Bootstrap 与 Bootstrap Icons）。
# ⚠️ 刻意**不用 CDN**：本系统可能部署在无外网的内网环境，
#    页面依赖公网 CDN 会直接掉样式与图标。
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = os.getenv('MEDIA_URL', '/media/')
MEDIA_ROOT = BASE_DIR / 'media'

# 路径前缀（Nginx 反向代理多项目共存时使用，本项目部署前缀 /attendance/）
FORCE_SCRIPT_NAME = os.getenv('FORCE_SCRIPT_NAME', None) or None

# CSRF 信任源
_csrf_origins = os.getenv('CSRF_TRUSTED_ORIGINS', '')
if _csrf_origins:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(',') if o.strip()]

LOGIN_URL = '/accounts/login/'

# 文件上传限制
ALLOWED_EXTENSIONS = os.getenv('ALLOWED_EXTENSIONS', 'xlsx').split(',')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ==================== 业务配置常量 ====================

# 数据导入
IMPORT_BATCH_SIZE = 500

# ⚠️ 上限必须读环境变量：这里此前写死 50MB，而环境变量喂给了另一个**没人引用**的
#    MAX_UPLOAD_SIZE —— 运维把 MAX_FILE_SIZE 调大后实际上限纹丝不动，
#    且没有任何报错。两者已合并为下面这一项。
IMPORT_MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 50 * 1024 * 1024))   # 默认 50MB

# 单次导入最大行数。真实钉钉月表约 2.7k 行，10 万行留足余量；
# 目的是挡住误传的超大文件，避免把整表读进内存后再失败。
IMPORT_MAX_ROWS = int(os.getenv('IMPORT_MAX_ROWS', 100000))

# 查询上限 —— ⚠️ 生产数据系统实测 3000 条命中 = 5MB HTML，10 万条约 170MB。
# 本项目从第一天就设硬上限，超限时页面提示"结果过多，请缩小范围"。
QUERY_MAX_RESULTS = 2000
QUERY_PAGE_SIZE = 100

# 备份
BACKUP_RETENTION_DAYS = 30
REMOTE_BACKUP_DIR = None

# 钉钉字段映射与考勤规则的默认值不在 settings 里 —— 规则口径的唯一来源是
# `attendance/mapping.py`（默认值）与 `AttendanceRule` 模型字段（真正生效的规则行）。
# 此前这里另有一份 DEFAULT_STANDARD_WORK_MINUTES / DEFAULT_MONTHLY_STANDARD_DAYS，
# 没有任何代码引用，只是给"改了这里就会生效"的错觉，已删除。


# ==================== 日志 ====================

LOGS_DIR = BASE_DIR / 'logs'
if not LOGS_DIR.exists():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'filters': {
        'require_debug_false': {'class': 'django.utils.log.RequireDebugFalse'},
        'require_debug_true': {'class': 'django.utils.log.RequireDebugTrue'},
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
            'filters': ['require_debug_true'],
        },
        'file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'app.log',
            'maxBytes': 10485760,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'error_file': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'error.log',
            'maxBytes': 10485760,
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['error_file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'core': {
            'handlers': ['console', 'file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'accounts': {
            'handlers': ['console', 'file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'attendance': {
            'handlers': ['console', 'file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'backups': {
            'handlers': ['console', 'file', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
}
