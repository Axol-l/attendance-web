"""
生产环境配置

通过环境变量 DJANGO_SETTINGS_MODULE=config.settings_production 加载。
继承 settings.py 的所有配置，覆盖生产环境需要的部分。
"""

from .settings import *  # noqa: F401,F403
import os

# ==================== 安全配置 ====================

DEBUG = os.getenv('DEBUG', 'False').lower() in ('true', '1', 'yes')

if not DEBUG:
    # Cookie 安全（HTTPS 启用后再开 Secure）
    SESSION_COOKIE_SECURE = os.getenv('SECURE_COOKIES', 'False').lower() in ('true', '1')
    CSRF_COOKIE_SECURE = os.getenv('SECURE_COOKIES', 'False').lower() in ('true', '1')
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = False  # 必须为 False，前端 JS 通过 document.cookie 读取 CSRF token

    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'


# ==================== 数据库优化 ====================

DATABASES['default']['CONN_MAX_AGE'] = 600
DATABASES['default']['CONN_HEALTH_CHECKS'] = True

DATABASES['default']['OPTIONS'].update({
    'connect_timeout': 10,
    'read_timeout': 30,
    'write_timeout': 30,
})


# ==================== 缓存 ====================

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'attendance-cache',
        'TIMEOUT': 300,
        'OPTIONS': {'MAX_ENTRIES': 1000},
    }
}

SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'
SESSION_CACHE_ALIAS = 'default'


# ==================== 静态文件 ====================

STATICFILES_FINDERS = [
    'django.contrib.staticfiles.finders.FileSystemFinder',
    'django.contrib.staticfiles.finders.AppDirectoriesFinder',
]

if not DEBUG:
    STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'


# ==================== 日志 ====================

if not DEBUG:
    LOGGING['handlers']['console']['filters'] = []
    LOGGING['handlers']['console']['level'] = 'INFO'

    LOGGING['handlers']['performance'] = {
        'level': 'INFO',
        'class': 'logging.handlers.RotatingFileHandler',
        'filename': os.path.join(LOGS_DIR, 'performance.log'),
        'maxBytes': 10485760,
        'backupCount': 5,
        'formatter': 'verbose',
    }

    LOGGING['loggers']['django.db.backends'] = {
        'handlers': ['performance'],
        'level': 'DEBUG' if os.getenv('SQL_DEBUG') == 'True' else 'INFO',
        'propagate': False,
    }


# ==================== 中间件 ====================

if not DEBUG:
    if 'django.middleware.security.SecurityMiddleware' not in MIDDLEWARE:
        MIDDLEWARE.insert(0, 'django.middleware.security.SecurityMiddleware')


# ==================== 模板 ====================

if not DEBUG:
    for template_engine in TEMPLATES:
        template_engine['OPTIONS']['debug'] = False
        template_engine.pop('APP_DIRS', None)
        template_engine['OPTIONS']['loaders'] = [
            ('django.template.loaders.cached.Loader', [
                'django.template.loaders.filesystem.Loader',
                'django.template.loaders.app_directories.Loader',
            ]),
        ]


# ==================== 邮件（错误通知） ====================

if not DEBUG:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.example.com')
    EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
    EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True').lower() in ('true', '1', 'yes')
    EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
    DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@example.com')

    ADMINS = [('Admin', os.getenv('ADMIN_EMAIL', 'admin@example.com'))]
    MANAGERS = ADMINS


# ==================== 上传/性能 ====================

DATA_UPLOAD_MAX_MEMORY_SIZE = 10485760
FILE_UPLOAD_MAX_MEMORY_SIZE = 10485760
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000

IMPORT_BATCH_SIZE = int(os.getenv('IMPORT_BATCH_SIZE', '500'))
QUERY_MAX_RESULTS = int(os.getenv('QUERY_MAX_RESULTS', '2000'))
QUERY_PAGE_SIZE = int(os.getenv('QUERY_PAGE_SIZE', '100'))


# ==================== 监控 ====================

INTERNAL_IPS = ['127.0.0.1', 'localhost']


# ==================== 国际化 ====================

USE_TZ = True
TIME_ZONE = 'Asia/Shanghai'
LANGUAGE_CODE = 'zh-hans'


# ==================== 上传处理器 ====================

FILE_UPLOAD_HANDLERS = [
    'django.core.files.uploadhandler.MemoryFileUploadHandler',
    'django.core.files.uploadhandler.TemporaryFileUploadHandler',
]
