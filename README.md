# 考勤管理系统（Web 版）

把处理钉钉考勤导出表的桌面工具改造为 Web 系统：上传 → 入库 → 查询 → 汇总 → 生成考勤报表。

| | |
|---|---|
| **技术栈** | Django 4.1 + Python 3.10 + MySQL 8.0 + Bootstrap 5 |
| **Excel 处理** | openpyxl + pandas |
| **部署** | Docker Compose + Gunicorn + Nginx |

---

## 1. 功能

| 功能 | 说明 |
|---|---|
| **钉钉数据导入** | 解析「每日统计」日考勤表与「请假单据」，自动处理**多行表头 + 合并单元格** |
| **导入可追溯** | 每次导入留记录与结构化导入报告；同一账期支持「合并」与「整账期覆盖」两种策略 |
| **每日明细查询** | 按人员 / 日期范围 / 部门筛选，分页浏览 |
| **请假记录查询** | 按姓名、类型、审批状态、**审批编号**筛选 |
| **月度汇总** | 应出勤、出勤打卡、实际出勤、各类请假、缺勤、加班；支持**跨月对比** |
| **考勤报表导出** | 以模板填充方式生成 xlsx，复刻桌面工具的合并单元格、状态着色、周末红字 |
| **考勤规则配置** | 排除规则、加班阈值、应出勤天数、颜色映射都可配置，不再硬编码 |
| **权限与审计** | 3 档角色 + 5 个细粒度权限码；关键操作写审计日志 |

### 技术文档

实现细节见 [`docs/modules/`](docs/modules/)：

| 文档 | 内容 |
|---|---|
| [`attendance.md`](docs/modules/attendance.md) | 核心业务模块：数据模型、字段映射、表头解析器、导入器、汇总计算、报表生成 |
| [`accounts.md`](docs/modules/accounts.md) | 用户 · 角色 · 权限码 · 装饰器链 · 审计日志 |
| [`config.md`](docs/modules/config.md) | 项目配置：settings 关键项、业务常量、日志、静态与媒体文件 |
| [`templates.md`](docs/modules/templates.md) | 模板体系、权限显示、页面交互与前端禁忌 |
| [`deployment.md`](docs/modules/deployment.md) | 部署与运维：容器、环境变量、启动流程、备份、升级回滚 |

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

首启自动建表并创建超级用户（默认 `admin` / `admin123456`）。

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

> ⚠️ 整账期覆盖**不可逆**。

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

## 4. 报表模板

生成报表采用「**以模板为基础填充**」的方式 —— 模板里预置了合并区域、列宽、行高、
边框与数字格式，从空白 workbook 重画既费事又容易漏。

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
│   ├── mapping.py              钉钉字段映射
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
├── static/                     前端资源
├── docs/modules/               技术模块文档
├── Dockerfile · docker-compose.yml · docker-compose.server.yml
├── docker-entrypoint.sh · gunicorn.conf.py · deploy.sh
└── requirements.txt
```

---

## 9. 隐私说明

### 部署前请确认

- [ ] `.env` 里的 `SECRET_KEY`、数据库口令、`DB_ROOT_PASSWORD` 都已改成强随机值
- [ ] 默认超级用户口令已修改
- [ ] 自备报表模板已放到 `attendance/templates_xlsx/考勤表模板.xlsx`
- [ ] 反向代理开启了 HTTPS，并把 `CSRF_TRUSTED_ORIGINS` 改成实际访问来源

---

## 10. 许可

内部项目，未附带开源许可。使用前请与作者确认。
