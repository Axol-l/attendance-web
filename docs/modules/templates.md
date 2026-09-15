# templates — 模板体系

模板统一放在项目根的 `templates/`（`settings.TEMPLATES['DIRS'] = [BASE_DIR / 'templates']`），
不使用 app 目录内的模板；`APP_DIRS = False`，app 级目录改由 `app_directories.Loader` 显式声明。
`DEBUG=False` 时套 `cached.Loader`，`DEBUG=True` 时直读磁盘（改模板刷新即生效）。

## 目录结构

```
templates/
├── base.html                     全站基模板：导航、消息提示、修改密码弹窗、公共 JS
├── home.html                     登录后首页：三张统计卡片 + 快捷操作 + 最近导入 5 条
├── attendance/
│   ├── overview.html             考勤概览：统计卡片（可点击）、账期快捷入口、最近导入 10 条
│   ├── upload.html               数据上传：日考勤汇总表 / 请假单据两个上传卡片 + 同账期冲突确认
│   ├── file_list.html            导入记录列表：按类型/账期筛选、分页、删除
│   ├── import_detail.html        导入报告：概要、Sheet 统计、错误明细、被排除人员、警告
│   ├── import_preview.html       解析预览：只解析不入库，展示表头识别与数据样本
│   ├── daily_list.html           每日明细：姓名/部门/日期区间筛选、状态色块、分页
│   ├── leave_list.html           请假记录：审批编号/类型/账期/审批状态筛选、分页
│   ├── monthly_summary.html      月度汇总（Web 版 Sheet2）+ 跨月对比
│   ├── report.html               生成考勤表：报表参数、内容与口径说明、两步式生成下载
│   └── rule_config.html          考勤规则配置：时长/应出勤、排除规则、计算口径
├── accounts/
│   ├── login.html                登录页（独立页面，不继承 base.html）
│   ├── user_list.html            用户管理：列表 + 新增/编辑弹窗
│   └── audit_log_list.html       操作日志：基础/高级筛选、筛选条件 chips、详情弹窗
└── backups/
    └── backup_list.html          数据备份：立即备份、备份历史、分页、删除
```

共 16 个 HTML 模板。全部由 `render()` 直接渲染，公共部分只靠 `base.html` 继承。

**前端资源全部随项目分发**（`static/vendor/`），页面里**没有任何公网 CDN 外链** ——
本系统可能部署在无外网的内网环境，依赖 CDN 会直接掉样式与图标。

| 资源 | 版本 | 路径 |
|---|---|---|
| Bootstrap CSS | 5.3.0 | `static/vendor/bootstrap/bootstrap.min.css` |
| Bootstrap JS bundle | 5.3.0 | `static/vendor/bootstrap/bootstrap.bundle.min.js` |
| Bootstrap Icons | 1.10.0 | `static/vendor/bootstrap-icons/bootstrap-icons.css` + `fonts/` |

两个入口模板（`base.html` 与独立的 `accounts/login.html`）都用 `{% static %}` 引用它们。

> ⚠️ 两处容易踩的坑，各有测试守住（见 `attendance/tests.py::OfflineAssetsTests`）：
> 1. **只下图标 CSS、忘下 `fonts/` 目录** —— 页面不报错，图标静默变成空白方块。
> 2. **`.min` 文件末尾的 `sourceMappingURL` 注释**指向未分发的 `.map`，
>    生产用的 `CompressedManifestStaticFilesStorage` 会因此**让 collectstatic 直接失败**，
>    而开发模式完全看不出来。该注释已被 `tools/_strip_sourcemap.py` 去掉。

## base.html

### 布局与区块

`{% block %}` 共 4 个，子模板按需覆盖：

| 区块 | 位置 | 用途 |
|------|------|------|
| `title` | `<head>` 内 `<title>` | 默认「考勤管理系统」；子模板写成「页面名 - 考勤管理系统」 |
| `extra_css` | 内联 `<style>` 之前 | 页面级 `<style>`（当前仅 `audit_log_list.html` 使用） |
| `content` | `<div class="content">` | 页面主体，唯一被所有子模板覆盖的区块 |
| `extra_js` | 末尾 bootstrap bundle 与公共脚本之后 | 页面级 `<script>` |

`base.html` 自身还包含：

- 内联 `<style>`：`body` 用 flex 撑满高度、`.content { flex: 1 }`、`footer` 样式；
  考勤状态色块 `.att-*`；
  概览页统计卡片的 `.stat-card-link` 悬浮效果。
