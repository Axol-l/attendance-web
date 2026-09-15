# attendance — 考勤数据模块（核心）

## 模块职责

本模块承担系统全部业务：钉钉导出文件的解析与导入、日考勤明细与请假记录查询、月度汇总计算、考勤报表（Excel）生成，以及考勤规则维护。

数据流：

```
钉钉 xlsx ──解析──▶ 入库（日考勤 / 请假）──计算──▶ 月度汇总 ──填充模板──▶ 考勤表{YYYYMM}.xlsx
                          ▲
                     AttendanceRule（规则口径）
```

## 文件清单

| 文件 | 说明 |
|------|------|
| `mapping.py` | 钉钉字段映射，**纯数据模块、无逻辑**：表头字段表、请假子类、颜色映射、排除名单默认值 |
| `models.py` | `UploadedFile` / `AttendanceDaily` / `LeaveRecord` / `AttendanceRule` |
| `services.py` | 核心业务：表头解析器、导入器、汇总计算器、文件存储与校验 |
| `report.py` | 报表生成（以模板填充方式复刻桌面工具输出）、列裁剪 |
| `permissions.py` | 页面 × 权限码的**单一事实来源** |
| `utils.py` | 从真实数据推导日期输入框的上下界 |
| `views.py` | 11 个函数视图 + 报表路径/导入流程辅助函数 |
| `urls.py` | 路由（namespace: `attendance`） |
| `admin.py` | 4 个模型的后台注册 |
| `templates_xlsx/考勤表模板.xlsx` | 报表模板，随代码分发（134 KB） |
| `management/commands/inspect_import.py` | 逐人统计导入结果，支持 `--code-stats` |
| `management/commands/export_stats.py` | 导出月度统计 JSON，供一致性对账使用 |

## 模型/表结构

### attendance_daily — 日考勤明细（宽表）

- 维度字段：`work_date`、`user_id`、`name`、`attend_group`、`department`、`employee_no`、`position`、`shift`
- 打卡字段：3 组上下班打卡的时间与结果（`in1_time`/`in1_result` … `out3_result`），共 12 列
- 考勤数值：出勤/休息天数、工作时长（分钟）、迟到、严重迟到、旷工迟到、早退、上班/下班缺卡、旷工、出差时长、外出时长
- 请假 10 子类：事假、调休、病假、年假、产假、陪产假、婚假、例假、丧假、哺乳假（**统一存小时**）
- 加班 4 列：总时长、工作日、休息日、节假日
- 外键：`source_file`（→ `UploadedFile`，`on_delete=CASCADE`）、`created_by`（→ `User`，`on_delete=PROTECT`）
- 索引：`(work_date, name)`、`(user_id, work_date)`

> ⚠️ **没有** `unique_together(work_date, user_id)`。原因写在 `Meta` 注释里：`user_id` 与
> `employee_no` 都可能为空，而 NULL 在唯一约束中不等于自身，会放进重复行。查重改在
> `services.py` 的 `ExcelImporter` 里显式处理（导入前按账期 purge 或按主键覆盖）。

### attendance_leave_records — 请假记录

- `approval_no`（审批编号）为 **UNIQUE**
- 审批维度：`approval_status`、`approval_result`、`submit_time`、`finish_time`
- 申请人维度：`applicant_no`、`applicant_user_id`、`applicant_name`、`applicant_name_raw`（原始，含 `（离职）` 后缀）、`applicant_dept`
- 请假维度：`leave_type`、`start_time`、`end_time`、`duration_hours`（统一小时）、`duration_raw`（原始文本，保留「小时」/「天」单位痕迹）、`duration_days`（折算天数）、`reason`
- 派生标记：`is_resigned`（离职）、`is_approved`（审批通过）、`period`（按 `start_time` 归属月份）
- 索引：`(period, applicant_name)`、`(period, leave_type, is_approved)`

### attendance_rules — 考勤规则

| 字段 | 作用 |
|------|------|
| `standard_work_minutes` | 标准工作时长（分钟），默认 450 |
| `monthly_standard_days` | 月度应出勤天数，**人工输入**，写入报表「应出勤」列 |
| `excluded_employees` | 排除名单（JSON 数组），默认 `[]` |
| `excluded_attend_groups` | 排除的考勤组，默认 `['未加入考勤组']` |
| `exclude_mode` | 排除判定模式，见下 |
| `exclude_if_no_data` | 是否排除「整月无任何数据」的人，默认 `False` |
| `color_map` | 状态 → 颜色映射（JSON） |
| `clamp_negative_overtime` | 负加班是否截断为 0，默认 `True` |
| `exclude_rejected_leave` | 是否排除未通过的请假，默认 `True` |
| `is_active` | 启用标记，`get_active()` 保证任一时刻只有一条生效规则 |

