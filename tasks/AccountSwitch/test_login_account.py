import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from module.exception import GamePageUnknownError
from tasks.Component.SwitchAccount.login_account import LoginAccount
from tasks.Component.SwitchAccount.switch_account import SwitchAccount
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.GameUi.page import page_login, page_main


class AccountRecognitionTests(unittest.TestCase):
    def test_empty_or_malformed_account_never_matches_another_account(self):
        for account in ['', '   ', '@163.com']:
            with self.subTest(account=account):
                self.assertFalse(AccountInfo(account=account).is_account_alias('other@163.com'))

    def test_mailbox_matching_ignores_ocr_spaces_and_case(self):
        account = AccountInfo(account='Example@163.com')
        self.assertTrue(account.is_account_alias(' E X A M P L E @163.com '))
        self.assertTrue(account.is_account_alias('EXAMPLE'))
        self.assertFalse(account.is_account_alias(''))

    def test_another_mailbox_with_the_same_prefix_does_not_match(self):
        account = AccountInfo(account='example@163.com')
        for other in ['example2@163.com', 'example2', 'example@126.com']:
            with self.subTest(other=other):
                self.assertFalse(account.is_account_alias(other))

    def test_explicit_ocr_alias_is_normalized_and_empty_alias_ignored(self):
        account = AccountInfo(account='example@163.com', account_alias=' # EXAMP1E ##exarnple')
        self.assertTrue(account.is_account_alias(' examp1e '))
        self.assertTrue(account.is_account_alias('exarnple'))
        self.assertFalse(account.is_account_alias('unrelated'))


class AccountLoginTests(unittest.TestCase):
    def make_task(self, states=('login', 'user_center', 'login', 'login'), recognized='alt@163.com'):
        task = LoginAccount.__new__(LoginAccount)
        task.device = SimpleNamespace(image=object())
        frames = iter(states)
        state = {'page': None}

        def screenshot():
            try:
                state['page'] = next(frames)
            except StopIteration as exc:
                raise AssertionError('Login did not finish after the simulated frames') from exc

        def appear(rule, **kwargs):
            return (
                (rule is task.I_CHECK_LOGIN_FORM and state['page'] == 'login')
                or (rule is task.I_SA_SWITCH_ACCOUNT_BTN and state['page'] == 'user_center')
                or (rule is task.I_SA_NETEASE_GAME_LOGO and state['page'] == 'sdk')
                or (rule is task.I_SA_LOGIN_FORM_APPLE and state['page'] == 'platform')
            )

        task.screenshot = Mock(side_effect=screenshot)
        task.appear = Mock(side_effect=appear)
        task.O_SA_ACCOUNT_ACCOUNT_SELECTED = SimpleNamespace(keyword='', ocr_single=Mock(return_value=recognized))
        task.O_SA_LOGIN_FORM_USER_CENTER_ACCOUNT = SimpleNamespace(keyword='', ocr_single=Mock(return_value=recognized))
        task.ui_click_until_disappear = Mock()
        task.ui_click = Mock(return_value=True)
        task.click = Mock()
        task.switch_character = Mock(return_value=True)
        task.switch_svr = Mock(return_value=True)
        task.selectAccount = Mock(return_value=True)
        return task

    def test_account_only_uses_the_remembered_server_without_character_ocr(self):
        task = self.make_task()
        self.assertTrue(task.login(AccountInfo(account='alt@163.com')))
        task.switch_character.assert_not_called()
        task.switch_svr.assert_not_called()

    def test_account_only_ignores_stale_server_value_without_selecting_server(self):
        task = self.make_task()
        self.assertTrue(task.login(AccountInfo(account='alt@163.com', svr='旧配置区服')))
        task.switch_svr.assert_not_called()
        task.switch_character.assert_not_called()

    def test_existing_character_selection_is_preserved(self):
        task = self.make_task()
        self.assertTrue(task.login(AccountInfo(account='alt@163.com', character='指定角色', svr='角色区服')))
        task.switch_character.assert_called_once_with('指定角色')
        task.switch_svr.assert_not_called()

    def test_existing_character_failure_can_fall_back_to_server(self):
        task = self.make_task()
        task.switch_character.return_value = False
        self.assertTrue(task.login(AccountInfo(account='alt@163.com', character='指定角色', svr='角色区服')))
        task.switch_svr.assert_called_once_with('角色区服')

    def test_sdk_recognized_alias_does_not_select_another_account(self):
        task = self.make_task(states=('sdk', 'sdk', 'platform', 'login'), recognized='A LT')
        self.assertTrue(task.login(AccountInfo(account='alt@163.com')))
        task.selectAccount.assert_not_called()

    def test_account_missing_from_sdk_does_not_submit_login(self):
        task = self.make_task(states=('sdk', 'sdk'), recognized='different@163.com')
        task.selectAccount.return_value = False
        self.assertFalse(task.login(AccountInfo(account='alt@163.com')))
        task.ui_click.assert_not_called()

    def test_platform_choice_uses_shared_android_setting(self):
        for android in [True, False]:
            with self.subTest(android=android):
                task = self.make_task(states=('sdk', 'sdk', 'platform', 'login'))
                self.assertTrue(task.login(AccountInfo(account='alt@163.com', apple_or_android=android)))
                expected = task.I_SA_LOGIN_FORM_ANDROID if android else task.I_SA_LOGIN_FORM_APPLE
                task.ui_click_until_disappear.assert_called_once_with(expected)

    def test_unknown_screen_does_not_start_login(self):
        task = self.make_task(states=('unknown',))
        self.assertFalse(task.login(AccountInfo(account='alt@163.com')))
        task.switch_character.assert_not_called()
        task.selectAccount.assert_not_called()

    def test_unreadable_account_list_finishes_after_bounded_scrolling(self):
        task = LoginAccount.__new__(LoginAccount)
        task.device = SimpleNamespace(image=object())
        task.screenshot = Mock()
        task.appear = Mock(return_value=False)
        task.O_SA_ACCOUNT_ACCOUNT_SELECTED = SimpleNamespace(keyword='')
        task.O_SA_ACCOUNT_ACCOUNT_LIST = SimpleNamespace(keyword='', detect_and_ocr=Mock(return_value=[]))
        task.swipe = Mock()
        with patch('tasks.Component.SwitchAccount.login_account.time.sleep'):
            self.assertFalse(task.selectAccount(AccountInfo(account='missing@163.com')))
        self.assertGreater(task.swipe.call_count, 0)
        self.assertLessEqual(task.swipe.call_count, 50)

