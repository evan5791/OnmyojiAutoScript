"""Local-time refresh periods and scheduling for the alt-account routine."""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class RoutinePeriod:
    start: datetime
    end: datetime
    evening: bool

    @property
    def day(self) -> str:
        return self.start.date().isoformat()

    def key(self, receiver: str) -> str:
        return f'{self.start.isoformat()}|{receiver}'


def refresh_period(now: datetime) -> RoutinePeriod:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour < 6:
        return RoutinePeriod(today - timedelta(hours=6), today + timedelta(hours=6), True)
    if now.hour < 18:
        return RoutinePeriod(today + timedelta(hours=6), today + timedelta(hours=18), False)
    return RoutinePeriod(today + timedelta(hours=18), today + timedelta(days=1, hours=6), True)


def period_run_time(period: RoutinePeriod, settings) -> datetime:
    run_time = settings.evening_run_time if period.evening else settings.daytime_run_time
    return datetime.combine(period.start.date(), run_time)


def next_scheduled_run(now: datetime, settings) -> datetime:
    candidates = [
        datetime.combine(now.date() + timedelta(days=offset), run_time)
        for offset in (0, 1)
        for run_time in (settings.daytime_run_time, settings.evening_run_time)
    ]
    return min(candidate for candidate in candidates if candidate > now)


def next_routine_run(now: datetime, settings, period: RoutinePeriod, retry: bool) -> datetime:
    target = next_scheduled_run(now, settings)
    if retry:
        retry_at = now + timedelta(minutes=settings.retry_minutes)
        if retry_at < period.end:
            target = min(target, retry_at)
    return target
