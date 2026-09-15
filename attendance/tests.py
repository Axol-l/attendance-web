"""
考勤模块测试

⚠️ 生产数据系统的四个 app tests.py 全是空骨架（计划书第 393 行）。
   本项目从第一天就写测试，且优先覆盖"最容易静默算错"的地方：
     · 多行表头 + 合并单元格展开（丢了就会少 9 个字段且不报错）
     · 日期解析（桌面版按下标取字符，10 月必错）
     · 时长单位换算
     · 加班负值截断、请假审批过滤
     · 权限边界

夹具用 openpyxl 在内存里造，形状与真实钉钉文件一致
（第 1-2 行标题、第 3-4 行两层表头且含合并单元格），
这样测试不依赖任何真实考勤数据文件。
"""
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserPermission

from . import mapping
from .models import AttendanceDaily, AttendanceRule, LeaveRecord, UploadedFile
from .services import (
    AttendanceCalculator,
    DingTalkHeaderParser,
    ImportError_,
    normalize_resigned_name,
    parse_daily_date,
    parse_duration_to_hours,
)


 # ============================================================================
# 夹具：程序化生成钉钉形状的工作簿
# ============================================================================

def _simple_fields_split():
    """把 DAILY_SIMPLE_FIELDS 的键切成「请假大类之前」与「之后」两段。

    真实钉钉文件的列顺序是：
        1-37  单层字段（姓名…外出时长）
        38-47 「请假」大类下的 10 个子列
        48    加班总时长
        49-51 「加班时长（转调休）」下的 3 个子列

    ⚠️ 夹具必须复刻这个顺序：如果像最初那样把请假列简单追加到最后，
       「加班总时长」就会被挤到请假大类左边，而它的第 4 行是空的 ——
       于是表头第 4 行会提前中断，解析器只会识别出第 3 行一个表头行，
       合并单元格展开也就无从谈起，测试会以"解析器坏了"的假象失败。
    """
    before, after = [], []
    for title in mapping.DAILY_SIMPLE_FIELDS:
        (after if title == '加班总时长' else before).append(title)
    return before, after


SIMPLE_BEFORE_LEAVE, SIMPLE_AFTER_LEAVE = _simple_fields_split()

# 全部 51 列的表头排布，与真实文件一致
DAILY_COLUMN_PLAN = (
    [(t, None) for t in SIMPLE_BEFORE_LEAVE]
    + [(leaf, '请假') for leaf in mapping.DAILY_COMPOSITE_FIELDS['请假']]
    + [(t, None) for t in SIMPLE_AFTER_LEAVE]
    + [(leaf, '加班时长（转调休）')
       for leaf in mapping.DAILY_COMPOSITE_FIELDS['加班时长（转调休）']]
)


def build_dingtalk_daily_workbook(path, rows):
    """
    生成一个结构与钉钉「每日统计」一致的 xlsx：
      行1 标题、行2 生成时间、行3 大类表头、行4 子类表头、行5+ 数据
    并对每个大类做合并单元格（复现 AW3:AY3 那种结构）。
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '每日统计'

    ws.cell(1, 1, '每日统计 统计日期：2014-08-01 至 2014-08-31')
    ws.cell(2, 1, '报表生成时间：2014-09-02 19:40')
    # 真实文件里第 1、2 行是横跨全表的合并单元格（A1:AY1、A2:AY2），
    # 夹具一并复刻，保证合并区域总数与真实文件一致（42 个）。
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(DAILY_COLUMN_PLAN))
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(DAILY_COLUMN_PLAN))

    # 把同一个大类的连续列聚成一段，便于合并
    groups = []          # [(group_name|None, start_col, end_col)]
    for idx, (title, group) in enumerate(DAILY_COLUMN_PLAN, start=1):
        if group:
            if groups and groups[-1][0] == group:
                groups[-1] = (group, groups[-1][1], idx)
            else:
                groups.append((group, idx, idx))
        else:
            groups.append((None, idx, idx))
        ws.cell(4 if group else 3, idx, title)
        if not group:
            ws.merge_cells(start_row=3, start_column=idx, end_row=4, end_column=idx)

    for group, start, end in groups:
        if group:
            ws.cell(3, start, group)
            ws.merge_cells(start_row=3, start_column=start, end_row=3, end_column=end)

    for r, data in enumerate(rows, start=5):
        for idx, (title, _group) in enumerate(DAILY_COLUMN_PLAN, start=1):
            if title in data:
                ws.cell(r, idx, data[title])

    wb.save(path)
    return path


def build_leave_workbook(path, rows):
    """生成结构与钉钉「请假单据」一致的 xlsx（单行表头）"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '201403242101000262'
    headers = list(mapping.LEAVE_FIELDS.keys())
    for i, h in enumerate(headers, start=1):
        ws.cell(1, i, h)
    for r, data in enumerate(rows, start=2):
        for i, h in enumerate(headers, start=1):
            if h in data:
                ws.cell(r, i, data[h])
    wb.save(path)
    return path


# ============================================================================
# 纯函数：日期解析
# ============================================================================

class ParseDailyDateTests(TestCase):
    def test_parses_real_dingtalk_format(self):
        """实测格式 '14-08-01 星期五'"""
        self.assertEqual(parse_daily_date('14-08-01 星期五', '2014-08'), date(2014, 8, 1))
        self.assertEqual(parse_daily_date('14-08-31 星期日', '2014-08'), date(2014, 8, 31))

    def test_october_is_not_lost(self):
        """
        ⚠️ 这是桌面版最严重的日期缺陷：'2014-10-06'[6] == '0'，
           与用户输入的 '10' 永不相等，10 月整月数据被静默丢弃。
        """
        self.assertEqual(parse_daily_date('14-10-06 星期一', '2014-10'), date(2014, 10, 6))
        self.assertEqual(parse_daily_date('14-11-06 星期四', '2014-11'), date(2014, 11, 6))
        self.assertEqual(parse_daily_date('14-12-06 星期六', '2014-12'), date(2014, 12, 6))

    def test_january_and_november_do_not_collide(self):
        """
        ⚠️ 桌面版用单字符比较：输入 '1' 时 '14-11-06 星期四'[6] 也是 '1'，
           会把 11 月的数据算进 1 月。这里必须互不串月。
        """
        jan = parse_daily_date('14-01-06 星期一', '2014-01')
        nov = parse_daily_date('14-11-06 星期四', '2014-11')
        self.assertEqual(jan, date(2014, 1, 6))
        self.assertEqual(nov, date(2014, 11, 6))
        self.assertNotEqual(jan.month, nov.month)

    def test_garbage_returns_none(self):
        for bad in (None, '', '   ', '不是日期', '2014/13/45'):
            self.assertIsNone(parse_daily_date(bad, '2014-08'), f'{bad!r} 应解析失败')


# ============================================================================
# 纯函数：时长换算
# ============================================================================

class ParseDurationTests(TestCase):
    def test_hour_suffix(self):
        """实测取值含小数：'7.5小时' '0.5小时' '22.5小时'"""
        self.assertEqual(parse_duration_to_hours('7.5小时'), Decimal('7.5'))
        self.assertEqual(parse_duration_to_hours('0.5小时'), Decimal('0.5'))
        self.assertEqual(parse_duration_to_hours('3小时'), Decimal('3'))

    def test_day_suffix_converts_to_hours(self):
        """'天' 必须按标准工作时长折算成小时：12天 × 7.5 = 90 小时"""
        self.assertEqual(parse_duration_to_hours('12天'), Decimal('90.0'))
        self.assertEqual(parse_duration_to_hours('1天'), Decimal('7.5'))

    def test_custom_standard_minutes(self):
        self.assertEqual(parse_duration_to_hours('2天', 480), Decimal('16'))

    def test_empty_is_zero(self):
        self.assertEqual(parse_duration_to_hours(None), Decimal('0'))
        self.assertEqual(parse_duration_to_hours(''), Decimal('0'))

    def test_unparseable_raises_with_value(self):
        """
        ⚠️ 桌面版用 f_val[:-2] 硬切字符串：钉钉若把单位改成 '7.5H' 或 '7.5 小时'，
           会切出垃圾而不报错。这里必须明确报错，且错误信息里带上原值。
        """
        with self.assertRaises(ImportError_) as ctx:
            parse_duration_to_hours('7.5H')
        self.assertIn('7.5H', str(ctx.exception))

        with self.assertRaises(ImportError_):
            parse_duration_to_hours('abc小时')


# ============================================================================
# 纯函数：离职标记归一化
# ============================================================================

class ResignedNameTests(TestCase):
    def test_halfwidth_marker(self):
        """实测源数据：'员工09(已离职)'"""
        name, resigned = normalize_resigned_name('员工09(已离职)')
        self.assertEqual(name, '员工09')
        self.assertTrue(resigned)

    def test_fullwidth_marker(self):
        name, resigned = normalize_resigned_name('员工03（离职）')
        self.assertEqual(name, '员工03')
        self.assertTrue(resigned)

    def test_normal_name_untouched(self):
        name, resigned = normalize_resigned_name('员工01')
        self.assertEqual(name, '员工01')
        self.assertFalse(resigned)


# ============================================================================
# 映射表不变量（Phase 5）
# ============================================================================
#
# 为什么单独测「映射」这一层：
#   mapping.py 是纯数据，改它不会报错，只会**静默算错/丢列**。
#   两条真实的失效路径：
#     1) DAILY_SIMPLE_FIELDS 的值拼错 → 导入时 get('xxx') 返回 None → 该列全空；
#     2) DAILY_COMPOSITE_FIELDS 的值拼错 → setattr(obj, 'xxx', val) 只是挂了个
#        实例属性，Django 保存时直接忽略 → 该列全空。
#   两种都不抛异常，只有拿真实文件对账才会发现。
#   下面用「双向覆盖」把它变成会红的测试。

# 解析器的中间键：只在导入过程中用，不对应模型字段
DAILY_PARSER_ONLY_KEYS = {'work_date_raw', 'work_date_ms'}
LEAVE_PARSER_ONLY_KEYS = {'start_time_raw', 'end_time_raw'}

# 由导入逻辑派生而非直接映射的模型字段
DAILY_DERIVED_FIELDS = {'work_date', 'source_file', 'created_by', 'created_at'}
LEAVE_DERIVED_FIELDS = {
    'applicant_name',      # 由 applicant_name_raw 归一化而来
    'start_time', 'end_time',      # 由 *_raw 解析而来
    'duration_hours', 'duration_days',   # 由 duration_raw 折算而来
    'is_resigned', 'is_approved', 'period',   # 派生标记
    'source_file', 'created_by', 'created_at',
}


