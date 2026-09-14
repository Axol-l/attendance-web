"""
自动创建 Django 超级用户脚本（Docker 友好版）
适用于 Docker 容器自动化部署

⚠️ 默认口令仅用于首次部署，登录后必须立即修改。
   生产环境请通过环境变量 DJANGO_SUPERUSER_PASSWORD 覆盖。
"""
import os
import sys

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User  # noqa: E402

from accounts.models import UserProfile  # noqa: E402


def create_superuser():
    username = os.getenv('DJANGO_SUPERUSER_USERNAME', 'admin')
    email = os.getenv('DJANGO_SUPERUSER_EMAIL', 'admin@example.com')
    password = os.getenv('DJANGO_SUPERUSER_PASSWORD', 'admin123456')

    try:
        if User.objects.filter(username=username).exists():
            print(f"[INFO] Superuser '{username}' already exists. Skipping creation.")
            return

        user = User.objects.create_superuser(username=username, email=email, password=password)

        # 注意：accounts/signals.py 的 post_save 已在 User 创建瞬间用默认值
        # role='user' 建好 profile，因此 get_or_create 的 defaults 会被静默忽略，
        # 必须显式回写 role，否则超管在业务层的角色仍是普通用户。
        try:
            profile, _ = UserProfile.objects.get_or_create(user=user)
            if profile.role != 'super_admin':
                profile.role = 'super_admin'
                profile.save(update_fields=['role'])
        except Exception as e:
            print(f"[WARNING] Could not create UserProfile: {e}")

        print("[SUCCESS] Superuser created successfully!")
        print(f"   Username: {username}")
        print(f"   Email: {email}")
        print("   Password: (已按环境变量或默认值设置，请立即修改)")
        print("   Admin URL: http://localhost:8001/admin/")

    except Exception as e:
        print(f"[ERROR] Failed to create superuser: {e}")
        sys.exit(1)


if __name__ == '__main__':
    create_superuser()
