from django.contrib import admin

from .models import BackupRecord


@admin.register(BackupRecord)
class BackupRecordAdmin(admin.ModelAdmin):
    list_display = ('id', 'file_path', 'file_size', 'status', 'created_by', 'created_at')
    list_filter = ('status',)
    readonly_fields = ('created_at',)
