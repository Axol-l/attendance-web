"""
考勤模块数据模型

设计决策（已与需求方确认，见项目计划书第 5 节与《第8节答复_勘查结论.md》）：
  1. 考勤数据用**宽表**（AttendanceDaily 与钉钉每日统计表字段一一对应），单公司固定。
  2. 唯一键用 (work_date, user_id)：实测「工号」列**全为空**，不可用；
     「姓名」有重名风险且带「（离职）」后缀会变化。UserId 是钉钉的稳定 ID。
  3. 请假 10 个子类**全建列**（不因当月没出现就不建），保证各月口径稳定。
  4. 所有"规则"（加班阈值、排除名单、颜色）抽到 AttendanceRule，不硬编码。
"""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from . import mapping


class UploadedFile(models.Model):
    """上传文件记录 —— 参照生产数据系统的结构"""

    FILE_KIND_CHOICES = [
        ('daily', '日考勤汇总表'),
        ('leave', '请假单据'),
    ]

    STATUS_CHOICES = [
        ('pending', '待处理'),
        ('processing', '处理中'),
        ('success', '成功'),
        ('partial', '部分成功'),
        ('failed', '失败'),
    ]

    original_filename = models.CharField('原始文件名', max_length=255)
    stored_filename = models.CharField('存储文件名', max_length=255)
    file_path = models.CharField('文件路径', max_length=500)
    file_size = models.BigIntegerField('文件大小', default=0)
    file_hash = models.CharField('文件SHA256', max_length=64, blank=True, null=True, db_index=True)

    file_kind = models.CharField('文件类型', max_length=20, choices=FILE_KIND_CHOICES, db_index=True)
    period = models.CharField('所属月份', max_length=7, blank=True, null=True, db_index=True,
                              help_text='格式 YYYY-MM，如 2014-08')

    record_count = models.IntegerField('导入记录数', default=0)
    skipped_count = models.IntegerField('跳过记录数', default=0)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    error_message = models.TextField('错误信息', blank=True, null=True)
    import_report = models.JSONField('导入报告', default=dict, blank=True,
                                     help_text='精确到 Sheet 名 + 行号 + 字段 + 值的错误明细')

    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='attendance_uploads',
                                    verbose_name='上传人')
    uploaded_at = models.DateTimeField('上传时间', auto_now_add=True)
    processed_at = models.DateTimeField('处理完成时间', blank=True, null=True)

    class Meta:
        db_table = 'attendance_uploaded_files'
        verbose_name = '上传文件'
        verbose_name_plural = '上传文件'
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['file_kind', 'period']),
        ]

    def __str__(self):
        return f"{self.get_file_kind_display()} {self.original_filename}"

    @property
    def file_size_display(self):
        size = float(self.file_size or 0)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024:
                return f'{size:.1f} {unit}'
            size /= 1024
        return f'{size:.1f} TB'


