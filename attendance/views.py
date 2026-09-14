"""
考勤模块视图 —— 函数视图 + 装饰器链，禁止 CBV（项目计划书第 2 节第 1 条）

装饰器顺序固定为：@login_required → @permission_required(...)

权限码：
    attendance.upload         上传、删除导入数据
    attendance.query          查询日明细 / 月度汇总
    attendance.export         导出查询结果
    attendance.report         生成并下载月度考勤报表
    attendance.rule_manage    修改考勤规则
"""
import logging
import os
import shutil

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from openpyxl.utils import get_column_letter

from accounts.decorators import permission_required
from accounts.services import log_action

from . import mapping
from .models import AttendanceDaily, AttendanceRule, LeaveRecord, UploadedFile
from .permissions import can_delete_import, can_view_import, can_view_import_all
from .services import (
    DingTalkHeaderParser,
    ExcelImporter,
    FileStorage,
    ImportError_,
    load_workbook_plain,
    normalize_text,
)

logger = logging.getLogger('attendance')


# ============================================================================
# 首页 / 概览
# ============================================================================

@login_required
@permission_required('attendance.query')
def overview(request):
    """考勤概览 —— 可见月份与最近导入"""
    periods = (
        UploadedFile.objects.exclude(period__isnull=True).exclude(period='')
        .order_by().values_list('period', flat=True).distinct()
    )
    context = {
        'periods': sorted([p for p in periods if p], reverse=True),
        'recent_files': UploadedFile.objects.select_related('uploaded_by').order_by('-uploaded_at')[:10],
        'daily_count': AttendanceDaily.objects.count(),
        'leave_count': LeaveRecord.objects.count(),
    }
    return render(request, 'attendance/overview.html', context)


# ============================================================================
# 上传与导入（Phase 2）
# ============================================================================

def _valid_period(period):
    """账期必须是 YYYY-MM。返回规范化字符串或 None。"""
    if not period:
        return None
    try:
        year, month = period.split('-')
        year, month = int(year), int(month)
    except (ValueError, AttributeError):
        return None
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    return f'{year:04d}-{month:02d}'


def _existing_imports(period, file_kind):
    """该账期该类型已有的成功导入"""
    return UploadedFile.objects.filter(
        period=period, file_kind=file_kind, status__in=('success', 'partial')
    ).order_by('-uploaded_at')


def _period_input_bounds():
    """
    上传页「账期」输入框的上下界。

    与查询页不同，上传要允许**补录当月**（当月数据可能还没导完），
    所以上界放宽到当前月份的下一个月；下界取已有数据的起点，
    再兜底留一年余地，避免历史数据还没导入时无法选择。
    """
    from .utils import month_bounds, attendance_date_bounds, leave_date_bounds

    lo, hi = month_bounds(attendance_date_bounds(), leave_date_bounds())
    today = timezone.localdate()

    # 下界：有数据时用数据起点再往前留 12 个月，否则用今天往前 36 个月
    if lo:
        y, m = (int(x) for x in lo.split('-'))
        m -= 12
        while m < 1:
            m += 12
            y -= 1
        lo = f'{y:04d}-{m:02d}'
    else:
        y, m = today.year, today.month - 36
        while m < 1:
            m += 12
            y -= 1
        lo = f'{y:04d}-{m:02d}'

    # 上界：当前月 + 1（允许提前建账期），且不小于已有数据的上界
    y, m = today.year, today.month + 1
    if m > 12:
        m -= 12
        y += 1
    hi_allowed = f'{y:04d}-{m:02d}'
    hi = max(hi, hi_allowed) if hi else hi_allowed

    return {'month_min': lo, 'month_max': hi}


def _run_import(request, record, rule, purge):
    """执行解析入库并落库报告。返回 (ok, skipped, report)；失败抛 ImportError_。"""
    if record.file_kind == 'daily':
        return ExcelImporter.import_daily(record, request.user, rule,
                                          purge_existing=purge)
    return ExcelImporter.import_leave(record, request.user, rule,
                                      purge_existing=purge)


