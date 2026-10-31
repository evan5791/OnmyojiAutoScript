import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from module.exception import TaskEnd
from tasks.AccountSwitch.jade import JadeCooperationTask, JadeResult
from tasks.Exploration.config import AutoRotate, Exploration, UserStatus
from tasks.Secret.config import Secret
from tasks.Secret.script_task import ScriptTask as SecretTask
from tasks.WantedQuests.config import CooperationType, WantedQuests
from tasks.WantedQuests.script_task import ScriptTask as WantedQuestsTask


class JadeOwnershipTests(unittest.TestCase):
    def make_task(self):
        task = JadeCooperationTask.__new__(JadeCooperationTask)
        task.invite_friend_name = '5791'
        task._pending = None
        task._on_checkpoint = None
        task._open_wanted_panel = Mock(return_value=True)
        task.get_cooperation_info = Mock(return_value=[
            {'type': CooperationType.Jade, 'inviteBtn': task.I_WQ_INVITE_2},
            {'type': CooperationType.Jade, 'inviteBtn': task.I_WQ_INVITE_3},
        ])
        task._read_card_monster = Mock(return_value='天邪鬼绿')
        task._invite_once = Mock(return_value=None)
        task._track_owned_card = Mock(return_value=True)
        task._finish_tracked_quest = Mock(return_value=JadeResult('completed'))
        return task

    def test_no_jade_does_not_reserve_the_period(self):
        task = self.make_task()
        task.get_cooperation_info.return_value = []
        result = task.run_jade()
        self.assertEqual(result.status, 'no_jade')
        self.assertIsNone(result.pending)
        task._invite_once.assert_not_called()

    def test_only_one_jade_is_selected_even_when_two_are_available(self):
        task = self.make_task()
        task.run_jade()
        task._invite_once.assert_called_once_with(2)
        task._read_card_monster.assert_called_once_with(2)
        task._finish_tracked_quest.assert_called_once_with()

    def test_confirmed_invitation_failure_does_not_keep_ownership(self):
        task = self.make_task()
        task._invite_once.return_value = JadeResult('invite_failed', reason='offline')
        result = task.run_jade()
        self.assertEqual(result.status, 'invite_failed')
        self.assertIsNone(result.pending)
        task._track_owned_card.assert_not_called()

    def test_sent_invitation_resumes_only_the_owned_task_without_resending(self):
        task = self.make_task()
        task.run_jade({'slot': 2, 'monster': '天邪鬼绿', 'stage': 'sent'})
        task.get_cooperation_info.assert_not_called()
        task._invite_once.assert_not_called()
        task._track_owned_card.assert_called_once_with()

    def test_interruption_during_send_keeps_ownership_and_never_duplicates_invitation(self):
        task = self.make_task()
        pending = {'slot': 2, 'monster': '天邪鬼绿', 'stage': 'sending'}
        result = task.run_jade(pending)
        self.assertEqual(result.status, 'incomplete')
        self.assertEqual(result.pending, pending)
        task._invite_once.assert_not_called()
        task._finish_tracked_quest.assert_not_called()

    def test_persisted_reward_confirmation_survives_restart(self):
        task = self.make_task()
        result = task.run_jade({'slot': 2, 'monster': '天邪鬼绿', 'stage': 'completed'})
        self.assertTrue(result.completed)
        task._open_wanted_panel.assert_not_called()
        task._invite_once.assert_not_called()

    def test_task_end_is_not_completion_evidence(self):
        task = self.make_task()
        task._finish_tracked_quest.side_effect = TaskEnd('unsupported battle')
        result = task.run_jade({'slot': 2, 'monster': '天邪鬼绿', 'stage': 'tracked'})
        self.assertEqual(result.status, 'incomplete')

    def test_failed_tracking_never_starts_other_quests(self):
        task = self.make_task()
        task._track_owned_card.return_value = False
        result = task.run_jade({'slot': 2, 'monster': '天邪鬼绿', 'stage': 'sent'})
        self.assertEqual(result.status, 'incomplete')
        task._finish_tracked_quest.assert_not_called()


