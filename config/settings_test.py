"""
测试用配置 —— 用 SQLite 跑单元测试，无需本地 MySQL

用法：
    python manage.py test --settings=config.settings_test
    python manage.py migrate --settings=config.settings_test

⚠️ 只用于本地测试与自检。生产与开发一律用 MySQL（见 settings.py），
   因为报表/汇总依赖 MySQL 的 STRICT_TRANS_TABLES 与 utf8mb4 行为。
"""
from .settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# 测试时不要写文件日志，避免污染 logs/
# 注意：必须整体替换 handler 定义 —— 只改 class 会把 filename 一起传给
# logging.NullHandler，它不接受该参数（Handler.__init__() got an unexpected
# keyword argument 'filename'）。
LOGGING['handlers']['file'] = {'class': 'logging.NullHandler'}
LOGGING['handlers']['error_file'] = {'class': 'logging.NullHandler'}

# 测试里用临时目录存上传文件
import tempfile  # noqa: E402

MEDIA_ROOT = tempfile.mkdtemp(prefix='attendance-test-media-')

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