class MappingInvariantTests(TestCase):
    """mapping.py 是纯数据，改错了不会报错 —— 用不变量守住。"""

    # ── 结构不变量 ──

    def test_daily_column_plan_totals_51(self):
        """真实钉钉文件是 51 列，夹具与映射必须一致。"""
        self.assertEqual(len(DAILY_COLUMN_PLAN), 51)

    def test_leave_subcolumns_are_ten(self):
        """日考勤表「请假」大类下必须正好 10 个子列。"""
        self.assertEqual(len(mapping.DAILY_COMPOSITE_FIELDS['请假']), 10)

    def test_overtime_subcolumns_are_three(self):
        self.assertEqual(len(mapping.DAILY_COMPOSITE_FIELDS['加班时长（转调休）']), 3)

    def test_report_leave_columns_are_nine_and_unique(self):
        codes = [code for code, _label in mapping.REPORT_LEAVE_COLUMNS]
        self.assertEqual(len(codes), 9)
        self.assertEqual(len(set(codes)), 9, '报表请假列不允许重复')

    def test_weekday_cn_covers_seven_days(self):
        self.assertEqual(len(mapping.WEEKDAY_CN), 7)
        self.assertEqual(mapping.WEEKDAY_CN, '一二三四五六日')

    # ── 单位与颜色的取值域 ──

    def test_leave_unit_covers_every_leave_subcolumn(self):
        """每个请假子列都要有单位定义，否则折算会走错分支。"""
        leaves = set(mapping.DAILY_COMPOSITE_FIELDS['请假'])
        units = set(mapping.DAILY_LEAVE_UNIT)
        self.assertEqual(leaves - units, set(), '有请假子列缺单位定义')
        self.assertEqual(units - leaves, set(), '有单位定义对应不到子列')

    def test_leave_units_are_known_values(self):
        self.assertTrue(set(mapping.DAILY_LEAVE_UNIT.values()) <= {'hour', 'day'})

    def test_unit_suffix_dictionary_covers_both_units(self):
        self.assertTrue({'hour', 'day'} <= set(mapping.DURATION_UNIT_SUFFIXES.values()))

    def test_status_color_map_values_are_six_digit_hex(self):
        """
        ⚠️ 必须是 6 位十六进制。写 8 位会让 report.argb() 原样透传，
           而桌面版基线的色值是 6 位 —— 多了 alpha 通道就对不上。
        """
        for status, color in mapping.STATUS_COLOR_MAP.items():
            self.assertRegex(color, r'^[0-9A-F]{6}$', f'{status} 的色值格式不对')
        self.assertRegex(mapping.WEEKEND_FONT_COLOR, r'^[0-9A-F]{6}$')

    def test_attend_status_set_is_not_empty(self):
        self.assertTrue(mapping.DAILY_ATTEND_STATUS)

    # ── 默认值 ──

    def test_default_exclude_mode_is_a_valid_choice(self):
        valid = {code for code, _label in mapping.EXCLUDE_MODE_CHOICES}
        self.assertIn(mapping.DEFAULT_EXCLUDE_MODE, valid)

    def test_default_factories_return_independent_objects(self):
        """
        ⚠️ JSONField 的 default 若返回共享的可变对象，一次修改会污染后续所有实例
           （也因此不能用字面量当 default）。
        """
        groups_a = mapping.default_excluded_attend_groups()
        groups_b = mapping.default_excluded_attend_groups()
        groups_a.append('污染测试')
        self.assertNotIn('污染测试', groups_b)
        self.assertNotIn('污染测试', mapping.DEFAULT_EXCLUDED_ATTEND_GROUPS)

        colors_a = mapping.default_color_map()
        colors_b = mapping.default_color_map()
        colors_a['新增状态'] = 'FFFFFF'
        self.assertNotIn('新增状态', colors_b)

    # ── 双向覆盖：映射 ↔ 模型字段 ──

    def _daily_mapping_targets(self):
        targets = set(mapping.DAILY_SIMPLE_FIELDS.values())
        for group in mapping.DAILY_COMPOSITE_FIELDS.values():
            targets |= set(group.values())
        return targets - DAILY_PARSER_ONLY_KEYS

    def _model_fields(self, model):
        return {f.name for f in model._meta.get_fields()
                if getattr(f, 'concrete', False)} - {'id'}

    def test_every_daily_mapping_target_is_a_real_model_field(self):
        """映射指向的每个字段都必须真实存在，拼错即报错。"""
        bogus = self._daily_mapping_targets() - self._model_fields(AttendanceDaily)
        self.assertEqual(bogus, set(), f'映射指向了不存在的字段：{sorted(bogus)}')

    def test_every_daily_model_field_is_reachable_from_mapping(self):
        """
        反向覆盖：除派生字段外，AttendanceDaily 的每个字段都要有映射来源。
        少了就意味着这一列永远不会被导入（静默丢列）。
        """
        covered = self._daily_mapping_targets() | DAILY_DERIVED_FIELDS
        uncovered = self._model_fields(AttendanceDaily) - covered
        self.assertEqual(uncovered, set(), f'这些字段没有任何映射来源：{sorted(uncovered)}')

    def test_every_leave_mapping_target_is_a_real_model_field(self):
        bogus = (set(mapping.LEAVE_FIELDS.values()) - LEAVE_PARSER_ONLY_KEYS
                 - self._model_fields(LeaveRecord))
        self.assertEqual(bogus, set(), f'映射指向了不存在的字段：{sorted(bogus)}')

    def test_every_leave_model_field_is_reachable_from_mapping(self):
        covered = (set(mapping.LEAVE_FIELDS.values()) - LEAVE_PARSER_ONLY_KEYS
                   | LEAVE_DERIVED_FIELDS)
        uncovered = self._model_fields(LeaveRecord) - covered
        self.assertEqual(uncovered, set(), f'这些字段没有任何映射来源：{sorted(uncovered)}')

    # ── 类型口径 ──

    def test_daily_and_report_leave_types_intersect_in_seven(self):
        """
        ⚠️ 已确认的业务事实：日考勤表的 10 个请假子类与请假单据的 9 个类型
           **交集只有 7 种**。报表以请假单据那套为准。
           两个集合的差集必须正好是这几项，否则说明有人动了类型口径 ——
           那会直接改变月报的请假分组，必须连同确认单一起更新。
        """
        daily = {leaf.split('(')[0].split('（')[0]
                 for leaf in mapping.DAILY_COMPOSITE_FIELDS['请假']}
        report = {code for code, _label in mapping.REPORT_LEAVE_COLUMNS}

        self.assertEqual(daily & report,
                         {'事假', '调休', '年假', '病假', '婚假', '陪产假', '产假'})
        self.assertEqual(daily - report, {'例假', '丧假', '哺乳假'})
        self.assertEqual(report - daily, {'产前假', '产检假'})

    def test_report_leave_columns_match_normalize_keys(self):
        """报表列必须都能被类型归一化命中，否则汇总时取不到值。"""
        report = {code for code, _label in mapping.REPORT_LEAVE_COLUMNS}
        self.assertEqual(report, set(mapping.LEAVE_TYPE_NORMALIZE.values()))
        self.assertEqual(report, set(mapping.LEAVE_TYPE_NORMALIZE.keys()))

    def test_leave_type_normalize_is_idempotent(self):
        for value in mapping.LEAVE_TYPE_NORMALIZE.values():
            self.assertEqual(mapping.LEAVE_TYPE_NORMALIZE[value], value)

    def test_exclude_mode_choices_cover_all_four_modes(self):
        codes = {code for code, _label in mapping.EXCLUDE_MODE_CHOICES}
        self.assertEqual(codes, {'no_dept', 'no_dept_no', 'group_only', 'name_only'})

    # ── 模块纯度 ──

    def test_mapping_module_contains_no_logic(self):
        """
        ⚠️ 设计约定：字段映射是纯数据模块，便于 HR 调整而不用改代码。
           一旦引入 import（尤其是 models），它就不再是纯数据了。
        """
        import ast
        import pathlib

        source = pathlib.Path(mapping.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [], 'mapping.py 必须保持纯数据，不允许 import')

    def test_field_maps_use_header_text_not_column_index(self):
        """
        ⚠️ 桌面版到处是 row[26]/row[31] 这类硬编码列号，钉钉一加列就静默错位。
           本项目的映射键必须是表头文字。
        """
        for label, field_map in (('DAILY_SIMPLE_FIELDS', mapping.DAILY_SIMPLE_FIELDS),
                                 ('LEAVE_FIELDS', mapping.LEAVE_FIELDS)):
            for key in field_map:
                self.assertIsInstance(key, str, f'{label} 的键必须是表头文字')
                self.assertNotRegex(str(key), r'^\d+$',
                                    f'{label} 里出现了列号式键 {key!r}')
        for group in mapping.DAILY_COMPOSITE_FIELDS.values():
            for key in group:
                self.assertIsInstance(key, str)


# ============================================================================
# 多行表头 + 合并单元格解析器（Phase 2 核心）
# ============================================================================

class DingTalkHeaderParserTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmpdir = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmpdir, 'daily.xlsx')
        build_dingtalk_daily_workbook(cls.path, [
            {'姓名': '员工01', '部门': '研发一部', '日期': '14-08-01 星期五',
             '工作时长': 462, '事假(小时)': 2, '年假(天)': 1},
        ])
        cls.wb = openpyxl.load_workbook(cls.path, data_only=True)
    @classmethod
    def tearDownClass(cls):
        cls.wb.close()
        try:
            os.remove(cls.path)
            os.rmdir(cls.tmpdir)
        except OSError:
            pass
        super().tearDownClass()

    def test_detects_header_rows_automatically(self):
        """
        不写死行号：前两行是标题（只有 1 个非空格），表头必须落在第 3-4 行。
        两行都要识别出来 —— 少一行就丢了「请假」大类的 10 个子类。
        """
        parser = DingTalkHeaderParser(self.wb['每日统计'])
        parser.parse()
        self.assertEqual(parser.header_row_numbers, [3, 4])

    def test_fixture_matches_real_dingtalk_shape(self):
        """
        夹具本身要可信：与真实 8 月文件同构 —— 51 列、42 个合并区域。
        （实测真实文件：A1:AY1 / A2:AY2 标题行合并 + 38 个单层列的垂直合并
          + 请假 AL3:AU3 + 转调休 AW3:AY3 = 42）
        """
        ws = self.wb['每日统计']
        self.assertEqual(ws.max_column, 51)
        self.assertEqual(len(DAILY_COLUMN_PLAN), 51)
        self.assertEqual(len(ws.merged_cells.ranges), 42)

        merged = {str(r) for r in ws.merged_cells.ranges}
        # 复刻真实文件的关键合并结构
        self.assertIn('A1:AY1', merged)
        self.assertIn('A3:A4', merged)
        self.assertIn('AL3:AU3', merged)   # 请假大类：10 个子列
        self.assertIn('AW3:AY3', merged)   # 转调休大类：3 个子列（真实文件里就是这一条）

        # 38 个单层列的上下合并
        vertical = [r for r in ws.merged_cells.ranges if r.min_row == 3 and r.max_row == 4]
        self.assertEqual(len(vertical), 38)

    def test_merged_cells_are_expanded(self):
        """
        ⚠️ 核心断言：'请假' 大类在第 3 行是合并单元格，只有第一列有值。
           展开后，10 个子类列都必须归到 '请假' 组下。
           桌面版的 ExcelImporter 不展开合并，会丢掉其中 9 个字段。
        """
        parser = DingTalkHeaderParser(self.wb['每日统计'])
        parser.parse()
        leave_cols = [c for c in parser.columns if c['group'] == '请假']
        self.assertEqual(len(leave_cols), len(mapping.DAILY_COMPOSITE_FIELDS['请假']))
        self.assertEqual(
            [c['leaf'] for c in leave_cols],
            list(mapping.DAILY_COMPOSITE_FIELDS['请假'].keys()),
        )

    def test_all_ten_leave_fields_are_mapped(self):
        """10 类请假必须全部映射到不同模型字段 —— 一个都不能少"""
        parser = DingTalkHeaderParser(self.wb['每日统计'])
        parser.parse()
        field_index, _ = parser.build_field_index()

        expected = set(mapping.DAILY_COMPOSITE_FIELDS['请假'].values())
        self.assertEqual(expected, {f for f in expected if f in field_index})
        self.assertEqual(len(expected), 10)
        # 确认映射到的列互不相同（防止多个子类指向同一列）
        idxs = [field_index[f] for f in expected]
        self.assertEqual(len(idxs), len(set(idxs)))

    def test_simple_fields_are_mapped(self):
        parser = DingTalkHeaderParser(self.wb['每日统计'])
        parser.parse()
        field_index, unknown = parser.build_field_index()
        for field in ('name', 'department', 'work_date_raw', 'work_minutes', 'user_id'):
            self.assertIn(field, field_index)
        self.assertEqual(unknown, [])

    def test_missing_header_raises_actionable_error(self):
        """没有表头的文件必须明确报错，而不是静默读出 0 行"""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(1, 1, '随便一个表')
        with self.assertRaises(ImportError_) as ctx:
            DingTalkHeaderParser(ws).parse()
        self.assertIn('表头', str(ctx.exception))
        wb.close()


# ============================================================================
# 导入：日考勤
# ============================================================================

class ImportDailyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('hr01', password='x')
        self.rule = AttendanceRule.get_active()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_upload(self, rows):
        path = os.path.join(self.tmpdir, 'daily.xlsx')
        build_dingtalk_daily_workbook(path, rows)
        return UploadedFile.objects.create(
            original_filename='daily.xlsx', stored_filename='daily.xlsx',
            file_path=path, file_size=os.path.getsize(path), file_kind='daily',
            period='2014-08', uploaded_by=self.user,
        )

    def test_imports_rows_and_leave_columns(self):
        from .services import ExcelImporter
        up = self._make_upload([
            {'姓名': '员工01', '考勤组': '默认考勤组', '部门': '研发一部', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '工作时长': 462, '出勤天数': 1,
             '事假(小时)': 2, '年假(天)': 1, '调休(小时)': 3},
        ])
        ok, skipped, report = ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertEqual(ok, 1)
        rec = AttendanceDaily.objects.get()
        self.assertEqual(rec.name, '员工01')
        self.assertEqual(rec.work_date, date(2014, 8, 1))
        self.assertEqual(rec.leave_personal, Decimal('2'))
        # '年假(天)' 必须按 7.5 折算为小时
        self.assertEqual(rec.leave_annual, Decimal('7.5'))
        self.assertEqual(rec.leave_compensatory, Decimal('3'))

    def test_placeholder_person_is_excluded_by_rule(self):
        """
        ⚠️ 已确认口径：按结构化的「占位账号」规则排除，不按姓名硬编码。
           员工06的特征 = 考勤组「未加入考勤组」+ 无部门 + 无工号。
        """
        from .services import ExcelImporter
        up = self._make_upload([
            {'姓名': '员工06', '考勤组': '未加入考勤组', '职位': '系统集成占位账号',
             'UserId': 'U999', '日期': '14-08-01 星期五'},
            {'姓名': '员工01', '考勤组': '默认考勤组', '部门': '研发一部', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '工作时长': 462, '出勤天数': 1},
        ])
        ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertEqual(AttendanceDaily.objects.count(), 1)
        self.assertEqual(AttendanceDaily.objects.get().name, '员工01')

    def test_real_employee_in_unjoined_group_is_kept(self):
        """
        ⚠️ 关键回归：实测「未加入考勤组」里有 员工03（财务部，13 天真实出勤）。
           「无部门」是承重条件，不能省 —— 只看考勤组就会丢真实数据。
        """
        from .services import ExcelImporter
        up = self._make_upload([
            {'姓名': '员工03（离职）', '考勤组': '未加入考勤组', '部门': '财务部',
             '职位': '部门经理', 'UserId': 'U200', '日期': '14-08-01 星期五',
             '上班1打卡时间': '09:15', '工作时长': 462, '出勤天数': 1},
        ])
        ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertEqual(AttendanceDaily.objects.count(), 1,
                         '有部门的人是真员工，不能被"未加入考勤组"规则排除')
        # 姓名已归一化去掉「（离职）」后缀，才能与请假单据对上
        self.assertEqual(AttendanceDaily.objects.get().name, '员工03')

    def test_no_dept_no_mode_additionally_requires_no_employee_no(self):
        """
        最严格模式：考勤组命中 + 无部门 + 无工号。
        有部门或有工号的人都不能被排除。
        """
        from .services import ExcelImporter
        self.rule.exclude_mode = 'no_dept_no'
        self.rule.save()
        up = self._make_upload([
            {'姓名': '有部门', '考勤组': '未加入考勤组', '部门': '某部门',
             'UserId': 'U201', '日期': '14-08-01 星期五', '出勤天数': 1},
            {'姓名': '有工号', '考勤组': '未加入考勤组', '工号': 'E1234',
             'UserId': 'U202', '日期': '14-08-01 星期五', '出勤天数': 1},
            {'姓名': '全空壳', '考勤组': '未加入考勤组',
             'UserId': 'U203', '日期': '14-08-01 星期五'},
        ])
        ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertEqual(
            sorted(AttendanceDaily.objects.values_list('name', flat=True)),
            ['有工号', '有部门'],
            '只有"部门/工号全空"的人才应被排除',
        )

    def test_group_only_mode_excludes_whole_group(self):
        """
        宽松模式（group_only）：考勤组命中即排除。
        ⚠️ 这个模式会误伤真人，只保留给「确认某考勤组整组无效」的场景。
        注意姓名名单优先级更高 —— 这里用一个不在名单里的人来测。
        """
        from .services import ExcelImporter
        self.rule.exclude_mode = 'group_only'
        self.rule.save()
        up = self._make_upload([
            {'姓名': '某临时工', '考勤组': '未加入考勤组', '部门': '财务部',
             'UserId': 'U200', '日期': '14-08-01 星期五', '出勤天数': 1},
        ])
        ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertEqual(AttendanceDaily.objects.count(), 0,
                         'group_only 模式下有部门也会被排除（这正是它危险的地方）')

    def test_name_only_mode_ignores_group_rule(self):
        """仅按姓名名单：考勤组规则完全不生效"""
        from .services import ExcelImporter
        self.rule.exclude_mode = 'name_only'
        self.rule.excluded_employees = ['占位账号A']      # 测试自备，不依赖默认值
        self.rule.save()
        up = self._make_upload([
            {'姓名': '占位账号A', '考勤组': '默认考勤组', '部门': '某部门', '工号': 'E9',
             'UserId': 'U999', '日期': '14-08-01 星期五', '出勤天数': 1},
            {'姓名': '无名之辈', '考勤组': '未加入考勤组',
             'UserId': 'U300', '日期': '14-08-01 星期五', '出勤天数': 1},
        ])
        ExcelImporter.import_daily(up, self.user, self.rule)
        # 占位账号A 在名单里 → 排除；无名之辈只靠考勤组规则 → name_only 下保留
        self.assertEqual(
            [r.name for r in AttendanceDaily.objects.all()], ['无名之辈'])

    def test_person_with_no_data_is_kept_by_default(self):
        """
        ⚠️ 关键回归：逐格核对基线后确认 —— 全月无打卡的人**要保留**。
           基线 Sheet2 里含 员工05/员工13/员工10/员工07 等整月无打卡的真实员工
           （总裁、总监、产假中）。早期版本按"无数据"排除会让月报漏人。
        """
        from .services import ExcelImporter
        self.assertFalse(self.rule.exclude_if_no_data, '默认不应开启"无数据排除"')
        up = self._make_upload([
            {'姓名': '员工05', '考勤组': '示例公司', '部门': '总经理办公室', '职位': '总裁',
             'UserId': 'U300', '日期': '14-08-01 星期五'},
            {'姓名': '员工01', '考勤组': '默认考勤组', '部门': '研发一部', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15', '出勤天数': 1},
        ])
        ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertEqual(
            sorted(AttendanceDaily.objects.values_list('name', flat=True)),
            # ⚠️ 用 sorted() 包住期望值：这里断言的是"两个人都保留"，
            #    不依赖占位符的编号顺序（编号顺序会随脱敏映射调整而变）。
            sorted(['员工05', '员工01']),
            '整月无打卡的真实员工默认必须保留',
        )

    def test_explicit_name_list_still_works(self):
        """人工名单作为补充判据仍然生效（不依赖默认值）"""
        from .services import ExcelImporter
        self.rule.excluded_employees = ['占位账号A']
        self.rule.save()
        up = self._make_upload([
            {'姓名': '占位账号A', '考勤组': '示例公司', '部门': '某部门', '工号': 'E9',
             'UserId': 'U999', '日期': '14-08-01 星期五', '出勤天数': 1},
        ])
        ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertEqual(AttendanceDaily.objects.count(), 0,
                         '名单命中应排除，即使其它字段看起来像真人')

    def test_default_name_list_is_empty(self):
        """
        ⚠️ 隐私要求：默认的姓名排除名单必须为空。
           真实员工姓名不允许硬编码进代码/镜像。
        """
        from . import mapping
        self.assertEqual(mapping.DEFAULT_EXCLUDED_EMPLOYEES, [])
        self.assertEqual(AttendanceRule.get_active().excluded_employees, [])

    def test_no_real_employee_names_in_shipped_python(self):
        """
        ⚠️ 隐私回归：生产代码（排除 tests / migrations / tools / docs）里不得出现真实员工姓名。
           曾经在 mapping.py 与 models.py 的 help_text 里写过真实姓名，
           会随代码库与镜像一起散布出去。

        ⚠️ 本用例**刻意只存 SHA-256 摘要，不存姓名明文**：
           它的职责是"禁止真实姓名进入生产代码"，若把姓名明文写在这里，
           **测试文件自身就成了泄露源**（它会随代码库一起公开）。
           摘要同样能拦住"把姓名回填进生产代码"这件事。
        """
        import hashlib
        import pathlib
        import re

        # 已知真实姓名的摘要。明文只存在于私有环境的脱敏工具里。
        forbidden_digests = {
            '077e4a06a15888ca24eea7753a31a75ceddc9cc066a4dfc5a45a39b77042f00a',
            '1dad3e751169dce25ee638e3888b1cdebea8810ac58e271a3b3e30b5746695e3',
            '2dd74b075837e3a8e5176ac8755ae18b50bad9010c315c3c4d5ba6deae5caf1d',
            '4108089458552a06aa0885f9431dcfcf74d1a7b1111cbe79737425417c635d35',
            '4633821936d698915f00573dcae3e43a5e83edc44005d0e3635a83d84ec79bc3',
            '4691740353c62358b717269ebc99049b0172bca3c90c11f9ab301977f1e4dbdf',
            '55e437bdaf34b6c85dbb7f6c2515c51bd42124efa03288be66e5eafe1fc7fc88',
            '566b226b3ca0c71f853d457e01428eb8394b84129a8b125c588e468e3638ee4c',
            '5cf4fdab0e406904bcf7a8b555bdcc6adcb3a77af7a1350fd4c1709bfa834780',
            '64beb38f9e0e7d116d8a4703cf0f8fca5c6552fcbc1779526f54f7e7f0fbbbaf',
            '6722f728395904a1cdd863c83c5260f4c37bed0fab1f50109d797d14f59083e9',
            '6926bf563fc4c90453c9b113c138bba94afc8ae4336e273a35179ef5b8b04aab',
            '6bf3cfba93b0eed740fb343f2967ccf87b33b067c623df79b33bec1eee49b6da',
            '96528bbc9812161f36e213933dd19a13c5f04266f9350aa12fc5cd81766770f7',
            '9efca3a4d4a2847a75790734270776d96556458c9263c947a6431874f17f17da',
            'a4c5fe51ff3709a6f43ed1dc088756d36d916e28cb500f88ec09d766b5cdab23',
            'aaa57953b9f3507dbe32fdc51d0980996faaeee44a96fb5f5b4d0ee9e4f9f1f3',
            'b989e2e6bf6773739323e0f515009ec0b984e11605039af62ec37984d51eedc5',
            'c2fd004aa250bdbc28e5bcff7964f743c1a46d99ab116dfb00d0b5f6cb9b03ba',
            'c563916a25753dd2dfdfc9590c477a57150a6b5070f4648d02db56055557aa30',
            'cc25f6fa901957e2abe5bf07060df077cd7661e94ab883d8c684f4ea70901568',
            'cc4daa1934133220f484dcea05cc528e98031614ff708090330bd03d0fee7be9',
            'ce45a3874d5dce5156fc0c280c4787ab4f4a05c5e2a554960ab6cab2e9b30186',
            'db9c91bab3bb5d60b1156f686df31b71839e6bc5ca06619815c7583c1e182c3b',
            'e49572491f8b22e6aadacfebbea31081dfff14fa12fe906fe1b550fa9d766de1',
            'f413c274cf174e0a447983040ccffc0f7c202551e1138939ae50839c5ffb01ae',
            'fc4877b1762c818b734be729a7879343b4a3993df5984ad8183df0bf1048c744',
            'fd630b36cfa970aa2c3c07a500dc4dd301815b951e8ff1e5459b507863b19955',
        }

        def _digest(chunk):
            return hashlib.sha256(chunk.encode('utf-8')).hexdigest()

        root = pathlib.Path(__file__).resolve().parent.parent
        skip_dirs = {'venv', '__pycache__', 'migrations', 'tools', 'docs',
                     '.git', 'media', 'staticfiles', 'logs'}
        # 在每个连续汉字串里枚举长度 2/3/4 的子串做摘要比对 ——
        # 直接按"2-4 个汉字"整体匹配会漏掉夹在长句中的姓名。
        chinese_run = re.compile(r'[\u4e00-\u9fa5]{2,}')

        offenders = []
        for py in root.rglob('*.py'):
            if set(py.parts) & skip_dirs:
                continue
            if py.name.startswith('test'):
                continue
            text = py.read_text(encoding='utf-8', errors='ignore')
            for line_no, line in enumerate(text.splitlines(), start=1):
                for run in chinese_run.findall(line):
                    for start in range(len(run)):
                        for size in (2, 3, 4):
                            chunk = run[start:start + size]
                            if len(chunk) == size and _digest(chunk) in forbidden_digests:
                                offenders.append(
                                    f'{py.relative_to(root)}:{line_no}: '
                                    f'命中已知姓名的摘要')

        self.assertEqual(offenders, [],
                         '生产代码里出现了真实员工姓名，请改为通用描述：\n  '
                         + '\n  '.join(offenders[:10]))

    def test_daily_name_strips_resigned_suffix(self):
        """
        ⚠️ 关键回归：日考勤表的姓名必须去掉「（离职）」后缀。
           实测：日考勤表写 `员工03（离职）`，请假单据写 `员工03` ——
           不归一化时按人名对不上，月度汇总会出现
           "一个 13 天出勤 + 一个 0 天"的重复行。
        """
        from .services import ExcelImporter
        up = self._make_upload([
            {'姓名': '员工03（离职）', '考勤组': '未加入考勤组', '部门': '财务部',
             'UserId': 'U200', '日期': '14-08-01 星期五',
             '上班1打卡时间': '09:15', '出勤天数': 1},
        ])
        ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertEqual(AttendanceDaily.objects.get().name, '员工03',
                         '入库的姓名应与请假单据侧的写法一致')

    def test_daily_and_leave_names_join(self):
        """归一化后，日考勤与请假能在同一个人名下汇总"""
        from .services import ExcelImporter
        up = self._make_upload([
            {'姓名': '员工03（离职）', '考勤组': '未加入考勤组', '部门': '财务部',
             'UserId': 'U200', '日期': '14-08-01 星期五',
             '上班1打卡时间': '09:15', '出勤天数': 13},
        ])
        ExcelImporter.import_daily(up, self.user, self.rule)
        LeaveRecord.objects.create(
            approval_no='Z1', applicant_name='员工03', leave_type='病假',
            start_time=timezone.make_aware(datetime(2014, 8, 5, 9, 15),
                                           timezone.get_current_timezone()),
            duration_hours=Decimal('7.5'), duration_days=Decimal('1'),
            is_approved=True, period='2014-08', source_file=up, created_by=self.user,
        )
        summary = AttendanceCalculator(self.rule).summarize_month('2014-08')
        self.assertEqual(list(summary.keys()), ['员工03'], '不应出现两个员工03')
        self.assertEqual(summary['员工03']['attend_days'], Decimal('13'))
        self.assertEqual(summary['员工03']['leave_days']['病假'], Decimal('1'))

    def test_reexport_overwrites_same_day(self):
        """同 (日期, UserId) 重复导入 → 覆盖而不是产生重复行"""
        from .services import ExcelImporter
        up1 = self._make_upload([{'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
                                  '日期': '14-08-01 星期五', '工作时长': 462, '出勤天数': 1}])
        ExcelImporter.import_daily(up1, self.user, self.rule)
        up2 = self._make_upload([{'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
                                  '日期': '14-08-01 星期五', '工作时长': 500, '出勤天数': 1}])
        ExcelImporter.import_daily(up2, self.user, self.rule)
        self.assertEqual(AttendanceDaily.objects.count(), 1)
        self.assertEqual(AttendanceDaily.objects.get().work_minutes, Decimal('500'))

    def test_missing_required_column_reports_which_one(self):
        """
        缺少必要列时必须指出缺哪一列（计划书 7.2 功能边界）。

        做法：保留真实文件 51 列的形状与表头，只把「姓名」这一列改名成
        「名字」—— 覆盖"列都在但关键列认不出来"这个真实场景，比造一个
        残缺小文件更贴近线上。
        """
        from .services import ExcelImporter

        path = os.path.join(self.tmpdir, 'renamed.xlsx')
        build_dingtalk_daily_workbook(path, [
            {'姓名': '员工01', 'UserId': 'U001', '日期': '14-08-01 星期五',
             '工作时长': 462, '出勤天数': 1},
        ])
        wb = openpyxl.load_workbook(path)
        ws = wb.active
        for c in range(1, ws.max_column + 1):
            if ws.cell(3, c).value == '姓名':
                ws.cell(3, c, '名字')       # 改掉必要列的表头文字
                break
        wb.save(path)
        wb.close()

        up = UploadedFile.objects.create(
            original_filename='renamed.xlsx', stored_filename='renamed.xlsx', file_path=path,
            file_size=os.path.getsize(path), file_kind='daily', period='2014-08',
            uploaded_by=self.user,
        )
        with self.assertRaises(ImportError_) as ctx:
            ExcelImporter.import_daily(up, self.user, self.rule)
        message = str(ctx.exception)
        self.assertIn('姓名', message)
        self.assertIn('缺少必要列', message)

    def test_no_header_at_all_is_reported(self):
        """整张表没有表头特征时也要明确报错，而不是静默读出 0 行"""
        from .services import ExcelImporter

        path = os.path.join(self.tmpdir, 'noheader.xlsx')
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(1, 1, '随便一个表')
        ws.cell(2, 1, '什么表头都没有')
        wb.save(path)
        wb.close()

        up = UploadedFile.objects.create(
            original_filename='noheader.xlsx', stored_filename='noheader.xlsx', file_path=path,
            file_size=os.path.getsize(path), file_kind='daily', period='2014-08',
            uploaded_by=self.user,
        )
        with self.assertRaises(ImportError_) as ctx:
            ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertIn('表头', str(ctx.exception))


# ============================================================================
# 导入：请假单据
# ============================================================================

class ImportLeaveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('hr02', password='x')
        self.rule = AttendanceRule.get_active()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_upload(self, rows, name='leave.xlsx'):
        path = os.path.join(self.tmpdir, name)
        build_leave_workbook(path, rows)
        return UploadedFile.objects.create(
            original_filename=name, stored_filename=name, file_path=path,
            file_size=os.path.getsize(path), file_kind='leave',
            period='2014-08', uploaded_by=self.user,
        )

    def test_imports_and_normalizes(self):
        from .services import ExcelImporter
        up = self._make_upload([{
            '审批编号': 'A001', '审批状态': '完成', '审批结果': '同意',
            '发起人姓名': '员工02', '发起人部门': '制造中心', '请假类型': '调休',
            '开始时间': '2014-08-06 08:00', '结束时间': '2014-08-06 10:00', '时长': '2小时',
        }])
        ok, skipped, report = ExcelImporter.import_leave(up, self.user, self.rule)
        self.assertEqual(ok, 1)
        rec = LeaveRecord.objects.get()
        self.assertEqual(rec.leave_type, '调休')
        self.assertEqual(rec.duration_hours, Decimal('2'))
        self.assertEqual(rec.period, '2014-08')
        self.assertTrue(rec.is_approved)
        self.assertFalse(rec.is_resigned)

    def test_rejected_leave_marked_not_approved(self):
        """
        ⚠️ 已确认口径：审批=拒绝 的记录不进明细也不进合计。
           桌面版只挡了明细，合计仍会算进去（实测员工04调休 8.5h 含 1h 被拒记录）。
        """
        from .services import ExcelImporter
        up = self._make_upload([{
            '审批编号': 'A002', '审批状态': '完成', '审批结果': '拒绝',
            '发起人姓名': '员工04', '请假类型': '调休',
            '开始时间': '2014-08-12 09:15', '时长': '1小时',
        }])
        ExcelImporter.import_leave(up, self.user, self.rule)
        self.assertFalse(LeaveRecord.objects.get().is_approved)

    def test_resigned_name_normalized_on_import(self):
        from .services import ExcelImporter
        up = self._make_upload([{
            '审批编号': 'A003', '审批状态': '完成', '审批结果': '同意',
            '发起人姓名': '员工03(已离职)', '请假类型': '病假',
            '开始时间': '2014-08-05 09:15', '时长': '7.5小时',
        }])
        ExcelImporter.import_leave(up, self.user, self.rule)
        rec = LeaveRecord.objects.get()
        self.assertEqual(rec.applicant_name, '员工03')
        self.assertTrue(rec.is_resigned)
        self.assertEqual(rec.applicant_name_raw, '员工03(已离职)')

    def test_day_unit_converted(self):
        from .services import ExcelImporter
        up = self._make_upload([{
            '审批编号': 'A004', '审批状态': '完成', '审批结果': '同意',
            '发起人姓名': '员工07', '请假类型': '产假',
            '开始时间': '2014-08-01 09:15', '时长': '12天',
        }])
        ExcelImporter.import_leave(up, self.user, self.rule)
        rec = LeaveRecord.objects.get()
        self.assertEqual(rec.duration_hours, Decimal('90.0'))
        self.assertEqual(rec.duration_days, Decimal('12.0000'))

    def test_idempotent_reimport_by_approval_no(self):
        from .services import ExcelImporter
        row = {'审批编号': 'A005', '审批状态': '完成', '审批结果': '同意',
               '发起人姓名': '员工02', '请假类型': '调休',
               '开始时间': '2014-08-06 08:00', '时长': '2小时'}
        ExcelImporter.import_leave(self._make_upload([row]), self.user, self.rule)
        ExcelImporter.import_leave(self._make_upload([row], name='leave2.xlsx'), self.user, self.rule)
        self.assertEqual(LeaveRecord.objects.count(), 1)


# ============================================================================
# 月度汇总计算
# ============================================================================

class CalculatorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('hr03', password='x')
        self.rule = AttendanceRule.get_active()
        self.rule.exclude_if_no_data = False   # 便于直接造数据
        self.rule.save()
        self.up = UploadedFile.objects.create(
            original_filename='d.xlsx', stored_filename='d.xlsx', file_path='/tmp/d.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.user,
        )

    def _day(self, day, minutes, **kw):
        return AttendanceDaily.objects.create(
            work_date=date(2014, 8, day), user_id='U001', name='测试员',
            work_minutes=minutes, attend_days=kw.pop('attend_days', 1),
            source_file=self.up, created_by=self.user, **kw,
        )

    def test_overtime_excludes_standard_minutes(self):
        """加班 = (工作时长 − 450) / 60"""
        self._day(1, 462)      # 462-450 = 12 分 = 0.2 小时
        calc = AttendanceCalculator(self.rule)
        self.assertEqual(calc.daily_overtime(AttendanceDaily.objects.get()), Decimal('0.2'))

    def test_negative_overtime_clamped(self):
        """
        ⚠️ 已确认口径：负加班截断为 0。
           实测源文件有 83 人次工作时长不足 450 分钟，桌面版会让它们
           冲减月度合计（例：300 分 → −2.5 小时）。
        """
        rec = self._day(1, 300)
        self.assertTrue(self.rule.clamp_negative_overtime)
        self.assertEqual(AttendanceCalculator(self.rule).daily_overtime(rec), Decimal('0'))

    def test_negative_overtime_can_be_kept_for_fidelity(self):
        """规则可切换：保留负值以复现桌面版输出（用于 L1 逐格比对）"""
        self.rule.clamp_negative_overtime = False
        self.rule.save()
        rec = self._day(1, 300)
        self.assertEqual(AttendanceCalculator(self.rule).daily_overtime(rec), Decimal('-2.5'))

    def test_empty_work_minutes_counts_as_zero(self):
        rec = self._day(1, None)
        self.assertEqual(AttendanceCalculator(self.rule).daily_overtime(rec), Decimal('0'))

    def test_month_summary_aggregates(self):
        self._day(1, 462)     # +0.2
        self._day(2, 510)     # +1.0
        self._day(3, 900)     # +7.5
        summary = AttendanceCalculator(self.rule).summarize_month('2014-08')
        row = summary['测试员']
        self.assertEqual(row['overtime_hours'], Decimal('8.7'))
        self.assertEqual(row['attend_days'], Decimal('3'))

    def test_month_boundary_excludes_other_months(self):
        """⚠️ 必须按 date 对象过滤，不能按字符串下标判断月份"""
        self._day(1, 462)
        AttendanceDaily.objects.create(
            work_date=date(2014, 7, 31), user_id='U001', name='测试员',
            work_minutes=900, attend_days=1, source_file=self.up, created_by=self.user,
        )
        summary = AttendanceCalculator(self.rule).summarize_month('2014-08')
        self.assertEqual(summary['测试员']['overtime_hours'], Decimal('0.2'))

    def test_rejected_leave_not_counted(self):
        """
        ⚠️ 已确认口径：被拒/撤销的请假不进合计。
           桌面版把拒绝记录的时长并入了同类型总计。
        """
        aware = timezone.make_aware(datetime(2014, 8, 12, 9, 15),
                                    timezone.get_current_timezone())
        LeaveRecord.objects.create(
            approval_no='R1', applicant_name='测试员', leave_type='调休',
            start_time=aware, duration_hours=Decimal('7.5'),
            duration_days=Decimal('1'), is_approved=True, period='2014-08',
            source_file=self.up, created_by=self.user,
        )
        LeaveRecord.objects.create(
            approval_no='R2', applicant_name='测试员', leave_type='调休',
            start_time=aware, duration_hours=Decimal('1'),
            duration_days=Decimal('0.1333'), is_approved=False, period='2014-08',
            source_file=self.up, created_by=self.user,
        )
        row = AttendanceCalculator(self.rule).summarize_month('2014-08')['测试员']
        # 只算通过的 7.5 小时 → 1 天
        self.assertEqual(row['leave_days']['调休'], Decimal('1'))

    def test_actual_attend_is_absent_minus_personal_leave(self):
        """实际出勤 = 出勤打卡 − 事假（沿用基线口径）"""
        self._day(1, 462, attend_days=24)
        LeaveRecord.objects.create(
            approval_no='P1', applicant_name='测试员', leave_type='事假',
            start_time=timezone.make_aware(datetime(2014, 8, 1, 9, 15),
                                           timezone.get_current_timezone()),
            duration_hours=Decimal('15'),
            duration_days=Decimal('2'), is_approved=True, period='2014-08',
            source_file=self.up, created_by=self.user,
        )
        row = AttendanceCalculator(self.rule).summarize_month('2014-08')['测试员']
        self.assertEqual(row['attend_days'], Decimal('24'))
        self.assertEqual(row['actual_attend'], Decimal('22'))


# ============================================================================
# 规则模型
# ============================================================================

class AttendanceRuleTests(TestCase):
    def test_get_active_creates_default_once(self):
        rule = AttendanceRule.get_active()
        self.assertEqual(rule.standard_work_minutes, 450)
        self.assertTrue(rule.clamp_negative_overtime)
        self.assertTrue(rule.exclude_rejected_leave)
        self.assertEqual(AttendanceRule.get_active().pk, rule.pk)

    def test_only_one_active_rule_at_a_time(self):
        """口径必须唯一：启用新规则时旧的自动停用"""
        first = AttendanceRule.get_active()
        second = AttendanceRule.objects.create(name='新规则', is_active=True)
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)