`exclude_mode` 取值（`mapping.EXCLUDE_MODE_CHOICES`）：

| 取值 | 含义 |
|------|------|
| `no_dept` | 考勤组命中 **且** 无部门 → 排除（**默认**） |
| `no_dept_no` | 考勤组命中 **且** 无部门 **且** 无工号 → 排除 |
| `group_only` | 仅按考勤组排除 |
| `name_only` | 仅按姓名名单排除 |

> 判定用**合取**而非析取：实测有离职员工虽在「未加入考勤组」里，但当月有完整打卡与真实出勤，
> 按「考勤组命中 ∨ 无部门」会连人带数据一起丢掉。

### attendance_uploaded_files — 上传文件

导入与删除的最小单元。记录 `original_filename`、`stored_filename`、`file_path`、`file_size`、
`file_hash`（SHA256）、`file_kind`（`daily` / `leave`）、`period`、`record_count`、`skipped_count`、
`status`、`error_message`、`import_report`（JSON 结构的导入报告）、`uploaded_by`、`uploaded_at`、`processed_at`。

`status` 取值：`pending` / `success` / `partial`（有时间跳过的成功）/ `failed`。

## 字段映射（mapping.py）

| 常量 | 说明 |
|------|------|
| `DAILY_SHEET` | 日考勤表的 Sheet 名 |
| `DAILY_SIMPLE_FIELDS` | 单层表头字段（表头文本 → 模型字段） |
| `DAILY_COMPOSITE_FIELDS` | 双层表头的分组字段：`请假` 下 10 个子列、`加班时长（转调休）` 下 3 个子列 |
| `DAILY_LEAVE_UNIT` | 各请假子类的原始单位（小时 / 天） |
| `STATUS_COLOR_MAP` | 打卡结果 → 填充色（ARGB） |
| `WEEKDAY_CN` / `WEEKEND_FONT_COLOR` | 星期中文表与周末红字色 |
| `LEAVE_FIELDS` | 请假单据表头字段 |
| `LEAVE_TYPE_NORMALIZE` | 请假类型别名归一 |
| `REPORT_LEAVE_COLUMNS` | 报表 Sheet2 使用的 **9 类**请假列 |
| `DURATION_UNIT_SUFFIXES` | 时长文本的单位后缀识别 |
| `RESIGNED_MARKERS` | 姓名中的离职标记（`（离职）` 等） |
| `DEFAULT_EXCLUDED_ATTEND_GROUPS` / `DEFAULT_EXCLUDED_EMPLOYEES` / `DEFAULT_EXCLUDE_MODE` / `DEFAULT_EXCLUDE_IF_NO_DATA` | 规则默认值 |
| `default_excluded_attend_groups()` 等 | 同名默认值的**具名函数**版本 |

> ⚠️ JSONField 的 `default` 必须用具名函数，不能用 `lambda`：
> `makemigrations` 会抛 `ValueError: Cannot serialize function: lambda`。
> `DEFAULT_EXCLUDED_EMPLOYEES` 默认是空列表 —— 排除名单不预置任何真实姓名。

**两套请假类型**：日考勤表的 10 个子类与请假单据的 9 个类型**交集只有 7 种**。已确认报表以
「请假单据」那套为准。表外类型（如实测出现过的 `居家办公`）会正常入库，但不参与月度分组汇总。

## 业务逻辑

### 上传校验与存储（services.py 模块级函数）

| 函数 | 作用 |
|------|------|
| `validate_upload()` | 校验扩展名与大小，非 `.xlsx` 直接抛 `ImportError_` 并给出明确文案 |
| `compute_hash()` | 计算 SHA256，用于重复上传提示 |
| `FileStorage.save()` | 落到 `MEDIA_ROOT` 下按类型/账期分目录保存 |

### 表头解析（`DingTalkHeaderParser`）

钉钉导出的 xlsx 有两个特点：**多行表头**（分组行 + 叶子行）与**合并单元格**。

