"""A single, resumable jade cooperation owned by one alt account."""

from dataclasses import dataclass
from time import monotonic
from typing import Callable
from unicodedata import normalize

from module.atom.click import RuleClick
from module.atom.image import RuleImage
from module.exception import TaskEnd
from module.logger import logger
from tasks.AccountSwitch.flows import IsolatedConfig, click_until_gone, wait_for
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Exploration.config import AutoRotate, UserStatus
from tasks.GameUi.page import page_exploration, page_main
from tasks.WantedQuests.config import CooperationSelectMaskDescription, CooperationType, WantedQuestsConfig
from tasks.WantedQuests.script_task import ScriptTask as WantedQuestsTask


@dataclass(frozen=True)
class JadeResult:
    status: str
    pending: dict | None = None
    reason: str = ''

    @property
    def completed(self) -> bool:
        return self.status == 'completed'


class JadeCooperationTask(WantedQuestsTask):
    """Complete only the selected jade quest and return evidence-based status.

    A persisted ``sending`` checkpoint is deliberately conservative: after an
    interruption the invitation is never sent again automatically. The parent
    keeps this alt as the period owner until completion can be confirmed.
    """

    JADE_TIMEOUT_SECONDS = 600
    WQ_LIST_MAX_SCROLLS = 4
    C_WQ_TRACE_ONE_CLOSE = RuleClick(
        roi_front=(170, 30, 400, 100), roi_back=(170, 30, 400, 100),
        name='account_switch_close_quest_detail',
    )
    I_JADE_TRACE_ENABLE_NEW = RuleImage(
        roi_front=(1141, 597, 84, 88), roi_back=(1136, 592, 94, 98),
        threshold=0.8, method='Template matching',
        file='./tasks/AccountSwitch/assets/wq_trace_enable_new.png',
    )
    I_JADE_TRACE_DISABLE_NEW = RuleImage(
        roi_front=(1141, 597, 84, 88), roi_back=(1136, 592, 94, 98),
        threshold=0.8, method='Template matching',
        file='./tasks/AccountSwitch/assets/wq_trace_disable_new.png',
    )

    def __init__(self, config, device, invite_friend_name: str):
        # Exploration's cached _config writes lock_team_enable and its battle
        # helper reads the exploration preset directly. Keep both private and
        # disable preset changes, automatic fodder rotation and team invites.
        exploration = config.exploration.model_copy(deep=True)
        exploration.general_battle_config = GeneralBattleConfig(lock_team_enable=True)
        exploration.exploration_config.user_status = UserStatus.ALONE
        exploration.exploration_config.auto_rotate = AutoRotate.no
        exploration.switch_soul_config.enable = False
        exploration.switch_soul_config.enable_switch_by_name = False
        exploration.scrolls.scrolls_enable = False
        isolated = IsolatedConfig(config, {'exploration': exploration})
        super().__init__(isolated, device)
        self.invite_friend_name = invite_friend_name.strip()
        self.unwanted_boss_name_list = []
        self.wq_executed_set = set()
        self.want_strategy_excluding = []
        self.battle_config = GeneralBattleConfig(lock_team_enable=True)
        self._jade_config = WantedQuestsConfig.model_validate(
            config.wanted_quests.wanted_quests_config.model_dump()
        )
        self._jade_config.invite_friend_name = self.invite_friend_name
        self._jade_config.cooperation_only = True
        self._jade_config.cooperation_type = CooperationSelectMaskDescription.JadeOnly
        self._pending = None
        self._on_checkpoint = None

    @staticmethod
    def _clean(text: str) -> str:
        return ''.join(normalize('NFKC', str(text)).split()).casefold()

    def get_config(self) -> WantedQuestsConfig:
        return self._jade_config

    def get_cooperation_info(self) -> list:
        self.screenshot()
        # A previous invitation can remove the first '+' while another card's
        # '+' remains. Inspect all slots instead of stopping at that first gap.
        return [
            {'type': CooperationType.Jade, 'inviteBtn': getattr(self, f'I_WQ_INVITE_{slot}')}
            for slot in range(1, 4)
            if self.appear(getattr(self, f'I_WQ_INVITE_{slot}'))
            and self.appear(getattr(self, f'I_WQ_COOPERATION_TYPE_JADE_{slot}'))
        ]

    def next_run(self):
        # This subtask never changes the independent WantedQuests schedule.
        return None

    def _checkpoint(self, stage: str, **updates) -> None:
        self._pending = {**(self._pending or {}), **updates, 'stage': stage}
        if self._on_checkpoint is not None:
            self._on_checkpoint(dict(self._pending))

    def _result(self, status: str, reason: str = '') -> JadeResult:
        return JadeResult(status, dict(self._pending) if self._pending else None, reason)

    def _wait(self, predicate, seconds: float) -> bool:
        return wait_for(self, predicate, seconds)

    @staticmethod
    def _wq_box_xywh(box) -> list[int]:
        xs = [int(point[0]) for point in box]
        ys = [int(point[1]) for point in box]
        left, right = min(xs), max(xs)
        top, bottom = min(ys), max(ys)
        return [left, top, right - left, bottom - top]

    def find_wq(self, image, *, allow_edge_fallback: bool = False):
        # dev's finder accepts only the image. Keep the feature's caller shape
        # local rather than changing the shared WantedQuests parser/API.
        return super().find_wq(image)

    def open_wq_info(self) -> bool:
        return self._click_until(self.O_WQ_TEXT_ALL, self.I_TRACE_TRUE, seconds=5)

    def execute_mission(self, num_want: int):
        # dev opens the selected tracker inside this method and requires the
        # OCR click target and challenge-ticket count as separate arguments.
        number = self.O_WQ_NUMBER.ocr(self.device.image)
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = 0
        return super().execute_mission(self.O_WQ_TEXT_ALL, num_want, number)

    def _click_until(self, click, stop, seconds: float = 8) -> bool:
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            self.screenshot()
            if self.appear(stop):
                return True
            self.click(click, interval=0.8)
        return False

    def _open_wanted_panel(self) -> bool:
        self.goto_page(page_main)
        deadline = monotonic() + 15
        while monotonic() < deadline:
            self.screenshot()
            if self._tracking_pair() is not None:
                return True
            if self.appear_then_click(self.I_WQ_SEAL, interval=1):
                continue
            if self.appear_then_click(self.I_WQ_DONE, interval=1):
                continue
            if self.special_main:
                self.click(self.C_SPECIAL_MAIN, interval=3)
        return False

    def _close_detail(self) -> bool:
        self.screenshot()
        self.click(self.I_UI_BACK_RED if self.appear(self.I_UI_BACK_RED) else self.C_WQ_TRACE_ONE_CLOSE)
        return self._wait(lambda: not self.appear(self.I_WQ_TRACE_ONE_CHECK_OPENED), 5)

    def _open_card_detail(self, slot: int) -> bool:
        button = getattr(self, f'I_WQ_INVITE_{slot}')
        x, y, _, _ = button.roi_front
        click = RuleClick(
            roi_front=(x, y - 42, 18, 12),
            roi_back=(x, y - 42, 18, 12),
            name='account_switch_jade_detail',
        )
        return self._click_until(click, self.I_WQ_TRACE_ONE_CHECK_OPENED)

    def _read_card_monster(self, slot: int) -> str:
        if not self._open_card_detail(slot):
            return ''
        self.screenshot()
        monster = self._clean(self.O_WQ_MONSTER_TYPE.detect_text(self.device.image))
        if not self._close_detail():
            return ''
        return monster

    def _tracking_pair(self):
        for enable, disable in (
            (self.I_TRACE_ENABLE, self.I_TRACE_DISABLE),
            (self.I_JADE_TRACE_ENABLE_NEW, self.I_JADE_TRACE_DISABLE_NEW),
        ):
            if self.appear(enable) or self.appear(disable):
                return enable, disable
        return None

    def _clear_tracking(self) -> bool:
        self.screenshot()
        pair = self._tracking_pair()
        if pair is None:
            return False
        enable, disable = pair
        return self._click_until(enable, disable) and self._click_until(disable, enable)

    def _find_owned_card(self) -> int | None:
        """The invite '+' may disappear or cards may move after acceptance."""
        preferred = int(self._pending['slot'])
        expected = self._pending['monster']
        for slot in [preferred, *[n for n in range(1, 4) if n != preferred]]:
            self.screenshot()
            if not self.appear(getattr(self, f'I_WQ_COOPERATION_TYPE_JADE_{slot}')):
                continue
            monster = self._read_card_monster(slot)
            if monster and monster == expected:
                return slot
        return None

    def _track_owned_card(self) -> bool:
        slot = self._find_owned_card()
        if slot is None or not self._clear_tracking() or not self._open_card_detail(slot):
            return False
        self.screenshot()
        if self._clean(self.O_WQ_MONSTER_TYPE.detect_text(self.device.image)) != self._pending['monster']:
            self._close_detail()
            return False
        if self.appear(self.I_WQ_TRACE_ONE_REALWORLD):
            self._close_detail()
            return False
        tracked = self._click_until(self.I_WQ_TRACE_ONE_DISABLE, self.I_WQ_TRACE_ONE_ENABLE)
        closed = self._close_detail()
        if tracked and closed:
            self._checkpoint('tracked', slot=slot)
            return True
        return False

    def _select_same_server_friend(self) -> bool:
        self.screenshot()
        # Stay on the same-server tab even when a previous manual action used
        # the cross-server tab. Never search or invite across servers.
        if not self.appear(self.I_WQ_INVITE_SAME_SVR):
            return False
        self.click(self.I_WQ_INVITE_SAME_SVR)
        if not self._wait(
            lambda: self.appear(self.I_WQ_INVITE_FRIEND_LIST_APPEAR)
            and not self.appear(self.I_WQ_INVITE_DIFF_SVR_HIGHLIGHT), 4
        ):
            return False
        wanted = self._clean(self.invite_friend_name)
        for rule in (self.O_WQ_INVITE_COLUMN_1, self.O_WQ_INVITE_COLUMN_2):
            for item in rule.detect_and_ocr(self.device.image):
                if self._clean(item.ocr_text) != wanted:
                    continue
                x, y, width, height = self._wq_box_xywh(item.box)
                area = (x + rule.roi[0], y + rule.roi[1], max(1, width), max(1, height))
                self.click(RuleClick(roi_front=area, roi_back=area, name='account_switch_jade_friend'))
                return self._wait(lambda: self.appear(self.I_WQ_INVITE_SELECTED), 2)
        return False

    def _invite_once(self, slot: int) -> JadeResult | None:
        if not self._fresh_invitable_card(slot):
            return self._result('invite_failed', '勾协卡片已变化，未发出邀请')
        if not self._click_until(getattr(self, f'I_WQ_INVITE_{slot}'), self.I_WQ_INVITE_ENSURE):
            return self._result('invite_failed', '未能打开同区好友邀请列表')
        if not self._select_same_server_friend():
            self.click(self.I_WQ_INVITE_CANCEL)
            self._wait(lambda: not self.appear(self.I_WQ_INVITE_ENSURE), 4)
            # No send checkpoint exists: the parent may safely try another alt.
            return self._result('invite_failed', '同区好友未在线或未能识别指定昵称')
        # Write before sending, closing the crash window where a new alt might
        # otherwise send a second cooperation after the scheduler restarts.
        self._checkpoint('sending')
        self.click(self.I_WQ_INVITE_ENSURE)
        if not self._wait(lambda: not self.appear(self.I_WQ_INVITE_ENSURE), 5):
            return self._result('incomplete', '邀请确认状态不明，保留当前小号等待重试')
        self._checkpoint('sent')
        return None

    def _fresh_invitable_card(self, slot: int) -> bool:
        self.screenshot()
        return (
            self.appear(getattr(self, f'I_WQ_INVITE_{slot}'))
            and self.appear(getattr(self, f'I_WQ_COOPERATION_TYPE_JADE_{slot}'))
            and self._read_card_monster(slot) == self._pending['monster']
        )

    def _finish_tracked_quest(self) -> JadeResult:
        if self.appear(self.I_UI_BACK_RED):
            if not click_until_gone(self, self.I_UI_BACK_RED, timeout=10):
                return self._result('incomplete', '未能关闭悬赏面板')
        self.goto_page(page_exploration)
        deadline = monotonic() + self.JADE_TIMEOUT_SECONDS
        scrolls = 0
        for _ in range(32):
            if monotonic() >= deadline:
                return self._result('incomplete', '勾协未在本次执行时间内完成')
            self.screenshot()
            if self.appear(self.I_WQ_BOX):
                # Only our owned jade task is tracked. Its reward box is the
                # positive completion evidence, unlike a disappearing tracker.
                self._checkpoint('completed')
                self.ui_get_reward(self.I_WQ_BOX)
                return self._result('completed', '已观察到唯一追踪勾协的完成奖励')
            current, found, total, area = self.find_wq(
                self.device.image, allow_edge_fallback=(scrolls >= self.WQ_LIST_MAX_SCROLLS)
            )
            if found < 0:
                if found == -2:
                    return self._result('incomplete', '悬赏进度已满，但尚未观察到完成奖励')
                if scrolls >= self.WQ_LIST_MAX_SCROLLS:
                    return self._result('incomplete', '未发现勾协进度或完成奖励，不能记为成功')
                self.swipe(self.S_WQ_LIST_UP, interval=1)
                scrolls += 1
                continue
            self.O_WQ_TEXT_ALL.area = area
            if not self.open_wq_info():
                return self._result('incomplete', '未能打开当前勾协目标')
            monster = self._clean(self.O_WQ_MONSTER_TYPE.detect_text(self.device.image))
            if monster != self._pending['monster']:
                return self._result('incomplete', '追踪目标与持久化勾协不一致')
            if self.execute_mission(total - current) is False:
                return self._result('incomplete', '当前小号无法自动完成该勾协目标')
            scrolls = 0
        return self._result('incomplete', '勾协执行次数达到保护上限，尚未确认完成')

    def run_jade(self, pending: dict | None = None,
                 on_checkpoint: Callable[[dict], None] | None = None) -> JadeResult:
        self._pending = dict(pending) if pending else None
        self._on_checkpoint = on_checkpoint
        if self._pending and self._pending.get('stage') == 'completed':
            return self._result('completed', '恢复已保存的奖励确认记录')
        if not self.invite_friend_name:
            return self._result('invite_failed', '未配置接收勾协的大号昵称')
        if not self._open_wanted_panel():
            return self._result('incomplete', '未能确认悬赏面板，不能断言没有勾协')
        if self._pending:
            if not self._pending.get('monster') or self._pending.get('slot') not in (1, 2, 3):
                return self._result('incomplete', '已保存的勾协定位信息不完整')
            if self._pending.get('stage') == 'sending':
                return self._result('incomplete', '中断发生在发出邀请时，需确认大号收到后再继续')
        else:
            invitations = self.get_cooperation_info()
            if not invitations:
                return self._result('no_jade', '当前小号没有可邀请的勾玉协作')
            selected = invitations[0]
            slot = next(n for n in range(1, 4) if selected['inviteBtn'] is getattr(self, f'I_WQ_INVITE_{n}'))
            monster = self._read_card_monster(slot)
            if not monster:
                return self._result('incomplete', '未能识别勾协怪物，未发出邀请')
            self._pending = {'slot': slot, 'monster': monster}
            result = self._invite_once(slot)
            if result is not None:
                if result.status == 'invite_failed':
                    self._pending = None
                    return self._result('invite_failed', result.reason)
                return result
        if not self._track_owned_card():
            return self._result('incomplete', '已保留当前勾协，但无法确认其唯一追踪状态')
        try:
            return self._finish_tracked_quest()
        except TaskEnd:
            return self._result('incomplete', '战斗子流程提前结束，尚无勾协完成证据')