class AttendanceDaily(models.Model):
    """
    日考勤明细 —— 宽表，与钉钉「每日统计」表字段一一对应。

    唯一键是 (work_date, user_id)。注意 user_id 可能为空（钉钉偶尔不给），
    此时由 services 层用 (work_date, name) 兜底查重，不依赖数据库唯一约束。
    """

    # ── 唯一键（用于导入查重） ──
    work_date = models.DateField('日期', db_index=True)
    user_id = models.CharField('UserId', max_length=100, db_index=True,
                               help_text='钉钉稳定用户 ID，导入查重的主键')
    name = models.CharField('姓名', max_length=50, db_index=True)

    # ── 基础信息 ──
    attend_group = models.CharField('考勤组', max_length=100, blank=True, null=True)
    department = models.CharField('部门', max_length=200, blank=True, null=True, db_index=True)
    employee_no = models.CharField('工号', max_length=50, blank=True, null=True)
    position = models.CharField('职位', max_length=100, blank=True, null=True)
    shift = models.CharField('班次', max_length=100, blank=True, null=True)

    # ── 打卡（3 组） ──
    in1_time = models.CharField('上班1打卡时间', max_length=30, blank=True, null=True)
    in1_result = models.CharField('上班1打卡结果', max_length=50, blank=True, null=True)
    out1_time = models.CharField('下班1打卡时间', max_length=30, blank=True, null=True)
    out1_result = models.CharField('下班1打卡结果', max_length=50, blank=True, null=True)

    in2_time = models.CharField('上班2打卡时间', max_length=30, blank=True, null=True)
    in2_result = models.CharField('上班2打卡结果', max_length=50, blank=True, null=True)
    out2_time = models.CharField('下班2打卡时间', max_length=30, blank=True, null=True)
    out2_result = models.CharField('下班2打卡结果', max_length=50, blank=True, null=True)

    in3_time = models.CharField('上班3打卡时间', max_length=30, blank=True, null=True)
    in3_result = models.CharField('上班3打卡结果', max_length=50, blank=True, null=True)
    out3_time = models.CharField('下班3打卡时间', max_length=30, blank=True, null=True)
    out3_result = models.CharField('下班3打卡结果', max_length=50, blank=True, null=True)

    approval_ref = models.CharField('关联的审批单', max_length=500, blank=True, null=True)

    # ── 统计字段 ──
    attend_days = models.DecimalField('出勤天数', max_digits=6, decimal_places=2, default=0)
    rest_days = models.DecimalField('休息天数', max_digits=6, decimal_places=2, default=0)
    work_minutes = models.DecimalField('工作时长(分钟)', max_digits=8, decimal_places=2,
                                       blank=True, null=True)

    late_count = models.IntegerField('迟到次数', default=0)
    late_minutes = models.DecimalField('迟到时长(分)', max_digits=8, decimal_places=2, default=0)
    serious_late_count = models.IntegerField('严重迟到次数', default=0)
    serious_late_minutes = models.DecimalField('严重迟到时长(分)', max_digits=8, decimal_places=2, default=0)
    absenteeism_late_days = models.DecimalField('旷工迟到天数', max_digits=6, decimal_places=2, default=0)

    early_leave_count = models.IntegerField('早退次数', default=0)
    early_leave_minutes = models.DecimalField('早退时长(分)', max_digits=8, decimal_places=2, default=0)
    missing_in_count = models.IntegerField('上班缺卡次数', default=0)
    missing_out_count = models.IntegerField('下班缺卡次数', default=0)
    absenteeism_days = models.DecimalField('旷工天数', max_digits=6, decimal_places=2, default=0)

    business_trip_hours = models.DecimalField('出差时长(小时)', max_digits=8, decimal_places=2, default=0)
    field_work_hours = models.DecimalField('外出时长(小时)', max_digits=8, decimal_places=2, default=0)

    # ── 请假（10 类，对齐钉钉两层表头）──
    # ⚠️ 单位不统一：事假/调休/病假/哺乳假 是「小时」，其余是「天」。
    #    为便于统计，这里**一律存小时**（天 × 标准工作时长 折算），
    #    原始单位记在 leave_unit_note 里备查。
    leave_personal = models.DecimalField('事假(小时)', max_digits=8, decimal_places=2, default=0)
    leave_compensatory = models.DecimalField('调休(小时)', max_digits=8, decimal_places=2, default=0)
    leave_sick = models.DecimalField('病假(小时)', max_digits=8, decimal_places=2, default=0)
    leave_annual = models.DecimalField('年假(小时)', max_digits=8, decimal_places=2, default=0)
    leave_maternity = models.DecimalField('产假(小时)', max_digits=8, decimal_places=2, default=0)
    leave_paternity = models.DecimalField('陪产假(小时)', max_digits=8, decimal_places=2, default=0)
    leave_marriage = models.DecimalField('婚假(小时)', max_digits=8, decimal_places=2, default=0)
    leave_menstrual = models.DecimalField('例假(小时)', max_digits=8, decimal_places=2, default=0)
    leave_funeral = models.DecimalField('丧假(小时)', max_digits=8, decimal_places=2, default=0)
    leave_nursing = models.DecimalField('哺乳假(小时)', max_digits=8, decimal_places=2, default=0)

    # ── 加班 ──
    overtime_total = models.DecimalField('加班总时长(小时)', max_digits=8, decimal_places=2, default=0)
    overtime_workday = models.DecimalField('加班-工作日(小时)', max_digits=8, decimal_places=2, default=0)
    overtime_restday = models.DecimalField('加班-休息日(小时)', max_digits=8, decimal_places=2, default=0)
    overtime_holiday = models.DecimalField('加班-节假日(小时)', max_digits=8, decimal_places=2, default=0)

    # ── 系统字段 ──
    source_file = models.ForeignKey(UploadedFile, on_delete=models.CASCADE,
                                    related_name='daily_records', verbose_name='来源文件')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_daily_records',
                                   verbose_name='创建人')
    created_at = models.DateTimeField('创建时间', default=timezone.now)

    class Meta:
        db_table = 'attendance_daily'
        verbose_name = '日考勤明细'
        verbose_name_plural = '日考勤明细'
        ordering = ['work_date', 'name']
        # ⚠️ 不用 unique_together(work_date, user_id)：user_id 与 employee_no
        #    都可能为空，NULL 在唯一约束里不等于自身，会放进重复行。
        #    查重逻辑放在 services.py 里显式处理（见 ExcelImporter）。
        indexes = [
            models.Index(fields=['work_date', 'name']),
            models.Index(fields=['user_id', 'work_date']),
        ]

    def __str__(self):
        return f"{self.work_date} {self.name}"

    @property
    def dedup_key(self):
        """导入查重用的业务主键。user_id 为空时退回姓名。"""
        return (self.work_date, self.user_id or f'name:{self.name}')