| 方法 | 作用 |
|------|------|
| `_sheet_values()` | 一次性把整张表读进内存缓存，避免逐格访问的 O(n) 往返 |
| `_merged_value_map()` | 展开合并区域：把合并左上角的值铺满整个区域 |
| `_detect_header_rows()` | 自动定位表头行 |
| `parse()` | 按**行号**取值构表头（倒数第二行 = 分组，最后一行 = 叶子） |
| `build_field_index()` | 表头文本 → 列号 |
| `read_data_rows()` | 逐行读数据（用 `iter_rows`） |
| `load_workbook_plain()` | 强制普通模式打开工作簿 |

> ⚠️ **表头行检测的两个坑**（都已修复，改动前请先看这两条）：
> 1. 不能用「标题行合并区域」当表头判据 —— 模板首行有 `A1:AY1` 的大合并，会误判。
>    现用**纵向合并覆盖率 ≥ 0.5** 配合**最小实际列数 ≥ 5**。
> 2. 「填充率 ≥ 50%」这种变体在真实文件上**失败过**（某候选行只有 13 个实际单元格）。
>    **阈值必须拿真实文件验证**，不能凭直觉调。
>
> ⚠️ **表头取值必须按行号，不能按"是否堆叠"判断**：同一列上下两格文本相同时，
> 会被误读成单层表头，导致 10 个请假子列全部丢失。

> ⚠️ **`load_workbook_plain()` 绝不能加 `read_only=True`**：钉钉导出的 xlsx 缺 dimension
> 元数据，只读模式下会把 `max_row` 读成 1，整张表读空。

### 导入（`ExcelImporter`）

| 方法 | 说明 |
|------|------|
| `import_daily()` | 导入日考勤；`purge_existing=True` 时先按月清空再导入 |
| `import_leave()` | 导入请假记录（支持多 Sheet） |
| `purge_daily_period()` / `purge_leave_period()` | 按账期清理 |
| `_is_placeholder_row()` | 判断占位行（**合取**条件，避免误杀真人） |
| `_row_has_data()` | 判断该行是否真有数据 |
| `_bulk_upsert_daily()` / `_bulk_upsert_leave()` | 批量写入 |

- 同账期重复上传有两条路径：**purge（替换）** 与 **merge（合并）**，由上传页的选项决定；
  未选 purge 时会先提示已有导入，让用户确认。
- 账期隔离：purge 只作用于目标月份，不碰其它月份。
- **大批量删除要分块**：一次构造上千个键的 `Q` 对象会撞上
  `OperationalError: Expression tree is too large (maximum depth 1000)`（SQLite 上必现，
  MySQL 上侥幸不报）。统一走 `_chunked(keys, 200)`。

### 汇总计算（`AttendanceCalculator`）

- `daily_overtime(record)`：`max(0, (工作时长 − 标准时长) / 60)`，是否截断由
  `clamp_negative_overtime` 决定。
- `summarize_month(period)`：按人聚合整月指标 —— 应出勤、出勤打卡、实际出勤、
  9 类请假、缺勤、加班。
- `summary_row_for(period, name)`：取单人汇总行。

### 报表生成（report.py）

- `template_path()` / `default_output_name(period)`（`考勤表{YYYYMM}.xlsx`）
- `build_report()`：主入口，`shutil.copy` 模板后填充数据
- `_fill_daily_sheet()` / `_fill_summary_sheet()`：Sheet1 每日明细 / Sheet2 月度汇总
- `argb()`：颜色归一。**6 位色值必须补成 8 位 ARGB**，否则 Excel 会当作 alpha=0（全透明）
- `_status_style()` / `_weekday_of()` / `_fmt_date_cn()` / `_dec()`

> ⚠️ **必须先合并单元格再上色/改字体**：`merge_cells` 会重置样式。
> 顺序反了会出现「周末红字数为 0」这类静默失效。

- `trim_trailing_columns()`：**在 `wb.save()` 之后做 XML 级列裁剪**。

> ⚠️ 模板的 `<cols>` 里有 `<col min="15" max="16384">` 这类定义（Sheet2 还有
> `min="26" max="16377"`），导致 Excel 打开时显示到 XFD 列。openpyxl **丢弃**这些定义
> （`ws.column_dimensions` 里没有对应项），但保存时会从内部 `_cols` 重新写回，
> 所以在 openpyxl 层面改不掉，只能落到 XML 处理。

### 报表下载（两步式）

