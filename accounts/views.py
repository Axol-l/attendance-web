from datetime import datetime, time

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import models
from django.http import JsonResponse
from django.core.paginator import Paginator
from django.utils.http import urlencode, url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import UserProfile, UserPermission, AuditLog
from .decorators import admin_permission_required
from .services import log_action

PERMISSION_CODES = [
    ('attendance.upload', '上传考勤/请假数据'),
    ('attendance.query', '查询考勤数据'),
    ('attendance.export', '导出查询结果'),
    ('attendance.report', '生成并下载考勤报表'),
    ('attendance.rule_manage', '修改考勤规则'),
]

ACTION_LABELS = {
    'LOGIN': '登录',
    'LOGIN_FAILED': '登录失败',
    'LOGOUT': '登出',
    'PASSWORD_CHANGE': '修改密码',
    'USER_CREATE': '创建用户',
    'USER_UPDATE': '修改用户',
    'USER_DELETE': '删除用户',
    'UPLOAD': '上传文件',
    'UPLOAD_FAILED': '上传失败',
    'DATA_DELETE': '删除考勤数据',
    'EXPORT': '导出查询结果',
    'ATTENDANCE_FILE_DELETE': '删除导入记录',
    'ATTENDANCE_LEAVE_LIST_VIEW': '查看请假记录',
    'ATTENDANCE_SUMMARY_VIEW': '查看月度汇总',
    'ATTENDANCE_SUMMARY_COMPARE': '跨月对比',
    'ATTENDANCE_REPORT_GENERATE': '生成考勤报表',
    'ATTENDANCE_RULE_UPDATE': '修改考勤规则',
    'BACKUP_CREATE': '创建备份',
    'BACKUP_DELETE': '删除备份',
    'BACKUP_RESTORE': '还原备份',
}

MODULE_LABELS = {
    'auth': '认证',
    'accounts': '用户',
    'core': '基础',
    'attendance': '考勤',
    'backups': '备份',
}


def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            log_action(request, 'LOGIN', 'auth', '用户登录')

            # 登录后跳转：校验 next 是否为本站地址，防止开放重定向（钓鱼）。
            # 例：/accounts/login/?next=https://evil.com/fake-login
            # 若不校验，用户从真实系统被跳到攻击者站点，钓鱼可信度极高。
            next_url = request.GET.get('next') or request.POST.get('next')
            if next_url and url_has_allowed_host_and_scheme(
                url=next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect('home')
        else:
            messages.error(request, '用户名或密码错误')
            log_action(request, 'LOGIN_FAILED', 'auth', f'登录失败：用户名 {username}')

    return render(request, 'accounts/login.html')


@login_required
def logout_view(request):
    log_action(request, 'LOGOUT', 'auth', '用户登出')
    logout(request)
    return redirect('accounts:login')


@login_required
@admin_permission_required
def user_list(request):
    users = User.objects.select_related('profile').order_by('-date_joined')
    context = {
        'users': users,
        'permission_codes': PERMISSION_CODES,
    }
    return render(request, 'accounts/user_list.html', context)


@login_required
@admin_permission_required
@require_http_methods(["POST"])
def user_create(request):
    username = request.POST.get('username')
    password = request.POST.get('password')
    role = request.POST.get('role', 'user')

    if not username or not password:
        return JsonResponse({'success': False, 'message': '用户名和密码不能为空'})

    if User.objects.filter(username=username).exists():
        return JsonResponse({'success': False, 'message': '用户名已存在'})

    if role == 'super_admin' and not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': '只有超级管理员才能创建超级管理员'})

    try:
        user = User.objects.create_user(username=username, password=password)

        if role == 'super_admin':
            user.is_superuser = True
            user.is_staff = True
            user.save()

        UserProfile.objects.update_or_create(
            user=user,
            defaults={'role': role, 'created_by': request.user}
        )

        if role == 'user':
            perms = request.POST.getlist('permissions')
            for code in perms:
                UserPermission.objects.get_or_create(
                    user=user, permission_code=code,
                    defaults={'created_by': request.user}
                )

        log_action(request, 'USER_CREATE', 'accounts',
                   f'创建用户 {username}，角色：{role}', 'auth_user', user.id)
        return JsonResponse({'success': True, 'message': '用户创建成功'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)})


