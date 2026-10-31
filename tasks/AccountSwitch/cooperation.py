"""Friends/cooperation selection on native OAS pages and shared OCR rules."""
from time import monotonic
import re

from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_friends
from tasks.AccountSwitch.assets import AccountFlowAssets
from tasks.AccountSwitch.dialogs import AccountDialogGuard
from tasks.AccountSwitch.assist import (AssistError, AssistSelection, click_box, frames,
                                      in_region, inside, rows, text)
from module.atom.swipe import RuleSwipe


def normal_usage(items):
    values=set();region=(755,145,220,55)
    valid=[row for row in items if row.score>=.7 and inside(row.box,region)]
    for row in valid:
        full=re.fullmatch(r'普通副本[:：]?(\d{1,2})/15',row.text)
        if full and 0<=int(full[1])<=15:values.add(int(full[1]))
        if row.text.rstrip(':：')!='普通副本':continue
        x,y,w,h=row.box
        for fraction in valid:
            match=re.fullmatch(r'(\d{1,2})/15',fraction.text)
            a,b,c,d=fraction.box
            if match and 0<=int(match[1])<=15 and -5<=a-x-w<=80 and abs(y+h/2-b-d/2)<=8:
                values.add(int(match[1]))
    return next(iter(values)) if len(values)==1 else None


class CooperationTask(AccountDialogGuard,GameUi,AccountFlowAssets):
    DEFAULT_UNKNOWN_CLOSERS=[GameUi.I_UI_BACK_RED,GameUi.I_UI_BACK_BLUE,GameUi.I_UI_BACK_YELLOW]

    @staticmethod
    def _same_card_rows(first,second):
        return len(first)==len(second) and all(
            a.text==b.text and all(abs(x-y)<=4 for x,y in zip(a.box,b.box))
            for a,b in zip(first,second))

    def _wait_friend_cards(self,before,seconds=4):
        """Observe one drag settling; never drag again while its page is pending."""
        started=monotonic();previous=[];stable=0
        for _ in frames(self,seconds):
            panel=rows(self,self.O_ASSIST_PANEL)
            if (not any(row.text=='每日协战次数' and row.score>=.7 for row in panel)
                    or normal_usage(panel) is None):
                previous=[];stable=0;continue
            cards=rows(self,self.O_ASSIST_CARDS)
            if not cards:
                previous=[];stable=0;continue
            stable=stable+1 if self._same_card_rows(previous,cards) else 1
            previous=cards
            elapsed=monotonic()-started
            if stable<3 or elapsed<.6:continue
            if not self._same_card_rows(before,cards):return True
            # SDK/game animation may keep showing the old page before moving.
            if elapsed>=2:return False
        raise AssistError('assist_friend_scroll_unconfirmed')

    def open_panel(self):
        self.goto_page(page_friends)
        clicked=False
        for _ in frames(self,20):
            panel=rows(self,self.O_ASSIST_PANEL)
            if any(row.text=='每日协战次数' and row.score>=.7 for row in panel):return
            tabs=[row for row in rows(self,self.O_ASSIST_TAB) if row.text=='协战' and row.score>=.7]
            # The shared OCR can split this vertical tab into two glyphs.
            if not tabs:
                glyphs=rows(self,self.O_ASSIST_TAB)
                for first in glyphs:
                    for second in glyphs:
                        if first.text=='协' and second.text=='战' and first.score>=.7 and second.score>=.7:
                            x,y,w,h=first.box;a,b,c,d=second.box
                            if b>y and b-y-h<22 and abs(x+w/2-a-c/2)<20:
                                tabs=[type(first)('协战',(min(x,a),y,max(x+w,a+c)-min(x,a),b+d-y),min(first.score,second.score))]
            if len(tabs)==1 and not clicked:
                click_box(self,tabs[0].box,'account_cooperation_tab');clicked=True
        raise AssistError('cooperation_panel_unconfirmed')

    def read_usage(self):
        self.open_panel();last=None;stable=0
        for _ in frames(self,15):
            panel=rows(self,self.O_ASSIST_PANEL)
            if not any(row.text=='每日协战次数' and row.score>=.7 for row in panel):
                stable=0;last=None;continue
            used=normal_usage(panel)
            stable=stable+1 if used is not None and used==last else 1;last=used
            if used is not None and stable>=2:return used
        raise AssistError('normal_cooperation_count_unconfirmed')

    def prepare(self,friend):
        friend=text(friend)
        if not friend:raise ValueError('协战好友昵称不能为空')
        self.read_usage();stable=clicks=scrolls=0;last=None;next_click=0
        for _ in frames(self,25):
            panel=rows(self,self.O_ASSIST_PANEL)
            if not any(row.text=='每日协战次数' and row.score>=.7 for row in panel):
                stable=0;last=None;continue
            used=normal_usage(panel)
            if used is None:
                stable=0;last=None;continue
            cards=rows(self,self.O_ASSIST_CARDS)
            if not cards:continue
            exact=[row for row in cards if row.text==friend and row.score>=.7]
            if len(exact)>1:raise AssistError('assist_friend_ambiguous')
            if not exact:
                if scrolls>=6:raise AssistError('assist_friend_not_found')
                scrolls+=1
                self.swipe(RuleSwipe((1040,580,20,20),(1040,370,20,20),mode='default',name='account_assist_friend_list'))
                if not self._wait_friend_cards(cards):raise AssistError('assist_friend_not_found')
                continue
            row=exact[0];cx=row.box[0]+row.box[2]//2;y=row.box[1]
            # The upper "出战式神" slots contain this account's own lending
            # shikigami. Borrowing is proved by the selected friend's card.
            # Leave enough vertical margin for OCR nickname-box variations;
            # a 55px region clipped the bottom of the actual 40px check icon.
            checked=in_region(self,self.I_ASSIST_SELECTED,(cx+10,y-155,75,75)) is not None
            if checked:
                stable=stable+1 if last is not None and last[0]==used and self._same_card_rows([last[1]],[row]) else 1
                last=(used,row)
                if stable>=2:
                    # Capture the confirmed current card, not an earlier
                    # loading/selection animation's first visible portrait.
                    portrait=self.device.image[y-110:y-62,cx-24:cx+24].copy()
                    return AssistSelection(friend,used,None,portrait)
                continue
            stable=0;last=None
            if not checked and clicks<2 and monotonic()>=next_click:
                click_box(self,(cx-15,y-95,30,30),'account_exact_assist_card')
                clicks+=1;next_click=monotonic()+1.2
        raise AssistError('assist_friend_selection_unconfirmed')


def read_normal_cooperation_usage(config,device):
    return CooperationTask(config,device).read_usage()


def prepare_cooperation_assist(config,device,assist_friend):
    if not text(assist_friend):
        raise AssistError('协战好友为空；请显式填写好友昵称，不能自动猜测或继承勾协大号')
    return CooperationTask(config,device).prepare(assist_friend)


read_assist=prepare_cooperation_assist
