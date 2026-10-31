from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tasks.AccountSwitch.config import AccountSwitch, AccountSwitchConfig
from tasks.AccountSwitch.migrate_alt import (
    MigrationError, build_migrated_config, migrate_database, read_alt_snapshot,
)
from tasks.AccountSwitch.routine import sample


def settings(**values):
    return {'accounts': 'One@example.com # 一号\ntwo@example.com', 'receiver': 'main-default',
            'assist_friend': '', 'android': True, 'routine_enabled': True, 'ios_daily_enabled': True,
            'daytime': '17:00', 'evening': '20:00', 'ios_daily_time': '02:00',
            'daytime_jitter_minutes': 15, 'evening_jitter_minutes': 7, 'ios_daily_jitter_minutes': 0,
            'awakening_layer': 1, 'awakening_count': 14, 'kirin': 'wind', 'retry_minutes': 10,
            'account_modes': {'one@example.com': {'android': 'login', 'ios': 'off'}},
            'account_targets': {'one@example.com': {'jade_receiver': 'main-one', 'assist_friend': 'friend-one'}},
            'daily': {'courtyard_affairs': True, 'pickup_email': True, 'friend_love': True,
                      'luck_msg': False, 'store_sign': False}, 'serial': '127.0.0.1:16416',
            'game_package': 'com.netease.onmyoji.test', 'finish_action': 'close_emulator', **values}


def target():
    return {'config_name': 'private', 'script': {'device': {'serial': 'original:5555', 'package_name': 'original.pkg'}},
            'restart': {'scheduler': {'enable': True}}, 'chess': {'keep': 'untouched'},
            'account_switch': {'scheduler': {'enable': True, 'priority': 8},
                               'account_switch_config': {'accounts': 'old', 'routine_enable': True},
                               'runtime': {'accounts': {}, 'periods': {}, 'schedule': {}}}}


def event(number, stage, details, *, day='2026-10-31', platform='android', account='one@example.com'):
    return {'id': number, 'day': day, 'occurred_at': '2026-10-31T20:15:00+08:00',
            'account_key': account, 'account': account, 'platform': platform, 'stage': stage,
            'details': details, 'event_key': f'event-{number}'}


