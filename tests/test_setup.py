import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('setup', ROOT / 'lib/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = patch.dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'),
                         XDG_STATE_HOME=str(self.home / '.local/state'))
        env.start()
        self.addCleanup(env.stop)
        self.commands = []

        def run(args):
            self.commands.append(args)
            return '[]' if args == ['hyprctl', '-j', 'binds'] else ''

        for mocking in (patch.object(setup, 'run', side_effect=run),
                        patch.object(setup.shutil, 'which', return_value='/usr/bin/python3'),
                        patch.object(setup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', ''))):
            mocking.start()
            self.addCleanup(mocking.stop)
        self.integration = setup.Setup(self.home / 'plugin with spaces')
        self.integration.bindings.parent.mkdir(parents=True)
        self.integration.bindings.write_text('-- My keybindings\n')
        self.integration.menu.parent.mkdir(parents=True)
        self.original_menu = '{\n // Keep my comments\n "personal": {"action": "open https://example.com/a,b"}\n}\n'
        self.integration.menu.write_text(self.original_menu)

    def test_install_and_uninstall_round_trip_with_spaces_in_path(self):
        self.integration.install()
        menu = setup.parse_jsonc(self.integration.menu.read_text())
        self.assertIn("'", menu['system.reboot']['action'])
        self.assertIn('exec omarchy-system-reboot', menu['system.reboot']['action'])
        self.assertIn('personal', menu)
        self.assertEqual(self.integration.menu.stat().st_mode & 0o777, 0o644)
        unit = (self.home / '.config/systemd/user' / setup.UNIT).read_text()
        self.assertIn('0.7s', unit)
        self.assertIn('TimeoutStopSec=1s', unit)
        self.integration.install()  # idempotent, no repeated blocks
        self.assertEqual(self.integration.menu.read_text().count(setup.BEGIN), 1)
        self.integration.uninstall()
        self.assertEqual(self.integration.menu.read_text(), self.original_menu)
        self.assertEqual(self.integration.bindings.read_text(), '-- My keybindings\n')
        self.assertFalse((self.home / '.local/bin/desktop-restore').exists())
        self.assertFalse(self.integration.receipt.exists())

    def test_uninstall_preserves_unrelated_later_config_changes(self):
        self.integration.install()
        with self.integration.bindings.open('a') as stream:
            stream.write('-- Later customization\n')
        menu = self.integration.menu.read_text()
        last = menu.rfind('}')
        self.integration.menu.write_text(menu[:last] + ' "another": {"label": "Keep me"}\n' + menu[last:])
        self.integration.uninstall()
        self.assertIn('Later customization', self.integration.bindings.read_text())
        parsed = setup.parse_jsonc(self.integration.menu.read_text())
        self.assertEqual(parsed['another']['label'], 'Keep me')
        self.assertNotIn('system.reboot', parsed)

    def test_custom_power_actions_are_not_overwritten(self):
        self.integration.menu.write_text('{"system.reboot":{"action":"my-own-reboot"}}')
        with self.assertRaisesRegex(RuntimeError, 'already customized'):
            self.integration.install()
        self.assertEqual(self.integration.bindings.read_text(), '-- My keybindings\n')
        self.assertFalse(self.integration.receipt.exists())

    def test_existing_shortcut_is_not_overridden(self):
        with patch.object(setup, 'run', return_value=json.dumps([{'modmask': 65, 'key': 'R'}])):
            with self.assertRaisesRegex(RuntimeError, 'already bound'):
                self.integration.install()

    def test_failed_hyprland_validation_rolls_back_all_files(self):
        def run(args):
            if args == ['hyprctl', '-j', 'binds']:
                return '[]'
            if args == ['hyprctl', 'configerrors']:
                return 'Invalid configuration'
            return ''
        with patch.object(setup, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'Invalid configuration'):
                self.integration.install()
        self.assertEqual(self.integration.menu.read_text(), self.original_menu)
        self.assertEqual(self.integration.bindings.read_text(), '-- My keybindings\n')
        self.assertFalse((self.home / '.local/bin/desktop-restore').exists())
        self.assertFalse(self.integration.receipt.exists())

    def test_jsonc_handles_urls_escaped_quotes_comments_and_trailing_commas(self):
        text = r'''{
            /* a comment */ "url": "https://example.com/a//b",
            "quoted": "say \"hello\"", // another comment
            "literal": ",}",
        }'''
        self.assertEqual(setup.parse_jsonc(text)['literal'], ',}')
        for source in ('{}', '{"x":1,}', '{"x":1}', text):
            after, block = setup.menu_block(source, Path('/a path/power-action'))
            self.assertIn('system.shutdown', setup.parse_jsonc(after))
            self.assertEqual(after.replace(block, ''), source)

    def test_systemd_paths_escape_specifiers_and_environment_expansion(self):
        quoted = setup.systemd_arg('/a path/50%/$something/file')
        self.assertEqual(quoted, '"/a path/50%%/$$something/file"')

    def test_edited_managed_content_is_retained_on_uninstall(self):
        self.integration.install()
        launcher = self.home / '.local/bin/desktop-restore'
        launcher.write_text('#!/bin/sh\necho custom\n')
        with self.assertRaisesRegex(RuntimeError, 'Managed content was edited'):
            self.integration.uninstall()
        self.assertIn('custom', launcher.read_text())
        self.assertTrue(self.integration.receipt.exists())


if __name__ == '__main__':
    unittest.main()
