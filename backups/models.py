from django.db import models
from django.contrib.auth.models import User


class BackupRecord(models.Model):
    """数据库备份记录"""

    STATUS_CHOICES = [
        ('success', '成功'),
        ('failed', '失败'),
    ]

    file_path = models.CharField('备份文件路径', max_length=500)
    file_size = models.BigIntegerField('文件大小', default=0)
    status = models.CharField(
        '状态', max_length=20,
        choices=STATUS_CHOICES, default='success'
    )
    error_message = models.TextField('失败原因', blank=True, null=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='backup_records', verbose_name='操作人'
    )
    created_at = models.DateTimeField('备份时间', auto_now_add=True)

    class Meta:
        db_table = 'backup_records'
        verbose_name = '备份记录'
        verbose_name_plural = '备份记录'
        ordering = ['-created_at']

    def __str__(self):
        return f"Backup {self.created_at:%Y-%m-%d %H:%M} ({self.get_status_display()})"
