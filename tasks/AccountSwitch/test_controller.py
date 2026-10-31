from datetime import datetime
from itertools import chain, repeat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from module.exception import RequestHumanTakeover, TaskEnd
from tasks.AccountSwitch.config import AccountSwitch, AccountSwitchConfig, AccountRoutineProgress, JadePeriodProgress
from tasks.AccountSwitch.flows import AwakeningResult, DailyResult
from tasks.AccountSwitch.script_task import ScriptTask


class NativeControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 31, 20)
        self.settings = AccountSwitchConfig(accounts='one\ntwo', routine_enable=True,
            invite_friend_name='receiver', assist_friend='assist', daytime_jitter_minutes=0,
            evening_jitter_minutes=0, ios_daily_jitter_minutes=0)
        self.model = AccountSwitch(account_switch_config=self.settings)
        self.model.runtime.schedule.update({'service:android': {}, 'service:ios': {}})
        self.task = ScriptTask.__new__(ScriptTask)
        self.task.config = SimpleNamespace(account_switch=self.model, save=Mock())
        self.task.device = object()
        self.task.goto_page, self.task.set_next_run = Mock(), Mock()
        self.events, self.current = [], None
        patches = [patch('tasks.AccountSwitch.script_task.datetime'),
            patch('tasks.AccountSwitch.script_task.SwitchAccount', side_effect=self.switch),
            patch('tasks.AccountSwitch.flows.run_daily', side_effect=self.daily),
            patch('tasks.AccountSwitch.flows.run_awakening', side_effect=self.awake),
            patch('tasks.AccountSwitch.cooperation.prepare_cooperation_assist', side_effect=self.assist),
            patch('tasks.AccountSwitch.jade.JadeCooperationTask'),
            patch('tasks.AccountSwitch.script_task.logger'),
            patch('tasks.AccountSwitch.history.logger')]
        self.clock, self.switch_mock, self.daily_mock, self.awake_mock, self.assist_mock, self.jade_mock, _, _ = [p.start() for p in patches]
        for p in patches: self.addCleanup(p.stop)
        self.clock.now.side_effect = lambda: self.now
        self.jade_mock.return_value.run_jade.return_value = SimpleNamespace(status='no_jade', pending={}, reason='')

    def switch(self, config, device, account):
        def enter():
            self.current = (account.account, 'android' if account.apple_or_android else 'ios')
            self.events.append(('login', *self.current))
            return True
        return SimpleNamespace(switchAccount=enter)

    def daily(self, config, device, record, on_progress, routine_day, **kwargs):
        self.events.append(('daily', *self.current))
        updated = record.model_copy(deep=True)
        updated.pickup_email_dt = self.now
        on_progress(updated)
        return DailyResult(updated, True, (), {'courtyard_affairs': True, 'pickup_email': True})

    def assist(self, config, device, friend):
        self.events.append(('assist', *self.current, friend))
        return SimpleNamespace(friend=friend, normal_used=0, card_used=0)

    def awake(self, config, device, layer, count, on_progress, **kwargs):
        self.events.append(('awake', *self.current, count))
        on_progress(count)
        return AwakeningResult(count, count, True)

    def run_task(self):
        with self.assertRaises(TaskEnd): self.task.run()
        return self.task.config.task_runtime_outcome

    def test_two_due_platforms_run_all_android_then_all_ios(self):
        self.settings.ios_daily_enable = True
        result = self.run_task()
        self.assertEqual([event[1:] for event in self.events if event[0] == 'login'],
                         [('one', 'android'), ('two', 'android'), ('one', 'ios'), ('two', 'ios')])
        self.assertEqual(self.awake_mock.call_count, 2)
        self.assertTrue(all(event[2] == 'android' for event in self.events if event[0] in ('awake', 'assist')))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.model.runtime.accounts['one|ios'].awakening_completed, 0)
        self.assertEqual(self.model.runtime.accounts['one|android'].awakening_script_wins, 14)

    def test_dedicated_controller_finish_keeps_restart_after_next_alt_appointment(self):
        self.model.scheduler.enable = True
        restart = SimpleNamespace(scheduler=SimpleNamespace(enable=True, next_run=datetime(2026, 11, 1, 9)))
        self.task.config.restart = restart
        self.task.config.model = SimpleNamespace(account_switch=self.model, restart=restart)
        result = self.run_task()
        target = self.task.set_next_run.call_args.kwargs['target']
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(target, datetime(2026, 11, 1, 17))
        self.assertEqual(restart.scheduler.next_run, datetime(2026, 11, 2, 17))
        self.assertTrue(restart.scheduler.enable)

    def test_ios_priority_applies_only_after_both_platforms_are_due(self):
        self.settings.ios_daily_enable, self.settings.platform_priority = True, 'ios'
        self.settings.ios_daily_run_time = datetime.strptime('19:30', '%H:%M').time()
        self.run_task()
        self.assertEqual(next(event[2] for event in self.events if event[0] == 'login'), 'ios')

    def test_per_account_platform_modes_are_independent(self):
        self.settings.ios_daily_enable = True
        self.settings.account_modes = '{"one":{"android":"login","ios":"off"},"two":{"android":"off","ios":"daily"}}'
        self.run_task()
        self.assertEqual([event[1:] for event in self.events if event[0] == 'login'], [('one', 'android'), ('two', 'ios')])
        self.daily_mock.assert_called_once()
        self.assist_mock.assert_not_called()
        self.awake_mock.assert_not_called()

    def test_quota_is_shared_only_by_the_same_receiver_and_assist_is_separate(self):
        self.settings.accounts = 'one\ntwo\nthree'
        self.settings.account_targets = '{"one":{"jade_receiver":"甲","assist_friend":"借用"},"two":{"jade_receiver":"甲"},"three":{"jade_receiver":"乙"}}'
        self.jade_mock.return_value.run_jade.return_value = SimpleNamespace(status='completed', pending={}, reason='')
        self.run_task()
        self.assertEqual([call.args[2] for call in self.jade_mock.call_args_list], ['甲', '乙'])
        self.assertEqual([call.args[2] for call in self.assist_mock.call_args_list], ['借用', 'assist', 'assist'])

    def test_counter_proof_failure_does_not_save_fourteen_or_block_next_account(self):
        def selection(config, device, friend):
            return SimpleNamespace(friend=friend, normal_used=14, card_used=None)
        self.assist_mock.side_effect = selection
        self.awake_mock.side_effect = None
        self.awake_mock.return_value = AwakeningResult(0, 0, False, 'assist_count_mismatch')
        result = self.run_task()
        self.assertEqual(result['status'], 'account_routine_pending')
        self.assertEqual(self.switch_mock.call_count, 2)
        self.assertEqual(self.model.runtime.accounts['one|android'].awakening_completed, 0)
        self.assertFalse(self.model.runtime.accounts['one|android'].awakening_daily_done)
        self.assertTrue(result['keep_emulator'])

    def test_fourteen_matching_counts_skip_without_a_battle(self):
        self.assist_mock.side_effect = lambda config, device, friend: SimpleNamespace(friend=friend, normal_used=14, card_used=14)
        self.run_task()
        self.awake_mock.assert_not_called()
        self.assertEqual(self.model.runtime.accounts['one|android'].awakening_script_wins, 0)
        self.assertEqual(self.model.runtime.accounts['one|android'].awakening_observed, 14)

    def test_saved_ten_and_observed_five_add_only_four_new_wins(self):
        self.settings.accounts = 'one'
        self.model.runtime.accounts['one|android'] = AccountRoutineProgress(day='2026-10-31', awakening_completed=10)
        self.assist_mock.side_effect = lambda config, device, friend: SimpleNamespace(friend=friend, normal_used=5, card_used=5)
        self.run_task()
        self.assertEqual(self.awake_mock.call_args.kwargs['count'], 4)
        self.assertEqual(self.model.runtime.accounts['one|android'].awakening_completed, 14)
        self.assertEqual(self.model.runtime.accounts['one|android'].awakening_script_wins, 4)

    def test_new_unarmed_service_waits_for_future_without_logging_in(self):
        self.now = datetime(2026, 10, 31, 20, 30)
        self.model.runtime.schedule.clear()
        result = self.run_task()
        self.switch_mock.assert_not_called()
        self.assertEqual(result['status'], 'waiting')
        self.assertGreater(self.task.set_next_run.call_args.kwargs['target'], self.now)

    def test_each_account_resets_only_previous_accounts_stuck_history(self):
        self.task.device = SimpleNamespace(click_record_clear=Mock(), stuck_record_clear=Mock())
        self.run_task()
        self.assertEqual(self.task.device.click_record_clear.call_count, 2)
        self.assertEqual(self.task.device.stuck_record_clear.call_count, 2)

    def test_due_daytime_expiring_at_eighteen_does_not_consume_evening_appointment(self):
        self.now = datetime(2026, 10, 31, 18)
        values = chain([datetime(2026, 10, 31, 17, 59, 59)], repeat(self.now))
        self.clock.now.side_effect = lambda: next(values)
        result = self.run_task()
        self.switch_mock.assert_not_called(); self.daily_mock.assert_not_called(); self.awake_mock.assert_not_called()
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(self.model.runtime.schedule['sample:evening:2026-10-31']['state'], 'pending')
        self.assertEqual(self.task.set_next_run.call_args.kwargs['target'], datetime(2026, 10, 31, 20))

    def test_due_evening_expiring_at_six_does_not_consume_the_new_game_day(self):
        self.now = datetime(2026, 11, 1, 6)
        values = chain([datetime(2026, 11, 1, 5, 59, 59)], repeat(self.now))
        self.clock.now.side_effect = lambda: next(values)
        result = self.run_task()
        self.switch_mock.assert_not_called(); self.awake_mock.assert_not_called()
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(self.model.runtime.schedule['sample:daytime:2026-11-01']['state'], 'pending')
        self.assertEqual(self.task.set_next_run.call_args.kwargs['target'], datetime(2026, 11, 1, 17))

    def test_pending_jade_owner_changed_to_off_or_login_blocks_false_completed(self):
        for mode in ('off', 'login'):
            with self.subTest(mode=mode):
                self.events.clear()
                self.settings.account_modes = '{"one":{"android":"' + mode + '"}}'
                self.model.runtime.accounts.clear()
                slot = JadePeriodProgress(owner_account='one|android',
                                         pending={'stage': 'sent', 'monster': '目标', 'slot': 1})
                self.model.runtime.periods['2026-10-31T18:00:00|receiver'] = slot
                outcome = self.task.run_account_batch(self.settings, self.settings.get_account_rows(), 'android', 'receiver')
                self.assertIn('one|android', outcome['failed_accounts'])
                self.assertTrue(slot.pending)
                self.assertFalse(outcome['jade_completed'])
        self.jade_mock.assert_not_called()

    def test_completed_jade_owner_may_be_disabled_without_invalidating_earned_quota(self):
        self.settings.account_modes = '{"one":{"android":"off"}}'
        self.model.runtime.periods['2026-10-31T18:00:00|receiver'] = JadePeriodProgress(
            completed=True, owner_account='one|android')
        outcome = self.task.run_account_batch(self.settings, self.settings.get_account_rows(), 'android', 'receiver')
        self.assertEqual(outcome['failed_accounts'], [])
        self.assertTrue(outcome['jade_completed'])
        self.jade_mock.assert_not_called()

    def test_batch_keeps_claimed_period_if_clock_crosses_before_first_account(self):
        self.now = datetime(2026, 11, 1, 6)
        outcome = self.task.run_account_batch(self.settings, self.settings.get_account_rows(), 'android', 'receiver',
                                             period_start=datetime(2026, 10, 31, 18))
        self.assertEqual(outcome['period'], '2026-10-31T18:00:00')
        self.assertEqual(outcome['failed_accounts'], ['refresh_boundary'])
        self.switch_mock.assert_not_called()
