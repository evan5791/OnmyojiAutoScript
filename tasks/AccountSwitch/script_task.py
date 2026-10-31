from datetime import datetime, timedelta
from datetime import datetime as _DateTime

from module.exception import RequestHumanTakeover, TaskEnd, ScriptEnd
from module.logger import logger
from tasks.AccountSwitch.config import AccountSwitchConfig, AccountRoutineProgress, JadePeriodProgress
from tasks.AccountSwitch.routine import refresh_period, window, next_due, next_appointment, begin, finish
from tasks.AccountSwitch.service import defer_dedicated_restart
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
        if settings.auto_jade or settings.routine_enable or settings.ios_daily_enable:
            if not invite_name:
                invite_name = self.config.wanted_quests.wanted_quests_config.invite_friend_name.strip()
            if (settings.routine_enable or settings.ios_daily_enable) and not invite_name:
                invite_name = '5791'
            if not invite_name:
                raise RequestHumanTakeover('已开启登录后自动勾协，请填写邀请好友昵称，或先配置「悬赏封印」的邀请好友')

        if settings.routine_enable or settings.ios_daily_enable:
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

    def audit(self, account, platform, stage, details=None, day=None):
        from tasks.AccountSwitch.history import record_event
        return record_event(self.config, account, platform, stage, details,
                            day=day, occurred_at=datetime.now())

    def daily_options(self, settings):
        source = getattr(getattr(self.config, 'daily_trifles', None), 'trifles_config', None)
        values = source.model_dump() if source is not None else {}
        options = {name: value for name, value in values.items() if type(value) is bool}
        options.update(courtyard_affairs=settings.daily_courtyard_affairs,
                       pickup_email=settings.daily_pickup_email)
        options.update(settings.get_daily_free_options())
        return options

    def run_routine(self, settings, accounts, invite_name):
        """One original OAS task owns both platforms and their saved quotas."""
        runtime = self.config.account_switch.runtime
        rows = settings.get_account_rows()
        completed, failed, skipped, batches = [], [], [], []
        appointment = next_due(runtime, settings, datetime.now())
        self.config.save()
        processed = set()
        while appointment and appointment['platform'] not in processed:
            platform = appointment['platform']
            _, started, until = window(_DateTime.fromisoformat(appointment['at']), platform)
            now = datetime.now()
            if not started <= now < until:
                # A due daytime appointment can expire between selection and
                # execution. Retire that slot; never claim the future evening.
                finish(runtime, settings, platform, now, attempted=started)
                self.config.save()
                self.audit('', platform, 'skipped', {'reason': 'appointment_expired',
                           'period': started.isoformat()}, started.date().isoformat())
                appointment = next_due(runtime, settings, now)
                continue
            processed.add(platform)
            begin(runtime, settings, platform, now, attempted=started)
            self.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'running',
                'platform': platform, 'period': started.isoformat(), 'keep_emulator': True}
            self.config.save()
            self.audit('', platform, 'task_start', {'plan': appointment['plan'], 'accounts': len(rows)})
            try:
                outcome = self.run_account_batch(settings, rows, platform, invite_name, period_start=started)
            except (ScriptEnd, KeyboardInterrupt, SystemExit):
                finish(runtime, settings, platform, datetime.now(), cancelled=True, attempted=started)
                self.config.save()
                self.config.task_runtime_outcome = {'task': 'AccountSwitch', 'status': 'cancelled',
                                                    'checkpoint_preserved': True, 'keep_emulator': True}
                self.audit('', platform, 'task_cancelled', {'checkpoint_preserved': True})
                raise
            finish(runtime, settings, platform, datetime.now(), failed=bool(outcome['failed_accounts']),
                   attempted=started)
            self.config.save()
            completed.extend(outcome['completed_accounts'])
            failed.extend(outcome['failed_accounts'])
            skipped.extend(outcome['skipped_accounts'])
            batches.append(outcome)
            appointment = next_due(runtime, settings, datetime.now())
        future = next_appointment(runtime, settings, datetime.now())
        target = _DateTime.fromisoformat(future['at']) if future else None
        if target is not None:
            defer_dedicated_restart(self.config, next_run=target, now=datetime.now(), logger=logger)
        status = ('account_routine_pending' if failed else 'completed') if batches else 'waiting'
        self.config.task_runtime_outcome = {
            'task': 'AccountSwitch', 'status': status, 'completed_accounts': completed,
            'failed_accounts': list(dict.fromkeys(failed)), 'skipped_accounts': skipped,
            'jade_completed': (all(batch['jade_completed'] for batch in batches if batch['platform'] == 'android')
                               if any(batch['platform'] == 'android' for batch in batches) else False),
            'batches': batches, 'keep_emulator': bool(failed),
        }
        self.config.save()
        if target is not None:
            self.set_next_run('AccountSwitch', target=target, server=False)
        logger.info('小号日程汇总：%s，完成%d，待处理%d，跳过%d；下次%s', status,
                    len(completed), len(set(failed)), len(skipped), target)
        raise TaskEnd('AccountSwitch: ' + status)

    def run_account_batch(self, settings, rows, platform, default_receiver, *, period_start=None):
        from tasks.AccountSwitch.flows import run_daily, run_awakening
        from tasks.AccountSwitch.cooperation import prepare_cooperation_assist
        from tasks.AccountSwitch.jade import JadeCooperationTask as RoutineJadeTask
        runtime = self.config.account_switch.runtime
        plan, started, until = window(period_start or datetime.now(), platform)
        day = started.date().isoformat()
        evening = plan == 'evening'
        options = self.daily_options(settings)
        completed, failed, skipped = [], [], []
        receivers = {row.jade_receiver or settings.jade_receiver or default_receiver
                     for row in rows if row.android_mode == 'plan'}
        slots = {receiver: runtime.periods.setdefault(started.isoformat() + '|' + receiver,
                                                     JadePeriodProgress()) for receiver in receivers} if platform == 'android' else {}
        owners = {slot.owner_account for slot in slots.values() if slot.owner_account}
        if owners:
            rows = sorted(rows, key=lambda row: row.key + '|android' not in owners)
        for receiver, slot in slots.items():
            if not slot.completed and slot.owner_account and not any(row.android_mode == 'plan' and
                    row.key + '|android' == slot.owner_account and
                    (row.jade_receiver or settings.jade_receiver or default_receiver) == receiver for row in rows):
                failed.append(slot.owner_account)
                logger.warning('待完成勾协原小号已移除或更改接收方，保留%s的派发状态', receiver)
        for index, row in enumerate(rows, 1):
            if window(datetime.now(), platform)[1] != started:
                logger.warning('执行期间跨刷新边界，停止本批并保留进度')
                failed.append('refresh_boundary')
                break
            mode = row.android_mode if platform == 'android' else row.ios_mode
            if mode == 'off':
                skipped.append(row.account)
                self.audit(row.account, platform, 'skipped', {'reason': 'disabled'}, day)
                continue
            key = row.key + '|' + platform
            progress = runtime.accounts.setdefault(key, AccountRoutineProgress())
            if progress.day != day:
                progress = AccountRoutineProgress(day=day)
                runtime.accounts[key] = progress
            receiver = row.jade_receiver or settings.jade_receiver or default_receiver
            friend = row.assist_friend or settings.assist_friend
            slot = slots.get(receiver)
            login_due = mode == 'login' and (progress.login_day != day if platform == 'ios'
                                            else progress.login_period != started.isoformat())
            jade_due = (platform == 'android' and mode == 'plan' and not slot.completed
                        and (slot.owner_account == key or key not in slot.checked_accounts)
                        and (not slot.owner_account or slot.owner_account == key))
            daily_due = mode != 'login' and (platform == 'ios' or evening) and (
                not progress.daily_done or bool(progress.daily_options) and any(
                    enabled and progress.daily_options.get(name) is not True for name, enabled in options.items()))
            awake_due = (platform == 'android' and mode == 'plan' and evening
                         and not progress.awakening_daily_done
                         and progress.awakening_completed < settings.awakening_count)
            if not (login_due or jade_due or daily_due or awake_due):
                skipped.append(row.account)
                self.audit(row.account, platform, 'skipped', {'reason': 'confirmed_already_done'}, day)
                continue
            logger.hr(f'小号 {platform} {index}/{len(rows)}: {row.account}')
            self.audit(row.account, platform, 'account_start', {'mode': mode, 'jade_receiver': receiver,
                                                              'assist_friend': friend}, day)
            step = 'login'
            account_failed = False
            try:
                # Each remembered account is a separate bounded operation.
                # Retain stuck detection inside it; discard prior accounts'
                # otherwise identical SDK-button histories at this boundary.
                for name in ('click_record_clear', 'stuck_record_clear'):
                    clear = getattr(self.device, name, None)
                    if callable(clear):
                        clear()
                info = row.as_account_info(platform)
                if not SwitchAccount(self.config, self.device, info).switchAccount():
                    raise RequestHumanTakeover('账号登录未完成')
                self.audit(row.account, platform, 'login', {'character': info.character}, day)
                if mode == 'login':
                    progress.login_day, progress.login_period = day, started.isoformat()
                else:
                    if daily_due:
                        step = 'daily'
                        def save_daily(record):
                            previous = progress.daily_record.model_dump()
                            current = record.model_dump()
                            for name, value in current.items():
                                if name.endswith('_dt') and value != previous.get(name):
                                    short = name[:-3]
                                    progress.daily_confirmed[short] = True
                                    self.audit(row.account, platform, 'daily_step', {'step': short}, day)
                            progress.daily_record = record.model_copy(deep=True)
                            self.config.save()
                        result = run_daily(self.config, self.device, progress.daily_record,
                                           on_progress=save_daily, routine_day=day,
                                           confirmed=progress.daily_confirmed)
                        save_daily(result.done_record)
                        progress.daily_confirmed.update(getattr(result, 'confirmed', {}) or {})
                        progress.daily_done = result.success
                        if result.success:
                            progress.daily_options = dict(options)
                            self.audit(row.account, platform, 'daily_done', {'steps': [name for name, enabled in options.items() if enabled]}, day)
                        else:
                            account_failed = True
                            self.audit(row.account, platform, 'task_error', {'step': step, 'reason': str(result.pending)}, day)
                        self.config.save()
                        self.goto_page(page_main)
                    if window(datetime.now(), platform)[1] != started:
                        raise RequestHumanTakeover('日常执行期间跨过刷新边界，停止本号后续步骤')
                    if jade_due:
                        step = 'jade'
                        def save_jade(value):
                            slot.owner_account, slot.pending = key, dict(value)
                            self.config.save()
                            self.audit(row.account, platform, 'jade_' + str(value.get('stage', 'checkpoint')),
                                       {'receiver': receiver, 'period': started.isoformat(), **value}, day)
                        result = RoutineJadeTask(self.config, self.device, receiver).run_jade(
                            pending=slot.pending or None, on_checkpoint=save_jade)
                        if result.status == 'completed':
                            slot.completed, slot.pending, slot.owner_account = True, {}, key
                            self.audit(row.account, platform, 'jade_completed', {'receiver': receiver}, day)
                        elif result.status == 'no_jade':
                            if key not in slot.checked_accounts: slot.checked_accounts.append(key)
                            self.audit(row.account, platform, 'jade_checked', {'receiver': receiver, 'result': 'no_jade'}, day)
                        else:
                            account_failed = True
                            if result.pending: save_jade(result.pending)
                            elif result.status == 'invite_failed': slot.owner_account, slot.pending = '', {}
                            self.audit(row.account, platform, 'task_error', {'step': step, 'reason': result.reason}, day)
                        self.config.save()
                        self.goto_page(page_main)
                    if window(datetime.now(), platform)[1] != started:
                        raise RequestHumanTakeover('勾协执行期间跨过刷新边界，停止后续觉醒')
                    if awake_due:
                        step = 'cooperation_counter'
                        selection = prepare_cooperation_assist(self.config, self.device, friend)
                        observed = selection.normal_used
                        if type(observed) is not int or not 0 <= observed <= 15:
                            raise RequestHumanTakeover('普通副本次数无效，不按0处理')
                        base = max(progress.awakening_completed, observed)
                        verified = selection.card_used is not None and selection.card_used == observed
                        def save_wins(count):
                            total = max(progress.awakening_completed, base + count)
                            delta = max(0, total - max(base, progress.awakening_completed))
                            progress.awakening_completed = total
                            progress.awakening_observed = observed
                            progress.awakening_assist_friend = selection.friend
                            progress.awakening_script_wins += delta
                            self.config.save()
                            if delta:
                                self.audit(row.account, platform, 'awakening_progress', {'confirmed': total,
                                    'observed': observed, 'delta_wins': delta, 'assist_friend': selection.friend}, day)
                        if observed >= 14 and verified:
                            progress.awakening_daily_done = True
                            save_wins(0)
                            self.audit(row.account, platform, 'awakening_skipped', {'observed': observed, 'delta_wins': 0}, day)
                        else:
                            step = 'awakening'
                            remaining = 0 if observed >= 14 else max(0, settings.awakening_count - base)
                            result = run_awakening(self.config, self.device, layer=settings.awakening_layer, count=remaining,
                                on_progress=save_wins, assist=selection, kirin=settings.awakening_kirin)
                            verified = verified or getattr(result, 'assist_verified', False)
                            if verified:
                                save_wins(result.completed)
                                if getattr(result, 'daily_target_reached', False): progress.awakening_daily_done = True
                                if result.success and result.completed == 0:
                                    self.audit(row.account, platform, 'awakening_skipped', {'observed': observed, 'delta_wins': 0}, day)
                            if not result.success or not verified:
                                account_failed = True
                                self.audit(row.account, platform, 'task_error', {'step': step, 'reason': getattr(result, 'reason', 'assist_unverified')}, day)
                        self.config.save()
                        self.goto_page(page_main)
                if account_failed:
                    failed.append(row.account)
                else:
                    completed.append(row.account)
                self.config.save()
                self.audit(row.account, platform, 'account_end', {'status': 'pending' if account_failed else 'completed'}, day)
            except ScriptEnd:
                raise
            except Exception as exc:
                if row.account not in failed: failed.append(row.account)
                self.config.save()
                logger.warning('小号%s的%s异常：%s；保留断点并继续下一号', row.account, step, exc)
                self.audit(row.account, platform, 'task_error', {'step': step, 'reason': str(exc),
                                                              'action': 'skip_keep_checkpoint'}, day)
        jade_done = bool(slots) and all(slot.completed for slot in slots.values())
        if platform == 'android' and not evening and jade_done:
            login_names = {row.account for row in rows if row.android_mode == 'login'}
            failed = [name for name in failed if name in login_names]
        return {'platform': platform, 'plan': plan, 'period': started.isoformat(),
                'completed_accounts': completed, 'failed_accounts': list(dict.fromkeys(failed)),
                'skipped_accounts': skipped, 'jade_completed': jade_done}
