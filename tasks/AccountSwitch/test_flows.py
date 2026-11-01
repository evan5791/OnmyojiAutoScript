from datetime import date, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from pydantic import BaseModel

from module.exception import GameStuckError, TaskEnd
from tasks.AccountSwitch.flows import (
    AccountAwakeningTask, AccountDailyTask, AwakeningResult, IsolatedConfig,
    _RoutineDailyConfig, click_until_gone, run_awakening, run_daily, wait_for,
    battle_wait_options,
)
from tasks.AccountSwitch.config import AccountSwitch, DoneRecord
from tasks.DailyTrifles.config import DailyTrifles
from tasks.DailyTrifles.script_task import ScriptTask as DailyTask
from tasks.EvoZone.config import EvoZone, Layer, UserStatus
from tasks.AccountSwitch.assist import AssistSelection,AssistProof,AssistError


def make_config():
    daily = _RoutineDailyConfig()
    daily.trifles_config.courtyard_affairs = False
    daily.trifles_config.pickup_email = True
    evo = EvoZone()
    model = SimpleNamespace(daily_trifles=daily, evo_zone=evo)
    return SimpleNamespace(model=model, daily_trifles=daily, evo_zone=evo,
                           save=Mock(), task_delay=Mock(), task_call=Mock())


class DailyFlowTests(unittest.TestCase):
    def test_plain_upstream_daily_model_gets_only_private_optional_harvest_fields(self):
        config = make_config()
        plain = DailyTrifles()
        self.assertFalse(hasattr(plain, 'done_record'))
        self.assertFalse(hasattr(plain.trifles_config, 'pickup_email'))
        config.daily_trifles = config.model.daily_trifles = plain
        config.account_switch = AccountSwitch()
        options = config.account_switch.account_switch_config
        options.daily_courtyard_affairs = False
        options.daily_pickup_email = False
        before = plain.model_dump()

        def make_task(isolated, device):
            self.assertFalse(isolated.daily_trifles.trifles_config.courtyard_affairs)
            self.assertFalse(isolated.daily_trifles.trifles_config.pickup_email)
            self.assertIsInstance(isolated.daily_trifles.done_record, DoneRecord)
            return SimpleNamespace(run=Mock(side_effect=TaskEnd))

        with patch('tasks.AccountSwitch.flows.AccountDailyTask', side_effect=make_task):
            result = run_daily(config, object(), DoneRecord())
        self.assertTrue(result.success)
        self.assertEqual(plain.model_dump(), before)
        self.assertFalse(hasattr(plain, 'done_record'))

    def test_existing_upstream_friend_and_luck_options_are_run_and_checkpointed(self):
        config = make_config()
        config.daily_trifles.trifles_config.pickup_email = False
        config.daily_trifles.trifles_config.friend_love = True
        config.daily_trifles.trifles_config.luck_msg = True

        def initialize(task, isolated, device):
            task.config = isolated
            task.device = device

        with patch.object(AccountDailyTask, '__init__', initialize), \
                patch.object(DailyTask, 'run_friend_love') as friend, \
                patch.object(DailyTask, 'run_luck_msg') as luck:
            result = run_daily(config, object(), DoneRecord())
        self.assertTrue(result.success)
        friend.assert_called_once()
        luck.assert_called_once()
        self.assertEqual(result.done_record.friend_love_dt.date(), datetime.now().date())
        self.assertEqual(result.done_record.luck_msg_dt.date(), datetime.now().date())

    def test_unimplemented_upstream_guild_wish_is_not_falsely_marked_done(self):
        config = make_config()
        config.daily_trifles.trifles_config.pickup_email = False
        config.daily_trifles.trifles_config.guild_wish = True

        def initialize(task, isolated, device):
            task.config = isolated
            task.device = device

        with patch.object(AccountDailyTask, '__init__', initialize):
            result = run_daily(config, object(), DoneRecord())
        self.assertFalse(result.success)
        self.assertEqual(result.pending, ('guild_wish',))

    def test_config_and_model_share_the_account_copy_without_saving_main_settings(self):
        config = make_config()
        before = config.daily_trifles.model_dump()
        progress = []
        original_record = DoneRecord()

        def make_task(isolated, device):
            self.assertIs(isolated.daily_trifles, isolated.model.daily_trifles)

            def run():
                isolated.daily_trifles.done_record.pickup_email_dt = datetime.now()
                isolated.model.daily_trifles.scheduler.next_run = datetime.now()
                isolated.save()
                isolated.task_delay('DailyTrifles', success=True)
                isolated.task_call('EvoZone')
                raise TaskEnd('DailyTrifles')
            return SimpleNamespace(run=run)

        with patch('tasks.AccountSwitch.flows.AccountDailyTask', side_effect=make_task):
            result = run_daily(config, object(), original_record, on_progress=progress.append)
        self.assertTrue(result.success)
        self.assertEqual(config.daily_trifles.model_dump(), before)
        self.assertEqual(original_record.pickup_email_dt.year, 2023)
        self.assertEqual(progress[-1].pickup_email_dt, result.done_record.pickup_email_dt)
        progress[0].pickup_email_dt = datetime(2000, 1, 1)
        self.assertNotEqual(progress[0].pickup_email_dt, result.done_record.pickup_email_dt)
        config.save.assert_not_called()
        config.task_delay.assert_not_called()
        config.task_call.assert_not_called()

    def test_another_accounts_main_done_record_does_not_skip_this_account(self):
        config = make_config()
        config.daily_trifles.done_record.pickup_email_dt = datetime.now()

        def make_task(isolated, device):
            self.assertFalse(isolated.daily_trifles.today_is_done('pickup_email'))
            return SimpleNamespace(run=Mock(side_effect=TaskEnd))

        with patch('tasks.AccountSwitch.flows.AccountDailyTask', side_effect=make_task):
            result = run_daily(config, object(), DoneRecord())
        self.assertFalse(result.success)
        self.assertEqual(result.pending, ('pickup_email',))

    def test_exception_checkpoints_completed_items_before_propagating_recovery(self):
        config = make_config()
        progress = []

        def make_task(isolated, device):
            def run():
                isolated.daily_trifles.done_record.pickup_email_dt = datetime.now()
                raise GameStuckError('interrupted later step')
            return SimpleNamespace(run=run)

        with patch('tasks.AccountSwitch.flows.AccountDailyTask', side_effect=make_task):
            with self.assertRaisesRegex(GameStuckError, 'interrupted'):
                run_daily(config, object(), DoneRecord(), on_progress=progress.append)
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0].pickup_email_dt.date(), datetime.now().date())
        self.assertEqual(config.daily_trifles.done_record.pickup_email_dt.year, 2023)
        config.save.assert_not_called()

    def test_daily_record_uses_midnight_independently_of_jade_refresh(self):
        routine_day = date(2026, 10, 31)
        daily = _RoutineDailyConfig()
        daily._routine_day = routine_day
        for completed_at, expected in [
            (datetime(2026, 10, 30, 23, 59), False),
            (datetime(2026, 10, 31, 0, 0), True),
            (datetime(2026, 10, 31, 5, 59), True),
            (datetime(2026, 10, 31, 6, 0), True),
            (datetime(2026, 10, 31, 23, 59), True),
            (datetime(2026, 11, 1, 0, 0), False),
            (datetime(2026, 11, 1, 6, 0), False),
        ]:
            with self.subTest(completed_at=completed_at):
                daily.done_record.pickup_email_dt = completed_at
                self.assertEqual(daily.today_is_done('pickup_email'), expected)

    def test_routine_day_string_is_used_for_completion_validation(self):
        config = make_config()
        record = DoneRecord(pickup_email_dt=datetime(2026, 11, 1, 0, 5))
        with patch('tasks.AccountSwitch.flows.AccountDailyTask') as task:
            task.return_value.run.side_effect = TaskEnd
            result = run_daily(config, object(), record, routine_day='2026-10-31')
        self.assertFalse(result.success)
        self.assertIn('pickup_email',result.pending)

    def test_courtyard_step_already_done_for_the_routine_is_not_repeated(self):
        task = AccountDailyTask.__new__(AccountDailyTask)
        daily = _RoutineDailyConfig()
        daily._routine_day = date(2026, 10, 31)
        daily.done_record.courtyard_affairs_dt = datetime(2026, 10, 31, 23, 59)
        task.config = SimpleNamespace(daily_trifles=daily, save=Mock())
        with patch.object(AccountDailyTask, 'harvest_courtyard_affairs') as base:
            task.run_courtyard_affairs()
        base.assert_not_called()
        task.config.save.assert_called_once()

    def test_email_checkpoint_is_not_repeated_before_parent_saves_daily_done(self):
        config = make_config()
        progress = []

        def initialize(task, isolated, device):
            task.config = isolated
            task.device = device
            task.goto_page = Mock()
            task.screenshot = Mock()

        with patch.object(AccountDailyTask, '__init__', initialize), \
                patch.object(AccountDailyTask, 'harvest_mail', return_value=True) as collect:
            first = run_daily(config, object(), DoneRecord(), on_progress=progress.append)
            # Only the account's checkpoint survived; the parent can still have
            # daily_done=False after a crash between these two separate saves.
            second = run_daily(config, object(), progress[-1], on_progress=progress.append)
        self.assertTrue(first.success)
        self.assertTrue(second.success)
        collect.assert_called_once()

    def test_pending_courtyard_step_does_not_repeat_the_completed_email_step(self):
        config = make_config()
        config.daily_trifles.trifles_config.courtyard_affairs = True

        def initialize(task, isolated, device):
            task.config = isolated
            task.device = device
            task.goto_page = Mock()
            task.screenshot = Mock()

        with patch.object(AccountDailyTask, '__init__', initialize), \
                patch.object(AccountDailyTask, 'harvest_courtyard_affairs', return_value=False), \
                patch.object(AccountDailyTask, 'harvest_mail', return_value=True) as collect:
            first = run_daily(config, object(), DoneRecord())
            second = run_daily(config, object(), first.done_record)
        self.assertEqual(first.pending, ('courtyard_affairs',))
        self.assertEqual(second.pending, ('courtyard_affairs',))
        collect.assert_called_once()

    def test_completed_email_from_before_midnight_is_skipped_for_the_same_routine(self):
        task = AccountDailyTask.__new__(AccountDailyTask)
        daily = _RoutineDailyConfig()
        daily._routine_day = date(2026, 10, 31)
        daily.done_record.pickup_email_dt = datetime(2026, 10, 31, 23, 59)
        task.config = SimpleNamespace(daily_trifles=daily, save=Mock())
        with patch.object(AccountDailyTask, 'harvest_mail') as collect:
            task.run_pickup_email()
        collect.assert_not_called()

    def test_every_completed_daily_subitem_skips_navigation_on_retry(self):
        task = AccountDailyTask.__new__(AccountDailyTask)
        daily = _RoutineDailyConfig()
        for field in DoneRecord.model_fields:
            if field.endswith('_dt'):
                setattr(daily.done_record, field, datetime.now())
        daily.done_record.guild_donate_finish = True
        daily.trifles_config.store_sign = True
        daily.trifles_config.buy_sushi_count = 1
        task.config = SimpleNamespace(daily_trifles=daily, save=Mock())
        task.goto_page = Mock(side_effect=AssertionError('A completed subitem must not navigate'))
        for method in ('run_one_summon', 'run_courtyard_affairs', 'run_pickup_email',
                       'run_friend_love', 'run_luck_msg', 'run_store', 'run_store_sign', 'run_buy_sushi'):
            with self.subTest(method=method):
                getattr(task, method)()
        task.goto_page.assert_not_called()

    def test_successful_friend_love_records_its_date_for_this_account(self):
        task = AccountDailyTask.__new__(AccountDailyTask)
        task.config = SimpleNamespace(daily_trifles=_RoutineDailyConfig(), save=Mock())

        with patch.object(DailyTask, 'run_friend_love') as collect:
            task.run_friend_love()
        self.assertTrue(task.config.daily_trifles.today_is_done('friend_love'))
        collect.assert_called_once()
        task.config.save.assert_called_once()

    def test_failed_summon_does_not_write_completion_marker(self):
        task = AccountDailyTask.__new__(AccountDailyTask)
        task.config = SimpleNamespace(daily_trifles=_RoutineDailyConfig(), save=Mock())
        task.goto_page = Mock()
        task._summon_once = Mock(return_value=False)
        task.back_summon_main = Mock()
        task.run_one_summon()
        self.assertFalse(task.config.daily_trifles.today_is_done('summon'))
        task.back_summon_main.assert_not_called()

    def test_failed_store_navigation_does_not_premark_sign_in_as_done(self):
        task = AccountDailyTask.__new__(AccountDailyTask)
        task.config = SimpleNamespace(daily_trifles=_RoutineDailyConfig(), save=Mock())
        task.goto_page = Mock(side_effect=GameStuckError('store unavailable'))
        with self.assertRaises(GameStuckError):
            task.run_store_sign()
        self.assertFalse(task.config.daily_trifles.today_is_done('store_sign'))

    def test_summon_without_tickets_does_not_enter_the_draw_page(self):
        task = AccountDailyTask.__new__(AccountDailyTask)
        task.device = SimpleNamespace(image=object())
        task.O_ONE_TICKET = SimpleNamespace(ocr=Mock(return_value='0'))
        with patch('tasks.AccountSwitch.flows.wait_for', return_value=True) as wait:
            self.assertFalse(task._summon_once(False))
        wait.assert_called_once()

    def test_summon_returns_after_one_successful_confirmation(self):
        task = AccountDailyTask.__new__(AccountDailyTask)
        task.device = SimpleNamespace(image=object())
        task.O_ONE_TICKET = SimpleNamespace(ocr=Mock(return_value='14'))
        task.screenshot = Mock()
        task.appear = Mock(side_effect=lambda rule, **kwargs: rule is task.I_SM_CONFIRM)
        with patch('tasks.AccountSwitch.flows.wait_for', return_value=True), \
                patch('tasks.AccountSwitch.flows.click_until_gone', return_value=True) as confirm:
            self.assertTrue(task._summon_once(False))
        confirm.assert_called_once()
        task.screenshot.assert_called_once()


