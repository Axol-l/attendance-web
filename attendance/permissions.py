"""
考勤模块的权限矩阵 —— 单一事实来源

⚠️ 为什么要有这个文件：权限判断散落在视图装饰器、上下文处理器、模板
   `{% if user_perms.xxx %}` 三处，非常容易写成"前端按钮看不见但后端能访问"
   （或反过来）。这里集中定义，视图与模板都用它，测试逐条覆盖。

权限码与角色的关系：
  · superuser / profile.is_admin = True  → 拥有全部权限
  · 普通用户 → 查 UserPermission 表里显式授予的权限码
"""
from accounts.decorators import has_permission, is_admin

# ── 页面 → 需要的权限码 ──
# 值可以是单个权限码，也可以是 tuple（满足其一即可）
PAGE_PERMISSIONS = {
    # 概览与查询
    'attendance:overview':        'attendance.query',
    'attendance:daily_list':      'attendance.query',
    'attendance:leave_list':      'attendance.query',
    'attendance:monthly_summary': 'attendance.query',
    'attendance:file_list':       'attendance.query',   # 上传者可看自己的，见 can_view_import

    # 上传与导入
    'attendance:upload':          'attendance.upload',
    'attendance:file_delete':     'attendance.upload',
    'attendance:import_preview':  'attendance.upload',
    'attendance:import_detail':   ('attendance.upload', 'attendance.query'),

    # 报表与规则
    # 报表的生成与下载是同一个视图（POST 直接回传文件），所以只有一个路由
    'attendance:report_generate': 'attendance.report',
    'attendance:rule_config':     'attendance.rule_manage',
    'attendance:rule_save':       'attendance.rule_manage',
}


def can_view_import_all(user):
    """
    能否查看**全部**人的导入记录。

    有 attendance.query 或管理员 → 可以看全部；
    只有 attendance.upload 的操作员 → 只能看自己的（见 can_view_import）。
    """
    if not user.is_authenticated:
        return False
    return is_admin(user) or has_permission(user, 'attendance.query')


def can_view_import(user, record):
    """
    能否查看某一次导入的报告/预览。

    规则（按优先级）：
      1. 管理员 → 可以
      2. 有 attendance.query → 可以看全部（HR 需要核对所有人的导入）
      3. 有 attendance.upload 且是自己上传的 → 可以看自己的
      4. 其余 → 不可以

    ⚠️ 为什么要有第 3 条：只授了"上传"权限的操作员，导入后必须能看到自己的
       导入报告（否则传失败了也不知道原因）；但不该看到别人的。
    """
    if not user.is_authenticated:
        return False
    if can_view_import_all(user):
        return True
    if has_permission(user, 'attendance.upload') and record.uploaded_by_id == user.id:
        return True
    return False


def can_delete_import(user, record):
    """
    能否删除某一次导入。

    删除是破坏性操作，且会连带删掉该文件导入的考勤/请假数据，因此比"查看"更严：
      · 管理员，或有 attendance.upload 且是自己上传的 → 可以
      · 只有 attendance.query → **不可以**（能看不能删）
    """
    if not user.is_authenticated:
        return False
    if is_admin(user):
        return True
    return (has_permission(user, 'attendance.upload')
            and record.uploaded_by_id == user.id)
