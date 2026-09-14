"""
core 应用测试 —— 健康检查
"""
from django.test import TestCase
from django.urls import reverse


class HealthCheckTests(TestCase):
    def test_health_returns_ok(self):
        """健康检查无需登录，且数据库正常时返回 status=ok"""
        resp = self.client.get(reverse('health_check'))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(data['database'])

    def test_health_does_not_leak_business_data(self):
        """健康检查不得返回任何业务数据（探活接口通常无认证）"""
        resp = self.client.get(reverse('health_check'))
        self.assertEqual(set(resp.json().keys()), {'status', 'database'})


class HomeTests(TestCase):
    def test_home_requires_login(self):
        """首页必须登录才能访问（未登录 302 到登录页）"""
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('accounts:login'), resp['Location'])
