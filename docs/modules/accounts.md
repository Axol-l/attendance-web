# accounts — 用户 · 权限 · 审计日志

## 模块职责

本模块管理登录态、用户角色与细粒度权限分配，并提供全局审计日志的写入与查询。

它是整个系统的权限判定基础：`attendance`、`backups` 等模块的视图装饰器、上下文处理器都直接调用本模块的 `accounts/decorators.py`，考勤模块的权限矩阵（`attendance/permissions.py`）也是基于这里的 `is_admin()` / `has_permission()` 构建的。

审计日志由本模块统一写入（`accounts/services.py::log_action`），考勤与备份模块只负责在关键操作处调用它。

## 文件清单

| 文件 | 说明 |
|---|---|
| `accounts/__init__.py` | 包标识，无内容 |
| `accounts/apps.py` | `AccountsConfig`；`ready()` 中导入 `accounts.signals` 以注册信号 |
| `accounts/models.py` | 三个模型：`UserProfile`（角色）、`UserPermission`（细粒度权限码）、`AuditLog`（审计日志） |
| `accounts/views.py` | 登录/登出/改密、用户增删改查、操作日志列表与详情；集中定义 `PERMISSION_CODES`、`ACTION_LABELS`、`MODULE_LABELS` 三张映射表 |
| `accounts/urls.py` | `app_name = 'accounts'`，9 条路由（见「页面路由」） |
| `accounts/decorators.py` | 权限判定单一入口：`is_admin()`、`has_permission()`、`deny()`、`permission_required(*codes)`、`admin_permission_required`；另导出别名 `_is_admin` / `_has_permission` / `_deny` |
| `accounts/services.py` | `get_client_ip(request)`（支持 `X-Forwarded-For`）、`log_action(request, action, module, description, target_table=None, target_id=None)` |
| `accounts/context_processors.py` | `user_permissions(request)` 注入模板变量 `user_perms`；`PERMISSION_TEMPLATE_KEYS` 定义模板键名与权限码的映射 |
| `accounts/signals.py` | `post_save(User)` 两个接收函数：创建用户时自动建 `UserProfile`，保存用户时同步保存 `profile` |
| `accounts/admin.py` | Django 后台注册：`UserAdmin`（内联 `UserProfileInline`，并 `unregister(User)` 后重注册）、`UserProfileAdmin`、`UserPermissionAdmin`、`AuditLogAdmin`（全部字段只读） |
| `accounts/templatetags/auth_tags.py` | `{% has_perm user 'attendance.upload' %}` 模板标签，内部转调 `decorators._has_permission` |
| `accounts/tests.py` | 空脚手架，仅 `from django.test import TestCase`，本模块行为由 `attendance/tests.py` 覆盖 |
| `accounts/migrations/0001_initial.py` | 唯一迁移，一次性建 `user_profiles`、`audit_logs`、`user_permissions` 三张表 |

## 模型/表结构

### user_profiles / user_permissions / audit_logs

**`UserProfile`（`db_table = 'user_profiles'`）** — 用户扩展角色，与 `auth_user` 一对一。

| 字段 | 类型/约束 |
|---|---|
| `user` | `OneToOneField(User, on_delete=CASCADE, related_name='profile')`；用户被删则档案随删 |
| `role` | `CharField(max_length=20)`，`choices` 为 `super_admin` / `admin` / `user`，`default='user'`，`db_index=True` |
| `created_by` | `ForeignKey(User, on_delete=SET_NULL, null=True, blank=True, related_name='created_profiles')`；创建人被删则置 NULL，档案保留 |
| `created_at` | `DateTimeField(default=timezone.now)` |
| `updated_at` | `DateTimeField(auto_now=True)` |

便利属性（均为 Python 层 `@property`，不入库）：`is_super_admin`（`role == 'super_admin'` 或 `user.is_superuser`）、`is_admin`（`role in ('super_admin', 'admin')` 或 `user.is_superuser`）、`can_access_admin`（等于 `is_super_admin`）。

档案由信号自动创建：`post_save(User, created=True)` 时 `UserProfile.objects.get_or_create(user=instance)`，因此新建用户不需要显式建档案。