class AwakeningFlowTests(unittest.TestCase):
    def setUp(self):
        for name in ('wait_for', 'click_until_gone'):
            helper = patch(f'tasks.AccountSwitch.flows.{name}', return_value=True)
            helper.start()
            self.addCleanup(helper.stop)

    def make_task(self, results):
        task = AccountAwakeningTask.__new__(AccountAwakeningTask)
        task.config = SimpleNamespace(evo_zone=EvoZone())
        task.start_time = datetime.now() - timedelta(hours=2)
        task.limit_time = timedelta(minutes=30)
        task.current_count = 999
        task.goto_page = Mock()
        task.evozone_enter = Mock(return_value=True)
        task._select_account_layer = Mock(return_value=True)
        task.check_lock = Mock()
        task.wait_until_appear = Mock(return_value=True)
        task.ui_click_until_disappear = Mock(return_value=True)
        task.run_general_battle = Mock(side_effect=results)
        return task

    def test_fourteen_settled_wins_are_required_despite_old_start_time_and_count(self):
        task = self.make_task([True] * 14)
        progress = []
        result = task.run_counted(14, progress.append)
        self.assertEqual(result, AwakeningResult(14, 14, True))
        self.assertEqual(progress, list(range(1, 15)))
        self.assertEqual(task.run_general_battle.call_count, 14)

    @unittest.skipIf(battle_wait_options is None, 'Legacy battle hooks are not installed')
    def test_legacy_battle_signature_and_global_hook_options_are_isolated(self):
        task = self.make_task([])

        def battle(config):
            # This intentionally accepts no exit_matcher argument, matching
            # upstream GeneralBattle rather than the newer local engine.
            self.assertFalse(battle_wait_options.options['preset'].preset_enable)
            self.assertFalse(battle_wait_options.options['green'].green_enable)
            self.assertFalse(battle_wait_options.options['prepare'].lock_team)
            self.assertEqual(battle_wait_options.options['completion'].check_imgs, [task.I_EVOZONE_FIRE])
            return True

        task.run_general_battle = battle
        original = battle_wait_options.options
        with battle_wait_options(preset={'preset_enable': True}, green={'green_enable': True}, prepare={'lock_team': True}):
            parent_options = battle_wait_options.options
            self.assertTrue(task.run_counted(1, None).success)
            self.assertIs(battle_wait_options.options, parent_options)
            self.assertTrue(parent_options['preset'].preset_enable)
            self.assertTrue(parent_options['prepare'].lock_team)
        self.assertIs(battle_wait_options.options, original)

    def test_lost_battle_preserves_partial_wins_and_retry_only_runs_remaining_count(self):
        first = self.make_task([True] * 4 + [False])
        progress = []
        result = first.run_counted(14, progress.append)
        self.assertFalse(result.success)
        self.assertEqual(result.completed, 4)
        self.assertEqual(result.reason, 'battle_lost')
        self.assertEqual(progress, [1, 2, 3, 4])
        retry = self.make_task([True] * 10)
        result_retry = retry.run_counted(14 - result.completed, None)
        self.assertTrue(result_retry.success)
        self.assertEqual(result.completed + result_retry.completed, 14)

    def test_interruption_keeps_wins_already_reported_to_parent(self):
        task = self.make_task([True, True, GameStuckError('network')])
        progress = []
        with self.assertRaises(GameStuckError):
            task.run_counted(14, progress.append)
        self.assertEqual(progress, [1, 2])

    def test_missing_layer_never_starts_a_battle_on_the_previous_layer(self):
        task = self.make_task([])
        task._select_account_layer.return_value = False
        result = task.run_counted(14, None)
        self.assertFalse(result.success)
        self.assertEqual(result.completed, 0)
        self.assertEqual(result.reason, 'layer_not_found')
        task.run_general_battle.assert_not_called()

    def test_adapter_disables_team_buff_soul_and_preset_changes_only_in_private_copy(self):
        config = make_config()
        config.evo_zone.evo_zone_config.soul_buff_enable = True
        config.evo_zone.invite_config.friend_1 = '主号好友'
        config.evo_zone.invite_config.friend_2 = '另一个好友'
        config.evo_zone.switch_soul_config.enable = True
        config.evo_zone.switch_soul_config.enable_switch_by_name = True
        config.evo_zone.general_battle_config.preset_enable = True
        config.evo_zone.general_battle_config.lock_team_enable = True
        before = config.evo_zone.model_dump()

        def make_task(isolated, device):
            evo = isolated.evo_zone
            self.assertIs(evo, isolated.model.evo_zone)
            self.assertEqual(evo.evo_zone_config.user_status, UserStatus.ALONE)
            self.assertEqual(evo.evo_zone_config.layer, Layer.ONE)
            self.assertFalse(evo.evo_zone_config.soul_buff_enable)
            self.assertEqual(evo.invite_config.friend_1, '')
            self.assertEqual(evo.invite_config.friend_2, '')
            self.assertFalse(evo.invite_config.default_invite)
            self.assertFalse(evo.switch_soul_config.enable)
            self.assertFalse(evo.switch_soul_config.enable_switch_by_name)
            self.assertFalse(evo.general_battle_config.preset_enable)
            self.assertFalse(evo.general_battle_config.lock_team_enable)
            isolated.task_delay('EvoZone', success=True)
            isolated.save()
            return SimpleNamespace(run_counted=Mock(return_value=AwakeningResult(14, 14, True)))

        with patch('tasks.AccountSwitch.flows.AccountAwakeningTask', side_effect=make_task):
            result = run_awakening(config, object())
        self.assertTrue(result.success)
        self.assertEqual(config.evo_zone.model_dump(), before)
        config.task_delay.assert_not_called()
        config.save.assert_not_called()

    def test_all_ten_layers_map_to_existing_evo_layer_values(self):
        config = make_config()
        for layer, expected in enumerate(Layer, 1):
            with self.subTest(layer=layer):
                with patch('tasks.AccountSwitch.flows.AccountAwakeningTask') as task:
                    run_awakening(config, object(), layer=layer, count=1)
                    self.assertEqual(task.call_args.args[0].evo_zone.evo_zone_config.layer, expected)

    def test_zero_remaining_count_does_not_touch_the_device(self):
        with patch('tasks.AccountSwitch.flows.AccountAwakeningTask') as task:
            self.assertEqual(run_awakening(make_config(), object(), count=0), AwakeningResult(0, 0, True))
        task.assert_not_called()

    def test_invalid_layer_and_count_fail_before_device_use(self):
        with patch('tasks.AccountSwitch.flows.AccountAwakeningTask') as task:
            for layer, count in [(0, 14), (11, 14), (True, 14), (1, -1), (1, True)]:
                with self.subTest(layer=layer, count=count):
                    with self.assertRaises(ValueError):
                        run_awakening(make_config(), object(), layer=layer, count=count)
        task.assert_not_called()


