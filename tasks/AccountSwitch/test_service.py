"""Execute the real shell hooks with stubs, without importing Device or OCR."""

import ast
from datetime import date, datetime, timedelta
from functools import cached_property
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from module import exception as errors
from tasks.AccountSwitch.config import AccountSwitch
from tasks.AccountSwitch import service
from tasks.AccountSwitch.routine import begin


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 31, 10)


class StopLoop(BaseException):
    pass


def shell_type():
    """Compile exactly the changed shell methods, with external services stubbed."""
    source = Path(__file__).resolve().parents[2] / 'script.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Script')
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef)
               and node.name in ('run', 'loop', '_handle_wait_during_idle', '_wait_stay_there', '_try_acquire_queue_token')]
    namespace = {name: value for name, value in vars(errors).items() if not name.startswith('_')}
    namespace.update(logger=Mock(), account_service=service, datetime=FixedDateTime, date=date,
                     timedelta=timedelta, Path=Path, IS_WINDOWS=False, _log_switch_lock=threading.Lock(),
                     load_module=Mock(), I18n=SimpleNamespace(trans_zh_cn=lambda value: value),
                     del_cached_property=Mock(), Device=Mock(side_effect=AssertionError('must not construct Device')),
                     inflection=SimpleNamespace(camelize=lambda value: value), time=SimpleNamespace(sleep=Mock()))
    module = ast.Module(body=[ast.ClassDef(name='Shell', bases=[], keywords=[], body=methods, decorator_list=[])],
                        type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    shell = namespace['Shell']
    def device(instance):
        return namespace['Device'](instance.config)
    prop = cached_property(device)
    prop.__set_name__(shell, 'device')
    shell.device = prop
    return shell, namespace


def fake_config(*, routine=True, ios=False, scheduler=True):
    block = AccountSwitch(scheduler={'enable': scheduler}, account_switch_config={
        'accounts': 'one@163.com', 'routine_enable': routine, 'ios_daily_enable': ios,
        'daytime_jitter_minutes': 0, 'evening_jitter_minutes': 0, 'ios_daily_jitter_minutes': 0,
    })
    return SimpleNamespace(
        account_switch=block, save=Mock(), task_call=Mock(), notifier=Mock(),
        model=SimpleNamespace(running_task=''),
        task=SimpleNamespace(command='AccountSwitch'),
        script=SimpleNamespace(device=SimpleNamespace(run_background_only=True),
                               optimization=SimpleNamespace(when_task_queue_empty='close_game')),
        other_task=SimpleNamespace(next_run=datetime(2026, 10, 31, 12)),
    )


class ServicePolicyTests(unittest.TestCase):
    def test_dedicated_profile_defers_enabled_restart_after_actual_future_alt_time(self):
        config = fake_config()
        config.restart = SimpleNamespace(scheduler=SimpleNamespace(enable=True, next_run=datetime(2026, 10, 31, 9)))
        config.model.account_switch = config.account_switch
        config.model.restart = config.restart
        self.assertTrue(service.arm_service(config, datetime(2026, 10, 31, 10)))
        self.assertEqual(config.account_switch.scheduler.next_run, datetime(2026, 10, 31, 17))
        self.assertEqual(config.restart.scheduler.next_run, datetime(2026, 11, 1, 17))
        self.assertTrue(config.restart.scheduler.enable)
        # Error retries retain recovery availability and keep the retry first.
        begin(config.account_switch.runtime, config.account_switch.account_switch_config,
              'android', datetime(2026, 10, 31, 17))
        service.handle_failure(config, ConnectionError('offline'), datetime(2026, 10, 31, 17, 2))
        self.assertEqual(config.account_switch.scheduler.next_run, datetime(2026, 10, 31, 17, 12))
        self.assertEqual(config.restart.scheduler.next_run, datetime(2026, 11, 1, 17, 12))

    def test_restart_deferral_does_not_enable_restart_or_change_mixed_or_manual_profiles(self):
        for mode in ('restart_disabled', 'mixed', 'manual', 'alt_disabled', 'already_later'):
            with self.subTest(mode=mode):
                config = fake_config(routine=mode != 'manual', scheduler=mode != 'alt_disabled')
                original = datetime(2026, 11, 4, 9) if mode == 'already_later' else datetime(2026, 10, 31, 9)
                config.restart = SimpleNamespace(scheduler=SimpleNamespace(
                    enable=mode != 'restart_disabled', next_run=original))
                config.model.account_switch = config.account_switch
                config.model.restart = config.restart
                if mode == 'mixed':
                    config.model.daily_trifles = SimpleNamespace(scheduler=SimpleNamespace(enable=True))
                service.arm_service(config, datetime(2026, 10, 31, 10))
                self.assertEqual(config.restart.scheduler.next_run, original)
                self.assertEqual(config.restart.scheduler.enable, mode != 'restart_disabled')

    def test_old_pure_switch_namespace_needs_no_runtime_scheduler_or_save_method(self):
        config = SimpleNamespace(account_switch=SimpleNamespace(account_switch_config=SimpleNamespace(
            routine_enable=False, ios_daily_enable=False)))
        self.assertFalse(service.arm_service(config, datetime(2026, 10, 31, 21)))
        self.assertFalse(service.preserve_vm(config))
        config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'completed'}
        service.remember_outcome(config)
        self.assertFalse(service.preserve_vm(config))
        self.assertTrue(service.handle_failure(config, errors.GamePageUnknownError('page')))
        self.assertTrue(service.preserve_vm(config))
        self.assertFalse(hasattr(config.account_switch, 'runtime'))
        service.clear_outcome(config)
        self.assertFalse(service.preserve_vm(config))

    def test_arm_sets_only_alt_future_schedule_and_does_not_touch_other_task(self):
        config = fake_config(ios=True)
        before = config.other_task.next_run
        self.assertTrue(service.arm_service(config, datetime(2026, 10, 31, 21)))
        self.assertEqual(config.account_switch.scheduler.next_run, datetime(2026, 11, 1, 17))
        self.assertEqual(config.other_task.next_run, before)
        config.save.assert_called_once_with()

    def test_disabled_scheduler_and_legacy_manual_switch_are_not_rearmed(self):
        for kwargs in ({'scheduler': False}, {'routine': False}):
            config = fake_config(**kwargs)
            before = config.account_switch.scheduler.next_run
            self.assertFalse(service.arm_service(config, datetime(2026, 10, 31, 21)))
            self.assertEqual(config.account_switch.scheduler.next_run, before)
            config.save.assert_not_called()

    def test_pending_outcome_survives_config_reopen_and_success_releases_protection(self):
        config = fake_config()
        config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'account_routine_pending', 'reason': 'offline'}
        service.remember_outcome(config)
        reopened = fake_config()
        reopened.account_switch = AccountSwitch.model_validate(config.account_switch.model_dump(mode='json'))
        self.assertTrue(service.preserve_vm(reopened))
        self.assertEqual(reopened.task_runtime_outcome['reason'], 'offline')
        reopened.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'completed'}
        service.remember_outcome(reopened)
        self.assertFalse(service.preserve_vm(reopened))

    def test_other_task_clears_only_alt_idle_protection(self):
        config = fake_config()
        config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'error'}
        service.remember_outcome(config)
        service.clear_outcome(config)
        self.assertFalse(service.preserve_vm(config))
        self.assertNotIn('outcome', config.account_switch.runtime.schedule)
        self.assertEqual(config.other_task.next_run, datetime(2026, 10, 31, 12))

    def test_known_error_retries_within_owned_window_and_keeps_partial_progress(self):
        config = fake_config()
        service.arm_service(config, datetime(2026, 10, 31, 10))
        begin(config.account_switch.runtime, config.account_switch.account_switch_config, 'android', datetime(2026, 10, 31, 17))
        config.account_switch.runtime.accounts['one|android'] = {'awakening_completed': 5}
        self.assertTrue(service.handle_failure(config, errors.GamePageUnknownError('page not ready'), datetime(2026, 10, 31, 17, 2)))
        self.assertEqual(config.account_switch.scheduler.next_run, datetime(2026, 10, 31, 17, 12))
        self.assertTrue(service.preserve_vm(config))
        self.assertEqual(config.task_runtime_outcome['platform'], 'android')
        self.assertEqual(config.account_switch.runtime.accounts['one|android']['awakening_completed'], 5)

    def test_late_error_never_puts_retry_in_the_next_refresh_window(self):
        config = fake_config()
        service.arm_service(config, datetime(2026, 10, 31, 10))
        config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'running', 'platform': 'android',
                                       'period': '2026-10-31T06:00:00'}
        service.handle_failure(config, ConnectionError('offline'), datetime(2026, 10, 31, 17, 55))
        self.assertEqual(config.account_switch.scheduler.next_run, datetime(2026, 10, 31, 20))

    def test_stop_human_takeover_and_programming_errors_are_not_swallowed(self):
        for error in (SystemExit(0), KeyboardInterrupt(), errors.TaskEnd(), errors.ScriptEnd(),
                      errors.RequestHumanTakeover(), errors.ScriptError('configuration'), ValueError('bug')):
            with self.subTest(error=type(error).__name__):
                config = fake_config()
                self.assertFalse(service.handle_failure(config, error, datetime(2026, 10, 31, 17)))
                config.save.assert_not_called()

    def test_adb_and_screenshot_classification_has_no_device_dependency(self):
        adb_error = type('AdbError', (Exception,), {'__module__': 'adbutils.errors'})('closed')
        image_error = type('ImageTruncated', (Exception,), {'__module__': 'module.device.method.utils'})('PNG')
        self.assertTrue(service.recoverable_error(adb_error))
        self.assertTrue(service.recoverable_error(image_error))
        self.assertTrue(service.recoverable_error(OSError('cannot load screenshot')))
        self.assertFalse(service.recoverable_error(FileNotFoundError('missing configuration')))