def make_database(path, values, events=(), *, wal=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('CREATE TABLE kv (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    connection.executemany('INSERT INTO kv VALUES(?,?)', [(key, json.dumps(value)) for key, value in values.items()])
    connection.execute('''CREATE TABLE activity (id INTEGER PRIMARY KEY, occurred_at TEXT, day TEXT,
                       account TEXT, account_display TEXT, platform TEXT, stage TEXT, details TEXT, event_key TEXT)''')
    for item in events:
        connection.execute('INSERT INTO activity VALUES(?,?,?,?,?,?,?,?,?)', (
            item['id'], item['occurred_at'], item['day'], item['account_key'], item['account'], item['platform'],
            item['stage'], json.dumps(item['details']), item['event_key']))
    connection.commit()
    return connection


class ConversionTests(unittest.TestCase):
    def test_settings_targets_platforms_and_optional_daily_map_without_starting_anything(self):
        values, original = {'settings': settings()}, target()
        before_values, before_original = deepcopy(values), deepcopy(original)
        result = build_migrated_config(values, original)
        block = result.config['account_switch']
        config = AccountSwitch.model_validate(block).account_switch_config
        self.assertFalse(block['scheduler']['enable'])
        self.assertFalse(config.routine_enable or config.ios_daily_enable or config.auto_jade)
        self.assertEqual(config.get_account_rows()[0].android_mode, 'login')
        self.assertEqual(config.get_account_rows()[0].assist_friend, 'friend-one')
        self.assertEqual(config.get_account_rows()[1].assist_friend, 'main-default')
        self.assertEqual(config.get_account_rows()[0].jade_receiver, 'main-one')
        self.assertEqual(config.ios_daily_run_time.isoformat(), '02:00:00')
        self.assertEqual((config.daytime_jitter_minutes, config.evening_jitter_minutes, config.ios_daily_jitter_minutes), (15, 7, 0))
        self.assertEqual(config.awakening_kirin, 'wind')
        self.assertEqual(config.get_daily_free_options(), {'friend_love': True, 'luck_msg': False, 'store_sign': False})
        self.assertEqual(result.config['script'], original['script'])
        self.assertEqual(result.config['restart'], original['restart'])
        self.assertEqual(result.config['chess'], original['chess'])
        self.assertEqual(values, before_values)
        self.assertEqual(original, before_original)

    def test_counter_fourteen_is_not_fourteen_script_wins(self):
        values = {'settings': settings(), 'account:one@example.com|android': {
            'day': '2026-10-31', 'awakening_completed': 14, 'awakening_counter': 14,
            'awakening_daily_done': True, 'awakening_assist_friend': 'friend-one'}}
        progress = build_migrated_config(values, target()).config['account_switch']['runtime']['accounts']['one@example.com|android']
        self.assertEqual(progress['awakening_completed'], 14)
        self.assertEqual(progress['awakening_observed'], 14)
        self.assertEqual(progress['awakening_script_wins'], 0)
        self.assertTrue(progress['awakening_daily_done'])

    def test_real_delta_wins_and_external_observation_stay_distinct_and_deduplicated(self):
        values = {'settings': settings(), 'account:one@example.com|android': {
            'day': '2026-10-31', 'awakening_completed': 14, 'awakening_counter': 5}}
        events = [event(1, 'awakening_progress', {'delta_wins': 0, 'observed': 5}),
                  event(2, 'awakening_progress', {'delta_wins': 9, 'observed': 5})]
        events.append(deepcopy(events[1]))
        progress = build_migrated_config(values, target(), events).config['account_switch']['runtime']['accounts']['one@example.com|android']
        self.assertEqual((progress['awakening_completed'], progress['awakening_observed'], progress['awakening_script_wins']), (14, 5, 9))

    def test_partial_daily_without_time_keeps_boolean_and_does_not_fabricate_datetime(self):
        values = {'settings': settings(), 'account:one@example.com|android': {
            'day': '2026-10-31', 'daily_done': False, 'daily': {'courtyard_affairs': True, 'pickup_email': False}}}
        progress = build_migrated_config(values, target()).config['account_switch']['runtime']['accounts']['one@example.com|android']
        self.assertTrue(progress['daily_confirmed']['courtyard_affairs'])
        self.assertFalse(progress['daily_done'])
        self.assertEqual(progress['daily_record'], {})

    def test_actual_daily_timestamp_and_ios_prefixed_checkpoint_are_preserved(self):
        values = {'settings': settings(), 'account:ONE@example.com|ios': {
            'ios_day': '2026-10-31', 'ios_login_day': '2026-10-31', 'ios_daily_done': False,
            'ios_daily_progress': {'pickup_email': True}, 'ios_daily_options': {'pickup_email': True}}}
        events = [event(1, 'daily_step', {'step': 'pickup_email'}, platform='ios')]
        progress = build_migrated_config(values, target(), events).config['account_switch']['runtime']['accounts']['one@example.com|ios']
        self.assertEqual(progress['day'], '2026-10-31')
        self.assertEqual(progress['login_day'], '2026-10-31')
        self.assertEqual(progress['daily_record']['pickup_email_dt'], '2026-10-31 20:15:00')
        self.assertTrue(progress['daily_confirmed']['pickup_email'])

    def test_newer_target_day_and_higher_same_day_progress_never_roll_back(self):
        original = target()
        original['account_switch']['runtime']['accounts'] = {
            'one@example.com|android': {'day': '2026-10-31', 'awakening_completed': 12, 'awakening_script_wins': 8,
                                       'daily_confirmed': {'pickup_email': True}},
            'two@example.com|android': {'day': '2026-11-01', 'awakening_completed': 7}}
        values = {'settings': settings(), 'account:one@example.com|android': {
            'day': '2026-10-31', 'awakening_completed': 3, 'daily': {'courtyard_affairs': True}},
            'account:two@example.com|android': {'day': '2026-10-31', 'awakening_completed': 14}}
        result = build_migrated_config(values, original).config
        one, two = (result['account_switch']['runtime']['accounts'][name + '@example.com|android'] for name in ('one', 'two'))
        self.assertEqual((one['awakening_completed'], one['awakening_script_wins']), (12, 8))
        self.assertTrue(one['daily_confirmed']['pickup_email'] and one['daily_confirmed']['courtyard_affairs'])
        self.assertEqual(two, original['account_switch']['runtime']['accounts']['two@example.com|android'])
        self.assertEqual(build_migrated_config(values, result).config, result)

    def test_pending_jade_owner_and_per_receiver_quota_are_kept(self):
        values = {'settings': settings(), 'jade:2026-10-31T18:00:00|main-one': {
            'completed': False, 'owner_account': 'ONE@example.com|android', 'pending': {'stage': 'sending'},
            'checked_accounts': ['two@example.com|android']}}
        result = build_migrated_config(values, target()).config
        slot = result['account_switch']['runtime']['periods']['2026-10-31T18:00:00|main-one']
        self.assertEqual(slot['owner_account'], 'one@example.com|android')
        self.assertEqual(slot['pending'], {'stage': 'sending'})
        original = deepcopy(result)
        original['account_switch']['runtime']['periods']['2026-10-31T18:00:00|main-one']['owner_account'] = 'two@example.com|android'
        with self.assertRaisesRegex(MigrationError, '冲突'):
            build_migrated_config(values, original)

    def test_random_sample_is_not_redrawn_and_arming_is_removed(self):
        values = {'settings': settings(), 'schedule:sample:ios_daily:2026-10-31': {
            'signature': ['02:00', 0], 'at': '2026-11-01T02:00:00', 'state': 'done'},
            'schedule:ios_meta': {'kind': 'retry', 'plan': 'ios_daily', 'day': '2026-10-31', 'at': '2026-11-01T02:10:00'}}
        original = target()
        original['account_switch']['runtime']['schedule']['service:ios'] = {'armed_at': 'old'}
        result = build_migrated_config(values, original).config['account_switch']
        self.assertNotIn('service:ios', result['runtime']['schedule'])
        config = AccountSwitchConfig.model_validate(result['account_switch_config'])
        with patch('tasks.AccountSwitch.routine.random.randint', side_effect=AssertionError('must not redraw')):
            record = sample(result['runtime'], config, 'ios_daily', date(2026, 10, 31))
        self.assertEqual((record['at'], record['state']), ('2026-11-01T02:00:00', 'done'))

    def test_device_mapping_is_explicit_and_does_not_guess_emulator_index(self):
        result = build_migrated_config({'settings': settings(emulator_index=6)}, target(), map_device=True).config
        self.assertEqual(result['script']['device'], {'serial': '127.0.0.1:16416', 'package_name': 'com.netease.onmyoji.test'})
        self.assertNotIn('emulator_index', result['script']['device'])

    def test_invalid_counter_or_ambiguous_mapping_aborts(self):
        with self.assertRaises(MigrationError):
            build_migrated_config({'settings': settings(), 'account:one@example.com|android': {'awakening_completed': '14'}}, target())
        with self.assertRaises(MigrationError):
            build_migrated_config({'settings': settings(account_targets={'a': {}, 'Ａ': {}})}, target())


class DatabaseMigrationTests(unittest.TestCase):
    def test_wal_latest_commits_are_read_without_writing_any_source_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source' / 'oas-alt.db'
            owner = make_database(path, {'settings': settings()}, wal=True)
            try:
                owner.execute('INSERT INTO kv VALUES(?,?)', ('account:one@example.com|android', '{"day":"2026-10-31","awakening_completed":9}'))
                owner.commit()
                files = [path, Path(str(path) + '-wal'), Path(str(path) + '-shm')]
                before = {file: file.read_bytes() for file in files}
                snapshot = read_alt_snapshot(path)
                self.assertEqual(snapshot.values['account:one@example.com|android']['awakening_completed'], 9)
                self.assertEqual({file: file.read_bytes() for file in files}, before)
                archived = Path(directory) / 'canonical.db'
                archived.write_bytes(snapshot.database)
                reader = sqlite3.connect(archived)
                try:
                    self.assertEqual(reader.execute('SELECT COUNT(*) FROM kv').fetchone()[0], 2)
                finally:
                    reader.close()
            finally:
                owner.close()

    def test_preview_writes_nothing_apply_backs_up_original_and_archives_every_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / 'source' / 'oas-alt.db', root / 'target' / 'profile.json'
            owner = make_database(source, {'settings': settings()}, [event(index, 'login', {}) for index in range(1, 1101)])
            owner.close()
            destination.parent.mkdir()
            original = json.dumps(target(), ensure_ascii=False).encode('utf-8')
            destination.write_bytes(original)
            preview = migrate_database(source, destination)
            self.assertFalse(preview['applied'])
            self.assertEqual(list(destination.parent.iterdir()), [destination])
            self.assertEqual(destination.read_bytes(), original)
            applied = migrate_database(source, destination, apply=True)
            archive = Path(applied['archive'])
            self.assertEqual((archive / 'target.before.json').read_bytes(), original)
            self.assertEqual(len((archive / 'activity.jsonl').read_text(encoding='utf-8').splitlines()), 1100)
            migrated = json.loads(destination.read_text(encoding='utf-8'))
            self.assertFalse(migrated['account_switch']['scheduler']['enable'])
            self.assertEqual(migrated['chess'], target()['chess'])

    def test_target_inside_source_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'oas-alt.db'
            owner = make_database(source, {'settings': settings()})
            owner.close()
            destination = Path(directory) / 'profile.json'
            destination.write_text(json.dumps(target()), encoding='utf-8')
            with self.assertRaisesRegex(MigrationError, '只读'):
                migrate_database(source, destination, apply=True)

    def test_source_changes_during_copy_abort_before_reading_or_writing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.db'
            owner = make_database(source, {'settings': settings()})
            owner.close()
            from tasks.AccountSwitch import migrate_alt
            original_copy = migrate_alt.shutil.copyfile
            def changing_copy(src, dst):
                result = original_copy(src, dst)
                with source.open('ab') as stream:
                    stream.write(b'changed')
                return result
            with patch('tasks.AccountSwitch.migrate_alt.shutil.copyfile', side_effect=changing_copy):
                with self.assertRaisesRegex(MigrationError, '发生变化'):
                    read_alt_snapshot(source)

    def test_target_changed_by_another_writer_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source' / 'oas-alt.db'
            owner = make_database(source, {'settings': settings()})
            owner.close()
            destination = Path(directory) / 'target' / 'profile.json'
            destination.parent.mkdir()
            destination.write_text(json.dumps(target()), encoding='utf-8')
            original_converter = build_migrated_config
            def competing_writer(*args, **kwargs):
                result = original_converter(*args, **kwargs)
                destination.write_text('{"newer_external_config":true}', encoding='utf-8')
                return result
            with patch('tasks.AccountSwitch.migrate_alt.build_migrated_config', side_effect=competing_writer):
                with self.assertRaisesRegex(MigrationError, '目标配置'):
                    migrate_database(source, destination, apply=True)
            self.assertEqual(json.loads(destination.read_text(encoding='utf-8')), {'newer_external_config': True})


class MetadataTests(unittest.TestCase):
    def test_template_and_localized_help_cover_the_actual_account_switch_schema(self):
        root = Path(__file__).resolve().parents[2]
        template = json.loads((root / 'config' / 'template.json').read_text(encoding='utf-8'))
        block = template['account_switch']
        model = AccountSwitch.model_validate(block)
        self.assertEqual(block['account_switch_config'], model.account_switch_config.model_dump())
        self.assertEqual(set(block['runtime']), {'accounts', 'periods', 'schedule'})
        self.assertFalse(model.scheduler.enable or model.account_switch_config.routine_enable or model.account_switch_config.ios_daily_enable)
        for language in ('zh-CN', 'en-US'):
            translated = json.loads((root / 'assets' / 'i18n' / (language + '.json')).read_text(encoding='utf-8'))
            for name, field in AccountSwitchConfig.model_fields.items():
                with self.subTest(language=language, field=name):
                    self.assertTrue(translated.get(name))
                    self.assertTrue(translated.get(field.description))


if __name__ == '__main__':
    unittest.main()