def _finalize_import(request, record, ok, skipped, report):
    """把导入结果写回 UploadedFile 并记审计日志"""
    has_errors = bool(report.get('errors'))
    record.record_count = ok
    record.skipped_count = skipped
    record.import_report = report
    record.status = 'partial' if (has_errors or skipped) else 'success'
    record.processed_at = timezone.now()
    record.save(update_fields=['record_count', 'skipped_count', 'import_report',
                               'status', 'processed_at'])

    kind_label = '日考勤' if record.file_kind == 'daily' else '请假'
    purge_note = ''
    if report.get('purged'):
        purge_note = f'，清空旧数据 {report["purged"]["records"]} 条'
    log_action(request, 'UPLOAD', 'attendance',
               f'导入{kind_label}数据 {record.original_filename}'
               f'（账期 {record.period}）：成功 {ok} 条，跳过 {skipped} 条{purge_note}',
               'attendance_uploaded_files', record.id)

    if ok:
        messages.success(request, f'导入完成：成功 {ok} 条，跳过 {skipped} 条。')
    else:
        messages.warning(request, f'没有导入任何记录（跳过 {skipped} 条），请查看导入报告。')


def _mark_failed(request, record, exc):
    """导入失败：落库错误并记审计，保证页面不 500"""
    logger.warning('导入失败 file=%s: %s', record.original_filename, exc)
    record.status = 'failed'
    record.error_message = str(exc)
    record.import_report = {'errors': [{'message': str(exc)}]}
    record.processed_at = timezone.now()
    record.save(update_fields=['status', 'error_message', 'import_report', 'processed_at'])
    log_action(request, 'UPLOAD_FAILED', 'attendance',
               f'导入失败（{record.get_file_kind_display()}，账期 {record.period}）：{exc}',
               'attendance_uploaded_files', record.id)
    messages.error(request, f'导入失败：{exc}')


@login_required
@permission_required('attendance.upload')
def upload(request):
    """
    数据上传页。

    上传是同步处理的：实测样本 2759 行 × 51 列的文件，解析+入库在 1 秒级，
    无需引入异步队列；失败时把可读的错误显示在页面上（不 500）。
    """
    rule = AttendanceRule.get_active()
    from .utils import date_input_bounds
    context = {
        'file_kinds': UploadedFile.FILE_KIND_CHOICES,
        'recent_files': UploadedFile.objects.select_related('uploaded_by')
                        .order_by('-uploaded_at')[:10],
        'max_size_mb': settings.IMPORT_MAX_FILE_SIZE / 1024 / 1024,
        'allowed_extensions': settings.ALLOWED_EXTENSIONS,
        'rule': rule,
        # 账期输入的上下界。上传新账期时略放宽（允许当前月之后一个月，
        # 便于补录当月数据），但不会像默认那样能翻到 1000 年。
        'period_bounds': _period_input_bounds(),
    }

    if request.method != 'POST':
        return render(request, 'attendance/upload.html', context)

    # ── 分支 A：用户在"该账期已有数据"提示里确认了处理方式 ──
    reuse_id = request.POST.get('reuse_id')
    if reuse_id:
        record = get_object_or_404(UploadedFile, pk=reuse_id, uploaded_by=request.user)
        # 确认页给了两个按钮：合并（仅新增/覆盖同日）与整账期覆盖。
        # 默认取合并——破坏性更小的那个，避免漏传参数时误清整月数据。
        purge = request.POST.get('purge_existing') == 'on'
        try:
            ok, skipped, report = _run_import(request, record, rule, purge=purge)
        except ImportError_ as exc:
            _mark_failed(request, record, exc)
            return redirect('attendance:import_detail', pk=record.pk)
        except Exception as exc:                       # noqa: BLE001
            logger.exception('导入未预期异常 file=%s', record.original_filename)
            _mark_failed(request, record, f'未预期异常：{exc}')
            return redirect('attendance:import_detail', pk=record.pk)
        _finalize_import(request, record, ok, skipped, report)
        return redirect('attendance:import_detail', pk=record.pk)

    # ── 分支 B：常规上传 ──
    uploaded = request.FILES.get('file')
    file_kind = request.POST.get('file_kind', '').strip()
    raw_period = request.POST.get('period', '').strip()
    period = _valid_period(raw_period)
    purge = request.POST.get('purge_existing') == 'on'

    form_state = {'file_kind': file_kind, 'period': raw_period, 'purge_existing': purge}

    valid_kinds = {code for code, _ in UploadedFile.FILE_KIND_CHOICES}
    if file_kind not in valid_kinds:
        messages.error(request, '请选择正确的文件类型。')
        return render(request, 'attendance/upload.html', {**context, 'form': form_state})
    if not period:
        messages.error(request, '账期格式不正确，请选择月份（如 2014-08）。')
        return render(request, 'attendance/upload.html', {**context, 'form': form_state})
    if uploaded is None:
        messages.error(request, '请选择要上传的文件。')
        return render(request, 'attendance/upload.html', {**context, 'form': form_state})

    # ── 落地前先做扩展名/大小/空文件校验（生产数据系统缺这三项）──
    try:
        FileStorage.validate_upload(uploaded)
    except ImportError_ as exc:
        log_action(request, 'UPLOAD_FAILED', 'attendance',
                   f'上传被拒（{file_kind}，{period}）：{exc}')
        messages.error(request, str(exc))
        return render(request, 'attendance/upload.html', {**context, 'form': form_state})

    existing = _existing_imports(period, file_kind)
    record = FileStorage.save(uploaded, file_kind, period, request.user)

    # 同账期已有数据且未勾选覆盖 → 先问，不擅自清库
    if existing and not purge:
        return render(request, 'attendance/upload.html', {
            **context, 'pending': record, 'existing': existing, 'form': form_state,
        })

    record.status = 'processing'
    record.save(update_fields=['status'])
    try:
        ok, skipped, report = _run_import(request, record, rule, purge=purge)
    except ImportError_ as exc:
        _mark_failed(request, record, exc)
        return redirect('attendance:import_detail', pk=record.pk)
    except Exception as exc:                           # noqa: BLE001
        logger.exception('导入未预期异常 file=%s', record.original_filename)
        _mark_failed(request, record, f'未预期异常：{exc}')
        return redirect('attendance:import_detail', pk=record.pk)

    _finalize_import(request, record, ok, skipped, report)
    return redirect('attendance:import_detail', pk=record.pk)


