"""评审服务时限的纯领域逻辑：按案件日历计算工作时间与时限截止点。

计时不是 7×24：只累计案件日历定义的工作时段（机构当地时区的
work_start–work_end、working_weekdays 工作日、且不在 holidays 节假日内）。
这样跨周末、节假日与跨时区的转交都不会“偷偷”消耗时限预算。

所有比较与返回均使用带时区的 UTC 绝对时刻；日历的墙上时间仅在切分
工作日区间时换算，DST 由 zoneinfo 按规则处理。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .enums import CasePriority
from .models import CaseCalendar

# 各优先级的默认时限预算（工作小时）：仅消耗工作时间
DEFAULT_SLA_BUDGETS: dict[str, float] = {
    CasePriority.URGENT.value: 4.0,
    CasePriority.HIGH.value: 8.0,
    CasePriority.NORMAL.value: 24.0,
    CasePriority.LOW.value: 48.0,
}

# 向前排期的安全上限（约 20 年），避免日历配置异常时死循环
_MAX_DAYS_AHEAD = 366 * 20


def budget_seconds_for(priority: str, overrides: dict[str, float] | None = None) -> int:
    """返回某优先级的时限预算（工作秒）。overrides 以小时为单位覆盖默认值。"""
    hours: float | None = None
    if overrides is not None:
        override = overrides.get(priority)
        if override is not None:
            hours = override
    if hours is None:
        if priority not in DEFAULT_SLA_BUDGETS:
            raise ValueError(f"未知案件优先级: {priority}")
        hours = DEFAULT_SLA_BUDGETS[priority]
    if hours < 0:
        raise ValueError("时限预算不能为负")
    return int(round(hours * 3600))


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_hm(text: str) -> tuple[int, int]:
    hour_str, _, minute_str = text.partition(":")
    hour, minute = int(hour_str), int(minute_str or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"非法时刻: {text}")
    return hour, minute


def _day_window_utc(
    cal: CaseCalendar, tz, day: date
) -> tuple[datetime, datetime] | None:
    """返回某一当地日期的工作时段对应的 UTC 区间；非工作日返回 None。"""
    if day.isoweekday() not in cal.working_weekdays:
        return None
    if day.isoformat() in cal.holidays:
        return None
    sh, sm = _parse_hm(cal.work_start)
    eh, em = _parse_hm(cal.work_end)
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
    window = (start.astimezone(timezone.utc), end.astimezone(timezone.utc))
    if window[1] <= window[0]:
        raise ValueError("工作结束时刻必须晚于开始时刻")
    return window


def working_seconds_between(
    start: str | datetime,
    end: str | datetime,
    calendar: CaseCalendar,
) -> float:
    """两个 UTC 时刻之间按案件日历流逝的工作秒数。

    逐日切分当地工作日的工作时段并与 [start, end] 求交，因此暂停区间
    （两端点之间夹着的非工作时间/转交窗口）天然不计入。
    """
    s = parse_utc(start)
    e = parse_utc(end)
    if e <= s:
        return 0.0
    tz = ZoneInfo(calendar.timezone)
    total = 0.0
    day = s.astimezone(tz).date()
    last_day = e.astimezone(tz).date()
    while day <= last_day:
        window = _day_window_utc(calendar, tz, day)
        if window is not None:
            seg_start = max(s, window[0])
            seg_end = min(e, window[1])
            if seg_end > seg_start:
                total += (seg_end - seg_start).total_seconds()
        day += timedelta(days=1)
    return total


def compute_due_at(
    start: str | datetime,
    budget_seconds: float,
    calendar: CaseCalendar,
) -> datetime:
    """从 start 起消耗 budget_seconds 个工作秒，返回到期的 UTC 绝对时刻。

    预算为 0 时，到期点即 start（与“恰好到期”的判定一致）。
    """
    s = parse_utc(start)
    if budget_seconds <= 0:
        return s
    tz = ZoneInfo(calendar.timezone)
    remaining = float(budget_seconds)
    day = s.astimezone(tz).date()
    for _ in range(_MAX_DAYS_AHEAD):
        window = _day_window_utc(calendar, tz, day)
        if window is not None and window[1] > s:
            seg_start = max(s, window[0])
            available = (window[1] - seg_start).total_seconds()
            if remaining <= available:
                return seg_start + timedelta(seconds=remaining)
            remaining -= available
        day += timedelta(days=1)
    raise RuntimeError("在安全上限内未能排满工作时段，请检查案件日历配置")


def elapsed_work_seconds(
    consumed_seconds: float,
    segment_started_at: str | datetime | None,
    now: str | datetime,
    calendar: CaseCalendar,
) -> float:
    """已结算秒数 + 当前连续计时段自起点按日历流逝的工作秒数。

    暂停的计时器 segment_started_at 为 None，只返回此前结算值，
    因而转交（暂停）期间不会继续累加。
    """
    total = float(consumed_seconds)
    if segment_started_at is not None:
        total += working_seconds_between(segment_started_at, now, calendar)
    return total
