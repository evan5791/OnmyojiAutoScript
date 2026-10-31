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
from module.atom.ocr import RuleOcr
from module.atom.swipe import RuleSwipe
from module.logger import logger


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
    score=getattr(rule,'last_score',None)
    task._assist_portrait_diagnostic={
        'matched':bool(matched),'score':round(float(score),4) if isinstance(score,(int,float,np.number)) else None,
        'region':tuple(region),'threshold':.84,
    }
    return rule if matched else None


def _read_card_counter(task,row):
    # Large-ROI OCR may produce only "13", or overlapping "13"/"3/15".
    # The fragment merely locates a line. First locate a complete counter box,
    # then recognize that tight box so neighboring card labels stay outside.
    roi=(row.box[0]-40,row.box[1]-5,110,row.box[3]+10)
    rule=RuleOcr(roi=roi,area=roi,mode='Full',method='Default',keyword='',
                 name='account_assist_card_counter_probe')
    rule.score=.8
    diagnostic={'probe_roi':roi,'full_row':row.text}
    task._assist_counter_diagnostic=diagnostic
    detected=rows(task,rule)
    diagnostic['full_boxes']=[(item.text,item.box,round(item.score,3)) for item in detected]
    verified=[]
    for item in detected:
        full=re.fullmatch(r'(\d{1,2})/15',item.text)
        if (item.score>=.8 and full and 0<=int(full[1])<=15 and inside(item.box,roi)
                and abs(item.box[1]+item.box[3]/2-row.box[1]-row.box[3]/2)<=8):
            verified.append(item)
    if not verified or len({item.text for item in verified})!=1:return None
    full_row=max(verified,key=lambda item:item.box[2]*item.box[3])
    x,y,w,h=full_row.box;tight=(x-2,y-2,w+4,h+4)
    single=RuleOcr(roi=tight,area=tight,mode='Single',method='Default',keyword='',
                   name='account_assist_card_counter')
    single.score=.8
    raw=text(single.ocr_single(task.device.image))
    diagnostic.update(raw=raw,roi=tight)
    if raw!=full_row.text:return None
    return int(raw.split('/')[0]),full_row


