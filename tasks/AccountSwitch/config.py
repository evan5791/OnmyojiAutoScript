from datetime import time
from dataclasses import dataclass
import json
from typing import Literal
from unicodedata import normalize

from pydantic import Field, field_validator

from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.Component.config_base import ConfigBase, DateTime, MultiLine, Time, dynamic_hide
from tasks.Component.config_scheduler import Scheduler


class DoneRecord(ConfigBase):
    """Per-account checkpoints; independent of the upstream daily task model."""

    courtyard_affairs_dt: DateTime = Field(default=DateTime.fromisoformat('2023-01-01 00:00:00'))
    pickup_email_dt: DateTime = Field(default=DateTime.fromisoformat('2023-01-01 00:00:00'))
    summon_dt: DateTime = Field(default=DateTime.fromisoformat('2023-01-01 00:00:00'))
    guild_donate_dt: DateTime = Field(default=DateTime.fromisoformat('2023-01-01 00:00:00'))
    guild_donate_finish: bool = False
    guild_wish_dt: DateTime = Field(default=DateTime.fromisoformat('2023-01-01 00:00:00'))
    friend_love_dt: DateTime = Field(default=DateTime.fromisoformat('2023-01-01 00:00:00'))
    luck_msg_dt: DateTime = Field(default=DateTime.fromisoformat('2023-01-01 00:00:00'))
    store_sign_dt: DateTime = Field(default=DateTime.fromisoformat('2023-01-01 00:00:00'))
    sushi_dt: DateTime = Field(default=DateTime.fromisoformat('2023-01-01 00:00:00'))


def normalize_account_key(account: str) -> str:
    if not isinstance(account, str):
        raise ValueError('账号名称须为文本')
    value = normalize('NFKC', account).strip()
    if not value or '#' in value or any(character.isspace() for character in value):
        raise ValueError('账号名称不能为空、包含空白或 # 别名')
    return value.casefold()


def _target(value):
    if not isinstance(value, str) or len(value) > 100 or any(ord(char) < 32 for char in value):
        raise ValueError('目标昵称须为空或不超过100字的单行文本')
    return value.strip()


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('逐账号 JSON 包含重复字段')
        result[key] = value
    return result


def _account_json(value, *, targets=False):
    if isinstance(value, str):
        try:
            value = json.loads(value.strip() or '{}', object_pairs_hook=_unique_json_object)
        except ValueError as exc:
            raise ValueError('逐账号配置须为 JSON 对象') from exc
    if not isinstance(value, dict):
        raise ValueError('逐账号配置须为 JSON 对象')
    result = {}
    for account, options in value.items():
        key = normalize_account_key(account)
        if key in result:
            raise ValueError('逐账号配置包含规范化后重复的账号')
        if not isinstance(options, dict):
            raise ValueError('每个账号的配置须为 JSON 对象')
        if targets:
            if set(options) - {'jade_receiver', 'assist_friend'}:
                raise ValueError('逐账号目标仅支持 jade_receiver 和 assist_friend')
            result[key] = {name: _target(options.get(name, '')) for name in ('jade_receiver', 'assist_friend')}
        else:
            values = {'android': 'plan', 'ios': 'daily'}
            allowed = {'android': ('plan', 'login', 'off'), 'ios': ('daily', 'login', 'off')}
            for platform, mode in options.items():
                if platform not in allowed or not isinstance(mode, str) or mode not in allowed[platform]:
                    raise ValueError('平台模式须为 android:plan/login/off 或 ios:daily/login/off')
                values[platform] = mode
            result[key] = values
    return json.dumps(result, ensure_ascii=False, indent=2)