# ============================================================================
# 权限边界
# ============================================================================

class PermissionBoundaryTests(TestCase):
    """无权限用户访问受限页面 → 302 或明确拒绝（计划书 7.2）"""

    def setUp(self):
        self.user = User.objects.create_user('nobody', password='x')

    def test_anonymous_redirected_to_login(self):
        for name in ('attendance:overview', 'attendance:daily_list',
                     'attendance:leave_list', 'attendance:monthly_summary'):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 302, name)
            self.assertIn(reverse('accounts:login'), resp['Location'])

    def test_logged_in_without_permission_is_denied(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse('attendance:overview'))
        self.assertEqual(resp.status_code, 302)

    def test_upload_requires_upload_permission(self):
        self.client.force_login(self.user)
        UserPermission.objects.create(user=self.user, permission_code='attendance.query')
        # 有 query 但没 upload → 上传页仍被拒
        self.assertEqual(self.client.get(reverse('attendance:upload')).status_code, 302)
        # 查页面放行
        self.assertEqual(self.client.get(reverse('attendance:daily_list')).status_code, 200)

    def test_admin_has_all_permissions(self):
        admin = User.objects.create_superuser('boss', password='x')
        self.client.force_login(admin)
        for name in ('attendance:overview', 'attendance:upload', 'attendance:daily_list',
                     'attendance:leave_list', 'attendance:monthly_summary',
                     'attendance:report_generate', 'attendance:rule_config'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_overview_leave_card_links_to_leave_list(self):
        """
        ⚠️ 回归：首页「请假记录」卡片曾经错误地跳到月度汇总页。
           请假记录应指向专门的请假记录页。
        """
        admin = User.objects.create_superuser('boss2', password='x')
        self.client.force_login(admin)
        resp = self.client.get(reverse('attendance:overview'))
        self.assertContains(resp, reverse('attendance:leave_list'))

    def test_leave_list_renders_and_filters(self):
        admin = User.objects.create_superuser('boss3', password='x')
        self.client.force_login(admin)
        up = UploadedFile.objects.create(
            original_filename='l.xlsx', stored_filename='l.xlsx', file_path='/tmp/l.xlsx',
            file_size=1, file_kind='leave', period='2014-08', uploaded_by=admin,
        )
        LeaveRecord.objects.create(
            approval_no='L1', applicant_name='员工02', leave_type='调休',
            start_time=timezone.make_aware(datetime(2014, 8, 6, 8, 0),
                                           timezone.get_current_timezone()),
            duration_hours=Decimal('2'), duration_days=Decimal('0.2667'),
            is_approved=True, period='2014-08', source_file=up, created_by=admin,
        )
        LeaveRecord.objects.create(
            approval_no='L2', applicant_name='员工04', leave_type='年假',
            start_time=timezone.make_aware(datetime(2014, 8, 12, 9, 0),
                                           timezone.get_current_timezone()),
            duration_hours=Decimal('1'), duration_days=Decimal('0.1333'),
            is_approved=False, period='2014-08', source_file=up, created_by=admin,
        )

        resp = self.client.get(reverse('attendance:leave_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '员工02')
        self.assertContains(resp, '员工04')

        # 只看未通过
        resp = self.client.get(reverse('attendance:leave_list'), {'status': 'rejected'})
        self.assertContains(resp, '员工04')
        self.assertNotContains(resp, 'L1')

        # 按类型筛选
        resp = self.client.get(reverse('attendance:leave_list'), {'leave_type': '年假'})
        self.assertContains(resp, '员工04')
        self.assertNotContains(resp, 'L1')

    def test_leave_list_filters_by_approval_no(self):
        """审批编号是钉钉的唯一单据号，要能按它查（支持片段模糊匹配）"""
        admin = User.objects.create_superuser('boss5', password='x')
        self.client.force_login(admin)
        up = UploadedFile.objects.create(
            original_filename='l.xlsx', stored_filename='l.xlsx', file_path='/tmp/l.xlsx',
            file_size=1, file_kind='leave', period='2014-08', uploaded_by=admin,
        )
        aware = timezone.make_aware(datetime(2014, 8, 6, 8, 0),
                                    timezone.get_current_timezone())
        for no, who in (('201408060734000375721', '甲'), ('201408051823000076247', '乙')):
            LeaveRecord.objects.create(
                approval_no=no, applicant_name=who, leave_type='调休',
                start_time=aware, duration_hours=Decimal('2'),
                duration_days=Decimal('0.27'), is_approved=True, period='2014-08',
                source_file=up, created_by=admin,
            )

        # 精确编号
        resp = self.client.get(reverse('attendance:leave_list'),
                               {'approval_no': '201408060734000375721'})
        self.assertContains(resp, '甲')
        self.assertNotContains(resp, '201408051823000076247')

        # 片段匹配（凭记忆里的后缀找单）
        resp = self.client.get(reverse('attendance:leave_list'),
                               {'approval_no': '000076247'})
        self.assertContains(resp, '乙')
        self.assertNotContains(resp, '甲')

        # 查不到时为空表
        resp = self.client.get(reverse('attendance:leave_list'),
                               {'approval_no': '不存在的编号'})
        self.assertContains(resp, '没有符合条件的记录')

        # 与其它条件组合（编号 + 类型）
        resp = self.client.get(reverse('attendance:leave_list'),
                               {'approval_no': '20140806', 'leave_type': '年假'})
        self.assertContains(resp, '没有符合条件的记录')

    def test_leave_list_pagination_keeps_approval_no(self):
        """翻页链接必须带上审批编号条件，否则第二页会丢筛选"""
        admin = User.objects.create_superuser('boss6', password='x')
        self.client.force_login(admin)
        up = UploadedFile.objects.create(
            original_filename='l.xlsx', stored_filename='l.xlsx', file_path='/tmp/l.xlsx',
            file_size=1, file_kind='leave', period='2014-08', uploaded_by=admin,
        )
        aware = timezone.make_aware(datetime(2014, 8, 6, 8, 0),
                                    timezone.get_current_timezone())
        LeaveRecord.objects.bulk_create([
            LeaveRecord(approval_no=f'NO{i:05d}', applicant_name=f'员工{i}',
                        leave_type='调休', start_time=aware,
                        duration_hours=Decimal('1'), duration_days=Decimal('0.1'),
                        is_approved=True, period='2014-08',
                        source_file=up, created_by=admin)
            for i in range(150)
        ])
        resp = self.client.get(reverse('attendance:leave_list'),
                               {'approval_no': 'NO0'})
        # 分页链接里的参数会被 URL 编码（approval_no=NO0 保持不变，但断言避免依赖编码细节）
        self.assertContains(resp, 'approval_no=')
        self.assertContains(resp, 'page=2')

    def test_leave_list_has_query_limit(self):
        admin = User.objects.create_superuser('boss4', password='x')
        self.client.force_login(admin)
        up = UploadedFile.objects.create(
            original_filename='l.xlsx', stored_filename='l.xlsx', file_path='/tmp/l.xlsx',
            file_size=1, file_kind='leave', period='2014-08', uploaded_by=admin,
        )
        aware = timezone.make_aware(datetime(2014, 8, 6, 8, 0),
                                    timezone.get_current_timezone())
        LeaveRecord.objects.bulk_create([
            LeaveRecord(approval_no=f'X{i:05d}', applicant_name=f'员工{i}',
                        leave_type='调休', start_time=aware,
                        duration_hours=Decimal('1'), duration_days=Decimal('0.1'),
                        is_approved=True, period='2014-08',
                        source_file=up, created_by=admin)
            for i in range(2100)
        ])
        resp = self.client.get(reverse('attendance:leave_list'))
        self.assertTrue(resp.context['over_limit'])
        self.assertIsNone(resp.context['page'])
        self.assertNotContains(resp, '员工02099')


class QueryLimitTests(TestCase):
    """⚠️ 查询命中数极多时页面不能崩：必须设数量上限"""

    def setUp(self):
        self.user = User.objects.create_user('hr04', password='x')
        self.client.force_login(self.user)
        UserPermission.objects.create(user=self.user, permission_code='attendance.query')

        up = UploadedFile.objects.create(
            original_filename='d.xlsx', stored_filename='d.xlsx', file_path='/tmp/d.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.user,
        )
        AttendanceDaily.objects.bulk_create([
            AttendanceDaily(work_date=date(2014, 8, (i % 28) + 1), user_id=f'U{i:05d}',
                             name=f'员工{i:05d}', source_file=up, created_by=self.user)
            for i in range(2500)
        ])

    def test_over_limit_shows_notice_instead_of_rendering_all(self):
        from django.conf import settings
        resp = self.client.get(reverse('attendance:daily_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['over_limit'])
        self.assertIsNone(resp.context['page'])
        self.assertGreater(resp.context['total'], settings.QUERY_MAX_RESULTS)
        # 页面里不应出现全部 2500 行
        self.assertNotIn('员工02499', resp.content.decode())

    def test_filter_narrows_below_limit(self):
        resp = self.client.get(reverse('attendance:daily_list'), {'name': '员工00001'})
        self.assertFalse(resp.context['over_limit'])
        self.assertIsNotNone(resp.context['page'])


class XssSafetyTests(TestCase):
    """⚠️ Excel 中带 HTML 的内容在页面渲染时不执行（计划书 7.2）"""

    def setUp(self):
        self.user = User.objects.create_user('hr05', password='x')
        self.client.force_login(self.user)
        UserPermission.objects.create(user=self.user, permission_code='attendance.query')
        up = UploadedFile.objects.create(
            original_filename='<img src=x onerror=alert(1)>.xlsx', stored_filename='d.xlsx',
            file_path='/tmp/d.xlsx', file_size=1, file_kind='daily', period='2014-08',
            uploaded_by=self.user,
        )
        AttendanceDaily.objects.create(
            work_date=date(2014, 8, 1), user_id='U1',
            name='<script>alert(1)</script>', department='<b>研发</b>',
            source_file=up, created_by=self.user,
        )

    def test_daily_list_escapes_excel_content(self):
        resp = self.client.get(reverse('attendance:daily_list'))
        html = resp.content.decode()
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<b>研发</b>', html)

    def test_file_list_escapes_filename(self):
        resp = self.client.get(reverse('attendance:file_list'))
        html = resp.content.decode()
        self.assertNotIn('<img src=x onerror=alert(1)>', html)


# ============================================================================
# 审计覆盖
# ============================================================================

class AuditTrailTests(TestCase):
    """⚠️ 不可回归项：审计日志覆盖上传、删除、导出、规则变更（计划书 7.3）"""

    def setUp(self):
        from accounts.models import AuditLog
        self.AuditLog = AuditLog
        self.user = User.objects.create_user('hr06', password='x')
        self.client.force_login(self.user)
        UserPermission.objects.create(user=self.user, permission_code='attendance.rule_manage')

    def test_rule_change_is_logged_with_diff(self):
        rule = AttendanceRule.get_active()
        resp = self.client.post(reverse('attendance:rule_save'), {
            'standard_work_minutes': '480',
            'monthly_standard_days': '22',
            'excluded_attend_groups': '未加入考勤组',
            'clamp_negative_overtime': 'on',
            'exclude_rejected_leave': 'on',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['success'])

        log = self.AuditLog.objects.filter(action='ATTENDANCE_RULE_UPDATE').first()
        self.assertIsNotNone(log, '规则变更必须写审计日志')
        self.assertEqual(log.module, 'attendance')
        self.assertIn('standard_work_minutes', log.description)

    def test_file_delete_is_logged(self):
        up = UploadedFile.objects.create(
            original_filename='d.xlsx', stored_filename='d.xlsx', file_path='/tmp/d.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.user,
        )
        UserPermission.objects.create(user=self.user, permission_code='attendance.upload')
        self.client.post(reverse('attendance:file_delete', args=[up.pk]))
        self.assertTrue(self.AuditLog.objects.filter(action='ATTENDANCE_FILE_DELETE').exists())
        self.assertFalse(UploadedFile.objects.filter(pk=up.pk).exists())


# ============================================================================
# Phase 2：上传流程（HTTP 端到端）
# ============================================================================

def _xlsx_bytes(builder, rows):
    """把工作簿构建函数跑在内存里，返回 bytes（用于 SimpleUploadedFile）"""
    import io
    buf = io.BytesIO()
    # builder 需要路径，这里先落临时文件再读回来
    import tempfile
    fd, path = tempfile.mkstemp(suffix='.xlsx')
    os.close(fd)
    try:
        builder(path, rows)
        with open(path, 'rb') as fh:
            buf.write(fh.read())
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return buf.getvalue()


class UploadFlowTests(TestCase):
    """端到端走 HTTP 上传：校验、导入、报告、查重、覆盖"""

    def setUp(self):
        self.user = User.objects.create_user('hr07', password='x')
        UserPermission.objects.create(user=self.user, permission_code='attendance.upload')
        UserPermission.objects.create(user=self.user, permission_code='attendance.query')
        self.client.force_login(self.user)
        self.rule = AttendanceRule.get_active()

    def _post_daily(self, rows, period='2014-08', purge=False, filename='daily.xlsx'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        blob = _xlsx_bytes(build_dingtalk_daily_workbook, rows)
        data = {'file_kind': 'daily', 'period': period}
        if purge:
            data['purge_existing'] = 'on'
        return self.client.post(reverse('attendance:upload'), {
            **data, 'file': SimpleUploadedFile(
                filename, blob,
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        })

    def _post_leave(self, rows, period='2014-08', purge=False, filename='leave.xlsx'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        blob = _xlsx_bytes(build_leave_workbook, rows)
        data = {'file_kind': 'leave', 'period': period}
        if purge:
            data['purge_existing'] = 'on'
        return self.client.post(reverse('attendance:upload'), {
            **data, 'file': SimpleUploadedFile(
                filename, blob,
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        })

    # ── 正常路径 ──

    def test_daily_upload_imports_and_redirects_to_report(self):
        resp = self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', '部门': '研发一部', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15', '工作时长': 462},
        ])
        self.assertEqual(resp.status_code, 302)
        record = UploadedFile.objects.get()
        self.assertRedirects(resp, reverse('attendance:import_detail', args=[record.pk]))
        self.assertEqual(record.status, 'success')
        self.assertEqual(record.record_count, 1)
        self.assertEqual(AttendanceDaily.objects.count(), 1)

    def test_leave_upload_imports(self):
        resp = self._post_leave([{
            '审批编号': 'A001', '审批状态': '完成', '审批结果': '同意',
            '发起人姓名': '员工02', '请假类型': '调休',
            '开始时间': '2014-08-06 08:00', '时长': '2小时',
        }])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(LeaveRecord.objects.count(), 1)

    def test_upload_writes_audit_log(self):
        from accounts.models import AuditLog
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'},
        ])
        log = AuditLog.objects.filter(action='UPLOAD').first()
        self.assertIsNotNone(log, '上传必须写审计日志')
        self.assertEqual(log.module, 'attendance')
        self.assertIn('2014-08', log.description)

    def test_import_detail_page_renders(self):
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'},
        ])
        record = UploadedFile.objects.get()
        resp = self.client.get(reverse('attendance:import_detail', args=[record.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, record.original_filename)

    def test_import_preview_does_not_write_to_db(self):
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'},
        ])
        record = UploadedFile.objects.get()
        before = AttendanceDaily.objects.count()
        resp = self.client.get(reverse('attendance:import_preview', args=[record.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '探测到的表头行')
        self.assertContains(resp, '10 个')
        self.assertEqual(AttendanceDaily.objects.count(), before, '预览不得写库')

    # ── 校验路径（计划书 7.2 功能边界）──

    def test_rejects_non_xlsx_with_clear_message(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        resp = self.client.post(reverse('attendance:upload'), {
            'file_kind': 'daily', 'period': '2014-08',
            'file': SimpleUploadedFile('data.csv', b'a,b,c', content_type='text/csv'),
        })
        self.assertEqual(resp.status_code, 200)      # 回到页面，不 500
        self.assertContains(resp, '不支持的文件类型')
        self.assertFalse(UploadedFile.objects.exists())

    def test_rejects_empty_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        resp = self.client.post(reverse('attendance:upload'), {
            'file_kind': 'daily', 'period': '2014-08',
            'file': SimpleUploadedFile('empty.xlsx', b''),
        })
        self.assertContains(resp, '文件为空')

    def test_rejects_bad_period(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        resp = self.client.post(reverse('attendance:upload'), {
            'file_kind': 'daily', 'period': '2014-13',
            'file': SimpleUploadedFile('d.xlsx', b'PK'),
        })
        self.assertContains(resp, '账期格式不正确')

    def test_rejects_missing_file_kind(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        resp = self.client.post(reverse('attendance:upload'), {
            'file_kind': 'nope', 'period': '2014-08',
            'file': SimpleUploadedFile('d.xlsx', b'PK'),
        })
        self.assertContains(resp, '请选择正确的文件类型')

    def test_unparseable_xlsx_is_recorded_as_failed_not_500(self):
        """损坏的 xlsx 应当落到"失败"状态并给出可读原因，而不是抛 500"""
        from django.core.files.uploadedfile import SimpleUploadedFile
        resp = self.client.post(reverse('attendance:upload'), {
            'file_kind': 'daily', 'period': '2014-08',
            'file': SimpleUploadedFile('broken.xlsx', b'PK\x03\x04 not a real xlsx'),
        })
        self.assertEqual(resp.status_code, 302)
        record = UploadedFile.objects.get()
        self.assertEqual(record.status, 'failed')
        self.assertTrue(record.error_message)
        from accounts.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(action='UPLOAD_FAILED').exists())

    def test_missing_required_column_reports_which_one_over_http(self):
        """缺必要列时报告里要指出缺哪一列"""
        from django.core.files.uploadedfile import SimpleUploadedFile
        import io
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(1, 1, '每日统计 x')
        for i in range(1, 40):
            ws.cell(3, i, f'列{i}')
            ws.cell(3, i).alignment = openpyxl.styles.Alignment()
            ws.cell(4, i, f'列{i}')
            ws.merge_cells(start_row=3, start_column=i, end_row=4, end_column=i)
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()

        resp = self.client.post(reverse('attendance:upload'), {
            'file_kind': 'daily', 'period': '2014-08',
            'file': SimpleUploadedFile('nocols.xlsx', buf.getvalue()),
        })
        self.assertEqual(resp.status_code, 302)
        record = UploadedFile.objects.get()
        self.assertEqual(record.status, 'failed')
        self.assertIn('姓名', record.error_message)

    # ── 查重与覆盖 ──

    def test_same_period_without_purge_asks_before_touching_data(self):
        """
        同账期再次上传且未勾选覆盖 → 必须**停下来问**，
        不得擅自改动已有数据（这是"可追溯"的关键）。
        """
        rows = [{'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
                 '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'}]
        self._post_daily(rows)
        first = UploadedFile.objects.get()
        first_id = first.pk
        AttendanceDaily.objects.get()          # 确认有数据

        resp = self._post_daily(rows)          # 未勾选覆盖
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '该账期已有数据')
        self.assertContains(resp, '覆盖')

        # 原有记录未被删除，且新增了 pending 记录
        self.assertTrue(UploadedFile.objects.filter(pk=first_id).exists())
        self.assertEqual(UploadedFile.objects.count(), 2)
        self.assertEqual(AttendanceDaily.objects.count(), 1)

    def test_confirm_reuse_overwrites_period(self):
        """确认覆盖：清空该账期旧数据后重新导入"""
        rows = [{'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
                 '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'}]
        self._post_daily(rows)
        resp = self._post_daily(rows)          # 触发"已有数据"
        self.assertEqual(resp.status_code, 200)
        pending = UploadedFile.objects.order_by('-pk').first()

        resp2 = self.client.post(reverse('attendance:upload'),
                                 {'reuse_id': pending.pk, 'purge_existing': 'on'})
        self.assertEqual(resp2.status_code, 302)
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'success')
        self.assertEqual(pending.record_count, 1)
        self.assertIsNotNone(pending.import_report.get('purged'))
        self.assertEqual(pending.import_report['purged']['records'], 1)
        # 覆盖后账期内仍然只有 1 条
        self.assertEqual(AttendanceDaily.objects.count(), 1)

    def test_purge_removes_people_absent_from_new_export(self):
        """
        ⚠️ 覆盖策略的核心价值：钉钉重导后消失的人不能留下幽灵数据。
        """
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'},
            {'姓名': '将离职', '考勤组': '默认考勤组', 'UserId': 'U002',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:16'},
        ])
        self.assertEqual(AttendanceDaily.objects.count(), 2)

        # 第二份导出里「将离职」已经不在
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'},
        ], purge=True)
        self.assertEqual(
            sorted(AttendanceDaily.objects.values_list('name', flat=True)), ['员工01']
        )

    def test_purge_only_touches_target_period(self):
        """覆盖只能动目标账期，不能误删别的月份"""
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'},
        ], period='2014-08')
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-07-01 星期二', '上班1打卡时间': '09:15'},
        ], period='2014-07', filename='july.xlsx')
        self.assertEqual(AttendanceDaily.objects.count(), 2)

        # 覆盖 8 月
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-02 星期六', '上班1打卡时间': '09:15'},
        ], period='2014-08', purge=True, filename='aug2.xlsx')

        dates = sorted(AttendanceDaily.objects.values_list('work_date', flat=True))
        self.assertEqual([str(d) for d in dates], ['2014-07-01', '2014-08-02'])

    def test_second_upload_same_period_always_asks_first(self):
        """
        ⚠️ 同账期第二次上传**总是**先停下来问，即使本次勾了覆盖。
           这是刻意的：勾选框只表达"我接受覆盖"的意图，
           真正执行前还必须看到"已有 N 次导入"的清单再确认一次，
           否则误传一个错文件就会静默清掉整个月的数据。
        """
        rows = [{'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
                 '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'}]
        self._post_daily(rows)
        resp = self._post_daily(rows, filename='aug2.xlsx')   # 即使 purge=True 也先问
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '该账期已有数据')
        self.assertEqual(AttendanceDaily.objects.count(), 1)
        self.assertEqual(UploadedFile.objects.filter(status='pending').count(), 1)

    def test_confirm_without_purge_merges_within_period(self):
        """
        确认时不给 purge → 按「日期 + UserId」逐行合并，别的日期保留。
        （对应提示页上的"合并/仅新增"路径）
        """
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15', '工作时长': 462},
        ])
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-02 星期六', '上班1打卡时间': '09:20', '工作时长': 500},
        ], filename='aug2.xlsx')
        pending = UploadedFile.objects.filter(status='pending').get()

        # 不发 purge_existing → 合并
        resp = self.client.post(reverse('attendance:upload'), {'reuse_id': pending.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AttendanceDaily.objects.count(), 2)
        self.assertIsNone((pending.import_report or {}).get('purged'))
        self.assertEqual(
            AttendanceDaily.objects.get(work_date=date(2014, 8, 1)).in1_time, '09:15')

    def test_confirm_with_purge_replaces_period(self):
        """确认时给 purge → 整账期替换"""
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15', '工作时长': 462},
        ])
        self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-02 星期六', '上班1打卡时间': '09:20', '工作时长': 500},
        ], filename='aug2.xlsx')
        pending = UploadedFile.objects.filter(status='pending').get()

        resp = self.client.post(reverse('attendance:upload'),
                                {'reuse_id': pending.pk, 'purge_existing': 'on'})
        self.assertEqual(resp.status_code, 302)
        pending.refresh_from_db()
        self.assertEqual(pending.import_report['purged']['records'], 1)
        self.assertEqual(
            [str(d) for d in AttendanceDaily.objects.values_list('work_date', flat=True)],
            ['2014-08-02'],
        )

    def test_purge_existing_true_on_first_upload_is_noop(self):
        """首次上传时就勾了覆盖 → 没有旧数据可清，不应报错"""
        resp = self._post_daily([
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'},
        ], purge=True)
        self.assertEqual(resp.status_code, 302)
        record = UploadedFile.objects.get()
        self.assertEqual(record.status, 'success')
        self.assertEqual(record.import_report['purged']['records'], 0)

    # ── 权限 ──

    def test_upload_post_requires_permission(self):
        other = User.objects.create_user('viewer', password='x')
        UserPermission.objects.create(user=other, permission_code='attendance.query')
        self.client.force_login(other)

        from django.core.files.uploadedfile import SimpleUploadedFile
        resp = self.client.post(reverse('attendance:upload'), {
            'file_kind': 'daily', 'period': '2014-08',
            'file': SimpleUploadedFile('d.xlsx', b'PK'),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(UploadedFile.objects.exists())

    def test_cannot_reuse_other_users_pending_file(self):
        """不能拿别人的 pending 记录来触发导入"""
        from accounts.models import UserPermission as UP
        owner = User.objects.create_user('owner', password='x')
        UP.objects.create(user=owner, permission_code='attendance.upload')
        record = UploadedFile.objects.create(
            original_filename='x.xlsx', stored_filename='x.xlsx', file_path='/tmp/x.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=owner,
        )
        resp = self.client.post(reverse('attendance:upload'),
                                {'reuse_id': record.pk, 'purge_existing': 'on'})
        self.assertEqual(resp.status_code, 404)

    # ── 文件名 XSS ──

    def test_malicious_filename_is_escaped_in_report(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        blob = _xlsx_bytes(build_dingtalk_daily_workbook, [
            {'姓名': '员工01', '考勤组': '默认考勤组', 'UserId': 'U001',
             '日期': '14-08-01 星期五', '上班1打卡时间': '09:15'},
        ])
        self.client.post(reverse('attendance:upload'), {
            'file_kind': 'daily', 'period': '2014-08',
            'file': SimpleUploadedFile('<img src=x onerror=alert(1)>.xlsx', blob),
        })
        record = UploadedFile.objects.get()
        resp = self.client.get(reverse('attendance:import_detail', args=[record.pk]))
        html = resp.content.decode()
        self.assertNotIn('<img src=x onerror=alert(1)>', html)
        self.assertIn('&lt;img', html)


# ============================================================================
# 权限矩阵（逐角色 × 逐页面）
# ============================================================================

class PermissionMatrixTests(TestCase):
    """
    按角色逐页验证可访问性，防止"页面能开但功能越权"或"权限被误放开"。

    ⚠️ 这里刻意用「角色」而不是「权限码」来写用例：需求方关心的是
       "HR 能做什么、上传操作员能做什么"，而不是某个权限码。
    """

    @classmethod
    def setUpTestData(cls):
        cls.viewer = User.objects.create_user('viewer', password='x')      # 只读
        UserPermission.objects.create(user=cls.viewer, permission_code='attendance.query')

        cls.uploader = User.objects.create_user('uploader', password='x')  # 只上传
        UserPermission.objects.create(user=cls.uploader, permission_code='attendance.upload')

        cls.reporter = User.objects.create_user('reporter', password='x')  # 只出报表
        UserPermission.objects.create(user=cls.reporter, permission_code='attendance.report')

        cls.rule_admin = User.objects.create_user('ruleadmin', password='x')
        UserPermission.objects.create(user=cls.rule_admin,
                                      permission_code='attendance.rule_manage')

        cls.nobody = User.objects.create_user('nobody', password='x')     # 无任何权限
        cls.boss = User.objects.create_superuser('boss', password='x')     # 管理员

    def _get(self, user, url_name, *args):
        self.client.force_login(user)
        return self.client.get(reverse(url_name, args=args) if args else reverse(url_name))

    # ── 只读用户（attendance.query）──

    def test_viewer_can_read_attendance_pages(self):
        for name in ('attendance:overview', 'attendance:daily_list',
                     'attendance:leave_list', 'attendance:monthly_summary'):
            self.assertEqual(self._get(self.viewer, name).status_code, 200, name)

    def test_viewer_can_list_imports(self):
        self.assertEqual(self._get(self.viewer, 'attendance:file_list').status_code, 200)

    def test_viewer_cannot_reach_privileged_pages(self):
        """
        已登录但无权限 → 被拒绝（重定向到首页并提示），不是跳登录页。
        跳登录页只用于未登录的情况。
        """
        for name in ('attendance:upload', 'attendance:report_generate',
                     'attendance:rule_config'):
            resp = self._get(self.viewer, name)
            self.assertEqual(resp.status_code, 302, f'{name} 不该对只读用户开放')
            self.assertEqual(resp['Location'], '/', f'{name} 应被拒回首页')

    def test_viewer_cannot_upload_via_post(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_login(self.viewer)
        resp = self.client.post(reverse('attendance:upload'), {
            'file_kind': 'daily', 'period': '2014-08',
            'file': SimpleUploadedFile('d.xlsx', b'PK'),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(UploadedFile.objects.exists())

    # ── 只上传用户（attendance.upload）──

    def test_uploader_can_upload(self):
        self.assertEqual(self._get(self.uploader, 'attendance:upload').status_code, 200)

    def test_uploader_sees_only_own_imports(self):
        mine = UploadedFile.objects.create(
            original_filename='mine.xlsx', stored_filename='m.xlsx', file_path='/tmp/m.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.uploader)
        other = UploadedFile.objects.create(
            original_filename='other.xlsx', stored_filename='o.xlsx', file_path='/tmp/o.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.viewer)

        resp = self._get(self.uploader, 'attendance:file_list')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['can_see_all'])
        self.assertContains(resp, 'mine.xlsx')
        self.assertNotContains(resp, 'other.xlsx')
        # 页面标题也该从「导入记录」变为「我的上传」
        self.assertContains(resp, '我的上传')

    def test_uploader_can_view_own_import_report_but_not_others(self):
        mine = UploadedFile.objects.create(
            original_filename='mine.xlsx', stored_filename='m.xlsx', file_path='/tmp/m.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.uploader)
        other = UploadedFile.objects.create(
            original_filename='other.xlsx', stored_filename='o.xlsx', file_path='/tmp/o.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.viewer)

        self.assertEqual(
            self._get(self.uploader, 'attendance:import_detail', mine.pk).status_code, 200)
        # 别人的报告 → 被挡回列表页
        resp = self._get(self.uploader, 'attendance:import_detail', other.pk)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('attendance:file_list'), resp['Location'])

    def test_viewer_can_see_all_imports(self):
        UploadedFile.objects.create(
            original_filename='a.xlsx', stored_filename='a.xlsx', file_path='/tmp/a.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.uploader)
        UploadedFile.objects.create(
            original_filename='b.xlsx', stored_filename='b.xlsx', file_path='/tmp/b.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.viewer)
        resp = self._get(self.viewer, 'attendance:file_list')
        self.assertTrue(resp.context['can_see_all'])
        self.assertContains(resp, 'a.xlsx')
        self.assertContains(resp, 'b.xlsx')

    def test_uploader_cannot_reach_query_only_pages(self):
        for name in ('attendance:daily_list', 'attendance:leave_list',
                     'attendance:monthly_summary', 'attendance:overview'):
            self.assertEqual(self._get(self.uploader, name).status_code, 302, name)

    # ── 删除：比查看更严 ──

    def test_viewer_cannot_delete_import(self):
        """只读用户能看全部，但不能删任何人的导入"""
        rec = UploadedFile.objects.create(
            original_filename='a.xlsx', stored_filename='a.xlsx', file_path='/tmp/a.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.uploader)
        self.client.force_login(self.viewer)
        resp = self.client.post(reverse('attendance:file_delete', args=[rec.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(UploadedFile.objects.filter(pk=rec.pk).exists(),
                        '只读用户不该能删除导入')

    def test_uploader_cannot_delete_others_import(self):
        rec = UploadedFile.objects.create(
            original_filename='a.xlsx', stored_filename='a.xlsx', file_path='/tmp/a.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.viewer)
        self.client.force_login(self.uploader)
        self.client.post(reverse('attendance:file_delete', args=[rec.pk]))
        self.assertTrue(UploadedFile.objects.filter(pk=rec.pk).exists())

    def test_uploader_can_delete_own_import(self):
        rec = UploadedFile.objects.create(
            original_filename='a.xlsx', stored_filename='a.xlsx', file_path='/tmp/a.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.uploader)
        self.client.force_login(self.uploader)
        self.client.post(reverse('attendance:file_delete', args=[rec.pk]))
        self.assertFalse(UploadedFile.objects.filter(pk=rec.pk).exists())

    def test_nobody_cannot_delete(self):
        rec = UploadedFile.objects.create(
            original_filename='a.xlsx', stored_filename='a.xlsx', file_path='/tmp/a.xlsx',
            file_size=1, file_kind='daily', period='2014-08', uploaded_by=self.viewer)
        self.client.force_login(self.nobody)
        self.client.post(reverse('attendance:file_delete', args=[rec.pk]))
        self.assertTrue(UploadedFile.objects.filter(pk=rec.pk).exists())

    # ── 报表 / 规则 ──

    def test_reporter_can_open_report_page(self):
        self.assertEqual(
            self._get(self.reporter, 'attendance:report_generate').status_code, 200)

    def test_reporter_cannot_manage_rules_or_upload(self):
        for name in ('attendance:rule_config', 'attendance:upload',
                     'attendance:daily_list'):
            self.assertEqual(self._get(self.reporter, name).status_code, 302, name)

    def test_rule_manager_can_manage_rules_only(self):
        self.assertEqual(
            self._get(self.rule_admin, 'attendance:rule_config').status_code, 200)
        for name in ('attendance:upload', 'attendance:daily_list',
                     'attendance:report_generate'):
            self.assertEqual(self._get(self.rule_admin, name).status_code, 302, name)

    # ── 无权限用户 ──

    def test_nobody_reaches_nothing(self):
        for name in ('attendance:overview', 'attendance:daily_list',
                     'attendance:leave_list', 'attendance:monthly_summary',
                     'attendance:upload', 'attendance:file_list',
                     'attendance:report_generate', 'attendance:rule_config'):
            self.assertEqual(self._get(self.nobody, name).status_code, 302, name)

    # ── 管理员 ──

    def test_admin_reaches_everything(self):
        for name in ('attendance:overview', 'attendance:daily_list',
                     'attendance:leave_list', 'attendance:monthly_summary',
                     'attendance:upload', 'attendance:file_list',
                     'attendance:report_generate', 'attendance:rule_config'):
            self.assertEqual(self._get(self.boss, name).status_code, 200, name)

    # ── 导航与权限一致性 ──

    def test_nav_reflects_permissions(self):
        """导航项必须与后端权限一致，避免"点了被弹回"或"藏着能进"的错位"""
        # 只读用户：不看"数据上传"，但要看"考勤查询"和"导入记录"
        resp = self._get(self.viewer, 'attendance:overview')
        self.assertNotContains(resp, '数据上传')
        self.assertContains(resp, '每日明细')
        self.assertContains(resp, '导入记录')

        # 只上传用户：看"数据上传"与"我的上传"，不看查询入口
        resp = self._get(self.uploader, 'attendance:upload')
        self.assertContains(resp, '数据上传')
        self.assertContains(resp, '我的上传')
        self.assertNotContains(resp, '月度汇总')

        # 规则管理员：能看到"管理功能"里的规则配置
        resp = self._get(self.rule_admin, 'attendance:rule_config')
        self.assertContains(resp, '考勤规则配置')

    def test_page_permission_matrix_is_self_consistent(self):
        """
        permissions.PAGE_PERMISSIONS 是权限的单一事实来源，
        每个条目都必须能在 urls.py 里找到对应路由（防止写了死条目）。
        """
        from django.urls import NoReverseMatch

        from .permissions import PAGE_PERMISSIONS
        for url_name in PAGE_PERMISSIONS:
            try:
                if url_name in ('attendance:file_delete', 'attendance:import_detail',
                                'attendance:import_preview'):
                    # 带参数的页面用占位参数反向解析
                    self.client.force_login(self.boss)
                    self.client.get(reverse(url_name, args=[1]))
                else:
                    reverse(url_name)
            except NoReverseMatch:
                self.fail(f'PAGE_PERMISSIONS 里的 {url_name} 在 urls.py 中不存在')


# ============================================================================
# 审计日志标签完整性（Phase 5）
# ============================================================================

class AuditLabelCoverageTests(TestCase):
    """
    ⚠️ 回归守卫：`ACTION_LABELS` 少登记一个 action 码，日志页就会把英文码
       直接显示给用户（模板取不到标签时回落到原值）。

       实测漏过两个：ATTENDANCE_LEAVE_LIST_VIEW、ATTENDANCE_SUMMARY_COMPARE ——
       都是在页面里加了 log_action 但忘了登记标签，测试也不报错。
       这里改为直接扫源码，把"漏登记"变成会红的测试。
    """

    def _written_action_codes(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        pattern = re.compile(r"log_action\(\s*request\s*,\s*'([A-Z_]+)'")
        codes = set()
        for rel in ('attendance/views.py', 'accounts/views.py',
                    'backups/views.py', 'core/views.py'):
            path = root / rel
            if not path.exists():
                continue
            codes |= set(pattern.findall(path.read_text(encoding='utf-8')))
        return codes

    def test_every_logged_action_has_a_label(self):
        from accounts.views import ACTION_LABELS

        written = self._written_action_codes()
        self.assertTrue(written, '没有扫到任何 log_action 调用，正则可能失效了')
        missing = written - set(ACTION_LABELS)
        self.assertEqual(missing, set(),
                         f'这些 action 码会以英文原文显示在日志页：{sorted(missing)}')

    def test_labels_are_chinese_and_non_empty(self):
        import re

        from accounts.views import ACTION_LABELS

        for code, label in ACTION_LABELS.items():
            self.assertTrue(label.strip(), f'{code} 的标签为空')
            self.assertTrue(re.search(r'[\u4e00-\u9fa5]', label),
                            f'{code} 的标签 {label!r} 里没有中文')

    def test_action_codes_are_upper_snake(self):
        import re

        from accounts.views import ACTION_LABELS

        for code in ACTION_LABELS:
            self.assertRegex(code, r'^[A-Z][A-Z0-9_]*$', f'{code} 命名不规范')

    def test_scan_pattern_still_matches_real_calls(self):
        """
        反向确认上一个测试的正则没有失效 —— 否则它会因为"扫不到东西"而假通过。
        """
        written = self._written_action_codes()
        for expected in ('UPLOAD', 'LOGIN', 'ATTENDANCE_REPORT_GENERATE'):
            self.assertIn(expected, written)


# ============================================================================
# 导入上限（Phase 5：把 settings 里声明过的边界真正接上）
# ============================================================================

class ImportLimitTests(TestCase):
    """
    计划书 390 行要求上传校验「扩展名 / 大小 / 单次数量」。
    `IMPORT_MAX_ROWS` 曾长期只是 settings 里的一个声明、没有任何代码引用，
    即"文档写了上限但实际无上限"。这里守住它真的生效。
    """

    def setUp(self):
        self.user = User.objects.create_user('hr09', password='x')
        self.rule = AttendanceRule.get_active()
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_upload(self, rows, name='daily.xlsx'):
        path = os.path.join(self.tmpdir, name)
        build_dingtalk_daily_workbook(path, rows)
        return UploadedFile.objects.create(
            original_filename=name, stored_filename=name,
            file_path=path, file_size=os.path.getsize(path), file_kind='daily',
            period='2014-08', uploaded_by=self.user,
        )

    def _rows(self, count, start_day=1):
        return [
            {'姓名': f'员工{i:03d}', '考勤组': '默认考勤组', '部门': '研发一部',
             'UserId': f'U{i:04d}', '日期': f'25-08-{(start_day + i % 28):02d} 星期五',
             '工作时长': 462, '出勤天数': 1}
            for i in range(count)
        ]

    def test_oversized_file_is_rejected(self):
        from django.test import override_settings

        from .services import ExcelImporter, ImportError_

        up = self._make_upload(self._rows(20))
        with override_settings(IMPORT_MAX_ROWS=5):
            with self.assertRaises(ImportError_) as ctx:
                ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertIn('行数过多', str(ctx.exception))
        self.assertEqual(AttendanceDaily.objects.count(), 0)

    def test_row_cap_is_checked_before_purge(self):
        """
        ⚠️ 关键：超限文件绝不能先把账期清空再失败 —— 那等于用户传错一个文件
           就丢了当月数据。上限判定必须先于 purge。
        """
        from django.test import override_settings

        from .services import ExcelImporter, ImportError_

        # 先正常导入一批当月数据
        good = self._make_upload(self._rows(6), name='good.xlsx')
        ExcelImporter.import_daily(good, self.user, self.rule)
        before = AttendanceDaily.objects.count()
        self.assertGreater(before, 0)

        # 再传一个超限文件，且带 purge_existing=True
        big = self._make_upload(self._rows(30), name='big.xlsx')
        with override_settings(IMPORT_MAX_ROWS=5):
            with self.assertRaises(ImportError_):
                ExcelImporter.import_daily(big, self.user, self.rule, purge_existing=True)

        self.assertEqual(AttendanceDaily.objects.count(), before,
                         '超限文件必须先被拒绝，不能清空已有数据')

    def test_file_within_cap_imports_normally(self):
        from django.test import override_settings

        from .services import ExcelImporter

        up = self._make_upload(self._rows(4))
        with override_settings(IMPORT_MAX_ROWS=1000):
            ok, _skipped, _report = ExcelImporter.import_daily(up, self.user, self.rule)
        self.assertEqual(ok, 4)


class UploadSizeLimitTests(TestCase):
    """
    ⚠️ 回归守卫：`MAX_FILE_SIZE` 环境变量曾喂给一个没人引用的常量
       (`MAX_UPLOAD_SIZE`)，而真正校验用的 `IMPORT_MAX_FILE_SIZE` 是写死的
       50MB —— 运维把环境变量调大后上限纹丝不动，且没有任何提示。
    """

    def test_size_limit_comes_from_settings(self):
        from django.test import override_settings

        from .services import FileStorage, ImportError_

        def fake_upload(size):
            class _F:
                name = 'x.xlsx'
            _F.size = size
            return _F()

        with override_settings(IMPORT_MAX_FILE_SIZE=1024):
            self.assertRaises(ImportError_, FileStorage.validate_upload, fake_upload(2048))
            # 上限之内必须放行
            self.assertEqual(FileStorage.validate_upload(fake_upload(512)), 'xlsx')

    def test_size_limit_reads_env_variable(self):
        """settings 里的上限必须真的取自 MAX_FILE_SIZE，而不是写死。"""
        import pathlib

        settings_src = (pathlib.Path(__file__).resolve().parent.parent
                        / 'config' / 'settings.py').read_text(encoding='utf-8')
        self.assertIn("os.getenv('MAX_FILE_SIZE'", settings_src,
                      'IMPORT_MAX_FILE_SIZE 必须读环境变量')

    def test_dead_size_constant_is_gone(self):
        """
        `MAX_UPLOAD_SIZE` 与真实校验用的常量重复且无人引用，是"改了不生效"的陷阱，
        已删除。防止有人再加回来。
        """
        from django.conf import settings as dj_settings

        self.assertFalse(hasattr(dj_settings, 'MAX_UPLOAD_SIZE'),
                         'MAX_UPLOAD_SIZE 是无人引用的死配置，不要加回来')


# ============================================================================
# 导入记录清理命令（Phase 5 收尾）
# ============================================================================

class PruneImportsCommandTests(TestCase):
    """
    反复重导同一账期会留下许多"名下已无数据行"的历史记录，
    让「导入记录」页面看不出哪条才是当前生效的。

    `prune_imports` 负责清理，但它的判定必须够保守 —— 删错就等于
    丢掉排错依据。这里逐条守住每个"不该删"的条件。
    """

    def setUp(self):
        import io
        import shutil

        self.io = io
        self.shutil = shutil
        self.user = User.objects.create_user('hr10', password='x')
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        self.shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _upload(self, period='2014-08', kind='daily', status='partial',
                rows=0, name=None):
        """建一条导入记录；rows>0 时顺带造出归属它的数据行。"""
        name = name or f'{kind}_{period}_{status}_{rows}.xlsx'
        path = os.path.join(self.tmpdir, name)
        with open(path, 'wb') as fh:
            fh.write(b'x')          # 占位文件，供孤儿文件清理断言使用
        rec = UploadedFile.objects.create(
            original_filename=name, stored_filename=name, file_path=path,
            file_size=1, file_kind=kind, period=period, status=status,
            uploaded_by=self.user,
        )
        for i in range(rows):
            AttendanceDaily.objects.create(
                work_date=date(2014, 8, 1 + i % 28), user_id=f'U{i}', name=f'员工{i}',
                source_file=rec, created_by=self.user,
            )
        return rec

    def _run(self, *args):
        from django.core.management import call_command
        out = self.io.StringIO()
        call_command('prune_imports', *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_preview_deletes_nothing(self):
        old = self._upload(rows=0)
        self._upload(rows=3, name='newer.xlsx')

        out = self._run()
        self.assertIn('冗余可清理 1 条', out)
        self.assertTrue(UploadedFile.objects.filter(pk=old.pk).exists(),
                        '预览模式不允许删除')

    def test_prunes_superseded_empty_import(self):
        old = self._upload(rows=0)
        self._upload(rows=3, name='newer.xlsx')

        out = self._run('--apply')
        self.assertIn('已删除 1 条', out)
        self.assertFalse(UploadedFile.objects.filter(pk=old.pk).exists())
        self.assertEqual(UploadedFile.objects.count(), 1)

    def test_keeps_failed_import_even_when_empty(self):
        """
        ⚠️ 失败记录带有 error_message，是排错依据。即使名下没有数据
           也必须保留 —— 否则用户排错时看不到失败原因。
        """
        failed = self._upload(status='failed', rows=0)
        self._upload(rows=3, name='newer.xlsx')

        self._run('--apply')
        self.assertTrue(UploadedFile.objects.filter(pk=failed.pk).exists(),
                        '失败记录必须保留')

    def test_keeps_latest_import_even_when_empty(self):
        """
        同账期最后一条导入是"当前生效"的那条。即使它一行数据都没有
        （例如整月的人都被规则排除），也不能删 —— 那是有效结果。
        """
        self._upload(rows=2, name='older.xlsx')
        newest = self._upload(rows=0, name='newest.xlsx')

        self._run('--apply')
        self.assertTrue(UploadedFile.objects.filter(pk=newest.pk).exists(),
                        '当前生效的导入记录不能删')

    def test_keeps_import_that_still_owns_rows(self):
        """名下有数据行的记录一律不动，哪怕它不是最新的。"""
        older_with_rows = self._upload(rows=2, name='older_rows.xlsx')
        self._upload(rows=0, name='newer.xlsx')

        self._run('--apply')
        self.assertTrue(UploadedFile.objects.filter(pk=older_with_rows.pk).exists())
        self.assertEqual(AttendanceDaily.objects.count(), 2)

    def test_apply_removes_orphan_upload_file(self):
        old = self._upload(rows=0)
        path = old.file_path
        self._upload(rows=1, name='newer.xlsx')

        self._run('--apply')
        self.assertFalse(os.path.exists(path), '记录删了，孤儿上传文件也应清掉')

    def test_leave_and_daily_are_grouped_separately(self):
        """
        ⚠️ 分组键必须是 (file_kind, period)：日考勤与请假是两个独立序列，
           不能因为"请假记录更晚"就把当日考勤的空记录清掉。
        """
        daily = self._upload(kind='daily', rows=0)
        self._upload(kind='leave', rows=2, name='leave_newer.xlsx')

        self._run('--apply')
        self.assertTrue(UploadedFile.objects.filter(pk=daily.pk).exists(),
                        '不同 file_kind 之间不应互相取代')

    def test_different_periods_do_not_supersede_each_other(self):
        june = self._upload(period='2014-06', rows=0)
        self._upload(period='2014-08', rows=2, name='aug.xlsx')

        self._run('--apply')
        self.assertTrue(UploadedFile.objects.filter(pk=june.pk).exists(),
                        '不同账期之间不应互相取代')


# ============================================================================
# 镜像排除规则覆盖（Phase 5）
# ============================================================================

class DockerignoreCoverageTests(TestCase):
    """
    ⚠️ 回归守卫：`.dockerignore` 用的是 Go `filepath.Match` 语义的模式，
       写窄了不会报错，只会**静默把不该进镜像的文件打进去**。

       实测踩到：模式写的是 `**/tests.py` + `**/test_*.py`，而
       `attendance/tests_report.py` 两个都不匹配（`test_*` 要求前缀是 `test_`，
       而它是 `tests_`），于是这个测试文件一直被静默打进镜像。

       这里改为：扫出仓库里所有"测试类文件"，断言每一个都能被 .dockerignore
       的某条排除规则匹配到 —— 再出现同类疏漏会直接变红。
    """

    def _dockerignore_patterns(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        patterns = []
        for line in (root / '.dockerignore').read_text(encoding='utf-8').splitlines():
            raw = line.strip()
            if not raw or raw.startswith('#'):
                continue
            if raw.startswith('!'):
                # 否定规则单独留着，判定时先按排除规则匹配再被否定推翻
                patterns.append(('negate', raw[1:]))
            else:
                patterns.append(('ignore', raw))
        return patterns

    @staticmethod
    def _segment_match(name, pattern):
        """
        模拟 Go `filepath.Match` 对**单个路径段**的匹配。

        ⚠️ 不能用 Python 的 `fnmatch`：它的 `*` 会跨 `/`，而 Go 的不会。
           这个差异会直接掩盖真实缺陷 —— 例如用 fnmatch 时
           `*.py[cod]` 居然能匹配 `attendance/__pycache__/x.pyc`，
           于是"只排除顶层 .pyc"的错误写法看起来是有效的。
        """
        import re

        out = []
        i = 0
        while i < len(pattern):
            ch = pattern[i]
            if ch == '*':
                out.append('[^/]*')
            elif ch == '?':
                out.append('[^/]')
            elif ch == '[':
                j = i + 1
                if j < len(pattern) and pattern[j] in '!^':
                    j += 1
                if j < len(pattern) and pattern[j] == ']':
                    j += 1
                while j < len(pattern) and pattern[j] != ']':
                    j += 1
                if j >= len(pattern):
                    out.append(re.escape('['))
                else:
                    body = pattern[i + 1:j].replace('/', '')
                    if body.startswith('!'):
                        body = '^' + body[1:]
                    out.append('[' + body + ']')
                    i = j
            else:
                out.append(re.escape(ch))
            i += 1
        return re.fullmatch(''.join(out), name) is not None

    def _segments_match(self, path_parts, pat_parts):
        if not pat_parts:
            return not path_parts
        head = pat_parts[0]
        if head == '**':
            # `**` 匹配任意数量（含零）的路径段
            for i in range(len(path_parts) + 1):
                if self._segments_match(path_parts[i:], pat_parts[1:]):
                    return True
            return False
        if not path_parts:
            return False
        if not self._segment_match(path_parts[0], head):
            return False
        return self._segments_match(path_parts[1:], pat_parts[1:])

    def _matches(self, rel_posix, pattern):
        """
        按 Docker 的语义判断单条模式是否命中路径。

        Docker 用的是 `MatchesOrParentMatches`：先拿完整路径比，再逐级拿**父路径**比
        （所以 `tools/` 能排除 `tools/x.py`），模式按 `/` 切段，
        每段用 Go `filepath.Match`，其中 `**` 可跨任意层。
        """
        path_parts = [p for p in rel_posix.split('/') if p]
        pat_parts = [p for p in pattern.rstrip('/').split('/') if p]
        if not pat_parts:
            return False
        for end in range(len(path_parts), 0, -1):
            if self._segments_match(path_parts[:end], pat_parts):
                return True
        return False

    def _is_excluded(self, rel_posix):
        ignored = False
        for kind, pattern in self._dockerignore_patterns():
            if self._matches(rel_posix, pattern):
                ignored = (kind == 'ignore')
        return ignored

    def _candidate_test_files(self):
        """仓库里所有"属于测试/开发脚本"的 .py 文件（相对路径，posix 风格）。"""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        skip_dirs = {'venv', '__pycache__', 'media', 'staticfiles', 'logs',
                     'backup_files', 'node_modules', '.git'}
        found = []
        for path in root.rglob('*.py'):
            parts = set(path.parts)
            if parts & skip_dirs:
                continue
            name = path.name
            # ⚠️ 覆盖四种命名风格，任何一种漏掉都会重演"静默进镜像"：
            #    test*.py         tests.py / tests_report.py / test_upload.py
            #    *_test.py        settings_test.py
            #    *_tests.py       自定义后缀风格
            #    conftest.py      pytest 夹具
            is_test = (name.startswith('test') or name.endswith('_test.py')
                       or name.endswith('_tests.py') or name == 'conftest.py')
            is_tool = 'tools' in path.relative_to(root).parts
            if is_test or is_tool:
                found.append(path.relative_to(root).as_posix())
        return sorted(found)

    def test_every_test_or_tool_file_is_excluded_from_image(self):
        leaked = [p for p in self._candidate_test_files() if not self._is_excluded(p)]
        self.assertEqual(
            leaked, [],
            '这些测试/工具文件会被打进镜像（含真实姓名夹具）：\n  '
            + '\n  '.join(leaked))

    def test_scan_actually_finds_the_known_test_files(self):
        """反向确认扫描没失效 —— 否则上一个用例会因为"扫不到东西"而假通过。"""
        found = self._candidate_test_files()
        for expected in ('attendance/tests.py', 'attendance/tests_report.py',
                         'backups/tests.py', 'core/tests.py',
                         'config/settings_test.py',
                         'tools/e2e_smoke.py'):
            self.assertIn(expected, found)

    def test_report_template_exception_still_works(self):
        """
        排除 `**/*.xlsx` 会连带排掉报表模板，必须由否定规则捞回来。
        这条一旦失效，容器里找不到模板、报表生成直接失败。
        """
        self.assertFalse(
            self._is_excluded('attendance/templates_xlsx/考勤表模板.xlsx'),
            '报表模板必须被 !attendance/templates_xlsx/*.xlsx 捞回镜像')

    def test_nested_pycache_and_bytecode_are_excluded(self):
        """
        ⚠️ 这是一条**实际发生过的隐私泄露**，务必保留：

           `.dockerignore` 原来写的是 `__pycache__/`（没有 `**/` 前缀），
           在 Docker 的匹配语义下**只排除顶层目录**，于是
           `attendance/__pycache__/tests.cpython-312.pyc` 被静默打进镜像。

           `.pyc` 里保留着字符串常量 —— 也就是测试夹具里的**真实员工姓名**。
           "排除测试文件"的规则管不到它：源文件确实没进去，进去的是字节码。
           （312 = 宿主机的 Python 版本，容器里是 3.10，从版本号就能看出不对。）

        所以这里同时守住"嵌套 __pycache__"与"任意层级的 .pyc/.pyo/.pyd"。
        """
        must_be_excluded = [
            '__pycache__/manage.cpython-310.pyc',
            'attendance/__pycache__/tests.cpython-312.pyc',
            'attendance/__pycache__/tests_report.cpython-312.pyc',
            'accounts/__pycache__/models.cpython-310.pyc',
            'config/__pycache__/settings.cpython-310.pyc',
            'attendance/spam.pyc',
            'manage.pyc',
            'backups/models.pyo',
            'core/views.pyd',
        ]
        leaked = [p for p in must_be_excluded if not self._is_excluded(p)]
        self.assertEqual(leaked, [], f'这些字节码文件会被打进镜像：{leaked}')

    def test_source_and_media_are_excluded_at_any_depth(self):
        """原始考勤数据与实现文件同理：任意层级的 .xlsx / .sql 都不能进镜像。"""
        must_be_excluded = [
            'attendance/uploads/2014-08/daily.xlsx',
            'backups/db_20140901.sql',
            'backup_files/old.zip',
            'media/uploads/daily/2014-08/x.xlsx',
        ]
        leaked = [p for p in must_be_excluded if not self._is_excluded(p)]
        self.assertEqual(leaked, [], f'这些敏感文件会被打进镜像：{leaked}')


# ============================================================================
# 离线前端资源（内网部署）
# ============================================================================

class OfflineAssetsTests(TestCase):
    """
    ⚠️ 本系统要能部署在**无外网的内网环境**，所以页面不能引用任何公网 CDN。

    三条容易各自失效的链路，分别守住：
      1. 模板里又出现外链（改样式时顺手贴回 CDN 链接）；
      2. 资源文件没随仓库分发（漏提交，或下载失败留下 0 字节文件）；
      3. 只下了图标 CSS、**忘了下字体文件** —— 页面不报错，
         图标静默变成空白方块，最难发现。
    """

    ASSETS = [
        'vendor/bootstrap/bootstrap.min.css',
        'vendor/bootstrap/bootstrap.bundle.min.js',
        'vendor/bootstrap-icons/bootstrap-icons.css',
        'vendor/bootstrap-icons/fonts/bootstrap-icons.woff2',
        'vendor/bootstrap-icons/fonts/bootstrap-icons.woff',
    ]

    def _templates(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / 'templates'
        return sorted(root.rglob('*.html'))

    def test_no_external_urls_in_templates(self):
        """模板里不得出现 http(s) 外链 —— 内网访问不到，会直接掉样式与图标。"""
        import re

        url_re = re.compile(r'https?://[^\s"\'<>]+')
        offenders = []
        for path in self._templates():
            text = path.read_text(encoding='utf-8')
            for m in url_re.finditer(text):
                offenders.append(f'{path.name}: {m.group(0)}')
        self.assertEqual(
            offenders, [],
            '模板里出现了外部链接，内网部署会加载失败：\n  ' + '\n  '.join(offenders))

    def test_vendored_assets_are_shipped(self):
        """资源文件必须在仓库里，且不能是空的/半截的。"""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / 'static'
        missing, too_small = [], []
        for rel in self.ASSETS:
            path = root / rel
            if not path.is_file():
                missing.append(rel)
            elif path.stat().st_size < 1024:
                too_small.append(f'{rel} ({path.stat().st_size} 字节)')
        self.assertEqual(missing, [], f'缺少前端资源文件：{missing}')
        self.assertEqual(too_small, [], f'资源文件过小，疑似下载失败：{too_small}')

    def test_icon_css_referenced_fonts_exist(self):
        """
        ⚠️ bootstrap-icons.css 用相对路径 `./fonts/xxx.woff2` 引字体。
           只拷贝 CSS 而不带 fonts/ 目录时，页面**不报错**，
           所有图标静默变成空白方块。这里按 CSS 里的实际引用路径逐个核对。
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / 'static'
        css = root / 'vendor' / 'bootstrap-icons' / 'bootstrap-icons.css'
        self.assertTrue(css.is_file(), '缺少 bootstrap-icons.css')

        refs = re.findall(r'url\(["\']?(\./fonts/[^"\')?]+)', css.read_text(encoding='utf-8'))
        self.assertTrue(refs, 'CSS 里没有解析到字体引用，正则可能失效了')

        missing = [r for r in sorted(set(refs)) if not (css.parent / r).is_file()]
        self.assertEqual(missing, [], f'CSS 引用了但文件不存在：{missing}')

    def test_templates_use_static_tag_for_assets(self):
        """两个入口模板都必须通过 {% static %} 引用本地资源。"""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / 'templates'
        for rel, needles in (
            ('base.html', ['vendor/bootstrap/bootstrap.min.css',
                           'vendor/bootstrap-icons/bootstrap-icons.css',
                           'vendor/bootstrap/bootstrap.bundle.min.js']),
            ('accounts/login.html', ['vendor/bootstrap/bootstrap.min.css',
                                     'vendor/bootstrap-icons/bootstrap-icons.css',
                                     'vendor/bootstrap/bootstrap.bundle.min.js']),
        ):
            text = (root / rel).read_text(encoding='utf-8')
            self.assertIn('{% load static %}', text, f'{rel} 缺少 {{% load static %}}')
            for needle in needles:
                self.assertIn("{% static '" + needle + "' %}", text,
                              f'{rel} 未通过 static 标签引用 {needle}')

    def test_staticfiles_dirs_includes_static(self):
        """STATICFILES_DIRS 必须包含项目 static/ 目录，否则 collectstatic 收不到。"""
        import pathlib

        from django.conf import settings as dj_settings

        static_dir = pathlib.Path(dj_settings.BASE_DIR) / 'static'
        configured = [pathlib.Path(p) for p in dj_settings.STATICFILES_DIRS]
        self.assertIn(static_dir, configured,
                      'STATICFILES_DIRS 未包含项目 static/ 目录')
        self.assertTrue(static_dir.is_dir(), 'static/ 目录不存在')

    def test_vendored_assets_have_no_dangling_sourcemap(self):
        """
        ⚠️ 这条守的是"**只有生产模式才会炸**"的一个部署阻断：

           Bootstrap 的 .min 文件末尾带
               /*# sourceMappingURL=bootstrap.min.css.map */
           而 .map 是开发期产物、没随仓库分发。

           生产用的是 CompressedManifestStaticFilesStorage，它会在 collectstatic
           时解析 CSS 里的引用，找不到文件就抛 MissingFileError，
           **整个 collectstatic 失败**（实测报错原文：
             Post-processing 'vendor/bootstrap/bootstrap.min.css' failed!
             MissingFileError: 'vendor/bootstrap/bootstrap.min.css.map' could not be found）

           而开发模式（DEBUG=True、普通存储、不做后处理）完全看不出来 ——
           典型的"上线才发现"。文件里的 sourceMappingURL 注释已被去掉，
           这个用例防止有人重新下载原版文件把它带回来。
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / 'static'
        offenders = []
        for rel in ('vendor/bootstrap/bootstrap.min.css',
                    'vendor/bootstrap/bootstrap.bundle.min.js',
                    'vendor/bootstrap-icons/bootstrap-icons.css'):
            path = root / rel
            if not path.is_file():
                continue
            text = path.read_text(encoding='utf-8', errors='ignore')
            for m in re.finditer(r'sourceMappingURL=(\S+?)(?:\s*\*/|\s*$)', text, re.M):
                target = m.group(1).strip()
                if not (path.parent / target).is_file():
                    offenders.append(f'{rel} -> {target}')
        self.assertEqual(
            offenders, [],
            '这些 sourceMappingURL 指向未分发的文件，会导致生产 collectstatic 失败：'
            f'{offenders}\n  修复：跑 tools/_strip_sourcemap.py，或补上对应的 .map 文件')