@login_required
@admin_permission_required
@require_http_methods(["POST"])
def user_update(request, pk):
    user = get_object_or_404(User, pk=pk)

    if user.is_superuser and not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': '无法编辑超级管理员'})

    role = request.POST.get('role')
    password = request.POST.get('password')
    is_active = request.POST.get('is_active')

    try:
        if role:
            if role == 'super_admin' and not request.user.is_superuser:
                return JsonResponse({'success': False, 'message': '只有超级管理员才能设置此角色'})

            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = role
            profile.save()

            user.is_superuser = (role == 'super_admin')
            user.is_staff = (role == 'super_admin')

        if password:
            user.set_password(password)

        if is_active is not None:
            user.is_active = (is_active == 'true')

        user.save()

        if role == 'user':
            UserPermission.objects.filter(user=user).delete()
            perms = request.POST.getlist('permissions')
            for code in perms:
                UserPermission.objects.create(
                    user=user, permission_code=code, created_by=request.user
                )
        elif role in ('admin', 'super_admin'):
            UserPermission.objects.filter(user=user).delete()

        log_action(request, 'USER_UPDATE', 'accounts',
                   f'修改用户 {user.username}', 'auth_user', pk)
        return JsonResponse({'success': True, 'message': '更新成功'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)})


@login_required
@admin_permission_required
@require_http_methods(["POST"])
def user_delete(request, pk):
    user = get_object_or_404(User, pk=pk)

    if user == request.user:
        return JsonResponse({'success': False, 'message': '不能删除自己'})
    if user.is_superuser and not request.user.is_superuser:
        return JsonResponse({'success': False, 'message': '无法删除超级管理员'})

    username = user.username
    try:
        user.delete()
    except models.ProtectedError:
        return JsonResponse({'success': False, 'message': f'用户 {username} 有关联的考勤数据，无法删除。请先删除该用户上传的文件。'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'删除失败：{e}'})

    log_action(request, 'USER_DELETE', 'accounts',
               f'删除用户 {username}', 'auth_user', pk)
    return JsonResponse({'success': True, 'message': '删除成功'})


@login_required
@require_http_methods(["POST"])
def change_password(request):
    old_password = request.POST.get('old_password')
    new_password = request.POST.get('new_password')

    if not request.user.check_password(old_password):
        return JsonResponse({'success': False, 'message': '原密码错误'})

    request.user.set_password(new_password)
    request.user.save()
    log_action(request, 'PASSWORD_CHANGE', 'accounts',
               f'用户 {request.user.username} 修改密码')
    return JsonResponse({'success': True, 'message': '密码修改成功，请重新登录'})


