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
                or (rule is task.I_SA_ACCOUNT_DROP_DOWN_CLOSED and state['page'] == 'sdk')
                or (rule is task.I_SA_LOGIN_FORM_APPLE and state['page'] == 'platform')
            )

        task.screenshot = Mock(side_effect=screenshot)
        task.appear = Mock(side_effect=appear)
        task.O_SA_ACCOUNT_ACCOUNT_SELECTED = SimpleNamespace(keyword='', ocr_single=Mock(return_value=recognized))
        task.O_SA_LOGIN_FORM_USER_CENTER_ACCOUNT = SimpleNamespace(keyword='', ocr_single=Mock(return_value=recognized))
        task.ui_click_until_disappear = Mock()
        task.ui_click = Mock(return_value=True)
        task._bounded_wait = Mock(return_value=True)
        task._wait_selected_account = Mock(return_value=True)
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

    def test_selected_row_with_unconfirmed_identity_never_submits_login(self):
        task=self.make_task(states=('sdk','sdk'),recognized='other@163.com')
        task._wait_selected_account.return_value=False
        self.assertFalse(task.login(AccountInfo(account='alt@163.com')))
        task.selectAccount.assert_called_once()
        task._wait_selected_account.assert_called_once()
        task._bounded_wait.assert_not_called()
        task.click.assert_not_called()

    def test_open_list_first_row_is_not_accepted_as_the_selected_identity(self):
        task=self.make_task(states=('sdk','sdk','platform','login'),recognized='alt@163.com')
        original=task.appear.side_effect
        task.appear.side_effect=lambda rule,**kwargs:False if rule is task.I_SA_ACCOUNT_DROP_DOWN_CLOSED else original(rule,**kwargs)
        self.assertTrue(task.login(AccountInfo(account='alt@163.com')))
        task.selectAccount.assert_called_once()

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