class JadeCompletionTests(unittest.TestCase):
    def make_task(self):
        task = JadeCooperationTask.__new__(JadeCooperationTask)
        task._pending = {'slot': 1, 'monster': '天邪鬼绿', 'stage': 'tracked'}
        task._on_checkpoint = Mock()
        task.device = SimpleNamespace(image=object())
        task.screenshot = Mock()
        task.goto_page = Mock()
        task.ui_click_until_disappear = Mock()
        task.ui_get_reward = Mock()
        task.swipe = Mock()
        task.appear = Mock(return_value=False)
        task.find_wq = Mock(return_value=(-1, -1, -1, [0, 0, 0, 0]))
        return task

    def test_disappearing_or_unreadable_tracker_does_not_consume_quota(self):
        task = self.make_task()
        result = task._finish_tracked_quest()
        self.assertEqual(result.status, 'incomplete')
        self.assertEqual(result.pending['stage'], 'tracked')
        task.ui_get_reward.assert_not_called()
        task._on_checkpoint.assert_not_called()

    def test_only_a_reward_box_confirms_completion_and_is_saved_before_collection(self):
        task = self.make_task()
        task.appear.side_effect = lambda rule: rule is task.I_WQ_BOX
        events = []
        task._on_checkpoint.side_effect = lambda data: events.append(('checkpoint', data['stage']))
        task.ui_get_reward.side_effect = lambda rule: events.append(('collect', rule))
        result = task._finish_tracked_quest()
        self.assertEqual(result.status, 'completed')
        self.assertEqual(events, [('checkpoint', 'completed'), ('collect', task.I_WQ_BOX)])
        task.find_wq.assert_not_called()

    def test_a_skipped_or_unsupported_monster_is_not_counted_as_success(self):
        task = self.make_task()
        task.find_wq.return_value = (0, 1, 8, [10, 20, 30, 40])
        task.O_WQ_TEXT_ALL = SimpleNamespace(area=None)
        task.O_WQ_MONSTER_TYPE = SimpleNamespace(detect_text=Mock(return_value='天邪鬼绿'))
        task.open_wq_info = Mock(return_value=True)
        task.execute_mission = Mock(return_value=False)
        result = task._finish_tracked_quest()
        self.assertEqual(result.status, 'incomplete')
        task.execute_mission.assert_called_once_with(8)
        task._on_checkpoint.assert_not_called()


class JadeInvitationTests(unittest.TestCase):
    def make_task(self):
        task = JadeCooperationTask.__new__(JadeCooperationTask)
        task._pending = {'slot': 1, 'monster': '天邪鬼绿'}
        task._on_checkpoint = Mock()
        task._fresh_invitable_card = Mock(return_value=True)
        task._click_until = Mock(return_value=True)
        task._select_same_server_friend = Mock(return_value=True)
        task.click = Mock()
        task._wait = Mock(return_value=True)
        return task

    def test_send_checkpoint_is_persisted_before_invitation_confirmation(self):
        task = self.make_task()
        events = []
        task._on_checkpoint.side_effect = lambda value: events.append(value['stage'])
        task.click.side_effect = lambda rule: events.append('confirm')
        result = task._invite_once(1)
        self.assertIsNone(result)
        self.assertEqual(events, ['sending', 'confirm', 'sent'])

    def test_uncertain_send_keeps_a_pending_record(self):
        task = self.make_task()
        task._wait.return_value = False
        result = task._invite_once(1)
        self.assertEqual(result.status, 'incomplete')
        self.assertEqual(result.pending['stage'], 'sending')

    def test_missing_online_friend_does_not_write_a_send_checkpoint(self):
        task = self.make_task()
        task._select_same_server_friend.return_value = False
        result = task._invite_once(1)
        self.assertEqual(result.status, 'invite_failed')
        task._on_checkpoint.assert_not_called()
        self.assertNotIn(task.I_WQ_INVITE_ENSURE, [call.args[0] for call in task.click.call_args_list])

    def test_changed_card_is_rejected_before_opening_or_sending_invitation(self):
        task = self.make_task()
        task._fresh_invitable_card.return_value = False
        result = task._invite_once(1)
        self.assertEqual(result.status, 'invite_failed')
        task._click_until.assert_not_called()
        task._on_checkpoint.assert_not_called()
        task.click.assert_not_called()

    def test_jade_card_after_an_already_invited_slot_is_still_found(self):
        task = JadeCooperationTask.__new__(JadeCooperationTask)
        task.screenshot = Mock()
        present = [task.I_WQ_INVITE_2, task.I_WQ_COOPERATION_TYPE_JADE_2]
        task.appear = Mock(side_effect=lambda rule: any(rule is item for item in present))
        self.assertEqual(task.get_cooperation_info(), [
            {'type': CooperationType.Jade, 'inviteBtn': task.I_WQ_INVITE_2}
        ])

    def test_numeric_recipient_is_exact_and_cross_server_tab_is_never_used(self):
        task = JadeCooperationTask.__new__(JadeCooperationTask)
        task.invite_friend_name = '5791'
        task.device = SimpleNamespace(image=object())
        task.screenshot = Mock()
        task.click = Mock()
        task._wait = lambda predicate, timeout: predicate()
        visible = [task.I_WQ_INVITE_SAME_SVR, task.I_WQ_INVITE_FRIEND_LIST_APPEAR, task.I_WQ_INVITE_SELECTED]
        task.appear = lambda rule: any(rule is value for value in visible)
        task.O_WQ_INVITE_COLUMN_1 = SimpleNamespace(roi=(240, 185, 190, 340), detect_and_ocr=Mock(return_value=[
            SimpleNamespace(ocr_text='15791', box=[[0, 0], [20, 0], [20, 15], [0, 15]]),
            SimpleNamespace(ocr_text='5791', box=[[0, 70], [20, 70], [20, 85], [0, 85]]),
        ]))
        self.assertTrue(task._select_same_server_friend())
        clicks = [call.args[0] for call in task.click.call_args_list]
        self.assertIs(clicks[0], task.I_WQ_INVITE_SAME_SVR)
        self.assertEqual(clicks[1].roi_front, (240, 255, 20, 15))
        self.assertNotIn(task.I_WQ_INVITE_DIFF_SVR, clicks)