@login_required
@admin_permission_required
def audit_log_list(request):
    """操作日志列表（仅管理员可见）"""
    queryset = AuditLog.objects.select_related('user')

    username = request.GET.get('username', '').strip()
    user_id = request.GET.get('user_id', '').strip()
    action = request.GET.get('action', '').strip()
    module = request.GET.get('module', '').strip()
    keyword = request.GET.get('keyword', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    if user_id:
        queryset = queryset.filter(user_id=user_id)
    if username:
        queryset = queryset.filter(username__icontains=username)
    if action:
        queryset = queryset.filter(action=action)
    if module:
        queryset = queryset.filter(module=module)
    if keyword:
        queryset = queryset.filter(description__icontains=keyword)

    tz = timezone.get_current_timezone()
    if date_from:
        try:
            d = datetime.strptime(date_from, '%Y-%m-%d').date()
            start = timezone.make_aware(datetime.combine(d, time.min), tz)
            queryset = queryset.filter(created_at__gte=start)
        except ValueError:
            date_from = ''
    if date_to:
        try:
            d = datetime.strptime(date_to, '%Y-%m-%d').date()
            end = timezone.make_aware(datetime.combine(d, time.max), tz)
            queryset = queryset.filter(created_at__lte=end)
        except ValueError:
            date_to = ''

    paginator = Paginator(queryset, 50)
    page = paginator.get_page(request.GET.get('page', 1))

    for log in page.object_list:
        log.action_label = ACTION_LABELS.get(log.action, log.action)
        log.module_label = MODULE_LABELS.get(log.module, log.module or '-')

    actor_ids = (
        AuditLog.objects.exclude(user__isnull=True)
        .order_by()
        .values_list('user_id', flat=True)
        .distinct()
    )
    actors = User.objects.filter(id__in=list(actor_ids)).order_by('username')

    action_choices = [
        (code, ACTION_LABELS.get(code, code))
        for code in sorted(
            AuditLog.objects.order_by().values_list('action', flat=True).distinct()
        )
        if code
    ]
    module_choices = [
        (code, MODULE_LABELS.get(code, code))
        for code in sorted(
            AuditLog.objects.order_by().values_list('module', flat=True).distinct()
        )
        if code
    ]

    filters = {
        'username': username,
        'user_id': user_id,
        'action': action,
        'module': module,
        'keyword': keyword,
        'date_from': date_from,
        'date_to': date_to,
    }
    active_params = {k: v for k, v in filters.items() if v}
    query_string = urlencode(active_params)

    actor_map = {str(u.id): u.username for u in actors}
    action_map = dict(action_choices)
    module_map = dict(module_choices)

    chip_defs = []
    if username:
        chip_defs.append(('username', '用户名包含', username))
    if user_id:
        chip_defs.append(('user_id', '用户', actor_map.get(user_id, user_id)))
    if action:
        chip_defs.append(('action', '操作', action_map.get(action, action)))
    if module:
        chip_defs.append(('module', '模块', module_map.get(module, module)))
    if keyword:
        chip_defs.append(('keyword', '描述包含', keyword))
    if date_from:
        chip_defs.append(('date_from', '起始', date_from))
    if date_to:
        chip_defs.append(('date_to', '截止', date_to))

    active_chips = []
    for key, label, value in chip_defs:
        remaining = {k: v for k, v in active_params.items() if k != key}
        active_chips.append({
            'key': key,
            'label': label,
            'value': value,
            'remove_qs': urlencode(remaining),
        })

    advanced_open = bool(
        user_id or action or module or keyword or date_from or date_to
    )

    context = {
        'logs': page,
        'actors': actors,
        'action_choices': action_choices,
        'module_choices': module_choices,
        'filters': filters,
        'active_chips': active_chips,
        'query_string': query_string,
        'advanced_open': advanced_open,
    }
    return render(request, 'accounts/audit_log_list.html', context)


@login_required
@admin_permission_required
def audit_log_detail(request, pk):
    """操作日志详情 JSON（仅管理员可见）"""
    log = get_object_or_404(AuditLog, pk=pk)
    return JsonResponse({
        'success': True,
        'data': {
            'id': log.id,
            'created_at': timezone.localtime(log.created_at).strftime('%Y-%m-%d %H:%M:%S'),
            'username': log.username,
            'user_id': log.user_id,
            'action': log.action,
            'action_label': ACTION_LABELS.get(log.action, log.action),
            'module': log.module,
            'module_label': MODULE_LABELS.get(log.module, log.module or '-'),
            'target_table': log.target_table or '',
            'target_id': log.target_id or '',
            'description': log.description or '',
            'ip_address': log.ip_address or '',
            'user_agent': log.user_agent or '',
        },
    })
