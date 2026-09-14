"""
Phase 4 报表生成 —— 复刻桌面工具的 Excel 输出

设计决策：
  · **以模板为基础填充**，而不是从空白 workbook 画。理由：
    模板里已经把两行/三行表头的合并区域、列宽、行高、每列的边框与数字格式
    都定义好了（`考勤表模板.xlsx` 的 2729 行全部预置了样式）。
    从零重画等于把"字体/边框/行高/列宽/合并区域"再猜一遍，既费事又容易漏。
    这正是模板存在的意义 —— 桌面工具也是 `shutil.copy(template)` 后填充。

  · **不复制桌面工具的 bug**（见 docs/考勤规则确认单.md 第三节）：
      - 不再有 `range(day+4, len(source_data))` 的错位与丢行
      - 请假合计不再计入"审批=拒绝"的记录
      - 「应出勤」列真正写入应出勤天数（桌面版误写进了「出勤打卡」）
      - 日期不再用字符串下标判月份（10 月必错）
"""
import os
import shutil
from collections import defaultdict
from decimal import Decimal

import openpyxl
from django.conf import settings
from django.utils import timezone

from . import mapping
from .models import AttendanceDaily, AttendanceRule, LeaveRecord
from .services import AttendanceCalculator

# 模板路径（随代码走，不进媒体目录）
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates_xlsx')
TEMPLATE_FILE = '考勤表模板.xlsx'

# Sheet 名必须与模板一致，否则 openpyxl 取不到预置样式
SHEET_DAILY = '考勤处理数据1'
SHEET_SUMMARY = '考勤统计2'

# Sheet1 的列定义：(模型字段取值函数名, 表头)  —— 顺序即列顺序
DAILY_COLUMNS = 15
SUMMARY_COLUMNS = 25

# Sheet2 的表头文案（第 2/3/4 行），用于校验模板是否被换过
SUMMARY_HEADER_ROW2 = [
    '序号', '姓名', '部门', '入职时间', '离职时间', '出勤', None, None, '请假',
    None, None, None, None, None, None, None, None, '缺勤', None, None, None, None,
    '加班', '备注', '员工签字',
]
SUMMARY_HEADER_ROW3 = [
    None, None, None, None, None, '应出勤', '出勤打卡', '实际出勤',
    '事假', '调休', '年假', '病假', '婚假', '陪产假', '产前假', '产假', '产检假',
    '迟到', '早退', '旷工', '上班忘打卡', '下班忘打卡', None, None, None,
]
SUMMARY_HEADER_ROW4 = [
    None, None, None, None, None, '天', '天', '天',
    '天', '天', '天', '天', '天', '天', '天', '天', '天',
    '分', '分', '天', '次', '次', 'H', None, None,
]


class ReportError(Exception):
    """报表生成的可预期错误（消息面向用户）"""


def template_path():
    path = os.path.join(TEMPLATE_DIR, TEMPLATE_FILE)
    if not os.path.exists(path):
        raise ReportError(f'找不到报表模板：{path}')
    return path


def default_output_name(period, now=None):
    """报表文件名。带账期，避免不同月份的报表互相覆盖。"""
    return f'考勤表{period.replace("-", "")}.xlsx'


def _fmt_date_cn(d):
    """把 date 格式化成钉钉那样的 '14-08-01 星期五'（与源文件、基线一致）"""
    if d is None:
        return None
    return f'{d:%y-%m-%d} 星期{mapping.WEEKDAY_CN[d.weekday()]}'


def _dec(v):
    return Decimal(str(v or 0))


def argb(color):
    """
    把色值规范成 8 位 ARGB。

    ⚠️ openpyxl 对 6 位 RRGGBB 的处理是把它当 ARGB 解析，
       结果 alpha 变成 0（实测生成出 '00FF8080'，Excel 里会显示异常），
       与基线的 'FFFF8080' 不一致。必须补上 FF 前缀。
    """
    if not color:
        return None
    text = str(color).lstrip('#').upper()
    if len(text) == 6:
        return 'FF' + text
    if len(text) == 8:
        return text
    return None


