import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from module.exception import RequestHumanTakeover, TaskEnd
from tasks.AccountSwitch.config import AccountSwitch, AccountSwitchConfig
from tasks.AccountSwitch.script_task import JadeCooperationTask, ScriptTask
from tasks.WantedQuests.config import CooperationSelectMaskDescription, CooperationType, WantedQuestsConfig
from tasks.WantedQuests.script_task import ScriptTask as WantedQuestsTask


class AccountListTests(unittest.TestCase):
    def test_text_preserves_order_and_aliases_without_server_or_character(self):
        settings = AccountSwitchConfig(
            accounts=' 153****4444 #153★***4444\r\n\n example@163.com#examp1e#exarnple\nEXAMPLE@163.COM\n',
            apple_or_android=False,
        )
        accounts = settings.get_accounts()
        self.assertEqual([item.account for item in accounts], ['153****4444', 'example@163.com'])
        self.assertEqual(accounts[1].account_alias, 'examp1e#exarnple')
        self.assertTrue(accounts[0].is_account_alias('153★***4444'))
        self.assertTrue(all(item.svr == '' and not item.apple_or_android for item in accounts))
        self.assertTrue(all(item.character == '' for item in accounts))

    def test_new_configuration_is_valid_without_accounts(self):
        config = AccountSwitch()
        self.assertEqual(config.account_switch_config.get_accounts(), [])
        self.assertFalse(config.account_switch_config.auto_jade)
        self.assertNotIn('server', AccountSwitchConfig.model_fields)
        self.assertEqual(
            AccountSwitchConfig.model_json_schema()['properties']['accounts']['type'],
            'multi_line',
        )

    def test_alias_without_account_reports_its_line(self):
        with self.assertRaisesRegex(ValueError, '第 2 行'):
            AccountSwitchConfig(accounts='one@163.com\n#alias').get_accounts()

    def test_ten_accounts_ignore_blank_lines_and_duplicates_without_truncation(self):
        names = [f'alt{index:02d}@163.com' for index in range(1, 11)]
        lines = ['']
        for name in names:
            lines.extend([f'  {name}  ', '', name.upper()])
        settings = AccountSwitchConfig(accounts='\r\n'.join(lines))
        self.assertEqual([item.account for item in settings.get_accounts()], names)

    def test_account_list_is_not_limited_to_ten_entries(self):
        names = [f'alt{index:02d}@163.com' for index in range(1, 38)]
        settings = AccountSwitchConfig(accounts='\n'.join(names))
        self.assertEqual([item.account for item in settings.get_accounts()], names)