@login_required
@permission_required('attendance.upload')
def import_preview(request, pk):
    """
    只解析不入库的预览 —— 看清表头识别结果再决定是否导入。

    权限：上传者只能预览自己传的文件；有 query 权限的可看全部
    （见 attendance/permissions.py）。
    """
    record = get_object_or_404(UploadedFile, pk=pk)
    if not can_view_import(request.user, record):
        messages.error(request, '您没有权限查看该导入记录。')
        return redirect('attendance:file_list')

    try:
        wb = load_workbook_plain(record.file_path)
    except ImportError_ as exc:
        messages.error(request, str(exc))
        return redirect('attendance:import_detail', pk=pk)

    sheets = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        info = {'sheet': sheet_name, 'max_row': ws.max_row, 'max_col': ws.max_column,
                'merged': len(ws.merged_cells.ranges), 'error': None,
                'header_rows': [], 'columns': [], 'samples': [], 'unknown': []}

        if record.file_kind == 'daily':
            try:
                parser = DingTalkHeaderParser(ws)
                parser.parse()
                field_index, unknown = parser.build_field_index()
                info['header_rows'] = parser.header_row_numbers
                info['columns'] = [
                    {'letter': c['letter'], 'group': c['group'], 'leaf': c['leaf'],
                     'mapped': field_index.get(
                         mapping.DAILY_COMPOSITE_FIELDS.get(c['group'], {}).get(c['leaf'])
                         if c['group'] else mapping.DAILY_SIMPLE_FIELDS.get(c['leaf']))}
                    for c in parser.columns
                ]
                info['unknown'] = unknown
                info['leave_leaf_count'] = len(
                    [c for c in parser.columns if c['group'] == '请假'])
                for i, (excel_row, values) in enumerate(parser.read_data_rows()):
                    if i >= 5:
                        break
                    info['samples'].append({
                        'row': excel_row,
                        'cells': [normalize_text(v) for v in values],
                    })
            except ImportError_ as exc:
                info['error'] = str(exc)
        else:
            # 只读前 6 行（够看表头与样本），一次批量读
            max_col = ws.max_column or 0
            head = [list(r) for r in ws.iter_rows(
                min_row=1, max_row=min((ws.max_row or 0), 6),
                min_col=1, max_col=max_col, values_only=True)]
            header = {}
            for c, v in enumerate(head[0] if head else [], start=1):
                header[c] = normalize_text(v)
            info['columns'] = [
                {'letter': get_column_letter(c), 'group': None, 'leaf': text,
                 'mapped': mapping.LEAVE_FIELDS.get(text)}
                for c, text in header.items() if text
            ]
            info['unknown'] = [text for c, text in header.items()
                               if text and text not in mapping.LEAVE_FIELDS]
            for i, row_values in enumerate(head[1:], start=2):
                info['samples'].append({
                    'row': i,
                    'cells': [normalize_text(v) for v in row_values],
                })
        sheets.append(info)

    wb.close()
    return render(request, 'attendance/import_preview.html', {
        'record': record, 'sheets': sheets,
    })


