import ast
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from module.config.config_model import ConfigModel
from module.exception import RequestHumanTakeover
from module.server.config_manager import (
    ConfigManager, ConfigNameError, ConfigValidationError,
)

ROOT = Path(__file__).resolve().parents[1]


class OasxConfigTransferTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.directory.name)
        Path('config').mkdir()
        self.data = json.loads((ROOT / 'config/template.json').read_text(encoding='utf-8'))
        for block in self.data.values():
            if isinstance(block, dict) and isinstance(block.get('scheduler'), dict):
                block['scheduler']['enable'] = False
        self.path = Path('config/test.json')
        self.path.write_text(json.dumps(self.data), encoding='utf-8')

    def test_in_memory_validation_does_not_load_or_save_a_named_profile(self):
        with patch.object(ConfigModel, 'read_json', side_effect=AssertionError('no disk load')), \
                patch.object(ConfigModel, 'write_json', side_effect=AssertionError('no disk write')):
            ConfigManager._validate_config_model('new', self.data)

    def test_task_copy_json_preserves_runtime_and_does_not_redact_friend(self):
        self.data['account_switch']['account_switch_config']['assist_friend'] = 'friend'
        self.path.write_text(json.dumps(self.data), encoding='utf-8')
        name, key, result = ConfigManager.load_task_for_transfer('test', 'AccountSwitch')
        self.assertEqual((name, key), ('test', 'account_switch'))
        self.assertEqual(result, {'account_switch': self.data['account_switch']})

    def test_task_import_keeps_other_tasks_and_stays_disabled(self):
        incoming = copy.deepcopy(self.data['account_switch'])
        incoming['account_switch_config']['assist_friend'] = '5791'
        ConfigManager.import_task_config('test', 'AccountSwitch', {'account_switch': incoming})
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['account_switch']['account_switch_config']['assist_friend'], '5791')
        self.assertEqual(saved['account_switch']['runtime'], self.data['account_switch']['runtime'])
        saved.pop('account_switch')
        expected = copy.deepcopy(self.data)
        expected.pop('account_switch')
        self.assertEqual(saved, expected)

    def test_unknown_task_field_is_rejected_without_modifying_file(self):
        original = self.path.read_bytes()
        incoming = copy.deepcopy(self.data['account_switch'])
        incoming['account_switch_config']['unknown_option'] = True
        with self.assertRaises(ConfigValidationError) as context:
            ConfigManager.import_task_config('test', 'AccountSwitch', {'account_switch': incoming})
        self.assertIn('account_switch.account_switch_config.unknown_option',
                      [field['field'] for field in context.exception.fields])
        self.assertEqual(self.path.read_bytes(), original)

    def test_path_traversal_and_reserved_template_import_are_rejected(self):
        for name in ('../outside', 'nested/name', 'template'):
            with self.subTest(name=name), self.assertRaises(ConfigNameError):
                ConfigManager.import_config(name, self.data)
        self.assertFalse(Path('outside.json').exists())

    def test_full_import_does_not_enable_tasks_or_load_another_profile(self):
        ConfigManager.import_config('new', self.data)
        saved = json.loads(Path('config/new.json').read_text(encoding='utf-8'))
        self.assertEqual(saved, self.data)
        self.assertEqual(ConfigManager.all_script_files(), ['new', 'test'])

    def test_newer_export_wait_and_single_courtyard_list_are_readable(self):
        self.data['global_game']['emergency']['when_network_abnormal'] = 'wait_5s'
        self.data['global_game']['costume_config']['costume_main_type'] = ['costume_main']
        ConfigManager._validate_config_model('new', self.data)
        values = {key: value for key, value in self.data.items() if key != 'config_name'}
        config = ConfigModel(config_name='new', **values)
        self.assertEqual(config.global_game.emergency.when_network_abnormal, 'wait_5s')
        self.assertEqual(config.global_game.costume_config.costume_main_type, 'costume_main')

    def test_multiple_courtyards_are_not_silently_reduced_to_one(self):
        self.data['global_game']['costume_config']['costume_main_type'] = ['costume_main', 'costume_main_1']
        with self.assertRaises(ConfigValidationError):
            ConfigManager._validate_config_model('new', self.data)

    def test_saving_known_fields_preserves_unmodeled_newer_shell_options(self):
        self.data['newer_task'] = {'scheduler': {'enable': False}, 'private_setting': 'retained'}
        self.data['script']['device']['newer_device_option'] = 'retained'
        self.path.write_text(json.dumps(self.data), encoding='utf-8')
        config = ConfigModel('test')
        config.account_switch.account_switch_config.assist_friend = '5791'
        config.save()
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['newer_task'], self.data['newer_task'])
        self.assertEqual(saved['script']['device']['newer_device_option'], 'retained')
        self.assertEqual(saved['account_switch']['account_switch_config']['assist_friend'], '5791')

    def test_removed_runtime_dictionary_entries_are_not_resurrected(self):
        self.data['account_switch']['runtime']['schedule'] = {'old_retry': {'state': 'pending'}}
        self.path.write_text(json.dumps(self.data), encoding='utf-8')
        config = ConfigModel('test')
        config.account_switch.runtime.schedule = {}
        config.save()
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['account_switch']['runtime']['schedule'], {})


class InactiveConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / 'module/server/script_router.py').read_text(encoding='utf-8'))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == 'connection_schedule')
        namespace = {'RequestHumanTakeover': RequestHumanTakeover}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<connection_schedule>', 'exec'), namespace)
        cls.read_schedule = staticmethod(namespace['connection_schedule'])

    def test_disabled_profile_returns_an_empty_schedule_without_starting_anything(self):
        def no_tasks():
            raise RequestHumanTakeover()
        config = SimpleNamespace(get_next=no_tasks, pending_task=[], waiting_task=[])
        self.assertEqual(self.read_schedule(config), {'running': {}, 'pending': [], 'waiting': []})

    def test_real_takeover_error_is_not_hidden(self):
        def fail():
            raise RequestHumanTakeover()
        config = SimpleNamespace(get_next=fail, pending_task=['task'], waiting_task=[])
        with self.assertRaises(RequestHumanTakeover):
            self.read_schedule(config)

    def test_existing_active_schedule_is_preserved(self):
        expected = {'running': {}, 'pending': [], 'waiting': [{'name': 'AccountSwitch'}]}
        config = SimpleNamespace(get_next=lambda: None, get_schedule_data=lambda: expected)
        self.assertIs(self.read_schedule(config), expected)


if __name__ == '__main__':
    unittest.main()