**`UserPermission`（`db_table = 'user_permissions'`）** — 普通用户的细粒度权限明细，一权限一行。

| 字段 | 类型/约束 |
|---|---|
| `user` | `ForeignKey(User, on_delete=CASCADE, related_name='custom_permissions')` |
| `permission_code` | `CharField(max_length=50)`，**无 `choices`**，可写入任意字符串 |
| `created_by` | `ForeignKey(User, on_delete=SET_NULL, null=True, blank=True, related_name='granted_permissions')`；授权人被删则置 NULL |
| `created_at` | `DateTimeField(auto_now_add=True)` |

唯一约束：`unique_together = ('user', 'permission_code')`。除主键外无其他自定义索引；`related_name` 取 `custom_permissions`，与 Django 自带的 `User.user_permissions` 多对多管理器区分（模板 `user_list.html` 用 `u.custom_permissions.all` 渲染）。

该表**只在 `role='user'` 时生效**——`is_admin(user)` 为真时直接放行，不再查表（见 `decorators.py`）。

**`AuditLog`（`db_table = 'audit_logs'`，`ordering = ['-created_at']`）** — 全局审计日志，无外键级联删除风险。

| 字段 | 类型/约束 |
|---|---|
| `user` | `ForeignKey(User, on_delete=SET_NULL, null=True, blank=True, related_name='audit_logs')`；操作人被删则日志保留、`user` 置 NULL |
| `username` | `CharField(max_length=150)`，**冗余存用户名**，与 `user` 外键独立，因此用户被删后日志仍能显示是谁操作的 |
| `action` | `CharField(max_length=50)`，`db_index=True` |
| `module` | `CharField(max_length=50, blank=True)`，`db_index=True` |
| `target_table` | `CharField(max_length=50, blank=True, null=True)` |
| `target_id` | `CharField(max_length=50, blank=True, null=True)`；`log_action` 写入前转成字符串 |
| `description` | `TextField(blank=True, null=True)` |
| `ip_address` | `GenericIPAddressField(blank=True, null=True)` |
| `user_agent` | `CharField(max_length=500, blank=True, null=True)` |
| `created_at` | `DateTimeField(auto_now_add=True, db_index=True)` |

索引情况：`action`、`module`、`created_at` 单列索引（`db_index=True`）；无复合索引、无 `Meta.indexes`。

## 权限模型

### 三个角色（super_admin / admin / user）

判定依据分散在 Python 层而非数据库约束，**`is_superuser` 优先于 `role` 字段**：

| 角色 | `UserProfile.role` | 同时设置的 Django 字段 | 判定结果 |
|---|---|---|---|
| super_admin | `'super_admin'` | `is_superuser=True`、`is_staff=True`（由 `user_create` / `user_update` 同步写入） | `is_admin()` → True，`is_super_admin` → True，可进 Django 后台 |
| admin | `'admin'` | `is_superuser=False`、`is_staff=False` | `is_admin()` → True |
| user | `'user'` | — | 需逐条查 `UserPermission` |

关键实现细节：

- `decorators.is_admin(user)`：`user.is_superuser` 为真直接返回 True，否则返回 `profile.is_admin`。因此**把某人的 `is_superuser` 置 True（含 Django 后台手工操作）等价于提权为管理员**，不必改 `role`。
- `UserProfile.is_admin` 把 `super_admin` 与 `admin` 一视同仁，两者权限上无差别；`super_admin` 的额外能力只有三点：可进 Django 后台、可创建/设置 `super_admin` 角色（`user_create` / `user_update` 中的显式校验）、可在用户管理页看到编辑超管的按钮（模板 `user_list.html`）。
- 普通用户若完全没有 `UserPermission` 记录，除首页外所有考勤页面都会被拒。
- 角色从 `user` 改为 `admin` / `super_admin` 时，该用户的 `UserPermission` 记录会被清空（`user_update` 中的 `elif role in ('admin', 'super_admin')` 分支）。

### 5 个权限码

