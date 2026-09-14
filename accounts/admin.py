from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import UserProfile, UserPermission, AuditLog


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    fk_name = 'user'
    can_delete = False
    verbose_name_plural = '角色'
    fields = ['role']


class UserAdmin(BaseUserAdmin):
    inlines = [UserProfileInline]
    list_display = ['username', 'get_role', 'is_active', 'is_superuser', 'date_joined']
    list_filter = ['is_active', 'is_superuser']

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('权限', {'fields': ('is_active', 'is_superuser', 'is_staff')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'password1', 'password2'),
        }),
    )

    def get_role(self, obj):
        if obj.is_superuser:
            return '超级管理员'
        if hasattr(obj, 'profile'):
            return obj.profile.get_role_display()
        return '-'
    get_role.short_description = '角色'


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'role', 'created_at']
    list_filter = ['role']
    search_fields = ['user__username']


@admin.register(UserPermission)
class UserPermissionAdmin(admin.ModelAdmin):
    list_display = ['user', 'permission_code', 'created_by', 'created_at']
    list_filter = ['permission_code']
    search_fields = ['user__username']


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'username', 'action', 'module', 'ip_address']
    list_filter = ['action', 'module']
    search_fields = ['username', 'description']
    readonly_fields = ('user', 'username', 'action', 'module', 'target_table',
                       'target_id', 'description', 'ip_address', 'user_agent', 'created_at')
