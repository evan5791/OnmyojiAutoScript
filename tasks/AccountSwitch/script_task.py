from datetime import datetime, timedelta

from module.exception import RequestHumanTakeover, TaskEnd
from module.logger import logger
from tasks.AccountSwitch.config import AccountSwitchConfig, AccountRoutineProgress, JadePeriodProgress
from tasks.AccountSwitch.routine import refresh_period, period_run_time, next_routine_run
from tasks.Component.SwitchAccount.switch_account import SwitchAccount
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main
from tasks.WantedQuests.config import CooperationSelectMaskDescription, CooperationType, WantedQuestsConfig
from tasks.WantedQuests.script_task import ScriptTask as WantedQuestsTask


class JadeCooperationTask(WantedQuestsTask):
    """Run the existing wanted-quest flow for one account's jade invitations."""

    def __init__(self, config, device, invite_friend_name: str):
        super().__init__(config, device)
        self.unwanted_boss_name_list = []
        self.wq_executed_set = set()
        self.invitation_failed = False
        self._jade_config = WantedQuestsConfig.model_validate(
            config.wanted_quests.wanted_quests_config.model_dump()
        )
        self._jade_config.invite_friend_name = invite_friend_name
        self._jade_config.cooperation_only = True
        self._jade_config.cooperation_type = CooperationSelectMaskDescription.JadeOnly

    def get_config(self) -> WantedQuestsConfig:
        return self._jade_config

    def get_cooperation_info(self) -> list:
        # The shared flow stops at the first excluded type. Filtering here also
        # finds jade when a gold/food cooperation appears before it.
        return [item for item in super().get_cooperation_info() if item['type'] == CooperationType.Jade]

    def all_cooperation_invite(self, name_all: str = None):
        if not self.get_cooperation_info():
            logger.info('当前小号没有可邀请的勾玉协作')
            raise TaskEnd('AccountSwitch: no jade cooperation')

        # Reset old tracking so the shared battle loop only sees the jade
        # cooperations successfully invited and traced during this run.
        self.ui_click(self.I_TRACE_ENABLE, self.I_TRACE_DISABLE, interval=1)
        self.ui_click(self.I_TRACE_DISABLE, self.I_TRACE_ENABLE, interval=1)
        invitations = super().all_cooperation_invite(name_all)
        if not invitations:
            raise TaskEnd('AccountSwitch: no jade cooperation')
        self.invitation_failed = any(not item.get('inviteResult') for item in invitations)
        if not any(item.get('inviteResult') for item in invitations):
            raise TaskEnd('AccountSwitch: jade invitation failed')
        return invitations

    def next_run(self):
        # AccountSwitch owns scheduling. Running on an alt must not delay the
        # separate WantedQuests task in the current OASX configuration.
        return None


