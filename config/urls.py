"""
config 包 URL 配置

URL 命名空间：
  /health/           健康检查（无认证，供 Nginx / 监控探活）
  /                  首页
  /accounts/         用户、权限、审计日志
  /attendance/       考勤模块（上传、查询、汇总、导出、规则）
  /backups/          数据库备份
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from core.views import home, health_check

urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', health_check, name='health_check'),
    path('', home, name='home'),
    path('accounts/', include('accounts.urls')),
    path('attendance/', include('attendance.urls')),
    path('backups/', include('backups.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
