"""
权限装饰器 — 适配 v2.0 权限模型

权限判断逻辑：
  super_admin / admin → 自动拥有所有权限
  user → 查 user_permissions 表
"""
from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages
from django.http import JsonResponse


def _get_profile(user):
    if not user.is_authenticated:
        return None
    return getattr(user, 'profile', None)


def is_admin(user):
    """super_admin / admin / Django superuser 均视为管理员"""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = _get_profile(user)
    return bool(profile and profile.is_admin)


def has_permission(user, permission_code):
    """检查用户是否拥有指定权限"""
    if not user.is_authenticated:
        return False
    if is_admin(user):
        return True
    from .models import UserPermission
    return UserPermission.objects.filter(
        user=user, permission_code=permission_code
    ).exists()


def deny(request):
    """根据请求类型返回拒绝响应"""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': False, 'message': '没有权限'})
    messages.error(request, '您没有权限访问此页面')
    return redirect('home')


_is_admin = is_admin
_has_permission = has_permission
_deny = deny


def permission_required(*permission_codes):
    """
    权限装饰器。

    用法：
        @permission_required('attendance.upload')
        @permission_required('attendance.upload', 'attendance.query')   # 满足其一即可

    ⚠️ 支持多个权限码是为了配合"按归属过滤"的场景：例如导入报告页，
       只授了 upload 的人应当能看**自己**的导入报告，有 query 的人能看全部。
       装饰器只能做粗粒度放行，细粒度判断放在视图里（见
       attendance/permissions.py 的 can_view_import）。
    """
    codes = tuple(permission_codes)

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('accounts:login')
            if _is_admin(request.user) or any(
                    _has_permission(request.user, c) for c in codes):
                return view_func(request, *args, **kwargs)
            return _deny(request)
        return wrapper
    return decorator


def admin_permission_required(view_func):
    """管理员权限装饰器"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('accounts:login')
        if _is_admin(request.user):
            return view_func(request, *args, **kwargs)
        return _deny(request)
    return wrapper
