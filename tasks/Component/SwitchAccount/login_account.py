import math
import time
from unicodedata import normalize

import cv2

from module.atom.click import RuleClick
from module.atom.gif import RuleGif
from module.atom.image import RuleImage
from module.atom.ocr import RuleOcr
from module.atom.swipe import RuleSwipe
from module.logger import logger
from tasks.Component.SwitchAccount.assets import SwitchAccountAssets
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.base_task import BaseTask


def exact_account_matches(info, value):
    def clean(item):return ''.join(normalize('NFKC',str(item or '')).split()).casefold().replace('★','*')
    account,found=clean(info.account),clean(value)
    if not account or not account.split('@',1)[0] or not found:return False
    choices={account}
    if '@' in account:choices.add(account.split('@',1)[0])
    choices.update(clean(alias) for alias in info.account_alias.split('#') if clean(alias))
    return found in choices


class LoginAccount(BaseTask, SwitchAccountAssets):

    def _wait_selected_account(self,accountInfo: AccountInfo,*,timeout=15,confirm_frames=2):
        """Wait for a closed SDK card and stable exact identity on fresh frames.

        Clicking a list row only requests a selection; the SDK can still expose
        the old text or an open dropdown for several frames afterwards.
        """
        started=time.monotonic()
        deadline=started+timeout
        stable=0
        previous=None
        last_raw=''
        last_phase='sdk_transition'
        while time.monotonic()<deadline:
            self.screenshot()
            sdk=bool(self.appear(self.I_SA_NETEASE_GAME_LOGO))
            closed=bool(self.appear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED))
            login=bool(self.appear(self.I_SA_ACCOUNT_LOGIN_BTN))
            last_raw=self.O_SA_ACCOUNT_ACCOUNT_SELECTED.ocr_single(self.device.image) if sdk and closed and login else ''
            matched=sdk and closed and login and exact_account_matches(accountInfo,last_raw)
            stable=stable+1 if matched else 0
            last_phase=('identity_verified' if stable>=confirm_frames else 'identity_candidate' if matched
                        else 'selected_identity_mismatch' if sdk and closed and login
                        else 'dropdown_open' if sdk and not closed else 'sdk_transition')
            observation=(last_phase,str(last_raw),sdk,closed,login)
            if observation!=previous:
                logger.info('SDK account verification phase=%s raw=%r sdk=%s closed=%s login=%s stable=%s',
                            last_phase,last_raw,sdk,closed,login,stable)
                previous=observation
            if stable>=confirm_frames:
                return True
            time.sleep(.2)
        self.login_failure_reason='selected_account_verification_timeout'
        logger.warning('SDK account verification timed out: phase=%s raw=%r target=%r elapsed=%.2fs',
                       last_phase,last_raw,accountInfo.account,time.monotonic()-started)
        return False

    def _bounded_wait(self,predicate,*,click=None,guard=None,timeout=30,interval=2):
        """Observe native rules without depending on BaseTask.ui_click versions."""
        deadline,next_click=time.monotonic()+timeout,0
        while time.monotonic()<deadline:
            self.screenshot()
            if predicate():return True
            now=time.monotonic()
            if click is not None and now>=next_click and self.appear(click) and (guard is None or guard()):
                self.click(click)
                next_click=now+interval
            time.sleep(.15)
        return False

    def get_svr_name(self):
        self.screenshot()
        ocrRes = self.O_SA_LOGIN_FORM_SVR_NAME.ocr(self.device.image)
        return ocrRes

    def switch_svr(self, svrName: str):
        """
            需保证账号已登录 且处于登录界面
        @param svrName:
        @type svrName:
        """
        self.O_SA_LOGIN_FORM_SVR_NAME.keyword = svrName
        if self.ocr_appear(self.O_SA_LOGIN_FORM_SVR_NAME):
            return True
        self.ui_click(self.C_SA_LOGIN_FORM_SWITCH_SVR_BTN, self.I_SA_CHECK_SELECT_SVR_1, 1.5)
        # 展开底部角色列表,显示角色所属服务器
        self.screenshot()
        if self.appear(self.I_SA_CHECK_SELECT_SVR_1) and (not self.appear(self.I_SA_CHECK_SELECT_SVR_2)):
            self.click(self.O_SA_SELECT_SVR_CHARACTER_LIST)

        self.O_SA_SELECT_SVR_SVR_LIST.keyword = svrName
        found = False
        lastSvrList: tuple = ()
        while 1:
            self.screenshot()
            self.device.image = cv2.cvtColor(self.device.image, cv2.COLOR_BGR2GRAY)
            # ret, self.device.image = cv2.threshold(self.device.image, 200, 255, cv2.THRESH_OTSU)
            ret, self.device.image = cv2.threshold(self.device.image, 100, 255, cv2.THRESH_BINARY)
            # self.device.image = cv2.adaptiveThreshold(self.device.image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 10)
            self.device.image = abs(255 - self.device.image)

            # RGB图
            self.device.image = cv2.cvtColor(self.device.image, cv2.COLOR_GRAY2RGB)


            ocrRes = self.O_SA_SELECT_SVR_SVR_LIST.detect_and_ocr(self.device.image)
            # 受限于图像识别文字准确率,此处对识别结果与实际服务器名字 进行检查 字重合度大于阈值 就认为查找成功
            thresh = 0.5
            ocrSvrList = [res.ocr_text for res in ocrRes]
            for index, ocrSvrName in enumerate(ocrSvrList):
                if len(ocrSvrName) < 3:
                    break
                tmp = set(svrName).intersection(set(ocrSvrName))
                if len(tmp) > max(len(svrName), len(ocrSvrName)) * thresh:
                    logger.info("found svr %s which is similar with %s", ocrSvrName, svrName)
                    found = True
                    # 确定点击位置
                    box = ocrRes[index].box
                    self.O_SA_SELECT_SVR_SVR_LIST.area = [self.O_SA_SELECT_SVR_SVR_LIST.roi[0] + box[0][0],
                                                          self.O_SA_SELECT_SVR_SVR_LIST.roi[1] + box[0][1],
                                                          box[1][0] - box[0][0],
                                                          box[2][1] - box[1][1]]
                    # 跳出此层for循环
                    break
            # 两次OCR结果相等表示滑动到最右侧
            if found or lastSvrList == ocrSvrList:
                break
            lastSvrList = ocrSvrList
            self.swipe(self.S_SA_SVR_SWIPE_LEFT)
            time.sleep(3.5)
        if found:
            self.click(self.O_SA_SELECT_SVR_SVR_LIST, interval=1.5)
            return True
        # 没找到 点击空白区域关闭选择服务器界面
        self.click(self.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT)
        return False

    def switch_character(self, characterName: str):
        """
              需保证账号已登录 且处于登录界面
        @param characterName:
        @return:
        @rtype:
        """
        logger.info("start switch_character")
        # 改成对比是否出现 已有角色 
        self.ui_click(self.C_SA_LOGIN_FORM_SWITCH_SVR_BTN, self.O_SA_CHECK_SELECT_SVR)
        # 展开底部角色列表,显示角色所属服务器
        self.screenshot()
        while (not self.appear(self.I_SA_CHECK_SELECT_SVR_2)) and self.appear(self.I_SA_CHECK_SELECT_SVR_1):
            logger.info("open svr icon")
            self.click(self.C_SA_SELECT_SVR_CHARACTER_LIST, interval=1.5)
            self.wait_until_appear(self.I_SA_CHECK_SELECT_SVR_2, False, 1)
            # self.ui_click(self.C_SA_SELECT_SVR_CHARACTER_LIST, self.I_SA_CHECK_SELECT_SVR_2, 1.5)
            self.screenshot()

        self.O_SA_SELECT_SVR_CHARACTER_LIST.keyword = characterName
        lastCharacterNameList = []
        while 1:
            self.screenshot()
            ocrRes = self.O_SA_SELECT_SVR_CHARACTER_LIST.detect_and_ocr(self.device.image)
            # 去除角色等级数字
            characterNameList = [ocrResItem.ocr_text.lstrip('1234567890 ([<>])【】（）《》') for ocrResItem in ocrRes]
            logger.info(characterNameList)
            ocrResBoxList = [ocrResItem.box for ocrResItem in ocrRes]
            for index, item in enumerate(characterNameList):
                if item != characterName:
                    continue
                tmp = self.O_SA_SELECT_SVR_CHARACTER_LIST
                from copy import deepcopy
                tmpClick = RuleClick(
                    roi_back=deepcopy(tmp.roi),
                    roi_front=[
                        tmp.roi[0] + ocrResBoxList[index][0][0],
                        tmp.roi[1] + ocrResBoxList[index][0][1],
                        ocrResBoxList[index][1][0] - ocrResBoxList[index][0][0],
                        ocrResBoxList[index][2][1] - ocrResBoxList[index][1][1]],
                    name="tmpClick"
                )

                # 此时 tmp 内存储的时角色名位置,而点击角色名没有反应
                # 所以需要获取到对应的服务器图标位置
                tmpClick.roi_front[1] -= 30
                self.ui_click_until_disappear(tmpClick, stop=self.I_SA_CHECK_SELECT_SVR_2,
                                              interval=3)
                logger.info("character %s found,and clicked svr icon", characterName)
                return True
            if lastCharacterNameList == characterNameList:
                break
            logger.info(f'{characterName} not found,start swipe')
            lastCharacterNameList = characterNameList
            self.swipe(self.S_SA_ACCOUNT_LIST_UP)
            # 等待滑动动画完成
            time.sleep(1.5)

        self.click(self.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT, 1.5)
        return False

    def jump2SelectAccount(self):
        """
            跳转到切换账号页面 该页面有红色登录按钮
        @return:
        @rtype:
        """
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            self.screenshot()
            if self.appear(self.I_SA_NETEASE_GAME_LOGO) and self.appear(self.I_SA_ACCOUNT_LOGIN_BTN):
                return
            if self.appear_then_click(self.I_SA_SWITCH_ACCOUNT_BTN, interval=1.5):
                continue
            if self.appear(self.I_CHECK_LOGIN_FORM):
                self.click(self.C_SA_LOGIN_FORM_USER_CENTER, 1.5)
                continue
            time.sleep(.15)
        return False

    def selectAccount(self, accountInfo: AccountInfo):
        logger.info("start selectAccount")
        self.O_SA_ACCOUNT_ACCOUNT_LIST.keyword = accountInfo.account
        self.O_SA_ACCOUNT_ACCOUNT_SELECTED.keyword = accountInfo.account
        # 多账号列表可能保留上次滚动位置；到达边界后反向回扫，补偿 OCR 漏识别。
        reverse_swipe = RuleSwipe(
            roi_front=self.S_SA_ACCOUNT_LIST_UP.roi_back,
            roi_back=self.S_SA_ACCOUNT_LIST_UP.roi_front,
            mode=self.S_SA_ACCOUNT_LIST_UP.mode,
            name='sa_account_list_down',
        )
        for i in range(3):
            previous_page = None
            swipe = self.S_SA_ACCOUNT_LIST_UP if i % 2 == 0 else reverse_swipe
            for _ in range(15):
                self.screenshot()
                if self.appear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED):
                    selected = self.O_SA_ACCOUNT_ACCOUNT_SELECTED.ocr_single(self.device.image)
                    if exact_account_matches(accountInfo,selected):
                        return True
                    self.ui_click_until_disappear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED,
                                                  interval=1.5)
                    continue

                # 账号列表已打开状态
                ocrRes = self.O_SA_ACCOUNT_ACCOUNT_LIST.detect_and_ocr(self.device.image)
                account_names = tuple(item.ocr_text for item in ocrRes)

                # 找到该账号
                for index, ocr_account in enumerate(account_names):
                    if not exact_account_matches(accountInfo,ocr_account):
                        continue
                    # if accountInfo.account in [ocrResItem.ocr_text for ocrResItem in ocrRes]:
                    #     index = [ocrResItem.ocr_text for ocrResItem in ocrRes].index(accountInfo.account)
                    ocrResBoxList = [ocrResItem.box for ocrResItem in ocrRes]
                    self.O_SA_ACCOUNT_ACCOUNT_LIST.area = [
                        self.O_SA_ACCOUNT_ACCOUNT_LIST.roi[0] + ocrResBoxList[index][0][
                            0],
                        self.O_SA_ACCOUNT_ACCOUNT_LIST.roi[1] + ocrResBoxList[index][0][
                            1],
                        ocrResBoxList[index][1][0] - ocrResBoxList[index][0][0],
                        ocrResBoxList[index][2][1] - ocrResBoxList[index][1][1]]
                    time.sleep(1)
                    self.click(self.O_SA_ACCOUNT_ACCOUNT_LIST)
                    logger.info("account [ %s ] found", accountInfo.account)
                    return True

                # 未找到该账号
                if account_names and account_names == previous_page:
                    break
                previous_page = account_names
                if i % 2 == 0 and self.appear(self.I_SA_ACCOUNT_DROP_DOWN_ADD_ACCOUNT):
                    break
                # Each iteration already waits for the drag animation. A second
                # cooldown can suppress the drag and mimic the end of the list.
                self.swipe(swipe)
                time.sleep(0.8)
        logger.info("account [ %s ] not found ", accountInfo.account)
        return False

    # def loginSubmit(self, appleOrAndroid: bool):
    #     """
    #
    #     @param appleOrAndroid: 安卓平台还是苹果平台
    #     @type appleOrAndroid:   False           Apple
    #                             True            Android
    #     @return:
    #     @rtype:
    #     """
    #     self.screenshot()
    #     if not (self.appear(self.I_SA_ACCOUNT_LOGIN_BTN) and self.appear(self.I_SA_NETEASE_GAME_LOGO)):
    #         # 不在登录界面,返回失败
    #         return False
    #     self.ui_click(self.C_SA_LOGIN_FORM_LOGIN_BTN, self.I_SA_LOGIN_FORM_APPLE, 1)
    #     if appleOrAndroid:
    #         logger.info("APPLE selected")
    #         self.ui_click_until_disappear(self.I_SA_LOGIN_FORM_APPLE, 1)
    #     else:
    #         logger.info("ANDROID selected")
    #         self.ui_click_until_disappear(self.I_SA_LOGIN_FORM_ANDROID, 1)
    #     return True

    def login(self, accountInfo: AccountInfo) -> bool:
        """

        @param accountInfo:
        @type accountInfo:
        @return:    True    点击了"进入游戏"按钮
                    False   未找到相应角色
        @rtype:bool
        """
        self.screenshot()
        #
        if not (self.appear(self.I_CHECK_LOGIN_FORM) or self.appear(self.I_SA_NETEASE_GAME_LOGO)
                or self.appear(self.I_SA_SWITCH_ACCOUNT_BTN)):
            logger.error("Unknown Page,%s %s Login Failed", accountInfo.character, accountInfo.svr)
            return False

        #
        isAccountLogon = False
        isCharacterSelected = False
        verified=False
        self.O_SA_ACCOUNT_ACCOUNT_SELECTED.keyword = accountInfo.account
        self.O_SA_LOGIN_FORM_USER_CENTER_ACCOUNT.keyword = accountInfo.account
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            self.screenshot()
            # 处于 选择服务器界面 直接点击空白区域退出该界面 进入切换账号流程
            if self.appear(self.I_SA_CHECK_SELECT_SVR_1) or self.appear(self.I_SA_CHECK_SELECT_SVR_2):
                self.click(self.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT)
                continue

            # 处于选择 苹果安卓界面
            if self.appear(self.I_SA_LOGIN_FORM_APPLE):
                if not verified:return False
                btn = self.I_SA_LOGIN_FORM_ANDROID if accountInfo.apple_or_android else self.I_SA_LOGIN_FORM_APPLE
                self.ui_click_until_disappear(btn)
                isAccountLogon = True
                continue
            # 处于选择账号界面
            if self.appear(self.I_SA_NETEASE_GAME_LOGO) and not self.appear(self.I_SA_LOGIN_FORM_APPLE):
                if not accountInfo.account:
                    logger.error("param account is None,cannot switch account")
                    return False
                # 当前选择账号不是account
                selected = self.O_SA_ACCOUNT_ACCOUNT_SELECTED.ocr_single(self.device.image)
                # With the dropdown open this ROI contains its first row,
                # which is not evidence that that account has been selected.
                if not self.appear(self.I_SA_ACCOUNT_DROP_DOWN_CLOSED) or not exact_account_matches(accountInfo,selected):
                    # 没有找到account
                    if not self.selectAccount(accountInfo):
                        self.ui_click_until_disappear(self.C_SA_LOGIN_FORM_ACCOUNT_CLOSE_BTN,
                                                      stop=self.I_SA_NETEASE_GAME_LOGO)
                        return False
                if not self._wait_selected_account(accountInfo):return False
                verified=True
                if not self._bounded_wait(
                        lambda:self.appear(self.I_SA_LOGIN_FORM_APPLE) or self.appear(self.I_SA_SWITCH_ACCOUNT_BTN)
                        or (self.appear(self.I_CHECK_LOGIN_FORM) and not self.appear(self.I_SA_NETEASE_GAME_LOGO)),
                        click=self.I_SA_ACCOUNT_LOGIN_BTN,timeout=35,
                        guard=lambda:exact_account_matches(accountInfo,self.O_SA_ACCOUNT_ACCOUNT_SELECTED.ocr_single(self.device.image))):
                    return False
                continue
            # 在用户中心界面
            if self.appear(self.I_SA_SWITCH_ACCOUNT_BTN):
                # 如果当前已登录用户就是account
                ocrRes = self.O_SA_LOGIN_FORM_USER_CENTER_ACCOUNT.ocr_single(self.device.image)
                # NOTE 由于邮箱账号@符号极易被误识别为其他,故对账号信息做预处理 便于比对
                if exact_account_matches(accountInfo,ocrRes):
                    logger.info("current is the account we want:ocr result %s", ocrRes)
                    isAccountLogon = True
                    verified=True
                    self.ui_click_until_disappear(self.C_SA_LOGIN_FORM_USER_CENTER_CLOSE_BTN, interval=1,
                                                  stop=self.I_SA_SWITCH_ACCOUNT_BTN)
                    continue
                #
                if self._bounded_wait(lambda:self.appear(self.I_SA_NETEASE_GAME_LOGO)
                                      or (self.appear(self.I_CHECK_LOGIN_FORM) and not self.appear(self.I_SA_SWITCH_ACCOUNT_BTN)),
                                      click=self.I_SA_SWITCH_ACCOUNT_BTN,timeout=30):
                    isAccountLogon = False
                    verified=False
                    continue
                return False
            # 在游戏登录界面 不在用户中心 不在切换账号界面
            if not (self.appear(self.I_SA_NETEASE_GAME_LOGO) or self.appear(self.I_SA_SWITCH_ACCOUNT_BTN)):
                # 判断是否已经账号登录
                if not isAccountLogon:
                    if self.appear(self.I_CHECK_LOGIN_FORM):
                        self.click(self.C_SA_LOGIN_FORM_USER_CENTER,interval=2)
                    time.sleep(.15)
                    continue

                # 同区切号只配置账号即可，沿用账号记忆的区服，不进入角色/区服选择。
                if not accountInfo.character:
                    isCharacterSelected = True
                    break
                # 已登录 查找对应角色
                if not isCharacterSelected and self.switch_character(accountInfo.character):
                    isCharacterSelected = True
                    continue
                break
            continue

        # 切换角色失败 /未找到该角色
        # 尝试使用 选择服务器方式
        if isAccountLogon and not isCharacterSelected and accountInfo.svr is not None and accountInfo.svr != "":
            logger.info("try to find character with svrName %s", accountInfo.svr)
            isCharacterSelected = self.switch_svr(accountInfo.svr)
        if isAccountLogon and isCharacterSelected:
            # 成功登录账号 找到角色
            # self.ui_click_until_disappear(self.C_SA_LOGIN_FORM_ENTER_GAME_BTN, stop=self.I_CHECK_LOGIN_FORM)
            logger.info("character %s-%s account:%s %s login Success", accountInfo.character, accountInfo.svr,
                        accountInfo.account,
                        'Android' if accountInfo.apple_or_android else 'Apple')
            return True

        logger.error("character %s-%s account:%s %s login Failed", accountInfo.character, accountInfo.svr,
                     accountInfo.account,
                     'Android' if accountInfo.apple_or_android else 'Apple')
        return False

    def ui_click_until_disappear(self, click, interval: float = 1, stop: RuleImage | RuleGif = None, timeout=30):
        """
        重写原ui_click_until_disappear方法,增加stop参数
        点击一个按钮直到stop消失
        如果click为RuleOcr ,直接当作RuleClick点击,不会进行ocr识别,
        @param interval:
        @param click:
        @param stop:
        @type stop:
        @return:
        """
        if (isinstance(click, RuleImage) or isinstance(click, RuleGif)) and (stop is None):
            stop = click
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.screenshot()
            if not self.appear(stop):
                return True
            if isinstance(click, RuleImage) or isinstance(click, RuleGif):
                self.appear_then_click(click, interval=interval)
                continue
            elif isinstance(click, RuleClick):
                self.click(click, interval)
                continue
            elif isinstance(click, RuleOcr):
                self.click(click)
                continue
        return False