class SdkAccountScrollTests(unittest.TestCase):
    def make_task(self, initial_page=0, miss_top_target_once=False, target_index=0,
                  account_count=10, simulate_cooldown=False):
        task = LoginAccount.__new__(LoginAccount)
        task.device = SimpleNamespace(image=object())
        task.screenshot = Mock()
        task.click = Mock()
        names = [f'alt{index:02d}@163.com' for index in range(1, account_count + 1)]
        pages = [names[start:start + 3] for start in range(0, len(names), 3)]
        state = {'page': initial_page, 'top_visits': 0}
        visited = []
        directions = []
        cooling_rules = set()

        def scan(image):
            visited.append(state['page'])
            visible = pages[state['page']]
            if state['page'] == 0:
                state['top_visits'] += 1
                if miss_top_target_once and state['top_visits'] == 1:
                    visible = visible[1:]
            return [SimpleNamespace(
                ocr_text=name,
                box=((0, index * 70), (180, index * 70), (180, index * 70 + 30), (0, index * 70 + 30)),
            ) for index, name in enumerate(visible)]

        def swipe(rule, interval=None):
            # With an interval longer than the account scan's animation wait,
            # BaseTask.swipe can swallow the next drag. Keep the viewport on
            # the same page when that happens, just as a real SDK list would.
            if simulate_cooldown and interval is not None:
                if rule.name in cooling_rules:
                    cooling_rules.remove(rule.name)
                    return False
                cooling_rules.add(rule.name)
            # Model the SDK response to a physical drag, independently of how
            # selectAccount names its rules or divides its search into retries.
            start_y = rule.roi_front[1] + rule.roi_front[3] / 2
            end_y = rule.roi_back[1] + rule.roi_back[3] / 2
            if start_y == end_y:
                raise AssertionError('A horizontal swipe cannot scroll the account list')
            direction = 1 if end_y < start_y else -1
            directions.append(direction)
            state['page'] = min(max(state['page'] + direction, 0), len(pages) - 1)
            return True

        task.appear = Mock(side_effect=lambda rule: (
            rule is task.I_SA_ACCOUNT_DROP_DOWN_ADD_ACCOUNT and state['page'] == len(pages) - 1
        ))
        task.O_SA_ACCOUNT_ACCOUNT_SELECTED = SimpleNamespace(keyword='')
        task.O_SA_ACCOUNT_ACCOUNT_LIST = SimpleNamespace(
            keyword='', roi=(460, 280, 440, 330), area=None, detect_and_ocr=Mock(side_effect=scan),
        )
        task.swipe = Mock(side_effect=swipe)
        return task, names[target_index], visited, directions

    def test_tenth_account_can_be_found_after_scanning_four_sdk_pages(self):
        task, target, visited, directions = self.make_task(target_index=9)
        with patch('tasks.Component.SwitchAccount.login_account.time.sleep'):
            self.assertTrue(task.selectAccount(AccountInfo(account=target)))
        self.assertEqual(list(dict.fromkeys(visited)), [0, 1, 2, 3])
        self.assertGreaterEqual(task.swipe.call_count, 3)
        task.click.assert_called_once_with(task.O_SA_ACCOUNT_ACCOUNT_LIST)
        self.assertEqual(task.O_SA_ACCOUNT_ACCOUNT_LIST.area, [460, 280, 180, 30])

    def test_ninth_account_is_found_when_swipe_cooldowns_can_leave_the_same_page_visible(self):
        task, target, visited, directions = self.make_task(
            target_index=8, account_count=9, simulate_cooldown=True,
        )
        with patch('tasks.Component.SwitchAccount.login_account.time.sleep'):
            self.assertTrue(task.selectAccount(AccountInfo(account=target)))
        self.assertEqual(list(dict.fromkeys(visited)), [0, 1, 2])
        task.click.assert_called_once_with(task.O_SA_ACCOUNT_ACCOUNT_LIST)
        self.assertEqual(task.O_SA_ACCOUNT_ACCOUNT_LIST.area, [460, 420, 180, 30])

    def test_tenth_account_is_found_despite_the_same_swipe_cooldown_behavior(self):
        task, target, visited, directions = self.make_task(target_index=9, simulate_cooldown=True)
        with patch('tasks.Component.SwitchAccount.login_account.time.sleep'):
            self.assertTrue(task.selectAccount(AccountInfo(account=target)))
        self.assertEqual(list(dict.fromkeys(visited)), [0, 1, 2, 3])
        task.click.assert_called_once_with(task.O_SA_ACCOUNT_ACCOUNT_LIST)
        self.assertEqual(task.O_SA_ACCOUNT_ACCOUNT_LIST.area, [460, 280, 180, 30])

    def test_top_account_missed_by_ocr_is_found_on_reverse_scan_from_bottom(self):
        task, target, visited, directions = self.make_task(miss_top_target_once=True)
        with patch('tasks.Component.SwitchAccount.login_account.time.sleep'):
            self.assertTrue(task.selectAccount(AccountInfo(account=target)))
        self.assertEqual(visited[0], 0)
        bottom_visit = visited.index(3)
        self.assertIn(0, visited[bottom_visit + 1:])
        self.assertIn(-1, directions)
        task.click.assert_called_once_with(task.O_SA_ACCOUNT_ACCOUNT_LIST)

    def test_search_starting_at_bottom_can_find_an_account_at_top(self):
        task, target, visited, directions = self.make_task(initial_page=3)
        with patch('tasks.Component.SwitchAccount.login_account.time.sleep'):
            self.assertTrue(task.selectAccount(AccountInfo(account=target)))
        self.assertEqual(visited[0], 3)
        self.assertEqual(visited[-1], 0)
        self.assertIn(-1, directions)
        task.click.assert_called_once_with(task.O_SA_ACCOUNT_ACCOUNT_LIST)


