"""
core 应用 —— 健康检查与首页

本应用刻意保持极薄：不放业务模型。考勤相关的一切都在 attendance 应用。
"""
import logging

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render

logger = logging.getLogger('core')


def health_check(request):
    """
    健康检查 —— 供 Nginx / 监控探活。

    刻意不要求认证（探活方通常无会话），因此这里不返回任何业务数据，
    只返回进程存活与数据库连通性。
    """
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception:
        logger.exception('健康检查：数据库连接失败')
        db_ok = False

    payload = {'status': 'ok' if db_ok else 'degraded', 'database': db_ok}
    return JsonResponse(payload, status=200 if db_ok else 503)


@login_required
def home(request):
    """首页 —— 考勤数据概览"""
    from attendance.models import AttendanceDaily, LeaveRecord, UploadedFile

    context = {
        'daily_count': AttendanceDaily.objects.count(),
        'leave_count': LeaveRecord.objects.count(),
        'file_count': UploadedFile.objects.count(),
        'recent_files': UploadedFile.objects.order_by('-uploaded_at')[:5],
    }
    return render(request, 'home.html', context)
