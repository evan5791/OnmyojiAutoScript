from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import cv2
import numpy as np

from module.exception import RequestHumanTakeover
from tasks.AccountSwitch.assets import AccountFlowAssets
from tasks.AccountSwitch.assist import AssistError,AssistSelection,OcrRow,prepare_battle_assist,leave_preparation,portrait_rule,_read_card_counter
from module.atom.image import RuleImage
from tasks.AccountSwitch.cooperation import CooperationTask,normal_usage,prepare_cooperation_assist
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
        with patch('tasks.AccountSwitch.assist.rows',side_effect=read), \
                patch('tasks.AccountSwitch.assist._read_card_counter',side_effect=lambda task,row:(self.task.used,row)):
            return prepare_battle_assist(self.task,AssistSelection('friend',normal,portrait=self.task.face),seconds=3)

    def test_original_crossed_weapons_and_yellow_verify_deployment(self):
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
        with patch.object(self.task,'screenshot',wraps=self.task.screenshot) as capture:
            with self.assertRaisesRegex(AssistError,'mismatch'):self.proof(normal=5)
        self.assertEqual(capture.call_count,3)
        self.assertEqual(self.task.events,[])

    def test_overlapping_full_ocr_13_and_3_over_15_use_the_full_tight_field(self):
        self.task.used=13
        def read(task,rule):
            left=298 if task.deployed else 259
            if rule is not task.O_ASSIST_PICKER:return [OcrRow('13/15',(left+52,634,53,19),1)]
            return [OcrRow('13',(left+52,634,26,19),1),OcrRow('3/15',(left+71,635,34,17),.91)]
        with patch('tasks.AccountSwitch.assist.rows',side_effect=read), \
                patch('tasks.AccountSwitch.assist.RuleOcr') as rule:
            rule.return_value.ocr_single.return_value='13/15'
            proof=prepare_battle_assist(self.task,AssistSelection('friend',13,portrait=self.task.face),seconds=3)
        self.assertEqual(proof.card_used,13);self.assertTrue(proof.deployed)
        self.assertEqual(sum(event[0]=='swipe' for event in self.task.events),1)
        self.assertEqual(rule.call_args_list[0].kwargs['roi'],(271,629,110,29))

    def test_numeric_13_only_anchor_recovers_complete_counter_before_card_location(self):
        self.task.deployed=True
        def read(task,rule):
            if rule is task.O_ASSIST_PICKER:return [OcrRow('13',(350,634,26,19),1)]
            return [OcrRow('13/15',(350,634,53,19),1)]
        with patch('tasks.AccountSwitch.assist.rows',side_effect=read), \
                patch('tasks.AccountSwitch.assist.RuleOcr') as rule:
            rule.return_value.ocr_single.return_value='13/15'
            proof=prepare_battle_assist(self.task,AssistSelection('friend',13),seconds=3)
        self.assertEqual(proof.card_used,13);self.assertTrue(proof.deployed)
        self.assertEqual(rule.call_args_list[0].kwargs['roi'],(310,629,110,29))
        self.assertEqual(rule.call_args.kwargs['roi'],(348,632,57,23))
        self.assertEqual(self.task.events,[])

    def test_missing_picker_counter_uses_bottom_line_detection_without_open_click(self):
        self.task.deployed=True
        calls=[]
        def read(task,rule):
            calls.append(rule)
            if rule is task.O_ASSIST_PICKER:return [OcrRow('满',(538,512,32,25),1)]
            return [OcrRow('0/15',(361,636,42,17),1)]
        with patch('tasks.AccountSwitch.assist.rows',side_effect=read), \
                patch('tasks.AccountSwitch.assist._read_card_counter',side_effect=lambda task,row:(0,row)):
            proof=prepare_battle_assist(self.task,AssistSelection('friend',0),seconds=3)
        self.assertTrue(proof.deployed)
        bands=[rule for rule in calls if rule is not self.task.O_ASSIST_PICKER]
        self.assertTrue(bands)
        self.assertTrue(all(tuple(rule.roi)==(240,615,850,50) for rule in bands))
        self.assertEqual(self.task.events,[])

    def test_two_borrowed_cards_choose_leftmost_without_any_portrait_requirement(self):
        self.task.deployed=True
        with patch('tasks.AccountSwitch.assist.rows',return_value=[
                OcrRow('6/15',(600,636,42,17),1),OcrRow('0/15',(361,636,42,17),1)]), \
                patch('tasks.AccountSwitch.assist._read_card_counter',side_effect=lambda task,row:(int(row.text.split('/')[0]),row)), \
                patch('tasks.AccountSwitch.assist.portrait_rule',side_effect=AssertionError('different shikigami must be allowed')) as portrait:
            proof=prepare_battle_assist(self.task,AssistSelection('friend',0,portrait=None),seconds=3)
        self.assertEqual(proof.card_used,0);self.assertTrue(proof.deployed)
        self.assertEqual(self.task.events,[]);portrait.assert_not_called()

    def test_yellow_positive_witness_survives_a_low_flash_frame_with_stable_weapons(self):
        self.task.deployed=True;original=self.task.screenshot;calls=[]
        def screenshot():
            calls.append(1);self.task.gold=len(calls)==1;original()
        self.task.screenshot=screenshot
        proof=self.proof()
        self.assertTrue(proof.deployed);self.assertEqual(len(calls),2)
        self.assertEqual(self.task.events,[])

    def test_already_deployed_without_yellow_witness_waits_without_dragging(self):
        self.task.deployed=True;self.task.gold=False
        with self.assertRaisesRegex(AssistError,'deployment_unconfirmed'):self.proof()
        self.assertEqual(self.task.events,[])

    def test_visible_strip_with_unconfirmed_counter_is_not_clicked_as_an_open_button(self):
        original=self.task.appear
        self.task.appear=lambda rule,**kwargs:True if rule is self.task.I_ASSIST_PREP_HINT else original(rule,**kwargs)
        with patch('tasks.AccountSwitch.assist.rows',return_value=[OcrRow('13',(311,634,26,19),1)]), \
                patch('tasks.AccountSwitch.assist._read_card_counter',return_value=None):
            with self.assertRaisesRegex(AssistError,'deployment_unconfirmed'):
                prepare_battle_assist(self.task,AssistSelection('friend',13),seconds=.7)
        self.assertEqual(self.task.events,[])

    def test_closed_preparation_character_full_at_y644_does_not_hide_the_picker(self):
        self.task.state='prep'
        with patch('tasks.AccountSwitch.assist.rows',return_value=[OcrRow('满',(538,644,32,31),1)]):
            with self.assertRaisesRegex(AssistError,'deployment_unconfirmed'):
                prepare_battle_assist(self.task,AssistSelection('friend',13),seconds=.7)
        self.assertEqual(self.task.events,[('click',self.task.C_ASSIST_OPEN_PICKER)])

    def test_levels_stats_materials_and_floating_tutorial_are_not_counter_anchors(self):
        with patch('tasks.AccountSwitch.assist.rows',return_value=[
                OcrRow('13',(510,490,25,19),1),OcrRow('11',(730,512,25,19),1),
                OcrRow('999999',(280,658,90,20),1),OcrRow('素材',(515,617,67,40),1)]), \
                patch('tasks.AccountSwitch.assist._read_card_counter') as count:
            with self.assertRaisesRegex(AssistError,'deployment_unconfirmed'):
                prepare_battle_assist(self.task,AssistSelection('friend',13),seconds=.7)
        count.assert_not_called();self.assertEqual(self.task.events,[])

    def test_transient_count_mismatch_does_not_drag_or_fail_before_stable_correct_frames(self):
        self.task.deployed=True
        def read(task,rule):return [OcrRow('13/15',(361,636,42,17),1)]
        with patch('tasks.AccountSwitch.assist.rows',side_effect=read), \
                patch('tasks.AccountSwitch.assist._read_card_counter',side_effect=[(value,OcrRow('13/15',(361,636,42,17),1)) for value in (3,13,13)]) as count:
            proof=prepare_battle_assist(self.task,AssistSelection('friend',13,portrait=self.task.face),seconds=3)
        self.assertEqual(proof.card_used,13);self.assertEqual(count.call_count,3)
        self.assertEqual(self.task.events,[])

    def test_unreadable_counter_breaks_mismatch_streak_and_never_turns_into_zero(self):
        self.task.deployed=True
        with patch('tasks.AccountSwitch.assist.rows',return_value=[OcrRow('13/15',(361,636,42,17),1)]), \
                patch('tasks.AccountSwitch.assist._read_card_counter',side_effect=[(value,OcrRow('13/15',(361,636,42,17),1)) if value is not None else None for value in (3,None,3,None,13,13)]) as count:
            proof=prepare_battle_assist(self.task,AssistSelection('friend',13,portrait=self.task.face),seconds=3)
        self.assertEqual(proof.card_used,13);self.assertEqual(count.call_count,6)
        self.assertEqual(self.task.events,[])

    def test_duplicate_fraction_detections_on_one_card_are_not_ambiguous_friends(self):
        self.task.deployed=True
        with patch('tasks.AccountSwitch.assist.rows',return_value=[
                OcrRow('13/15',(361,636,42,17),1),OcrRow('3/15',(369,635,34,17),.91)]), \
                patch('tasks.AccountSwitch.assist._read_card_counter',return_value=(13,OcrRow('13/15',(361,636,42,17),1))) as count:
            proof=prepare_battle_assist(self.task,AssistSelection('friend',13,portrait=self.task.face),seconds=3)
        self.assertEqual(proof.card_used,13);self.assertEqual(count.call_count,4)
        self.assertEqual(self.task.events,[])

    def test_unreadable_tight_counter_keeps_preparation_pending_without_dragging(self):
        with patch('tasks.AccountSwitch.assist.rows',return_value=[OcrRow('3/15',(330,635,34,17),.91)]), \
                patch('tasks.AccountSwitch.assist._read_card_counter',return_value=None):
            with self.assertRaisesRegex(AssistError,'deployment_unconfirmed'):
                prepare_battle_assist(self.task,AssistSelection('friend',13,portrait=self.task.face),seconds=.7)
        self.assertEqual(self.task.events,[])

    def test_diagnostic_phases_explain_unconfirmed_full_counter_without_repeating_static_frames(self):
        with patch('tasks.AccountSwitch.assist.rows',return_value=[OcrRow('13/15',(322,636,42,17),1)]), \
                patch('tasks.AccountSwitch.assist._read_card_counter',return_value=None), \
                patch('tasks.AccountSwitch.assist.logger.info') as log:
            with self.assertRaisesRegex(AssistError,'deployment_unconfirmed'):
                prepare_battle_assist(self.task,AssistSelection('friend',13,portrait=self.task.face),seconds=.7)
        waiting=[call for call in log.call_args_list if call.args[1]=='waiting_card']
        self.assertEqual(len(waiting),1)
        evidence=waiting[0].args[2]
        self.assertEqual(evidence['candidates'],0)
        self.assertEqual(evidence['cards'][0]['rejected'],'full_counter_unconfirmed')
        self.assertEqual(self.task.events,[])

    def test_diagnostic_phase_records_refined_counter_and_deployment_proof(self):
        self.task.deployed=True
        with patch('tasks.AccountSwitch.assist.logger.info') as log:
            proof=self.proof()
        candidates=[call.args[2] for call in log.call_args_list if call.args[1]=='proof_candidate']
        self.assertTrue(proof.deployed);self.assertEqual(candidates[-1]['stable'],2)
        self.assertTrue(candidates[-1]['weapon']);self.assertTrue(candidates[-1]['golden'])
        self.assertTrue(candidates[-1]['yellow_seen'])
        self.assertNotIn('portrait',candidates[-1]['cards'][0])

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


