"""
考勤模块的小工具函数 —— 放视图与模板共用的纯查询逻辑。
"""
from django.db.models import Max, Min

from .models import AttendanceDaily, LeaveRecord

# 数据为空时给一个合理的兜底区间，避免日期控件完全没有约束
FALLBACK_MIN_YEAR = 2000
FALLBACK_MAX_YEAR = 2100


def _span(values):
    """(min, max) 里去掉 None；都为空时返回 (None, None)"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None
    return min(vals), max(vals)


def attendance_date_bounds():
    """
    全库考勤数据的日期范围，用于给日期选择控件加上下界。

    返回 (min_date|None, max_date|None)。空库返回 (None, None)。

    ⚠️ 为什么要加下界：`<input type="date">` / `<input type="month">` 默认
       可以翻到任意年份（甚至 1000 年），用户很容易选到明显无意义的值，
       然后在页面上看到"没有数据"。给上数据范围即可避免这种无效查询。
    """
    agg = AttendanceDaily.objects.aggregate(lo=Min('work_date'), hi=Max('work_date'))
    return agg['lo'], agg['hi']


def leave_date_bounds():
    """请假数据的起止时间范围（取日期部分）。"""
    agg = LeaveRecord.objects.aggregate(lo=Min('start_time'), hi=Max('start_time'))
    lo, hi = agg['lo'], agg['hi']
    return (lo.date() if lo else None), (hi.date() if hi else None)


def month_bounds(*date_pairs):
    """
    由若干 (min_date, max_date) 推出「账期」的可选范围，返回 ('YYYY-MM', 'YYYY-MM')。

    空数据时退化为 (None, None)，模板里不渲染 min/max 属性。
    """
    los, his = [], []
    for lo, hi in date_pairs:
        if lo:
            los.append(lo)
        if hi:
            his.append(hi)
    if not los and not his:
        return None, None
    lo = min(los) if los else None
    hi = max(his) if his else None
    fmt = lambda d: f'{d.year:04d}-{d.month:02d}'   # noqa: E731
    return (fmt(lo) if lo else None), (fmt(hi) if hi else None)


def year_bounds(*date_pairs):
    """由日期范围推出年份上下界，用于 number 类型的年份输入。"""
    years = []
    for lo, hi in date_pairs:
        for d in (lo, hi):
            if d:
                years.append(d.year)
    if not years:
        return FALLBACK_MIN_YEAR, FALLBACK_MAX_YEAR
    return min(years), max(years)


def date_input_bounds():
    """
    给视图用：一次性算出日期与月份控件的上下界。

    返回 dict：
        date_min / date_max   'YYYY-MM-DD' 或 None   （给 <input type="date">）
        month_min / month_max 'YYYY-MM'    或 None   （给 <input type="month">）
        year_min / year_max   int                    （给年份 number 输入）
    """
    daily = attendance_date_bounds()
    leave = leave_date_bounds()
    iso = lambda d: d.isoformat() if d else None      # noqa: E731

    loos = [d for d in (daily[0], leave[0]) if d]
    his = [d for d in (daily[1], leave[1]) if d]
    d_lo = min(loos) if loos else None
    d_hi = max(his) if his else None

    m_lo, m_hi = month_bounds(daily, leave)
    y_lo, y_hi = year_bounds(daily, leave)

    return {
        'date_min': iso(d_lo),
        'date_max': iso(d_hi),
        'month_min': m_lo,
        'month_max': m_hi,
        'year_min': y_lo,
        'year_max': y_hi,
    }
