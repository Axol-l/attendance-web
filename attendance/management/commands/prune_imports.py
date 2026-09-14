"""清理「导入记录」里的冗余历史条目。

背景
----
同一账期被重复导入时，数据行会被后来的导入接管（同键覆盖或整账期 purge），
**早先那几条 `UploadedFile` 记录就不再持有任何数据行**，但仍留在「导入记录」
页面里。反复重导后该页面会出现一长串同名条目，看不出哪条才是当前生效的。

本命令只删除满足**全部**以下条件的记录：

  1. 名下既无 `attendance_daily` 也无 `attendance_leave_records`；
  2. 同一 `(file_kind, period)` 存在**更晚**的导入；
  3. 状态是 `success` 或 `partial`。

刻意**不删**的：

  · `status='failed'` —— 失败记录带有 `error_message`，是排错依据，即使没有数据也要留；
  · 同一账期最后一条导入 —— 它是当前生效的那条，哪怕暂时没有数据行
    （例如文件里所有行都被规则排除，这在业务上是有效结果）；
  · `pending` —— 可能正在处理中。

用法
----
    # 预览（默认，不删任何东西）
    python manage.py prune_imports

    # 真正执行
    python manage.py prune_imports --apply
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from attendance.models import AttendanceDaily, LeaveRecord, UploadedFile


class Command(BaseCommand):
    help = '删除已被后续同账期导入取代、且名下无任何数据行的导入记录'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真正执行删除（默认只预览）')

    def handle(self, *args, **options):
        apply_changes = options['apply']

        # 一次查出每条记录名下的数据行数，避免逐条 count() 打 N+1 次库
        daily_counts = dict(
            AttendanceDaily.objects.values_list('source_file_id')
            .annotate(n=Count('id')).values_list('source_file_id', 'n'))
        leave_counts = dict(
            LeaveRecord.objects.values_list('source_file_id')
            .annotate(n=Count('id')).values_list('source_file_id', 'n'))

        records = list(UploadedFile.objects.order_by('id'))

        # 按 (类型, 账期) 分组，每组最后一条（id 最大）视为生效记录
        latest = {}
        for rec in records:
            key = (rec.file_kind, rec.period)
            if key not in latest or rec.id > latest[key]:
                latest[key] = rec.id

        to_delete = []
        for rec in records:
            key = (rec.file_kind, rec.period)
            rows = daily_counts.get(rec.id, 0) + leave_counts.get(rec.id, 0)

            if rows:
                continue
            if rec.status not in ('success', 'partial'):
                continue
            if latest.get(key) == rec.id:
                continue
            to_delete.append(rec)

        total = len(records)
        self.stdout.write(f'导入记录共 {total} 条，其中冗余可清理 {len(to_delete)} 条：')
        for rec in to_delete:
            self.stdout.write(
                f'  #{rec.id} {rec.get_file_kind_display()} {rec.period} '
                f'{rec.original_filename} [{rec.status}] 数据行=0')

        if not to_delete:
            self.stdout.write(self.style.SUCCESS('无需清理'))
            return

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                '这是预览。确认无误后加 --apply 真正删除。'))
            return

        removed_files = 0
        for rec in to_delete:
            # 名下的文件是当时上传的原始副本；记录删了它就是孤儿文件，
            # 顺手清掉，避免 media/uploads 目录随重导次数无限膨胀。
            path = rec.file_path
            try:
                import os
                if path and os.path.isfile(path):
                    os.remove(path)
                    removed_files += 1
            except OSError as exc:
                self.stderr.write(f'  文件删除失败（忽略）：{path} -> {exc}')
            rec.delete()

        self.stdout.write(self.style.SUCCESS(
            f'已删除 {len(to_delete)} 条冗余导入记录，'
            f'同时清理 {removed_files} 个孤儿上传文件。'))
        self.stdout.write(f'剩余导入记录 {UploadedFile.objects.count()} 条。')