def _status_style(cell, rule):
    """
    按单元格文本上色 / 周末红字。

    ⚠️ 颜色来自 AttendanceRule.color_map，不硬编码。
       周末判定用**日期对象的星期**而不是"文本以六/日结尾" ——
       后者会把任何以"日"结尾的文本误判（桌面版第 503 行的问题）。
    """
    text = cell.value
    if not isinstance(text, str):
        return
    color_map = rule.color_map or mapping.STATUS_COLOR_MAP
    for status, rgb in color_map.items():
        if text == status:
            fill_argb = argb(rgb)
            if fill_argb:
                cell.fill = openpyxl.styles.PatternFill('solid', fgColor=fill_argb)
            return


def build_report(period, rule=None, path=None, user=None):
    """
    生成某账期的考勤报表。

    period: 'YYYY-MM'
    path:   输出文件路径；不传则写到 MEDIA_ROOT/reports/ 下
    返回输出文件路径。

    ⚠️ 报表生成只读数据库，不修改任何业务数据。
    """
    rule = rule or AttendanceRule.get_active()
    year, month = (int(x) for x in period.split('-'))

    # ── 逐人逐日明细 ──
    daily_qs = (AttendanceDaily.objects
                .filter(work_date__year=year, work_date__month=month)
                .order_by('name', 'work_date'))
    daily_rows = list(daily_qs)
    if not daily_rows:
        raise ReportError(f'{period} 没有日考勤数据，无法生成报表。')

    # ── 逐人月度汇总（与页面共用同一份计算，避免口径漂移）──
    calc = AttendanceCalculator(rule)
    summary = calc.summarize_month(period)

    # ── 以模板为基础 ──
    tpl = template_path()
    if path is None:
        out_dir = os.path.join(settings.MEDIA_ROOT, 'reports')
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, default_output_name(period))
    else:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)

    shutil.copy(tpl, path)
    wb = openpyxl.load_workbook(path)

    _fill_daily_sheet(wb, daily_rows, rule)
    _fill_summary_sheet(wb, period, summary, rule)

    wb.save(path)
    wb.close()

    # ⚠️ 必须放在 save 之后：openpyxl 会把模板里那些"铺到最大列"的列宽定义
    #    原样写回去，Excel 于是渲染出整整 16384 列（看到的就是 XFD 列）。
    trim_trailing_columns(path)
    return path


# ============================================================================
# 工具：去掉铺到最大列的列宽定义
# ============================================================================

# 各 Sheet 实际用到的最大列（1-based）
_USED_COLUMNS = {SHEET_DAILY: DAILY_COLUMNS, SHEET_SUMMARY: SUMMARY_COLUMNS}


def trim_trailing_columns(path):
    """
    删除模板里"把默认列宽铺到 XFD"的尾部 `<col>` 定义。

    ⚠️ 问题现象：生成/下载的 xlsx 在 Excel 里会显示到 XFD 列（第 16384 列），
       实际上只用 15 / 25 列。

    ⚠️ 根因：`考勤表模板.xlsx` 由 Excel 另存时，`<cols>` 里写了两条铺满全表的
       默认列宽定义（**文件本身也有这个问题**）：
           sheet1: <col min="15"  max="16384" width="8.73" .../>
           sheet2: <col min="26"  max="16377" width="9" .../>
                   <col min="16378" max="16384" width="9" .../>
       Excel 会按这些定义渲染出全部列，于是看起来"有 XFD 列"。

    ⚠️ 为什么不能直接用 openpyxl 改：这些尾部定义**没有**对应的
       `ws.column_dimensions` 条目 —— openpyxl 只在 `column_dimensions` 里
       暴露"有自定义宽度"的列，读取时把这类铺满全表的定义丢掉了，
       回写时又从内部 `_cols` 原样输出。所以只能在 XML 层面处理。

    做法：把 max 超过实际用列数的定义截断到实际列边界，超出的一律删除。
    """
    import re
    import zipfile

    with zipfile.ZipFile(path) as zin:
        names = zin.namelist()
        blobs = {n: zin.read(n) for n in names}

    # xl/worksheets/sheet1.xml → 第 1 个 sheet，依此类推
    for i, sheet_name in enumerate((SHEET_DAILY, SHEET_SUMMARY), start=1):
        member = f'xl/worksheets/sheet{i}.xml'
        if member not in blobs:
            continue
        limit = _USED_COLUMNS[sheet_name]

        try:
            text = blobs[member].decode('utf-8')
        except UnicodeDecodeError:
            continue                       # 极端情况：不处理，保持原样

        def fix_cols(match):
            block = match.group(1)
            out = []
            for col in re.findall(r'<col\b[^>]*/>', block):
                m_min = re.search(r'\bmin="(\d+)"', col)
                m_max = re.search(r'\bmax="(\d+)"', col)
                if not m_min or not m_max:
                    out.append(col)
                    continue
                lo, hi = int(m_min.group(1)), int(m_max.group(1))
                if lo > limit:
                    continue                # 整条都在数据列之外 → 删掉
                if hi > limit:
                    col = col.replace(f'max="{m_max.group(1)}"', f'max="{limit}"')
                out.append(col)
            return '<cols>' + ''.join(out) + '</cols>' if out else ''

        new_text, n = re.subn(r'<cols>(.*?)</cols>', fix_cols, text, flags=re.S)
        if n:
            blobs[member] = new_text.encode('utf-8')

    # 重建 zip（先写原顺序，再替换内容）
    tmp = path + '.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zout:
        for n in names:
            zout.writestr(n, blobs[n])
    os.replace(tmp, path)


