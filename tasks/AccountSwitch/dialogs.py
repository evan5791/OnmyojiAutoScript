"""Cancel only positively identified exit overlays before interpreting pages."""
from time import sleep
from module.exception import RequestHumanTakeover
from tasks.AccountSwitch.assets import AccountFlowAssets


class AccountDialogGuard(AccountFlowAssets):
    def dismiss_account_dialog(self):
        pairs = [(self.I_ACCOUNT_EXIT_GAME_TITLE, self.I_ACCOUNT_EXIT_GAME_CANCEL)]
        if not getattr(self, '_allow_account_battle_exit', False):
            pairs.append((self.I_ACCOUNT_EXIT_BATTLE_TITLE, self.I_ACCOUNT_EXIT_BATTLE_CANCEL))
        for title, cancel in pairs:
            if self.appear(title):
                if not self.appear(cancel):
                    raise RequestHumanTakeover('已识别退出弹窗，但取消按钮未确认，停止输入')
                self.click(cancel, interval=1)
                return True
        return False

    def screenshot(self):
        for _ in range(12):
            frame = super().screenshot()
            if not self.dismiss_account_dialog():
                return frame
            sleep(.15)
        raise RequestHumanTakeover('退出弹窗未消失，已停止继续切号')
