import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from module.exception import RequestHumanTakeover
from tasks.Component.SwitchAccount.switch_account import SwitchAccount
from tasks.Restart.assets import RestartAssets

class PhonePopupTests(unittest.TestCase):
    def make_task(self, frames):
        task=SwitchAccount.__new__(SwitchAccount)
        task.device=SimpleNamespace(click_record_clear=Mock())
        state={'frame':None}; iterator=iter(frames)
        task.screenshot=Mock(side_effect=lambda: state.update(frame=next(iterator)))
        task.appear=Mock(side_effect=lambda rule: (rule is RestartAssets.I_LOGIN_LOGIN_GOTO_BIND_PHONE and state['frame']=='go') or (rule is SwitchAccount.I_ACCOUNT_CANCEL_BIND_PHONE and state['frame']=='cancel'))
        task.click=Mock()
        return task

    def test_go_then_cancel_clears_the_courtyard_overlay(self):
        task=self.make_task(['go','cancel','clear'])
        with patch('tasks.Component.SwitchAccount.switch_account.time.sleep'):
            self.assertTrue(task.handle_bind_phone_popup())
        self.assertEqual([call.args[0] for call in task.click.call_args_list],[RestartAssets.I_LOGIN_LOGIN_GOTO_BIND_PHONE,SwitchAccount.I_ACCOUNT_CANCEL_BIND_PHONE])
        task.device.click_record_clear.assert_called_once()

    def test_clear_courtyard_requires_no_popup_click(self):
        task=self.make_task(['clear'])
        self.assertFalse(task.handle_bind_phone_popup())
        task.click.assert_not_called()

    def test_unclosable_phone_popup_stops_with_a_bounded_retry(self):
        task=self.make_task(['go']*24)
        with patch('tasks.Component.SwitchAccount.switch_account.time.sleep'):
            with self.assertRaises(RequestHumanTakeover):
                task.handle_bind_phone_popup()
        self.assertEqual(task.click.call_count,24)

if __name__=='__main__':
    unittest.main()