class ShellHookTests(unittest.TestCase):
    def setUp(self):
        cls, self.namespace = shell_type()
        self.shell = cls()
        self.shell.config = fake_config()
        self.shell.config_name = 'test'
        self.shell._emulator_down = False
        self.shell.instance_guard = None
        self.shell.anti_ban_guard = Mock()
        self.shell._try_acquire_queue_token = Mock(return_value=True)
        self.shell.wait_until = Mock(return_value=True)
        self.shell.exception_handler = Mock()
        self.shell.save_error_log = Mock()

    def load_device(self):
        device = Mock()
        device.package = 'com.netease.onmyoji.test'
        self.shell.__dict__['device'] = device
        return device

    def test_loop_arms_future_before_get_next_and_never_constructs_device(self):
        def get_next_task():
            self.assertGreater(self.shell.config.account_switch.scheduler.next_run, FixedDateTime.now())
            self.assertIn('service:android', self.shell.config.account_switch.runtime.schedule)
            raise StopLoop()
        self.shell.get_next_task = get_next_task
        with patch('tasks.AccountSwitch.service.datetime', FixedDateTime):
            with self.assertRaises(StopLoop):
                self.shell.loop()
        self.namespace['Device'].assert_not_called()

    def test_future_account_wait_does_not_construct_device_to_close_game(self):
        target = datetime(2026, 10, 31, 17)
        self.assertTrue(self.shell._handle_wait_during_idle(target))
        self.shell.wait_until.assert_called_once_with(target)
        self.namespace['Device'].assert_not_called()

    def test_pending_idle_uses_stay_policy_even_with_global_close_game(self):
        device = self.load_device()
        self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'account_routine_pending'}
        target = datetime(2026, 10, 31, 17)
        self.assertTrue(self.shell._handle_wait_during_idle(target))
        device.release_during_wait.assert_called_once_with()
        device.app_stop.assert_not_called(); device.emulator_stop.assert_not_called()

    def test_pending_down_instance_waits_without_preheat_or_device_creation(self):
        self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'interrupted'}
        self.shell._emulator_down = True
        self.assertTrue(self.shell._handle_wait_during_idle(datetime(2026, 10, 31, 17)))
        self.namespace['Device'].assert_not_called()

    def test_other_task_idle_retains_the_original_close_policy(self):
        self.shell.config.task.command = 'Exploration'
        self.shell._wait_close_game = Mock(return_value=True)
        self.shell._wait_goto_main = Mock()
        target = datetime(2026, 10, 31, 17)
        self.assertTrue(self.shell._handle_wait_during_idle(target))
        self.shell._wait_close_game.assert_called_once_with(target)

    def test_alt_screenshot_error_is_handled_without_restart_or_close(self):
        device = self.load_device()
        device.screenshot.side_effect = errors.GamePageUnknownError('page not ready')
        self.assertTrue(self.shell.run('AccountSwitch'))
        self.assertTrue(service.preserve_vm(self.shell.config))
        self.shell.config.task_call.assert_not_called()
        device.app_stop.assert_not_called(); device.emulator_stop.assert_not_called()
        device.sleep.assert_not_called(); self.shell.exception_handler.assert_not_called()
        self.namespace['load_module'].assert_not_called()

    def test_alt_task_adb_error_is_handled_after_initial_screenshot(self):
        device = self.load_device()
        adb_error = type('AdbError', (Exception,), {'__module__': 'adbutils.errors'})('closed')
        worker = Mock(); worker.run.side_effect = adb_error
        self.namespace['load_module'].return_value = SimpleNamespace(ScriptTask=Mock(return_value=worker))
        self.assertTrue(self.shell.run('AccountSwitch'))
        device.screenshot.assert_called_once_with()
        self.assertEqual(self.shell.config.task_runtime_outcome['error_type'], 'AdbError')
        self.shell.config.task_call.assert_not_called(); device.emulator_stop.assert_not_called()

    def test_old_pure_switch_task_end_hook_accepts_namespace_without_runtime(self):
        self.load_device()
        self.shell.config = SimpleNamespace(account_switch=SimpleNamespace(account_switch_config=SimpleNamespace(
            routine_enable=False, ios_daily_enable=False)),
            task_runtime_outcome={'task': 'AccountSwitch', 'status': 'completed'})
        worker = Mock(); worker.run.side_effect = errors.TaskEnd('AccountSwitch')
        self.namespace['load_module'].return_value = SimpleNamespace(ScriptTask=Mock(return_value=worker))
        self.assertTrue(self.shell.run('AccountSwitch'))

    def test_stop_exceptions_propagate_without_mutating_retry_or_closing(self):
        device = self.load_device()
        for error in (SystemExit(0), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                device.screenshot.side_effect = error
                with self.assertRaises(type(error)):
                    self.shell.run('AccountSwitch')
        self.shell.config.save.assert_not_called()
        device.emulator_stop.assert_not_called(); device.app_stop.assert_not_called()

    def test_other_task_uses_existing_generic_game_page_recovery(self):
        device = self.load_device()
        device.screenshot.side_effect = errors.GamePageUnknownError('page not ready')
        self.assertFalse(self.shell.run('Exploration'))
        self.shell.config.task_call.assert_called_once_with('Restart')
        device.sleep.assert_called_once_with(10)
        self.shell.exception_handler.assert_called_once()

    def test_pending_queue_wait_does_not_close_emulator_to_release_resources(self):
        device = self.load_device()
        self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'error'}
        self.shell.config.script.optimization.queue_mode = True
        self.shell.config.start_watching = Mock()
        self.shell.config.should_reload = Mock(return_value=False)
        self.shell.instance_guard = Mock()
        self.shell.instance_guard.try_acquire.side_effect = [False, True]
        del self.shell._try_acquire_queue_token
        self.assertTrue(self.shell._try_acquire_queue_token())
        device.emulator_stop.assert_not_called()

    def test_pending_token_loss_rejoins_queue_without_closing_vm(self):
        device = self.load_device()
        self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'error'}
        self.shell.instance_guard = Mock(token_lost=True)
        self.assertFalse(self.shell.run('AccountSwitch'))
        device.emulator_stop.assert_not_called()
        self.assertFalse(self.shell._emulator_down)
        self.shell.instance_guard.release.assert_called_once_with()

    def test_three_failed_alt_attempts_stop_shell_but_preserve_vm_for_review(self):
        device = self.load_device(); device.is_desktop = False
        self.shell.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'account_routine_pending'}
        self.shell.config.script.error = SimpleNamespace(error_repeated=True, handle_error=True)
        self.shell.get_next_task = Mock(return_value='AccountSwitch')
        self.shell.run = Mock(return_value=False)
        self.shell.is_first_task = False
        self.shell.failure_record = {'AccountSwitch': 2}
        with self.assertRaises(SystemExit), patch('tasks.AccountSwitch.service.datetime', FixedDateTime):
            self.shell.loop()
        device.emulator_stop.assert_not_called()

    def test_three_failed_other_task_attempts_keep_legacy_close_behavior(self):
        device = self.load_device(); device.is_desktop = False
        self.shell.config.script.error = SimpleNamespace(error_repeated=True, handle_error=True)
        self.shell.get_next_task = Mock(return_value='Exploration')
        self.shell.run = Mock(return_value=False)
        self.shell.is_first_task = False
        self.shell.failure_record = {'Exploration': 2}
        with self.assertRaises(SystemExit), patch('tasks.AccountSwitch.service.datetime', FixedDateTime):
            self.shell.loop()
        device.emulator_stop.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