class AccountSwitchFlowTests(unittest.TestCase):
    def setUp(self):
        self.settings = AccountSwitchConfig(accounts='one@163.com\ntwo@163.com')
        self.task = ScriptTask.__new__(ScriptTask)
        self.task.config = SimpleNamespace(
            account_switch=SimpleNamespace(account_switch_config=self.settings),
            wanted_quests=SimpleNamespace(wanted_quests_config=WantedQuestsConfig()),
        )
        self.task.device = object()
        self.task.set_next_run = Mock()
        self.task.run_jade_cooperation = Mock(return_value=True)

    def run_task(self):
        with self.assertRaises(TaskEnd):
            self.task.run()

    def test_switch_only_does_not_start_wanted_quests(self):
        with patch('tasks.AccountSwitch.script_task.SwitchAccount') as switch:
            switch.return_value.switchAccount.return_value = True
            self.run_task()
        self.assertEqual([call.args[2].account for call in switch.call_args_list], ['one@163.com', 'two@163.com'])
        self.task.run_jade_cooperation.assert_not_called()
        self.task.set_next_run.assert_called_once_with('AccountSwitch', success=True, finish=True)

    def test_failed_login_skips_its_jade_and_continues(self):
        self.settings.auto_jade = True
        self.settings.invite_friend_name = '好友'
        with patch('tasks.AccountSwitch.script_task.SwitchAccount') as switch:
            switch.return_value.switchAccount.side_effect = [False, True]
            self.run_task()
        self.task.run_jade_cooperation.assert_called_once_with('好友')
        self.assertEqual(self.task.config.task_runtime_outcome['failed_accounts'], ['one@163.com'])
        self.assertEqual(self.task.config.task_runtime_outcome['completed_accounts'], ['two@163.com'])
        self.task.set_next_run.assert_called_once_with('AccountSwitch', success=False, finish=True)

    def test_all_failed_accounts_use_failure_schedule(self):
        with patch('tasks.AccountSwitch.script_task.SwitchAccount') as switch:
            switch.return_value.switchAccount.return_value = False
            self.run_task()
        self.assertEqual(self.task.config.task_runtime_outcome['status'], 'account_switch_failed')
        self.assertEqual(self.task.config.task_runtime_outcome['completed_accounts'], [])
        self.task.set_next_run.assert_called_once_with('AccountSwitch', success=False, finish=True)

    def test_auto_jade_runs_between_successful_logins_and_inherits_recipient(self):
        self.settings.auto_jade = True
        self.task.config.wanted_quests.wanted_quests_config.invite_friend_name = '主号'
        events = []
        self.task.run_jade_cooperation.side_effect = lambda name: events.append(('jade', name)) or True

        def make_switch(config, device, account):
            def login():
                events.append(('login', account.account))
                return True
            return SimpleNamespace(switchAccount=login)

        with patch('tasks.AccountSwitch.script_task.SwitchAccount', side_effect=make_switch):
            self.run_task()
        self.assertEqual(events, [
            ('login', 'one@163.com'), ('jade', '主号'),
            ('login', 'two@163.com'), ('jade', '主号'),
        ])

    def test_missing_accounts_or_recipient_does_not_start_switching(self):
        for accounts, auto_jade in [('', False), ('one@163.com', True), ('#alias', False)]:
            with self.subTest(accounts=accounts, auto_jade=auto_jade):
                self.settings.accounts = accounts
                self.settings.auto_jade = auto_jade
                with patch('tasks.AccountSwitch.script_task.SwitchAccount') as switch:
                    with self.assertRaises(RequestHumanTakeover):
                        self.task.run()
                switch.assert_not_called()

    def test_invitation_failure_is_reported_and_does_not_skip_next_account(self):
        self.settings.auto_jade = True
        self.settings.invite_friend_name = '好友'
        self.task.run_jade_cooperation.side_effect = [False, True]
        with patch('tasks.AccountSwitch.script_task.SwitchAccount') as switch:
            switch.return_value.switchAccount.return_value = True
            self.run_task()
        self.assertEqual(switch.call_count, 2)
        self.assertEqual(self.task.config.task_runtime_outcome['failed_accounts'], ['one@163.com'])
        self.task.set_next_run.assert_called_once_with('AccountSwitch', success=False, finish=True)

    def test_login_requiring_human_intervention_propagates(self):
        with patch('tasks.AccountSwitch.script_task.SwitchAccount') as switch:
            switch.return_value.switchAccount.side_effect = RequestHumanTakeover('验证码')
            with self.assertRaisesRegex(RequestHumanTakeover, '验证码'):
                self.task.run()
        self.task.set_next_run.assert_not_called()


class TenAccountFlowTests(unittest.TestCase):
    def setUp(self):
        self.names = [f'alt{index:02d}@163.com' for index in range(1, 11)]
        logger_patch = patch('tasks.AccountSwitch.script_task.logger')
        logger_patch.start()
        self.addCleanup(logger_patch.stop)

    def make_task(self, auto_jade=False):
        task = ScriptTask.__new__(ScriptTask)
        settings = AccountSwitchConfig(
            accounts='\n'.join(self.names),
            auto_jade=auto_jade,
            invite_friend_name='主号',
        )
        task.config = SimpleNamespace(
            account_switch=SimpleNamespace(account_switch_config=settings),
            wanted_quests=SimpleNamespace(wanted_quests_config=WantedQuestsConfig()),
        )
        task.device = object()
        task.set_next_run = Mock()
        task.run_jade_cooperation = Mock(return_value=True)
        return task

    def run_with_events(self, task, failed_logins=(), failed_invitations=()):
        events = []
        current = {'account': None}

        def make_switch(config, device, account):
            def login():
                current['account'] = account.account
                events.append(('login', account.account))
                return account.account not in failed_logins
            return SimpleNamespace(switchAccount=login)

        def jade(name):
            self.assertEqual(name, '主号')
            events.append(('jade', current['account']))
            return current['account'] not in failed_invitations

        task.run_jade_cooperation.side_effect = jade
        with patch('tasks.AccountSwitch.script_task.SwitchAccount', side_effect=make_switch):
            with self.assertRaisesRegex(TaskEnd, 'AccountSwitch'):
                task.run()
        return events

    def assert_outcome(self, task, failed=()):
        outcome = task.config.task_runtime_outcome
        self.assertEqual(outcome['task'], 'AccountSwitch')
        self.assertEqual(outcome['status'], 'account_switch_failed' if failed else 'completed')
        self.assertEqual(outcome['failed_accounts'], list(failed))
        self.assertEqual(outcome['completed_accounts'], [name for name in self.names if name not in failed])
        self.assertEqual(len(outcome['failed_accounts']) + len(outcome['completed_accounts']), 10)
        task.set_next_run.assert_called_once_with('AccountSwitch', success=not failed, finish=True)

    def test_ten_accounts_switch_once_each_in_text_order(self):
        task = self.make_task()
        events = self.run_with_events(task)
        self.assertEqual(events, [('login', name) for name in self.names])
        task.run_jade_cooperation.assert_not_called()
        self.assert_outcome(task)

    def test_ten_accounts_run_jade_once_between_each_successful_login(self):
        task = self.make_task(auto_jade=True)
        events = self.run_with_events(task)
        self.assertEqual(events, [event for name in self.names for event in [('login', name), ('jade', name)]])
        self.assertEqual(task.run_jade_cooperation.call_count, 10)
        self.assert_outcome(task)

    def test_failed_second_and_seventh_login_still_reach_tenth_account(self):
        failed = [self.names[1], self.names[6]]
        for auto_jade in [False, True]:
            with self.subTest(auto_jade=auto_jade):
                task = self.make_task(auto_jade=auto_jade)
                events = self.run_with_events(task, failed_logins=failed)
                expected = []
                for name in self.names:
                    expected.append(('login', name))
                    if auto_jade and name not in failed:
                        expected.append(('jade', name))
                self.assertEqual(events, expected)
                self.assertEqual(task.run_jade_cooperation.call_count, 8 if auto_jade else 0)
                self.assert_outcome(task, failed)

    def test_failed_second_and_seventh_invitation_still_run_all_ten_accounts(self):
        failed = [self.names[1], self.names[6]]
        task = self.make_task(auto_jade=True)
        events = self.run_with_events(task, failed_invitations=failed)
        self.assertEqual(events, [event for name in self.names for event in [('login', name), ('jade', name)]])
        self.assertEqual(task.run_jade_cooperation.call_count, 10)
        self.assert_outcome(task, failed)


