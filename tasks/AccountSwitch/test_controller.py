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

    def test_saved_ten_does_not_replace_observed_five_when_computing_remaining(self):
        self.settings.accounts = 'one'
        self.model.runtime.accounts['one|android'] = AccountRoutineProgress(day='2026-10-31', awakening_completed=10)
        self.assist_mock.side_effect = lambda config, device, friend: SimpleNamespace(friend=friend, normal_used=5, card_used=5)
        self.run_task()
        self.assertEqual(self.awake_mock.call_args.kwargs['count'], 9)
        self.assertEqual(self.model.runtime.accounts['one|android'].awakening_completed, 14)
        self.assertEqual(self.model.runtime.accounts['one|android'].awakening_script_wins, 9)

    def test_imported_one_unverified_win_does_not_reduce_fourteen_native_wins(self):
        self.settings.accounts = 'one'
        self.model.runtime.accounts['one|android'] = AccountRoutineProgress(
            day='2026-10-31', awakening_completed=1, awakening_script_wins=1)
        result = self.run_task()
        progress = self.model.runtime.accounts['one|android']
        self.assertEqual(self.awake_mock.call_args.kwargs['count'], 14)
        self.assertEqual(progress.awakening_completed, 14)
        self.assertEqual(progress.awakening_script_wins, 15)
        self.assertEqual(progress.awakening_observed, 0)
        self.assertEqual(result['status'], 'completed')

    def test_native_pending_fourteen_with_ui_thirteen_adds_one_without_losing_history(self):
        self.settings.accounts = 'one'
        self.model.runtime.accounts['one|android'] = AccountRoutineProgress(
            day='2026-10-31', awakening_completed=14, awakening_script_wins=113,
            awakening_assist_friend='assist')
        self.assist_mock.side_effect = lambda *args: SimpleNamespace(friend='assist', normal_used=13, card_used=13)
        with patch.object(self.task, 'audit') as audit:
            result = self.run_task()
        progress = self.model.runtime.accounts['one|android']
        self.assertEqual(self.awake_mock.call_args.kwargs['count'], 1)
        self.assertEqual(progress.awakening_completed, 14)
        self.assertEqual(progress.awakening_observed, 13)
        self.assertEqual(progress.awakening_script_wins, 114)
        self.assertTrue(progress.awakening_daily_done)
        deltas = [call.args[3]['delta_wins'] for call in audit.call_args_list if call.args[2] == 'awakening_progress']
        self.assertEqual(deltas, [1])
        self.assertEqual(result['status'], 'completed')

    def test_duplicate_cumulative_callbacks_and_final_result_do_not_double_count(self):
        self.settings.accounts = 'one'
        def partial(config, device, layer, count, on_progress, **kwargs):
            for cumulative in (1, 1, 2, 1, 2): on_progress(cumulative)
            return AwakeningResult(count, 2, False, 'interrupted', True)
        self.awake_mock.side_effect = partial
        with patch.object(self.task, 'audit') as audit:
            result = self.run_task()
        progress = self.model.runtime.accounts['one|android']
        self.assertEqual(progress.awakening_completed, 2)
        self.assertEqual(progress.awakening_script_wins, 2)
        deltas = [call.args[3]['delta_wins'] for call in audit.call_args_list if call.args[2] == 'awakening_progress']
        self.assertEqual(deltas, [1, 1])
        self.assertFalse(progress.awakening_daily_done)
        self.assertEqual(result['status'], 'account_routine_pending')

    def test_thirteen_successful_native_battles_cannot_report_fourteen_complete(self):
        self.settings.accounts = 'one'
        self.model.runtime.accounts['one|android'] = AccountRoutineProgress(day='2026-10-31', awakening_completed=1)
        def short(config, device, layer, count, on_progress, **kwargs):
            on_progress(13)
            return AwakeningResult(count, 13, True, assist_verified=True, daily_target_reached=True)
        self.awake_mock.side_effect = short
        result = self.run_task()
        progress = self.model.runtime.accounts['one|android']
        self.assertEqual(self.awake_mock.call_args.kwargs['count'], 14)
        self.assertEqual(progress.awakening_completed, 13)
        self.assertFalse(progress.awakening_daily_done)
        self.assertEqual(result['status'], 'account_routine_pending')

    def test_old_completed_fourteen_without_native_evidence_is_not_replayed_same_day(self):
        self.settings.accounts = 'one'
        self.model.runtime.accounts['one|android'] = AccountRoutineProgress(
            day='2026-10-31', daily_done=True, awakening_completed=14, awakening_script_wins=14)
        self.model.runtime.periods['2026-10-31T18:00:00|receiver'] = JadePeriodProgress(completed=True)
        self.run_task()
        self.switch_mock.assert_not_called()
        self.awake_mock.assert_not_called()
        self.assertEqual(self.model.runtime.accounts['one|android'].awakening_script_wins, 14)

    def test_custom_five_finishes_once_then_increasing_target_resumes_from_ui(self):
        self.settings.accounts = 'one'
        self.settings.awakening_count = 5
        self.run_task()
        progress = self.model.runtime.accounts['one|android']
        self.assertEqual(self.awake_mock.call_args.kwargs['count'], 5)
        self.assertTrue(progress.awakening_daily_done)
        self.awake_mock.reset_mock()
        self.task.run_account_batch(self.settings, self.settings.get_account_rows(), 'android', 'receiver')
        self.awake_mock.assert_not_called()
        self.settings.awakening_count = 14
        self.assist_mock.side_effect = lambda *args: SimpleNamespace(friend='assist', normal_used=5, card_used=5)
        self.task.run_account_batch(self.settings, self.settings.get_account_rows(), 'android', 'receiver')
        self.assertEqual(self.awake_mock.call_args.kwargs['count'], 9)
        self.assertEqual(progress.awakening_script_wins, 14)
        self.assertTrue(progress.awakening_daily_done)

    def test_custom_goal_already_observed_skips_without_adding_script_wins(self):
        self.settings.accounts = 'one'
        self.settings.awakening_count = 5
        self.assist_mock.side_effect = lambda *args: SimpleNamespace(friend='assist', normal_used=5, card_used=5)
        self.run_task()
        progress = self.model.runtime.accounts['one|android']
        self.awake_mock.assert_not_called()
        self.assertEqual(progress.awakening_completed, 5)
        self.assertEqual(progress.awakening_script_wins, 0)
        self.assertTrue(progress.awakening_daily_done)

    def test_midnight_starts_new_account_day_but_keeps_evening_jade_slot(self):
        self.settings.accounts = 'one'
        self.now = datetime(2026, 11, 1, 0, 5)
        self.model.runtime.accounts['one|android'] = AccountRoutineProgress(
            day='2026-10-31', daily_done=True, awakening_completed=14, awakening_daily_done=True)
        slot = JadePeriodProgress(completed=True)
        self.model.runtime.periods['2026-10-31T18:00:00|receiver'] = slot
        self.task.run_account_batch(self.settings, self.settings.get_account_rows(), 'android', 'receiver')
        progress = self.model.runtime.accounts['one|android']
        self.assertEqual(progress.day, '2026-11-01')
        self.assertEqual(self.daily_mock.call_args.kwargs['routine_day'], '2026-11-01')
        self.assertEqual(self.awake_mock.call_args.kwargs['count'], 14)
        self.jade_mock.assert_not_called()
        self.assertIs(self.model.runtime.periods['2026-10-31T18:00:00|receiver'], slot)

    def test_cross_midnight_after_daily_stops_following_steps_and_accounts(self):
        self.now = datetime(2026, 10, 31, 23, 59)
        def daily_then_midnight(*args, **kwargs):
            result = self.daily(*args, **kwargs)
            self.now = datetime(2026, 11, 1)
            return result
        self.daily_mock.side_effect = daily_then_midnight
        result = self.task.run_account_batch(self.settings, self.settings.get_account_rows(), 'android', 'receiver')
        self.assertIn('refresh_boundary', result['failed_accounts'])
        self.awake_mock.assert_not_called()
        self.jade_mock.assert_not_called()
        self.assertEqual(self.switch_mock.call_count, 1)
        self.assertEqual(self.model.runtime.accounts['one|android'].day, '2026-10-31')

    def test_callback_after_midnight_cannot_put_a_new_win_into_yesterdays_progress(self):
        self.settings.accounts = 'one\ntwo'
        self.now = datetime(2026, 10, 31, 23, 59)
        def win_after_midnight(config, device, layer, count, on_progress, **kwargs):
            self.now = datetime(2026, 11, 1)
            on_progress(1)
            raise AssertionError('A midnight callback must stop the flow')
        self.awake_mock.side_effect = win_after_midnight
        with patch.object(self.task, 'audit') as audit:
            result = self.task.run_account_batch(self.settings, self.settings.get_account_rows(), 'android', 'receiver')
        progress = self.model.runtime.accounts['one|android']
        self.assertEqual(progress.day, '2026-10-31')
        self.assertEqual(progress.awakening_completed, 0)
        self.assertEqual(progress.awakening_script_wins, 0)
        self.assertIn('refresh_boundary', result['failed_accounts'])
        events = [call for call in audit.call_args_list if call.args[2] == 'awakening_progress']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].args[4], '2026-11-01')
        self.assertEqual(events[0].args[3]['delta_wins'], 1)

    def test_login_crossing_midnight_stops_before_daily_or_battle(self):
        self.now = datetime(2026, 10, 31, 23, 59)
        def switch_at_midnight(config, device, account):
            def enter():
                self.now = datetime(2026, 11, 1)
                return True
            return SimpleNamespace(switchAccount=enter)
        self.switch_mock.side_effect = switch_at_midnight
        result = self.task.run_account_batch(self.settings, self.settings.get_account_rows(), 'android', 'receiver')
        self.daily_mock.assert_not_called()
        self.awake_mock.assert_not_called()
        self.assertEqual(self.switch_mock.call_count, 1)
        self.assertIn('refresh_boundary', result['failed_accounts'])

    def test_audit_uses_actual_calendar_day_even_when_jade_period_started_yesterday(self):
        self.now = datetime(2026, 11, 1, 0, 5)
        with patch('tasks.AccountSwitch.history.record_event') as record:
            self.task.audit('one', 'android', 'login', {}, '2026-10-31')
        self.assertEqual(record.call_args.kwargs['day'], '2026-11-01')

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