class ModernBattleLifecycleTests(unittest.TestCase):
    def make_task(self, won=True, timeout=-1):
        task=AwakeningFlowTests().make_task([])
        class NativeConfig(BaseModel):
            battle_timeout:int=-1
            continuous_battle:bool=True
            max_continuous:int=5
            quick_exit:bool=True
            preset_enable:bool=True
            green_enable:bool=True
            lock_team_enable:bool=True
        task.config.evo_zone.general_battle_config=NativeConfig(battle_timeout=timeout)
        def battle(config=None,buff=None,battle_key='default',exit_matcher=None):
            self.assertIs(exit_matcher,task.I_EVOZONE_FIRE)
            self.assertEqual(battle_key,'account_switch_awakening')
            self.assertFalse(config.preset_enable)
            self.assertFalse(config.green_enable)
            self.assertFalse(config.lock_team_enable)
            self.assertFalse(config.continuous_battle)
            self.assertFalse(config.quick_exit)
            self.assertEqual(config.max_continuous,0)
            self.assertGreater(config.battle_timeout,0)
            task.called_config=config
            return won
        task.run_general_battle=Mock(side_effect=battle)
        return task

    def test_modern_signature_uses_native_context_and_keeps_parent_config(self):
        task=self.make_task();original=task.config.evo_zone.general_battle_config
        wins=[]
        with patch('tasks.AccountSwitch.flows.battle_wait_options',None), \
                patch('tasks.AccountSwitch.flows.wait_for',return_value=True), \
                patch('tasks.AccountSwitch.flows.click_until_gone',return_value=True):
            result=task.run_counted(1,wins.append)
        self.assertTrue(result.success);self.assertEqual(wins,[1])
        self.assertEqual(task.called_config.battle_timeout,420)
        self.assertEqual(original.battle_timeout,-1)
        self.assertTrue(original.preset_enable);self.assertTrue(original.continuous_battle)
        self.assertTrue(original.lock_team_enable)

    def test_modern_positive_native_timeout_is_preserved(self):
        task=self.make_task(timeout=37)
        with patch('tasks.AccountSwitch.flows.battle_wait_options',None):
            self.assertTrue(task._run_account_battle(task.config.evo_zone.general_battle_config))
        self.assertEqual(task.called_config.battle_timeout,37)

    def test_win_without_return_to_challenge_is_not_checkpointed(self):
        task=self.make_task();wins=[]
        with patch('tasks.AccountSwitch.flows.battle_wait_options',None), \
                patch('tasks.AccountSwitch.flows.wait_for',side_effect=[True,False]), \
                patch('tasks.AccountSwitch.flows.click_until_gone',return_value=True):
            result=task.run_counted(1,wins.append)
        self.assertFalse(result.success);self.assertEqual(result.completed,0)
        self.assertEqual(result.reason,'battle_return_unconfirmed');self.assertEqual(wins,[])

    def test_modern_loss_does_not_increment_or_retry_inside_the_call(self):
        task=self.make_task(won=False);wins=[]
        with patch('tasks.AccountSwitch.flows.battle_wait_options',None), \
                patch('tasks.AccountSwitch.flows.wait_for',return_value=True), \
                patch('tasks.AccountSwitch.flows.click_until_gone',return_value=True):
            result=task.run_counted(14,wins.append)
        self.assertFalse(result.success);self.assertEqual(result.reason,'battle_lost')
        self.assertEqual(result.completed,0);self.assertEqual(wins,[])
        task.run_general_battle.assert_called_once()