class SwitchCompletionTests(unittest.TestCase):
    def make_task(self, character=''):
        task = SwitchAccount.__new__(SwitchAccount)
        task.config = object()
        task.device = object()
        task.to_account_info = AccountInfo(account='alt@163.com', character=character, svr='共同区服')
        task.get_current_page = Mock(return_value=page_login)
        task.goto_page = Mock(return_value=True)
        task.exitGame = Mock()
        task.login = Mock(return_value=True)
        task.screenshot = Mock()
        task.handle_bind_phone_popup = Mock()
        return task

    def test_recovery_receives_character_instead_of_server_and_verifies_courtyard(self):
        for character in ['', '指定角色']:
            with self.subTest(character=character):
                task = self.make_task(character)
                events = []
                task.goto_page.side_effect = lambda page: events.append(('page', page)) or True
                with patch('tasks.Component.SwitchAccount.switch_account.LoginHandler') as service:
                    service.return_value.app_handle_login.side_effect = lambda: events.append(('login',))
                    self.assertTrue(task.switchAccount())
                service.return_value.set_specific_usr.assert_called_once_with(character)
                self.assertEqual(events, [('login',), ('page', page_main)])

    def test_failed_account_selection_does_not_start_login_recovery(self):
        task = self.make_task()
        task.login.return_value = False
        with patch('tasks.Component.SwitchAccount.switch_account.LoginHandler') as service:
            self.assertFalse(task.switchAccount())
        service.assert_not_called()
        task.goto_page.assert_not_called()

    def test_courtyard_failure_propagates_instead_of_claiming_switch_success(self):
        task = self.make_task()
        task.goto_page.side_effect = GamePageUnknownError('not in courtyard')
        with patch('tasks.Component.SwitchAccount.switch_account.LoginHandler'):
            with self.assertRaises(GamePageUnknownError):
                task.switchAccount()

    def test_switching_from_courtyard_exits_the_current_account_first(self):
        task = self.make_task()
        task.get_current_page.return_value = page_main
        events = []
        task.exitGame.side_effect = lambda: events.append('exit')
        task.login.side_effect = lambda info: events.append('login') or True
        with patch('tasks.Component.SwitchAccount.switch_account.LoginHandler'):
            self.assertTrue(task.switchAccount())
        self.assertEqual(events, ['exit', 'login'])


if __name__ == '__main__':
    unittest.main()
