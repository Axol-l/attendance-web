# 考勤管理系统（Web 版）

把处理钉钉考勤导出表的桌面工具改造为 Web 系统：上传 → 入库 → 查询 → 汇总 → 生成考勤报表。

| | |
|---|---|
| **技术栈** | Django 4.1 + Python 3.10 + MySQL 8.0 + Bootstrap 5（CDN，无前端框架） |
| **Excel 处理** | openpyxl + pandas |
| **部署** | Docker Compose + Gunicorn + Nginx（支持子路径前缀部署） |
| **测试** | 205 个单元测试，用内存 SQLite 跑，不依赖 MySQL |

> ⚠️ **本仓库刻意不包含**任何真实业务数据、报表模板与内部文档。
> 见文末[隐私说明](#隐私说明)。

---

## 1. 功能

| 功能 | 说明 |
|---|---|
| **钉钉数据导入** | 解析「每日统计」日考勤表与「请假单据」，自动处理**多行表头 + 合并单元格** |
| **导入可追溯** | 每次导入留记录与结构化导入报告；同一账期支持「合并」与「整账期覆盖」两种策略 |
| **每日明细查询** | 按人员 / 日期范围 / 部门筛选，分页浏览 |
| **请假记录查询** | 按姓名、类型、审批状态、**审批编号**（支持片段匹配）筛选 |
| **月度汇总** | 应出勤、出勤打卡、实际出勤、各类请假、缺勤、加班；支持**跨月对比** |
| **考勤报表导出** | 以模板填充方式生成 xlsx，复刻桌面工具的合并单元格、状态着色、周末红字 |
| **考勤规则配置** | 排除规则、加班阈值、应出勤天数、颜色映射都可配置，不再硬编码 |
| **权限与审计** | 3 档角色 + 5 个细粒度权限码；关键操作写审计日志 |

---

## 2. 快速开始

### 2.1 跑测试（无需 MySQL）

```bash
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS / Linux
pip install -r requirements.txt

python manage.py test --settings=config.settings_test
```

`config/settings_test.py` 用内存 SQLite，专供本地自检。

### 2.2 本地开发（MySQL）

```bash
cp .env.example .env             # 按需修改
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

### 2.3 Docker Compose（推荐）

```bash
cp .env.example .env
docker compose up -d --build
# 访问 http://localhost:8001
# 健康检查 http://localhost:8001/health/
```

首启自动建表并创建超级用户（默认 `admin` / `admin123456`，**登录后立即修改**）。

---

## 3. 使用流程

### 3.1 准备两份导出文件

| 文件 | 命名特征 | 内容 |
|---|---|---|
| 日考勤汇总表 | `<公司名>_每日统计_<起>-<止>.xlsx` | 打卡时间与结果、班次、迟到早退、缺卡、加班、日请假子类 |
| 请假单据 | `ding_8_请假单据.xlsx` | 审批编号、审批状态与结果、请假类型与时长 |

两份文件的「姓名」写法可能不一致（一边带 `（离职）` 后缀），系统会**自动归一化**后再按人对齐。

### 3.2 上传

进入「数据上传」，选择文件类型与账期后上传。上传前会先解析并展示预览，确认无误再入库。

同一账期重复上传有两种策略：

| 策略 | 行为 | 适用场景 |
|---|---|---|
| **合并**（默认） | 同 `(日期, UserId)` 覆盖更新，文件里没有的人保留 | 补传、增量修正 |
| **整账期覆盖** | 先清空该账期全部记录再写入 | 重新导出了完整月表，人员有增减 |

> ⚠️ 整账期覆盖**不可逆**。它的意义在于：重导后消失的人不会留下幽灵数据。

### 3.3 查询与汇总

- **每日明细**：按人员、日期范围、部门筛选
- **请假记录**：可按审批编号直接定位单条记录
- **月度汇总**：先上传「应出勤天数」规则值，再看月度结果；支持选两个账期做跨月对比

### 3.4 生成报表

进入「报表生成」，选择账期后点击生成，浏览器会下载 `考勤表YYYYMM.xlsx`。

> ⚠️ **需要自备报表模板**，见下一节。

### 3.5 清理冗余导入记录

反复重导同一账期会留下多条"名下已无数据行"的历史记录，让导入记录页看不出哪条生效：

```bash
python manage.py prune_imports            # 预览（默认不删任何东西）
python manage.py prune_imports --apply    # 执行
```

只删除**同时满足**「名下无数据行」「同账期存在更晚的导入」「状态为成功或部分成功」的记录。
失败记录、当前生效记录、仍持有数据行的记录一律保留。

---

## 4. 报表模板（必读）

生成报表采用「**以模板为基础填充**」的方式 —— 模板里预置了合并区域、列宽、行高、
边框与数字格式，从空白 workbook 重画既费事又容易漏。

**模板不随本仓库分发**（它来自具体企业的内部流程，且文件属性里带有创建者信息）。
部署前请自备模板并放到：

```
attendance/templates_xlsx/考勤表模板.xlsx
```

模板需要包含两个 Sheet：

| Sheet | 内容 |
|---|---|
| `考勤处理数据1` | 每日明细：第 3–4 行为表头，第 1–2 行为标题行 |
| `考勤统计2` | 月度汇总：两行表头 + 数据区 |

缺少模板时生成报表会明确报错提示路径，不会静默失败。

> 生成报表时程序会顺带做一次**尾部空列裁剪**：模板里若有
> `<col min="N" max="16384">` 这类定义，Excel 会一直显示到 XFD 列，
> 生成的报表会在 XML 层把多余的列定义删掉。
> 直接打开模板文件本身仍会看到 XFD 列，这是模板自身的属性，不影响生成结果。

---

## 5. 权限模型

角色三档（`accounts.UserProfile.role`）：

| 角色 | 说明 |
|---|---|
| `super_admin` | 全部权限 + Django 后台 |
| `admin` | 全部业务权限 |
| `user` | 按下表逐项授权（**必须勾选，否则登录后所有考勤页都会被拒**） |

权限码（`accounts/views.py: PERMISSION_CODES`）：

| 权限码 | 说明 |
|---|---|
| `attendance.upload` | 上传钉钉数据、删除**自己上传的**导入 |
| `attendance.query` | 查询明细 / 请假 / 月度汇总；查看**全部**导入记录与报告 |
| `attendance.export` | 导出查询结果（预留，尚未实现） |
| `attendance.report` | 生成并下载月度考勤报表 |
| `attendance.rule_manage` | 修改考勤规则 |

要点：

- 导入记录 / 报告：有 `query` 看全部；只有 `upload` **只见自己上传的**
- **删除导入比查看更严格**：只有管理员或该文件的上传者本人可删
  （删除会连带清掉该文件导入的数据，不可逆）
- 导航与后端权限由 `attendance/permissions.py` 与 `context_processors` 保证一致，
  并由 `PermissionMatrixTests` 逐条覆盖

> ⚠️ 新增权限码时要同时改四处，否则会出现"前端按钮消失但后端仍放行"：
> 1. `accounts/context_processors.py` 的 `PERMISSION_TEMPLATE_KEYS`
> 2. `accounts/views.py` 的 `PERMISSION_CODES`
> 3. `attendance/permissions.py` 的 `PAGE_PERMISSIONS`
> 4. 对应视图的 `@permission_required` 装饰器
>
> 新增 `log_action(...)` 调用时，还要把 action 码登记进 `ACTION_LABELS`，
> 否则操作日志页会显示英文码（有测试扫源码强制守住）。

---

## 6. 环境变量

参考 `.env.example`（本地）与 `.env.prod.template`（生产）。

| 变量 | 用途 | 生产必须修改 |
|---|---|---|
| `SECRET_KEY` | Django 密钥 | ✅ |
| `DEBUG` | 调试开关，生产必须为 `False` | ✅ |
| `ALLOWED_HOSTS` | 允许的 Host | ✅ |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | 数据库连接 | ✅ |
| `DB_HOST` / `DB_PORT` | 数据库地址与端口 | — |
| `DB_ROOT_PASSWORD` | MySQL root 口令（**Docker 编排要用，缺失会导致 db 容器起不来**） | ✅ |
| `FORCE_SCRIPT_NAME` | 子路径前缀（如 `/attendance`），根路径部署留空 | 按部署方式 |
| `STATIC_URL` / `MEDIA_URL` | 静态与媒体路径前缀 | 按部署方式 |
| `CSRF_TRUSTED_ORIGINS` | CSRF 信任源，逗号分隔 | ✅ |
| `MAX_FILE_SIZE` | 单文件上传上限（字节，默认 50MB） | 按需 |
| `IMPORT_MAX_ROWS` | 单次导入最大行数（默认 100000） | 按需 |
| `QUERY_MAX_RESULTS` | 查询结果硬上限（默认 2000，防止把巨量结果内联进 HTML） | 按需 |

---

## 7. 部署

### 7.1 Docker Compose

```bash
cp .env.prod.template .env
vi .env                          # 填 SECRET_KEY、数据库口令、域名等
./deploy.sh                      # 构建 + 启动 + 健康检查
```

### 7.2 子路径前缀部署

与其它系统共用一台 Nginx 时，用路径前缀区分：

```nginx
location /attendance/ {
    proxy_pass http://127.0.0.1:8001/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header SCRIPT_NAME /attendance;
    client_max_body_size 60m;
}
```

同时把 `FORCE_SCRIPT_NAME=/attendance`、`STATIC_URL=/attendance/static/`、
`MEDIA_URL=/attendance/media/` 配好。

### 7.3 备份

`/backups/` 页面可创建 / 下载 / 删除数据库备份（mysqldump + zip）。
**没有定时自动备份**，需要时请用系统 cron 调 `mysqldump`。

---

## 8. 目录结构

```
├── config/                     项目配置
│   ├── settings.py             主配置
│   ├── settings_production.py  生产覆盖
│   ├── settings_test.py        SQLite 测试配置
│   └── urls.py                 路由汇总
├── accounts/                   用户 · 角色 · 细粒度权限 · 审计日志
│   ├── models.py               UserProfile / UserPermission / AuditLog
│   ├── decorators.py           @permission_required
│   ├── services.py             log_action() —— 统一审计入口
│   └── context_processors.py   向模板注入 user_perms
├── attendance/                 考勤业务（核心）
│   ├── mapping.py              钉钉字段映射（纯数据，无逻辑）
│   ├── models.py               AttendanceDaily / LeaveRecord / AttendanceRule / UploadedFile
│   ├── services.py             表头解析器 · 导入器 · 汇总计算
│   ├── report.py               报表生成与列裁剪
│   ├── permissions.py          页面 × 权限码的单一事实来源
│   ├── utils.py                日期输入上下界
│   ├── views.py                函数视图 + 装饰器链
│   ├── tests.py                单元测试
│   ├── tests_report.py         报表单元测试
│   ├── management/commands/    inspect_import · export_stats · prune_imports
│   └── templates_xlsx/         报表模板（**需自备，不在仓库内**）
├── backups/                    数据库备份
├── core/                       健康检查与首页
├── templates/                  base.html 及页面模板
├── docs/                       内部文档（**不在仓库内**）
├── tools/                      对账与校验脚本（**不在仓库内**）
├── Dockerfile · docker-compose.yml · docker-compose.server.yml
├── docker-entrypoint.sh · gunicorn.conf.py · deploy.sh
└── requirements.txt
```

---

## 9. 工程约定

1. **视图**：函数视图 + `@login_required` → `@permission_required(...)`，不使用 CBV
2. **业务逻辑**：放 `services.py`，不写在视图里
3. **字段映射**：单独模块（`attendance/mapping.py`），纯数据无逻辑，便于调整而不用改代码
4. **审计日志**：统一走 `accounts/services.py` 的 `log_action()`
5. **权限 UI**：`user_perms` 上下文，模板用 `{% if user_perms.xxx %}`
6. **迁移**：`makemigrations` 后**必须人工审阅**，确认是 `CreateModel`/`AddField` 而非 `RemoveField`

### 数据模型要点

- **唯一键**：日考勤按 `(work_date, user_id)` 覆盖导入。
  「工号」列在真实导出里可能全为空不可用；「姓名」有重名与离职后缀变化风险；
  `UserId` 是钉钉的稳定 ID，作为主键依据。
- **请假统一存小时**：源表里部分类型是「小时」、部分是「天」，
  导入时按标准工作时长折算成小时，原始单位留痕。
- **没有 `unique_together(work_date, user_id)`**：这两个字段都可能为空，
  而 NULL 在唯一约束里不等于自身，会放进重复行。查重逻辑在 `ExcelImporter` 里显式处理。

---

## 10. 已避开的坑

这些都是开发过程中实测踩到并修掉的，改动相关代码前建议先看一眼。

| 坑 | 做法 |
|---|---|
| **多行表头 + 合并单元格**：只读第一行表头会丢掉全部 10 个请假子列 | 表头行自动定位 + 合并区域展开；表头按**行号**取值（倒数第二行为分组、最后一行为叶子） |
| 表头行检测被标题行的大合并区域误导 | 用**纵向合并覆盖率**配合最小实际列数判定；阈值必须拿真实文件验证，不能凭感觉调 |
| 钉钉导出的 xlsx 缺 `dimension` 元数据，`read_only=True` 下 `max_row` 恒为 1 | 强制普通模式加载，解析器全程不使用 `read_only` |
| 日期用字符串下标判断月份 | 统一解析成 `date` 对象再比较（字符串下标法在两位数月份上会串月） |
| 负加班不截断会冲减月度合计 | 规则化，默认截断为 0 |
| 「应出勤天数」被写进出勤打卡列 | 写入正确的列，且改为规则配置项 |
| 未通过的请假被计入合计 | 按审批状态与结果过滤，默认排除 |
| 按姓名硬编码排除人员 | 改为按结构化规则（考勤组 + 无部门）排除，姓名名单仅作补充且默认为空 |
| 同一人在两份表里姓名写法不一致，被当成两个人 | 导入时统一归一化离职后缀 |
| 用 `setattr` 写映射字段，字段名拼错**不报错**只丢数据 | 加双向覆盖测试：映射指向的字段必须真实存在，模型字段必须有映射来源 |
| 一次构造上千个键的 `Q` 对象 → `Expression tree is too large` | 批量删除分块（每批 200 个键） |
| `merge_cells` 会重置样式，先上色后合并会丢颜色 | 先合并再上色 |
| 6 位色值不补 ARGB 前缀会被当成 alpha=0（全透明） | 颜色统一归一化为 8 位 ARGB |
| 查询结果全量内联进 HTML | `QUERY_MAX_RESULTS` 硬上限，超限只提示不渲染 |
| `innerHTML` 拼接 Excel 数据 → 存储型 XSS | 模板全部走 Django 自动转义，并提供 `escapeHtml()` |
| Django 4.1 起 `TEMPLATES` 未配 `loaders` 会**无条件**缓存模板（与 `DEBUG` 无关） | 显式声明 loaders |
| `.dockerignore` 里 `*.sql` 不跨 `/`（Go `filepath.Match` 语义） | 用 `**/*.sql` |
| `.dockerignore` 的 `__pycache__/` 只排除顶层目录 | 用 `**/__pycache__/`（否则嵌套的字节码会进镜像） |
| 排除测试文件的模式写窄了（`**/test_*.py` 匹配不到 `tests_report.py`） | 用 `**/test*.py`；并用测试断言每个测试文件都被排除 |
| `subprocess.run` 的 `timeout` 仅在 `stdout=PIPE` 时生效 | 捕获输出后自行落盘 |

---

## 11. 隐私说明

本仓库为公开发布版本，**刻意排除了**以下内容：

| 排除项 | 原因 |
|---|---|
| 考勤原始数据与生成的报表（`*.xlsx` / `*.xls` / `media/`） | 含员工姓名、工号等个人信息 |
| 报表模板 `attendance/templates_xlsx/` | 模板文件属性中含创建者信息，且属企业内部表格格式 |
| `tools/` 对账脚本 | 按真实样本与真实姓名逐行对账 |
| `docs/` 内部文档 | 业务口径确认单、权限矩阵、迁移手册等 |
| `.env` 系列 | 含口令与服务器地址 |
| 备份文件（`*.sql` / `*.zip`） | 含完整数据库内容 |

代码与测试中的人员姓名一律使用 `员工NN` 这类占位符，账期使用示意值。
另有一个单元测试专门扫描生产代码，**只保存姓名的 SHA-256 摘要**（不存明文）
来防止真实姓名被回填进代码 —— 如果把姓名明文写在测试里，测试文件自身就会成为泄露源。

### 部署前请确认

- [ ] `.env` 里的 `SECRET_KEY`、数据库口令、`DB_ROOT_PASSWORD` 都已改成强随机值
- [ ] 默认超级用户口令已修改
- [ ] 自备报表模板已放到 `attendance/templates_xlsx/考勤表模板.xlsx`
- [ ] 反向代理开启了 HTTPS，并把 `CSRF_TRUSTED_ORIGINS` 改成实际访问来源

---

## 12. 许可

内部项目，未附带开源许可。使用前请与作者确认。