class LeaveRecord(models.Model):
    """请假记录 —— 对应钉钉「请假单据」文件"""

    approval_no = models.CharField('审批编号', max_length=64, unique=True)

    title = models.CharField('标题', max_length=255, blank=True, null=True)
    approval_status = models.CharField('审批状态', max_length=50, blank=True, null=True, db_index=True)
    approval_result = models.CharField('审批结果', max_length=50, blank=True, null=True, db_index=True)

    submit_time = models.DateTimeField('发起时间', blank=True, null=True)
    finish_time = models.DateTimeField('完成时间', blank=True, null=True)

    applicant_no = models.CharField('发起人工号', max_length=50, blank=True, null=True)
    applicant_user_id = models.CharField('发起人UserID', max_length=100, blank=True, null=True, db_index=True)

    applicant_name = models.CharField('发起人姓名', max_length=50, db_index=True,
                                      help_text='已归一化：去离职后缀')
    applicant_name_raw = models.CharField('发起人姓名(原始)', max_length=80, blank=True, null=True)
    applicant_dept = models.CharField('发起人部门', max_length=200, blank=True, null=True)

    leave_type = models.CharField('请假类型', max_length=50, db_index=True)
    start_time = models.DateTimeField('开始时间', db_index=True)
    end_time = models.DateTimeField('结束时间', blank=True, null=True)

    duration_hours = models.DecimalField('时长(小时)', max_digits=10, decimal_places=2, default=0)
    duration_raw = models.CharField('时长(原始文本)', max_length=50, blank=True, null=True)

    reason = models.TextField('请假事由', blank=True, null=True)

    # ── 归一化字段（导入时计算，便于统计） ──
    duration_days = models.DecimalField('折算天数', max_digits=10, decimal_places=4, default=0,
                                        help_text='duration_hours ÷ 标准工作时长')
    is_resigned = models.BooleanField('离职标记', default=False, db_index=True)
    is_approved = models.BooleanField('审批通过', default=False, db_index=True,
                                      help_text='审批状态=完成 且 审批结果=同意')
    period = models.CharField('所属月份', max_length=7, db_index=True,
                              help_text='按开始时间归属，格式 YYYY-MM')

    source_file = models.ForeignKey(UploadedFile, on_delete=models.CASCADE,
                                    related_name='leave_records', verbose_name='来源文件')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_leave_records',
                                   verbose_name='创建人')
    created_at = models.DateTimeField('创建时间', default=timezone.now)

    class Meta:
        db_table = 'attendance_leave_records'
        verbose_name = '请假记录'
        verbose_name_plural = '请假记录'
        ordering = ['-start_time']
        indexes = [
            models.Index(fields=['period', 'applicant_name']),
            models.Index(fields=['period', 'leave_type', 'is_approved']),
        ]

    def __str__(self):
        return f"{self.applicant_name} {self.leave_type} {self.start_time:%Y-%m-%d}"