class JadeConfigurationTests(unittest.TestCase):
    def test_exploration_presets_and_main_account_settings_are_isolated(self):
        exploration = Exploration()
        exploration.general_battle_config.preset_enable = True
        exploration.exploration_config.user_status = UserStatus.LEADER
        exploration.exploration_config.auto_rotate = AutoRotate.yes
        exploration.switch_soul_config.enable = True
        exploration.switch_soul_config.enable_switch_by_name = True
        exploration.scrolls.scrolls_enable = True
        wanted = WantedQuests()
        secret = Secret()
        source = SimpleNamespace(
            model=SimpleNamespace(exploration=exploration, wanted_quests=wanted, secret=secret),
            exploration=exploration,
            wanted_quests=wanted,
        )

        def initialize(task, config, device):
            task.config = config
            task.device = device

        inherited_exclusions = list(WantedQuestsTask.want_strategy_excluding)
        with patch.object(WantedQuestsTask, '__init__', initialize):
            task = JadeCooperationTask(source, object(), '5791')
            second = JadeCooperationTask(source, object(), '5791')
        private = task._config  # Its inherited getter also writes lock_team_enable.
        self.assertIsNot(private, exploration)
        self.assertFalse(private.general_battle_config.preset_enable)
        self.assertEqual(private.exploration_config.user_status, UserStatus.ALONE)
        self.assertEqual(private.exploration_config.auto_rotate, AutoRotate.no)
        self.assertFalse(private.switch_soul_config.enable)
        self.assertFalse(private.switch_soul_config.enable_switch_by_name)
        self.assertFalse(private.scrolls.scrolls_enable)
        self.assertTrue(exploration.general_battle_config.preset_enable)
        self.assertFalse(exploration.general_battle_config.lock_team_enable)
        self.assertTrue(exploration.switch_soul_config.enable)
        task.config.secret.general_battle.preset_enable = True
        self.assertFalse(secret.general_battle.preset_enable)
        task.want_strategy_excluding.append('first alt only')
        self.assertEqual(second.want_strategy_excluding, [])
        self.assertEqual(WantedQuestsTask.want_strategy_excluding, inherited_exclusions)