权限码定义在 `accounts/views.py::PERMISSION_CODES`（用户管理页勾选框的数据源），模板键名映射在 `accounts/context_processors.py::PERMISSION_TEMPLATE_KEYS`。

| 权限码 | 授予的能力 | 模板键名 |
|---|---|---|
| `attendance.upload` | 上传考勤/请假数据；查看与删除**自己上传的**导入记录（重传覆盖同一周期也在此列） | `user_perms.attendance_upload` |
| `attendance.query` | 查询考勤数据：考勤概览、每日明细、请假记录、月度汇总；查看**全部**人的导入记录与导入报告 | `user_perms.attendance_query` |
| `attendance.export` | 导出查询结果（**当前无任何视图或模板使用，见「已知限制」**） | `user_perms.attendance_export` |
| `attendance.report` | 生成并下载月度考勤报表 | `user_perms.attendance_report` |
| `attendance.rule_manage` | 修改考勤规则（规则配置页与保存接口） | `user_perms.attendance_rule_manage` |

没有独立的"删除"权限码：删除导入跟随 `attendance.upload`，且只允许删自己上传的；此规则不在装饰器里，而在 `attendance/permissions.py::can_delete_import()` 中由视图显式调用。逐页、逐角色的完整对照由测试逐条覆盖，见 `attendance/tests.py` 的 `PermissionMatrixTests` 与 `PermissionBoundaryTests`，本文不重复。

### 新增权限码时必须同步修改的四处

这是本模块最容易出错的改动，四处不同步会出现"前端看得见但后端拒绝""前端看不见但直接敲 URL 能进"两类错位。

| # | 位置 | 漏改的后果 |
|---|---|---|
| 1 | `accounts/context_processors.py` 的 `PERMISSION_TEMPLATE_KEYS` | 新权限码没有模板键名，`{% if user_perms.xxx %}` 取值恒为空（Django 模板对不存在的键静默失败、不报错）→ 即使后端已放行，导航与按钮也不显示，功能等于不可用 |
| 2 | `accounts/views.py` 的 `PERMISSION_CODES` | 用户管理页的新增/编辑弹窗里没有该权限码的勾选框 → 无法给普通用户授权，只能靠管理员绕过，`attendance.export` 就是这种状态 |
| 3 | 使用该权限码的视图上的 `@permission_required(...)` 装饰器 | 后端没有拦截：任何已登录用户直接敲 URL 即可访问（越权漏洞）；反过来若只加了装饰器而漏了第 1 处，则按钮消失但 URL 能进 |
| 4 | `attendance/permissions.py` 的 `PAGE_PERMISSIONS` | 权限的"单一事实来源"缺条目，文档与实现脱节。注意 `PermissionMatrixTests.test_page_permission_matrix_is_self_consistent` 只校验已有条目能在 `urls.py` 中反解，**无法发现漏写的条目**，漏了不会报错 |

另外应补一条 `PermissionMatrixTests` 用例：该测试类是权限矩阵的逐条兜底，新增码不被用例覆盖时，回归没有防线。

## 关键实现

### 装饰器链（@login_required → @permission_required(...)）

装饰顺序在 `attendance/views.py` 顶部注释中固定为 `@login_required` 在上、`@permission_required(...)` 在下，即 `login_required` 位于最外层最先执行。`accounts/views.py` 同样遵循该顺序，写操作再叠一层 `@require_http_methods(["POST"])`（`user_create` / `user_update` / `user_delete` / `change_password`），因此非 POST 请求会得到 405。

`accounts/decorators.py::permission_required(*permission_codes)` 的行为：

- 未登录 → `redirect('accounts:login')`（装饰器内部自带判断，不依赖外层 `login_required`）。
- **管理员绕过**：`is_admin(request.user)` 为真直接放行，不查 `UserPermission` 表。
- **多权限码 any-of**：`any(_has_permission(request.user, c) for c in codes)`，满足任一即放行。用途是配合"按归属过滤"——例如导入报告页同时接受 `upload` 与 `query`，只授 `upload` 的人看自己的导入报告、有 `query` 的人看全部；装饰器只做粗粒度放行，细粒度判断放在视图里（`attendance/permissions.py::can_view_import` / `can_view_import_all` / `can_delete_import`）。
- 拒绝时走 `deny(request)`：请求头 `X-Requested-With == 'XMLHttpRequest'` → 返回 `JsonResponse({'success': False, 'message': '没有权限'})`（前端 fetch 拿到的就是该 JSON）；否则 `messages.error` 写入"您没有权限访问此页面"并 `redirect('home')`。