- `footer`：`考勤管理系统 vx.0`（刻意不写年份 —— 写死的年份过一段时间就是错的）。
- 修改密码弹窗 `#changePasswordModal`：仅登录用户渲染，内含 `原密码 / 新密码 / 确认新密码`
  三个字段，确认按钮调用 `changePassword()`。

### 导航栏

整个已登录菜单包在 `{% if user.is_authenticated %}` 内；未登录只显示「登录」链接。

| 菜单项 | 目标路由 | 显示条件 |
|--------|----------|----------|
| 数据上传 | `attendance:upload` | `user_perms.attendance_upload` |
| 考勤查询（下拉） | — | `user_perms.attendance_query` |
| ├ 每日明细 | `attendance:daily_list` | 同上 |
| ├ 请假记录 | `attendance:leave_list` | 同上 |
| └ 月度汇总 | `attendance:monthly_summary` | 同上 |
| 生成报表 | `attendance:report_generate` | `user_perms.attendance_report` |
| 导入记录 / 我的上传 | `attendance:file_list` | `user_perms.attendance_query or user_perms.attendance_upload` |
| 管理功能（下拉） | — | `user_perms.attendance_rule_manage or user_perms.is_admin` |
| ├ 考勤规则配置 | `attendance:rule_config` | 同上（模板内二次判断） |
| ├ 用户管理 | `accounts:user_list` | `user_perms.is_admin` |
| ├ 操作日志 | `accounts:audit_log_list` | `user_perms.is_admin` |
| └ 备份管理 | `backups:backup_list` | `user_perms.is_admin` |

右上角用户名带角色徽章：`user.is_superuser` → 「超级管理员」（`bg-danger`）、
`user.profile.role == 'admin'` → 「管理员」（`bg-warning`）、其余 → 「用户」；
「后台管理」（Django admin）仅对 `user.is_superuser` 显示。

### escapeHtml() 辅助函数

定义在 `base.html` 末尾的内联 `<script>` 中，是**全局函数**：

```js
function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c => ({...}[c]));
}
```

存在的理由：考勤数据全部来自 Excel（含员工姓名、部门、审批单号），属**不可信输入**；
凡是要用 `innerHTML` / `insertAdjacentHTML` 拼接这类内容的地方，
必须先用它转义，否则构成存储型 XSS。

实际使用处：`accounts/audit_log_list.html` 的详情弹窗在**本页额外定义了一份同名函数**，
用模板字符串拼 HTML 时对 `created_at`、`username`、`user_agent`、`description` 等
每个字段逐一 `escapeHtml()`。其余页面走 Django 模板自动转义（服务端），无需该函数。

### 消息提示、csrf 与分页

- **消息提示**：`{% if messages %}` 循环渲染 `alert alert-{{ message.tags }}`，
  放在导航栏与 `.content` 之间。
  > 备注：settings 中没有配置 `MESSAGE_TAGS`，因此 `messages.error()` 渲染出的类名是
  > `alert-error`，而 Bootstrap 5.3 只有 `alert-danger` —— 该类名不会命中红色样式。
- **CSRF**：所有 POST 表单都带 `{% csrf_token %}`；`base.html` 用 `getCookie('csrftoken')`
  取出 token 作为全局常量 `csrftoken`，页面级 `fetch` 统一放 `X-CSRFToken` 头
  （`report.html` 另在 `FormData` 里补了一份 `csrfmiddlewaretoken`）。
- **子路径部署**：`const SCRIPT_PREFIX = '{{ request.META.SCRIPT_NAME|default:"" }}';`
  所有 `fetch` 的 URL 都拼上它，否则在 `/attendance` 这类子路径部署下会打到别的项目上。
  Django 侧对应 `FORCE_SCRIPT_NAME` 设置。
- **修改密码**：`changePassword()` 用 `FormData` 取值，先在前端比较两次新密码是否一致，
  再 `fetch` POST 到 `accounts:change_password`，成功后跳登录页。
- **分页**：没有公共分页组件，各页自行拼 `?page=N&...`。

## 权限控制显示

模板只从上下文处理器 `accounts/context_processors.user_permissions` 拿一个 `user_perms` 字典：

- 键是**下划线形式**的模板变量名（`attendance_upload`、`attendance_query`、
  `attendance_export`、`attendance_report`、`attendance_rule_manage`），
  值是点号形式的权限码（`attendance.upload` …），映射集中在
  `accounts/context_processors.PERMISSION_TEMPLATE_KEYS`。