def _daily_json(value):
    if isinstance(value, str):
        try:
            value = json.loads(value.strip() or '{}', object_pairs_hook=_unique_json_object)
        except ValueError as exc:
            raise ValueError('免费日常覆盖须为 JSON 对象') from exc
    if (not isinstance(value, dict) or set(value) - {'friend_love', 'luck_msg', 'store_sign'}
            or any(type(enabled) is not bool for enabled in value.values())):
        raise ValueError('免费日常覆盖只支持 friend_love/luck_msg/store_sign 的布尔值')
    return json.dumps(value, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class AccountRow:
    account: str
    aliases: tuple[str, ...]
    android_mode: str = 'plan'
    ios_mode: str = 'daily'
    jade_receiver: str = ''
    assist_friend: str = ''

    @property
    def key(self):
        return normalize_account_key(self.account)

    def as_account_info(self, platform: str) -> AccountInfo:
        if platform not in ('android', 'ios'):
            raise ValueError('平台须为 android 或 ios')
        return AccountInfo(account=self.account, account_alias='#'.join(self.aliases),
                           svr='', apple_or_android=platform == 'android')


class AccountSwitchConfig(ConfigBase):
    accounts: MultiLine = Field(default="", description="accounts_help")
    account_modes: MultiLine = Field(default='{}', description='account_modes_help')
    account_targets: MultiLine = Field(default='{}', description='account_targets_help')
    apple_or_android: bool = Field(default=True, description="apple_or_android_help")
    auto_jade: bool = Field(default=False, description="auto_jade_help")
    invite_friend_name: str = Field(default="", description="account_switch_invite_friend_name_help")
    jade_receiver: str = Field(default='', description='account_jade_receiver_help')
    assist_friend: str = Field(default='', description='account_assist_friend_help')
    routine_enable: bool = Field(default=False, description="account_routine_enable_help")
    ios_daily_enable: bool = Field(default=False, description='account_ios_daily_enable_help')
    ios_daily_run_time: Time = Field(default=time(20), description='account_ios_daily_run_time_help')
    platform_priority: Literal['android', 'ios'] = Field(default='android', description='account_platform_priority_help')
    daily_courtyard_affairs: bool = Field(default=True, description='account_daily_courtyard_affairs_help')
    daily_pickup_email: bool = Field(default=True, description='account_daily_pickup_email_help')
    daily_free_options: MultiLine = Field(default='{}', description='account_daily_free_options_help')
    daytime_run_time: Time = Field(default=time(17), description="account_daytime_run_time_help")
    evening_run_time: Time = Field(default=time(20), description="account_evening_run_time_help")
    daytime_jitter_minutes: int = Field(default=15, ge=0, le=180, description='account_daytime_jitter_minutes_help')
    evening_jitter_minutes: int = Field(default=15, ge=0, le=180, description='account_evening_jitter_minutes_help')
    ios_daily_jitter_minutes: int = Field(default=15, ge=0, le=180, description='account_ios_daily_jitter_minutes_help')
    awakening_layer: int = Field(default=1, ge=1, le=10, description="account_awakening_layer_help")
    awakening_count: int = Field(default=14, ge=1, description="account_awakening_count_help")
    awakening_kirin: Literal['lightning', 'fire', 'wind', 'water'] = Field(default='lightning', description='account_awakening_kirin_help')
    retry_minutes: int = Field(default=10, ge=1, le=60, description="account_retry_minutes_help")

    @field_validator('account_modes', mode='before')
    @classmethod
    def parse_modes(cls, value):
        return _account_json(value)

    @field_validator('account_targets', mode='before')
    @classmethod
    def parse_targets(cls, value):
        return _account_json(value, targets=True)

    @field_validator('daily_free_options', mode='before')
    @classmethod
    def parse_daily_free_options(cls, value):
        return _daily_json(value)

    def get_daily_free_options(self) -> dict[str, bool]:
        return json.loads(_daily_json(self.daily_free_options))

    @field_validator('jade_receiver', 'assist_friend', mode='before')
    @classmethod
    def target_text(cls, value):
        return _target(value)

    @field_validator('daytime_jitter_minutes', 'evening_jitter_minutes', 'ios_daily_jitter_minutes', mode='before')
    @classmethod
    def strict_jitter(cls, value):
        if type(value) is not int or not 0 <= value <= 180:
            raise ValueError('随机偏移分钟须为0至180的整数')
        return value

    @field_validator('daytime_run_time')
    @classmethod
    def daytime_within_refresh_period(cls, value):
        if not time(6) <= value < time(18):
            raise ValueError('白天执行时间须在 06:00 至 18:00 之前')
        return value

    @field_validator('evening_run_time')
    @classmethod
    def evening_after_refresh(cls, value):
        if value < time(18):
            raise ValueError('晚间执行时间须在 18:00 至 24:00 之前')
        return value

    def get_account_rows(self) -> list[AccountRow]:
        """Read the SDK's remembered account names, preserving the configured order."""
        accounts = []
        seen = set()
        modes = json.loads(_account_json(self.account_modes))
        targets = json.loads(_account_json(self.account_targets, targets=True))
        for line_number, line in enumerate(self.accounts.splitlines(), 1):
            if not line.strip():
                continue
            account, *aliases = (part.strip() for part in normalize('NFKC', line).split('#'))
            try:
                key = normalize_account_key(account)
            except ValueError as exc:
                raise ValueError(f"第 {line_number} 行缺少账号名称，请填写账号后再用 # 添加识别别名") from exc
            if key in seen:
                continue
            seen.add(key)
            values = modes.get(key, {'android': 'plan', 'ios': 'daily'})
            per_account = targets.get(key, {})
            accounts.append(AccountRow(
                account=account,
                aliases=tuple(alias for alias in aliases if alias),
                android_mode=values['android'], ios_mode=values['ios'],
                jade_receiver=per_account.get('jade_receiver') or self.jade_receiver.strip() or self.invite_friend_name.strip(),
                assist_friend=per_account.get('assist_friend') or self.assist_friend.strip(),
            ))
        return accounts

    def get_accounts(self, platform: str | None = None) -> list[AccountInfo]:
        platform = platform or ('android' if self.apple_or_android else 'ios')
        return [row.as_account_info(platform) for row in self.get_account_rows()]


class AccountRoutineProgress(ConfigBase):
    day: str = ''
    daily_done: bool = False
    daily_record: DoneRecord = Field(default_factory=DoneRecord)
    awakening_completed: int = 0
    awakening_observed: int = 0
    awakening_script_wins: int = 0
    awakening_daily_done: bool = False
    awakening_assist_friend: str = ''
    login_period: str = ''
    login_day: str = ''
    daily_options: dict[str, bool] = Field(default_factory=dict)
    daily_confirmed: dict[str, bool] = Field(default_factory=dict)


class JadePeriodProgress(ConfigBase):
    completed: bool = False
    owner_account: str = ''
    pending: dict = Field(default_factory=dict)
    checked_accounts: list[str] = Field(default_factory=list)


class AccountRoutineState(ConfigBase):
    accounts: dict[str, AccountRoutineProgress] = Field(default_factory=dict)
    periods: dict[str, JadePeriodProgress] = Field(default_factory=dict)
    schedule: dict[str, dict] = Field(default_factory=dict)


class AccountSwitch(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    account_switch_config: AccountSwitchConfig = Field(default_factory=AccountSwitchConfig)
    runtime: AccountRoutineState = Field(default_factory=AccountRoutineState)

    hide_fields = dynamic_hide('runtime')
