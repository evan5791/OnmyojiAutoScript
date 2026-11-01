"""Cold account-switch startup without enabling or executing Restart."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from module.exception import GameNotRunningError, RequestHumanTakeover, TaskEnd
from tasks.AccountSwitch import service
from tasks.AccountSwitch.test_service import fake_config, shell_type


class GameStartupTests(unittest.TestCase):
    def setUp(self):
        self.device = Mock(spec=['app_is_running', 'app_start', 'app_stop', 'wait_app_start_ready'])

    def test_closed_game_starts_once_and_waits_for_native_readiness(self):
        self.device.app_is_running.side_effect = [False, True]
        service.ensure_game_started(self.device)
        self.device.app_start.assert_called_once_with()
        self.device.wait_app_start_ready.assert_called_once_with()
        self.device.app_stop.assert_not_called()

    def test_running_game_keeps_its_current_page(self):
        self.device.app_is_running.return_value = True
        service.ensure_game_started(self.device)
        self.device.app_start.assert_not_called()
        self.device.wait_app_start_ready.assert_not_called()

    def test_older_device_without_readiness_method_is_supported(self):
        device = Mock(spec=['app_is_running', 'app_start'])
        device.app_is_running.side_effect = [False, True]
        service.ensure_game_started(device)
        device.app_start.assert_called_once_with()

    def test_foreground_transition_is_allowed_to_finish(self):
        self.device.app_is_running.side_effect = [False, False, True]
        with patch.object(service, 'sleep') as sleep:
            service.ensure_game_started(self.device)
        sleep.assert_called_once_with(0.5)
        self.device.app_start.assert_called_once_with()

    def test_failed_launch_is_recoverable_and_does_not_stop_game(self):
        self.device.app_is_running.return_value = False
        with patch.object(service, 'monotonic', side_effect=[0, 20]):
            with self.assertRaises(GameNotRunningError):
                service.ensure_game_started(self.device)
        self.device.app_stop.assert_not_called()

    def test_app_start_respects_manual_takeover(self):
        self.device.app_is_running.return_value = False
        self.device.app_start.side_effect = RequestHumanTakeover('launch forbidden')
        with self.assertRaisesRegex(RequestHumanTakeover, 'launch forbidden'):
            service.ensure_game_started(self.device)
        self.device.wait_app_start_ready.assert_not_called()

    def test_unavailable_app_state_does_not_launch_blindly(self):
        self.device.app_is_running.side_effect = RequestHumanTakeover('adb disconnected')
        with self.assertRaises(GameNotRunningError):
            service.ensure_game_started(self.device)
        self.device.app_start.assert_not_called()


class StartupShellTests(unittest.TestCase):
    def setUp(self):
        cls, self.namespace = shell_type()
        self.shell = cls()
        self.shell.config = fake_config()
        self.shell.config.restart = SimpleNamespace(scheduler=SimpleNamespace(enable=False))
        self.shell.config_name = 'startup-test'
        self.shell.instance_guard = None
        self.shell._try_acquire_queue_token = Mock(return_value=True)
        self.shell.exception_handler = Mock()
        self.shell.save_error_log = Mock()
        self.device = Mock(spec=['app_is_running', 'app_start', 'screenshot', 'app_stop'])
        self.shell.__dict__['device'] = self.device
        self.worker = Mock()
        self.worker.run.side_effect = TaskEnd()
        self.namespace['load_module'].return_value = SimpleNamespace(ScriptTask=Mock(return_value=self.worker))

    def test_cold_launch_precedes_screenshot_and_account_login(self):
        self.device.app_is_running.side_effect = [False, True]
        events = []
        self.device.app_start.side_effect = lambda: events.append('launch')
        self.device.screenshot.side_effect = lambda: events.append('screenshot')
        def run():
            events.append('account-login')
            raise TaskEnd()
        self.worker.run.side_effect = run
        self.assertTrue(self.shell.run('AccountSwitch'))
        self.assertEqual(events, ['launch', 'screenshot', 'account-login'])
        self.assertFalse(self.shell.config.restart.scheduler.enable)
        self.shell.config.task_call.assert_not_called()
        self.device.app_stop.assert_not_called()

    def test_failed_launch_keeps_checkpoint_without_generic_restart(self):
        self.device.app_is_running.return_value = False
        with patch.object(service, 'monotonic', side_effect=[0, 20]):
            self.assertTrue(self.shell.run('AccountSwitch'))
        self.assertTrue(service.preserve_vm(self.shell.config))
        self.device.screenshot.assert_not_called()
        self.worker.run.assert_not_called()
        self.shell.config.task_call.assert_not_called()
        self.device.app_stop.assert_not_called()

    def test_other_tasks_keep_existing_startup_policy(self):
        self.assertTrue(self.shell.run('Exploration'))
        self.device.app_is_running.assert_not_called()
        self.device.app_start.assert_not_called()

    def test_queue_denial_does_not_launch_game(self):
        self.shell._try_acquire_queue_token.return_value = False
        self.assertFalse(self.shell.run('AccountSwitch'))
        self.device.app_start.assert_not_called()
        self.device.screenshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
