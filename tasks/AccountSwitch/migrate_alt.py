"""Offline, non-starting migration from an OAS-Alt SQLite database to OAS.

The source directory is never opened by SQLite: DB and WAL bytes are copied
to a temporary directory, checked for changes, then read there. No OAS-Alt
installation or GUI dependency is required. Run this module with --help.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any
from unicodedata import normalize
from uuid import uuid4


HK = timezone(timedelta(hours=8))
DAILY = {'courtyard_affairs': True, 'pickup_email': True,
         'friend_love': False, 'luck_msg': False, 'store_sign': False}


class MigrationError(ValueError):
    pass


@dataclass(frozen=True)
class AltSnapshot:
    values: dict[str, Any]
    activity: list[dict]
    database: bytes
    source_hashes: dict[str, str]


@dataclass(frozen=True)
class MigrationResult:
    config: dict
    report: dict


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MigrationError('JSON包含重复字段，不能确定应保留的值')
        result[key] = value
    return result


def _json(value):
    return json.loads(value, object_pairs_hook=_unique_pairs)


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _source_files(path):
    return [candidate for candidate in (path, Path(str(path) + '-wal')) if candidate.is_file()]


def read_alt_snapshot(source_db) -> AltSnapshot:
    """Read all committed KV/history, including an uncheckpointed WAL.

    Stop OAS-Alt first. A source change during copying aborts rather than
    accepting a potentially inconsistent checkpoint. SQLite may create its
    own SHM/journal only beside the temporary copy, never in the source.
    """
    source = Path(source_db).resolve(strict=True)
    files = _source_files(source)
    before = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
    try:
        with tempfile.TemporaryDirectory(prefix='oas-alt-migration-') as directory:
            copied = Path(directory) / 'source.db'
            hashes = {}
            for path in files:
                destination = copied if path == source else Path(str(copied) + '-wal')
                shutil.copyfile(path, destination)
                hashes[path.name] = _digest(destination)
            current = _source_files(source)
            if ({path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in current} != before
                    or any(_digest(path) != hashes[path.name] for path in current)):
                raise MigrationError('数据源在读取时发生变化，请先关闭独立工具及其后端后重试')
            connection = sqlite3.connect(copied)
            connection.row_factory = sqlite3.Row
            try:
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise MigrationError('SQLite快照校验未通过，未修改目标配置')
                values = {row['key']: _json(row['value']) for row in connection.execute('SELECT key,value FROM kv')}
                activity = []
                exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity'").fetchone()
                if exists:
                    columns = {row['name'] for row in connection.execute('PRAGMA table_info(activity)')}
                    if not {'id', 'account', 'account_display', 'details', 'platform', 'stage', 'day', 'occurred_at', 'event_key'} <= columns:
                        raise MigrationError('活动历史结构无法完整识别，未修改目标配置')
                    for row in connection.execute('SELECT * FROM activity ORDER BY id'):
                        event = dict(row)
                        event['account_key'] = event.pop('account')
                        event['account'] = event.pop('account_display')
                        event['details'] = _json(event['details'])
                        activity.append(event)
                canonical = Path(directory) / 'snapshot.db'
                backup = sqlite3.connect(canonical)
                try:
                    connection.backup(backup)
                finally:
                    backup.close()
                database = canonical.read_bytes()
            finally:
                connection.close()
    except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        raise MigrationError('无法读取完整的独立工具SQLite快照，未修改目标配置') from exc
    return AltSnapshot(values, activity, database, hashes)


def _key(value):
    if not isinstance(value, str):
        raise MigrationError('账号标识须为文本')
    result = normalize('NFKC', value).strip()
    if not result or '#' in result or any(character.isspace() for character in result):
        raise MigrationError('存在无效账号标识，未修改目标配置')
    return result.casefold()


def _account_key(value):
    name, separator, platform = value.rpartition('|')
    if not separator or platform not in ('android', 'ios'):
        raise MigrationError('账号断点缺少明确的android/ios平台')
    return _key(name) + '|' + platform


def _object(value, label):
    if isinstance(value, str):
        try:
            value = _json(value.strip() or '{}')
        except ValueError as exc:
            raise MigrationError(label + '不是有效JSON对象') from exc
    if not isinstance(value, dict):
        raise MigrationError(label + '须为对象')
    return deepcopy(value)


def _account_map(value, label):
    result = {}
    for account, options in _object(value, label).items():
        key = _key(account)
        if key in result:
            raise MigrationError(label + '包含规范化后重复的账号')
        result[key] = _object(options, label)
    return result


def _number(value):
    return value if type(value) is int and value >= 0 else 0


def _stamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(HK).replace(tzinfo=None) if result.tzinfo else result
    except (ValueError, TypeError):
        return None


def _clock(value):
    try:
        return time.fromisoformat(value).isoformat()
    except (ValueError, TypeError) as exc:
        raise MigrationError('执行时间须为有效的HH:MM或HH:MM:SS') from exc


def _bool_map(value):
    if not isinstance(value, dict) or any(type(flag) is not bool for flag in value.values()):
        raise MigrationError('日常断点须为布尔值对象')
    return {key: flag for key, flag in value.items() if key in DAILY}


def _facts(events):
    result, seen = {}, set()
    for event in events:
        if not event.get('account_key', event.get('account')) or event.get('platform') not in ('android', 'ios'):
            continue
        identifier = event.get('event_key')
        if not identifier:
            identifier = ('id', event['id']) if event.get('id') is not None else json.dumps(event, sort_keys=True)
        if identifier in seen:
            continue
        seen.add(identifier)
        key = _key(event.get('account_key', event.get('account'))) + '|' + event['platform']
        fact = result.setdefault((key, event.get('day', '')), {'wins': 0, 'observed': 0, 'steps': {}, 'confirmed': {}})
        details = event.get('details', {})
        if not isinstance(details, dict):
            continue
        if event.get('stage') == 'awakening_progress':
            fact['wins'] += _number(details.get('delta_wins'))
        if event.get('stage') in ('awakening_progress', 'awakening_skipped'):
            fact['observed'] = max(fact['observed'], _number(details.get('observed')))
        steps = ([details.get('step')] if event.get('stage') == 'daily_step'
                 else details.get('steps', []) if event.get('stage') == 'daily_done' else [])
        if not isinstance(steps, list):
            continue
        for step in steps:
            if step not in DAILY:
                continue
            fact['confirmed'][step] = True
            stamp = _stamp(event.get('occurred_at'))
            if stamp and (step not in fact['steps'] or stamp > fact['steps'][step]):
                fact['steps'][step] = stamp
    return result


def _progress(key, raw, facts, enabled):
    raw = _object(raw, '账号断点')
    for name in ('awakening_completed', 'awakening_counter'):
        if name in raw and (type(raw[name]) is not int or raw[name] < 0):
            raise MigrationError('觉醒断点须为非负整数，不能把无法识别的值当0')
    if _number(raw.get('awakening_counter')) > 15:
        raise MigrationError('普通副本观察计数超出0至15，未修改目标配置')
    if 'awakening_daily_done' in raw and type(raw['awakening_daily_done']) is not bool:
        raise MigrationError('觉醒完成标志须为布尔值')
    ios = key.endswith('|ios')
    day = raw.get('ios_day', raw.get('day', '')) if ios else raw.get('day', '')
    if not day and not ios and _stamp(raw.get('login_period')):
        day = _stamp(raw['login_period']).date().isoformat()
    if day:
        try:
            day = date.fromisoformat(day).isoformat()
        except (ValueError, TypeError) as exc:
            raise MigrationError('账号断点日期无效') from exc
    fact = facts.get((key, day), {'wins': 0, 'observed': 0, 'steps': {}, 'confirmed': {}})
    confirmed = _bool_map(raw.get('ios_daily_progress', raw.get('daily', {})) if ios else raw.get('daily', {}))
    confirmed.update(fact['confirmed'])
    done = raw.get('ios_daily_done', raw.get('daily_done', False)) if ios else raw.get('daily_done', False)
    if type(done) is not bool:
        raise MigrationError('每日完成标志须为布尔值')
    options = _bool_map(raw.get('ios_daily_options', raw.get('daily_options', {})) if ios else raw.get('daily_options', {}))
    if done:
        for name, flag in (options or enabled).items():
            if flag:
                confirmed[name] = True
    record = _object(raw.get('daily_record', {}), '每日时间记录')
    for name, stamp in fact['steps'].items():
        previous = _stamp(record.get(name + '_dt'))
        if previous is None or stamp > previous:
            record[name + '_dt'] = stamp.strftime('%Y-%m-%d %H:%M:%S')
    return {'day': day, 'daily_done': done, 'daily_confirmed': confirmed, 'daily_options': options,
            'daily_record': record, 'login_day': raw.get('ios_login_day', raw.get('login_day', '')) if ios else raw.get('login_day', ''),
            'login_period': raw.get('login_period', ''),
            'awakening_completed': 0 if ios else _number(raw.get('awakening_completed')),
            'awakening_observed': 0 if ios else max(_number(raw.get('awakening_counter')), fact['observed']),
            'awakening_script_wins': 0 if ios else fact['wins'],
            'awakening_daily_done': False if ios else raw.get('awakening_daily_done', False),
            'awakening_assist_friend': '' if ios else raw.get('awakening_assist_friend', '')}


def _merge_progress(current, incoming):
    current = _object(current, '目标账号断点')
    old_day, new_day = current.get('day', ''), incoming.get('day', '')
    if old_day:
        try:
            date.fromisoformat(old_day)
        except (ValueError, TypeError) as exc:
            raise MigrationError('目标断点日期无效，未回退或清空原进度') from exc
    if old_day and (not new_day or old_day > new_day):
        return current
    if old_day != new_day:
        return {**current, **incoming, 'daily_record': {**current.get('daily_record', {}), **incoming.get('daily_record', {})}}
    result = {**current, **incoming}
    for name in ('daily_done', 'awakening_daily_done'):
        result[name] = bool(current.get(name) or incoming.get(name))
    for name in ('awakening_completed', 'awakening_observed', 'awakening_script_wins'):
        result[name] = max(_number(current.get(name)), _number(incoming.get(name)))
    for name in ('daily_confirmed', 'daily_options'):
        result[name] = _bool_map(current.get(name, {}))
        for key, flag in incoming.get(name, {}).items():
            result[name][key] = result[name].get(key, False) or flag
    record = _object(current.get('daily_record', {}), '目标日常时间记录')
    for key, value in incoming.get('daily_record', {}).items():
        old, new = _stamp(record.get(key)), _stamp(value)
        if key not in record or new and (old is None or new > old):
            record[key] = value
    result['daily_record'] = record
    for name in ('login_period', 'login_day'):
        result[name] = max(current.get(name, ''), incoming.get(name, ''))
    return result


def _merge_period(current, incoming):
    current, incoming = _object(current, '目标勾协断点'), _object(incoming, '源勾协断点')
    for item in (current, incoming):
        owner = item.get('owner_account', '')
        if owner:
            item['owner_account'] = _account_key(owner)
        if item.get('pending') and not owner:
            raise MigrationError('待确认勾协缺少原账号，不能安全迁移')
    if current.get('pending') and incoming.get('pending') and (
            current.get('owner_account') != incoming.get('owner_account') or current['pending'] != incoming['pending']):
        raise MigrationError('同一时段的勾协派发断点冲突，未修改目标配置')
    result = {**current, **incoming}
    result['completed'] = bool(current.get('completed') or incoming.get('completed'))
    if current.get('pending') and not incoming.get('pending') and not result['completed']:
        result['pending'], result['owner_account'] = current['pending'], current['owner_account']
    result['checked_accounts'] = list(dict.fromkeys(_account_key(value) for value in
                                                   current.get('checked_accounts', []) + incoming.get('checked_accounts', [])))
    return result


def build_migrated_config(values: dict, target_config: dict, activity=(), *, map_device=False) -> MigrationResult:
    """Pure conversion; retain newer/higher target progress and disable work."""
    settings = _object(values.get('settings'), '源settings')
    activity = list(activity)
    if not isinstance(settings.get('accounts'), str):
        raise MigrationError('源settings缺少账号文本')
    result = _object(target_config, '目标配置')
    block = result.setdefault('account_switch', {})
    config = block.setdefault('account_switch_config', {})
    modes = _account_map(config.get('account_modes', {}), '目标账号模式')
    targets = _account_map(config.get('account_targets', {}), '目标账号目标')
    source_modes = _account_map(settings.get('account_modes', {}), '源账号模式')
    source_targets = _account_map(settings.get('account_targets', {}), '源账号目标')
    modes.update(source_modes)
    targets.update(source_targets)
    account_keys = set()
    for line in settings['accounts'].splitlines():
        if line.strip():
            key = _key(line.split('#', 1)[0])
            account_keys.add(key)
            modes[key] = {'android': 'plan', 'ios': 'daily', **source_modes.get(key, {})}
            targets[key] = {'jade_receiver': '', 'assist_friend': '', **source_targets.get(key, {})}
    daily = {**DAILY, **_bool_map(settings.get('daily', {}))}
    config.update(accounts=settings['accounts'], account_modes=json.dumps(modes, ensure_ascii=False, indent=2),
                  account_targets=json.dumps(targets, ensure_ascii=False, indent=2),
                  apple_or_android=settings.get('android', True), auto_jade=False,
                  invite_friend_name=settings.get('receiver', '5791'), jade_receiver=settings.get('receiver', '5791'),
                  assist_friend=settings.get('assist_friend') or settings.get('receiver', '5791'),
                  routine_enable=False, ios_daily_enable=False,
                  daytime_run_time=_clock(settings.get('daytime', '17:00')),
                  evening_run_time=_clock(settings.get('evening', '20:00')),
                  ios_daily_run_time=_clock(settings.get('ios_daily_time', '20:00')),
                  platform_priority=settings.get('platform_priority', 'android'),
                  awakening_layer=settings.get('awakening_layer', 1), awakening_count=settings.get('awakening_count', 14),
                  awakening_kirin=settings.get('kirin', 'lightning'), retry_minutes=settings.get('retry_minutes', 10),
                  daily_courtyard_affairs=daily['courtyard_affairs'], daily_pickup_email=daily['pickup_email'],
                  daily_free_options=json.dumps({name: daily[name] for name in ('friend_love', 'luck_msg', 'store_sign')}, ensure_ascii=False))
    for name in ('daytime_jitter_minutes', 'evening_jitter_minutes', 'ios_daily_jitter_minutes'):
        config[name] = settings.get(name, 15)
    block.setdefault('scheduler', {})['enable'] = False
    runtime = block.setdefault('runtime', {})
    accounts = {}
    for key, value in _object(runtime.get('accounts', {}), '目标账号断点').items():
        identity = _account_key(key)
        accounts[identity] = _merge_progress(accounts[identity], value) if identity in accounts else value
    runtime['accounts'] = accounts
    facts = _facts(activity)
    migrated = 0
    for key, value in values.items():
        if key.startswith('account:'):
            key = _account_key(key[len('account:'):])
            incoming = _progress(key, value, facts, daily)
            accounts[key] = _merge_progress(accounts.get(key, {}), incoming)
            migrated += 1
        elif key.startswith('jade:'):
            identity = key[len('jade:'):]
            start, separator, receiver = identity.partition('|')
            if not separator or _stamp(start) is None or not receiver:
                raise MigrationError('勾协时段标识无效')
            identity = _stamp(start).isoformat() + '|' + receiver
            periods = runtime.setdefault('periods', {})
            periods[identity] = _merge_period(periods.get(identity, {}), value)
    runtime.setdefault('periods', {})
    schedule = runtime.setdefault('schedule', {})
    for key in list(schedule):
        if key.startswith('service:'):
            del schedule[key]
    for key, value in values.items():
        if key.startswith('schedule:sample:'):
            entry = deepcopy(value)
            if isinstance(entry, dict) and isinstance(entry.get('signature'), list) and len(entry['signature']) == 2:
                entry['signature'][0] = _clock(entry['signature'][0])
            name = key[len('schedule:'):]
            previous = schedule.get(name)
            if name not in schedule or (isinstance(entry, dict) and isinstance(previous, dict)
                                        and entry.get('signature') != previous.get('signature')):
                schedule[name] = entry
            elif isinstance(entry, dict) and isinstance(previous, dict) and entry.get('at') == previous.get('at'):
                priority = {'pending': 0, 'running': 1, 'waiting_future': 2, 'paused': 3, 'done': 4}
                if priority.get(entry.get('state'), -1) > priority.get(previous.get('state'), -1):
                    schedule[name] = entry
        elif key in ('schedule:meta', 'schedule:ios_meta'):
            name = 'android' if key == 'schedule:meta' else 'ios'
            if not schedule.get(name):
                schedule[name] = deepcopy(value)
    if map_device:
        device = result.setdefault('script', {}).setdefault('device', {})
        if settings.get('serial'):
            device['serial'] = settings['serial']
        if settings.get('game_package'):
            device['package_name'] = settings['game_package']
    report = {'accounts': len(account_keys), 'checkpoints': migrated, 'activity_events': len(activity),
              'scheduler_enabled': False, 'routine_enabled': False, 'ios_daily_enabled': False,
              'device_mapped': bool(map_device), 'history_in_config': False,
              'notes': ['完整历史保存在私有SQLite及JSONL归档，不注入配置或伪造旧OAS日志。',
                        '无准确时间的日常完成布尔保存在daily_confirmed，不编造DateTime。',
                        '脚本胜场仅来自activity的delta_wins；缺历史时为未知下界0，不从总数14推算。',
                        '同日目标断点取较大值、不累加，重复迁移不会重复计胜；较新目标日期不回退。',
                        '启动/收尾沿用原OAS设置；独立工具模拟器路径/索引/finish_action仅归档。',
                        'AccountSwitch调度、安卓和iOS例程均关闭；其他原OAS任务开关保持不变。']}
    return MigrationResult(result, report)


def migrate_database(source_db, target_config, *, apply=False, map_device=False) -> dict:
    """Dry-run by default; --apply backs up, archives, then atomically writes."""
    source, target = Path(source_db).resolve(strict=True), Path(target_config).resolve(strict=True)
    if target.is_relative_to(source.parent):
        raise MigrationError('目标配置须在数据源目录之外；源目录保持只读')
    original = target.read_bytes()
    snapshot = read_alt_snapshot(source)
    try:
        existing = _json(original.decode('utf-8-sig'))
        converted = build_migrated_config(snapshot.values, existing, snapshot.activity, map_device=map_device)
        from tasks.AccountSwitch.config import AccountSwitch
        AccountSwitch.model_validate(converted.config['account_switch'])
    except (ValueError, TypeError) as exc:
        raise MigrationError('迁移结果未通过配置校验，未修改目标配置：' + str(exc)) from exc
    report = {**converted.report, 'applied': bool(apply)}
    if not apply:
        return report
    archive = target.parent / (target.stem + '.alt-migration-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
    archive.mkdir(mode=0o700)
    with (archive / 'target.before.json').open('xb') as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    (archive / 'alt-source.sqlite3').write_bytes(snapshot.database)
    (archive / 'source-values.json').write_text(json.dumps(snapshot.values, ensure_ascii=False, indent=2), encoding='utf-8')
    with (archive / 'activity.jsonl').open('w', encoding='utf-8', newline='\n') as stream:
        for event in snapshot.activity:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    (archive / 'migration-report.json').write_text(json.dumps({**report, 'source_hashes': snapshot.source_hashes}, ensure_ascii=False, indent=2), encoding='utf-8')
    if target.read_bytes() != original:
        raise MigrationError('目标配置在迁移期间被其他程序修改，已保留归档但未覆盖目标')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=target.parent,
                                         prefix='.' + target.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(converted.config, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.parent.resolve() == target.parent:
            temporary.unlink(missing_ok=True)
    return {**report, 'archive': str(archive)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读迁移OAS-Alt断点到原OAS；默认预览，不会运行任务。')
    parser.add_argument('--source-db', required=True, type=Path)
    parser.add_argument('--target-config', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='先备份目标与完整源历史，再写入目标配置')
    parser.add_argument('--map-device', action='store_true', help='额外迁移ADB地址及非空包名；默认不动原OAS设备设置')
    args = parser.parse_args(argv)
    try:
        report = migrate_database(args.source_db, args.target_config, apply=args.apply, map_device=args.map_device)
    except (MigrationError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