- 额外的 `user_perms.is_admin`：`user.is_superuser` 或 `profile.is_admin` 为真时，
  所有键一次性置 `True` 并附 `is_admin = True`；未登录返回 `{'user_perms': {}}`。
- 因此模板里的写法固定为 `{% if user_perms.xxx %}` / `{% if user_perms.is_admin %}`，
  不直接读权限码字符串。

用到权限判断的位置（共 3 个模板 + base）：

| 模板 | 控制的元素 |
|------|-----------|
| `base.html` | 导航栏全部菜单项与「后台管理」入口 |
| `home.html` | 「快捷操作」里的上传 / 查询 / 生成报表按钮 |
| `attendance/overview.html` | 「还没有任何考勤数据」提示里的「数据上传」链接 |
| `attendance/file_list.html` | 右上角「上传新文件」按钮、「操作」列与删除按钮 |
| `attendance/monthly_summary.html` | 汇总区右上角的「生成报表」按钮（`period and user_perms.attendance_report`） |

> ⚠️ **隐藏按钮只是体验，不是安全边界。** 真正的准入由视图装饰器链
> `@login_required` → `@permission_required(...)` 决定；数据可见范围（上传者只看自己的导入）
> 由 `attendance/permissions.py` 的 `can_view_import()` / `can_view_import_all()` /
> `can_delete_import()` 决定。把按钮藏起来不会让 URL 变得不可访问。
>
> ⚠️ **单一事实来源是 `attendance/permissions.py`**（`PAGE_PERMISSIONS` 页面 → 权限码映射）。
> 它被视图、模板上下文处理器和测试共同引用。
> 新增权限码时必须同步改三处，否则会出现「前端按钮消失但后端仍放行」或反之：
> 1. `accounts/context_processors.py` 的 `PERMISSION_TEMPLATE_KEYS`
> 2. `accounts/views.py` 的 `PERMISSION_CODES`（用户管理页的勾选框）
> 3. 各视图的 `@permission_required` 装饰器
>
> 页面级映射的死条目由 `PermissionMatrixTests.test_page_permission_matrix_is_self_consistent`
> 兜底（逐条反向解析 `PAGE_PERMISSIONS`，解析不到就 fail）。

## 页面 → 模板 → 主要 context 变量

逐条核对 `attendance/views.py`、`accounts/views.py`、`backups/views.py`、`core/views.py` 的 `render()`
调用后整理（「权限码」列对装饰器；「可见性」列是视图内的额外判断）：

| 页面 | 模板文件 | 关键 context 变量 | 权限码 |
|------|----------|-------------------|--------|
| `/`（`core.views.home`） | `home.html` | `daily_count`、`leave_count`、`file_count`、`recent_files`（5 条） | 仅 `@login_required` |
| `/attendance/`（`overview`） | `attendance/overview.html` | `periods`（倒序去重账期）、`recent_files`（10 条）、`daily_count`、`leave_count` | `attendance.query` |
| `/attendance/upload/`（`upload`） | `attendance/upload.html` | `file_kinds`、`recent_files`（10 条）、`max_size_mb`、`allowed_extensions`、`rule`、`period_bounds`；冲突分支追加 `pending`、`existing`；回显追加 `form`（`file_kind`/`period`/`purge_existing`） | `attendance.upload` |
| `/attendance/imports/<pk>/preview/`（`import_preview`） | `attendance/import_preview.html` | `record`、`sheets`（每 Sheet：`sheet`/`max_row`/`max_col`/`merged`/`header_rows`/`leave_leaf_count`/`columns`/`unknown`/`samples`/`error`） | `attendance.upload`；可见性 `can_view_import()` |
| `/attendance/imports/<pk>/`（`import_detail`） | `attendance/import_detail.html` | `record`、`report`、`errors`、`warnings`、`excluded`、`sheets`、`purged` | `attendance.upload` 或 `attendance.query`；可见性 `can_view_import()` |
| `/attendance/imports/`（`file_list`） | `attendance/file_list.html` | `files`（Paginator 20/页）、`kind`、`period`、`file_kinds`、`bounds`、`can_see_all` | `attendance.upload` 或 `attendance.query`；范围由 `can_view_import_all()` 收敛 |
| `/attendance/daily/`（`daily_list`） | `attendance/daily_list.html` | `page`（100/页，超限为 `None`）、`total`、`over_limit`、`max_results`、`filters`（`name`/`department`/`date_from`/`date_to`）、`departments`、`bounds` | `attendance.query` |
| `/attendance/leave/`（`leave_list`） | `attendance/leave_list.html` | `page`、`total`、`over_limit`、`max_results`、`filters`（`name`/`approval_no`/`leave_type`/`period`/`status`）、`leave_types`、`periods`、`approved_count`、`resigned_count`、`bounds` | `attendance.query` |
| `/attendance/monthly/`（`monthly_summary`） | `attendance/monthly_summary.html` | `rows`、`period`、`periods`、`leave_columns`、`total_overtime`、`total_attend`、`compare_periods`、`comparison`（`periods`/`total_rows`/`per_person`） | `attendance.query` |
| `/attendance/report/`（`report_generate`） | `attendance/report.html` | `periods`、`rule`（四处 `render` 都只传这两个） | `attendance.report` |
| `/attendance/rules/`（`rule_config`） | `attendance/rule_config.html` | `rule`、`exclude_modes` | `attendance.rule_manage` |
| `/accounts/login/`（`login_view`） | `accounts/login.html` | 无业务 context（仅上下文处理器注入的 `messages`） | 匿名可访问 |
| `/accounts/users/`（`user_list`） | `accounts/user_list.html` | `users`、`permission_codes` | `@admin_permission_required` |
| `/accounts/logs/`（`audit_log_list`） | `accounts/audit_log_list.html` | `logs`（分页）、`actors`、`action_choices`、`module_choices`、`filters`、`active_chips`、`query_string`、`advanced_open` | `@admin_permission_required` |
| `/backups/`（`backup_list`） | `backups/backup_list.html` | `backups`（Paginator 20/页） | `@admin_permission_required` |

