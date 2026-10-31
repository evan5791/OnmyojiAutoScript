import unittest
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError

from module.exception import RequestHumanTakeover, TaskEnd

from tasks.AccountSwitch.config import (
    AccountRoutineProgress,
    AccountRoutineState,
    AccountSwitch,
    AccountSwitchConfig,
    JadePeriodProgress,
)
from tasks.AccountSwitch.routine import (
    next_routine_run,
    next_scheduled_run,
    period_run_time,
    refresh_period,
)


class RefreshPeriodTests(unittest.TestCase):
    def test_six_and_eighteen_start_new_independent_refresh_periods(self):
        cases = [
            ('2026-10-31 05:59:59', '2026-10-30 18:00:00', '2026-10-31 06:00:00', True),
            ('2026-10-31 06:00:00', '2026-10-31 06:00:00', '2026-10-31 18:00:00', False),
            ('2026-10-31 17:59:59', '2026-10-31 06:00:00', '2026-10-31 18:00:00', False),
            ('2026-10-31 18:00:00', '2026-10-31 18:00:00', '2026-11-01 06:00:00', True),
        ]
        for instant, start, end, evening in cases:
            with self.subTest(instant=instant):
                period = refresh_period(datetime.fromisoformat(instant))
                self.assertEqual(period.start, datetime.fromisoformat(start))
                self.assertEqual(period.end, datetime.fromisoformat(end))
                self.assertEqual(period.evening, evening)

    def test_midnight_retains_the_same_jade_evening_period(self):
        before_midnight = refresh_period(datetime(2026, 10, 31, 23, 59))
        after_midnight = refresh_period(datetime(2026, 11, 1, 1))
        self.assertEqual(before_midnight, after_midnight)
        self.assertEqual(after_midnight.day, '2026-10-31')
        self.assertEqual(after_midnight.key('5791'), before_midnight.key('5791'))

    def test_different_receiver_or_refresh_never_shares_a_quota_key(self):
        daytime = refresh_period(datetime(2026, 10, 31, 17))
        evening = refresh_period(datetime(2026, 10, 31, 20))
        next_day = refresh_period(datetime(2026, 11, 1, 17))
        keys = {
            daytime.key('5791'), daytime.key('另一个大号'),
            evening.key('5791'), next_day.key('5791'),
        }
        self.assertEqual(len(keys), 4)


