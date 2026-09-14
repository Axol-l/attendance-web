import os

from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, Http404
from django.views.decorators.http import require_http_methods
from django.core.paginator import Paginator

from accounts.decorators import admin_permission_required
from accounts.services import log_action
from .models import BackupRecord
from .backup import backup_service


@login_required
@admin_permission_required
def backup_list(request):
    """备份管理页面"""
    records = BackupRecord.objects.all()
    paginator = Paginator(records, 20)
    page_number = request.GET.get('page')
    page = paginator.get_page(page_number)
    context = {'backups': page}
    return render(request, 'backups/backup_list.html', context)


@login_required
@admin_permission_required
@require_http_methods(["POST"])
def create_backup(request):
    """创建备份"""
    success, message, filename = backup_service.create_backup()

    if success:
        record = BackupRecord.objects.create(
            file_path=os.path.join(backup_service.backup_dir, filename),
            file_size=os.path.getsize(os.path.join(backup_service.backup_dir, filename)),
            status='success',
            created_by=request.user,
        )
        log_action(request, 'BACKUP_CREATE', 'backups',
                   f'创建数据库备份 {filename}', 'backup_records', record.id)
        return JsonResponse({
            'success': True,
            'message': message,
            'backup_id': record.id,
        })

    record = BackupRecord.objects.create(
        file_path='',
        status='failed',
        error_message=message,
        created_by=request.user,
    )
    log_action(request, 'BACKUP_CREATE', 'backups',
               f'创建数据库备份失败：{message}', 'backup_records', record.id)
    return JsonResponse({'success': False, 'message': message})


@login_required
@admin_permission_required
def download_backup(request, pk):
    """下载备份文件"""
    record = get_object_or_404(BackupRecord, pk=pk)
    if not os.path.exists(record.file_path):
        raise Http404('备份文件不存在')

    filename = os.path.basename(record.file_path)
    resp = backup_service.download_backup(filename)
    if resp is None:
        raise Http404('备份文件不存在')
    return resp


@login_required
@admin_permission_required
@require_http_methods(["POST"])
def delete_backup(request, pk):
    """删除备份"""
    record = get_object_or_404(BackupRecord, pk=pk)
    filename = os.path.basename(record.file_path) if record.file_path else ''
    try:
        if filename:
            backup_service.delete_backup(filename)
        record.delete()
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'删除失败：{e}'})
    log_action(request, 'BACKUP_DELETE', 'backups',
               f'删除备份文件 {filename}', 'backup_records', pk)
    return JsonResponse({'success': True, 'message': '删除成功'})