class ModernDailyLifecycleTests(unittest.TestCase):
    def test_modern_daily_actions_are_used_when_login_harvest_was_removed(self):
        for name,harvest,record in (
                ('courtyard_affairs','harvest_courtyard_affairs','courtyard_affairs_dt'),
                ('pickup_email','harvest_mail','pickup_email_dt')):
            with self.subTest(name=name):
                task=AccountDailyTask.__new__(AccountDailyTask)
                task.config=make_config()
                before=getattr(task.config.daily_trifles.done_record,record)
                with patch.object(AccountDailyTask,harvest,None), \
                        patch.object(DailyTask,'run_'+name,create=True) as native:
                    getattr(task,'run_'+name)()
                native.assert_called_once_with()
                # Only the native implementation may confirm its completion.
                self.assertEqual(getattr(task.config.daily_trifles.done_record,record),before)
                task.config.save.assert_called_once()

    def test_missing_native_friend_love_is_left_pending(self):
        task=AccountDailyTask.__new__(AccountDailyTask)
        task.config=make_config();before=task.config.daily_trifles.done_record.friend_love_dt
        with patch.object(DailyTask,'run_friend_love',None):
            task.run_friend_love()
        self.assertEqual(task.config.daily_trifles.done_record.friend_love_dt,before)