def prepare_battle_assist(task, selection: AssistSelection, seconds=20):
    if type(selection.normal_used) is not int or not 0<=selection.normal_used<=15:
        raise AssistError('assist_normal_count_invalid')
    if selection.card_used is not None and selection.card_used!=selection.normal_used:
        raise AssistError('assist_count_mismatch')
    opened=drags=stable=weapon_stable=0; previous=None;next_drag=next_open=0
    yellow_seen=False
    mismatch_previous=None;mismatch_stable=0
    last_diagnostic=None
    def phase(name,**evidence):
        nonlocal last_diagnostic
        diagnostic=(name,evidence)
        if diagnostic!=last_diagnostic:
            logger.info('协战准备 phase=%s evidence=%s',name,evidence)
            last_diagnostic=diagnostic
    phase('start',normal_used=selection.normal_used,portrait_required=False)
    for _ in frames(task,seconds):
        if task.appear(task.I_ACCOUNT_EXIT_GAME_TITLE) or task.appear(task.I_ACCOUNT_EXIT_BATTLE_TITLE):
            phase('dialog_blocked')
            raise AssistError('assist_dialog_blocks_preparation')
        prepare=bool(task.appear(task.I_PREPARE_HIGHLIGHT));exit_visible=bool(task.appear(task.I_EXIT))
        if not prepare or not exit_visible:
            phase('waiting_preparation',prepare=prepare,exit=exit_visible)
            stable=mismatch_stable=weapon_stable=0;previous=mismatch_previous=None;yellow_seen=False;continue
        candidates=[];traces=[]
        picker_rows=rows(task,task.O_ASSIST_PICKER)
        px,py,pw,_=task.O_ASSIST_PICKER.roi
        band=(px,py+110,pw,50)
        def is_anchor(row):
            return (row.score>=.75 and re.fullmatch(r'(?:\d{1,2}(?:/15)?|/15)',row.text)
                    and inside(row.box,band))
        seed_rows=[row for row in picker_rows if is_anchor(row)]
        band_rows=[]
        if not seed_rows:
            # Detect only the existing card counter line, never click a fixed
            # position when the larger picker OCR omitted its small counter.
            counter_band=RuleOcr(roi=band,area=band,mode='Full',method='Default',keyword='',
                                 name='account_assist_counter_band')
            band_rows=rows(task,counter_band)
            seed_rows=[row for row in band_rows if is_anchor(row)]
        for row in seed_rows:
            trace={'full_raw':row.text,'row':row.box}
            traces.append(trace)
            resolved=_read_card_counter(task,row)
            trace['refined']=getattr(task,'_assist_counter_diagnostic',None)
            if resolved is None:
                trace['rejected']='full_counter_unconfirmed';continue
            used,full_row=resolved
            trace['used']=used;trace['counter_box']=full_row.box
            x,y,w,_=full_row.box;card=(x+w-105,y-130,116,174)
            trace['card']=card
            if not inside(card,(230,490,880,210)):
                trace['rejected']='card_outside_picker';continue
            if any(abs(card[0]-other[0])<=8 and abs(card[1]-other[1])<=8 for _,other in candidates):
                trace['rejected']='duplicate_card';continue
            candidates.append((used,card))
        strip_visible=any(row.score>=.7 and inside(row.box,task.O_ASSIST_PICKER.roi)
                          and ((row.text=='满' and inside(row.box,(px,py-5,pw,60)))
                               or (inside(row.box,(px,py+110,pw,70))
                                   and (row.text=='素材' or re.fullmatch(r'[\d/]{1,8}',row.text))))
                          for row in picker_rows+band_rows)
        if not candidates:
            phase('waiting_card',candidates=0,cards=traces,opened=opened,
                  picker_visible=strip_visible,
                  counter_band_rows=[(row.text,row.box,round(row.score,2)) for row in band_rows],
                  full_rows=[(row.text,row.box,round(row.score,2)) for row in picker_rows])
            stable=mismatch_stable=weapon_stable=0;previous=mismatch_previous=None;yellow_seen=False
            if not strip_visible and opened<2 and monotonic()>=next_open and task.appear(task.I_ASSIST_PREP_HINT):
                task.click(task.C_ASSIST_OPEN_PICKER);opened+=1;next_open=monotonic()+2
            continue
        # A friend may lend two different shikigami. The leftmost borrowed
        # card with a verified N/15 field is the user's selection rule.
        used,card=min(candidates,key=lambda item:item[1][0])
        if used!=selection.normal_used:
            stable=weapon_stable=0;previous=None;yellow_seen=False
            mismatch_stable=mismatch_stable+1 if mismatch_previous==used else 1
            mismatch_previous=used
            phase('count_mismatch',normal_used=selection.normal_used,used=used,stable=mismatch_stable,cards=traces)
            if mismatch_stable>=3:raise AssistError('assist_count_mismatch')
            continue
        mismatch_previous=None;mismatch_stable=0
        weapon=in_region(task,task.I_ASSIST_DEPLOYED,(card[0],card[1]-5,116,48))
        x,y=card[0]+18,card[1]+40
        hsv=cv2.cvtColor(task.device.image[y:y+75,x:x+77],cv2.COLOR_RGB2HSV)
        golden_ratio=float(((hsv[:,:,0]>=10)&(hsv[:,:,0]<=40)&(hsv[:,:,1]>=45)&(hsv[:,:,2]>=100)).mean())
        golden=golden_ratio>=.4
        same=previous is not None and previous[0]==used and all(abs(a-b)<=8 for a,b in zip(previous[1],card))
        if not same:
            stable=weapon_stable=0;yellow_seen=False
        previous=(used,card);stable+=1
        yellow_seen=yellow_seen or golden
        weapon_stable=weapon_stable+1 if weapon is not None else 0
        deployed=weapon_stable>=2 and yellow_seen
        if used>=14 or deployed:
            phase('proof_candidate',used=used,weapon=weapon is not None,golden=golden,
                  golden_ratio=round(golden_ratio,3),yellow_seen=yellow_seen,weapon_stable=weapon_stable,
                  deployed=deployed,stable=stable,cards=traces)
            if stable>=2:return AssistProof(selection.normal_used,used,bool(deployed))
            continue
        phase('waiting_deployment',used=used,weapon=weapon is not None,golden=golden,
              golden_ratio=round(golden_ratio,3),yellow_seen=yellow_seen,weapon_stable=weapon_stable,
              stable=stable,drags=drags,cards=traces)
        if weapon is None and drags<3 and monotonic()>=next_drag:
            source=(card[0]+50,card[1]+77,16,16)
            task.swipe(RuleSwipe(source,(632,342,16,16),mode='default',name='account_verified_assist_deploy'))
            drags+=1;next_drag=monotonic()+1.2
    phase('timeout',reason='assist_deployment_unconfirmed',last_phase=last_diagnostic)
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