`admin_permission_required` 是只判 `is_admin` 的简化版，用于用户管理与操作日志四个视图，不接受权限码。

### 审计日志 log_action()

定义在 `accounts/services.py`，签名 `log_action(request, action, module, description, target_table=None, target_id=None)`。

写入字段与来源：

| 字段 | 来源 |
|---|---|
| `user` | `request.user`，未登录时写 `None` |
| `username` | `request.user.username`，未登录时字面量 `'anonymous'` |
| `action` / `module` / `description` | 调用方传入 |
| `target_table` / `target_id` | 调用方传入；`target_id` 非空时转成 `str` 再存 |
| `ip_address` | `get_client_ip()`：优先取 `HTTP_X_FORWARDED_FOR` 逗号分隔的第一段并 `strip()`，否则 `REMOTE_ADDR` |
| `user_agent` | `HTTP_USER_AGENT`，截断到 500 字符 |
| `created_at` | `auto_now_add` |

写库异常被整体捕获，只 `logger.exception('写入审计日志失败: ...')` 不抛出：**审计失败不影响主业务流程**，代价是日志可能静默缺失（`accounts` logger 在 `settings.LOGGING` 中挂 console/file/error_file，级别 INFO）。

调用方（实际 `log_action(` 调用点）：`accounts/views.py` 7 处、`attendance/views.py` 9 处、`backups/views.py` 3 处。

`action` 取值分两类，均从代码核实：

- **实际写入的码（17 个）**：`LOGIN`、`LOGIN_FAILED`、`LOGOUT`、`PASSWORD_CHANGE`、`USER_CREATE`、`USER_UPDATE`、`USER_DELETE`（`accounts/views.py`）；`UPLOAD`、`UPLOAD_FAILED`、`ATTENDANCE_FILE_DELETE`、`ATTENDANCE_LEAVE_LIST_VIEW`、`ATTENDANCE_SUMMARY_VIEW`、`ATTENDANCE_SUMMARY_COMPARE`、`ATTENDANCE_REPORT_GENERATE`、`ATTENDANCE_RULE_UPDATE`（`attendance/views.py`）；`BACKUP_CREATE`、`BACKUP_DELETE`（`backups/views.py`）。
- **仅在 `accounts/views.py::ACTION_LABELS` 中定义了中文标签、当前没有任何调用点（3 个）**：`DATA_DELETE`、`EXPORT`、`BACKUP_RESTORE`。

`module` 实际写入值：`auth`、`accounts`、`attendance`、`backups`。`MODULE_LABELS` 另定义了未被使用的 `core`。

### context processor 注入 user_perms

`accounts/context_processors.py::user_permissions` 在 `config/settings.py` 的 `TEMPLATES[0].OPTIONS.context_processors` 中注册（`'accounts.context_processors.user_permissions'`），对所有模板生效。

- 未登录 → `{'user_perms': {}}`，模板里所有键取空值。
- 管理员（`user.is_superuser` 或 `profile.is_admin`）→ 5 个键全部 `True`，另加 `is_admin = True`，不查 `UserPermission` 表。
- 普通用户 → 查一次 `UserPermission` 的 `permission_code` 集合，逐键判断 `code in codes`，`is_admin = False`。

键名来源：`PERMISSION_TEMPLATE_KEYS` 把权限码里的点号换成下划线（`attendance.upload` → `attendance_upload`），模板必须使用下划线形式（点号在 Django 模板中会被解析为属性查找，取不到值）。

模板中的实际用法（`templates/` 下共 16 处引用）：