class AttendanceRule(models.Model):
    """
    考勤规则 —— 替换桌面工具的硬编码

    桌面版实测的硬编码清单（每一条都对应这里的一个字段）：
      · 第 387 行 (float(j_val) - 450) / 60      → standard_work_minutes
      · 第 543/626/720 行 /7.5、*7.5             → standard_work_minutes
      · 第 381-382 行 if name == '<某个姓名>': pass → excluded_employees / exclude_mode
      · 第 481-486 行 PatternFill(fgColor=...)   → color_map
      · GUI 输入框「应出勤天数」默认 22，实跑 24   → monthly_standard_days
    """

    name = models.CharField('规则名称', max_length=100)

    standard_work_minutes = models.IntegerField(
        '标准工作时长(分钟)', default=450,
        help_text='超过此值算加班。默认 450 分 = 7.5 小时（桌面版硬编码值）')

    monthly_standard_days = models.DecimalField(
        '月应出勤天数', max_digits=6, decimal_places=2, default=24,
        help_text='人工设定值，写入报表「应出勤」列')

    excluded_employees = models.JSONField(
        '排除人员名单', default=mapping.default_excluded_employees, blank=True,
        help_text='按姓名排除的补充名单。主判据是下面的结构化规则；'
                  '真实员工姓名不要写进代码，需要时在此页填写')

    excluded_attend_groups = models.JSONField(
        '占位账号的考勤组', default=mapping.default_excluded_attend_groups, blank=True,
        help_text='⚠️ 不是"见到这个考勤组就排除"！还要同时满足下面的"无信息"条件。'
                  '实测该考勤组里混着真实员工（有部门、有真实出勤）')

    exclude_mode = models.CharField(
        '占位账号排除严格度', max_length=20,
        choices=mapping.EXCLUDE_MODE_CHOICES, default=mapping.DEFAULT_EXCLUDE_MODE,
        help_text='⚠️ 不要选「考勤组命中即排除」：该考勤组里有真实员工，会被误伤')

    exclude_if_no_data = models.BooleanField(
        '排除全月无数据的人', default=False,
        help_text='⚠️ 默认关闭。实测真实员工可能整月无打卡（产假、长期出差、'
                  '高管不打卡），基线报表里这些人是保留的，排除会让月报漏人')

    color_map = models.JSONField(
        '状态颜色映射', default=mapping.default_color_map, blank=True,
        help_text='单元格文本 → ARGB 色值，如 {"请假": "FFC000"}')

    clamp_negative_overtime = models.BooleanField(
        '负加班截断为 0', default=True,
        help_text='单日加班 = max(0, (工作时长-标准时长)/60)。'
                  '桌面版不截断，负加班会冲减月度合计')

    exclude_rejected_leave = models.BooleanField(
        '排除未通过的请假', default=True,
        help_text='审批状态≠完成 或 审批结果≠同意 的记录不进明细也不进合计。'
                  '桌面版只挡了明细，合计仍会把被拒记录算进去')

    is_active = models.BooleanField('启用', default=True, db_index=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='created_rules', verbose_name='创建人')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'attendance_rules'
        verbose_name = '考勤规则'
        verbose_name_plural = '考勤规则'
        ordering = ['-is_active', '-updated_at']

    def __str__(self):
        return f"{self.name}{'（启用）' if self.is_active else '（停用）'}"

    def save(self, *args, **kwargs):
        """同一时间只允许一条启用规则 —— 口径必须唯一，否则报表无法解释。"""
        super().save(*args, **kwargs)
        if self.is_active:
            AttendanceRule.objects.exclude(pk=self.pk).filter(is_active=True).update(is_active=False)

    @classmethod
    def get_active(cls):
        """取当前生效规则；没有则按默认值建一条（幂等）。"""
        rule = cls.objects.filter(is_active=True).first()
        if rule is None:
            rule = cls.objects.create(
                name='默认规则',
                standard_work_minutes=450,
                monthly_standard_days=24,
                excluded_employees=mapping.default_excluded_employees(),
                excluded_attend_groups=mapping.default_excluded_attend_groups(),
                exclude_mode=mapping.DEFAULT_EXCLUDE_MODE,
                exclude_if_no_data=mapping.DEFAULT_EXCLUDE_IF_NO_DATA,
                color_map=mapping.default_color_map(),
                clamp_negative_overtime=True,
                exclude_rejected_leave=True,
                is_active=True,
            )
        return rule