class ScriptTask(GameUi):
    def run(self):
        settings: AccountSwitchConfig = self.config.account_switch.account_switch_config
        try:
            accounts = settings.get_accounts()
        except ValueError as exc:
            raise RequestHumanTakeover(str(exc)) from exc
        if not accounts:
            raise RequestHumanTakeover('请在「小号切换」的账号列表中每行填写一个已登录的账号名称')

        invite_name = settings.invite_friend_name.strip()
        if settings.auto_jade or settings.routine_enable:
            if not invite_name:
                invite_name = self.config.wanted_quests.wanted_quests_config.invite_friend_name.strip()
            if settings.routine_enable and not invite_name:
                invite_name = '5791'
            if not invite_name:
                raise RequestHumanTakeover('已开启登录后自动勾协，请填写邀请好友昵称，或先配置「悬赏封印」的邀请好友')

        if settings.routine_enable:
            self.run_routine(settings, accounts, invite_name)
            return

        completed = []
        failed = []
        for index, account in enumerate(accounts, 1):
            logger.hr(f'小号切换 {index}/{len(accounts)}: {account.account}')
            if not SwitchAccount(self.config, self.device, account).switchAccount():
                logger.warning('小号登录失败：%s', account.account)
                failed.append(account.account)
                continue
            if settings.auto_jade and not self.run_jade_cooperation(invite_name):
                logger.warning('小号已登录，但勾玉协作邀请失败：%s', account.account)
                failed.append(account.account)
                continue
            completed.append(account.account)

        logger.info('小号切换结束：完成 %d/%d，失败 %d', len(completed), len(accounts), len(failed))
        if failed:
            logger.warning('失败账号：%s', '、'.join(failed))
        self.config.task_runtime_outcome = {
            'task': 'AccountSwitch',
            'status': 'account_switch_failed' if failed else 'completed',
            'completed_accounts': completed,
            'failed_accounts': failed,
        }
        self.set_next_run('AccountSwitch', success=not failed, finish=True)
        raise TaskEnd('AccountSwitch')

    def run_jade_cooperation(self, invite_name: str) -> bool:
        task = JadeCooperationTask(self.config, self.device, invite_name)
        try:
            task.run()
        except TaskEnd:
            pass
        task.goto_page(page_main)
        return not task.invitation_failed

    @staticmethod
    def account_key(account) -> str:
        platform = 'android' if account.apple_or_android else 'ios'
        return f'{account.account.strip().casefold()}|{platform}'

    def run_routine(self, settings, accounts, invite_name):
        from tasks.AccountSwitch.flows import run_daily, run_awakening
        from tasks.AccountSwitch.jade import JadeCooperationTask as RoutineJadeTask

        now = datetime.now()
        period = refresh_period(now)
        planned = period_run_time(period, settings)
        if now < planned:
            logger.info('小号日程尚未到执行时间：%s', planned)
            self.config.save()
            self.set_next_run('AccountSwitch', target=planned, server=False)
            raise TaskEnd('AccountSwitch: waiting for scheduled time')

        runtime = self.config.account_switch.runtime
        key = period.key(invite_name)
        slot = runtime.periods.setdefault(key, JadePeriodProgress())
        # Retain recent refresh periods so restarts and duplicate runs keep their quota.
        cutoff = (period.start - timedelta(days=3)).isoformat()
        for old_key in list(runtime.periods):
            if old_key.split('|', 1)[0] < cutoff:
                del runtime.periods[old_key]
        self.config.save()

        completed, failed = [], []
        if slot.owner_account:
            accounts = sorted(accounts, key=lambda item: self.account_key(item) != slot.owner_account)
            if not any(self.account_key(item) == slot.owner_account for item in accounts):
                logger.warning('本轮待完成勾协的小号已不在账号列表，保留邀请记录以免重复派发')
                failed.append(slot.owner_account)

        def mark_failed(account):
            if account.account not in failed:
                failed.append(account.account)

        for index, account in enumerate(accounts, 1):
            account_key = self.account_key(account)
            progress = runtime.accounts.setdefault(account_key, AccountRoutineProgress())
            if period.evening and progress.day != period.day:
                progress = AccountRoutineProgress(day=period.day)
                runtime.accounts[account_key] = progress
                self.config.save()

            jade_due = (
                not slot.completed
                and account_key not in slot.checked_accounts
                and (not slot.owner_account or slot.owner_account == account_key)
                and refresh_period(datetime.now()).start == period.start
            )
            daily_due = period.evening and not progress.daily_done
            awakening_due = period.evening and progress.awakening_completed < settings.awakening_count
            if not (jade_due or daily_due or awakening_due):
                continue

            logger.hr(f'小号日程 {index}/{len(accounts)}: {account.account}')
            if not SwitchAccount(self.config, self.device, account).switchAccount():
                mark_failed(account)
                continue

            if daily_due:
                def save_daily(record):
                    progress.daily_record = record.model_copy(deep=True)
                    self.config.save()

                result = run_daily(
                    self.config, self.device, progress.daily_record,
                    on_progress=save_daily, routine_day=period.day,
                )
                save_daily(result.done_record)
                progress.daily_done = result.success
                self.config.save()
                if not result.success:
                    logger.warning('每日琐事尚未完成：%s', result.pending)
                    mark_failed(account)
                self.goto_page(page_main)

            if jade_due and refresh_period(datetime.now()).start == period.start:
                def save_jade(pending):
                    slot.owner_account = account_key
                    slot.pending = dict(pending)
                    self.config.save()

                result = RoutineJadeTask(self.config, self.device, invite_name).run_jade(
                    pending=dict(slot.pending) or None, on_checkpoint=save_jade,
                )
                if result.status == 'completed':
                    slot.completed = True
                    slot.owner_account = account_key
                    slot.pending = {}
                    logger.info('本刷新时段已向 %s 派发并完成勾协，其余小号跳过勾协', invite_name)
                elif result.status == 'no_jade':
                    if account_key not in slot.checked_accounts:
                        slot.checked_accounts.append(account_key)
                    slot.owner_account = ''
                    slot.pending = {}
                else:
                    mark_failed(account)
                    if result.pending:
                        save_jade(result.pending)
                    elif result.status == 'invite_failed':
                        slot.owner_account = ''
                        slot.pending = {}
                    logger.warning('勾协未完成：%s', result.reason)
                self.config.save()
                self.goto_page(page_main)

            if awakening_due:
                previous_count = progress.awakening_completed

                def save_awakening(count):
                    progress.awakening_completed = previous_count + count
                    self.config.save()

                result = run_awakening(
                    self.config, self.device, layer=settings.awakening_layer,
                    count=settings.awakening_count - previous_count,
                    on_progress=save_awakening,
                )
                save_awakening(result.completed)
                if not result.success:
                    logger.warning('觉醒进度 %d/%d，下次接着执行', progress.awakening_completed, settings.awakening_count)
                    mark_failed(account)
                self.goto_page(page_main)

            if account.account not in failed:
                completed.append(account.account)
            if not period.evening and slot.completed:
                break

        if not period.evening and slot.completed:
            # A different alt already met the only daytime goal; earlier
            # unavailable accounts no longer require retrying this period.
            failed.clear()
        self.config.task_runtime_outcome = {
            'task': 'AccountSwitch',
            'status': 'account_routine_pending' if failed else 'completed',
            'completed_accounts': completed,
            'failed_accounts': failed,
            'jade_completed': slot.completed,
            'period': period.start.isoformat(),
        }
        # task_delay reloads the config; flush all progress before requesting it.
        self.config.save()
        target = next_routine_run(datetime.now(), settings, period, retry=bool(failed))
        self.set_next_run('AccountSwitch', target=target, server=False)
        logger.info('小号日程结束，本轮勾协完成：%s；下次执行：%s', slot.completed, target)
        raise TaskEnd('AccountSwitch')
