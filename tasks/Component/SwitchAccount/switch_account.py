import time

from module.config.config import Config
from module.device.device import Device
from module.exception import RequestHumanTakeover
from module.atom.image import RuleImage
from tasks.Component.SwitchAccount.assets import SwitchAccountAssets
from tasks.Component.SwitchAccount.exit_game import ExitGame
from tasks.Component.SwitchAccount.login_account import LoginAccount
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.Restart.login import LoginHandler as OriginalLoginHandler
from tasks.AccountSwitch.dialogs import AccountDialogGuard
from tasks.Restart.assets import RestartAssets
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_login
from tasks.GameUi.matcher import all_of

from module.logger import logger


class LoginHandler(AccountDialogGuard,OriginalLoginHandler):
    pass


class SwitchAccount(AccountDialogGuard,LoginAccount, ExitGame, GameUi, SwitchAccountAssets):
    DEFAULT_UNKNOWN_CLOSERS=[GameUi.I_UI_BACK_RED,GameUi.I_UI_BACK_BLUE,GameUi.I_UI_BACK_YELLOW]

    def _sdk_visible(self):
        if getattr(self.device,'image',None) is None:return False
        return any(self.appear(rule) for rule in (self.I_SA_NETEASE_GAME_LOGO,self.I_SA_ACCOUNT_DROP_DOWN_CLOSED,
                                                 self.I_SA_LOGIN_FORM_APPLE,self.I_SA_SWITCH_ACCOUNT_BTN))
    I_ACCOUNT_CANCEL_BIND_PHONE = RuleImage(
        roi_front=(450, 460, 160, 100), roi_back=(450, 460, 160, 100),
        threshold=0.8, method='Template matching',
        file='./tasks/AccountSwitch/assets/cancel_bind_phone.png',
    )

    def __init__(self, config: Config, device: Device, to: AccountInfo, frm: AccountInfo = None):
        """

        @param config:
        @type config:
        @param device:
        @type device:
        @param to: 要登录的账号信息
        @type to:
        @param frm: 上一个账号信息 ,避免关键字from
        @type frm:
        """
        super().__init__(config, device)
        main=self.navigator.pages[page_main.key]
        main.recognizer=all_of(main.recognizer,lambda task:not task._sdk_visible())
        self.to_account_info = to
        self.from_account_info = frm

    def switchAccount(self):
        logger.info("start switchAccount %s-%s", self.to_account_info.account, self.to_account_info.svr)
        # 判断所处界面
        self.screenshot()
        self.handle_bind_phone_popup()
        curPage = page_login if self._sdk_visible() else self.get_current_page()

        if curPage != page_login and curPage != page_main:
            self.goto_page(page_main)
            curPage = self.get_current_page()
        if curPage == page_main:
            self.exitGame()

        # 处于登录界面
        if not self.login(self.to_account_info):
            return False
        logger.info("%s login suc", self.to_account_info.character)
        # 处理位于登录界面各种奇葩弹窗
        login_handler = LoginHandler(config=self.config, device=self.device)
        login_handler.set_specific_usr(self.to_account_info.character)
        login_handler.app_handle_login()
        # 登录弹窗处理结束后，确认已进入庭院才允许触发后续任务。
        self.goto_page(page_main)
        self.handle_bind_phone_popup()
        if self._sdk_visible():
            raise RequestHumanTakeover('SDK遮挡庭院，未确认登录完成')
        return True

    def handle_bind_phone_popup(self):
        """Dismiss the courtyard overlay through its own cancel flow."""
        handled = False
        for _ in range(24):
            self.screenshot()
            if self.appear(RestartAssets.I_LOGIN_LOGIN_GOTO_BIND_PHONE):
                self.click(RestartAssets.I_LOGIN_LOGIN_GOTO_BIND_PHONE, interval=1)
                handled = True
            elif self.appear(self.I_ACCOUNT_CANCEL_BIND_PHONE):
                self.click(self.I_ACCOUNT_CANCEL_BIND_PHONE, interval=1)
                handled = True
            else:
                if handled:
                    self.device.click_record_clear()
                return handled
            time.sleep(0.5)
        raise RequestHumanTakeover('关联手机弹窗未能取消，请手动关闭后继续切号')


if __name__ == '__main__':
    config = Config('oas1')
    device=Device()
    toAccount=AccountInfo(account="email0@163.com", account_alias="emailO#emailo", apple_or_android=True, character="粘贴", svr="立秋夕烛")
    sa=SwitchAccount(config,device,toAccount)
    sa.switchAccount()
