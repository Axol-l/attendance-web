from django.urls import path

from . import views

app_name = 'attendance'

urlpatterns = [
    path('', views.overview, name='overview'),

    # 上传与导入
    path('upload/', views.upload, name='upload'),
    path('imports/', views.file_list, name='file_list'),
    path('imports/<int:pk>/', views.import_detail, name='import_detail'),
    path('imports/<int:pk>/preview/', views.import_preview, name='import_preview'),
    path('imports/<int:pk>/delete/', views.file_delete, name='file_delete'),

    # 查询
    path('daily/', views.daily_list, name='daily_list'),
    path('leave/', views.leave_list, name='leave_list'),
    path('monthly/', views.monthly_summary, name='monthly_summary'),

    # 报表
    path('report/', views.report_generate, name='report_generate'),

    # 规则配置
    path('rules/', views.rule_config, name='rule_config'),
    path('rules/save/', views.rule_save, name='rule_save'),
]