@login_required
@permission_required('attendance.upload', 'attendance.query')
def import_detail(request, pk):
    """
    导入报告：精确到 Sheet 名 + 行号 + 字段 + 值。

    ⚠️ 装饰器只能粗粒度放行（upload 或 query），真正的可见性判断在
       can_view_import 里：上传者仅能看自己的，有 query 的可看全部。
    """
    record = get_object_or_404(UploadedFile.objects.select_related('uploaded_by'), pk=pk)
    if not can_view_import(request.user, record):
        messages.error(request, '您没有权限查看该导入记录。')
        return redirect('attendance:file_list')
    report = record.import_report or {}
    return render(request, 'attendance/import_detail.html', {
        'record': record,
        'report': report,
        'errors': report.get('errors', []),
        'warnings': report.get('warnings', []),
        'excluded': report.get('excluded', []),
        'sheets': report.get('sheets', []),
        'purged': report.get('purged'),
    })


@login_required
def file_delete(request, pk):
    """
    删除一次导入（连同其考勤/请假记录）。

    ⚠️ 删除是破坏性操作，权限比"查看"更严：只有管理员或**该文件的上传者本人**
       可以删。有 attendance.query 的人能看但不能删（见 can_delete_import）。
    """
    record = get_object_or_404(UploadedFile, pk=pk)
    if not can_delete_import(request.user, record):
        messages.error(request, '您没有权限删除该导入记录。')
        return redirect('attendance:file_list')

    filename = record.original_filename
    kind = record.get_file_kind_display()
    period = record.period
    log_action(request, 'ATTENDANCE_FILE_DELETE', 'attendance',
               f'删除{kind}导入：{filename}（账期 {period}）',
               'attendance_uploaded_files', pk)
    record.delete()
    messages.success(request, f'已删除导入记录「{filename}」及其数据。')
    return redirect('attendance:file_list')


@login_required
@permission_required('attendance.upload', 'attendance.query')
def file_list(request):
    """
    导入记录列表。

    ⚠️ 数据范围按权限收敛（见 attendance/permissions.py）：
       · 有 attendance.query → 看到全部导入
       · 只有 attendance.upload → 只看到**自己上传**的
    导航与页面上的"我的上传/导入记录"文案也据此切换。
    """
    can_see_all = can_view_import_all(request.user)
    qs = UploadedFile.objects.select_related('uploaded_by').order_by('-uploaded_at')
    if not can_see_all:
        qs = qs.filter(uploaded_by=request.user)

    kind = request.GET.get('kind', '').strip()
    period = request.GET.get('period', '').strip()
    if kind:
        qs = qs.filter(file_kind=kind)
    if period:
        qs = qs.filter(period=period)

    page = Paginator(qs, 20).get_page(request.GET.get('page', 1))
    from .utils import date_input_bounds
    return render(request, 'attendance/file_list.html', {
        'files': page,
        'kind': kind,
        'period': period,
        'file_kinds': UploadedFile.FILE_KIND_CHOICES,
        'bounds': date_input_bounds(),
        'can_see_all': can_see_all,
    })


