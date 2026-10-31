from tasks.Component.SwitchAccount.assets import SwitchAccountAssets
from tasks.base_task import BaseTask
from module.logger import logger
from module.exception import RequestHumanTakeover
from module.atom.click import RuleClick
from time import monotonic,sleep
from tasks.GameUi.assets import GameUiAssets


class ExitGame(BaseTask, SwitchAccountAssets):

    def exitGame(self):
        logger.info("start game exit")
        # 打开该页面比较慢 如果interval短 将发生异常
        def transition(predicate,button,guard,seconds):
            deadline,next_click=monotonic()+seconds,0
            while monotonic()<deadline:
                self.screenshot()
                if predicate():return
                if monotonic()>=next_click and guard():
                    if isinstance(button,RuleClick) or self.appear(button):
                        self.click(button);next_click=monotonic()+2
                sleep(.15)
            raise RequestHumanTakeover('切号退出登录界面超时，未确认目标页面')
        transition(lambda:self.appear(self.I_SA_USER_CENTER_PROFILE) or self.appear(self.I_SA_USER_CENTER),
                   self.C_SA_EG_PROFILE_PHOTO,
                   lambda:self.appear(GameUiAssets.I_MAIN_GOTO_SHIKIGAMI_RECORDS)
                   and not any(self.appear(rule) for rule in (self.I_SA_NETEASE_GAME_LOGO,self.I_SA_SWITCH_ACCOUNT_BTN,
                                                            self.I_SA_ACCOUNT_DROP_DOWN_CLOSED,self.I_SA_LOGIN_FORM_APPLE)),25)
        transition(lambda:self.appear(self.I_SA_SWITCH_ACCOUNT_BTN),self.I_SA_USER_CENTER,
                   lambda:self.appear(self.I_SA_USER_CENTER),30)
        transition(lambda:not self.appear(self.I_SA_SWITCH_ACCOUNT_BTN)
                   and (self.appear(self.I_SA_NETEASE_GAME_LOGO) or self.appear(self.I_CHECK_LOGIN_FORM)),
                   self.I_SA_SWITCH_ACCOUNT_BTN,lambda:self.appear(self.I_SA_SWITCH_ACCOUNT_BTN),35)