class SelectedAccountVerificationTests(unittest.TestCase):
    def make_task(self,frames):
        task=LoginAccount.__new__(LoginAccount)
        task.device=SimpleNamespace(image=object())
        state={'index':-1,'time':0,'frame':frames[0]}
        def screenshot():
            state['index']=min(state['index']+1,len(frames)-1)
            state['frame']=frames[state['index']]
        def appear(rule,**kwargs):
            sdk,closed,login,raw=state['frame']
            return ((rule is task.I_SA_NETEASE_GAME_LOGO and sdk)
                    or (rule is task.I_SA_ACCOUNT_DROP_DOWN_CLOSED and closed)
                    or (rule is task.I_SA_ACCOUNT_LOGIN_BTN and login))
        task.screenshot=Mock(side_effect=screenshot)
        task.appear=Mock(side_effect=appear)
        task.click=Mock()
        task.O_SA_ACCOUNT_ACCOUNT_SELECTED=SimpleNamespace(
            ocr_single=Mock(side_effect=lambda image:state['frame'][3]))
        return task,state

    def verify(self,task,state,info=None,timeout=2):
        with patch('tasks.Component.SwitchAccount.login_account.time.sleep',
                   side_effect=lambda n:state.update(time=state['time']+n)), \
                patch('tasks.Component.SwitchAccount.login_account.time.monotonic',side_effect=lambda:state['time']):
            return task._wait_selected_account(info or AccountInfo(account='alt@163.com'),timeout=timeout)

    def test_waits_for_dropdown_closure_stale_identity_and_two_exact_frames(self):
        task,state=self.make_task([
            (True,False,True,'alt@163.com'),(True,False,True,'alt@163.com'),
            (True,True,True,'previous@163.com'),(True,True,True,''),
            (True,True,True,'alt@163.com'),(True,True,True,'alt@163.com')])
        self.assertTrue(self.verify(task,state))
        self.assertEqual(task.screenshot.call_count,6)
        self.assertEqual(task.O_SA_ACCOUNT_ACCOUNT_SELECTED.ocr_single.call_count,4)
        task.click.assert_not_called()

    def test_transient_exact_frame_followed_by_mismatch_times_out_without_submit(self):
        task,state=self.make_task([(True,True,True,'alt@163.com'),(True,True,True,'other@163.com')])
        self.assertFalse(self.verify(task,state,timeout=1))
        self.assertEqual(task.login_failure_reason,'selected_account_verification_timeout')
        self.assertGreater(task.screenshot.call_count,1)
        task.click.assert_not_called()

    def test_similarity_and_unconfigured_q_g_confusion_do_not_verify(self):
        for text in ('alt2@163.com','a1t@163.com','glt@163.com'):
            with self.subTest(text=text):
                task,state=self.make_task([(True,True,True,text)])
                self.assertFalse(self.verify(task,state,timeout=.6))
                task.click.assert_not_called()

    def test_explicit_alias_still_requires_a_stable_closed_card(self):
        task,state=self.make_task([(True,False,True,'a1t'),(True,True,True,'a1t'),(True,True,True,'a1t')])
        self.assertTrue(self.verify(task,state,AccountInfo(account='alt@163.com',account_alias='a1t')))
        self.assertEqual(task.screenshot.call_count,3)

    def test_login_waits_after_selecting_row_before_submitting(self):
        task,state=LegacyUiSignatureTests().make_task()
        del task._wait_selected_account
        state.update(selection=False,frames=0,submit_at=None)
        def screenshot():
            if state['selection'] and state['page']=='sdk':state['frames']+=1
        def appear(rule,**kwargs):
            return ((state['page']=='sdk' and rule in (task.I_SA_NETEASE_GAME_LOGO,task.I_SA_ACCOUNT_LOGIN_BTN))
                    or (state['page']=='sdk' and state['frames']>=3 and rule is task.I_SA_ACCOUNT_DROP_DOWN_CLOSED)
                    or (state['page']=='platform' and rule in (task.I_SA_LOGIN_FORM_APPLE,task.I_SA_LOGIN_FORM_ANDROID))
                    or (state['page']=='login' and rule is task.I_CHECK_LOGIN_FORM))
        task.screenshot.side_effect=screenshot
        task.appear.side_effect=appear
        task.O_SA_ACCOUNT_ACCOUNT_SELECTED.ocr_single=lambda image:'alt@163.com' if state['frames']>=5 else 'previous@163.com'
        task.selectAccount.side_effect=lambda info:state.update(selection=True) or True
        original_click=task.click.side_effect
        def click(rule,interval=None):
            if rule is task.I_SA_ACCOUNT_LOGIN_BTN:state['submit_at']=state['frames']
            original_click(rule,interval)
        task.click.side_effect=click
        with patch('tasks.Component.SwitchAccount.login_account.time.sleep',side_effect=lambda n:state.update(time=state['time']+n)), \
                patch('tasks.Component.SwitchAccount.login_account.time.monotonic',side_effect=lambda:state['time']):
            self.assertTrue(task.login(AccountInfo(account='alt@163.com')))
        task.selectAccount.assert_called_once()
        self.assertGreaterEqual(state['submit_at'],6)


