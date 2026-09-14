"""
查看导入报告的运维命令

用法：
    python manage.py inspect_import              # 列出全部导入及概要
    python manage.py inspect_import --id 3       # 看某一次的完整报告
    python manage.py inspect_import --id 3 --errors-only
    python manage.py inspect_import --code-stats # 逐人统计（用于跟桌面版对账）

为什么要有这个命令：导入报告里的「被排除的人员」「错误明细」在页面上是分页/
折叠的，运维核对总量时用命令行更快，也方便在容器里直接跑。
"""
from collections import Counter, defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from attendance.models import AttendanceDaily, LeaveRecord, UploadedFile


class Command(BaseCommand):
    help = '查看考勤导入报告与逐人统计'

    def add_arguments(self, parser):
        parser.add_argument('--id', type=int, help='上传文件 ID')
        parser.add_argument('--errors-only', action='store_true',
                            help='只显示错误与被排除的人员')
        parser.add_argument('--code-stats', action='store_true',
                            help='逐人统计（出勤天数/加班/迟到/缺卡/请假），用于与桌面版对账')

    def handle(self, *args, **options):
        if options['code_stats']:
            return self._code_stats()

        qs = UploadedFile.objects.order_by('pk')
        if options['id']:
            qs = qs.filter(pk=options['id'])
            if not qs.exists():
                raise CommandError(f'找不到 ID 为 {options["id"]} 的导入记录')

        for f in qs:
            self._print_one(f, options['errors_only'])

    # ── 单条报告 ──

    def _print_one(self, f, errors_only=False):
        r = f.import_report or {}
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n=== #{f.pk} {f.original_filename} ==='))
        self.stdout.write(
            f'  类型={f.get_file_kind_display()}  账期={f.period}  '
            f'状态={f.get_status_display()}  '
            f'导入={f.record_count}  跳过={f.skipped_count}')
        self.stdout.write(f'  文件={f.file_path}')
        self.stdout.write(f'  SHA256={f.file_hash}')

        purged = r.get('purged')
        if purged:
            self.stdout.write(self.style.WARNING(
                f'  已清空旧数据：账期 {purged["period"]}，'
                f'{purged["records"]} 条记录 / {purged["files"]} 个历史文件'))

        if not errors_only:
            for s in r.get('sheets', []):
                self.stdout.write(
                    f'  Sheet {s["sheet"]!r}: 导入 {s["imported"]}，跳过 {s["skipped"]}')

        errors = r.get('errors', [])
        if errors:
            self.stdout.write(self.style.ERROR(f'  错误 {len(errors)} 条：'))
            for e in errors[:30]:
                sheet = e.get('sheet') or '-'
                self.stdout.write(
                    f'    ! [{sheet} 第{e.get("row", "-")}行] '
                    f'{e.get("field", "-")}={e.get("value", "-")!r} → {e.get("message")}')
            if len(errors) > 30:
                self.stdout.write(f'    …… 另有 {len(errors) - 30} 条')

        excluded = r.get('excluded', [])
        if excluded:
            self.stdout.write(f'  被排除的人员 {len(excluded)} 名：')
            for e in excluded:
                self.stdout.write(f'    - {e["name"]}: {e["reason"]}')

        if not errors_only:
            warnings = r.get('warnings', [])
            if warnings:
                self.stdout.write(self.style.WARNING(f'  提示 {len(warnings)} 条：'))
                for w in warnings:
                    self.stdout.write(f'    - {w}')

            if not errors and not excluded and f.status == 'success':
                self.stdout.write(self.style.SUCCESS('  干净完成，无错误、无排除'))

    # ── 逐人统计（与桌面版对账用） ──

    def _code_stats(self):
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== 逐人统计（全库） ==='))

        daily = defaultdict(lambda: {
            'name': '', 'dept': '', 'days': Decimal('0'), 'ot': Decimal('0'),
            'late': Decimal('0'), 'early': Decimal('0'),
            'miss_in': 0, 'miss_out': 0, 'rows': 0,
        })
        for d in AttendanceDaily.objects.all().iterator(chunk_size=2000):
            row = daily[d.name]
            row['name'] = d.name
            row['dept'] = row['dept'] or (d.department or '')
            row['days'] += Decimal(d.attend_days or 0)
            row['late'] += Decimal(d.late_minutes or 0)
            row['early'] += Decimal(d.early_leave_minutes or 0)
            row['miss_in'] += int(d.missing_in_count or 0)
            row['miss_out'] += int(d.missing_out_count or 0)
            row['rows'] += 1
            if d.work_minutes is not None:
                row['ot'] += (Decimal(d.work_minutes) - 450) / 60

        leave = defaultdict(lambda: defaultdict(Decimal))
        for l in LeaveRecord.objects.filter(is_approved=True).iterator(chunk_size=2000):
            leave[l.applicant_name][l.leave_type] += Decimal(l.duration_hours or 0)

        self.stdout.write(
            f'{"姓名":<10}{"部门":<20}{"行数":>5}{"出勤":>7}{"加班H":>9}'
            f'{"迟到分":>8}{"早退分":>8}{"上缺":>5}{"下缺":>5}  请假(小时)')
        for name in sorted(daily):
            row = daily[name]
            lv = ' '.join(f'{k}={v}' for k, v in sorted(leave.get(name, {}).items()))
            self.stdout.write(
                f'{name:<10}{row["dept"][:18]:<20}{row["rows"]:>5}'
                f'{row["days"]:>7}{row["ot"]:>9.2f}'
                f'{row["late"]:>8}{row["early"]:>8}'
                f'{row["miss_in"]:>5}{row["miss_out"]:>5}  {lv}')

        self.stdout.write('')
        self.stdout.write(f'人数: {len(daily)}')
        self.stdout.write(f'日考勤行数: {AttendanceDaily.objects.count()}')
        self.stdout.write(f'请假记录: {LeaveRecord.objects.count()}'
                          f'（其中已通过 {LeaveRecord.objects.filter(is_approved=True).count()}）')
        per_period = Counter(
            AttendanceDaily.objects.values_list('work_date__year', 'work_date__month')
        )
        self.stdout.write(f'按月份分布: {dict(sorted(per_period.items()))}')

        ot_negative = sum(
            1 for d in AttendanceDaily.objects.all().iterator(chunk_size=2000)
            if d.work_minutes is not None and Decimal(d.work_minutes) < 450
        )
        self.stdout.write(self.style.WARNING(
            f'工作时长不足 450 分钟的行数（会产生负加班）: {ot_negative}'))