class MigratedConfirmationTests(unittest.TestCase):
    def test_imported_partial_bool_skips_work_without_fabricating_a_done_time(self):
        config=make_config();original=DoneRecord()
        with patch('tasks.AccountSwitch.flows.AccountDailyTask') as task:
            task.return_value.run.side_effect=TaskEnd
            result=run_daily(config,object(),original,routine_day='2026-10-31',confirmed={'pickup_email':True})
        self.assertTrue(result.success)
        self.assertTrue(result.confirmed['pickup_email'])
        self.assertEqual(result.done_record.pickup_email_dt,original.pickup_email_dt)

    def test_free_daily_overrides_only_change_the_private_selected_options(self):
        config=make_config();before=config.daily_trifles.model_dump()
        config.account_switch=SimpleNamespace(account_switch_config=SimpleNamespace(
            daily_courtyard_affairs=False,daily_pickup_email=True,
            get_daily_free_options=lambda:{'friend_love':False,'luck_msg':True,'store_sign':True}))
        def factory(private,device):
            options=private.daily_trifles.trifles_config
            self.assertFalse(options.friend_love);self.assertTrue(options.luck_msg);self.assertTrue(options.store_sign)
            return SimpleNamespace(run=Mock(side_effect=TaskEnd))
        with patch('tasks.AccountSwitch.flows.AccountDailyTask',side_effect=factory):
            result=run_daily(config,object(),DoneRecord(),confirmed={'pickup_email':True,'luck_msg':True,'store_sign':True})
        self.assertTrue(result.success)
        self.assertEqual(config.daily_trifles.model_dump(),before)


