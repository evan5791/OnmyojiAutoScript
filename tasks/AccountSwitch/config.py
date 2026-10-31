from datetime import time

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


class AccountSwitchConfig(ConfigBase):
    accounts: MultiLine = Field(default="", description="accounts_help")
    apple_or_android: bool = Field(default=True, description="apple_or_android_help")
    auto_jade: bool = Field(default=False, description="auto_jade_help")
    invite_friend_name: str = Field(default="", description="account_switch_invite_friend_name_help")
    routine_enable: bool = Field(default=False, description="account_routine_enable_help")
    daily_courtyard_affairs: bool = Field(default=True, description='account_daily_courtyard_affairs_help')
    daily_pickup_email: bool = Field(default=True, description='account_daily_pickup_email_help')
    daytime_run_time: Time = Field(default=time(17), description="account_daytime_run_time_help")
    evening_run_time: Time = Field(default=time(20), description="account_evening_run_time_help")
    awakening_layer: int = Field(default=1, ge=1, le=10, description="account_awakening_layer_help")
    awakening_count: int = Field(default=14, ge=1, description="account_awakening_count_help")
    retry_minutes: int = Field(default=10, ge=1, le=60, description="account_retry_minutes_help")

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

    def get_accounts(self) -> list[AccountInfo]:
        """Read the SDK's remembered account names, preserving the configured order."""
        accounts = []
        seen = set()
        for line_number, line in enumerate(self.accounts.splitlines(), 1):
            if not line.strip():
                continue
            account, *aliases = (part.strip() for part in line.split('#'))
            if not account:
                raise ValueError(f"第 {line_number} 行缺少账号名称，请填写账号后再用 # 添加识别别名")
            key = account.casefold()
            if key in seen:
                continue
            seen.add(key)
            accounts.append(AccountInfo(
                account=account,
                account_alias='#'.join(alias for alias in aliases if alias),
                svr='',
                apple_or_android=self.apple_or_android,
            ))
        return accounts


class AccountRoutineProgress(ConfigBase):
    day: str = ''
    daily_done: bool = False
    daily_record: DoneRecord = Field(default_factory=DoneRecord)
    awakening_completed: int = 0


class JadePeriodProgress(ConfigBase):
    completed: bool = False
    owner_account: str = ''
    pending: dict = Field(default_factory=dict)
    checked_accounts: list[str] = Field(default_factory=list)


class AccountRoutineState(ConfigBase):
    accounts: dict[str, AccountRoutineProgress] = Field(default_factory=dict)
    periods: dict[str, JadePeriodProgress] = Field(default_factory=dict)


class AccountSwitch(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    account_switch_config: AccountSwitchConfig = Field(default_factory=AccountSwitchConfig)
    runtime: AccountRoutineState = Field(default_factory=AccountRoutineState)

    hide_fields = dynamic_hide('runtime')
