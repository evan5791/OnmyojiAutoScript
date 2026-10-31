"""Small scheduler-shell hooks; importing this module never constructs a device."""

from datetime import datetime, timedelta
from datetime import datetime as _DateTime

from module.exception import (
    EmulatorNotRunningError, GameBugError, GameNotRunningError, GamePageUnknownError,
    GameStuckError, GameTooManyClickError, RequestHumanTakeover, ScriptEnd, ScriptError, TaskEnd,
)
from tasks.AccountSwitch.routine import arm_future, finish, next_appointment, next_due, window


def _block(config):
    return getattr(config, 'account_switch', None)


def _log(logger, message):
    if logger is not None:
        logger.info(message)


def _routine_enabled(block):
    scheduler = getattr(block, 'scheduler', None)
    settings = getattr(block, 'account_switch_config', None)
    return bool(scheduler is not None and getattr(scheduler, 'enable', False) and settings is not None
                and (getattr(settings, 'routine_enable', False) or getattr(settings, 'ios_daily_enable', False)))


def _runtime_schedule(block):
    schedule = getattr(getattr(block, 'runtime', None), 'schedule', None)
    return schedule if isinstance(schedule, dict) else None


def defer_dedicated_restart(config, next_run=None, now=None, logger=None) -> bool:
    """Keep Restart available for recovery without its own earlier alt-only run.

    Callers persist this change together with the AccountSwitch appointment.
    Unknown/incomplete task inventories deliberately do not qualify.
    """
    block = _block(config)
    if not _routine_enabled(block):
        return False
    model = getattr(config, 'model', None)
    if model is None:
        return False
    dump = getattr(model, 'model_dump', None) or getattr(model, 'dict', None)
    entries = dump() if callable(dump) else getattr(model, '__dict__', None)
    if not isinstance(entries, dict):
        return False
    enabled = set()
    for name, task in entries.items():
        scheduler = task.get('scheduler') if isinstance(task, dict) else getattr(task, 'scheduler', None)
        active = scheduler.get('enable') if isinstance(scheduler, dict) else getattr(scheduler, 'enable', None)
        if active is True:
            enabled.add(name.replace('_', '').casefold())
    if enabled != {'accountswitch', 'restart'}:
        return False
    restart = getattr(config, 'restart', None) or getattr(model, 'restart', None)
    scheduler = getattr(restart, 'scheduler', None)
    if scheduler is None or not getattr(scheduler, 'enable', False):
        return False
    target = next_run if next_run is not None else block.scheduler.next_run
    if not isinstance(target, _DateTime) or target <= (now or datetime.now()):
        return False
    deferred = target + timedelta(days=1)
    current = getattr(scheduler, 'next_run', None)
    if isinstance(current, _DateTime) and current >= deferred:
        return False
    scheduler.next_run = deferred
    _log(logger, f'小号专用配置保留Restart恢复功能，独立重启计划延至 {deferred.isoformat()}')
    return True


def arm_service(config, now=None, logger=None) -> bool:
    """Arm enabled platforms before get_next_task or any lazy device access."""
    block = _block(config)
    if not _routine_enabled(block):
        return False
    now = now or datetime.now()
    settings = block.account_switch_config
    for platform, enabled in (('android', settings.routine_enable), ('ios', settings.ios_daily_enable)):
        if enabled:
            arm_future(block.runtime, settings, platform, now)
    appointment = next_appointment(block.runtime, settings, now)
    if appointment is None:
        return False
    block.scheduler.next_run = datetime.fromisoformat(appointment['at'])
    defer_dedicated_restart(config, now=now, logger=logger)
    config.save()
    _log(logger, f'小号服务已等待未来计划：{appointment["platform"]} / {appointment["plan"]} / {appointment["at"]}；未启动设备')
    return True


def remember_outcome(config) -> None:
    """Keep the last alt outcome across the shell's Config object reloads."""
    block = _block(config)
    outcome = getattr(config, 'task_runtime_outcome', None)
    schedule = _runtime_schedule(block)
    if schedule is not None and isinstance(outcome, dict) and outcome.get('task') == 'AccountSwitch':
        schedule['outcome'] = dict(outcome)
        config.save()


def clear_outcome(config) -> None:
    """Another task owns its ordinary idle/recovery policy once it begins."""
    block = _block(config)
    schedule = _runtime_schedule(block)
    if schedule is not None and schedule.pop('outcome', None) is not None:
        config.save()
    outcome = getattr(config, 'task_runtime_outcome', None)
    if isinstance(outcome, dict) and outcome.get('task') == 'AccountSwitch':
        config.task_runtime_outcome = None


