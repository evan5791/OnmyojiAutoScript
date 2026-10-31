"""Pure configuration and persistent scheduler regression tests; no device I/O."""

from datetime import date, datetime, time
import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from tasks.AccountSwitch.config import AccountRoutineState, AccountSwitch, AccountSwitchConfig
from tasks.AccountSwitch.routine import (
    arm_future, begin, finish, next_appointment, next_due, resolve, sample, window,
)


class AccountSchemaMigrationTests(unittest.TestCase):
    def test_legacy_account_info_and_aliases_remain_compatible(self):
        settings = AccountSwitchConfig(accounts='one@163.com#a1#a2\n\nTWO\nONE@163.COM', apple_or_android=False)
        self.assertEqual([item.account for item in settings.get_accounts()], ['one@163.com', 'TWO'])
        self.assertEqual(settings.get_accounts()[0].account_alias, 'a1#a2')
        self.assertFalse(settings.get_accounts()[0].apple_or_android)
        self.assertEqual(settings.get_accounts()[0].svr, '')

    def test_modes_targets_and_fullwidth_keys_are_independent(self):
        settings = AccountSwitchConfig(
            accounts='ＡＬＴ＠１６３．ＣＯＭ＃alias\nalt@163.com', jade_receiver='5791', assist_friend='借用好友',
            account_modes={'ALT@163.COM': {'android': 'login', 'ios': 'off'}},
            account_targets={'alt@163.com': {'jade_receiver': '另外接收号'}},
        )
        row = settings.get_account_rows()[0]
        self.assertEqual((row.key, row.aliases), ('alt@163.com', ('alias',)))
        self.assertEqual((row.android_mode, row.ios_mode), ('login', 'off'))
        self.assertEqual((row.jade_receiver, row.assist_friend), ('另外接收号', '借用好友'))
        self.assertTrue(row.as_account_info('android').apple_or_android)
        self.assertFalse(row.as_account_info('ios').apple_or_android)
        self.assertEqual(len(settings.get_account_rows()), 1)

    def test_missing_modes_default_to_full_android_and_daily_ios(self):
        row = AccountSwitchConfig(accounts='one').get_account_rows()[0]
        self.assertEqual((row.android_mode, row.ios_mode), ('plan', 'daily'))

    def test_free_daily_overlay_keeps_inheritance_and_explicit_false_separate(self):
        self.assertEqual(AccountSwitchConfig().get_daily_free_options(), {})
        options = AccountSwitchConfig(daily_free_options={'friend_love': True, 'store_sign': False})
        self.assertEqual(options.get_daily_free_options(), {'friend_love': True, 'store_sign': False})
        self.assertEqual(AccountSwitchConfig.model_validate(options.model_dump(mode='json')).get_daily_free_options(),
                         {'friend_love': True, 'store_sign': False})
        for value in ('[]', {'buy_sushi': True}, {'friend_love': 1}, {'luck_msg': 'true'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AccountSwitchConfig(daily_free_options=value)

    def test_awakening_kirin_is_preserved_without_adding_a_runtime_dependency(self):
        self.assertEqual(AccountSwitchConfig().awakening_kirin, 'lightning')
        for kirin in ('lightning', 'fire', 'wind', 'water'):
            self.assertEqual(AccountSwitchConfig(awakening_kirin=kirin).awakening_kirin, kirin)
        with self.assertRaises(ValueError):
            AccountSwitchConfig(awakening_kirin='all')

    def test_legacy_invitation_target_never_becomes_assist_target_implicitly(self):
        row = AccountSwitchConfig(accounts='one', invite_friend_name='旧接收号').get_account_rows()[0]
        self.assertEqual(row.jade_receiver, '旧接收号')
        self.assertEqual(row.assist_friend, '')

    def test_tenth_and_further_accounts_keep_order_and_per_account_modes(self):
        names = [f'alt{number:02}@163.com' for number in range(1, 126)]
        settings = AccountSwitchConfig(accounts='\n\n'.join(names) + '\nALT02@163.com',
                                       account_modes={names[9]: {'android': 'off', 'ios': 'login'}})
        rows = settings.get_account_rows()
        self.assertEqual([row.account for row in rows], names)
        self.assertEqual((rows[9].android_mode, rows[9].ios_mode), ('off', 'login'))

    def test_json_multiline_schema_and_persistence_use_native_oasx_fields(self):
        model = AccountSwitch(account_switch_config={'account_modes': {'one': {'android': 'off'}}})
        dumped = model.model_dump(mode='json')
        self.assertIsInstance(dumped['account_switch_config']['account_modes'], str)
        restored = AccountSwitch.model_validate(dumped)
        self.assertEqual(json.loads(restored.account_switch_config.account_modes)['one']['ios'], 'daily')
        schema = AccountSwitchConfig.model_json_schema()
        self.assertEqual(schema['properties']['account_modes']['type'], 'multi_line')
        self.assertEqual(schema['properties']['account_targets']['type'], 'multi_line')

    def test_invalid_json_platform_mode_key_and_target_are_rejected(self):
        cases = (
            {'account_modes': '[]'}, {'account_modes': 'bad-json'},
            {'account_modes': {'one': {'ios': 'plan'}}}, {'account_modes': {'one': {'Android': 'plan'}}},
            {'account_modes': {'one': {'android': True}}}, {'account_modes': {'one#alias': {}}},
            {'account_modes': {'ONE': {}, 'ＯＮＥ': {}}},
            {'account_modes': '{"one":{},"one":{}}'},
            {'account_targets': {'one': {'receiver': 'bad'}}},
            {'account_targets': {'one': {'assist_friend': '\nwrong'}}},
            {'account_targets': {'one': {'jade_receiver': True}}},
            {'platform_priority': 'both'}, {'daytime_jitter_minutes': True},
            {'evening_jitter_minutes': 181}, {'ios_daily_jitter_minutes': -1},
        )
        for data in cases:
            with self.subTest(data=data), self.assertRaises((ValueError, ValidationError)):
                AccountSwitchConfig(**data)

    def test_runtime_retains_imported_flags_without_forging_timestamp_or_script_wins(self):
        runtime = AccountRoutineState.model_validate({'accounts': {'one|android': {
            'day': '2026-10-31', 'awakening_completed': 14, 'awakening_observed': 14,
            'awakening_daily_done': True, 'daily_confirmed': {'pickup_email': True},
        }}})
        progress = runtime.accounts['one|android']
        self.assertEqual(progress.awakening_script_wins, 0)
        self.assertTrue(progress.daily_confirmed['pickup_email'])
        self.assertEqual(progress.daily_record.pickup_email_dt.year, 2023)


class PersistentScheduleTests(unittest.TestCase):
    def setUp(self):
        self.settings = AccountSwitchConfig(routine_enable=True, ios_daily_enable=True,
                                           daytime_jitter_minutes=0, evening_jitter_minutes=0,
                                           ios_daily_jitter_minutes=0)
        self.runtime = AccountRoutineState()

    def arm(self, now=datetime(2026, 10, 31, 10)):
        for platform in ('android', 'ios'):
            arm_future(self.runtime, self.settings, platform, now)

    def test_three_independent_draws_are_persistent_across_model_reopen(self):
        self.settings.daytime_jitter_minutes = 15
        self.settings.evening_jitter_minutes = 15
        self.settings.ios_daily_jitter_minutes = 15
        with patch('tasks.AccountSwitch.routine.random.randint', side_effect=[-60, 120, 180]) as draw:
            values = [sample(self.runtime, self.settings, plan, date(2026, 10, 31))
                      for plan in ('daytime', 'evening', 'ios_daily')]
            self.assertEqual(draw.call_count, 3)
        reopened = AccountRoutineState.model_validate(self.runtime.model_dump(mode='json'))
        with patch('tasks.AccountSwitch.routine.random.randint', side_effect=AssertionError('must reuse saved sample')):
            self.assertEqual([sample(reopened, self.settings, plan, '2026-10-31')
                              for plan in ('daytime', 'evening', 'ios_daily')], values)
        self.assertEqual([value['at'] for value in values], ['2026-10-31T16:59:00', '2026-10-31T20:02:00', '2026-10-31T20:03:00'])

    def test_changing_one_plan_redraws_only_that_plan(self):
        for plan in ('daytime', 'evening', 'ios_daily'):
            sample(self.runtime, self.settings, plan, '2026-10-31')
        self.settings.evening_jitter_minutes = 10
        with patch('tasks.AccountSwitch.routine.random.randint', return_value=60) as draw:
            for plan in ('daytime', 'evening', 'ios_daily'):
                sample(self.runtime, self.settings, plan, '2026-10-31')
        draw.assert_called_once_with(-600, 600)

    def test_sample_never_crosses_six_eighteen_or_ios_game_day_boundaries(self):
        self.settings.daytime_run_time = time(17, 59)
        self.settings.daytime_jitter_minutes = 180
        self.settings.evening_run_time = time(18)
        self.settings.evening_jitter_minutes = 180
        self.settings.ios_daily_run_time = time(23, 59)
        self.settings.ios_daily_jitter_minutes = 180
        with patch('tasks.AccountSwitch.routine.random.randint', side_effect=[10800, -10800, 10800]):
            self.assertEqual(sample(self.runtime, self.settings, 'daytime', '2026-10-31')['at'], '2026-10-31T17:59:59')
            self.assertEqual(sample(self.runtime, self.settings, 'evening', '2026-10-31')['at'], '2026-10-31T18:00:00')
            self.assertEqual(sample(self.runtime, self.settings, 'ios_daily', '2026-10-31')['at'], '2026-10-31T23:59:59')

    def test_first_service_start_after_past_slots_never_runs_catchup(self):
        now = datetime(2026, 10, 31, 21)
        self.assertIsNone(next_due(self.runtime, self.settings, now))
        target = next_appointment(self.runtime, self.settings, now)
        self.assertEqual((target['platform'], target['at']), ('android', '2026-11-01T17:00:00'))
        self.assertEqual(self.runtime.schedule['sample:evening:2026-10-31']['state'], 'waiting_future')
        self.assertEqual(self.runtime.schedule['sample:ios_daily:2026-10-31']['state'], 'waiting_future')

    def test_armed_future_slot_becomes_due_without_redrawing(self):
        self.arm()
        due = next_due(self.runtime, self.settings, datetime(2026, 10, 31, 17, 1))
        self.assertEqual((due['platform'], due['plan'], due['at']), ('android', 'daytime', '2026-10-31T17:00:00'))

    def test_due_platform_priority_and_future_appointment_order_are_independent(self):
        self.arm()
        finish(self.runtime, self.settings, 'android', datetime(2026, 10, 31, 17), attempted=datetime(2026, 10, 31, 6))
        self.settings.platform_priority = 'ios'
        self.assertEqual(next_due(self.runtime, self.settings, datetime(2026, 10, 31, 20))['platform'], 'ios')
        self.settings.evening_run_time = time(19, 45)
        earlier = AccountRoutineState()
        arm_future(earlier, self.settings, 'android', datetime(2026, 10, 31, 10))
        arm_future(earlier, self.settings, 'ios', datetime(2026, 10, 31, 10))
        self.assertEqual(next_appointment(earlier, self.settings, datetime(2026, 10, 31, 19, 30))['platform'], 'android')
        self.assertEqual(next_due(earlier, self.settings, datetime(2026, 10, 31, 20))['platform'], 'ios')

    def test_retry_does_not_change_sample_or_consume_other_platform(self):
        self.arm()
        original = dict(self.runtime.schedule['sample:evening:2026-10-31'])
        begin(self.runtime, self.settings, 'android', datetime(2026, 10, 31, 20))
        retry = finish(self.runtime, self.settings, 'android', datetime(2026, 10, 31, 20, 2), failed=True,
                       attempted=datetime(2026, 10, 31, 18))
        self.assertEqual((retry['kind'], retry['at']), ('retry', '2026-10-31T20:12:00'))
        self.assertEqual(self.runtime.schedule['sample:evening:2026-10-31']['at'], original['at'])
        self.assertEqual(self.runtime.schedule['sample:ios_daily:2026-10-31']['state'], 'pending')

    def test_retry_never_crosses_eighteen_or_six_boundary(self):
        self.arm()
        for now, attempted, expected in (
            (datetime(2026, 10, 31, 17, 55), datetime(2026, 10, 31, 6), '2026-10-31T20:00:00'),
            (datetime(2026, 11, 1, 5, 55), datetime(2026, 10, 31, 18), '2026-11-01T17:00:00'),
        ):
            with self.subTest(now=now):
                target = finish(self.runtime, self.settings, 'android', now, failed=True, attempted=attempted)
                self.assertEqual((target['kind'], target['at']), ('plan', expected))

    def test_cross_boundary_does_not_mark_the_new_period_running(self):
        self.arm()
        with self.assertRaises(ValueError):
            begin(self.runtime, self.settings, 'android', datetime(2026, 10, 31, 18), attempted=datetime(2026, 10, 31, 6))
        self.assertEqual(self.runtime.schedule['sample:evening:2026-10-31']['state'], 'pending')

    def test_cancelled_round_pauses_and_service_reenable_skips_its_old_retry(self):
        self.arm()
        finish(self.runtime, self.settings, 'android', datetime(2026, 10, 31, 20, 1), cancelled=True,
               attempted=datetime(2026, 10, 31, 18))
        self.assertEqual(self.runtime.schedule['sample:evening:2026-10-31']['state'], 'paused')
        target = arm_future(self.runtime, self.settings, 'android', datetime(2026, 10, 31, 20, 2))
        self.assertEqual(target['at'], '2026-11-01T17:00:00')

    def test_ios_has_independent_midnight_day_and_early_clock(self):
        self.settings.ios_daily_run_time = time(1)
        target = arm_future(self.runtime, self.settings, 'ios', datetime(2026, 10, 31, 20))
        self.assertEqual(target['at'], '2026-11-01T01:00:00')
        self.assertEqual(window(datetime(2026, 11, 1, 1), 'ios')[1], datetime(2026, 11, 1))
        self.assertEqual(window(datetime(2026, 10, 31, 23, 59, 59), 'ios')[2], datetime(2026, 11, 1))

    def test_ios_retry_stops_at_midnight_while_android_jade_period_remains_evening(self):
        self.arm()
        target = finish(self.runtime, self.settings, 'ios', datetime(2026, 10, 31, 23, 55),
                        failed=True, attempted=datetime(2026, 10, 31))
        self.assertEqual((target['kind'], target['at']), ('plan', '2026-11-01T20:00:00'))
        self.assertEqual(window(datetime(2026, 11, 1, 0, 5), 'android')[1], datetime(2026, 10, 31, 18))

    def test_disabling_one_platform_does_not_disable_the_other(self):
        self.arm()
        self.settings.routine_enable = False
        self.assertEqual(next_appointment(self.runtime, self.settings, datetime(2026, 10, 31, 17))['platform'], 'ios')
        self.settings.ios_daily_enable = False
        self.assertIsNone(next_appointment(self.runtime, self.settings, datetime(2026, 10, 31, 17)))


if __name__ == '__main__':
    unittest.main()