class CardCounterFieldTests(unittest.TestCase):
    def test_native_single_rule_reads_full_card_field_and_sets_confidence(self):
        task=SimpleNamespace(device=SimpleNamespace(image=object()))
        row=OcrRow('3/15',(330,635,34,17),.91)
        full=OcrRow('13/15',(311,634,53,19),1)
        with patch('tasks.AccountSwitch.assist.RuleOcr') as rule,patch('tasks.AccountSwitch.assist.rows',return_value=[full]):
            rule.return_value.ocr_single.return_value='13/15'
            self.assertEqual(_read_card_counter(task,row),(13,full))
        self.assertEqual(rule.call_args_list[0].kwargs['roi'],(290,630,110,27))
        self.assertEqual(rule.call_args.kwargs['roi'],(309,632,57,23))
        self.assertEqual(rule.return_value.score,.8)

    def test_missing_or_invalid_full_counter_is_unconfirmed(self):
        task=SimpleNamespace(device=SimpleNamespace(image=object()))
        row=OcrRow('3/15',(330,635,34,17),.91)
        for raw in ('','13','13/3','16/15','other'):
            with self.subTest(raw=raw),patch('tasks.AccountSwitch.assist.RuleOcr') as rule, \
                    patch('tasks.AccountSwitch.assist.rows',return_value=[OcrRow('13/15',(311,634,53,19),1)]):
                rule.return_value.ocr_single.return_value=raw
                self.assertIsNone(_read_card_counter(task,row))

    def test_neighbor_label_is_outside_tight_single_counter_and_never_stripped(self):
        task=SimpleNamespace(device=SimpleNamespace(image=object()))
        anchor=OcrRow('0/15',(318,633,49,23),1)
        with patch('tasks.AccountSwitch.assist.rows',return_value=[
                anchor,OcrRow('胜',(386,628,24,23),1)]), \
                patch('tasks.AccountSwitch.assist.RuleOcr') as rule:
            rule.return_value.ocr_single.return_value='0/15'
            self.assertEqual(_read_card_counter(task,anchor),(0,anchor))
        self.assertEqual(rule.call_args.kwargs['roi'],(316,631,53,27))
        with patch('tasks.AccountSwitch.assist.rows',return_value=[anchor]), \
                patch('tasks.AccountSwitch.assist.RuleOcr') as rule:
            rule.return_value.ocr_single.return_value='0/15胜'
            self.assertIsNone(_read_card_counter(task,anchor))

    def test_complete_single_text_requires_matching_complete_ocr_box(self):
        task=SimpleNamespace(device=SimpleNamespace(image=object()))
        row=OcrRow('13',(311,634,26,19),1)
        for found in ('13','3/15'):
            with self.subTest(found=found),patch('tasks.AccountSwitch.assist.RuleOcr') as rule, \
                    patch('tasks.AccountSwitch.assist.rows',return_value=[OcrRow(found,(311,634,53,19),1)]):
                rule.return_value.ocr_single.return_value='13/15'
                self.assertIsNone(_read_card_counter(task,row))