# ============================================================================
# 查询（Phase 4 完善筛选，Phase 1 打通分页与数量上限）
# ============================================================================

@login_required
@permission_required('attendance.query')
def daily_list(request):
    """
    每日明细查询。

    ⚠️ 必须设数量上限：生产数据系统实测 3000 条命中 = 5MB HTML，
       10 万条约 170MB，页面直接卡死（计划书第 389 行）。
       这里做两层保护：
         1. 先 count()，超过 QUERY_MAX_RESULTS 就拒绝渲染并提示缩小范围；
         2. 未超限才用 Paginator 分页。
    """
    qs = AttendanceDaily.objects.select_related('source_file').order_by('-work_date', 'name')

    name = request.GET.get('name', '').strip()
    department = request.GET.get('department', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    if name:
        qs = qs.filter(name__icontains=name)
    if department:
        qs = qs.filter(department__icontains=department)
    if date_from:
        qs = qs.filter(work_date__gte=date_from)
    if date_to:
        qs = qs.filter(work_date__lte=date_to)

    total = qs.count()
    over_limit = total > settings.QUERY_MAX_RESULTS
    page = None if over_limit else Paginator(qs, settings.QUERY_PAGE_SIZE).get_page(request.GET.get('page', 1))

    from .utils import date_input_bounds
    return render(request, 'attendance/daily_list.html', {
        'page': page,
        'total': total,
        'over_limit': over_limit,
        'max_results': settings.QUERY_MAX_RESULTS,
        'filters': {'name': name, 'department': department, 'date_from': date_from, 'date_to': date_to},
        'departments': (AttendanceDaily.objects.exclude(department__isnull=True)
                        .exclude(department='').order_by()
                        .values_list('department', flat=True).distinct()),
        'bounds': date_input_bounds(),
    })


@login_required
@permission_required('attendance.query')
def leave_list(request):
    """
    请假记录查询。

    与月度汇总互补：汇总页只看得到"每人每类几天"，这里能看到**每一条单据**
    （审批编号、起止时间、时长、审批状态），用于核对与追溯。

    同样有数量上限保护，避免命中过多把页面拖死。
    """
    qs = LeaveRecord.objects.select_related('source_file').order_by('-start_time')

    name = request.GET.get('name', '').strip()
    approval_no = request.GET.get('approval_no', '').strip()
    leave_type = request.GET.get('leave_type', '').strip()
    period = request.GET.get('period', '').strip()
    status = request.GET.get('status', '').strip()

    if name:
        qs = qs.filter(applicant_name__icontains=name)
    if approval_no:
        # 审批编号是钉钉的唯一单据号，模糊匹配便于凭记忆里的片段找单
        qs = qs.filter(approval_no__icontains=approval_no)
    if leave_type:
        qs = qs.filter(leave_type=leave_type)
    if period:
        qs = qs.filter(period=period)
    if status == 'approved':
        qs = qs.filter(is_approved=True)
    elif status == 'rejected':
        qs = qs.filter(is_approved=False)
    elif status == 'resigned':
        qs = qs.filter(is_resigned=True)

    total = qs.count()
    over_limit = total > settings.QUERY_MAX_RESULTS
    page = None if over_limit else Paginator(qs, settings.QUERY_PAGE_SIZE).get_page(
        request.GET.get('page', 1))

    if page is not None:
        log_action(request, 'ATTENDANCE_LEAVE_LIST_VIEW', 'attendance',
                   f'查询请假记录：命中 {total} 条'
                   + (f'（姓名={name}）' if name else '')
                   + (f'（审批编号={approval_no}）' if approval_no else '')
                   + (f'（账期={period}）' if period else ''))

    from .utils import date_input_bounds
    return render(request, 'attendance/leave_list.html', {
        'page': page,
        'total': total,
        'over_limit': over_limit,
        'max_results': settings.QUERY_MAX_RESULTS,
        'filters': {'name': name, 'approval_no': approval_no, 'leave_type': leave_type,
                    'period': period, 'status': status},
        'leave_types': (LeaveRecord.objects.order_by()
                        .values_list('leave_type', flat=True).distinct()),
        'periods': sorted(
            {p for p in LeaveRecord.objects.exclude(period__isnull=True)
             .exclude(period='').values_list('period', flat=True) if p},
            reverse=True,
        ),
        'approved_count': LeaveRecord.objects.filter(is_approved=True).count(),
        'resigned_count': LeaveRecord.objects.filter(is_resigned=True).count(),
        'bounds': date_input_bounds(),
    })


@login_required
@permission_required('attendance.query')
def monthly_summary(request):
    """
    月度汇总页 —— Web 版 Sheet2（替代"只能看 Excel"）。

    计算逻辑统一走 AttendanceCalculator，与报表导出共用同一份口径。
    支持勾选多个账期做**跨月对比**（桌面工具不具备的能力）。
    """
    from . import mapping
    from .services import AttendanceCalculator

    period = request.GET.get('period', '').strip()
    # 对比账期：最多 3 个，且不与主账期重复
    compare_periods = []
    for p in request.GET.getlist('compare'):
        p = p.strip()
        if p and p != period and p not in compare_periods and len(compare_periods) < 3:
            compare_periods.append(p)

    periods = sorted(
        {p for p in UploadedFile.objects.exclude(period__isnull=True)
         .exclude(period='').values_list('period', flat=True) if p},
        reverse=True,
    )

    leave_columns = [label for _code, label in mapping.REPORT_LEAVE_COLUMNS]
    calc = AttendanceCalculator()

    def build_rows(p):
        summary = calc.summarize_month(p)
        out = []
        for name in sorted(summary):
            row = summary[name]
            out.append({
                'name': name,
                'department': row['department'],
                'attend_should': row['attend_should'],
                'attend_days': row['attend_days'],
                'actual_attend': row['actual_attend'],
                'leave': [row.get('leave_days', {}).get(label, 0) for label in leave_columns],
                'late_minutes': row['late_minutes'],
                'early_leave_minutes': row['early_leave_minutes'],
                'absenteeism_days': row['absenteeism_days'],
                'missing_in_count': row['missing_in_count'],
                'missing_out_count': row['missing_out_count'],
                'overtime_hours': row['overtime_hours'],
            })
        return out

    rows = []
    if period:
        rows = build_rows(period)
        log_action(request, 'ATTENDANCE_SUMMARY_VIEW', 'attendance', f'查看 {period} 月度汇总')

    # ── 跨月对比 ──
    # 只对比"总体指标 + 逐人加班/出勤"，不把 9 类请假全展开（否则列太多没法看）。
    comparison = None
    if period and compare_periods:
        all_periods = [period] + compare_periods
        summaries = {p: calc.summarize_month(p) for p in all_periods}
        names = sorted(set().union(*(set(s) for s in summaries.values())))

        def totals(s):
            return {
                'people': len(s),
                'attend': sum(r['attend_days'] for r in s.values()),
                'overtime': sum(r['overtime_hours'] for r in s.values()),
                'late': sum(r['late_minutes'] for r in s.values()),
                'leave': sum(sum(r.get('leave_days', {}).values()) for r in s.values()),
            }

        per_period_totals = [totals(summaries[p]) for p in all_periods]
        # 汇总指标按行组织并在视图里格式化好，模板里直接渲染，
        # 避免在模板中做字典查找与格式判断（Django 模板不支持带参数的过滤器变量）
        total_rows = [
            {'label': '人数',
             'values': [str(t['people']) for t in per_period_totals]},
            {'label': '出勤合计（天）',
             'values': [f"{t['attend']:.0f}" for t in per_period_totals]},
            {'label': '加班合计（H）',
             'values': [f"{t['overtime']:.1f}" for t in per_period_totals]},
            {'label': '请假合计（天）',
             'values': [f"{t['leave']:.1f}" for t in per_period_totals]},
            {'label': '迟到合计（分）',
             'values': [f"{t['late']:.0f}" for t in per_period_totals]},
        ]

        comparison = {
            'periods': all_periods,
            'total_rows': total_rows,
            'per_person': [
                {'name': n,
                 'cells': [
                     {'attend': summaries[p].get(n, {}).get('attend_days', 0),
                      'overtime': summaries[p].get(n, {}).get('overtime_hours', 0),
                      'leave': sum(summaries[p].get(n, {}).get('leave_days', {}).values()),
                      'present': n in summaries[p]}
                     for p in all_periods]}
                for n in names
            ],
        }
        log_action(request, 'ATTENDANCE_SUMMARY_COMPARE', 'attendance',
                   f'跨月对比 {"、".join(all_periods)}')

    total_overtime = sum((r['overtime_hours'] or 0) for r in rows)
    total_attend = sum((r['attend_days'] or 0) for r in rows)
    return render(request, 'attendance/monthly_summary.html', {
        'rows': rows,
        'period': period,
        'periods': periods,
        'leave_columns': leave_columns,
        'total_overtime': total_overtime,
        'total_attend': total_attend,
        'compare_periods': compare_periods,
        'comparison': comparison,
    })


# ============================================================================
# 报表导出（Phase 4 实现，先占位并做权限边界）
# ============================================================================

def _report_session_key(request):
    return f'attendance_report_dir_{request.session.session_key or "anon"}'


def _report_dir_for_write(request):
    """
    为当前会话准备报表暂存目录：**先清掉上一次的**，再建一个新的。

    ⚠️ 只在"生成"路径调用。不要在"下载"路径调用 ——
       这个函数会把目录连同里面刚生成的文件一起删掉
       （踩过：下载时只是想取路径，结果把文件删了，于是报"文件不存在"）。
    """
    import tempfile

    key = _report_session_key(request)
    old = request.session.get(key)
    if old and os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
    new_dir = tempfile.mkdtemp(prefix='attendance-report-')
    request.session[key] = new_dir
    return new_dir


def _report_dir_for_read(request):
    """读取当前会话已有的报表暂存目录；没有则返回 None（**不做任何清理**）"""
    return request.session.get(_report_session_key(request))


def _report_path(request, period, *, for_write):
    from .report import default_output_name

    directory = (_report_dir_for_write(request) if for_write
                 else _report_dir_for_read(request))
    if not directory:
        return None
    return os.path.join(directory, default_output_name(period))


def _generate_report_file(request, period, rule):
    """
    生成报表到会话暂存目录。返回 (文件路径, 错误消息)。
    生成失败时返回 (None, 可读错误)。
    """
    from .report import ReportError, build_report

    path = _report_path(request, period, for_write=True)
    try:
        build_report(period, rule=rule, path=path)
    except ReportError as exc:
        return None, str(exc)
    except Exception as exc:                               # noqa: BLE001
        logger.exception('报表生成异常 period=%s', period)
        return None, f'生成过程发生未预期异常：{exc}'

    log_action(request, 'ATTENDANCE_REPORT_GENERATE', 'attendance',
               f'生成 {period} 考勤报表：{os.path.basename(path)}')
    return path, None


@login_required
@permission_required('attendance.report')
def report_generate(request):
    """
    生成考勤报表。

    三种用法：
      · GET  无参数          → 参数页
      · POST period=...      → 生成并以 **JSON** 回执（页面据此复位按钮）
      · GET  period=...&download=1 → 下载已生成的文件

    报表按需生成、下载后即弃：文件只暂存在当前会话的临时目录里，
    下次生成时清掉上一次的。
    """
    periods = sorted(
        {p for p in UploadedFile.objects.exclude(period__isnull=True)
         .exclude(period='').values_list('period', flat=True) if p},
        reverse=True,
    )
    rule = AttendanceRule.get_active()

    if request.method == 'GET' and not (request.GET.get('period') or '').strip():
        return render(request, 'attendance/report.html', {'periods': periods, 'rule': rule})

    raw = (request.POST.get('period') if request.method == 'POST'
           else request.GET.get('period')) or ''
    period = _valid_period(raw.strip())
    if not period:
        message = '请选择账期。'
        if request.method == 'POST':
            return JsonResponse({'success': False, 'message': message}, status=400)
        messages.error(request, message)
        return render(request, 'attendance/report.html', {'periods': periods, 'rule': rule})

    # ── 下载：直接回传已生成的文件（只读，不清理目录） ──
    if request.method == 'GET' and request.GET.get('download'):
        from .report import default_output_name

        path = _report_path(request, period, for_write=False)
        if not path or not os.path.exists(path):
            messages.error(request, '报表文件不存在或已过期，请重新生成。')
            return render(request, 'attendance/report.html',
                          {'periods': periods, 'rule': rule})
        return FileResponse(open(path, 'rb'), as_attachment=True,
                            filename=default_output_name(period))

    # ── 生成 ──
    path, error = _generate_report_file(request, period, rule)
    if request.method == 'POST':
        if error:
            return JsonResponse({'success': False, 'message': f'生成失败：{error}'},
                                status=400)
        from .report import default_output_name
        return JsonResponse({
            'success': True,
            'message': '报表已生成，正在开始下载…',
            'filename': default_output_name(period),
            'size': os.path.getsize(path),
            'url': f"{reverse('attendance:report_generate')}"
                   f"?period={period}&download=1",
        })

    if error:
        messages.error(request, f'生成失败：{error}')
    return render(request, 'attendance/report.html', {'periods': periods, 'rule': rule})


# ============================================================================
# 规则配置
# ============================================================================

@login_required
@permission_required('attendance.rule_manage')
def rule_config(request):
    """考勤规则配置页 —— 替换桌面工具的硬编码"""
    rule = AttendanceRule.get_active()
    return render(request, 'attendance/rule_config.html', {
        'rule': rule,
        'exclude_modes': mapping.EXCLUDE_MODE_CHOICES,
    })


@login_required
@permission_required('attendance.rule_manage')
@require_http_methods(['POST'])
def rule_save(request):
    """保存规则（变更必须留痕，对应计划书 7.3 审计覆盖）"""
    rule = AttendanceRule.get_active()
    before = {
        'standard_work_minutes': rule.standard_work_minutes,
        'monthly_standard_days': str(rule.monthly_standard_days),
        'excluded_employees': list(rule.excluded_employees or []),
        'excluded_attend_groups': list(rule.excluded_attend_groups or []),
        'exclude_if_no_data': rule.exclude_if_no_data,
        'clamp_negative_overtime': rule.clamp_negative_overtime,
        'exclude_rejected_leave': rule.exclude_rejected_leave,
    }

    try:
        rule.standard_work_minutes = int(request.POST.get('standard_work_minutes') or 450)
        rule.monthly_standard_days = request.POST.get('monthly_standard_days') or 24
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'message': '标准工作时长与应出勤天数必须是数字'})

    rule.excluded_employees = [s.strip() for s in (request.POST.get('excluded_employees') or '').replace('，', ',').split(',') if s.strip()]
    rule.excluded_attend_groups = [s.strip() for s in (request.POST.get('excluded_attend_groups') or '').replace('，', ',').split(',') if s.strip()]
    mode = request.POST.get('exclude_mode') or 'no_dept'
    valid_modes = {code for code, _ in mapping.EXCLUDE_MODE_CHOICES}
    if mode not in valid_modes:
        return JsonResponse({'success': False, 'message': '排除严格度取值非法'})
    rule.exclude_mode = mode
    rule.exclude_if_no_data = request.POST.get('exclude_if_no_data') == 'on'
    rule.clamp_negative_overtime = request.POST.get('clamp_negative_overtime') == 'on'
    rule.exclude_rejected_leave = request.POST.get('exclude_rejected_leave') == 'on'
    rule.save()

    after = {
        'standard_work_minutes': rule.standard_work_minutes,
        'monthly_standard_days': str(rule.monthly_standard_days),
        'excluded_employees': list(rule.excluded_employees or []),
        'excluded_attend_groups': list(rule.excluded_attend_groups or []),
        'exclude_if_no_data': rule.exclude_if_no_data,
        'clamp_negative_overtime': rule.clamp_negative_overtime,
        'exclude_rejected_leave': rule.exclude_rejected_leave,
    }
    changed = {k: {'before': before[k], 'after': after[k]} for k in before if before[k] != after[k]}
    log_action(request, 'ATTENDANCE_RULE_UPDATE', 'attendance',
               f'修改考勤规则：{changed}', 'attendance_rules', rule.id)

    return JsonResponse({'success': True, 'message': '规则已保存', 'changed': changed})
