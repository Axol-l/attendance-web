from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class UserProfile(models.Model):
    """用户扩展信息：角色"""

    ROLE_CHOICES = [
        ('super_admin', '超级管理员'),
        ('admin', '管理员'),
        ('user', '普通用户'),
    ]

    user = models.OneToOneField(
        User, on_delete=models.CASCADE,
        related_name='profile', verbose_name='用户'
    )
    role = models.CharField(
        '角色', max_length=20,
        choices=ROLE_CHOICES, default='user', db_index=True
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='created_profiles', verbose_name='创建人'
    )
    created_at = models.DateTimeField('创建时间', default=timezone.now)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'user_profiles'
        verbose_name = '用户角色'
        verbose_name_plural = '用户角色'

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    @property
    def is_super_admin(self):
        return self.role == 'super_admin' or self.user.is_superuser

    @property
    def is_admin(self):
        return self.role in ('super_admin', 'admin') or self.user.is_superuser

    @property
    def can_access_admin(self):
        return self.is_super_admin


class UserPermission(models.Model):
    """细粒度权限表 — 仅 user 角色需要查此表"""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='custom_permissions', verbose_name='用户'
    )
    permission_code = models.CharField('权限编码', max_length=50)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='granted_permissions', verbose_name='授权人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'user_permissions'
        verbose_name = '用户权限'
        verbose_name_plural = '用户权限'
        unique_together = ('user', 'permission_code')

    def __str__(self):
        return f"{self.user.username} - {self.permission_code}"


class AuditLog(models.Model):
    """审计日志"""

    user = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='audit_logs', verbose_name='操作人'
    )
    username = models.CharField('用户名', max_length=150)
    action = models.CharField('操作类型', max_length=50, db_index=True)
    module = models.CharField('功能模块', max_length=50, blank=True, db_index=True)
    target_table = models.CharField('操作表', max_length=50, blank=True, null=True)
    target_id = models.CharField('记录ID', max_length=50, blank=True, null=True)
    description = models.TextField('操作描述', blank=True, null=True)
    ip_address = models.GenericIPAddressField('IP地址', blank=True, null=True)
    user_agent = models.CharField('浏览器信息', max_length=500, blank=True, null=True)
    created_at = models.DateTimeField('操作时间', auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'audit_logs'
        verbose_name = '审计日志'
        verbose_name_plural = '审计日志'
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {self.username} - {self.action}"