class NativeAssistedAwakeningTests(unittest.TestCase):
    def setUp(self):
        self.task=AccountAwakeningTask.__new__(AccountAwakeningTask)
        self.task.config=SimpleNamespace(evo_zone=EvoZone())
        self.task.goto_page=Mock();self.task.evozone_enter=Mock(return_value=True)
        self.task._select_account_layer=Mock(return_value=True);self.task.check_lock=Mock(return_value=True)
        self.task.run_general_battle=Mock(return_value=True)
        self.task.appear=Mock(return_value=False)
        for name in ('wait_for','click_until_gone'):
            patcher=patch('tasks.AccountSwitch.flows.'+name,return_value=True)
            patcher.start();self.addCleanup(patcher.stop)
        self.selection=AssistSelection('friend',0)
        self.proof_patch=patch('tasks.AccountSwitch.flows.prepare_battle_assist',return_value=AssistProof(0,0,True))
        self.proof=self.proof_patch.start();self.addCleanup(self.proof_patch.stop)
        self.leave_patch=patch('tasks.AccountSwitch.flows.leave_preparation',return_value=True)
        self.leave=self.leave_patch.start();self.addCleanup(self.leave_patch.stop)

    def test_fourteen_skip_still_verifies_a_zero_remaining_request(self):
        self.selection=AssistSelection('friend',14);self.proof.return_value=AssistProof(14,14,False)
        result=self.task.run_counted(0,None,assist=self.selection)
        self.assertTrue(result.success);self.assertTrue(result.assist_verified);self.assertTrue(result.daily_target_reached)
        self.assertEqual(result.completed,0);self.proof.assert_called_once();self.leave.assert_called_once()
        self.task.run_general_battle.assert_not_called()

    def test_mismatched_counts_abandon_preparation_without_battle(self):
        self.proof.side_effect=AssistError('assist_count_mismatch')
        result=self.task.run_counted(14,None,assist=self.selection)
        self.assertFalse(result.success);self.assertEqual(result.completed,0)
        self.task.run_general_battle.assert_not_called();self.leave.assert_called_once()

    def test_custom_target_is_capped_before_a_fifteenth_use(self):
        self.selection=AssistSelection('friend',13);self.proof.return_value=AssistProof(13,13,True)
        wins=[];result=self.task.run_counted(100,wins.append,assist=self.selection)
        self.assertEqual(result.completed,1);self.assertTrue(result.daily_target_reached)
        self.assertEqual(wins,[1]);self.task.run_general_battle.assert_called_once()

    def test_only_real_settled_wins_checkpoint_on_partial_failure(self):
        self.proof.return_value=AssistProof(5,5,True)
        self.task.run_general_battle.side_effect=[True,False]
        wins=[];result=self.task.run_counted(9,wins.append,assist=AssistSelection('friend',5))
        self.assertFalse(result.success);self.assertEqual(result.completed,1);self.assertEqual(wins,[1])
        self.assertTrue(result.assist_verified);self.assertFalse(result.daily_target_reached)

    def test_zero_request_returns_verified_without_claiming_fourteen_or_fighting(self):
        result=self.task.run_counted(0,None,assist=self.selection)
        self.assertTrue(result.success);self.assertTrue(result.assist_verified)
        self.assertFalse(result.daily_target_reached);self.assertEqual(result.completed,0)
        self.leave.assert_called_once()
        self.task.run_general_battle.assert_not_called()

    def test_zero_request_requires_confirmed_exit_even_with_a_verified_counter(self):
        self.leave.return_value=False
        result=self.task.run_counted(0,None,assist=self.selection)
        self.assertFalse(result.success);self.assertEqual(result.reason,'assist_exit_unconfirmed')
        self.task.run_general_battle.assert_not_called()

    def test_verified_run_does_not_repeat_unlock_after_first_win(self):
        self.task.check_lock.side_effect=[True,False]
        wins=[];result=self.task.run_counted(14,wins.append,assist=self.selection)
        self.assertTrue(result.success);self.assertEqual(result.completed,14)
        self.assertEqual(wins,list(range(1,15)))
        self.task.check_lock.assert_called_once_with(False)
        self.assertEqual(self.task.run_general_battle.call_count,14)

    def test_fourteen_unlocked_battles_verify_assist_once_before_first_battle(self):
        expected_counts=[]
        def verify(task,selection):
            expected_counts.append(selection.normal_used)
            self.assertEqual(task.run_general_battle.call_count,selection.normal_used)
            self.assertEqual(task.check_lock.call_args.args,(False,))
            return AssistProof(selection.normal_used,selection.normal_used,True)
        self.proof.side_effect=verify
        wins=[]
        result=self.task.run_counted(14,wins.append,assist=self.selection)
        self.assertTrue(result.success)
        self.assertTrue(result.daily_target_reached)
        self.assertEqual((result.normal_used,result.card_used,result.completed),(0,0,14))
        self.assertEqual(expected_counts,[0])
        self.assertEqual(wins,list(range(1,15)))
        self.assertEqual(self.task.check_lock.call_count,1)
        self.assertTrue(all(call.args==(False,) for call in self.task.check_lock.call_args_list))
        self.leave.assert_not_called()

    def test_resumed_counts_and_result_keep_the_initial_baseline(self):
        self.selection=AssistSelection('friend',11,11)
        seen=[]
        def verify(task,selection):
            seen.append((selection.normal_used,selection.card_used))
            return AssistProof(selection.normal_used,selection.normal_used,True)
        self.proof.side_effect=verify
        result=self.task.run_counted(3,None,assist=self.selection)
        self.assertEqual(seen,[(11,11)])
        self.assertEqual((result.normal_used,result.card_used,result.completed),(11,11,3))
        self.assertTrue(result.daily_target_reached)

    def test_verified_proof_is_reused_for_remaining_battles(self):
        self.proof.side_effect=[AssistProof(0,0,True),AssistError('assist_count_mismatch')]
        wins=[]
        result=self.task.run_counted(14,wins.append,assist=self.selection)
        self.assertTrue(result.success)
        self.assertEqual((result.completed,result.card_used),(14,0))
        self.assertEqual(wins,list(range(1,15)))
        self.proof.assert_called_once()
        self.assertEqual(self.task.run_general_battle.call_count,14)
        self.leave.assert_not_called()

    def test_first_preparation_missing_deployment_never_starts_battle(self):
        self.proof.return_value=AssistProof(0,0,False)
        wins=[]
        result=self.task.run_counted(2,wins.append,assist=self.selection)
        self.assertFalse(result.success)
        self.assertEqual(result.reason,'assist_remaining_zero_or_undeployed')
        self.assertEqual(wins,[])
        self.task.run_general_battle.assert_not_called()

    def test_retry_invocation_requires_a_new_proof(self):
        self.task.run_general_battle.side_effect=[True,False,True]
        first=self.task.run_counted(2,None,assist=self.selection)
        self.assertFalse(first.success)
        self.proof.side_effect=AssistError('assist_count_mismatch')
        second=self.task.run_counted(1,None,assist=AssistSelection('other_friend',1))
        self.assertFalse(second.success)
        self.assertFalse(second.assist_verified)
        self.assertEqual(self.proof.call_count,2)
        self.assertEqual(self.task.run_general_battle.call_count,2)