class CooperationCardSelectionTests(unittest.TestCase):
    def make_task(self,selected=True):
        task=CooperationTask.__new__(CooperationTask)
        frame=np.random.default_rng(214).integers(0,255,(720,1280,3),dtype=np.uint8)
        # Lending slots at the top are deliberately unrelated to the friend's
        # lower card, matching the real cooperation page's separate meanings.
        frame[150:250,440:760]=0
        icon=cv2.cvtColor(cv2.imread(task.I_ASSIST_SELECTED.file),cv2.COLOR_BGR2RGB)
        if selected:frame[362:402,595:634]=icon
        task.device=SimpleNamespace(image=frame)
        task.read_usage=Mock(return_value=0)
        task.screenshot=Mock()
        task.appear=Mock(side_effect=lambda rule:rule.match(frame))
        task.swipe=Mock(side_effect=AssertionError('exact friend is already visible'))
        task.click=Mock()
        state={'time':0.}
        for path in ('tasks.AccountSwitch.cooperation.monotonic','tasks.AccountSwitch.assist.monotonic'):
            timer=patch(path,side_effect=lambda:state['time']);timer.start();self.addCleanup(timer.stop)
        timer=patch('tasks.AccountSwitch.assist.sleep',side_effect=lambda n:state.update(time=state['time']+n))
        timer.start();self.addCleanup(timer.stop)
        def read(current,rule):
            if rule is task.O_ASSIST_PANEL:
                return [OcrRow('每日协战次数',(760,100,170,30),1),
                        OcrRow('普通副本0/15',(775,160,150,25),1)]
            if rule is task.O_ASSIST_CARDS:
                return [OcrRow('friend',(532,500,73,32),1),OcrRow('other',(765,504,50,25),1)]
            return []
        patched=patch('tasks.AccountSwitch.cooperation.rows',side_effect=read)
        patched.start();self.addCleanup(patched.stop)
        return task,frame,icon

    def test_checked_friend_is_reused_without_matching_own_lending_slots_or_clicking(self):
        task,frame,icon=self.make_task()
        with patch('tasks.AccountSwitch.assist.portrait_rule',side_effect=AssertionError('lending slots are unrelated')):
            selection=task.prepare('friend')
        self.assertEqual(selection.normal_used,0)
        self.assertIsNone(selection.card_used)
        self.assertTrue(np.array_equal(selection.portrait,frame[390:438,544:592]))
        task.click.assert_not_called();task.swipe.assert_not_called()
        self.assertEqual(task.screenshot.call_count,2)

    def test_checked_template_fits_nickname_relative_roi_instead_of_clipping_two_pixels(self):
        task,frame,icon=self.make_task()
        from tasks.AccountSwitch.assist import in_region
        self.assertIsNone(in_region(task,task.I_ASSIST_SELECTED,(583,345,55,55)))
        self.assertIsNotNone(in_region(task,task.I_ASSIST_SELECTED,(578,345,75,75)))

    def test_portrait_is_captured_from_confirmed_current_frame_not_stale_first_frame(self):
        task,frame,icon=self.make_task()
        first=frame[390:438,544:592].copy()
        current=np.random.default_rng(817).integers(0,255,(48,48,3),dtype=np.uint8)
        state={'frames':0}
        def screenshot():
            state['frames']+=1
            if state['frames']>=2:frame[390:438,544:592]=current
        task.screenshot.side_effect=screenshot
        selection=task.prepare('friend')
        self.assertFalse(np.array_equal(selection.portrait,first))
        self.assertTrue(np.array_equal(selection.portrait,current))
        task.click.assert_not_called()

    def test_unchecked_target_is_clicked_once_then_verified_on_two_fresh_frames(self):
        task,frame,icon=self.make_task(selected=False)
        def click(rule,interval=None):
            self.assertEqual(rule.name,'account_exact_assist_card')
            frame[362:402,595:634]=icon
        task.click.side_effect=click
        selection=task.prepare('friend')
        self.assertEqual(selection.friend,'friend')
        task.click.assert_called_once()
        self.assertEqual(task.screenshot.call_count,3)


