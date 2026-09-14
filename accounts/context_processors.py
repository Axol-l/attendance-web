"""
模板上下文处理器 —— 将当前用户权限注入所有模板上下文

用法（模板中）：
    {% if user_perms.attendance_upload %}...{% endif %}
    {% if user_perms.is_admin %}...{% endif %}

⚠️ 新增权限码时，必须同时改三个地方，否则会出现"前端按钮消失但后端仍放行"
   （或反之）的不一致：
    1. 本文件的 PERMISSION_TEMPLATE_KEYS 映射
    2. accounts/views.py 的 PERMISSION_CODES（用户管理页的勾选框）
    3. attendance/views.py 的 @permission_required 装饰器
"""

# 模板变量名（下划线） ←→ 权限码（点号）
PERMISSION_TEMPLATE_KEYS = {
    'attendance_upload': 'attendance.upload',
    'attendance_query': 'attendance.query',
    'attendance_export': 'attendance.export',
    'attendance_report': 'attendance.report',
    'attendance_rule_manage': 'attendance.rule_manage',
}


def user_permissions(request):
    user = request.user
    if not user.is_authenticated:
        return {'user_perms': {}}

    profile = getattr(user, 'profile', None)
    is_admin = user.is_superuser or bool(profile and profile.is_admin)

    if is_admin:
        perms = {key: True for key in PERMISSION_TEMPLATE_KEYS}
        perms['is_admin'] = True
        return {'user_perms': perms}

    from .models import UserPermission
    codes = set(
        UserPermission.objects.filter(user=user)
        .values_list('permission_code', flat=True)
    )

    perms = {key: (code in codes) for key, code in PERMISSION_TEMPLATE_KEYS.items()}
    perms['is_admin'] = False
    return {'user_perms': perms}