class AwakeningLayerTests(unittest.TestCase):
    def test_layer_ocr_accepts_with_or_without_layer_suffix_and_clicks_matched_row(self):
        for text, layer in [('壹', Layer.ONE), ('拾层', Layer.TEN), ('10', Layer.TEN)]:
            with self.subTest(text=text):
                task = AccountAwakeningTask.__new__(AccountAwakeningTask)
                task.L_LAYER_LIST = SimpleNamespace(roi_back=(130, 124, 64, 581))
                task.screenshot = Mock()
                task.device = SimpleNamespace(image=object(), click=Mock())
                task.swipe = Mock()
                row = SimpleNamespace(ocr_text=text, box=((2, 10), (40, 10), (40, 35), (2, 35)))
                with patch('tasks.AccountSwitch.flows.RuleOcr') as rule:
                    rule.return_value.detect_and_ocr.return_value = [row]
                    self.assertTrue(task._select_account_layer(layer))
                task.device.click.assert_called_once_with(151, 146)
                task.swipe.assert_not_called()

    def test_empty_layer_ocr_cannot_be_interpreted_as_a_successful_zero_zero_click(self):
        task = AccountAwakeningTask.__new__(AccountAwakeningTask)
        task.screenshot = Mock()
        task.device = SimpleNamespace(image=object(), click=Mock())
        task.swipe = Mock()
        with patch('tasks.AccountSwitch.flows.RuleOcr') as rule, patch('tasks.AccountSwitch.flows.sleep'):
            rule.return_value.detect_and_ocr.return_value = []
            self.assertFalse(task._select_account_layer(Layer.ONE))
        task.device.click.assert_not_called()