| 位置 | 用法 |
|---|---|
| `base.html` | 导航栏：`attendance_upload` 控制"数据上传"菜单；`attendance_query` 控制"考勤查询"下拉（每日明细/请假记录/月度汇总）；`attendance_report` 控制"生成报表"；`attendance_query or attendance_upload` 控制导入记录入口，且只有 `query` 时标题显示"导入记录"、否则显示"我的上传"；`attendance_rule_manage or is_admin` 控制"管理功能"，其下用户管理/操作日志/备份管理仅 `is_admin` 可见 |
| `home.html` | 首页功能卡片：`attendance_upload` / `attendance_query` / `attendance_report` |
| `attendance/file_list.html` | 上传按钮与"操作"列仅 `attendance_upload` 时渲染 |
| `attendance/overview.html` | 空数据提示里引导到上传页 |
| `attendance/monthly_summary.html` | 报表入口按钮仅 `attendance_report` 时渲染 |

另有模板标签 `{% has_perm user '<码>' %}`（`accounts/templatetags/auth_tags.py`），按权限码实时查询，不依赖 context processor。

## 页面路由

挂载点为 `config/urls.py` 的 `path('accounts/', include('accounts.urls'))`。

| URL | 视图 | 权限要求 |
|---|---|---|
| `/accounts/login/` | `login_view` | 无（已登录访问会 302 跳首页）；POST 校验 `next` 是否为本站地址，防开放重定向 |
| `/accounts/logout/` | `logout_view` | 登录即可（`@login_required`）；先写 `LOGOUT` 日志再登出 |
| `/accounts/users/` | `user_list` | 管理员（`@admin_permission_required`） |
| `/accounts/users/create/` | `user_create` | 管理员 + 仅 POST；创建 `super_admin` 还需调用者 `is_superuser` |
| `/accounts/users/<int:pk>/update/` | `user_update` | 管理员 + 仅 POST；非超管不能编辑超管，也不能把角色设为 `super_admin` |
| `/accounts/users/<int:pk>/delete/` | `user_delete` | 管理员 + 仅 POST；不能删自己；非超管不能删超管；有关联考勤数据时 `ProtectedError` → 返回失败提示 |
| `/accounts/change-password/` | `change_password` | 登录即可 + 仅 POST；只能改自己的密码，需校验原密码 |
| `/accounts/logs/` | `audit_log_list` | 管理员；支持用户名/操作人/操作类型/模块/描述关键字/起止日期筛选，每页 50 条，渲染已选条件 chip |
| `/accounts/logs/<int:pk>/detail/` | `audit_log_detail` | 管理员；返回单条日志详情 JSON |

未登录访问以上任一页面（`login/` 除外）→ 302 到 `/accounts/login/`（`settings.LOGIN_URL = '/accounts/login/'`）。已登录但无权限 → 302 回首页并带错误消息（AJAX 请求返回 `{"success": false, "message": "没有权限"}`）。

返回类型：`login_view` / `logout_view` 返回重定向或直接渲染页面；`user_create` / `user_update` / `user_delete` / `change_password` 四个写接口返回 JSON，异常时把 `str(e)` 直接放进 `message` 字段透传给前端。

## 边界与测试

本模块自身没有测试用例（`accounts/tests.py` 是空的脚手架），权限与审计的边界全部由 `attendance/tests.py` 覆盖。合计 **35 个用例**：

