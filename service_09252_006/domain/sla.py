"""评审服务时限（SLA）：案件日历与工作时间计算（纯函数，无 I/O 依赖）。

- 案件日历定义工作日窗口（当地时间）与节假日，计时只累计工作时间；
  日历可由 Python 从 JSON 文件读取（``CaseCalendar.load``）；
- 转交期间以暂停区间 [started_at, ended_at) 记录，累计与推算截止时刻
  时都扣除——转交不消耗时限；
- 优先级决定时限长度（工作秒），超时判定与升级在应用层完成。

所有时刻为带时区的绝对时间；工作窗口按日历时区的墙上时间解释，
DST 等由 zoneinfo 处理。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: 默认优先级时限（工作秒）：P1 最急。
DEFAULT_PRIORITY_LIMITS: dict[str, int] = {
    "P1": 4 * 3600,
    "P2": 8 * 3600,
    "P3": 24 * 3600,
    "P4": 72 * 3600,
}

#: 推算截止时刻时向前扫描的天数上限（防止异常输入导致无限循环）。
MAX_SCAN_DAYS = 3700

#: 暂停区间：(开始, 结束) 绝对时刻。
PauseSpan = tuple[datetime, datetime]


def parse_moment(iso_text: str) -> datetime:
    """解析存储用的 ISO-8601 时刻；缺时区按 UTC 处理。"""
    moment = datetime.fromisoformat(iso_text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _parse_hhmm(text: str) -> int:
    """'09:30' -> 距午夜分钟数。"""
    try:
        hour_s, minute_s = str(text).split(":")
        hour, minute = int(hour_s), int(minute_s)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"无法解析时间: {text!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"非法时间: {text!r}")
    return hour * 60 + minute


def _weekday_index(key) -> int:
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        idx = int(key)
        if 0 <= idx <= 6:
            return idx
        raise ValueError(f"未知星期: {key!r}")
    lowered = str(key).strip().lower()[:3]
    if lowered in WEEKDAY_KEYS:
        return WEEKDAY_KEYS.index(lowered)
    raise ValueError(f"未知星期: {key!r}")


def _subtract(lo: datetime, hi: datetime, spans: Iterable[PauseSpan]) -> list[PauseSpan]:
    """从 [lo, hi) 中扣除暂停区间，返回剩余的有效段（spans 需按开始排序）。"""
    segments: list[PauseSpan] = []
    cursor = lo
    for ps, pe in spans:
        if pe <= cursor or ps >= hi:
            continue
        if ps > cursor:
            segments.append((cursor, min(ps, hi)))
        cursor = max(cursor, pe)
        if cursor >= hi:
            return segments
    if cursor < hi:
        segments.append((cursor, hi))
    return segments


@dataclass(frozen=True)
class CaseCalendar:
    """案件日历：工作窗口（本地墙上时间，分钟表示）+ 节假日 + 优先级时限。"""

    timezone: str = "Asia/Shanghai"
    work_windows: Mapping[int, tuple[tuple[int, int], ...]] = field(
        default_factory=lambda: {i: ((9 * 60, 18 * 60),) for i in range(5)}
    )
    holidays: frozenset[date] = frozenset()
    priority_limits: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_PRIORITY_LIMITS)
    )

    # ------------------------------------------------------------ 构造
    @classmethod
    def default(cls) -> "CaseCalendar":
        return cls()

    @classmethod
    def from_dict(cls, data: dict) -> "CaseCalendar":
        tz_name = data.get("timezone", "Asia/Shanghai")
        try:
            ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区: {tz_name}") from exc

        windows: dict[int, tuple[tuple[int, int], ...]] = {}
        for key, value in (data.get("work_windows") or {}).items():
            idx = _weekday_index(key)
            spans = []
            for item in value:
                begin, end = _parse_hhmm(item[0]), _parse_hhmm(item[1])
                if end <= begin:
                    raise ValueError(f"工作窗口结束必须晚于开始: {item!r}")
                spans.append((begin, end))
            windows[idx] = tuple(sorted(spans))

        holidays = frozenset(
            date.fromisoformat(str(d)) for d in (data.get("holidays") or [])
        )

        limits = dict(DEFAULT_PRIORITY_LIMITS)
        for prio, hours in (data.get("priority_limits_hours") or {}).items():
            seconds = int(float(hours) * 3600)
            if seconds <= 0:
                raise ValueError(f"优先级时限必须为正: {prio!r}")
            limits[str(prio)] = seconds

        return cls(
            timezone=tz_name,
            work_windows=windows,
            holidays=holidays,
            priority_limits=limits,
        )

    @classmethod
    def load(cls, path: str | Path) -> "CaseCalendar":
        """从 JSON 文件读取案件日历。

        格式::

            {
              "timezone": "Asia/Shanghai",
              "work_windows": {"mon": [["09:00", "18:00"]], ...},
              "holidays": ["2026-10-01"],
              "priority_limits_hours": {"P1": 4, "P2": 8}
            }
        """
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    # ------------------------------------------------------------ 查询
    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def limit_seconds(self, priority: str) -> int:
        try:
            return int(self.priority_limits[priority])
        except KeyError as exc:
            raise ValueError(f"未知优先级: {priority}") from exc

    def _windows_for(self, day: date) -> tuple[tuple[int, int], ...]:
        if day in self.holidays:
            return ()
        return tuple(self.work_windows.get(day.weekday(), ()))

    # ------------------------------------------------------------ 计时
    def business_seconds_between(self, start: datetime, end: datetime) -> float:
        """[start, end) 内落在工作窗口中的秒数（绝对时长）。"""
        start = _aware(start).astimezone(self.tzinfo)
        end = _aware(end).astimezone(self.tzinfo)
        if end <= start:
            return 0.0
        total = 0.0
        day = start.date()
        last = end.date()
        for _ in range(MAX_SCAN_DAYS):
            if day > last:
                break
            for begin_min, end_min in self._windows_for(day):
                ws = datetime.combine(day, time(begin_min // 60, begin_min % 60), self.tzinfo)
                we = datetime.combine(day, time(end_min // 60, end_min % 60), self.tzinfo)
                lo = max(ws, start)
                hi = min(we, end)
                if hi > lo:
                    total += (hi - lo).total_seconds()
            day += timedelta(days=1)
        return total

    def elapsed_seconds(
        self, opened: datetime, now: datetime, pauses: Iterable[PauseSpan]
    ) -> float:
        """从 opened 到 now 的有效工作秒数：工作时长减去暂停区间内的工作时长。"""
        opened = _aware(opened)
        now = _aware(now)
        if now <= opened:
            return 0.0
        total = self.business_seconds_between(opened, now)
        for ps, pe in pauses:
            lo = max(_aware(ps), opened)
            hi = min(_aware(pe), now)
            if hi > lo:
                total -= self.business_seconds_between(lo, hi)
        return max(total, 0.0)

    def due_at(
        self, opened: datetime, pauses: Iterable[PauseSpan], limit_seconds: float
    ) -> datetime | None:
        """从 opened 起累计满 limit_seconds 工作秒（扣除暂停）的绝对时刻（UTC）。

        在扫描上限内无法累计满时返回 None。
        """
        tz = self.tzinfo
        cursor = _aware(opened).astimezone(tz)
        spans = sorted(
            (
                (max(_aware(ps), cursor), _aware(pe))
                for ps, pe in pauses
                if _aware(pe) > cursor
            ),
            key=lambda s: s[0],
        )
        remaining = float(limit_seconds)
        for _ in range(MAX_SCAN_DAYS):
            day = cursor.date()
            for begin_min, end_min in self._windows_for(day):
                ws = datetime.combine(day, time(begin_min // 60, begin_min % 60), tz)
                we = datetime.combine(day, time(end_min // 60, end_min % 60), tz)
                if we <= cursor:
                    continue
                for seg_lo, seg_hi in _subtract(max(ws, cursor), we, spans):
                    secs = (seg_hi - seg_lo).total_seconds()
                    if secs >= remaining:
                        return (seg_lo + timedelta(seconds=remaining)).astimezone(
                            timezone.utc
                        )
                    remaining -= secs
            cursor = datetime.combine(day + timedelta(days=1), time(0, 0), tz)
        return None