| 步骤 | 请求 | 响应 |
|------|------|------|
| 1. 生成 | `POST /attendance/report/`（`fetch`） | JSON（成功含文件名/大小/URL；失败 HTTP 400 + 原因） |
| 2. 下载 | `GET /attendance/report/?period=…&download=1` | xlsx 附件 |

报表文件按会话隔离存放，并有**只读路径**与**可写路径**两个入口：

- `_report_dir_for_write()`：清空旧目录再重建（生成前调用）
- `_report_dir_for_read()`：纯只读（取下载路径时调用）

### 日期输入上下界（utils.py）

`attendance_date_bounds()` / `leave_date_bounds()` / `month_bounds()` / `year_bounds()` /
`date_input_bounds()` —— 从库中真实数据的最大最小值推导 `min`/`max`，交给日期输入框，
避免用户选出空区间。**不是**写死的常量。

## 页面路由

| URL | 视图 | 权限码 | 说明 |
|-----|------|--------|------|
| `/attendance/` | `overview` | `attendance.query` | 总览 |
| `/attendance/upload/` | `upload` | `attendance.upload` | 上传与导入 |
| `/attendance/imports/` | `file_list` | `attendance.upload` 或 `attendance.query` | 导入记录（按权限限定范围） |
| `/attendance/imports/<pk>/` | `import_detail` | `attendance.upload` 或 `attendance.query` | 导入报告 |
| `/attendance/imports/<pk>/preview/` | `import_preview` | `attendance.upload` | 解析预览（入库前） |
| `/attendance/imports/<pk>/delete/` | `file_delete` | **视图内判定**（管理员或上传者本人） | 删除导入 |
| `/attendance/daily/` | `daily_list` | `attendance.query` | 每日明细 |
| `/attendance/leave/` | `leave_list` | `attendance.query` | 请假记录（支持审批编号查询） |
| `/attendance/monthly/` | `monthly_summary` | `attendance.query` | 月度汇总（含跨月对比） |
| `/attendance/report/` | `report_generate` | `attendance.report` | 报表生成与下载 |
| `/attendance/rules/` | `rule_config` | `attendance.rule_manage` | 规则配置 |
| `/attendance/rules/save/` | `rule_save` | `attendance.rule_manage` | 规则保存（仅 POST） |

装饰器顺序固定为 `@login_required` → `@permission_required(...)`；
`permission_required` 接受**多个**权限码，语义是 **any-of**，管理员自动绕过。

`file_delete` 是唯一不走装饰器判权限的视图 —— 因为它的规则是「管理员 **或** 该文件的上传者」，
比「拥有某个权限码」更细，判定逻辑集中在 `permissions.can_delete_import()`。


## 边界与测试

`attendance/tests.py` 与 `attendance/tests_report.py` 中与本模块直接相关的测试类：

| 测试类 | 覆盖内容 |
|--------|----------|
| `ParseDailyDateTests` | 日期解析（含 10/11/12 月的下标坑） |
| `ParseDurationTests` | 时长文本 → 小时（小时/天两种单位） |
| `ResignedNameTests` | `（离职）` 后缀归一，保证日考勤与请假能按人对齐 |
| `DingTalkHeaderParserTests` | 表头行定位、合并单元格展开、10 个请假子列一个不少 |
| `ImportDailyTests` | 日考勤导入、占位行判定、缺列报错、账期隔离 |
| `ImportLeaveTests` | 请假多 Sheet 导入、审批编号唯一、拒绝记录标记 |
| `CalculatorTests` | 汇总计算 |
| `AttendanceRuleTests` | 规则默认值与生效规则唯一性 |
| `BuildReportStructureTests` / `ReportSummarySheetTests` / `ReportStylingTests` | 报表结构、汇总 Sheet、颜色与合并 |
| `ColumnTrimmingTests` | 尾部列裁剪（含"模板本身仍有缺陷"的守卫用例） |
| `ReportDownloadViewTests` | 两步式下载流程 |

`attendance/tests.py`、`attendance/tests_report.py` 与 `backups/tests.py`、`core/tests.py`
合计 **210 个用例**，全部用内存 SQLite 运行：

```bash
python manage.py test --settings=config.settings_test
```

## 已知限制

- **`attendance.export`（导出查询结果）为预留权限码**，当前无对应功能。
- 表外请假类型（如 `居家办公`）可入库但**不参与月度分组汇总**。
- 报表以「请假单据」类型集为准，与日考勤表 10 子类的**差集类型不会出现在报表里**。
