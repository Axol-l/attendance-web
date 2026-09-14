"""
DatabaseBackup 服务类 — 从 orders/backup.py 迁移

提供：手动备份(mysqldump+zip)、下载、删除、恢复、异地同步、过期清理、统计
"""
import os
import subprocess
import shutil
import zipfile
from datetime import datetime, timedelta
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse
from django.utils import timezone


class DatabaseBackup:

    def __init__(self):
        # 备份目录：优先读环境变量 BACKUP_DIR，便于与 docker-compose 的挂载点对齐。
        # docker-compose.server.yml 把宿主机的 ./backup_files 挂到容器的
        # /app/backup_files，web 服务的 environment 也据此设置了 BACKUP_DIR。
        # 若不加这个开关，页面读的 /app/backups 与挂载目录对不上，
        # 历史备份永远列不出来。
        #
        # ⚠️ 早前这里的注释写的是"对应宿主 /data/backups/attendance"，
        #    与实际挂载点不符（会误导排障），已按 compose 的真实配置更正。
        self.backup_dir = os.getenv('BACKUP_DIR') or os.path.join(settings.BASE_DIR, 'backups')
        os.makedirs(self.backup_dir, exist_ok=True)
        self.remote_backup_dir = getattr(settings, 'REMOTE_BACKUP_DIR', None)
        self.retention_days = getattr(settings, 'BACKUP_RETENTION_DAYS', 30)

    # ── 核心操作 ──

    # 子进程超时（秒）。必须小于 gunicorn 的 timeout（120s），
    # 否则备份还没结束就被 gunicorn 杀掉 worker，留下半截文件且无记录。
    SUBPROCESS_TIMEOUT = 90

    def create_backup(self, backup_name=None):
        """mysqldump → zip，返回 (success, message, filename|None)"""
        try:
            if not backup_name:
                backup_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"

            sql_path = os.path.join(self.backup_dir, backup_name)
            db = settings.DATABASES['default']

            cmd = [
                'mysqldump',
                f"--host={db.get('HOST', 'localhost')}",
                f"--port={db.get('PORT', '3306')}",
                f"--user={db['USER']}",
                f"--password={db['PASSWORD']}",
                '--skip-ssl',
                '--single-transaction',
                '--routines',
                '--triggers',
                db['NAME'],
            ]

            # 注意：subprocess.run 的 timeout 只有在「捕获输出（PIPE）」时才会
            # 真正杀掉子进程（官方文档明确说明）。这里必须用 stdout=PIPE 捕获，
            # 再自己写文件，才能保证超时后 mysqldump 被终止、不残留僵尸进程。
            try:
                with open(sql_path, 'w', encoding='utf-8') as f:
                    result = subprocess.run(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, timeout=self.SUBPROCESS_TIMEOUT,
                    )
                    f.write(result.stdout or '')
            except subprocess.TimeoutExpired:
                if os.path.exists(sql_path):
                    os.remove(sql_path)
                return (False,
                        f'备份超时（超过 {self.SUBPROCESS_TIMEOUT} 秒）：数据量过大，'
                        f'请改用服务器 crontab 执行 mysqldump', None)

            if result.returncode != 0:
                if os.path.exists(sql_path):
                    os.remove(sql_path)
                return False, f'备份失败：{result.stderr}', None

            zip_path = sql_path + '.zip'
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(sql_path, os.path.basename(sql_path))
            os.remove(sql_path)

            file_size = os.path.getsize(zip_path)
            zip_name = backup_name + '.zip'

            if self.remote_backup_dir:
                self.sync_to_remote(zip_name)

            return True, f'备份成功！文件大小：{file_size / 1024 / 1024:.2f} MB', zip_name

        except Exception as e:
            return False, f'备份异常：{e}', None

    def list_backups(self):
        """扫描 backups/ 目录，返回文件列表（按时间倒序）"""
        items = []
        if not os.path.exists(self.backup_dir):
            return items

        for name in os.listdir(self.backup_dir):
            if not (name.endswith('.zip') or name.endswith('.sql')):
                continue
            path = os.path.join(self.backup_dir, name)
            stat = os.stat(path)
            items.append({
                'name': name,
                'size': stat.st_size,
                'size_display': self._format_size(stat.st_size),
                'date': datetime.fromtimestamp(stat.st_mtime),
            })

        items.sort(key=lambda x: x['date'], reverse=True)
        return items

    def download_backup(self, backup_name):
        """返回 FileResponse 或 None"""
        path = os.path.join(self.backup_dir, backup_name)
        if not os.path.exists(path):
            return None
        resp = FileResponse(open(path, 'rb'))
        ct = 'application/zip' if backup_name.endswith('.zip') else 'application/sql'
        resp['Content-Type'] = ct
        resp['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(backup_name)}"
        return resp

    def delete_backup(self, backup_name):
        """删除物理文件，返回 (success, message)"""
        path = os.path.join(self.backup_dir, backup_name)
        if not os.path.exists(path):
            return False, '文件不存在'
        os.remove(path)
        if self.remote_backup_dir:
            remote = os.path.join(self.remote_backup_dir, backup_name)
            if os.path.exists(remote):
                os.remove(remote)
        return True, '删除成功'

    def restore_backup(self, backup_name):
        """解压 zip → mysql 导入，返回 (success, message)"""
        path = os.path.join(self.backup_dir, backup_name)
        if not os.path.exists(path):
            return False, '文件不存在'

        sql_file = path
        temp_sql = None
        if backup_name.endswith('.zip'):
            with zipfile.ZipFile(path, 'r') as zf:
                sql_name = backup_name.replace('.zip', '')
                temp_sql = os.path.join(self.backup_dir, f'_temp_{sql_name}')
                with open(temp_sql, 'wb') as f:
                    f.write(zf.read(sql_name))
            sql_file = temp_sql

        try:
            db = settings.DATABASES['default']
            cmd = [
                'mysql',
                f"--host={db.get('HOST', 'localhost')}",
                f"--port={db.get('PORT', '3306')}",
                f"--user={db['USER']}",
                f"--password={db['PASSWORD']}",
                db['NAME'],
            ]
            with open(sql_file, 'r', encoding='utf-8') as f:
                try:
                    result = subprocess.run(
                        cmd, stdin=f, stderr=subprocess.PIPE, text=True,
                        timeout=self.SUBPROCESS_TIMEOUT,
                    )
                except subprocess.TimeoutExpired:
                    return False, f'恢复超时（超过 {self.SUBPROCESS_TIMEOUT} 秒）'

            if result.returncode != 0:
                return False, f'恢复失败：{result.stderr}'
            return True, '恢复成功'
        finally:
            if temp_sql and os.path.exists(temp_sql):
                os.remove(temp_sql)

    # ── 容灾 ──

    def sync_to_remote(self, backup_name):
        if not self.remote_backup_dir:
            return False, '未配置异地备份目录'
        src = os.path.join(self.backup_dir, backup_name)
        if not os.path.exists(src):
            return False, '文件不存在'
        os.makedirs(self.remote_backup_dir, exist_ok=True)
        shutil.copy2(src, os.path.join(self.remote_backup_dir, backup_name))
        return True, '同步成功'

    def cleanup_old_backups(self):
        """删除超过 retention_days 的文件，返回 (success, count, message)"""
        cutoff = timezone.now() - timedelta(days=self.retention_days)
        deleted = 0
        # ⚠️ 必须先判目录存在：备份目录由环境变量 BACKUP_DIR 配置，
        #    换了部署方式（如独立部署未挂载卷）时目录可能不存在，
        #    os.listdir 会直接抛 FileNotFoundError 而不是"没有可清理的备份"。
        if not os.path.isdir(self.backup_dir):
            return True, 0, '备份目录不存在，无需清理'
        for name in os.listdir(self.backup_dir):
            if not (name.endswith('.zip') or name.endswith('.sql')):
                continue
            path = os.path.join(self.backup_dir, name)
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            mtime = timezone.make_aware(mtime)
            if mtime < cutoff:
                os.remove(path)
                deleted += 1
                if self.remote_backup_dir:
                    remote = os.path.join(self.remote_backup_dir, name)
                    if os.path.exists(remote):
                        os.remove(remote)
        return True, deleted, f'清理了 {deleted} 个过期备份'

    def get_backup_stats(self):
        """
        备份统计。

        ⚠️ 时区一致性：list_backups() 返回的 date 是 naive datetime
        （datetime.fromtimestamp），而这里用 timezone.now() 得到的是 aware
        datetime，两者直接比较在 USE_TZ=True 下会抛
        "can't compare offset-naive and offset-aware datetimes"。
        因此先把 naive 时间本地化再比。
        """
        backups = self.list_backups()
        now = timezone.now()
        today = timezone.localdate()
        week_ago = now - timedelta(days=7)

        def as_aware(dt):
            return timezone.make_aware(dt) if timezone.is_naive(dt) else dt

        return {
            'total_count': len(backups),
            'total_size_mb': sum(b['size'] for b in backups) / 1024 / 1024,
            'today_count': sum(1 for b in backups
                               if timezone.localtime(as_aware(b['date'])).date() == today),
            'week_count': sum(1 for b in backups if as_aware(b['date']) >= week_ago),
        }

    @staticmethod
    def _format_size(size_bytes):
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size_bytes < 1024:
                return f'{size_bytes:.1f} {unit}'
            size_bytes /= 1024
        return f'{size_bytes:.1f} TB'


backup_service = DatabaseBackup()