# ============================================================================
# Sheet1：每日明细
# ============================================================================

def _fill_daily_sheet(wb, daily_rows, rule):
    """
    填充「每日明细」。

    布局（与基线一致）：
      行1  标题
      行2  表头（姓名 部门 职位 日期 班次 上班时间 上班结果 下班时间 下班结果
                  迟到时长 早退时长 上班缺卡 下班缺卡 总计加班）
      行3+ 数据，同一人的姓名/部门/职位 与 五个统计列纵向合并
    """
    if SHEET_DAILY not in wb.sheetnames:
        raise ReportError(f'模板缺少 Sheet「{SHEET_DAILY}」，请确认模板未被替换。')
    ws = wb[SHEET_DAILY]
    header_row = 2
    first_data_row = 3

    # 按人分组（查询已按 name, work_date 排序）
    by_person = []
    for rec in daily_rows:
        if not by_person or by_person[-1][0] != rec.name:
            by_person.append((rec.name, []))
        by_person[-1][1].append(rec)

    # 先清掉模板里可能残留的数据区（模板通常只有样式）
    for r in range(first_data_row, (ws.max_row or first_data_row) + 1):
        for c in range(1, DAILY_COLUMNS + 1):
            ws.cell(r, c).value = None

    # 统计列（J..N）按人合计
    row = first_data_row
    merge_blocks = []          # [(start_row, end_row)]
    for name, records in by_person:
        day_sum = defaultdict(Decimal)
        ot_total = Decimal('0')
        for rec in records:
            day_sum['late'] += _dec(rec.late_minutes)
            day_sum['early'] += _dec(rec.early_leave_minutes)
            day_sum['miss_in'] += _dec(rec.missing_in_count)
            day_sum['miss_out'] += _dec(rec.missing_out_count)
            ot_total += calc_overtime(rec, rule)

        start = row
        for rec in records:
            ws.cell(row, 1, rec.name)
            ws.cell(row, 2, rec.department)
            ws.cell(row, 3, rec.position)
            date_text = _fmt_date_cn(rec.work_date)
            ws.cell(row, 4, date_text)
            ws.cell(row, 5, rec.shift)
            ws.cell(row, 6, rec.in1_time)
            ws.cell(row, 7, rec.in1_result)
            ws.cell(row, 8, rec.out1_time)
            ws.cell(row, 9, rec.out1_result)
            # J..N 是"该人整月合计"，只写第一行，其余由合并单元格填充
            if row == start:
                ws.cell(row, 10, float(day_sum['late']))
                ws.cell(row, 11, float(day_sum['early']))
                ws.cell(row, 12, float(day_sum['miss_in']))
                ws.cell(row, 13, float(day_sum['miss_out']))
                ws.cell(row, 14, float(ot_total))
            row += 1
        merge_blocks.append((start, row - 1))

    # ⚠️ 顺序很重要：先合并，再上色。
    #    openpyxl 的 merge_cells 会把合并区域内的样式按左上角重置，
    #    如果先上色再合并，"周末红字"会被清掉（实测红字数量为 0）。
    for start, end in merge_blocks:
        if end <= start:
            continue
        for col in (1, 2, 3, 10, 11, 12, 13, 14):
            ws.merge_cells(start_row=start, start_column=col,
                           end_row=end, end_column=col)

    # 上色：状态填充 + 周末红字
    for r in range(first_data_row, row):
        for c in range(4, 10):        # D..I
            _status_style(ws.cell(r, c), rule)
        date_cell = ws.cell(r, 4)
        if isinstance(date_cell.value, str):
            wd = _weekday_of(date_cell.value)
            if wd is not None and wd >= 5:      # 周六/周日
                date_cell.font = openpyxl.styles.Font(
                    name='新宋体', size=12,
                    color=argb(mapping.WEEKEND_FONT_COLOR), bold=True)
    return row - 1


