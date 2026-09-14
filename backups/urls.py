from django.urls import path
from . import views

app_name = 'backups'

urlpatterns = [
    path('', views.backup_list, name='backup_list'),
    path('create/', views.create_backup, name='create_backup'),
    path('<int:pk>/download/', views.download_backup, name='download_backup'),
    path('<int:pk>/delete/', views.delete_backup, name='delete_backup'),
]
