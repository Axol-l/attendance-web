"""
导出 Web 侧的逐人汇总，供 L3 对账。

用法（本地或容器内）：
    python manage.py export_stats --period 2014-08 > web_stats.json

输出结构与 tools/independent_recalc.py 对齐，便于直接 diff。
"""
import json
import sys
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand

from attendance.models import AttendanceDaily, LeaveRecord


def dec(v, places=4):
    return round(float(v or 0), places)


class Command(BaseCommand):
    help = '导出逐人汇总 JSON（与 tools/independent_recalc.py 的输出对齐）'

    def add_arguments(self, parser):
        parser.add_argument('--period', default='2014-08', help='账期 YYYY-MM')
        parser.add_argument('--out', help='输出文件路径（不给则写 stdout）')

    def handle(self, *args, **options):
        period = options['period']
        year, month = (int(x) for x in period.split('-'))

        people = defaultdict(lambda: {
            'department': '', 'rows': 0, 'attend_days': Decimal('0'),
            'overtime_hours': Decimal('0'), 'late_minutes': Decimal('0'),
            'early_leave_minutes': Decimal('0'),
            'missing_in_count': 0, 'missing_out_count': 0,
            'overtime_total_sum': Decimal('0'),
            'leave_hours_approved': defaultdict(Decimal),
        })

        qs = AttendanceDaily.objects.filter(work_date__year=year, work_date__month=month)
        for d in qs.iterator(chunk_size=2000):
            p = people[d.name]
            p['rows'] += 1
            p['department'] = p['department'] or (d.department or '')
            p['attend_days'] += Decimal(d.attend_days or 0)
            p['late_minutes'] += Decimal(d.late_minutes or 0)
            p['early_leave_minutes'] += Decimal(d.early_leave_minutes or 0)
            p['missing_in_count'] += int(d.missing_in_count or 0)
            p['missing_out_count'] += int(d.missing_out_count or 0)
            p['overtime_total_sum'] += Decimal(d.overtime_total or 0)

            if d.work_minutes is not None:
                ot = (Decimal(d.work_minutes) - 450) / 60
                if ot < 0:
                    ot = Decimal('0')          # 已确认口径：负加班截断
                p['overtime_hours'] += ot

        for rec in LeaveRecord.objects.filter(period=period, is_approved=True) \
                                      .iterator(chunk_size=2000):
            people[rec.applicant_name]['leave_hours_approved'][rec.leave_type] += \
                Decimal(rec.duration_hours or 0)

        result = {
            'period': period,
            'totals': {
                'people_imported': len(people),
                'daily_rows_imported': qs.count(),
                'leave_rows_approved': LeaveRecord.objects.filter(
                    period=period, is_approved=True).count(),
            },
            'people': {
                name: {
                    'department': p['department'],
                    'rows': p['rows'],
                    'attend_days': dec(p['attend_days'], 2),
                    'overtime_hours': dec(p['overtime_hours']),
                    'late_minutes': dec(p['late_minutes'], 2),
                    'early_leave_minutes': dec(p['early_leave_minutes'], 2),
                    'missing_in_count': p['missing_in_count'],
                    'missing_out_count': p['missing_out_count'],
                    'overtime_total_sum': dec(p['overtime_total_sum']),
                    'leave_hours_approved': {
                        k: dec(v, 2) for k, v in sorted(p['leave_hours_approved'].items())
                    },
                }
                for name, p in sorted(people.items())
            },
        }

        text = json.dumps(result, ensure_ascii=False, indent=2)
        if options.get('out'):
            with open(options['out'], 'w', encoding='utf-8') as fh:
                fh.write(text)
            self.stderr.write(f'已写入 {options["out"]}')
        else:
            sys.stdout.write(text)
