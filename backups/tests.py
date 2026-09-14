"""
backups 应用测试

⚠️ 备份属计划书 7.3 的"不可回归项"：备份功能可用、审计覆盖。
   这里不真的调 mysqldump（测试环境是 SQLite），只测文件层与边界条件。
"""
import os
import shutil
import tempfile
import zipfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserPermission

from .backup import DatabaseBackup
from .models import BackupRecord


class BackupServiceTests(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='attendance-backup-test-')
        self.service = DatabaseBackup()
        self.service.backup_dir = self.tmpdir

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, name, content=b'x', age_days=0):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'wb') as fh:
            fh.write(content)
        if age_days:
            old = (timezone.now() - timedelta(days=age_days)).timestamp()
            os.utime(path, (old, old))
        return path

    def test_list_backups_sorted_desc_and_formats_size(self):
        self._make_file('old.zip', b'a' * 10, age_days=5)
        self._make_file('new.zip', b'a' * 2048)
        self._make_file('notes.txt', b'ignored')   # 非备份文件应被忽略

        items = self.service.list_backups()
        self.assertEqual([i['name'] for i in items], ['new.zip', 'old.zip'])
        self.assertEqual(items[0]['size'], 2048)
        self.assertIn('KB', items[0]['size_display'])

    def test_list_backups_missing_dir_returns_empty(self):
        self.service.backup_dir = os.path.join(self.tmpdir, 'nope')
        self.assertEqual(self.service.list_backups(), [])

    def test_cleanup_missing_dir_does_not_raise(self):
        """
        目录不存在时必须正常返回，而不是抛 FileNotFoundError。
        （备份目录由 BACKUP_DIR 环境变量控制，换部署方式时目录可能不存在。）
        """
        self.service.backup_dir = os.path.join(self.tmpdir, 'nope')
        success, count, message = self.service.cleanup_old_backups()
        self.assertTrue(success)
        self.assertEqual(count, 0)

    def test_cleanup_removes_only_expired(self):
        self.service.retention_days = 30
        self._make_file('expired.zip', age_days=40)
        self._make_file('fresh.zip', age_days=1)

        success, count, _ = self.service.cleanup_old_backups()
        self.assertTrue(success)
        self.assertEqual(count, 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'expired.zip')))
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, 'fresh.zip')))

    def test_get_backup_stats_does_not_mix_naive_and_aware(self):
        """
        ⚠️ list_backups() 返回 naive datetime，timezone.now() 是 aware；
        直接比较在 USE_TZ=True 下会抛 TypeError。
        """
        self._make_file('today.zip', age_days=0)
        self._make_file('ancient.zip', age_days=90)

        stats = self.service.get_backup_stats()
        self.assertEqual(stats['total_count'], 2)
        self.assertEqual(stats['today_count'], 1)
        self.assertEqual(stats['week_count'], 1)
        self.assertGreater(stats['total_size_mb'], 0)

    def test_get_backup_stats_empty_dir(self):
        stats = self.service.get_backup_stats()
        self.assertEqual(stats['total_count'], 0)
        self.assertEqual(stats['today_count'], 0)

    def test_download_backup_returns_response_or_none(self):
        self._make_file('dump.zip', b'PK')
        resp = self.service.download_backup('dump.zip')
        self.assertIsNotNone(resp)
        self.assertEqual(resp['Content-Type'], 'application/zip')
        resp.close()
        self.assertIsNone(self.service.download_backup('missing.zip'))

    def test_delete_backup(self):
        self._make_file('dump.zip')
        ok, _ = self.service.delete_backup('dump.zip')
        self.assertTrue(ok)
        ok, message = self.service.delete_backup('dump.zip')
        self.assertFalse(ok)
        self.assertIn('不存在', message)

    def test_zip_roundtrip(self):
        """备份产物是 zip；确认能正常解出内部 sql"""
        sql = os.path.join(self.tmpdir, 'x.sql')
        with open(sql, 'w', encoding='utf-8') as fh:
            fh.write('SELECT 1;')
        zip_path = sql + '.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(sql, os.path.basename(sql))
        os.remove(sql)

        with zipfile.ZipFile(zip_path) as zf:
            self.assertEqual(zf.read('x.sql').decode(), 'SELECT 1;')


class BackupViewTests(TestCase):
    """权限边界：备份页仅管理员可见"""

    def setUp(self):
        self.user = User.objects.create_user('hr', password='x')

    def test_anonymous_redirected(self):
        resp = self.client.get(reverse('backups:backup_list'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('accounts:login'), resp['Location'])

    def test_normal_user_denied(self):
        self.client.force_login(self.user)
        UserPermission.objects.create(user=self.user, permission_code='attendance.query')
        self.assertEqual(self.client.get(reverse('backups:backup_list')).status_code, 302)

    def test_admin_can_open(self):
        admin = User.objects.create_superuser('boss', password='x')
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse('backups:backup_list')).status_code, 200)


class BackupRecordTests(TestCase):
    def test_str_is_readable(self):
        record = BackupRecord.objects.create(file_path='/tmp/a.zip', file_size=1024)
        self.assertIn('成功', str(record))