class UpstreamApiCompatibilityTests(unittest.TestCase):
    def test_jade_keeps_upstreams_secret_battle_wait_instead_of_unrelated_task_hooks(self):
        self.assertIs(JadeCooperationTask.battle_wait, SecretTask.battle_wait)

    def test_finder_adapter_uses_upstreams_single_image_argument(self):
        task = JadeCooperationTask.__new__(JadeCooperationTask)
        image = object()
        with patch.object(WantedQuestsTask, 'find_wq', autospec=True, return_value=(0, 1, 8, [1, 2, 3, 4])) as finder:
            self.assertEqual(task.find_wq(image, allow_edge_fallback=True), (0, 1, 8, [1, 2, 3, 4]))
        finder.assert_called_once_with(task, image)

    def test_mission_adapter_matches_upstreams_three_required_arguments(self):
        task = JadeCooperationTask.__new__(JadeCooperationTask)
        task.device = SimpleNamespace(image=object())
        task.O_WQ_NUMBER = SimpleNamespace(ocr=Mock(return_value='12'))
        with patch.object(WantedQuestsTask, 'execute_mission', autospec=True, return_value=False) as execute:
            self.assertFalse(task.execute_mission(8))
        execute.assert_called_once_with(task, task.O_WQ_TEXT_ALL, 8, 12)

    def test_empty_ticket_ocr_is_safe_when_adapting_old_api(self):
        task = JadeCooperationTask.__new__(JadeCooperationTask)
        task.device = SimpleNamespace(image=object())
        task.O_WQ_NUMBER = SimpleNamespace(ocr=Mock(return_value=''))
        with patch.object(WantedQuestsTask, 'execute_mission', autospec=True) as execute:
            task.execute_mission(8)
        execute.assert_called_once_with(task, task.O_WQ_TEXT_ALL, 8, 0)

    def test_modern_single_argument_mission_delegates_without_legacy_ocr(self):
        task=JadeCooperationTask.__new__(JadeCooperationTask)
        task.O_WQ_NUMBER=SimpleNamespace(ocr=Mock(side_effect=AssertionError('native task owns ticket OCR')))
        calls=[]
        def execute(native,num_want):
            calls.append((native,num_want))
            return False
        with patch.object(WantedQuestsTask,'execute_mission',execute):
            self.assertFalse(task.execute_mission(8))
        self.assertEqual(calls,[(task,8)])
        task.O_WQ_NUMBER.ocr.assert_not_called()

    def test_modern_internal_type_error_is_not_retried_with_legacy_arguments(self):
        task=JadeCooperationTask.__new__(JadeCooperationTask)
        calls=[]
        def execute(native,num_want):
            calls.append(num_want)
            raise TypeError('native strategy failed')
        with patch.object(WantedQuestsTask,'execute_mission',execute):
            with self.assertRaisesRegex(TypeError,'native strategy failed'):task.execute_mission(8)
        self.assertEqual(calls,[8])

    def test_modern_finder_preserves_native_edge_fallback(self):
        task=JadeCooperationTask.__new__(JadeCooperationTask);image=object();calls=[]
        def finder(native,frame,*,allow_edge_fallback=False):
            calls.append((native,frame,allow_edge_fallback))
            return (0,1,8,[1,2,3,4])
        with patch.object(WantedQuestsTask,'find_wq',finder):
            self.assertEqual(task.find_wq(image,allow_edge_fallback=True),(0,1,8,[1,2,3,4]))
        self.assertEqual(calls,[(task,image,True)])

    def test_modern_detail_opening_reuses_native_flow(self):
        task=JadeCooperationTask.__new__(JadeCooperationTask);task._click_until=Mock()
        calls=[]
        def native(current):
            calls.append(current)
            return True
        with patch.object(WantedQuestsTask,'open_wq_info',native,create=True):
            self.assertTrue(task.open_wq_info())
        self.assertEqual(calls,[task])
        task._click_until.assert_not_called()

    def test_old_detail_opening_uses_local_adapter_when_native_method_is_absent(self):
        task=JadeCooperationTask.__new__(JadeCooperationTask);task._click_until=Mock(return_value=True)
        with patch.object(WantedQuestsTask,'open_wq_info',None,create=True):
            self.assertTrue(task.open_wq_info())
        task._click_until.assert_called_once_with(task.O_WQ_TEXT_ALL,task.I_TRACE_TRUE,seconds=5)

    def test_new_trace_templates_are_shipped_with_the_feature(self):
        for rule in [JadeCooperationTask.I_JADE_TRACE_ENABLE_NEW, JadeCooperationTask.I_JADE_TRACE_DISABLE_NEW]:
            self.assertTrue(Path(rule.file).is_file(), rule.file)
            self.assertIn('tasks/AccountSwitch/assets/', rule.file)

    def test_completed_only_finder_result_is_not_a_clickable_negative_progress(self):
        task = JadeCompletionTests().make_task()
        task.find_wq.return_value = (-2, -2, -2, [0, 0, 0, 0])
        task.open_wq_info = Mock()
        result = task._finish_tracked_quest()
        self.assertEqual(result.status, 'incomplete')
        task.open_wq_info.assert_not_called()


if __name__ == '__main__':
    unittest.main()
