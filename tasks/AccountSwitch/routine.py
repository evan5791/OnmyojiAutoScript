"""Local-time refresh periods and scheduling for the alt-account routine."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import random
from threading import RLock


_lock = RLock()
PLANS = ('daytime', 'evening', 'ios_daily')


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


def window(now: datetime, platform: str = 'android') -> tuple[str, datetime, datetime]:
    """Android jade refreshes at 06/18; iOS daily work resets at midnight."""
    if platform == 'android':
        period = refresh_period(now)
        return ('evening' if period.evening else 'daytime'), period.start, period.end
    if platform != 'ios':
        raise ValueError('平台须为 android 或 ios')
    start = datetime.combine(now.date(), time())
    return 'ios_daily', start, start + timedelta(days=1)


def _bounds(plan, day):
    if plan not in PLANS:
        raise ValueError('未知小号日程')
    if isinstance(day, str):
        day = date.fromisoformat(day)
    start = datetime.combine(day, time(0 if plan == 'ios_daily' else 18 if plan == 'evening' else 6))
    return start, start + timedelta(hours=24 if plan == 'ios_daily' else 12)


def _schedule(runtime):
    return runtime.setdefault('schedule', {}) if isinstance(runtime, dict) else runtime.schedule


def _sample_key(plan, day):
    return f'sample:{plan}:{day.isoformat() if isinstance(day, date) else day}'


def _clock(settings, plan):
    value = getattr(settings, {'daytime': 'daytime_run_time', 'evening': 'evening_run_time',
                               'ios_daily': 'ios_daily_run_time'}[plan])
    return time.fromisoformat(value) if isinstance(value, str) else value


def _parse(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def sample(runtime, settings, plan: str, day) -> dict:
    """Persist one bounded jitter draw per plan/day; caller saves the config."""
    if plan not in PLANS:
        raise ValueError('未知小号日程')
    if isinstance(day, str):
        day = date.fromisoformat(day)
    with _lock:
        records = _schedule(runtime)
        key = _sample_key(plan, day)
        radius = getattr(settings, plan + '_jitter_minutes')
        selected = _clock(settings, plan)
        signature = [selected.isoformat(), radius]
        value = records.get(key)
        if (not isinstance(value, dict) or value.get('signature') != signature
                or _parse(value.get('at')) is None):
            start, end = _bounds(plan, day)
            nominal = datetime.combine(day, selected)
            offset = random.randint(-radius * 60, radius * 60) if radius else 0
            at = min(max(nominal + timedelta(seconds=offset), start), end - timedelta(seconds=1))
            value = {'signature': signature, 'at': at.isoformat(), 'state': 'pending'}
            records[key] = value
        return dict(value)


def _plans(platform):
    if platform not in ('android', 'ios'):
        raise ValueError('平台须为 android 或 ios')
    return ('ios_daily',) if platform == 'ios' else ('daytime', 'evening')


def arm_future(runtime, settings, platform: str, now: datetime) -> dict:
    """An enabled/restarted service never automatically catches up old slots.

    Account progress and previously drawn clocks are retained. Only past
    pending work and old retries are disarmed; manual work may still use its
    saved per-account checkpoints without consuming a scheduled round.
    """
    with _lock:
        records = _schedule(runtime)
        _, start, _ = window(now, platform)
        records['service:' + platform] = {'armed_at': now.isoformat()}
        records[platform] = {}
        for plan in _plans(platform):
            key = _sample_key(plan, start.date())
            value = sample(runtime, settings, plan, start.date())
            if value.get('state') not in ('done', 'running'):
                if _parse(value['at']) <= now:
                    value.update(state='waiting_future', wait_reason='service_started_after_appointment',
                                 skipped_at=now.isoformat())
                else:
                    value['state'] = 'pending'
                    for name in ('pause_reason', 'paused_at', 'wait_reason', 'skipped_at'):
                        value.pop(name, None)
                records[key] = value
        return resolve(runtime, settings, platform, now)


def resolve(runtime, settings, platform: str, now: datetime) -> dict:
    """Resolve a regular appointment or its separate within-window retry."""
    with _lock:
        plans = _plans(platform)
        records = _schedule(runtime)
        if 'service:' + platform not in records:
            return arm_future(runtime, settings, platform, now)
        active, start, end = window(now, platform)
        meta = records.get(platform, {})
        retry = _parse(meta.get('at')) if isinstance(meta, dict) and meta.get('kind') == 'retry' else None
        if (retry is not None and meta.get('plan') == active and meta.get('day') == start.date().isoformat()
                and start <= retry < end):
            return dict(meta)
        candidates = []
        for offset in (0, 1, 2):
            day = start.date() + timedelta(days=offset)
            for plan in plans:
                _, upper = _bounds(plan, day)
                if upper <= now:
                    continue
                value = sample(runtime, settings, plan, day)
                if value.get('state') not in ('done', 'running', 'paused', 'waiting_future'):
                    candidates.append({'kind': 'plan', 'plan': plan, 'day': day.isoformat(), 'at': value['at']})
        if not candidates:
            raise ValueError('未来三日没有可执行的小号日程')
        chosen = min(candidates, key=lambda value: value['at'])
        records[platform] = chosen
        return dict(chosen)


def begin(runtime, settings, platform: str, now: datetime, *, attempted: datetime | None = None) -> dict:
    """Claim a whole scheduled batch; selected-account/manual runs skip this."""
    with _lock:
        plan, start, end = window(attempted or now, platform)
        if not start <= now < end:
            raise ValueError('计划已跨悬赏刷新边界，不消费下一时段日程')
        records = _schedule(runtime)
        value = sample(runtime, settings, plan, start.date())
        value['state'] = 'running'
        records[_sample_key(plan, start.date())] = value
        records['service:' + platform] = records.get('service:' + platform, {'armed_at': now.isoformat()})
        records[platform] = {}
        return resolve(runtime, settings, platform, now)


def finish(runtime, settings, platform: str, now: datetime, *, failed=False, cancelled=False,
           attempted: datetime | None = None) -> dict:
    with _lock:
        plan, start, end = window(attempted or now, platform)
        records = _schedule(runtime)
        value = sample(runtime, settings, plan, start.date())
        retry = now + timedelta(minutes=settings.retry_minutes)
        may_retry = failed and not cancelled and start <= retry < end
        value['state'] = 'paused' if cancelled else ('pending' if may_retry else 'done')
        if cancelled:
            value.update(pause_reason='user_cancelled_batch', paused_at=now.isoformat())
        records[_sample_key(plan, start.date())] = value
        records['service:' + platform] = records.get('service:' + platform, {'armed_at': now.isoformat()})
        records[platform] = ({'kind': 'retry', 'plan': plan, 'day': start.date().isoformat(),
                              'at': retry.isoformat()} if may_retry else {})
        return resolve(runtime, settings, platform, now)


def next_appointment(runtime, settings, now: datetime) -> dict | None:
    """Earlier real appointments win; priority only breaks an exact tie."""
    candidates = []
    for platform, enabled in (('android', settings.routine_enable), ('ios', settings.ios_daily_enable)):
        if enabled:
            candidates.append({'platform': platform, **resolve(runtime, settings, platform, now)})
    if not candidates:
        return None
    return min(candidates, key=lambda value: (value['at'], value['platform'] != settings.platform_priority))


def next_due(runtime, settings, now: datetime) -> dict | None:
    candidates = []
    for platform, enabled in (('android', settings.routine_enable), ('ios', settings.ios_daily_enable)):
        if enabled:
            value = {'platform': platform, **resolve(runtime, settings, platform, now)}
            if _parse(value['at']) <= now:
                candidates.append(value)
    return min(candidates, key=lambda value: (value['platform'] != settings.platform_priority, value['at'])) if candidates else None
