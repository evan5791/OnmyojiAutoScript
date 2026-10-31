"""Opt-in AST checks for newer ScriptRuntimeController shells; no device imports."""

import ast
from datetime import date, datetime, timedelta
from functools import cached_property
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from module import exception as errors
from tasks.AccountSwitch import service
from tasks.AccountSwitch.config import AccountSwitch


class StopLoop(BaseException):
    pass


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 31, 10)


def modern_shell():
    source = Path(os.environ.get('OAS_ACCOUNT_SWITCH_COMPAT_SCRIPT',
                                str(Path(__file__).resolve().parents[2] / 'script.py')))
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Script')
    names = {n.name for n in original.body if isinstance(n, ast.FunctionDef)}
    if '_handle_task_exception' not in names:
        raise unittest.SkipTest('This shell uses the legacy hooks tested in test_service.py')
    selected = {'run', 'loop', 'get_next_task', '_handle_task_exception',
                '_capture_task_runtime_outcome', '_reset_task_runtime_outcome'}
    methods = [n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in selected]
    ns = {name: value for name, value in vars(errors).items() if not name.startswith('_')}
    ns.update(account_service=service, datetime=FixedDateTime, date=date, timedelta=timedelta,
              Path=Path, logger=Mock(), _log_switch_lock=threading.Lock(), IS_WINDOWS=False,
              NetworkRecoveryRequired=type('NetworkRecoveryRequired', (Exception,), {}),
              ScriptRuntimeDecision=SimpleNamespace(READY='ready', RESCHEDULE='reschedule', FAILED='failed'),
              Cooperation=Mock(), RuleScatter=Mock(), set_ocr_logging_enabled=Mock(),
              get_ocr_client=Mock(), load_module=Mock(), prepare_task_cooperation=Mock(), BATTLE_TASKS=set(),
              del_cached_property=Mock(), inflection=SimpleNamespace(camelize=lambda value: value),
              I18n=SimpleNamespace(trans_zh_cn=lambda value: value), time=SimpleNamespace(sleep=Mock()),
              Device=Mock(side_effect=AssertionError('must not initialize Device')))
    module = ast.Module(body=[ast.ClassDef(name='Shell', bases=[], keywords=[], body=methods,
                                         decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), ns)
    shell = ns['Shell']
    prop = cached_property(lambda instance: ns['Device'](instance.config))
    prop.__set_name__(shell, 'device')
    shell.device = prop
    return shell, ns


class ModernShellCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modern_shell()

    def setUp(self):
        cls, self.ns = modern_shell()
        self.shell = cls()
        block = AccountSwitch(scheduler={'enable': True}, account_switch_config={
            'accounts': 'one@163.com', 'routine_enable': True,
            'daytime_jitter_minutes': 0, 'evening_jitter_minutes': 0,
        })
        self.shell.config = SimpleNamespace(
            account_switch=block, save=Mock(), notifier=Mock(), task_call=Mock(),
            model=SimpleNamespace(running_task='', restart=SimpleNamespace(
                restart_config=SimpleNamespace(error_restart_limit=0))),
            script=SimpleNamespace(device=SimpleNamespace(run_background_only=True),
                                   error=SimpleNamespace(error_repeated=True, handle_error=True)),
            global_game=SimpleNamespace(ocr=SimpleNamespace(save_ocr_log=False)),
        )
        self.shell.config_name = 'compat-test'
        self.shell.state_queue = None
        self.shell.runtime = Mock()
        self.shell._emulator_down = False
        self.shell._error_restart_pending = False
        self.shell.resource_precache_enable = False
        self.shell.failure_record = {}
        self.shell.is_first_task = False
        self.shell.last_task_runtime_outcome = None
        self.shell.wait_until = Mock(side_effect=StopLoop())
        self.shell._antiban_wake_time = Mock(return_value=None)
        self.shell._reset_continuous_task_rest = Mock()
        self.shell._handle_continuous_task_rest = Mock(return_value=False)
        self.shell._request_error_restart = Mock()
        self.shell.exception_handler = Mock()
        self.shell.save_error_log = Mock()
        self.addCleanup(patch.stopall)
        patch('tasks.AccountSwitch.service.datetime', FixedDateTime).start()
        patch.dict('sys.modules', {'module.atom.click': SimpleNamespace(RuleClick=Mock())}).start()

    def load_device(self):
        device = Mock()
        self.shell.__dict__['device'] = device
        return device

    def future_task(self, name='AccountSwitch'):
        task = SimpleNamespace(command=name, next_run=datetime(2026, 10, 31, 17))
        self.shell.config.get_next = Mock(return_value=task)
        return task

    def test_loop_arms_future_before_cooperation_runtime_or_device(self):
        def started():
            self.assertEqual(self.shell.config.account_switch.scheduler.next_run,
                             datetime(2026, 10, 31, 17))
            self.assertIn('service:android', self.shell.config.account_switch.runtime.schedule)
            raise StopLoop()
        self.ns['Cooperation'].return_value.start.side_effect = started
        with self.assertRaises(StopLoop):
            self.shell.loop()
        self.shell.runtime.prepare_task_execution.assert_not_called()
        self.ns['Device'].assert_not_called()

    def test_future_first_run_waits_without_runtime_idle_or_device(self):
        task = self.future_task()
        with self.assertRaises(StopLoop):
            self.shell.get_next_task()
        self.shell.wait_until.assert_called_once_with(task.next_run)
        self.shell.runtime.handle_wait_during_idle.assert_not_called()
        self.ns['Device'].assert_not_called()

    def test_pending_outcome_reopens_and_preserves_loaded_page(self):
        self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'account_routine_pending'}
        service.remember_outcome(self.shell.config)
        self.shell.config.task_runtime_outcome = None
        device = self.load_device()
        self.future_task()
        with self.assertRaises(StopLoop):
            self.shell.get_next_task()
        self.assertTrue(service.preserve_vm(self.shell.config))
        self.shell.runtime.handle_wait_during_idle.assert_not_called()
        device.app_stop.assert_not_called()
        device.emulator_stop.assert_not_called()

    def test_pending_offline_instance_waits_without_preheat(self):
        self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'cancelled'}
        self.shell._emulator_down = True
        self.future_task()
        with self.assertRaises(StopLoop):
            self.shell.get_next_task()
        self.shell.runtime.handle_wait_during_idle.assert_not_called()
        self.ns['Device'].assert_not_called()

    def test_other_task_idle_retains_runtime_policy(self):
        task = self.future_task('Exploration')
        self.shell.runtime.handle_wait_during_idle.side_effect = StopLoop()
        with self.assertRaises(StopLoop):
            self.shell.get_next_task()
        self.shell.runtime.handle_wait_during_idle.assert_called_once_with(task.next_run)
        self.shell.wait_until.assert_not_called()

    def test_known_alt_prepare_failure_does_not_request_restart(self):
        self.assertTrue(self.shell._handle_task_exception(errors.GameNotRunningError('offline'), 'AccountSwitch'))
        self.assertTrue(service.preserve_vm(self.shell.config))
        self.shell._request_error_restart.assert_not_called()
        self.shell.config.task_call.assert_not_called()
        self.ns['Device'].assert_not_called()

    def test_other_task_keeps_original_recovery(self):
        self.assertTrue(self.shell._handle_task_exception(errors.GameNotRunningError('offline'), 'Exploration'))
        self.shell._request_error_restart.assert_called_once_with('Exploration')

    def task_runner(self, action):
        runner = Mock()
        runner.run.side_effect = action
        self.ns['load_module'].return_value = SimpleNamespace(ScriptTask=Mock(return_value=runner))

    def test_task_end_and_run_finally_persist_outcome_for_reload(self):
        self.load_device()
        def end():
            self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'account_switch_failed'}
            raise errors.TaskEnd()
        self.task_runner(end)
        self.assertFalse(self.shell.run('AccountSwitch'))
        self.assertEqual(self.shell.last_task_runtime_outcome['status'], 'account_switch_failed')
        self.shell.config.task_runtime_outcome = None
        self.assertTrue(service.preserve_vm(self.shell.config))
        self.assertFalse(self.shell.config.defer_task_courtyard_return)

    def test_cancel_propagates_and_finally_preserves_cancelled_outcome(self):
        device = self.load_device()
        def stop():
            self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'cancelled'}
            raise SystemExit(0)
        self.task_runner(stop)
        with self.assertRaises(SystemExit):
            self.shell.run('AccountSwitch')
        self.shell.config.task_runtime_outcome = None
        self.assertTrue(service.preserve_vm(self.shell.config))
        self.shell._request_error_restart.assert_not_called()
        device.app_stop.assert_not_called()
        device.emulator_stop.assert_not_called()

    def test_known_screenshot_failure_is_handled_and_retains_navigation_policy(self):
        device = self.load_device()
        self.shell.config.defer_task_courtyard_return = True
        device.screenshot.side_effect = errors.GamePageUnknownError('screenshot page')
        self.assertTrue(self.shell.run('AccountSwitch'))
        self.assertTrue(self.shell.config.defer_task_courtyard_return)
        self.assertEqual(self.shell.last_task_runtime_outcome['status'], 'account_routine_pending')
        self.shell._request_error_restart.assert_not_called()
        device.app_stop.assert_not_called()
        device.emulator_stop.assert_not_called()

    def test_other_task_start_clears_alt_protection(self):
        self.load_device()
        self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'cancelled'}
        service.remember_outcome(self.shell.config)
        self.task_runner(errors.TaskEnd())
        self.assertTrue(self.shell.run('Exploration'))
        self.assertFalse(service.preserve_vm(self.shell.config))

    def test_repeated_alt_failure_can_stop_shell_without_stopping_emulator(self):
        device = self.load_device()
        self.shell.get_next_task = Mock(return_value='AccountSwitch')
        self.shell.runtime.prepare_task_execution.return_value = 'ready'
        def failed(command):
            outcome = {'task': 'AccountSwitch', 'status': 'account_switch_failed'}
            self.shell.config.task_runtime_outcome = outcome
            self.shell.last_task_runtime_outcome = outcome
            return False
        self.shell.run = failed
        with self.assertRaises(SystemExit):
            self.shell.loop()
        device.emulator_stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
