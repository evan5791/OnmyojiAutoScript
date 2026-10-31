"""Isolated per-account daily and counted awakening subflows."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from time import monotonic, sleep
from typing import Callable

from pydantic import BaseModel, Field, PrivateAttr

from module.atom.ocr import RuleOcr
from module.atom.swipe import RuleSwipe
from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger
from tasks.AccountSwitch.config import DoneRecord
from tasks.DailyTrifles.config import DailyTrifles, DailyTriflesConfig, SummonType
from tasks.DailyTrifles.script_task import ScriptTask as DailyTask
from tasks.EvoZone.config import Layer, UserStatus
from tasks.EvoZone.script_task import ScriptTask as EvoTask
from tasks.Component.GeneralBattle.battle_wait import battle_wait_options
from tasks.GameUi.page import page_awake_zones, page_main, page_summon
from tasks.DailyTrifles.page import page_store_gift_room
from tasks.Restart.assets import RestartAssets
from tasks.Restart.login import LoginHandler


def wait_for(task, predicate, seconds: float, action=None) -> bool:
    """Wait on fresh frames without relying on version-specific UI helpers."""
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        task.screenshot()
        if predicate():
            return True
        if action is not None:
            action()
        sleep(0.05)
    return False


def click_until_gone(task, rule, interval: float = 0.6, timeout: float = 10,
                     confirm_frames: int = 1, max_clicks: int | None = None) -> bool:
    """Return True only after at least one click and confirmed disappearance."""
    deadline = monotonic() + timeout
    clicked, clicks, missing = False, 0, 0
    while monotonic() < deadline:
        task.screenshot()
        if not task.appear(rule):
            missing += 1
            if missing >= confirm_frames:
                return clicked
            continue
        missing = 0
        if max_clicks is not None and clicks >= max_clicks:
            return False
        if task.appear_then_click(rule, interval=interval):
            clicked = True
            clicks += 1
        sleep(0.05)
    return False


@dataclass(frozen=True)
class DailyResult:
    done_record: DoneRecord
    success: bool
    pending: tuple[str, ...] = ()


@dataclass(frozen=True)
class AwakeningResult:
    requested: int
    completed: int
    success: bool
    reason: str = ''


class _ModelView:
    def __init__(self, model, overrides):
        self._source = model
        self._copies = overrides

    def __getattr__(self, name):
        if name not in self._copies:
            value = getattr(self._source, name)
            # Other task/global settings are copied lazily if a reused helper
            # accesses them. Assignments never reach the parent's ConfigModel.
            self._copies[name] = value.model_copy(deep=True) if isinstance(value, BaseModel) else deepcopy(value)
        return self._copies[name]


class IsolatedConfig:
    """Map both config.task and config.model.task to private task settings."""

    def __init__(self, source, overrides, on_save=None):
        self._source = source
        self._on_save = on_save
        self.model = _ModelView(source.model, overrides)
        self.cooperation_runtime = None
        self.task_runtime_outcome = None

    def __getattr__(self, name):
        try:
            return getattr(self.model, name)
        except AttributeError:
            return getattr(self._source, name)

    def save(self):
        if self._on_save is not None:
            self._on_save()

    def reload(self):
        return None

    def task_delay(self, *args, **kwargs):
        return None

    def task_call(self, *args, **kwargs):
        return False


class _RoutineDailyOptions(DailyTriflesConfig):
    courtyard_affairs: bool = True
    pickup_email: bool = True


class _RoutineDailyConfig(DailyTrifles):
    trifles_config: _RoutineDailyOptions = Field(default_factory=_RoutineDailyOptions)
    done_record: DoneRecord = Field(default_factory=DoneRecord)
    _routine_day: date | None = PrivateAttr(default=None)

    def today_is_done(self, mode: str) -> bool:
        completed_at = getattr(self.done_record, f'{mode}_dt', None)
        if self._routine_day is None:
            within_routine = completed_at is not None and completed_at.date() == datetime.now().date()
        else:
            start = datetime.combine(self._routine_day, time(hour=18))
            within_routine = completed_at is not None and start <= completed_at < start + timedelta(days=1)
        if mode == 'guild_donate':
            if not within_routine:
                self.done_record.guild_donate_finish = False
            return within_routine and self.done_record.guild_donate_finish
        return within_routine


class AccountDailyTask(DailyTask, RestartAssets):
    harvest_courtyard_affairs = LoginHandler.harvest_courtyard_affairs
    harvest_mail = LoginHandler.harvest_mail

    def run(self):
        settings = self.config.daily_trifles.trifles_config
        for name, enabled in (
            ('one_summon', settings.one_summon),
            ('courtyard_affairs', settings.courtyard_affairs),
            ('pickup_email', settings.pickup_email),
            ('friend_love', settings.friend_love),
            ('luck_msg', settings.luck_msg),
        ):
            if enabled:
                getattr(self, f'run_{name}')()
        if settings.guild_wish:
            # Upstream run_guild_wish is a pass-only stub. Do not invent a
            # successful checkpoint for an action the task cannot perform.
            logger.warning('上游寮祈愿尚未实现，保留未完成状态')
        if settings.store_sign or settings.buy_sushi_count > 0:
            self.run_store()
        self.config.save()
        raise TaskEnd('AccountSwitch daily')

    def plan_next_dt(self):
        # The parent AccountSwitch task owns the next run time.
        return None

    def run_courtyard_affairs(self):
        daily = self.config.daily_trifles
        if not daily.today_is_done('courtyard_affairs'):
            self.goto_page(page_main)
            if self.harvest_courtyard_affairs():
                daily.done_record.courtyard_affairs_dt = datetime.now()
            self.goto_page(page_main)
        self.config.save()

    def run_pickup_email(self):
        daily = self.config.daily_trifles
        if not daily.today_is_done('pickup_email'):
            self.goto_page(page_main)
            self.screenshot()
            # False explicitly means that no mail entry/read-all button is
            # visible. Checking an empty inbox still completes this check.
            self.harvest_mail()
            self.goto_page(page_main)
            daily.done_record.pickup_email_dt = datetime.now()
        self.config.save()

    def run_luck_msg(self):
        daily = self.config.daily_trifles
        if not daily.today_is_done('luck_msg'):
            super().run_luck_msg()
            daily.done_record.luck_msg_dt = datetime.now()
        self.config.save()

    def run_friend_love(self):
        daily = self.config.daily_trifles
        if not daily.today_is_done('friend_love'):
            super().run_friend_love()
            daily.done_record.friend_love_dt = datetime.now()
        self.config.save()

    def run_store(self):
        daily = self.config.daily_trifles
        settings = daily.trifles_config
        if ((settings.store_sign and not daily.today_is_done('store_sign'))
                or (settings.buy_sushi_count > 0 and not daily.today_is_done('sushi'))):
            super().run_store()
        self.config.save()

    def run_buy_sushi(self):
        daily = self.config.daily_trifles
        if not daily.today_is_done('sushi'):
            super().run_buy_sushi()
            daily.done_record.sushi_dt = datetime.now()
        self.config.save()

    def run_store_sign(self):
        daily = self.config.daily_trifles
        if daily.today_is_done('store_sign'):
            return
        # The base implementation stamps completion before navigating. Here a
        # real reward must have appeared before the account record is advanced.
        self.goto_page(page_store_gift_room)
        self.screenshot()
        self.appear_then_click(self.I_GIFT_RECOMMEND, interval=1)
        sleep(1)
        reward_seen = False
        deadline = Timer(15).start()
        while not deadline.reached():
            self.screenshot()
            if self.ui_reward_appear_click():
                reward_seen = True
                continue
            if reward_seen and not self.appear(self.I_UI_REWARD):
                daily.done_record.store_sign_dt = datetime.now()
                self.config.save()
                return
            self.appear_then_click(self.I_GIFT_SIGN, interval=2.5)
        logger.warning('小号商店签到未确认领奖，保留未完成状态')

    def run_one_summon(self):
        daily = self.config.daily_trifles
        if daily.today_is_done('summon'):
            return
        self.goto_page(page_summon)
        recall = daily.trifles_config.summon_type == SummonType.recall
        if not self._summon_once(recall):
            logger.warning('小号每日召唤未确认成功，保留未完成状态')
            return
        self.back_summon_main()
        daily.done_record.summon_dt = datetime.now()
        self.check_time()
        self.config.save()

    def _summon_once(self, recall: bool) -> bool:
        if recall:
            found = False
            for _ in range(3):
                for select in (self.O_SELECT_SM2, self.O_SELECT_SM3, self.O_SELECT_SM4):
                    self.goto_page(page_summon)
                    self.appear_then_click(self.I_UI_BACK_RED, interval=0.6)
                    self.device.click(*select.coord())
                    sleep(1)
                    self.screenshot()
                    if self.appear(self.I_RECALL_TICKET):
                        found = True
                        break
                if found:
                    break
            if not found:
                return False
        ticket = self.I_RECALL_TICKET if recall else self.I_BLUE_TICKET
        count_rule = self.O_RECALL_TICKET_AREA if recall else self.O_ONE_TICKET
        draw_page = self.I_RECALL_ONE_TICKET if recall else self.I_ONE_TICKET
        if not wait_for(self, lambda: self.appear(ticket), 10):
            return False
        count_match = re.search(r'\d+', str(count_rule.ocr(self.device.image) or ''))
        if not count_match or int(count_match.group()) <= 0:
            return False
        if not wait_for(self, lambda: self.appear(draw_page), 15,
                        action=lambda: self.appear_then_click(ticket, interval=1)):
            return False
        deadline = Timer(45).start()
        confirmations = (self.I_RECALL_SM_CONFIRM, self.I_SM_CONFIRM_2) if recall else (self.I_SM_CONFIRM, self.I_SM_CONFIRM_2)
        while not deadline.reached():
            self.screenshot()
            for confirmation in confirmations:
                if self.appear(confirmation):
                    return click_until_gone(self, confirmation, interval=0.6, timeout=10)
            if self.appear(draw_page, interval=1):
                if self.appear_then_click(self.I_UI_CANCEL, interval=0.8):
                    continue
                if not recall and self.config.daily_trifles.trifles_config.draw_mystery_pattern:
                    self.summon_mystery_pattern()
                else:
                    self.summon()
        return False


def _daily_pending(daily: DailyTrifles) -> tuple[str, ...]:
    settings = daily.trifles_config
    enabled = (
        ('summon', settings.one_summon),
        ('courtyard_affairs', settings.courtyard_affairs),
        ('pickup_email', settings.pickup_email),
        ('guild_wish', settings.guild_wish),
        ('friend_love', settings.friend_love),
        ('luck_msg', settings.luck_msg),
        ('store_sign', settings.store_sign),
        ('sushi', settings.buy_sushi_count > 0),
    )
    return tuple(name for name, is_enabled in enabled if is_enabled and not daily.today_is_done(name))


def run_daily(config, device, done_record: DoneRecord | dict,
              on_progress: Callable[[DoneRecord], None] | None = None,
              routine_day: date | str | None = None) -> DailyResult:
    """Run the parent's selected daily options against one account's record.

    on_progress is the only persistence hook. It receives a detached DoneRecord
    after each completed substep and on exceptions; the parent saves it under
    its account/platform key. This function never saves the main configuration.
    """
    daily = _RoutineDailyConfig.model_validate(config.daily_trifles.model_dump())
    account_settings = getattr(getattr(config, 'account_switch', None), 'account_switch_config', None)
    if account_settings is not None:
        daily.trifles_config.courtyard_affairs = account_settings.daily_courtyard_affairs
        daily.trifles_config.pickup_email = account_settings.daily_pickup_email
    daily.done_record = DoneRecord.model_validate(
        done_record.model_dump() if isinstance(done_record, DoneRecord) else done_record
    )
    daily._routine_day = date.fromisoformat(routine_day) if isinstance(routine_day, str) else routine_day

    def checkpoint():
        if on_progress is not None:
            on_progress(daily.done_record.model_copy(deep=True))

    isolated = IsolatedConfig(config, {'daily_trifles': daily}, on_save=checkpoint)
    try:
        AccountDailyTask(isolated, device).run()
    except TaskEnd:
        pass
    finally:
        checkpoint()
    pending = _daily_pending(daily)
    return DailyResult(daily.done_record.model_copy(deep=True), not pending, pending)


class AccountAwakeningTask(EvoTask):
    def _select_account_layer(self, layer: Layer) -> bool:
        # Accept either "壹" or "壹层" from the SDK's narrow layer column.
        # Unlike RuleList's empty-OCR (0, 0) fallback, no OCR match means failure.
        column = tuple(self.L_LAYER_LIST.roi_back)
        rule = RuleOcr(roi=column, area=column, mode='Full', method='Default', keyword='',
                       name='account_switch_awakening_layer')
        layer_names = [entry.value.removesuffix('层') for entry in Layer]
        target = layer_names.index(layer.value.removesuffix('层'))
        x, y, width, height = column
        previous = None
        for _ in range(12):
            self.screenshot()
            rows = rule.detect_and_ocr(self.device.image)
            recognized = []
            for row in rows:
                if getattr(row, 'score', 1) < 0.5:
                    continue
                text = ''.join(row.ocr_text.split()).removesuffix('层').removeprefix('第')
                index = layer_names.index(text) if text in layer_names else int(text) - 1 if text.isdigit() else -1
                if not 0 <= index < 10:
                    continue
                recognized.append(index)
                if index == target:
                    box = row.box
                    left, top = box[0][0], box[0][1]
                    box_width, box_height = box[1][0] - left, box[2][1] - top
                    if box_width > 0 and box_height > 0:
                        self.device.click(int(x + left + box_width / 2), int(y + top + box_height / 2))
                        return True
            fingerprint = tuple(row.ocr_text for row in rows)
            if fingerprint and fingerprint == previous:
                break
            previous = fingerprint
            toward_bottom = not recognized or target > max(recognized)
            start = (x + width // 2, y + int(height * (0.75 if toward_bottom else 0.25)), 4, 4)
            end = (x + width // 2, y + int(height * (0.25 if toward_bottom else 0.75)), 4, 4)
            self.swipe(RuleSwipe(start, end, mode='default', name='account_switch_awakening_layers'))
            sleep(0.8)
        return False

    def run_counted(self, count: int, on_progress: Callable[[int], None] | None) -> AwakeningResult:
        self.goto_page(page_awake_zones)
        if not self.evozone_enter():
            return AwakeningResult(count, 0, False, 'entry_failed')
        settings = self.config.evo_zone
        if not self._select_account_layer(settings.evo_zone_config.layer):
            return AwakeningResult(count, 0, False, 'layer_not_found')
        self.check_lock(True)
        completed = 0
        while completed < count:
            if not wait_for(self, lambda: self.appear(self.I_EVOZONE_FIRE), 20):
                return AwakeningResult(count, completed, False, 'challenge_page_missing')
            if not click_until_gone(self, self.I_EVOZONE_FIRE, interval=1, timeout=20,
                                    confirm_frames=2, max_clicks=10):
                return AwakeningResult(count, completed, False, 'challenge_not_started')
            with battle_wait_options(
                completion={'check_imgs': [self.I_EVOZONE_FIRE]},
                preset={'preset_enable': False}, green={'green_enable': False},
                prepare={'lock_team': True},
            ):
                won = self.run_general_battle(config=settings.general_battle_config)
            if not won:
                return AwakeningResult(count, completed, False, 'battle_lost')
            # GeneralBattle.current_count counts starts, including losses. Only
            # a settled win contributes to the account's persistent daily count.
            completed += 1
            if on_progress is not None:
                on_progress(completed)
        return AwakeningResult(count, completed, True)


def run_awakening(config, device, layer: int = 1, count: int = 14,
                  on_progress: Callable[[int], None] | None = None) -> AwakeningResult:
    """Run exactly count remaining solo wins, reporting this call's win count.

    The parent adds completed to its saved count and passes only the remainder
    on retries. There is no inherited 30-minute task limit or EvoZone scheduling.
    """
    if isinstance(layer, bool) or not isinstance(layer, int) or not 1 <= layer <= 10:
        raise ValueError('觉醒层数必须为 1 至 10')
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError('觉醒剩余次数必须为非负整数')
    if count == 0:
        return AwakeningResult(0, 0, True)
    evo = config.evo_zone.model_copy(deep=True)
    evo.evo_zone_config.user_status = UserStatus.ALONE
    evo.evo_zone_config.layer = list(Layer)[layer - 1]
    evo.evo_zone_config.limit_count = count
    evo.evo_zone_config.soul_buff_enable = False
    for field in ('friend_list', 'cooperation_script', 'friend_1', 'friend_2'):
        if hasattr(evo.invite_config, field):
            setattr(evo.invite_config, field, '')
    if hasattr(evo.invite_config, 'default_invite'):
        evo.invite_config.default_invite = False
    evo.switch_soul_config.enable = False
    evo.switch_soul_config.enable_switch_by_name = False
    battle = evo.general_battle_config
    battle.preset_enable = False
    battle.green_enable = False
    battle.lock_team_enable = True
    for field, value in (('continuous_battle', False), ('max_continuous', 0), ('quick_exit', False)):
        if hasattr(battle, field):
            setattr(battle, field, value)
    isolated = IsolatedConfig(config, {'evo_zone': evo})
    return AccountAwakeningTask(isolated, device).run_counted(count, on_progress)