class BoundedUiTests(unittest.TestCase):
    def test_disappearance_requires_a_real_click_and_confirmed_missing_frames(self):
        task = SimpleNamespace(screenshot=Mock(), appear=Mock(side_effect=[True, False, False]),
                               appear_then_click=Mock(return_value=True))
        with patch('tasks.AccountSwitch.flows.monotonic', side_effect=[0, 0, 0.1, 0.2]), \
                patch('tasks.AccountSwitch.flows.sleep'):
            self.assertTrue(click_until_gone(task, object(), confirm_frames=2))
        task.appear_then_click.assert_called_once()
        self.assertEqual(task.screenshot.call_count, 3)

    def test_an_already_missing_button_is_not_a_successful_challenge_start(self):
        task = SimpleNamespace(screenshot=Mock(), appear=Mock(return_value=False), appear_then_click=Mock())
        with patch('tasks.AccountSwitch.flows.monotonic', side_effect=[0, 0]):
            self.assertFalse(click_until_gone(task, object()))
        task.appear_then_click.assert_not_called()

    def test_wait_timeout_does_not_use_legacy_ui_methods_return_values(self):
        task = SimpleNamespace(screenshot=Mock())
        action = Mock(return_value=None)
        with patch('tasks.AccountSwitch.flows.monotonic', side_effect=[0, 0, 0.5, 1.1]), \
                patch('tasks.AccountSwitch.flows.sleep'):
            self.assertFalse(wait_for(task, lambda: False, 1, action=action))
        self.assertEqual(action.call_count, 2)


if __name__ == '__main__':
    unittest.main()
