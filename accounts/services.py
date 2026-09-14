import logging

from .models import AuditLog

logger = logging.getLogger('accounts')


def get_client_ip(request):
    """
    从 request 中提取客户端 IP，支持反向代理（X-Forwarded-For）。
    """
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def log_action(request, action, module, description,
               target_table=None, target_id=None):
    """
    统一审计日志记录函数。

    Args:
        request: Django HttpRequest
        action: 操作类型（LOGIN, UPLOAD, DATA_DELETE 等）
        module: 功能模块（auth, shipments, accounts, core, backups）
        description: 操作描述
        target_table: 操作的表名（可选）
        target_id: 操作的记录 ID（可选）
    """
    try:
        AuditLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            username=getattr(request.user, 'username', 'anonymous'),
            action=action,
            module=module,
            target_table=target_table,
            target_id=str(target_id) if target_id else None,
            description=description,
            ip_address=get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
        )
    except Exception:
        logger.exception('写入审计日志失败: action=%s, module=%s', action, module)