另有三个只返回 JSON、不渲染模板的接口：`attendance:rule_save`、`accounts:user_create` /
`user_update` / `user_delete` / `change_password` / `audit_log_detail`、
`backups:create_backup` / `delete_backup`。

> 备注：`backups/backup_list.html` 里显示「备份文件保留 `{{ retention_days }}` 天」，
> 但 `backup_list` 视图只传了 `backups`，`retention_days` 在页面上渲染为空；
> 该值在 `config/settings.py` 中以 `BACKUP_RETENTION_DAYS = 30` 存在，由 `backups/backup.py` 读取。

## 交互实现

统一的形态：**服务端渲染 + 少量原生 JS**。页面数据全部由 Django 渲染，
JS 只负责「提交、等待、反馈、弹窗」，不接管列表渲染（除日志详情弹窗外）。
除 Bootstrap bundle 外无第三方库。

| 页面 | 方式 | 说明 |
|------|------|------|
| 数据上传 | **原生表单 POST**（非 fetch） | 两个卡片各一个 `form.upload-form`，`enctype="multipart/form-data"`，隐藏域带 `file_kind`；提交时禁用按钮并换成 spinner、显示进度条容器、用 `setInterval` 每秒刷新「已用 N 秒」。注释已说明：解析在服务端、浏览器没有可用的解析进度事件，所以给的是**明确的进行中反馈 + 已用时间**。表单照常提交，结束后跳转导入报告页。 |
| 同账期冲突确认 | 三个表单按钮 | 检测到该账期已有数据且未勾选覆盖时，同页渲染确认块：「合并」（`reuse_id`）／「整账期覆盖」（`reuse_id` + `purge_existing=on`，带 `onsubmit="return confirm(...)"`）／「取消」（POST 到 `attendance:file_delete` 删掉暂存文件）。 |
| 生成报表 | **两步式 fetch** | ① `submit` 被 `preventDefault()`，`FormData` POST 到 `attendance:report_generate`，带 `X-CSRFToken`，期望 JSON；期间按钮 spinner + 状态 alert 每秒更新「已用 N 秒」。② 成功后显示文件名与 KB 大小，并 `window.location.href = SCRIPT_PREFIX + data.url` 触发下载（`GET ?period=…&download=1` → `FileResponse` 附件）。失败/异常都在页内显示并复位按钮。 |
| 规则配置 | `fetch` + `FormData` | 拦截 `submit`，POST 到 `attendance:rule_save`，`alert(data.message)`，成功后 `location.reload()`。 |
| 用户管理 | `fetch` + Bootstrap Modal | 新增/编辑放在 `#addModal` / `#editModal` 里；`createUser()` / `updateUser()` 提交 `FormData` 后 `json()`；`deleteUser()` 先 `confirm` 再提交，成功后直接移除该行。`togglePerms()` 按角色切换权限勾选框的显示（`user` 才显示）。上一行「编辑」按钮把权限串内联进 `onclick`，`editUser()` 再按 `' - '` 拆分取最后一段作为权限码勾选 —— 依赖该拼接格式。更新/删除 URL 用反引号拼 `SCRIPT_PREFIX + '/accounts/users/<id>/...'`。 |
| 操作日志 | `fetch` + 折叠面板 + Modal | 高级筛选是 Bootstrap `collapse`，`aria-expanded` 与 `.show` 由 `advanced_open` 决定；已生效条件渲染成 chips，每个 chip 的 `href="?{{ chip.remove_qs }}"` 用于**移除单个条件**；翻页链接拼 `?page=N&{{ query_string }}`。点「详情」调 `showLogDetail(id)`，`fetch` JSON 后用 `innerHTML` + `escapeHtml()` 渲染到 `#logDetailModal`。 |
| 数据备份 | `fetch` | 「立即备份」POST 到 `backups:create_backup`，`alert` 后 `location.reload()`；删除走 `DELETE_URL_TEMPLATE = '{% url "backups:delete_backup" 0 %}'`，再用 `replace(/0\/$/, id + '/')` 替换占位（注释说明是为了避免手拼路径）。 |
| 各类筛选表单 | 纯 `method="get"` 表单 | `file_list`（类型 + 账期）、`daily_list`（姓名 + 部门 + 起止日期）、`leave_list`（姓名 + 审批编号 + 类型 + 账期 + 审批状态）、`monthly_summary`（账期 + 多选对比账期）、`audit_log_list`（关键词 + 高级筛选）。每个表单都配一个指向无参 URL 的「重置」链接。 |
| 分页保留筛选条件 | 手拼查询串 | `file_list`：`?page=N&kind=…&period=…`；`daily_list`：`?page=N&name=…&department=…&date_from=…&date_to=…`（文本项带 `|urlencode`）；`leave_list`：五个条件全部拼上（文本项 `|urlencode`）；`audit_log_list`：`?page=N&{{ query_string }}`；`backup_list`：只有 `?page=N`（无筛选）。 |
| 输入范围提示 | 服务端算出的 `min`/`max` | 日期框用 `bounds.date_min/date_max`，月份框用 `period_bounds.month_min/month_max`（来自 `attendance/utils.py`，按库中真实数据推导），避免选出必然空结果的区间。 |
| 删除确认 | `onsubmit="return confirm(...)"` | `file_list` 的删除按钮提示「删除后该文件导入的考勤/请假数据会一并移除」。 |
| 部门输入提示 | `<datalist id="deptList">` | `daily_list` 的部门输入框配 `list="deptList"`，候选来自 `departments`。 |