class JadeCooperationTests(unittest.TestCase):
    def make_task(self, source=None):
        source = source or WantedQuestsConfig()
        config = SimpleNamespace(wanted_quests=SimpleNamespace(wanted_quests_config=source))
        with patch.object(WantedQuestsTask, '__init__', return_value=None):
            task = JadeCooperationTask(config, object(), '主号')
        task.ui_click = Mock()
        return task

    def test_preserves_battle_settings_without_mutating_wanted_quests(self):
        source = WantedQuestsConfig(invite_friend_name='原好友', battle_priority='秘闻 > 探索', unwanted_boss_names='阎魔')
        first = self.make_task(source)
        second = self.make_task(source)
        config = first.get_config()
        self.assertTrue(config.cooperation_only)
        self.assertEqual(config.cooperation_type, CooperationSelectMaskDescription.JadeOnly)
        self.assertEqual(config.battle_priority, '秘闻 > 探索')
        self.assertEqual(config.unwanted_boss_names, '阎魔')
        self.assertEqual(config.invite_friend_name, '主号')
        self.assertFalse(source.cooperation_only)
        self.assertEqual(source.invite_friend_name, '原好友')
        first.wq_executed_set.add('first account only')
        first.unwanted_boss_name_list.append('first account only')
        self.assertEqual(second.wq_executed_set, set())
        self.assertEqual(second.unwanted_boss_name_list, [])

    def test_jade_is_found_after_another_cooperation_type(self):
        task = self.make_task()
        jade = {'type': CooperationType.Jade, 'inviteBtn': object()}
        with patch.object(WantedQuestsTask, 'get_cooperation_info', return_value=[
            {'type': CooperationType.Gold}, jade, {'type': CooperationType.Food},
        ]):
            self.assertEqual(task.get_cooperation_info(), [jade])

    def test_no_jade_stops_before_the_shared_battle_loop(self):
        task = self.make_task()
        with patch.object(task, 'get_cooperation_info', return_value=[]):
            with self.assertRaises(TaskEnd):
                task.all_cooperation_invite()
        task.ui_click.assert_not_called()
        self.assertFalse(task.invitation_failed)

    def test_clears_tracking_before_inviting_and_marks_failed_invitation(self):
        task = self.make_task()
        events = []
        task.ui_click.side_effect = lambda click, stop, **kw: events.append(('clear', click))

        def invite(name):
            events.append(('invite', name))
            return [{'type': CooperationType.Jade, 'inviteResult': False}]

        with patch.object(task, 'get_cooperation_info', return_value=[{'type': CooperationType.Jade}]), \
                patch.object(WantedQuestsTask, 'all_cooperation_invite', side_effect=invite):
            with self.assertRaises(TaskEnd):
                task.all_cooperation_invite('主号')
        self.assertEqual(events, [('clear', task.I_TRACE_ENABLE), ('clear', task.I_TRACE_DISABLE), ('invite', '主号')])
        self.assertTrue(task.invitation_failed)

    def test_jade_does_not_change_wanted_quests_schedule(self):
        task = self.make_task()
        task.set_next_run = Mock()
        task.next_run()
        task.set_next_run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