class FriendScrollWaitingTests(unittest.TestCase):
    def make_task(self,card_names):
        task=CooperationTask.__new__(CooperationTask)
        task.device=SimpleNamespace(image=np.zeros((720,1280,3),np.uint8))
        task.read_usage=Mock(return_value=0)
        state={'time':0.,'drags':0,'drag_at':None,'drag_times':[],'frames':0}
        task.screenshot=Mock(side_effect=lambda:state.update(frames=state['frames']+1))
        def swipe(rule):
            state['drags']+=1
            state['drag_at']=state['time']
            state['drag_times'].append(state['time'])
        task.swipe=Mock(side_effect=swipe)
        def read(current,rule):
            if rule is task.O_ASSIST_PANEL:
                return [OcrRow('每日协战次数',(760,100,170,30),1),
                        OcrRow('普通副本0/15',(775,160,150,25),1)]
            if rule is task.O_ASSIST_CARDS:
                return [OcrRow(name,(500+index*150,460,80,25),1)
                        for index,name in enumerate(card_names(state))]
            return []
        for path in ('tasks.AccountSwitch.cooperation.monotonic','tasks.AccountSwitch.assist.monotonic'):
            clock=patch(path,side_effect=lambda:state['time'])
            clock.start();self.addCleanup(clock.stop)
        pause=patch('tasks.AccountSwitch.assist.sleep',side_effect=lambda n:state.update(time=state['time']+n))
        pause.start();self.addCleanup(pause.stop)
        for path,kwargs in (
                ('tasks.AccountSwitch.cooperation.rows',{'side_effect':read}),
                ('tasks.AccountSwitch.cooperation.in_region',{'return_value':object()}),
                ('tasks.AccountSwitch.cooperation.click_box',{})):
            patched=patch(path,**kwargs);patched.start();self.addCleanup(patched.stop)
        return task,state

    def test_delayed_old_frames_after_one_drag_wait_for_the_new_friend_page(self):
        def names(state):
            if not state['drags'] or state['time']-state['drag_at']<1.8:return ['other']
            return ['friend']
        task,state=self.make_task(names)
        result=task.prepare('friend')
        self.assertEqual(result.friend,'friend')
        task.swipe.assert_called_once()
        self.assertGreaterEqual(state['time'],1.8)
        self.assertGreater(task.screenshot.call_count,8)

    def test_stable_unchanged_page_confirms_bottom_after_wait_without_redrag(self):
        task,state=self.make_task(lambda state:['other'])
        with self.assertRaisesRegex(AssistError,'assist_friend_not_found'):
            task.prepare('friend')
        task.swipe.assert_called_once()
        self.assertGreaterEqual(state['time'],2)
        self.assertLess(state['time'],2.5)

    def test_pages_without_target_are_bounded_and_each_drag_waits_for_stability(self):
        task,state=self.make_task(lambda state:['other'+str(state['drags'])])
        with self.assertRaisesRegex(AssistError,'assist_friend_not_found'):
            task.prepare('friend')
        self.assertEqual(task.swipe.call_count,6)
        self.assertTrue(all(later-earlier>=.6 for earlier,later in zip(state['drag_times'],state['drag_times'][1:])))
        self.assertLess(state['time'],10)

    def test_empty_ocr_after_drag_is_unconfirmed_and_never_a_second_blind_drag(self):
        task,state=self.make_task(lambda state:['other'] if not state['drags'] else [])
        with self.assertRaisesRegex(AssistError,'assist_friend_scroll_unconfirmed'):
            task.prepare('friend')
        task.swipe.assert_called_once()
        self.assertLessEqual(state['time'],4.2)

    def test_cancelled_screenshot_propagates_without_more_input(self):
        task,state=self.make_task(lambda state:['other'])
        def screenshot():
            if state['drags']:raise RequestHumanTakeover('cancelled')
        task.screenshot.side_effect=screenshot
        with self.assertRaisesRegex(RequestHumanTakeover,'cancelled'):
            task.prepare('friend')
        task.swipe.assert_called_once()


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
