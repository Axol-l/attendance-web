"""
考勤业务逻辑 —— 全部业务逻辑放这里，不写在视图里（项目计划书第 2 节第 2 条）

模块划分：
  · FileStorage        —— 上传文件落盘、哈希、查重
  · DingTalkHeaderParser —— 多行表头 + 合并单元格展开解析器（Phase 2 核心难点）
  · ExcelImporter      —— 日考勤 / 请假数据入库
  · AttendanceCalculator —— 月度汇总计算（规则来自 AttendanceRule，不硬编码）

⚠️ 所有解析都**不使用 openpyxl 的 read_only=True**：
   实测钉钉导出的 xlsx 缺少 dimension 元数据，read_only 模式下
   `max_row` / `max_column` 恒为 1，会直接把整张表读空。
"""
import hashlib
import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import openpyxl
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import mapping
from .models import AttendanceDaily, AttendanceRule, LeaveRecord, UploadedFile

logger = logging.getLogger('attendance')


# ============================================================================
# 通用工具
# ============================================================================

class ImportError_(Exception):
    """导入过程中的可预期错误（消息面向用户，直接展示）"""


def to_decimal(value, default=Decimal('0')):
    """把 Excel 单元格值安全转成 Decimal（'' 和 None 都当默认值）"""
    if value is None or value == '':
        return default
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return default


def normalize_text(value):
    """把单元格文本规范化：去首尾空白、全角空格，None → None"""
    if value is None:
        return None
    text = str(value).replace('\u3000', ' ').strip()
    return text or None


# 钉钉「日期」列的格式实测为 '14-08-01 星期五'
_DAILY_DATE_RE = re.compile(r'^(\d{2})-(\d{2})-(\d{2})')

# 钉钉「开始时间」等列实测为 '2014-08-06 08:00'（可能带秒）
_DT_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d')
_DATE_FORMATS = ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d')

# 中文星期 → 用于补齐年份推断（此处只做解析，不参与计算）
_WEEKDAY_CN = '一二三四五六日'