| 测试类 | 起始行 | 用例数 | 覆盖的边界 |
|---|---|---|---|
| `PermissionMatrixTests` | 1546 | **20** | 按角色逐页验证：只读（`query`）可读四个查询页与导入列表、但被拒于上传/报表/规则页（302 且 `Location == '/'`）、POST 上传也不落库；只上传（`upload`）能打开上传页、列表只见自己的文件且标题变"我的上传"、能看自己的导入报告但看别人的报告被弹回列表页、进不了查询页；删除比查看更严——只读用户不能删、上传员不能删别人的、能删自己的、无权限者删不动；报表员只能开报表页；规则管理员只能管规则；无权限用户 8 个页面全被拒；管理员 8 个页面全通过；导航文案与后端权限一致（`数据上传` / `我的上传` / `每日明细` / `月度汇总` 的有无）；`PAGE_PERMISSIONS` 每个条目都能在 `urls.py` 反解 |
| `PermissionBoundaryTests` | 872 | **9** | 匿名访问四个查询页 → 302 且 `Location` 含登录页；已登录但无权限 → 302（而非跳登录页）；有 `query` 无 `upload` 时上传页仍被拒、查询页放行；超管 7 个页面全通过；概览页"请假记录"卡片指向请假记录页（防回归）；请假记录的按状态/类型筛选、按审批编号精确与片段匹配、空结果提示、条件组合；翻页链接保留审批编号；命中数超上限时不渲染全表（`over_limit`） |
| `AuditTrailTests` | 1109 | **2** | 规则变更写入 `ATTENDANCE_RULE_UPDATE` 日志、`module == 'attendance'`、描述中含被改字段名；删除导入写入 `ATTENDANCE_FILE_DELETE` 且记录确实被删除 |
| `AuditLabelCoverageTests` | — | **4** | 扫源码取全部 `log_action(request, 'CODE')` 调用，断言每个 action 码都在 `ACTION_LABELS` 里登记（漏登记会让日志页显示英文码）、标签非空且含中文、码名为大写蛇形，并反向确认正则本身没失效 |

用例数按各类中 `def test_` 方法实际计数得出。`docs/权限矩阵.md` 里原先写的"14 个用例"已同步更正为 20 个。

## 已知限制

以下均为代码中客观存在的现状：

1. **`attendance.export` 预留未实现**：该权限码已出现在 `PERMISSION_CODES` 与 `PERMISSION_TEMPLATE_KEYS` 中，但全项目没有 `@permission_required('attendance.export')`，也没有模板使用 `user_perms.attendance_export`。授予它目前不产生任何访问效果。
2. **审计日志中 3 个标签没有对应写入点**：`ACTION_LABELS` 定义了 `DATA_DELETE`、`EXPORT`、`BACKUP_RESTORE` 的中文标签，但代码中没有任何 `log_action` 使用这三个码。其中 `BACKUP_RESTORE` 对应的还原功能本身也未实现（`backups/views.py` 只有列表、创建、下载、删除四个视图）。
3. **2 个实际写入的码没有中文标签**：`ATTENDANCE_LEAVE_LIST_VIEW` 与 `ATTENDANCE_SUMMARY_COMPARE` 未在 `ACTION_LABELS` 中登记，列表页与详情接口用 `ACTION_LABELS.get(log.action, log.action)` 兜底，因此这两类日志直接显示英文码。
4. **授权变更无法从日志还原**：`USER_CREATE` / `USER_UPDATE` 的 `description` 只包含用户名与角色（例如"修改用户 张三"），不记录这次勾选/取消了哪些权限码，也没有独立的权限变更日志。
5. **`permission_code` 无取值校验**：模型字段是无 `choices` 的 `CharField(max_length=50)`，`user_create` / `user_update` 把 `request.POST.getlist('permissions')` 原样入库，写错或伪造的权限码不会报错，只是永远匹配不上任何装饰器。数据库层唯一约束仅为 `(user, permission_code)`。
6. **登录无失败次数限制与锁定**：`login_view` 只做 `authenticate` + `messages.error` + 记 `LOGIN_FAILED`，无验证码、无锁定、无速率限制。
7. **密码无强度校验**：`user_create`、`user_update`、`change_password` 都直接用 `create_user` / `set_password`，未调用 Django 的密码校验器（`validate_password`）。
8. **首页不受权限码约束**：`core.views.home` 只有 `@login_required`。无任何权限的用户被拒绝后正是被重定向到 `/`，该页仍展示考勤、请假、导入记录的总条数与最近 5 条导入文件名。
9. **无行级数据范围**：`UserPermission` 只回答"有没有某个权限码"，没有部门或人员维度的过滤条件。
10. **审计日志无保留期与归档**：代码中只有写入与查询（列表、详情、Django 后台只读页），没有清理、轮转或导出逻辑。
11. **`core` 模块标签未被使用**：`MODULE_LABELS` 中定义了 `core`，但没有任何调用点以 `core` 作为 `module` 参数。