def preserve_vm(config) -> bool:
    outcome = getattr(config, 'task_runtime_outcome', None)
    if not isinstance(outcome, dict):
        block = _block(config)
        schedule = _runtime_schedule(block)
        outcome = schedule.get('outcome') if schedule is not None else None
        if isinstance(outcome, dict) and outcome.get('task') == 'AccountSwitch':
            config.task_runtime_outcome = dict(outcome)
    return (isinstance(outcome, dict) and outcome.get('task') == 'AccountSwitch'
            and outcome.get('status') != 'completed')


def initial_future_wait(config) -> bool:
    block = _block(config)
    task = getattr(config, 'task', None)
    return bool(_routine_enabled(block) and getattr(task, 'command', None) == 'AccountSwitch')


def recoverable_error(error) -> bool:
    if isinstance(error, (TaskEnd, ScriptEnd, ScriptError, RequestHumanTakeover)):
        return False
    if isinstance(error, (GameNotRunningError, GameStuckError, GameTooManyClickError, GameBugError,
                          GamePageUnknownError, EmulatorNotRunningError, ConnectionError, TimeoutError)):
        return True
    module, name = type(error).__module__, type(error).__name__
    if module.startswith('adbutils') or (module == 'cv2' and name == 'error'):
        return True
    if module.startswith('module.device') and name in ('ImageTruncated', 'NemuIpcError', 'ScrcpyError'):
        return True
    if isinstance(error, (OSError, RuntimeError)):
        text = str(error).casefold()
        return any(marker in text for marker in ('adb ', 'adb:', 'screenshot', 'cannot load screenshot',
                                                '截图', 'device offline', 'error: closed', 'connection reset'))
    return False


def _failure_slot(block, config, now):
    settings, runtime = block.account_switch_config, block.runtime
    outcome = getattr(config, 'task_runtime_outcome', None)
    if isinstance(outcome, dict) and outcome.get('task') == 'AccountSwitch' and outcome.get('status') == 'running':
        platform = outcome.get('platform')
        if platform in ('android', 'ios'):
            try:
                attempted = datetime.fromisoformat(outcome.get('period', ''))
                return platform, window(attempted, platform)[1]
            except (TypeError, ValueError):
                pass
    running = []
    for platform in ('android', 'ios'):
        plan, start, _ = window(now, platform)
        if runtime.schedule.get(f'sample:{plan}:{start.date().isoformat()}', {}).get('state') == 'running':
            running.append((platform, start))
    if running:
        return min(running, key=lambda item: item[0] != settings.platform_priority)
    if settings.routine_enable or settings.ios_daily_enable:
        due = next_due(runtime, settings, now)
        if due is not None:
            platform = due['platform']
            return platform, window(datetime.fromisoformat(due['at']), platform)[1]
    platform = ('ios' if settings.ios_daily_enable and not settings.routine_enable
                else 'android' if settings.apple_or_android else 'ios')
    return platform, window(now, platform)[1]


def handle_failure(config, error, now=None, logger=None) -> bool:
    """Report an alt-only recoverable failure without generic game restarts."""
    block = _block(config)
    if block is None or not recoverable_error(error):
        return False
    if _runtime_schedule(block) is None:
        # Old pure-switch proxies do not own a persistent routine/scheduler.
        # Preserve their outcome without inventing or accessing hidden state.
        config.task_runtime_outcome = {
            'task': 'AccountSwitch', 'status': 'account_switch_failed',
            'reason': str(error) or type(error).__name__, 'preserve_vm': True,
        }
        _log(logger, f'手动小号切换未完成，保留模拟器与当前画面：{error}')
        return True
    now = now or datetime.now()
    platform, attempted = _failure_slot(block, config, now)
    appointment = finish(block.runtime, block.account_switch_config, platform, now,
                         failed=True, attempted=attempted)
    if block.account_switch_config.routine_enable or block.account_switch_config.ios_daily_enable:
        appointment = next_appointment(block.runtime, block.account_switch_config, now) or appointment
    block.scheduler.next_run = datetime.fromisoformat(appointment['at'])
    defer_dedicated_restart(config, now=now, logger=logger)
    config.task_runtime_outcome = {
        'task': 'AccountSwitch', 'status': 'account_routine_pending', 'platform': platform,
        'period': attempted.isoformat(), 'reason': str(error) or type(error).__name__,
        'error_type': type(error).__name__, 'retry_at': appointment['at'], 'preserve_vm': True,
    }
    remember_outcome(config)
    _log(logger, f'小号任务未完成，保留模拟器与当前画面：{type(error).__name__}: {error}；下次检查 {appointment["at"]}')
    return True