def parse_daily_date(value, period_hint=None):
    """
    解析日考勤表的「日期」列。

    实测格式：'14-08-01 星期五'（两位年 + 月 + 日 + 空格 + 中文星期）

    ⚠️ 桌面版用 `d_val[6]` 取字符判断月份，实测后果：
       · 10 月：'2014-10-06'[6] = '0'，与用户输入的 '10' 永不相等 → 整月数据静默丢弃
       · 11/12 月：'2014-11-06'[6] = '1'，输入 '1' 会同时命中 1 月和 11 月 → 跨月串数据
    这里改为解析成真正的 date 对象再比较。

    period_hint: 'YYYY-MM'，用于两位年份的世纪推断（如 '25' → 2014）
    """
    text = normalize_text(value)
    if not text:
        return None

    m = _DAILY_DATE_RE.match(text)
    if m:
        yy, mm, dd = (int(g) for g in m.groups())
        century = 2000
        if period_hint and len(period_hint) >= 4:
            try:
                century = (int(period_hint[:4]) // 100) * 100
            except ValueError:
                century = 2000
        try:
            return date(century + yy, mm, dd)
        except ValueError:
            return None

    # 兜底：完整日期字符串
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime(value):
    """解析 '2014-08-06 08:00' 这类时间文本，返回 aware datetime 或 None"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = normalize_text(value)
        if not text:
            return None
        dt = None
        for fmt in _DT_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def parse_duration_to_hours(raw, standard_work_minutes=450):
    """
    解析请假「时长」文本 → 小时数。

    实测源数据取值：'7.5小时' '3小时' '0.5小时' '12天' '70天' '255小时' …

    ⚠️ 桌面版写的是 `f_val[:-2]` / `f_val[:-1]`（源代码.py 第 624/626 行）。
       长度恰好对，但语义脆弱：只要钉钉把单位改成 '7.5H'、'7.5 小时'（带空格）
       或 '7.5Hours'，切出来的就是垃圾且不报错。
    这里改为显式匹配单位后缀，认不出来就明确报错（由调用方决定是跳过还是中断）。
    """
    text = normalize_text(raw)
    if not text:
        return Decimal('0')

    for suffix, unit in mapping.DURATION_UNIT_SUFFIXES.items():
        if text.endswith(suffix):
            number_part = text[:-len(suffix)].strip()
            try:
                number = Decimal(number_part)
            except InvalidOperation:
                raise ImportError_(f'请假时长无法解析：{raw!r}（数字部分 {number_part!r} 不是数字）')
            if unit == 'day':
                # 天 → 小时
                return number * (Decimal(standard_work_minutes) / Decimal('60'))
            return number

    # 没有单位后缀：钉钉偶尔直接给数字
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ImportError_(f'请假时长无法解析：{raw!r}（缺少「小时」/「天」单位后缀）')


def normalize_resigned_name(raw):
    """
    归一化离职标记。

    实测源数据形如 '员工09(已离职)'（半角括号，末尾 5 个字符）。
    桌面版第 596-604 行把它转成 '员工09（离职）'（全角），以便与日考勤表
    里钉钉自己输出的名字对上。
    返回 (归一化姓名, 是否离职)
    """
    text = normalize_text(raw)
    if not text:
        return None, False
    for marker in mapping.RESIGNED_MARKERS:
        if text.endswith(marker):
            return text[:-len(marker)].strip(), True
    return text, False


def period_of(dt):
    """aware datetime → 'YYYY-MM'"""
    return timezone.localtime(dt).strftime('%Y-%m') if dt else None


def _chunked(seq, size):
    """把序列切成固定大小的块，用于避免单条 SQL 的条件过多。"""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ============================================================================
# 文件存储
# ============================================================================

class FileStorage:
    """上传文件落盘与查重"""

    @staticmethod
    def validate_upload(uploaded_file):
        """
        校验上传文件：扩展名 / 大小 / 非空。

        ⚠️ 生产数据系统实测缺这三项校验（计划书第 390 行"上传需校验扩展名/
           大小/单次数量"）。这里从第一天就做。
        """
        name = uploaded_file.name or ''
        ext = os.path.splitext(name)[1].lower().lstrip('.')
        allowed = [e.strip().lower() for e in settings.ALLOWED_EXTENSIONS if e.strip()]

        if ext not in allowed:
            raise ImportError_(
                f'不支持的文件类型：.{ext}。仅支持 {"、".join("." + e for e in allowed)} 文件。'
            )
        if uploaded_file.size == 0:
            raise ImportError_('文件为空，请重新导出后再上传。')
        if uploaded_file.size > settings.IMPORT_MAX_FILE_SIZE:
            limit_mb = settings.IMPORT_MAX_FILE_SIZE / 1024 / 1024
            raise ImportError_(f'文件过大（{uploaded_file.size / 1024 / 1024:.1f}MB），上限 {limit_mb:.0f}MB。')
        return ext

    @staticmethod
    def compute_hash(uploaded_file):
        """流式计算 SHA256，避免把大文件整个读进内存"""
        digest = hashlib.sha256()
        for chunk in uploaded_file.chunks():
            digest.update(chunk)
        uploaded_file.seek(0)
        return digest.hexdigest()

    @staticmethod
    def save(uploaded_file, file_kind, period, user):
        """落盘并建 UploadedFile 记录"""
        ext = FileStorage.validate_upload(uploaded_file)
        file_hash = FileStorage.compute_hash(uploaded_file)

        subdir = os.path.join('uploads', file_kind, period or 'unknown')
        target_dir = os.path.join(settings.MEDIA_ROOT, subdir)
        os.makedirs(target_dir, exist_ok=True)

        stored_name = f'{timezone.now():%Y%m%d_%H%M%S}_{file_hash[:12]}.{ext}'
        file_path = os.path.join(target_dir, stored_name)

        with open(file_path, 'wb') as fh:
            for chunk in uploaded_file.chunks():
                fh.write(chunk)

        return UploadedFile.objects.create(
            original_filename=uploaded_file.name,
            stored_filename=stored_name,
            file_path=file_path,
            file_size=uploaded_file.size,
            file_hash=file_hash,
            file_kind=file_kind,
            period=period,
            uploaded_by=user,
            status='pending',
        )


# ============================================================================
# 多行表头 + 合并单元格解析器（Phase 2 核心）
# ============================================================================

class DingTalkHeaderParser:
    """
    钉钉表格表头解析器。

    解决的三个问题（对应计划书第 9 节的技术难点）：
      1. **多行标题**：第 1-2 行是标题与生成时间，不是表头，不能写死行号。
      2. **两层表头**：第 3 行是大类、第 4 行是子类，要拼成「请假-事假(小时)」。
      3. **合并单元格**：`AW3:AY3` 的值只存在于左上角单元格，其余为空，
         必须展开填充，否则「调休、病假、年假…」等 9 个字段全部丢失。

    产出 `columns` 列表，每项：
        {
          'index': 0-based 列下标,
          'letter': 'A',
          'top': 第 3 行原文（可能为空）,
          'sub': 第 4 行原文（可能为空）,
          'key': 用于字段映射的键（单层字段=top，复合字段='大类-子类'）,
          'group': 所属大类（单层字段为 None）,
          'leaf': 叶子列名（有子类时=子类，否则=top）,
        }
    """

    def __init__(self, worksheet, config=None):
        self.ws = worksheet
        self.config = config or mapping.DAILY_SHEET
        self.header_rows = []
        self.header_row_numbers = []
        self.columns = []
        self._shadow = None
        self._values = None

    # ── 批量读缓存 ──

    def _sheet_values(self):
        """
        一次性把整张表的原始值读进 {(row, col): value}。

        ⚠️ 与 read_data_rows 同一个原因：绝不逐格调 ws.cell()。
           这里一次 iter_rows 读完整表（实测 2759×51 约 0.4s），
           后续所有查表都是 dict 命中。
        """
        if self._values is not None:
            return self._values

        max_row = self.ws.max_row or 0
        max_col = self.ws.max_column or 0
        values = {}
        for offset, row in enumerate(
                self.ws.iter_rows(min_row=1, max_row=max_row,
                                  min_col=1, max_col=max_col, values_only=True)):
            r = offset + 1
            for c, v in enumerate(row, start=1):
                if v is not None:
                    values[(r, c)] = v
        self._values = values
        return values

    # ── 步骤 1：展开合并单元格 ──

    def _merged_value_map(self):
        """
        把合并区域内除左上角以外的单元格，映射到左上角的值。

        返回 {(row, col): value}
        注意：openpyxl 的 MergedCell 是只读的，不能直接改它的值，
        所以这里构造一张"影子表"供读取，而不是去写单元格。
        """
        if self._shadow is not None:
            return self._shadow
        values = self._sheet_values()
        shadow = {}
        for rng in self.ws.merged_cells.ranges:
            top_left = values.get((rng.min_row, rng.min_col))
            if top_left is None:
                continue
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    if r == rng.min_row and c == rng.min_col:
                        continue
                    shadow[(r, c)] = top_left
        self._shadow = shadow
        return shadow

    def _raw(self, row, col):
        """原始单元格值（含合并区域左上角的真实值）"""
        return self._sheet_values().get((row, col))

    def _cell(self, row, col, shadow):
        value = self._raw(row, col)
        if value is None:
            return shadow.get((row, col))
        return value

    # ── 步骤 2：自动定位表头行 ──

    def _detect_header_rows(self):
        """
        在前 N 行里找表头行，不写死行号。

        判定标准（三个条件同时满足，顺序即筛除力）：
          1. **纵向合并覆盖率 >= min_header_merge_ratio**
             表头行之间普遍存在上下合并（A3:A4）或横向分组合并（AL3:AU3），
             数据行不会。这一条把标题行（A1:AY1 / A2:AY2 只是横向合并，
             没有纵向合并）干净地滤掉。
          2. 展开合并后非空值个数 >= min_header_cols
             —— 用展开后的值是为了兼容「大类只在最左列有值」的复合表头。
          3. **实格数 >= min_header_actual_cols**
             ⚠️ 这一条是实测踩出来的：真实钉钉文件里，数据落在 1..48 列，
                但表头块横跨 1..51 列，于是第 4 行只有 13 个实格
                （子类名 + 末尾三个转调休子类），填充率仅 13/51 ≈ 25%。
                最初用「实格 / 总列数 >= 50%」判定，会把第 4 行整个丢掉，
                结果只识别出 [3] 一行表头，10 个请假子类全部退化成未知列。
                改成绝对下限（>=5 个实格）后既保住第 4 行，又挡得住
                只有 1 个实格的标题行。

        取**最后一段连续命中行**作为表头。
        """
        scan = self.config.get('header_scan_rows', 10)
        min_cols = self.config.get('min_header_cols', 30)
        min_actual = self.config.get('min_header_actual_cols', 5)
        min_merge_ratio = self.config.get('min_header_merge_ratio', 0.5)
        shadow = self._merged_value_map()

        max_row = min(scan, self.ws.max_row or scan)
        max_col = self.ws.max_column or 0

        # 每列的纵向合并覆盖区间（表头块的典型特征）
        vertical_spans = []          # [(min_row, max_row)]
        for rng in self.ws.merged_cells.ranges:
            if rng.max_row > rng.min_row:
                vertical_spans.append((rng.min_row, rng.max_row))

        def merge_ratio(row):
            if not vertical_spans or not max_col:
                return 0.0
            covered = 0
            for c in range(1, max_col + 1):
                for lo, hi in vertical_spans:
                    if lo <= row <= hi:
                        covered += 1
                        break
            return covered / max_col

        candidates = []
        for r in range(1, max_row + 1):
            expanded = actual = 0
            for c in range(1, max_col + 1):
                if normalize_text(self._cell(r, c, shadow)) is not None:
                    expanded += 1
                if normalize_text(self._raw(r, c)) is not None:
                    actual += 1
            if (merge_ratio(r) >= min_merge_ratio
                    and expanded >= min_cols
                    and actual >= min_actual):
                candidates.append(r)

        if not candidates:
            raise ImportError_(
                f'在前 {scan} 行内找不到表头：需要某一行有纵向合并、'
                f'展开后 ≥{min_cols} 个值、且 ≥{min_actual} 个实格。'
                f'请确认上传的是钉钉导出的「每日统计」表。'
            )

        # 从后往前收集连续的一段
        rows = [candidates[-1]]
        for r in reversed(candidates[:-1]):
            if r == rows[0] - 1:
                rows.insert(0, r)
            else:
                break
        return rows

    # ── 步骤 3：拼装列定义 ──

    def parse(self):
        shadow = self._merged_value_map()
        self.header_row_numbers = self._detect_header_rows()

        # 表头行的语义：钉钉是「倒数第二行=大类，最后一行=叶子」。
        # 单层表头时两行是同一个值（垂直合并），复合表头时大类只在最左列
        # 出现、其余列要靠合并单元格展开才能拿到。
        #
        # ⚠️ 这里绝不能把两行的取值堆成一个列表再取首尾 —— 实测会踩坑：
        #    第 38 列的第 3 行是「请假」、第 4 行是「事假(小时)」，
        #    但第 39 列的第 3 行从【影子表】取到的也是「请假」，
        #    于是 values = ['请假','请假','事假(小时)']，values[0] == values[-1]，
        #    会被误判成单层列，10 个请假子类全部丢失。
        #    必须固定按「行号」取，而不是按值的位置取。
        rows_seq = self.header_row_numbers
        leaf_row = rows_seq[-1]
        group_row = rows_seq[-2] if len(rows_seq) >= 2 else None

        max_col = self.ws.max_column or 0
        for c in range(1, max_col + 1):
            leaf = normalize_text(self._cell(leaf_row, c, shadow))
            if leaf is None:
                continue

            group = None
            if group_row is not None:
                above = normalize_text(self._cell(group_row, c, shadow))
                # 大类与叶子同名 → 其实是单层表头（垂直合并单元格）
                if above and above != leaf:
                    group = above

            if group:
                key = f'{group}|{leaf}'      # 复合列：大类|子类
            else:
                key = leaf

            self.columns.append({
                'index': c - 1,           # 0-based，供 row[..] 使用
                'letter': openpyxl.utils.get_column_letter(c),
                'top': group,
                'sub': leaf if group else None,
                'key': key,
                'group': group,
                'leaf': leaf,
            })

        if not self.columns:
            raise ImportError_('表头解析结果为空，请确认文件格式。')
        return self.columns

    # ── 步骤 4：列名 → 模型字段名 ──

    def build_field_index(self):
        """
        返回 {模型字段名: 列下标(0-based)}，以及未识别列的清单。

        ⚠️ 关键点：`请假` 大类下有 10 个子类，全部映射到不同字段。
           桌面版的 ExcelImporter 只读第一行表头、不展开合并单元格，
           于是 'AW'（调休）等 9 列的值全丢。
        """
        field_index = {}
        unknown = []

        for col in self.columns:
            if col['group']:
                sub_map = mapping.DAILY_COMPOSITE_FIELDS.get(col['group'])
                if sub_map and col['leaf'] in sub_map:
                    field_index[sub_map[col['leaf']]] = col['index']
                    continue
                unknown.append(col['key'])
                continue

            field_name = mapping.DAILY_SIMPLE_FIELDS.get(col['leaf'])
            if field_name:
                field_index[field_name] = col['index']
            else:
                unknown.append(col['key'])

        return field_index, unknown

    def read_data_rows(self, min_row=None):
        """
        逐行产出 (excel行号, 列表)，跳过整行为空的行。

        ⚠️ 性能：必须用 ws.iter_rows() **批量读**，不能 `ws.cell(r, c)` 逐格读。
           实测同一个文件（2759 行 × 51 列）：
               iter_rows 批量读 →  0.4s
               cell 逐格读      → 29.0s   ← 慢 70 倍
           openpyxl 的 ws.cell() 每次调用都要走一遍 Worksheet 的坐标解析，
           在 14 万次调用下开销爆炸。这是上传慢的主因。
        """
        start = min_row or (self.header_row_numbers[-1] + 1)
        max_row = self.ws.max_row or 0
        if max_row < start:
            return
        max_col = self.ws.max_column or 0

        for offset, row in enumerate(
                self.ws.iter_rows(min_row=start, max_row=max_row,
                                  min_col=1, max_col=max_col, values_only=True)):
            if all(normalize_text(v) is None for v in row):
                continue
            yield start + offset, list(row)


def load_workbook_plain(path):
    """
    以**普通模式**加载工作簿。

    ⚠️ 绝不能加 read_only=True：钉钉导出的 xlsx 缺少 dimension 元数据，
       实测 `max_row` / `max_column` 都变成 1，整张表被读空。
       （见计划书第 56 行，本项目在 probe01 中已复现。）
    """
    try:
        return openpyxl.load_workbook(path, data_only=True, read_only=False)
    except Exception as exc:
        raise ImportError_(f'无法打开 Excel 文件：{exc}') from exc


# ============================================================================
# 导入服务
# ============================================================================

class ExcelImporter:
    """钉钉数据入库"""

    # ── 日考勤 ──

    @staticmethod
    def _is_placeholder_row(values, field_index, rule):
        """
        判断某人是否属于「系统占位账号」应整人排除。

        ⚠️ 判定必须是**合取**，不能只看考勤组：实测「未加入考勤组」这个考勤组里
           混着真实员工（有部门、有工号、当月有真实出勤与完整打卡记录）。
           只看考勤组会直接丢掉真实数据。

        默认严格度 `no_dept` 的判据：考勤组命中 ∧ 无部门。
        命中者通常是系统集成账号（例如职位字段写着某个集成流程的名字）。

        返回 (是否排除, 原因)。
        """
        name_idx = field_index.get('name')
        name = normalize_text(values[name_idx]) if name_idx is not None else None

        # 1) 人工名单优先（显式意图，且不受考勤组规则影响）
        if name and rule.excluded_employees and name in rule.excluded_employees:
            return True, f'在排除名单中（{name}）'

        mode = getattr(rule, 'exclude_mode', 'no_dept') or 'no_dept'
        if mode == 'name_only' or not rule.excluded_attend_groups:
            return False, None

        def raw(field):
            idx = field_index.get(field)
            if idx is None or idx >= len(values):
                return None
            return normalize_text(values[idx])

        group = raw('attend_group')
        if not group or group not in rule.excluded_attend_groups:
            return False, None

        reasons = [f'考勤组为「{group}」']

        # 'no_dept' 与 'no_dept_no' 都要求「无部门」——
        # 这是防止误伤真人的承重条件，不能省。
        if mode in ('no_dept', 'no_dept_no'):
            if raw('department'):
                return False, None          # 有部门 → 是真人，绝不能排除
            reasons.append('无部门')

        if mode == 'no_dept_no':
            if raw('employee_no'):
                return False, None
            reasons.append('无工号')

        return True, '、'.join(reasons)

    # 判定「这一行是否含有真实考勤数据」时要看的字段。
    # 数值型字段为 0（或空）不算有数据；文本型字段非空即算。
    _HAS_DATA_TEXT_FIELDS = (
        'in1_time', 'out1_time', 'in2_time', 'out2_time', 'in3_time', 'out3_time',
        'in1_result', 'out1_result', 'shift', 'approval_ref',
        'business_trip_hours', 'field_work_hours',
    )
    _HAS_DATA_NUMERIC_FIELDS = (
        'attend_days', 'rest_days', 'work_minutes', 'overtime_total',
        'late_minutes', 'early_leave_minutes', 'missing_in_count',
        'missing_out_count', 'absenteeism_days', 'overtime_workday',
        'overtime_restday', 'overtime_holiday',
    )

    @classmethod
    def _row_has_data(cls, values, field_index, parser):
        """
        这一行是否含真实考勤数据。

        判定要宽松但避开误判：只要打卡时间、出勤天数、工作时长、加班、
        任一考勤统计或任一请假子类里有非零值，就算真人。
        实测钉钉对「未加入考勤组」的人会把上述字段**全部留空**。
        """
        for field in cls._HAS_DATA_TEXT_FIELDS:
            idx = field_index.get(field)
            if idx is not None and idx < len(values) and normalize_text(values[idx]):
                return True

        for field in cls._HAS_DATA_NUMERIC_FIELDS:
            idx = field_index.get(field)
            if idx is not None and idx < len(values) and to_decimal(values[idx]) != 0:
                return True

        # 10 个请假子类（列名固定，不依赖 build_field_index）
        for leaf in mapping.DAILY_COMPOSITE_FIELDS.get('请假', {}):
            idx = next((c['index'] for c in parser.columns
                        if c['group'] == '请假' and c['leaf'] == leaf), None)
            if idx is not None and idx < len(values) and to_decimal(values[idx]) != 0:
                return True

        return False

    @staticmethod
    def purge_daily_period(period):
        """
        删除某账期的全部日考勤记录（用于"覆盖"策略）。

        ⚠️ 为什么要整账期清掉，而不是只按唯一键 upsert：
           钉钉重新导出后可能整人消失（离职、移出考勤组），
           只做 upsert 会把这些人的旧行永远留在库里，月度汇总跟着错。
        返回 (删除的记录数, 受影响的文件数)。
        """
        year, month = (int(x) for x in period.split('-'))
        qs = AttendanceDaily.objects.filter(work_date__year=year, work_date__month=month)
        file_ids = set(qs.values_list('source_file_id', flat=True))
        deleted, _ = qs.delete()
        return deleted, len(file_ids)

    @staticmethod
    def purge_leave_period(period):
        """删除某账期的全部请假记录（用于"覆盖"策略）。返回 (记录数, 文件数)。"""
        qs = LeaveRecord.objects.filter(period=period)
        file_ids = set(qs.values_list('source_file_id', flat=True))
        deleted, _ = qs.delete()
        return deleted, len(file_ids)

    @classmethod
    def import_daily(cls, uploaded_file, user, rule=None, purge_existing=False):
        """
        导入日考勤汇总表。返回 (成功数, 跳过数, 报告dict)

        策略：
          · purge_existing=False（默认，"追加/覆盖"）：
            同一 (work_date, user_id) 已存在则覆盖更新，并计入报告。
          · purge_existing=True（"整账期覆盖"）：
            先清掉该账期全部旧记录，再整体写入 —— 这样钉钉重导后
            消失的人不会留下幽灵数据。
        """
        rule = rule or AttendanceRule.get_active()
        report = {'sheets': [], 'errors': [], 'warnings': [], 'excluded': []}

        wb = load_workbook_plain(uploaded_file.file_path)

        # ⚠️ 行数上限必须在 purge **之前**判定：否则一个超限文件会先把整个账期
        #    清空、再在读表阶段失败，用户当场丢掉当月数据。
        #    这里取各 Sheet 的 max_row 之和作为上界（含表头行），宁可略保守。
        total_rows = sum(wb[name].max_row for name in wb.sheetnames)
        if total_rows > settings.IMPORT_MAX_ROWS:
            wb.close()
            raise ImportError_(
                f'文件行数过多（约 {total_rows} 行），单次导入上限为 '
                f'{settings.IMPORT_MAX_ROWS} 行，请拆分后分批上传。'
            )

        if purge_existing and uploaded_file.period:
            purged, files = cls.purge_daily_period(uploaded_file.period)
            report['purged'] = {'period': uploaded_file.period,
                                'records': purged, 'files': files}

        total_ok = total_skip = 0

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_report = {'sheet': sheet_name, 'imported': 0, 'skipped': 0}

            parser = DingTalkHeaderParser(ws)
            parser.parse()
            field_index, unknown = parser.build_field_index()

            # 必要列缺失 → 明确报错，指出缺哪一列（对应计划书 7.2 的功能边界）
            required = {'name': '姓名', 'work_date_raw': '日期'}
            missing = [label for field, label in required.items() if field not in field_index]
            if missing:
                raise ImportError_(
                    f'Sheet「{sheet_name}」缺少必要列：{"、".join(missing)}。'
                    f'已识别到的表头为：{"、".join(c["key"] for c in parser.columns[:12])}…'
                )

            if unknown:
                report['warnings'].append(
                    f'Sheet「{sheet_name}」有 {len(unknown)} 个未映射列（已忽略）：'
                    + '、'.join(unknown[:10])
                )

            # 先扫一遍：找出占位行的人，整人排除
            # ⚠️ 条件里必须包含 rule.exclude_if_no_data —— 漏掉它会导致
            #    「只勾选无数据排除、没填考勤组规则」时整段扫描被跳过，
            #    占位行原样入库（这个 bug 曾让单元测试失败）。
            rows = list(parser.read_data_rows())
            excluded_names = set()
            if (rule.exclude_if_no_data
                    or rule.excluded_attend_groups
                    or rule.excluded_employees):
                per_person = {}
                for excel_row, values in rows:
                    name_idx = field_index.get('name')
                    raw_name = normalize_text(values[name_idx]) if name_idx is not None else None
                    if not raw_name:
                        continue
                    # 与入库时保持同一套归一化，否则排除名单会因「（离职）」后缀对不上
                    name, _ = normalize_resigned_name(raw_name)
                    info = per_person.setdefault(name, {'has_data': False, 'excluded': None})
                    is_ph, why = cls._is_placeholder_row(values, field_index, rule)
                    if is_ph:
                        info['excluded'] = why
                        continue
                    if cls._row_has_data(values, field_index, parser):
                        info['has_data'] = True

                for name, info in per_person.items():
                    if info['excluded']:
                        excluded_names.add(name)
                        report['excluded'].append({'name': name, 'reason': info['excluded']})
                    elif rule.exclude_if_no_data and not info['has_data']:
                        excluded_names.add(name)
                        report['excluded'].append({'name': name, 'reason': '全月无任何打卡与出勤数据'})

            period_hint = uploaded_file.period
            objects = []
            for excel_row, values in rows:
                name_idx = field_index['name']
                name = normalize_text(values[name_idx]) if name_idx < len(values) else None
                if not name:
                    sheet_report['skipped'] += 1
                    continue
                if name in excluded_names:
                    sheet_report['skipped'] += 1
                    continue

                date_idx = field_index['work_date_raw']
                raw_date = values[date_idx] if date_idx < len(values) else None
                work_date = parse_daily_date(raw_date, period_hint)
                if work_date is None:
                    report['errors'].append({
                        'sheet': sheet_name, 'row': excel_row, 'field': '日期', 'value': str(raw_date),
                        'message': '日期无法解析，该行已跳过',
                    })
                    sheet_report['skipped'] += 1
                    continue

                def get(field, default=None):
                    idx = field_index.get(field)
                    if idx is None or idx >= len(values):
                        return default
                    return values[idx]

                # ⚠️ 日考勤表的姓名也要去掉「（离职）」后缀：
                #    两边文件的写法不一致会导致**按人名对不上**：
                #      日考勤表：某某（离职）     请假单据：某某
                #    不归一化时，月度汇总会把他们当成两个人，
                #    出现"一个 N 天出勤 + 一个 0 天"的重复行（实测踩到）。
                raw_name = normalize_text(values[name_idx]) if name_idx < len(values) else None
                name, _is_resigned = normalize_resigned_name(raw_name)

                user_id = normalize_text(get('user_id')) or ''
                obj = AttendanceDaily(
                    work_date=work_date,
                    user_id=user_id,
                    name=name,
                    attend_group=normalize_text(get('attend_group')),
                    department=normalize_text(get('department')),
                    employee_no=normalize_text(get('employee_no')),
                    position=normalize_text(get('position')),
                    shift=normalize_text(get('shift')),
                    in1_time=normalize_text(get('in1_time')), in1_result=normalize_text(get('in1_result')),
                    out1_time=normalize_text(get('out1_time')), out1_result=normalize_text(get('out1_result')),
                    in2_time=normalize_text(get('in2_time')), in2_result=normalize_text(get('in2_result')),
                    out2_time=normalize_text(get('out2_time')), out2_result=normalize_text(get('out2_result')),
                    in3_time=normalize_text(get('in3_time')), in3_result=normalize_text(get('in3_result')),
                    out3_time=normalize_text(get('out3_time')), out3_result=normalize_text(get('out3_result')),
                    approval_ref=normalize_text(get('approval_ref')),
                    attend_days=to_decimal(get('attend_days')),
                    rest_days=to_decimal(get('rest_days')),
                    work_minutes=to_decimal(get('work_minutes'), None) if normalize_text(get('work_minutes')) else None,
                    late_count=int(to_decimal(get('late_count'))),
                    late_minutes=to_decimal(get('late_minutes')),
                    serious_late_count=int(to_decimal(get('serious_late_count'))),
                    serious_late_minutes=to_decimal(get('serious_late_minutes')),
                    absenteeism_late_days=to_decimal(get('absenteeism_late_days')),
                    early_leave_count=int(to_decimal(get('early_leave_count'))),
                    early_leave_minutes=to_decimal(get('early_leave_minutes')),
                    missing_in_count=int(to_decimal(get('missing_in_count'))),
                    missing_out_count=int(to_decimal(get('missing_out_count'))),
                    absenteeism_days=to_decimal(get('absenteeism_days')),
                    business_trip_hours=to_decimal(get('business_trip_hours')),
                    field_work_hours=to_decimal(get('field_work_hours')),
                    overtime_total=to_decimal(get('overtime_total')),
                    overtime_workday=to_decimal(get('overtime_workday')),
                    overtime_restday=to_decimal(get('overtime_restday')),
                    overtime_holiday=to_decimal(get('overtime_holiday')),
                    source_file=uploaded_file,
                    created_by=user,
                )

                # 10 类请假：天 → 小时 统一折算
                hours_per_day = Decimal(rule.standard_work_minutes) / Decimal('60')
                for leaf, field in mapping.DAILY_COMPOSITE_FIELDS.get('请假', {}).items():
                    idx = next((c['index'] for c in parser.columns
                                if c['group'] == '请假' and c['leaf'] == leaf), None)
                    if idx is None:
                        continue
                    raw = values[idx] if idx < len(values) else None
                    val = to_decimal(raw)
                    if mapping.DAILY_LEAVE_UNIT.get(leaf) == 'day':
                        val = val * hours_per_day
                    setattr(obj, field, val)

                objects.append(obj)

            total_ok += len(objects)
            total_skip += sheet_report['skipped']
            sheet_report['imported'] = len(objects)
            report['sheets'].append(sheet_report)

            # 批量入库 + 同键覆盖
            cls._bulk_upsert_daily(objects, uploaded_file, report)

        wb.close()
        return total_ok, total_skip, report

    @staticmethod
    @transaction.atomic
    def _bulk_upsert_daily(objects, uploaded_file, report):
        """
        按 (work_date, user_id) 覆盖写入。

        ⚠️ 归一化「（离职）」后缀后可能出现同键冲突：
           同一人若在源文件里既有「张三」又有「张三（离职）」两种写法，
           两者会归一到同一个名字；如果 user_id 还恰好相同，就会撞键。
           这里显式检测并记入报告，避免"静默少一行"。
        """
        if not objects:
            return

        seen = {}
        deduped = []
        for obj in objects:
            key = (obj.work_date, obj.user_id, obj.name)
            if key in seen:
                report.setdefault('errors', []).append({
                    'sheet': '-', 'row': '-', 'field': '姓名',
                    'value': obj.name,
                    'message': f'{obj.work_date} 出现重复记录（UserId={obj.user_id or "空"}），'
                               f'已保留后一条',
                })
                continue
            seen[key] = obj
            deduped.append(obj)
        objects = deduped

        # 先删掉同一批键的旧记录（含其他来源文件的），保证幂等。
        #
        # ⚠️ 不要用 for 循环逐键 delete()：2700 个键会发 2700 条 DELETE，
        #    实测 25 秒。
        # ⚠️ 也不要用单个 Q 对象把 2700 个条件 OR 起来：SQLite 会报
        #    "Expression tree is too large (maximum depth 1000)"（实测踩到），
        #    MySQL 的 max_allowed_packet 也是同性质的隐患。
        #    正确做法是**分块**：每块 200 个键一条 DELETE，10 余条语句搞定。
        keys = {(o.work_date, o.user_id) for o in objects if o.user_id}
        removed = 0
        for chunk in _chunked(sorted(keys), 200):
            cond = Q()
            for work_date, user_id in chunk:
                cond |= Q(work_date=work_date, user_id=user_id)
            removed += AttendanceDaily.objects.filter(cond).delete()[0]
        if removed:
            report.setdefault('overwritten', 0)
            report['overwritten'] += removed

        batch = settings.IMPORT_BATCH_SIZE
        for i in range(0, len(objects), batch):
            AttendanceDaily.objects.bulk_create(objects[i:i + batch], batch_size=batch)

    # ── 请假单据 ──

    @classmethod
    def import_leave(cls, uploaded_file, user, rule=None, purge_existing=False):
        """
        导入请假单据。返回 (成功数, 跳过数, 报告dict)

        策略同 import_daily：purge_existing=True 时先整账期清空再写入。
        ⚠️ 注意请假文件常含跨月记录，账期按「开始时间」归属（已确认口径）。
        """
        rule = rule or AttendanceRule.get_active()
        report = {'sheets': [], 'errors': [], 'warnings': []}

        wb = load_workbook_plain(uploaded_file.file_path)

        # ⚠️ 同 import_daily：行数上限必须在 purge 之前判定，避免先清空账期再失败。
        total_rows = sum(wb[name].max_row for name in wb.sheetnames)
        if total_rows > settings.IMPORT_MAX_ROWS:
            wb.close()
            raise ImportError_(
                f'文件行数过多（约 {total_rows} 行），单次导入上限为 '
                f'{settings.IMPORT_MAX_ROWS} 行，请拆分后分批上传。'
            )

        if purge_existing and uploaded_file.period:
            purged, files = cls.purge_leave_period(uploaded_file.period)
            report['purged'] = {'period': uploaded_file.period,
                                'records': purged, 'files': files}

        total_ok = total_skip = 0
        to_create = []

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            if ws.max_row is None or ws.max_row < 2:
                continue

            # ⚠️ 同上：一次性批量读完整个 Sheet，别逐格 ws.cell()。
            max_col = ws.max_column or 0
            all_rows = [list(r) for r in ws.iter_rows(
                min_row=1, max_row=ws.max_row, min_col=1, max_col=max_col,
                values_only=True)]

            header = {}
            for c, v in enumerate(all_rows[0], start=1):
                text = normalize_text(v)
                if text:
                    header[text] = c - 1     # 0-based

            # 先把本 Sheet 的「表头文字 → 字段名」映射建好
            present_fields = {}
            for text, idx in header.items():
                field = mapping.LEAVE_FIELDS.get(text)
                if field:
                    present_fields[field] = idx

            required_labels = {
                'applicant_name_raw': '发起人姓名',
                'leave_type': '请假类型',
                'start_time_raw': '开始时间',
                'duration_raw': '时长',
            }
            missing = [label for field, label in required_labels.items()
                       if field not in present_fields]
            if missing:
                # 这个 Sheet 不是请假单据（钉钉一次导出可能带多个附表），跳过并记录
                report['warnings'].append(
                    f'Sheet「{sheet_name}」缺少列 {"、".join(missing)}，已跳过该 Sheet'
                )
                continue

            sheet_ok = sheet_skip = 0
            for offset, row_values in enumerate(all_rows[1:], start=2):
                excel_row = offset
                raw = {field: (row_values[idx] if idx < len(row_values) else None)
                       for field, idx in present_fields.items()}

                if all(normalize_text(v) is None for v in raw.values()):
                    continue

                approval_no = normalize_text(raw.get('approval_no'))
                if not approval_no:
                    report['errors'].append({
                        'sheet': sheet_name, 'row': excel_row, 'field': '审批编号', 'value': '',
                        'message': '缺少审批编号，该行已跳过',
                    })
                    sheet_skip += 1
                    continue

                start_dt = parse_datetime(raw.get('start_time_raw'))
                if start_dt is None:
                    report['errors'].append({
                        'sheet': sheet_name, 'row': excel_row, 'field': '开始时间',
                        'value': str(raw.get('start_time_raw')), 'message': '开始时间无法解析，该行已跳过',
                    })
                    sheet_skip += 1
                    continue

                name_norm, is_resigned = normalize_resigned_name(raw.get('applicant_name_raw'))
                is_approved = (
                    normalize_text(raw.get('approval_status')) == mapping.LEAVE_STATUS_DONE
                    and normalize_text(raw.get('approval_result')) == mapping.LEAVE_RESULT_APPROVED
                )

                try:
                    hours = parse_duration_to_hours(raw.get('duration_raw'), rule.standard_work_minutes)
                except ImportError_ as exc:
                    report['errors'].append({
                        'sheet': sheet_name, 'row': excel_row, 'field': '时长',
                        'value': str(raw.get('duration_raw')), 'message': str(exc),
                    })
                    sheet_skip += 1
                    continue

                to_create.append(LeaveRecord(
                    approval_no=approval_no,
                    title=normalize_text(raw.get('title')),
                    approval_status=normalize_text(raw.get('approval_status')),
                    approval_result=normalize_text(raw.get('approval_result')),
                    submit_time=parse_datetime(raw.get('submit_time')),
                    finish_time=parse_datetime(raw.get('finish_time')),
                    applicant_no=normalize_text(raw.get('applicant_no')),
                    applicant_user_id=normalize_text(raw.get('applicant_user_id')),
                    applicant_name=name_norm or '',
                    applicant_name_raw=normalize_text(raw.get('applicant_name_raw')),
                    applicant_dept=normalize_text(raw.get('applicant_dept')),
                    leave_type=normalize_text(raw.get('leave_type')) or '',
                    start_time=start_dt,
                    end_time=parse_datetime(raw.get('end_time_raw')),
                    duration_hours=hours,
                    duration_raw=normalize_text(raw.get('duration_raw')),
                    reason=normalize_text(raw.get('reason')),
                    duration_days=(hours / (Decimal(rule.standard_work_minutes) / Decimal('60'))).quantize(Decimal('0.0001')),
                    is_resigned=is_resigned,
                    is_approved=is_approved,
                    period=period_of(start_dt),
                    source_file=uploaded_file,
                    created_by=user,
                ))
                sheet_ok += 1

            total_ok += sheet_ok
            total_skip += sheet_skip
            report['sheets'].append({'sheet': sheet_name, 'imported': sheet_ok, 'skipped': sheet_skip})

        # 审批编号唯一 → 已存在则更新
        cls._bulk_upsert_leave(to_create, report)
        wb.close()
        return total_ok, total_skip, report

    @staticmethod
    @transaction.atomic
    def _bulk_upsert_leave(records, report):
        if not records:
            return
        by_no = {r.approval_no: r for r in records}
        existing = set(
            LeaveRecord.objects.filter(approval_no__in=list(by_no)).values_list('approval_no', flat=True)
        )
        if existing:
            LeaveRecord.objects.filter(approval_no__in=list(existing)).delete()
            report['overwritten'] = len(existing)
        batch = settings.IMPORT_BATCH_SIZE
        records = list(by_no.values())
        for i in range(0, len(records), batch):
            LeaveRecord.objects.bulk_create(records[i:i + batch], batch_size=batch)


# ============================================================================
# 月度汇总计算
# ============================================================================

class AttendanceCalculator:
    """
    月度汇总 —— 规则全部来自 AttendanceRule，不硬编码。

    ⚠️ 桌面版实测的规则缺陷（本项目按已确认的口径修正）：
       1. 加班 = (工作时长 - 450) / 60，**不截断负值** → 负加班会冲减月度合计。
          本项目：clamp_negative_overtime=True 时取 max(0, ...)。
       2. 请假合计**无视审批状态** → 被拒记录被并入同类型总计。
          本项目：exclude_rejected_leave=True 时只统计 is_approved=True。
       3. 日期按字符串下标判断月份 → 10 月必错、11/12 月串月。
          本项目：按 work_date 的 date 对象过滤。
    """

    def __init__(self, rule=None):
        self.rule = rule or AttendanceRule.get_active()
        self.hours_per_day = Decimal(self.rule.standard_work_minutes) / Decimal('60')

    def daily_overtime(self, record):
        """单人单日加班小时数（已按规则决定是否截断负值）"""
        if record.work_minutes is None:
            return Decimal('0')
        raw = (Decimal(record.work_minutes) - Decimal(self.rule.standard_work_minutes)) / Decimal('60')
        if self.rule.clamp_negative_overtime and raw < 0:
            return Decimal('0')
        return raw

    def summarize_month(self, period):
        """
        汇总某个月（'YYYY-MM'）。返回 {姓名: {...}}，供页面与报表复用。

        注意：这里**不**写 Excel，只出结构化结果 —— 报表渲染在 report 模块，
        页面渲染在视图，两者共用同一份计算，避免口径漂移。
        """
        year, month = (int(x) for x in period.split('-'))
        start = date(year, month, 1)
        end = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)

        summary = defaultdict(lambda: {
            'name': '', 'department': '',
            'attend_should': Decimal(self.rule.monthly_standard_days),
            'attend_days': Decimal('0'),      # 出勤打卡（源文件累计）
            'actual_attend': Decimal('0'),    # 实际出勤
            'late_minutes': Decimal('0'), 'early_leave_minutes': Decimal('0'),
            'absenteeism_days': Decimal('0'),
            'missing_in_count': 0, 'missing_out_count': 0,
            'overtime_hours': Decimal('0'),
            'leave_hours': defaultdict(Decimal),
        })

        qs = AttendanceDaily.objects.filter(work_date__gte=start, work_date__lte=end) \
            .select_related('source_file').order_by('name', 'work_date')

        for rec in qs.iterator(chunk_size=2000):
            row = summary[rec.name]
            row['name'] = rec.name
            row['department'] = row['department'] or rec.department or ''
            row['attend_days'] += Decimal(rec.attend_days or 0)
            row['late_minutes'] += Decimal(rec.late_minutes or 0)
            row['early_leave_minutes'] += Decimal(rec.early_leave_minutes or 0)
            row['absenteeism_days'] += Decimal(rec.absenteeism_days or 0)
            row['missing_in_count'] += int(rec.missing_in_count or 0)
            row['missing_out_count'] += int(rec.missing_out_count or 0)
            row['overtime_hours'] += self.daily_overtime(rec)

        # 请假（来自请假单据，按类型汇总为小时 → 天）
        leave_qs = LeaveRecord.objects.filter(period=period)
        if self.rule.exclude_rejected_leave:
            leave_qs = leave_qs.filter(is_approved=True)

        for rec in leave_qs.iterator(chunk_size=2000):
            row = summary[rec.applicant_name]
            row['name'] = rec.applicant_name
            row['department'] = row['department'] or rec.applicant_dept or ''
            row['leave_hours'][rec.leave_type] += Decimal(rec.duration_hours or 0)

        # 实际出勤 = 出勤打卡 − 事假（沿用基线口径，见《第8节答复》第二部分第 4 题）
        for row in summary.values():
            personal_days = row['leave_hours'].get('事假', Decimal('0')) / self.hours_per_day
            row['actual_attend'] = row['attend_days'] - personal_days
            row['leave_days'] = {t: (h / self.hours_per_day) for t, h in row['leave_hours'].items()}

        return dict(summary)

    def summary_row_for(self, period, name):
        """取某人在某月的汇总行，用于单测与报表渲染"""
        return self.summarize_month(period).get(name)