跨月对比是纯服务端计算：`monthly_summary` 视图把 5 项总体指标与逐人三列
（出勤 / 加班H / 请假天）**格式化好**再传模板，模板只做渲染。
视图注释说明原因：Django 模板不支持「带参数的过滤器变量」，也不该在模板里做字典查找与格式判断。

<!-- ## 前端禁忌

1. **模板里不能残留 `{%`、`{{`、`{#` 之类的语法到输出里。**
   `attendance/tests_report.py::ReportDownloadViewTests.test_report_page_script_has_no_serverside_leftovers`
   对此有断言：

   ```python
   for marker in ('{%', '{{', '{#'):
       self.assertNotIn(marker, html, f'页面里残留了未渲染的 {marker}')
   ```

   未闭合或写坏的模板标记不会被 Django 报错，而是原样发给浏览器（典型位置就是内联 `<script>`），
   在页面上表现为乱码或脚本直接报错。

2. **内联 `<script>` 里不要写长篇中文 `//` 注释。**
   JS 注释会原样发给浏览器，在「查看源码」里变成与页面无关的文字。同一测试用例守住这条：先正则取出含 `reportForm` 的脚本块，
   再断言不存在超过 30 字符的 `//` 行：

   ```python
   prose = [ln.strip() for ln in body.splitlines()
            if ln.strip().startswith('//') and len(ln.strip()) > 30]
   self.assertEqual(prose, [], f'脚本里还有开发期注释：{prose[:3]}')
   ```

   即：保留短的分隔线注释可以，成段中文说明不行（说明应留在 `.py` 或本文档里）。

其余几条由代码注释明确记录、改动时容易踩的：