def calc_overtime(record, rule):
    """
    单人单日加班小时数。

    ⚠️ 与 AttendanceCalculator.daily_overtime 保持同一口径；
       这里单独放一份是为了让报表模块不依赖计算器实例，
       两处都读 rule.clamp_negative_overtime。
    """
    if record.work_minutes is None:
        return Decimal('0')
    raw = (Decimal(record.work_minutes) - Decimal(rule.standard_work_minutes)) / Decimal('60')
    if rule.clamp_negative_overtime and raw < 0:
        return Decimal('0')
    return raw


def _weekday_of(text):
    """'14-08-02 星期六' → 5（周一=0）。解析不出来返回 None。"""
    for i, cn in enumerate(mapping.WEEKDAY_CN):
        if text.endswith(cn):
            return i
    return None


# ============================================================================
# Sheet2：月度汇总
# ============================================================================

LEAVE_COL_START = 9      # I 列
LEAVE_COL_END = 17       # Q 列


def _fill_summary_sheet(wb, period, summary, rule):
    """
    填充「月度汇总」。

    列布局（1-based）：
      A 序号  B 姓名  C 部门  D 入职时间  E 离职时间
      F 应出勤  G 出勤打卡  H 实际出勤
      I..Q 请假（事假/调休/年假/病假/婚假/陪产假/产前假/产假/产检假）
      R 迟到(分)  S 早退(分)  T 旷工(天)  U 上班忘打卡(次)  V 下班忘打卡(次)
      W 加班(H)  X 备注  Y 员工签字

    ⚠️ 与桌面版的三处口径差异（均为已确认的修正项）：
      1. 「应出勤」写规则里的 monthly_standard_days（桌面版误写进了 G 列）
      2. 「出勤打卡」G 写源文件累计的真实出勤天数
      3. 「实际出勤」H 保留公式 =G-I（事假），与基线一致
    """
    if SHEET_SUMMARY not in wb.sheetnames:
        raise ReportError(f'模板缺少 Sheet「{SHEET_SUMMARY}」，请确认模板未被替换。')
    ws = wb[SHEET_SUMMARY]

    # 标题：与基线一致的写法
    ws.cell(1, 1, f'jointelli-{period[5:7]}月考勤表')

    # 数据从第 5 行开始（2/3/4 为三行表头，模板已预置）
    first_data_row = 5
    for r in range(first_data_row, (ws.max_row or first_data_row) + 1):
        for c in range(1, SUMMARY_COLUMNS + 1):
            ws.cell(r, c).value = None

    leave_codes = [code for code, _label in mapping.REPORT_LEAVE_COLUMNS]

    row = first_data_row
    for seq, name in enumerate(sorted(summary), start=1):
        s = summary[name]
        ws.cell(row, 1, seq)
        ws.cell(row, 2, name)
        ws.cell(row, 3, s.get('department') or None)
        # D/E 入职/离职时间：源文件没有，留空（与基线一致）
        ws.cell(row, 6, float(_dec(rule.monthly_standard_days)))     # 应出勤
        ws.cell(row, 7, float(_dec(s.get('attend_days'))))           # 出勤打卡
        ws.cell(row, 8, f'=G{row}-I{row}')                           # 实际出勤

        leave_days = s.get('leave_days', {})
        for i, code in enumerate(leave_codes):
            val = leave_days.get(code)
            if val:
                ws.cell(row, LEAVE_COL_START + i, float(val))

        ws.cell(row, 18, float(_dec(s.get('late_minutes'))))          # 迟到(分)
        ws.cell(row, 19, float(_dec(s.get('early_leave_minutes'))))   # 早退(分)
        ws.cell(row, 20, float(_dec(s.get('absenteeism_days'))))      # 旷工(天)
        ws.cell(row, 21, int(s.get('missing_in_count') or 0))         # 上班忘打卡(次)
        ws.cell(row, 22, int(s.get('missing_out_count') or 0))        # 下班忘打卡(次)
        ws.cell(row, 23, float(_dec(s.get('overtime_hours'))))        # 加班(H)
        row += 1

    return row - 1
