"""Borrowed-team proofs using the shared RuleImage/RuleOcr runtime."""
from copy import deepcopy
from dataclasses import dataclass, field
from time import monotonic, sleep
from unicodedata import normalize
import re
import cv2
import numpy as np

from module.atom.click import RuleClick
from module.atom.image import RuleImage
from module.atom.swipe import RuleSwipe


class AssistError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssistSelection:
    friend: str
    normal_used: int
    card_used: int | None = None
    portrait: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class AssistProof:
    normal_used: int
    card_used: int
    deployed: bool


@dataclass(frozen=True)
class OcrRow:
    text: str
    box: tuple
    score: float


def text(value):
    return ''.join(normalize('NFKC', str(value)).split())


def inside(box, region):
    x,y,w,h=box; left,top,width,height=region
    return w>0 and h>0 and left<=x and top<=y and x+w<=left+width and y+h<=top+height


def rows(task, rule):
    result=[]
    x,y,_,_=rule.roi
    for row in rule.detect_and_ocr(task.device.image):
        points=np.asarray(row.box)
        left,top=points.min(axis=0); right,bottom=points.max(axis=0)
        box=(int(x+left),int(y+top),int(right-left),int(bottom-top))
        result.append(OcrRow(text(row.ocr_text),box,float(getattr(row,'score',1))))
    return result


def frames(task, seconds=20):
    deadline=monotonic()+seconds
    for _ in range(max(1,int(seconds/.15)+1)):
        if monotonic()>=deadline: return
        task.screenshot()
        yield
        sleep(.15)


def click_box(task, box, name):
    task.click(RuleClick(roi_front=box,roi_back=box,name=name))


def in_region(task, rule, region):
    local=deepcopy(rule);local.roi_back=region
    return local if task.appear(local) else None


def portrait_rule(task, portrait, region):
    if not isinstance(portrait,np.ndarray) or portrait.shape[:2]!=(48,48):
        raise AssistError('assist_portrait_missing')
    gray=cv2.cvtColor(portrait,cv2.COLOR_RGB2GRAY)
    if float(gray.std())<12: raise AssistError('assist_portrait_ambiguous')
    gray_frame=cv2.cvtColor(task.device.image,cv2.COLOR_RGB2GRAY)
    rule=RuleImage(roi_front=(0,0,48,48),roi_back=region,threshold=.84,
                   method='Template matching',file='account_runtime_portrait.png')
    rule._image=cv2.cvtColor(gray,cv2.COLOR_GRAY2RGB)
    frame=cv2.cvtColor(gray_frame,cv2.COLOR_GRAY2RGB)
    legacy_match=getattr(rule,'match_multi_scale',None)
    if callable(legacy_match):
        matched=legacy_match(frame,threshold=.84,scales=[.6,.7,.8,.9,1,1.1,1.2])
    else:
        # The newer shell dispatches .match() through image RPC, whose payload
        # contains file paths rather than this anonymous in-memory patch. Use
        # that version's native local matcher so no portrait file/RPC is needed.
        local_match=getattr(rule,'multi_scale_template_match',None)
        if not callable(local_match):raise AssistError('assist_native_portrait_matcher_unavailable')
        rule.scale_range=(.6,1.2,.1)
        matched=local_match(frame,threshold=.84)
    return rule if matched else None


def prepare_battle_assist(task, selection: AssistSelection, seconds=20):
    if type(selection.normal_used) is not int or not 0<=selection.normal_used<=15:
        raise AssistError('assist_normal_count_invalid')
    if selection.card_used is not None and selection.card_used!=selection.normal_used:
        raise AssistError('assist_count_mismatch')
    opened=drags=stable=0; previous=None;next_drag=0
    for _ in frames(task,seconds):
        if task.appear(task.I_ACCOUNT_EXIT_GAME_TITLE) or task.appear(task.I_ACCOUNT_EXIT_BATTLE_TITLE):
            raise AssistError('assist_dialog_blocks_preparation')
        if not task.appear(task.I_PREPARE_HIGHLIGHT) or not task.appear(task.I_EXIT):
            stable=0;previous=None;continue
        candidates=[]
        for row in rows(task,task.O_ASSIST_PICKER):
            fraction=re.fullmatch(r'(\d{1,2})/15',row.text)
            if row.score<.75 or not fraction or not inside(row.box,task.O_ASSIST_PICKER.roi): continue
            used=int(fraction[1])
            if not 0<=used<=15:continue
            x,y,w,_=row.box;card=(x+w-105,y-130,116,174)
            if not inside(card,(230,490,880,210)):continue
            match=portrait_rule(task,selection.portrait,(card[0]+12,card[1]+15,94,112))
            if match is not None:candidates.append((used,card))
        if len(candidates)>1:raise AssistError('assist_card_identity_ambiguous')
        if not candidates:
            stable=0;previous=None
            if opened<2 and task.appear(task.I_ASSIST_PREP_HINT):
                task.click(task.C_ASSIST_OPEN_PICKER);opened+=1
            continue
        used,card=candidates[0]
        if used!=selection.normal_used:raise AssistError('assist_count_mismatch')
        weapon=in_region(task,task.I_ASSIST_DEPLOYED,(card[0],card[1]-5,116,48))
        x,y=card[0]+18,card[1]+40
        hsv=cv2.cvtColor(task.device.image[y:y+75,x:x+77],cv2.COLOR_RGB2HSV)
        golden=((hsv[:,:,0]>=10)&(hsv[:,:,0]<=40)&(hsv[:,:,1]>=45)&(hsv[:,:,2]>=100)).mean()>=.4
        deployed=weapon is not None and golden
        evidence=(used,bool(deployed))
        if used>=14 or deployed:
            stable=stable+1 if previous==evidence else 1;previous=evidence
            if stable>=2:return AssistProof(selection.normal_used,used,bool(deployed))
            continue
        stable=0;previous=None
        if drags<3 and monotonic()>=next_drag:
            source=(card[0]+50,card[1]+77,16,16)
            task.swipe(RuleSwipe(source,(632,342,16,16),mode='default',name='account_verified_assist_deploy'))
            drags+=1;next_drag=monotonic()+1.2
    raise AssistError('assist_deployment_unconfirmed')


def leave_preparation(task, seconds=15):
    """Only this explicit pre-battle operation may confirm abandoning a fight."""
    previous=getattr(task,'_allow_account_battle_exit',False)
    task._allow_account_battle_exit=True
    opened=0; submitted=False;next_click=0
    try:
        for _ in frames(task,seconds):
            if task.appear(task.I_ACCOUNT_EXIT_GAME_TITLE):return False
            if task.appear(task.I_ACCOUNT_EXIT_BATTLE_TITLE):
                if task.appear(task.I_EXIT_ENSURE) and monotonic()>=next_click:
                    task.click(task.I_EXIT_ENSURE);submitted=True;next_click=monotonic()+1
                continue
            if submitted and task.appear(task.I_FALSE):
                task.click(task.I_FALSE,interval=1)
                continue
            if submitted and task.appear(task.I_EVOZONE_FIRE):return True
            if opened<2 and monotonic()>=next_click and task.appear(task.I_PREPARE_HIGHLIGHT) and task.appear(task.I_EXIT):
                task.click(task.I_EXIT);opened+=1;next_click=monotonic()+1
        return False
    finally:
        task._allow_account_battle_exit=previous