3. **fetch 的 URL 必须拼 `SCRIPT_PREFIX`。** `base.html` 注释原文：Nginx 路径前缀（如
   `/attendance`）下，不拼就会打到别的项目上。新页面用 `{% url %}` 生成路径后仍要拼前缀
   （`user_list.html` 的更新/删除、`audit_log_list.html` 的详情都是手拼前缀的写法），
   动态主键优先用 `{% url ... 0 %}` 占位再替换，而不是手写路径。
4. **用 `innerHTML` 拼「来自数据库或上传文件」的内容必须先 `escapeHtml()`。**
   考勤数据全部来自 Excel，属不可信输入，否则构成存储型 XSS（`base.html` 注释）。
5. **超过数量上限就不要渲染表格。** `daily_list` / `leave_list` 在超限时走提示分支，
   不进入表格渲染。注释给出的量级：生产数据系统实测 3000 条命中 ≈ 5MB HTML、10 万条约 170MB。
6. **不要在模板里做计算、字典查找或格式化。** 需要展示的派生值（月度对比的合计行、逐人格子）
   在视图里算好并格式化成字符串再传进来。 -->

### TemplatePresenceTests（`attendance/tests_report.py`，3 个用例）

| 用例 | 断言内容 |
|------|----------|
| `test_template_is_shipped` | `template_path()` 指向的文件存在，且大小 > 10 000 字节（「报表模板必须随代码一起提供 —— 缺了就没法生成」） |
| `test_template_has_expected_sheets` | 工作簿含 `SHEET_DAILY` / `SHEET_SUMMARY` 两个 Sheet；逐格校验表头文案（姓名、应出勤、出勤打卡、实际出勤、事假、产检假、迟到、加班、H）与 `W2:W3` 合并区域 |
| `test_argb_normalizes_six_digit_colors` | `argb()` 把 6 位色值补成 8 位 ARGB（`'FF8080'` → `'FFFF8080'`），非法值返回 `None` |

### XssSafetyTests（`attendance/tests.py`，2 个用例）

`setUp` 里造的可疑数据：文件名 `<img src=x onerror=alert(1)>.xlsx`、
姓名 `<script>alert(1)</script>`、部门 `<b>研发</b>`。

| 用例 | 断言内容 |
|------|----------|
| `test_daily_list_escapes_excel_content` | 每日明细页 HTML 中不含 `<script>alert(1)</script>`、不含 `<b>研发</b>`，且含转义后的 `&lt;script&gt;` |
| `test_file_list_escapes_filename` | 导入记录页 HTML 中不含 `<img src=x onerror=alert(1)>` |

### 其它触及模板的用例（用于回归时定位）

| 测试类（文件） | 用例数 | 与模板相关的部分 |
|----------------|--------|------------------|
| `PermissionBoundaryTests`（tests.py） | 9 | `test_overview_leave_card_links_to_leave_list`（概览页须含 `attendance:leave_list` 链接）、`test_leave_list_renders_and_filters`、`test_leave_list_filters_by_approval_no`、`test_leave_list_pagination_keeps_approval_no`（断言含 `approval_no=` 与 `page=2`）、`test_leave_list_has_query_limit` |
| `QueryLimitTests`（tests.py） | 2 | 2500 条命中时 `over_limit` 为真、`page` 为 `None`，且页面**不出现**最后一行数据；加筛选后回落到正常分页 |
| `UploadFlowTests`（tests.py） | 22 | `test_import_detail_page_renders`（200 且含文件名）、`test_malicious_filename_is_escaped_in_report`（含 `&lt;img`，不含原始标签） |
| `PermissionMatrixTests`（tests.py） | 20 | `test_nav_reflects_permissions`：只读用户页内**无**「数据上传」、有「每日明细」「导入记录」；只上传用户有「数据上传」「我的上传」、**无**「月度汇总」；规则管理员页内含「考勤规则配置」 |
| `ReportDownloadViewTests`（tests_report.py） | 13 | `test_get_shows_form` / `test_get_without_period_shows_form`（断言页面含「生成并下载」）、`test_report_page_script_has_no_serverside_leftovers` |

覆盖缺口（按实际文件逐个核对后确认）：`monthly_summary.html`、`import_preview.html`、
`rule_config.html`、`login.html`、`user_list.html`、`backup_list.html` 没有针对页面文案或
结构的模板断言，只有间接的「能打开」覆盖（如 `PermissionMatrixTests` 里对
`attendance:monthly_summary` / `attendance:rule_config` 的 200 断言）。
