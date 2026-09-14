from django.contrib import admin

from .models import AttendanceDaily, AttendanceRule, LeaveRecord, UploadedFile


@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display = ('original_filename', 'file_kind', 'period', 'record_count',
                    'status', 'uploaded_by', 'uploaded_at')
    list_filter = ('file_kind', 'status', 'period')
    search_fields = ('original_filename', 'file_hash')
    readonly_fields = ('file_hash', 'uploaded_at')


@admin.register(AttendanceDaily)
class AttendanceDailyAdmin(admin.ModelAdmin):
    list_display = ('work_date', 'name', 'department', 'shift', 'attend_days',
                    'work_minutes', 'late_minutes', 'source_file')
    list_filter = ('work_date', 'department')
    search_fields = ('name', 'user_id', 'employee_no')
    date_hierarchy = 'work_date'


@admin.register(LeaveRecord)
class LeaveRecordAdmin(admin.ModelAdmin):
    list_display = ('applicant_name', 'leave_type', 'start_time', 'duration_hours',
                    'duration_days', 'is_approved', 'is_resigned', 'period')
    list_filter = ('leave_type', 'is_approved', 'is_resigned', 'period')
    search_fields = ('applicant_name', 'applicant_name_raw', 'approval_no')
    date_hierarchy = 'start_time'


@admin.register(AttendanceRule)
class AttendanceRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'standard_work_minutes', 'monthly_standard_days',
                    'exclude_if_no_data', 'clamp_negative_overtime',
                    'exclude_rejected_leave', 'is_active', 'updated_at')
    list_filter = ('is_active',)
