"""
用户账户URL配置
"""
from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('users/', views.user_list, name='user_list'),
    path('users/create/', views.user_create, name='user_create'),
    path('users/<int:pk>/update/', views.user_update, name='user_update'),
    path('users/<int:pk>/delete/', views.user_delete, name='user_delete'),
    path('change-password/', views.change_password, name='change_password'),
    path('logs/', views.audit_log_list, name='audit_log_list'),
    path('logs/<int:pk>/detail/', views.audit_log_detail, name='audit_log_detail'),
]