class LegacyUiSignatureTests(unittest.TestCase):
    def make_task(self,initial='sdk'):
        task=LoginAccount.__new__(LoginAccount)
        task.device=SimpleNamespace(image=object());state={'page':initial,'time':0,'clicks':[]}
        task.screenshot=Mock()
        def appear(rule,**kwargs):
            return ((state['page']=='sdk' and rule in (task.I_SA_NETEASE_GAME_LOGO,task.I_SA_ACCOUNT_LOGIN_BTN,task.I_SA_ACCOUNT_DROP_DOWN_CLOSED))
                    or (state['page']=='platform' and rule in (task.I_SA_LOGIN_FORM_APPLE,task.I_SA_LOGIN_FORM_ANDROID))
                    or (state['page']=='login' and rule is task.I_CHECK_LOGIN_FORM)
                    or (state['page']=='center' and rule is task.I_SA_SWITCH_ACCOUNT_BTN))
        task.appear=Mock(side_effect=appear)
        def click(rule,interval=None):
            state['clicks'].append(rule)
            if rule is task.I_SA_ACCOUNT_LOGIN_BTN:state['page']='platform'
            elif rule in (task.I_SA_LOGIN_FORM_APPLE,task.I_SA_LOGIN_FORM_ANDROID):state['page']='login'
            elif rule is task.I_SA_SWITCH_ACCOUNT_BTN:state['page']='sdk'
        task.click=Mock(side_effect=click)
        task.appear_then_click=Mock(side_effect=lambda rule,**kwargs:(click(rule) or True) if appear(rule) else False)
        # The live shell's old signature has no timeout keyword. This helper
        # must not be used at all by the two migrated SDK transitions.
        def legacy_ui_click(click,stop,interval=1):raise AssertionError('unbounded legacy ui_click must not run')
        task.ui_click=legacy_ui_click
        task.O_SA_ACCOUNT_ACCOUNT_SELECTED=SimpleNamespace(keyword='',ocr_single=lambda image:'alt@163.com')
        task.O_SA_LOGIN_FORM_USER_CENTER_ACCOUNT=SimpleNamespace(keyword='',ocr_single=lambda image:'other@163.com')
        task.switch_character=Mock();task.switch_svr=Mock();task.selectAccount=Mock()
        task._wait_selected_account=Mock(return_value=True)
        return task,state

    def test_sdk_submit_and_wrong_user_center_work_with_no_timeout_ui_signature(self):
        for initial in ('sdk','center'):
            with self.subTest(initial=initial):
                task,state=self.make_task(initial)
                with patch('tasks.Component.SwitchAccount.login_account.time.sleep',side_effect=lambda n:state.update(time=state['time']+n)), \
                     patch('tasks.Component.SwitchAccount.login_account.time.monotonic',side_effect=lambda:state['time']):
                    self.assertTrue(task.login(AccountInfo(account='alt@163.com')))
                self.assertIn(task.I_SA_ACCOUNT_LOGIN_BTN,state['clicks'])
                if initial=='center':self.assertIn(task.I_SA_SWITCH_ACCOUNT_BTN,state['clicks'])

    def test_bounded_wait_never_clicks_an_invisible_button_and_times_out(self):
        task,state=self.make_task('unknown')
        with patch('tasks.Component.SwitchAccount.login_account.time.sleep',side_effect=lambda n:state.update(time=state['time']+n)), \
             patch('tasks.Component.SwitchAccount.login_account.time.monotonic',side_effect=lambda:state['time']):
            self.assertFalse(task._bounded_wait(lambda:False,click=task.I_SA_ACCOUNT_LOGIN_BTN,timeout=1))
        task.click.assert_not_called()
        self.assertLess(state['time'],1.3)


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

    def test_sdk_overlay_over_courtyard_does_not_trigger_avatar_logout(self):
        task=self.make_task();task.device=SimpleNamespace(image=object())
        state={'sdk':True}
        task.appear=Mock(side_effect=lambda rule,**kwargs:state['sdk'] and rule is task.I_SA_NETEASE_GAME_LOGO)
        task.get_current_page.return_value=page_main
        task.goto_page.side_effect=lambda page:state.update(sdk=False)
        with patch('tasks.Component.SwitchAccount.switch_account.LoginHandler'):
            self.assertTrue(task.switchAccount())
        task.exitGame.assert_not_called();task.get_current_page.assert_not_called()

    def test_profile_welcome_toast_can_hide_title_without_reclicking_avatar(self):
        task=SwitchAccount.__new__(SwitchAccount)
        task.device=SimpleNamespace(image=object());task.screenshot=Mock()
        state={'page':'profile'}
        def appear(rule,**kwargs):
            return ((state['page']=='profile' and rule is task.I_SA_USER_CENTER)
                    or (state['page']=='center' and rule is task.I_SA_SWITCH_ACCOUNT_BTN)
                    or (state['page']=='sdk' and rule is task.I_SA_NETEASE_GAME_LOGO))
        task.appear=Mock(side_effect=appear)
        task.click=Mock(side_effect=lambda rule:state.update(page='center' if rule is task.I_SA_USER_CENTER else 'sdk'))
        with patch('tasks.Component.SwitchAccount.exit_game.sleep'):
            task.exitGame()
        self.assertEqual([call.args[0] for call in task.click.call_args_list],
                         [task.I_SA_USER_CENTER,task.I_SA_SWITCH_ACCOUNT_BTN])


if __name__ == '__main__':
    unittest.main()