class RoutineScheduleTests(unittest.TestCase):
    def setUp(self):
        self.settings = AccountSwitchConfig(routine_enable=True)

    def test_default_plan_has_one_afternoon_and_one_evening_run(self):
        self.assertEqual(self.settings.daytime_run_time, time(17))
        self.assertEqual(self.settings.evening_run_time, time(20))
        self.assertEqual(self.settings.awakening_layer, 1)
        self.assertEqual(self.settings.awakening_count, 14)

    def test_next_run_moves_between_two_daily_times_without_immediate_loop(self):
        cases = [
            ('2026-10-31 16:59:59', '2026-10-31 17:00:00'),
            ('2026-10-31 17:00:00', '2026-10-31 20:00:00'),
            ('2026-10-31 18:00:00', '2026-10-31 20:00:00'),
            ('2026-10-31 20:00:00', '2026-11-01 17:00:00'),
            ('2026-10-31 23:59:59', '2026-11-01 17:00:00'),
        ]
        for instant, expected in cases:
            with self.subTest(instant=instant):
                self.assertEqual(
                    next_scheduled_run(datetime.fromisoformat(instant), self.settings),
                    datetime.fromisoformat(expected),
                )

    def test_configured_times_replace_default_seventeen_and_twenty(self):
        settings = AccountSwitchConfig(daytime_run_time='16:15:00', evening_run_time='21:40:00')
        self.assertEqual(next_scheduled_run(datetime(2026, 10, 31, 16), settings), datetime(2026, 10, 31, 16, 15))
        self.assertEqual(next_scheduled_run(datetime(2026, 10, 31, 17), settings), datetime(2026, 10, 31, 21, 40))

    def test_evening_run_time_after_midnight_belongs_to_previous_evening(self):
        period = refresh_period(datetime(2026, 11, 1, 2))
        self.assertEqual(period_run_time(period, self.settings), datetime(2026, 10, 31, 20))

    def test_failed_invitation_retries_before_refresh_expires(self):
        now = datetime(2026, 10, 31, 17, 20)
        self.assertEqual(
            next_routine_run(now, self.settings, refresh_period(now), retry=True),
            datetime(2026, 10, 31, 17, 30),
        )

    def test_retry_does_not_cross_eighteen_into_the_next_refresh(self):
        for minute in (50, 55):
            now = datetime(2026, 10, 31, 17, minute)
            with self.subTest(now=now):
                self.assertEqual(
                    next_routine_run(now, self.settings, refresh_period(now), retry=True),
                    datetime(2026, 10, 31, 20),
                )

    def test_overnight_retry_stops_at_six(self):
        now = datetime(2026, 11, 1, 5, 55)
        self.assertEqual(
            next_routine_run(now, self.settings, refresh_period(now), retry=True),
            datetime(2026, 11, 1, 17),
        )

    def test_new_configured_run_takes_priority_over_a_later_retry(self):
        settings = AccountSwitchConfig(evening_run_time='20:05:00', retry_minutes=10)
        now = datetime(2026, 10, 31, 20)
        self.assertEqual(
            next_routine_run(now, settings, refresh_period(now), retry=True),
            datetime(2026, 10, 31, 20, 5),
        )

    def test_finished_batch_uses_the_next_plan_instead_of_retry_interval(self):
        now = datetime(2026, 10, 31, 20, 15)
        self.assertEqual(
            next_routine_run(now, self.settings, refresh_period(now), retry=False),
            datetime(2026, 11, 1, 17),
        )

    def test_execution_times_must_match_their_refresh_period(self):
        for field, value in [
            ('daytime_run_time', '05:59:59'), ('daytime_run_time', '18:00:00'),
            ('evening_run_time', '17:59:59'),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                AccountSwitchConfig(**{field: value})


class RoutinePersistenceTests(unittest.TestCase):
    def test_restart_preserves_per_account_progress_and_pending_invitation(self):
        config = AccountSwitch()
        first = AccountRoutineProgress(day='2026-10-31', daily_done=True, awakening_completed=6)
        first.daily_record.pickup_email_dt = datetime(2026, 10, 31, 20, 3)
        config.runtime.accounts['android:first@163.com'] = first
        config.runtime.accounts['android:second@163.com'] = AccountRoutineProgress(day='2026-10-31')
        key = refresh_period(datetime(2026, 10, 31, 20)).key('5791')
        config.runtime.periods[key] = JadePeriodProgress(
            owner_account='android:first@163.com',
            pending={'quest_slot': 2, 'stage': 'sent'},
            checked_accounts=['android:second@163.com'],
        )

        restored = AccountSwitch.model_validate_json(config.model_dump_json())

        self.assertTrue(restored.runtime.accounts['android:first@163.com'].daily_done)
        self.assertEqual(restored.runtime.accounts['android:first@163.com'].awakening_completed, 6)
        self.assertEqual(restored.runtime.accounts['android:first@163.com'].daily_record.pickup_email_dt, datetime(2026, 10, 31, 20, 3))
        self.assertFalse(restored.runtime.accounts['android:second@163.com'].daily_done)
        self.assertEqual(restored.runtime.accounts['android:second@163.com'].awakening_completed, 0)
        self.assertEqual(restored.runtime.periods[key].pending, {'quest_slot': 2, 'stage': 'sent'})
        self.assertEqual(restored.runtime.periods[key].owner_account, 'android:first@163.com')

    def test_restart_keeps_completed_quota_separate_from_new_refresh(self):
        daytime = refresh_period(datetime(2026, 10, 31, 17))
        evening = refresh_period(datetime(2026, 10, 31, 20))
        config = AccountSwitch()
        config.runtime.periods[daytime.key('5791')] = JadePeriodProgress(completed=True, owner_account='first')
        restored = AccountSwitch.model_validate_json(config.model_dump_json())
        self.assertTrue(restored.runtime.periods[daytime.key('5791')].completed)
        self.assertNotIn(evening.key('5791'), restored.runtime.periods)

    def test_hidden_runtime_is_still_persisted_and_isolated_between_accounts(self):
        first = AccountRoutineProgress()
        second = AccountRoutineProgress()
        first.daily_record.summon_dt = datetime(2026, 10, 31, 20)
        self.assertNotEqual(first.daily_record.summon_dt, second.daily_record.summon_dt)

        config = AccountSwitch(runtime=AccountRoutineState(accounts={'first': first, 'second': second}))
        self.assertEqual(config.model_dump(context={'hide': True})['runtime'], 0xABCDEF)
        self.assertIsInstance(config.model_dump()['runtime'], dict)
        self.assertEqual(set(config.model_dump()['runtime']['accounts']), {'first', 'second'})

    def test_oasx_exposes_settings_but_keeps_runtime_out_of_the_form(self):
        from module.config.config_model import ConfigModel

        arguments = ConfigModel().script_task('AccountSwitch')
        self.assertNotIn('runtime', arguments)
        names = {argument['name'] for argument in arguments['account_switch_config']}
        self.assertTrue({'routine_enable', 'daytime_run_time', 'evening_run_time', 'awakening_layer', 'awakening_count'} <= names)


class RoutineWorkflowTests(unittest.TestCase):
    def make_task(self, count=3, saved=None):
        from tasks.AccountSwitch.script_task import ScriptTask

        task = ScriptTask.__new__(ScriptTask)
        model = saved or AccountSwitch(account_switch_config=AccountSwitchConfig(
            accounts='\n'.join(f'alt{index:02d}@163.com' for index in range(1, count + 1)),
            invite_friend_name='5791', assist_friend='5791', routine_enable=True,
            daytime_jitter_minutes=0, evening_jitter_minutes=0, ios_daily_jitter_minutes=0,
        ))
        model.runtime.schedule.setdefault('service:android', {'armed_at': '2026-10-30T00:00:00'})
        task.config = SimpleNamespace(account_switch=model, save=Mock())
        task.device = object()
        task.goto_page = Mock()
        task.set_next_run = Mock()
        self.events = []
        self.current_account = None
        self.snapshots = []
        task.config.save.side_effect = lambda: self.snapshots.append(AccountSwitch.model_validate_json(model.model_dump_json()))
        return task

    def execute(self, task, now, jade_results=None, daily=None, awakening=None, login=None, clock_state=None, batch_platform=None, observed=0):
        jade_results = iter(jade_results or [SimpleNamespace(status='completed', pending=None, reason='')])

        def switch(config, device, account):
            def run():
                self.current_account = account.account
                self.events.append(('login', account.account))
                return login(account) if login else True
            return SimpleNamespace(switchAccount=run)

        def jade(pending=None, on_checkpoint=None):
            self.events.append(('jade', self.current_account, pending))
            result = next(jade_results)
            if result.pending:
                on_checkpoint(result.pending)
            return result

        def daily_default(config, device, record, on_progress=None, routine_day=None, **kwargs):
            self.events.append(('daily', self.current_account, routine_day))
            record = record.model_copy(deep=True)
            record.pickup_email_dt = now
            on_progress(record)
            return SimpleNamespace(done_record=record, success=True, pending=())

        def awakening_default(config, device, layer=1, count=14, on_progress=None, **kwargs):
            self.events.append(('awakening', self.current_account, layer, count))
            on_progress(count)
            return SimpleNamespace(requested=count, completed=count, success=True)

        with patch('tasks.AccountSwitch.script_task.datetime') as clock, \
                patch('tasks.AccountSwitch.script_task.SwitchAccount', side_effect=switch) as switches, \
                patch('tasks.AccountSwitch.flows.run_daily', side_effect=daily or daily_default) as daily_flow, \
                patch('tasks.AccountSwitch.flows.run_awakening', side_effect=awakening or awakening_default) as awakening_flow, \
                patch('tasks.AccountSwitch.jade.JadeCooperationTask') as jade_task, \
                patch('tasks.AccountSwitch.cooperation.prepare_cooperation_assist',
                      return_value=SimpleNamespace(friend='5791',normal_used=observed,card_used=observed)), \
                patch('tasks.AccountSwitch.script_task.logger'):
            if clock_state is None:
                clock.now.return_value = now
            else:
                clock.now.side_effect = lambda: clock_state['now']
            jade_task.return_value.run_jade.side_effect = jade
            if batch_platform:
                settings = task.config.account_switch.account_switch_config
                task.run_account_batch(settings, settings.get_account_rows(), batch_platform, settings.invite_friend_name or '5791')
            else:
                with self.assertRaises(TaskEnd):
                    task.run()
        return switches, daily_flow, awakening_flow, jade_task

    @staticmethod
    def jade_result(status, pending=None):
        return SimpleNamespace(status=status, pending=pending, reason=status)

    def period_state(self, task, now, receiver='5791'):
        return task.config.account_switch.runtime.periods[refresh_period(now).key(receiver)]

    def test_running_early_only_schedules_the_configured_time(self):
        task = self.make_task()
        switches, daily, awakening, jade = self.execute(task, datetime(2026, 10, 31, 14))
        switches.assert_not_called()
        daily.assert_not_called()
        awakening.assert_not_called()
        jade.assert_not_called()
        task.set_next_run.assert_called_once_with('AccountSwitch', target=datetime(2026, 10, 31, 17), server=False)

    def test_daytime_only_checks_jade_and_stops_at_first_completed_cooperation(self):
        task = self.make_task(count=10)
        now = datetime(2026, 10, 31, 17)
        switches, daily, awakening, jade = self.execute(task, now, [
            self.jade_result('no_jade'), self.jade_result('completed'),
        ])
        self.assertEqual([call.args[2].account for call in switches.call_args_list], ['alt01@163.com', 'alt02@163.com'])
        daily.assert_not_called()
        awakening.assert_not_called()
        self.assertEqual(jade.call_count, 2)
        self.assertTrue(self.period_state(task, now).completed)
        self.assertEqual(task.config.task_runtime_outcome['status'], 'completed')
        task.set_next_run.assert_called_once_with('AccountSwitch', target=datetime(2026, 10, 31, 20), server=False)

    def test_finding_jade_after_an_earlier_login_failure_finishes_daytime_goal(self):
        task = self.make_task()
        self.execute(task, datetime(2026, 10, 31, 17), login=lambda account: account.account != 'alt01@163.com')
        self.assertTrue(task.config.task_runtime_outcome['jade_completed'])
        self.assertEqual(task.config.task_runtime_outcome['status'], 'completed')
        task.set_next_run.assert_called_once_with('AccountSwitch', target=datetime(2026, 10, 31, 20), server=False)

    def test_ten_accounts_without_jade_are_checked_once_per_refresh(self):
        task = self.make_task(count=10)
        now = datetime(2026, 10, 31, 17)
        switches, daily, awakening, jade = self.execute(task, now, [self.jade_result('no_jade') for _ in range(10)])
        self.assertEqual(switches.call_count, 10)
        self.assertEqual(jade.call_count, 10)
        self.assertEqual(len(self.period_state(task, now).checked_accounts), 10)
        self.assertFalse(self.period_state(task, now).completed)
        daily.assert_not_called()
        awakening.assert_not_called()

        restored = AccountSwitch.model_validate_json(task.config.account_switch.model_dump_json())
        rerun = self.make_task(saved=restored)
        switches, _, _, jade = self.execute(rerun, datetime(2026, 10, 31, 17, 30))
        switches.assert_not_called()
        jade.assert_not_called()

    def test_evening_runs_daily_then_single_jade_then_fourteen_awakening_for_all_ten(self):
        task = self.make_task(count=10)
        now = datetime(2026, 10, 31, 20)
        switches, daily, awakening, jade = self.execute(task, now)
        self.assertEqual(switches.call_count, 10)
        self.assertEqual(daily.call_count, 10)
        self.assertEqual(awakening.call_count, 10)
        self.assertEqual(jade.call_count, 1)
        self.assertEqual([event[0] for event in self.events[:4]], ['login', 'daily', 'jade', 'awakening'])
        self.assertTrue(all(event[2:] == (1, 14) for event in self.events if event[0] == 'awakening'))
        progress = task.config.account_switch.runtime.accounts
        self.assertTrue(all(value.daily_done and value.awakening_completed == 14 for value in progress.values()))
        self.assertEqual(task.config.task_runtime_outcome['failed_accounts'], [])
        task.set_next_run.assert_called_once_with('AccountSwitch', target=datetime(2026, 11, 1, 17), server=False)

    def test_daytime_completion_does_not_use_up_the_evening_quota(self):
        task = self.make_task(count=2)
        daytime = refresh_period(datetime(2026, 10, 31, 17))
        task.config.account_switch.runtime.periods[daytime.key('5791')] = JadePeriodProgress(completed=True)
        _, _, _, jade = self.execute(task, datetime(2026, 10, 31, 20))
        self.assertEqual(jade.call_count, 1)
        self.assertTrue(self.period_state(task, datetime(2026, 10, 31, 20)).completed)

    def test_incomplete_invitation_blocks_other_accounts_but_not_their_evening_daily_and_awakening(self):
        task = self.make_task(count=3)
        now = datetime(2026, 10, 31, 20)
        _, daily, awakening, jade = self.execute(task, now, [self.jade_result('incomplete', {'stage': 'sent', 'quest_slot': 1})])
        self.assertEqual(jade.call_count, 1)
        self.assertEqual(daily.call_count, 3)
        self.assertEqual(awakening.call_count, 3)
        slot = self.period_state(task, now)
        self.assertFalse(slot.completed)
        self.assertEqual(task.config.task_runtime_outcome['status'], 'account_routine_pending')
        self.assertEqual(slot.owner_account, 'alt01@163.com|android')
        self.assertEqual(slot.pending, {'stage': 'sent', 'quest_slot': 1})
        self.assertTrue(any(snapshot.runtime.periods[refresh_period(now).key('5791')].pending for snapshot in self.snapshots if refresh_period(now).key('5791') in snapshot.runtime.periods))
        task.set_next_run.assert_called_once_with('AccountSwitch', target=datetime(2026, 10, 31, 20, 10), server=False)

    def test_restart_resumes_pending_owner_first_without_repeating_finished_daily_work(self):
        task = self.make_task(count=3)
        now = datetime(2026, 10, 31, 20)
        runtime = task.config.account_switch.runtime
        for account in task.config.account_switch.account_switch_config.get_accounts():
            runtime.accounts[task.account_key(account)] = AccountRoutineProgress(day='2026-10-31', daily_done=True, awakening_completed=14)
        runtime.periods[refresh_period(now).key('5791')] = JadePeriodProgress(
            owner_account='alt03@163.com|android', pending={'stage': 'sent', 'quest_slot': 2},
        )
        restored = AccountSwitch.model_validate_json(task.config.account_switch.model_dump_json())
        task = self.make_task(saved=restored)
        switches, daily, awakening, jade = self.execute(task, datetime(2026, 10, 31, 20, 10))
        self.assertEqual([call.args[2].account for call in switches.call_args_list], ['alt03@163.com'])
        jade.return_value.run_jade.assert_called_once()
        self.assertEqual(jade.return_value.run_jade.call_args.kwargs['pending'], {'stage': 'sent', 'quest_slot': 2})
        daily.assert_not_called()
        awakening.assert_not_called()

    def test_awakening_restart_only_runs_remaining_count_and_keeps_accounts_separate(self):
        task = self.make_task(count=2)
        now = datetime(2026, 10, 31, 20)
        runtime = task.config.account_switch.runtime
        runtime.periods[refresh_period(now).key('5791')] = JadePeriodProgress(completed=True)
        runtime.accounts['alt01@163.com|android'] = AccountRoutineProgress(day='2026-10-31', daily_done=True, awakening_completed=6)

        def awakening(config, device, layer, count, on_progress, **kwargs):
            self.events.append(('awakening', self.current_account, layer, count))
            if self.current_account == 'alt01@163.com':
                on_progress(4)
            on_progress(count)
            return SimpleNamespace(requested=count, completed=count, success=True)

        _, daily, _, jade = self.execute(task, now, awakening=awakening, observed=6)
        self.assertEqual([event[3] for event in self.events if event[0] == 'awakening'], [8, 8])
        self.assertEqual(daily.call_count, 1)
        jade.assert_not_called()
        self.assertTrue(any(snapshot.runtime.accounts['alt01@163.com|android'].awakening_completed == 10 for snapshot in self.snapshots))
        self.assertEqual(runtime.accounts['alt01@163.com|android'].awakening_completed, 14)
        self.assertEqual(runtime.accounts['alt02@163.com|android'].awakening_completed, 14)
        self.assertNotEqual(runtime.accounts['alt01@163.com|android'].daily_record.pickup_email_dt, runtime.accounts['alt02@163.com|android'].daily_record.pickup_email_dt)

    def test_partial_awakening_keeps_its_count_and_retries_without_repeating_daily(self):
        task = self.make_task(count=1)
        now = datetime(2026, 10, 31, 20)

        def partial(config, device, layer, count, on_progress, **kwargs):
            on_progress(5)
            return SimpleNamespace(requested=count, completed=5, success=False)

        self.execute(task, now, awakening=partial)
        self.assertEqual(task.config.account_switch.runtime.accounts['alt01@163.com|android'].awakening_completed, 5)
        self.assertEqual(task.config.task_runtime_outcome['status'], 'account_routine_pending')
        task.set_next_run.assert_called_once_with('AccountSwitch', target=datetime(2026, 10, 31, 20, 10), server=False)

        restored = AccountSwitch.model_validate_json(task.config.account_switch.model_dump_json())
        resumed = self.make_task(saved=restored)
        _, daily, awakening, jade = self.execute(resumed, datetime(2026, 10, 31, 20, 10), observed=5)
        daily.assert_not_called()
        jade.assert_not_called()
        self.assertEqual(awakening.call_args.kwargs['count'], 9)

    def test_completed_schedule_waits_for_next_evening_but_manual_midnight_batch_uses_new_day(self):
        task = self.make_task(count=2)
        self.execute(task, datetime(2026, 10, 31, 20))
        restored = AccountSwitch.model_validate_json(task.config.account_switch.model_dump_json())
        overnight = self.make_task(saved=restored)
        switches, daily, awakening, jade = self.execute(overnight, datetime(2026, 11, 1, 1))
        switches.assert_not_called()
        daily.assert_not_called()
        awakening.assert_not_called()
        jade.assert_not_called()

        manual = self.make_task(saved=AccountSwitch.model_validate_json(restored.model_dump_json()))
        switches, daily, awakening, jade = self.execute(manual, datetime(2026, 11, 1, 1), batch_platform='android')
        self.assertEqual((switches.call_count, daily.call_count, awakening.call_count), (2, 2, 2))
        jade.assert_not_called()
        self.assertTrue(all(progress.day == '2026-11-01' for progress in manual.config.account_switch.runtime.accounts.values()))

        next_evening = self.make_task(saved=AccountSwitch.model_validate_json(restored.model_dump_json()))
        switches, daily, awakening, jade = self.execute(next_evening, datetime(2026, 11, 1, 20))
        self.assertEqual(switches.call_count, 2)
        self.assertEqual(daily.call_count, 2)
        self.assertEqual(awakening.call_count, 2)
        self.assertEqual(jade.call_count, 1)
        self.assertTrue(all(progress.day == '2026-11-01' for progress in next_evening.config.account_switch.runtime.accounts.values()))

    def test_checkpoint_remains_saved_when_a_battle_requires_human_takeover(self):
        task = self.make_task(count=1)

        def interrupted(config, device, layer, count, on_progress, **kwargs):
            on_progress(4)
            raise RequestHumanTakeover('battle interrupted')

        self.execute(task, datetime(2026, 10, 31, 20), awakening=interrupted)
        self.assertEqual(task.config.task_runtime_outcome['status'], 'account_routine_pending')
        self.assertTrue(self.snapshots[-1].runtime.accounts['alt01@163.com|android'].daily_done)
        self.assertEqual(self.snapshots[-1].runtime.accounts['alt01@163.com|android'].awakening_completed, 4)

    def test_changing_main_receiver_starts_a_new_quota_without_redoing_daily_work(self):
        task = self.make_task(count=1)
        now = datetime(2026, 10, 31, 20)
        self.execute(task, now)
        restored = AccountSwitch.model_validate_json(task.config.account_switch.model_dump_json())
        restored.account_switch_config.invite_friend_name = '另一个大号'
        task = self.make_task(saved=restored)
        # Explicit batch continuation can service a changed recipient without
        # reopening a finished scheduled window or replaying daily checkpoints.
        _, daily, awakening, jade = self.execute(task, datetime(2026, 10, 31, 20, 20), batch_platform='android')
        daily.assert_not_called()
        awakening.assert_not_called()
        self.assertEqual(jade.call_count, 1)
        self.assertTrue(self.period_state(task, now, receiver='另一个大号').completed)

    def test_confirmed_invitation_failure_does_not_prevent_next_account_fulfilling_daytime_goal(self):
        task = self.make_task()
        now = datetime(2026, 10, 31, 17)
        _, _, _, jade = self.execute(task, now, [self.jade_result('invite_failed'), self.jade_result('completed')])
        self.assertEqual(jade.call_count, 2)
        slot = self.period_state(task, now)
        self.assertTrue(slot.completed)
        self.assertEqual(slot.owner_account, 'alt02@163.com|android')
        self.assertEqual(task.config.task_runtime_outcome['status'], 'completed')

    def test_removing_pending_owner_does_not_send_duplicate_invitation_from_another_account(self):
        task = self.make_task(count=2)
        now = datetime(2026, 10, 31, 20)
        runtime = task.config.account_switch.runtime
        key = refresh_period(now).key('5791')
        runtime.periods[key] = JadePeriodProgress(owner_account='removed@163.com|android', pending={'stage': 'sent'})
        _, daily, awakening, jade = self.execute(task, now)
        jade.assert_not_called()
        self.assertEqual(daily.call_count, 2)
        self.assertEqual(awakening.call_count, 2)
        self.assertEqual(runtime.periods[key].pending, {'stage': 'sent'})
        self.assertFalse(runtime.periods[key].completed)
        self.assertEqual(task.config.task_runtime_outcome['status'], 'account_routine_pending')

    def test_crossing_refresh_while_doing_daily_does_not_invite_into_an_unplanned_new_period(self):
        task = self.make_task(count=1)
        now = datetime(2026, 10, 31, 20)
        clock_state = {'now': now}

        def daily(config, device, record, on_progress, routine_day, **kwargs):
            self.assertEqual(routine_day, '2026-10-31')
            clock_state['now'] = datetime(2026, 11, 1, 6, 1)
            on_progress(record)
            return SimpleNamespace(done_record=record, success=True, pending=())

        _, daily_flow, awakening, jade = self.execute(task, now, daily=daily, clock_state=clock_state)
        self.assertEqual(daily_flow.call_count, 1)
        awakening.assert_not_called()
        jade.assert_not_called()
        self.assertFalse(self.period_state(task, now).completed)
        self.assertEqual(task.config.account_switch.runtime.accounts['alt01@163.com|android'].day, '2026-10-31')
        task.set_next_run.assert_called_once_with('AccountSwitch', target=datetime(2026, 11, 1, 17), server=False)


if __name__ == '__main__':
    unittest.main()
