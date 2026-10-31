from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import cv2
import numpy as np

from module.exception import RequestHumanTakeover
from tasks.AccountSwitch.assets import AccountFlowAssets
from tasks.AccountSwitch.assist import AssistError,AssistSelection,OcrRow,prepare_battle_assist,leave_preparation,portrait_rule
from module.atom.image import RuleImage
from tasks.AccountSwitch.cooperation import normal_usage,prepare_cooperation_assist
from tasks.AccountSwitch.dialogs import AccountDialogGuard
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets
from tasks.EvoZone.assets import EvoZoneAssets
from tasks.Component.SwitchAccount.login_account import exact_account_matches
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo


class NativeFake(AccountFlowAssets,GeneralBattleAssets,EvoZoneAssets):
    def __init__(self,used=0):
        self.device=SimpleNamespace(image=None)
        self.time=0;self.state='picker';self.deployed=False;self.used=used;self.events=[]
        self.gold=True;self.has_weapons=True;self.drag_ok=True
        self.face=np.random.default_rng(24).integers(0,255,(48,48,3),dtype=np.uint8)

    def screenshot(self):
        frame=np.zeros((720,1280,3),np.uint8);left=298 if self.deployed else 259
        if self.deployed and self.gold:frame[546:621,left+18:left+95]=(230,180,80)
        frame[539:587,left+36:left+84]=self.face
        if self.deployed and self.has_weapons:
            icon=cv2.cvtColor(cv2.imread(self.I_ASSIST_DEPLOYED.file),cv2.COLOR_BGR2RGB)
            frame[508:538,left+40:left+76]=icon
        self.device.image=frame

    def appear(self,rule,**kwargs):
        if rule in (self.I_PREPARE_HIGHLIGHT,self.I_EXIT):return self.state in ('prep','picker')
        if rule is self.I_ACCOUNT_EXIT_GAME_TITLE:return self.state=='exit_game'
        if rule is self.I_ACCOUNT_EXIT_BATTLE_TITLE:return self.state=='exit_battle'
        if rule is self.I_EXIT_ENSURE:return self.state=='exit_battle'
        if rule is self.I_FALSE:return self.state=='loss'
        if rule is self.I_EVOZONE_FIRE:return self.state=='challenge'
        if rule is self.I_ASSIST_PREP_HINT:return self.state=='prep'
        return rule.match(self.device.image)

    def click(self,rule,interval=None):
        self.events.append(('click',rule))
        if rule is self.I_EXIT:self.state='prep' if self.state=='picker' else 'exit_battle'
        elif rule is self.I_EXIT_ENSURE:self.state='loss'
        elif rule is self.I_FALSE:self.state='challenge'
        elif rule is self.C_ASSIST_OPEN_PICKER:self.state='picker'

    def swipe(self,rule):
        self.events.append(('swipe',rule))
        if self.drag_ok:self.deployed=True


class NativeAssistTests(unittest.TestCase):
    def setUp(self):
        self.task=NativeFake()
        self.clock=patch('tasks.AccountSwitch.assist.monotonic',side_effect=lambda:self.task.time)
        self.pause=patch('tasks.AccountSwitch.assist.sleep',side_effect=lambda n:setattr(self.task,'time',self.task.time+n))
        self.clock.start();self.pause.start();self.addCleanup(self.clock.stop);self.addCleanup(self.pause.stop)

    def proof(self,normal=0):
        def read(task,rule):
            left=298 if task.deployed else 259
            return [OcrRow(f'{task.used}/15',(left+63,636,42,17),1)] if task.state=='picker' else []
        with patch('tasks.AccountSwitch.assist.rows',side_effect=read):
            return prepare_battle_assist(self.task,AssistSelection('friend',normal,portrait=self.task.face),seconds=3)

    def test_original_rule_image_matches_runtime_portrait_and_deployment(self):
        proof=self.proof()
        self.assertTrue(proof.deployed)
        self.assertEqual(proof.card_used,0)
        self.assertEqual(sum(event[0]=='swipe' for event in self.task.events),1)
        self.assertFalse(any(event==('click',self.task.I_PREPARE_HIGHLIGHT) for event in self.task.events))

    def test_both_gold_and_local_crossed_weapons_are_required(self):
        for name in ('gold','has_weapons'):
            with self.subTest(name=name):
                self.task=NativeFake();setattr(self.task,name,False)
                with self.assertRaises(AssistError):self.proof()

    def test_count_mismatch_never_starts_a_battle_or_drags(self):
        self.task.used=6
        with self.assertRaisesRegex(AssistError,'mismatch'):self.proof(normal=5)
        self.assertEqual(self.task.events,[])

    def test_fourteen_is_verified_without_placing_or_starting_a_shikigami(self):
        self.task.used=14
        proof=self.proof(normal=14)
        self.assertEqual(proof.card_used,14)
        self.assertFalse(proof.deployed)
        self.assertEqual(self.task.events,[])

    def test_failed_drag_is_bounded(self):
        self.task.drag_ok=False
        with self.assertRaises(AssistError):self.proof()
        self.assertLessEqual(sum(event[0]=='swipe' for event in self.task.events),3)

    def test_guarded_leave_uses_two_matched_exits_then_settlement_without_wins(self):
        self.assertTrue(leave_preparation(self.task,seconds=5))
        self.assertEqual([event[1] for event in self.task.events],
                         [self.task.I_EXIT,self.task.I_EXIT,self.task.I_EXIT_ENSURE,self.task.I_FALSE])
        self.assertFalse(self.task._allow_account_battle_exit)

    def test_blank_friend_fails_before_constructing_a_task_or_guessing_a_receiver(self):
        with patch('tasks.AccountSwitch.cooperation.CooperationTask') as task:
            with self.assertRaises(AssistError):prepare_cooperation_assist(object(),object(),'')
        task.assert_not_called()

    def test_normal_count_requires_same_row_and_exact_denominator(self):
        self.assertEqual(normal_usage([OcrRow('普通副本',(780,164,95,25),1),OcrRow('5/15',(880,164,50,25),1)]),5)
        for value,box in [('5/3',(880,164,50,25)),('16/15',(880,164,50,25)),('5/15',(880,212,50,25))]:
            self.assertIsNone(normal_usage([OcrRow('普通副本',(780,164,95,25),1),OcrRow(value,box,1)]))

    def test_aliases_are_exact_in_the_actual_login_path(self):
        info=AccountInfo(account='example@163.com',account_alias='examp1e')
        self.assertTrue(exact_account_matches(info,' EXAMP1E '))
        self.assertFalse(exact_account_matches(info,'examp1e2'))
        self.assertFalse(exact_account_matches(info,'example@126.com'))

    def test_all_local_image_rules_reference_real_anonymous_templates(self):
        from module.atom.image import RuleImage
        for name,rule in vars(AccountFlowAssets).items():
            if isinstance(rule,RuleImage):
                with self.subTest(name=name):self.assertTrue(Path(rule.file).is_file())


class PortraitApiCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.portrait=np.random.default_rng(91).integers(0,255,(48,48,3),dtype=np.uint8)
        self.frame=np.zeros((720,1280,3),np.uint8)
        self.frame[180:228,200:248]=self.portrait
        self.task=SimpleNamespace(device=SimpleNamespace(image=self.frame))

    def test_legacy_matcher_is_preferred_when_both_names_exist(self):
        rule=SimpleNamespace(match_multi_scale=Mock(return_value=True),multi_scale_template_match=Mock(),match=Mock())
        with patch('tasks.AccountSwitch.assist.RuleImage',return_value=rule):
            self.assertIs(portrait_rule(self.task,self.portrait,(150,150,200,200)),rule)
        rule.match_multi_scale.assert_called_once()
        rule.multi_scale_template_match.assert_not_called();rule.match.assert_not_called()

    def test_new_shell_calls_the_local_native_matcher_and_never_image_rpc(self):
        native_class=RuleImage
        class NewShellRule:
            def __init__(self,**kwargs):self.args=kwargs
            def match(self,*args,**kwargs):raise AssertionError('runtime portrait must never use image RPC')
            def multi_scale_template_match(self,frame,threshold=None):
                assert self.scale_range==(.6,1.2,.1)
                native=native_class(**self.args);native._image=self._image
                matched=native.match_multi_scale(frame,threshold=threshold,scale_range=self.scale_range)
                self.roi_front=native.roi_front
                return matched
        with patch('tasks.AccountSwitch.assist.RuleImage',NewShellRule):
            found=portrait_rule(self.task,self.portrait,(150,150,200,200))
        self.assertIsNotNone(found)
        self.assertEqual(tuple(found.roi_front),(200,180,48,48))

    def test_unknown_native_api_fails_closed_instead_of_trying_rpc(self):
        rule=SimpleNamespace(match=Mock(side_effect=AssertionError('RPC forbidden')))
        with patch('tasks.AccountSwitch.assist.RuleImage',return_value=rule):
            with self.assertRaisesRegex(AssistError,'matcher_unavailable'):
                portrait_rule(self.task,self.portrait,(150,150,200,200))
        rule.match.assert_not_called()


class DialogGuardTests(unittest.TestCase):
    def test_exit_game_is_cancelled_before_any_underlying_page_is_returned(self):
        class FrameSource:
            def screenshot(self):self.frame=next(self.frames);return self.frame
        class Guard(AccountDialogGuard,FrameSource):
            def appear(self,rule):
                return self.frame=='exit' and rule in (self.I_ACCOUNT_EXIT_GAME_TITLE,self.I_ACCOUNT_EXIT_GAME_CANCEL)
            def click(self,rule,interval=None):self.clicked.append(rule)
        task=Guard();task.frames=iter(['exit','main']);task.clicked=[]
        with patch('tasks.AccountSwitch.dialogs.sleep'):
            self.assertEqual(task.screenshot(),'main')
        self.assertEqual(task.clicked,[task.I_ACCOUNT_EXIT_GAME_CANCEL])

    def test_positive_title_without_a_cancel_does_not_click_a_generic_confirmation(self):
        task=SimpleNamespace(appear=lambda rule:rule is AccountFlowAssets.I_ACCOUNT_EXIT_GAME_TITLE,click=Mock())
        for name in ('I_ACCOUNT_EXIT_GAME_TITLE','I_ACCOUNT_EXIT_GAME_CANCEL','I_ACCOUNT_EXIT_BATTLE_TITLE','I_ACCOUNT_EXIT_BATTLE_CANCEL'):
            setattr(task,name,getattr(AccountFlowAssets,name))
        with self.assertRaises(RequestHumanTakeover):AccountDialogGuard.dismiss_account_dialog(task)
        task.click.assert_not_called()


if __name__=='__main__':unittest.main()
