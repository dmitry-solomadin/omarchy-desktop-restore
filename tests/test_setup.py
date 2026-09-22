import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REAL_RUN = subprocess.run
spec = importlib.util.spec_from_file_location('setup', ROOT / 'lib/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = patch.dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home / '.codex'),
                         CLAUDE_CONFIG_DIR=str(self.home / '.claude'), XDG_CONFIG_HOME=str(self.home / '.config'),
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
        self.assertFalse(self.integration.state.exists())
        self.assertEqual(list(self.home.rglob('*.bak.desktop-restore-*')), [])

    def test_install_on_foot_only_desktop_does_not_require_ghostty(self):
        with patch.object(setup.shutil, 'which', side_effect=lambda command:
                          None if command == 'ghostty' else '/usr/bin/' + command):
            self.integration.install()
        self.assertTrue(self.integration.receipt.exists())

    def test_watcher_and_lifecycle_use_the_same_xdg_directories(self):
        self.integration.install()
        for unit in (setup.UNIT, setup.LIFECYCLE_UNIT):
            text = (self.home / '.config/systemd/user' / unit).read_text()
            self.assertIn('XDG_CONFIG_HOME=' + str(self.integration.config), text)
            self.assertIn('XDG_STATE_HOME=' + str(self.integration.state.parent), text)

    def test_failed_upgrade_rolls_back_new_files_receipt_and_user_edits(self):
        with patch.object(setup.shutil, 'which', side_effect=lambda command:
                          None if command == 'codex' else '/usr/bin/python3'):
            self.integration.install()
        settings = self.home / '.claude/settings.json'
        value = json.loads(settings.read_text())
        value['theme'] = 'changed-since-install'
        settings.write_text(json.dumps(value))
        receipt = json.loads(self.integration.receipt.read_text())
        paths = [Path(item['path']) for item in receipt['files']] + [self.integration.receipt]
        before = {path: (path.read_bytes(), path.stat().st_mode) for path in paths}
        original_write = setup.write
        failed = False

        def fail_receipt(path, *args):
            nonlocal failed
            if path == self.integration.receipt and not failed:
                failed = True
                raise OSError('simulated write failure')
            original_write(path, *args)

        with patch.object(setup, 'write', side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.integration.install()
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mode) for path in paths})
        self.assertFalse((self.home / '.codex/hooks.json').exists())

    def test_uninstall_refuses_a_managed_path_replaced_by_a_symlink(self):
        self.integration.install()
        path = self.home / '.local/bin/desktop-restore'
        original = path.with_name('user-launcher')
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.integration.uninstall()
        self.assertTrue(path.is_symlink())
        self.assertTrue(self.integration.receipt.exists())

    def test_failed_service_restart_rolls_back_upgrade_receipt_and_files(self):
        self.integration.root.mkdir()
        self.integration.install()
        (self.integration.root / 'manifest.json').write_text('{"version":"new"}')
        receipt = json.loads(self.integration.receipt.read_text())
        paths = [Path(item['path']) for item in receipt['files']] + [self.integration.receipt]
        before = {path: path.read_bytes() for path in paths}

        def fail_restart(args):
            if 'restart' in args:
                raise RuntimeError('Service restart failed')
            return ''

        with patch.object(setup, 'run', side_effect=fail_restart):
            with self.assertRaisesRegex(RuntimeError, 'restart failed'):
                self.integration.start()
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        self.assertNotEqual(json.loads(self.integration.receipt.read_text())['revision'], self.integration.revision())

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

    def test_shell_entry_point_installs_once_without_restarting_on_reload(self):
        self.integration.root.mkdir()
        self.integration.execute('start')
        self.assertTrue(self.integration.receipt.exists())
        before = self.integration.receipt.read_bytes()
        self.commands.clear()
        self.integration.execute('start')
        self.assertEqual(self.commands, [['systemctl', '--user', 'start', setup.UNIT, setup.LIFECYCLE_UNIT]])
        self.assertEqual(self.integration.receipt.read_bytes(), before)
        self.assertEqual(self.integration.menu.read_text().count(setup.BEGIN), 1)

    def test_shell_start_refreshes_code_updates_once_and_keeps_user_settings(self):
        self.integration.root.mkdir()
        self.integration.execute('start')
        self.integration.bindings.write_text(self.integration.bindings.read_text() + '-- user setting\n')
        (self.integration.root / 'manifest.json').write_text('{"version":"updated"}')
        self.commands.clear()
        self.integration.execute('start')
        self.assertIn(['systemctl', '--user', 'restart', setup.UNIT, setup.LIFECYCLE_UNIT], self.commands)
        self.assertIn('-- user setting', self.integration.bindings.read_text())
        self.commands.clear()
        self.integration.execute('start')
        self.assertEqual(self.commands, [['systemctl', '--user', 'start', setup.UNIT, setup.LIFECYCLE_UNIT]])

    def test_shell_start_upgrades_receipts_without_a_revision(self):
        self.integration.root.mkdir()
        self.integration.install()
        receipt = json.loads(self.integration.receipt.read_text())
        receipt.pop('revision')
        self.integration.receipt.write_text(json.dumps(receipt))
        self.integration.execute('start')
        self.assertEqual(json.loads(self.integration.receipt.read_text())['revision'], self.integration.revision())

    def test_shell_start_picks_up_a_newly_installed_agent(self):
        self.integration.root.mkdir()
        with patch.object(setup.shutil, 'which', side_effect=lambda command:
                          None if command == 'codex' else '/usr/bin/python3'):
            self.integration.execute('start')
        self.assertFalse((self.home / '.codex/hooks.json').exists())
        self.integration.execute('start')
        self.assertTrue((self.home / '.codex/hooks.json').exists())

    def test_shell_start_does_not_recreate_a_removed_plugin(self):
        self.integration.execute('start')
        self.assertFalse(self.integration.state.exists())
        self.assertEqual(self.commands, [])

    def test_auto_setup_conflict_preserves_user_configuration(self):
        self.integration.root.mkdir()
        self.integration.menu.write_text('{"system.reboot":{"action":"my-own-reboot"}}')
        with self.assertRaisesRegex(RuntimeError, 'already customized'):
            self.integration.execute('start')
        self.assertFalse(self.integration.receipt.exists())
        self.assertEqual(self.integration.bindings.read_text(), '-- My keybindings\n')

    def test_concurrent_shell_startups_install_only_once(self):
        fakebin = self.home / 'fakebin'
        fakebin.mkdir()
        log = self.home / 'commands.log'
        for command in ('hyprctl', 'systemctl', 'uwsm-app', 'gio', 'timeout', 'busctl'):
            path = fakebin / command
            text = f'#!/bin/sh\nprintf "%s\\n" "{command} $*" >> {shlex.quote(str(log))}\n'
            if command == 'hyprctl':
                text += 'if [ "$1" = "-j" ]; then /usr/bin/sleep 0.1; printf "[]\\n"; fi\n'
            path.write_text(text + 'exit 0\n')
            path.chmod(0o700)
        env = {**os.environ, 'PATH': str(fakebin) + ':' + os.environ['PATH']}
        workers = [subprocess.Popen([sys.executable, '-B', str(ROOT / 'lib/setup.py'), 'start'],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                   for _ in range(2)]
        try:
            for worker in workers:
                _, stderr = worker.communicate(timeout=15)
                self.assertEqual(worker.returncode, 0, stderr)
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate()
        commands = log.read_text().splitlines()
        self.assertEqual(commands.count('hyprctl -j binds'), 1)
        self.assertFalse(any('restart' in command for command in commands))
        self.assertEqual(self.integration.menu.read_text().count(setup.BEGIN), 1)

    def test_agent_hooks_keep_user_settings_across_update_and_uninstall(self):
        path = self.home / '.claude/settings.json'
        path.parent.mkdir()
        original = {'model': 'custom', 'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'my-hook'}]}]}}
        path.write_text(json.dumps(original))
        self.integration.install()
        current = json.loads(path.read_text())
        self.assertEqual(len(current['hooks']['SessionStart']), 2)
        current['theme'] = 'user-change-after-install'
        path.write_text(json.dumps(current))
        self.integration.install()
        self.assertEqual(len(json.loads(path.read_text())['hooks']['SessionStart']), 2)
        self.integration.uninstall()
        self.assertEqual(json.loads(path.read_text()), {**original, 'theme': 'user-change-after-install'})
        self.assertFalse((self.home / '.codex/hooks.json').exists())

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
        self.assertEqual(list(self.home.rglob('*.bak.desktop-restore-*')), [])

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

    def test_plugin_folder_removal_cleans_up_using_surviving_helper(self):
        self.integration.root.mkdir()
        self.integration.install()
        saved = self.integration.state / 'restore.json'
        saved.write_text('{"windows": ["delete this checkpoint"]}')
        helper = self.integration.state / 'cleanup.py'
        fakebin = self.home / 'fakebin'
        fakebin.mkdir()
        for command in ('hyprctl', 'systemctl'):
            path = fakebin / command
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o700)
        # The remover and its source folder are gone before the monitor starts.
        # Only the installed cleanup copy and receipt remain available.
        shutil.rmtree(self.integration.root)
        result = REAL_RUN([sys.executable, '-B', str(helper), 'watch-removal'],
                          env={**os.environ, 'PATH': str(fakebin) + ':' + os.environ['PATH']},
                          capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(helper.exists())
        self.assertFalse(self.integration.receipt.exists())
        self.assertFalse((self.home / '.config/systemd/user' / setup.UNIT).exists())
        self.assertFalse((self.home / '.config/systemd/user' / setup.LIFECYCLE_UNIT).exists())
        self.assertFalse((self.home / '.local/bin/desktop-restore').exists())
        self.assertFalse((self.home / '.config/omarchy/hooks/post-boot.d/desktop-restore').exists())
        self.assertEqual(self.integration.menu.read_text(), self.original_menu)
        self.assertEqual(self.integration.bindings.read_text(), '-- My keybindings\n')
        self.assertFalse(self.integration.state.exists())
        self.assertEqual(list(self.home.rglob('*.bak.desktop-restore-*')), [])
        self.integration.execute('start')
        self.assertFalse(self.integration.state.exists())

    def test_uninstall_removes_all_private_data_and_legacy_backups(self):
        self.integration.execute('install')
        for name in ('latest.json', 'restore.json', 'shutdown.json', 'sessions-cache.json',
                     'sessions-v1-test.json', 'last-result.json', 'instance.json',
                     'restored-windows.json', 'setup.lock', 'watch.lock', 'restore.lock', 'state.lock'):
            (self.integration.state / name).write_text('private data')
        agents = self.integration.state / 'agents'
        agents.mkdir()
        (agents / 'claude-123.json').write_text('session identity')
        backups = self.integration.state.with_name('desktop-restore-backups')
        backups.mkdir()
        (backups / 'old-migration.tar.gz').write_bytes(b'backup')
        legacy = self.integration.bindings.with_name('bindings.lua.bak.desktop-restore-20260918')
        legacy.write_text('old installer backup')
        unrelated = legacy.with_name('bindings.lua.bak.personal')
        unrelated.write_text('personal backup')
        other_state = self.integration.state.parent / 'other-app.json'
        other_state.write_text('other app')
        self.integration.execute('uninstall')
        self.assertFalse(self.integration.state.exists())
        self.assertFalse(backups.exists())
        self.assertFalse(legacy.exists())
        self.assertEqual(list(self.home.rglob('*.bak.desktop-restore-*')), [])
        self.assertEqual(unrelated.read_text(), 'personal backup')
        self.assertEqual(other_state.read_text(), 'other app')
        self.integration.execute('uninstall')  # Repeated cleanup leaves no lock file.
        self.assertFalse(self.integration.state.exists())

    def test_data_cleanup_does_not_follow_symlinks(self):
        self.integration.execute('install')
        external = self.home / 'external'
        external.mkdir()
        sentinel = external / 'keep.json'
        sentinel.write_text('unrelated data')
        (self.integration.state / 'linked').symlink_to(external, target_is_directory=True)
        backups = self.integration.state.with_name('desktop-restore-backups')
        backups.symlink_to(external, target_is_directory=True)
        self.integration.execute('uninstall')
        self.assertFalse(self.integration.state.exists())
        self.assertFalse(backups.is_symlink())
        self.assertEqual(sentinel.read_text(), 'unrelated data')

    def test_cleanup_without_receipt_removes_leftover_saved_data(self):
        self.integration.state.mkdir(parents=True)
        (self.integration.state / 'restore.json').write_text('old checkpoint')
        self.integration.execute('uninstall')
        self.assertFalse(self.integration.state.exists())

    def test_uninstall_conflict_keeps_data_for_retry(self):
        self.integration.execute('install')
        saved = self.integration.state / 'restore.json'
        saved.write_text('checkpoint')
        launcher = self.home / '.local/bin/desktop-restore'
        launcher.write_text('#!/bin/sh\necho edited\n')
        with self.assertRaisesRegex(RuntimeError, 'Managed content was edited'):
            self.integration.execute('uninstall')
        self.assertEqual(saved.read_text(), 'checkpoint')
        self.assertTrue(self.integration.receipt.exists())

    def test_failed_uninstall_validation_restores_integration_for_retry(self):
        self.integration.execute('install')
        saved = self.integration.state / 'restore.json'
        saved.write_text('checkpoint')
        before = self.integration.receipt.read_bytes()
        with patch.object(setup, 'run', side_effect=lambda args:
                          'Invalid configuration' if args == ['hyprctl', 'configerrors'] else ''):
            with self.assertRaisesRegex(RuntimeError, 'Invalid configuration'):
                self.integration.execute('uninstall')
        self.assertEqual(saved.read_text(), 'checkpoint')
        self.assertEqual(self.integration.receipt.read_bytes(), before)
        self.assertTrue((self.integration.state / 'cleanup.py').exists())
        self.integration.execute('uninstall')
        self.assertFalse(self.integration.state.exists())

    def test_monitor_ignores_shell_disable_and_brief_folder_replacement(self):
        self.integration.root.mkdir()
        self.integration.install()
        calls = 0

        def step(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.integration.root.rmdir()
            elif calls == 2:
                self.integration.root.mkdir()
            elif calls == 3:
                raise InterruptedError('end test')

        with patch.object(setup.time, 'sleep', side_effect=step), patch.object(self.integration, 'uninstall') as uninstall:
            with self.assertRaises(InterruptedError):
                self.integration.watch_removal()
        uninstall.assert_not_called()
        self.assertTrue(self.integration.receipt.exists())

    def test_existing_installation_gets_lifecycle_upgrade_without_rewriting_bindings(self):
        self.integration.install()
        receipt = json.loads(self.integration.receipt.read_text())
        for path, _, _ in self.integration.lifecycle_files():
            if path.name != 'desktop-restore':
                path.unlink()
                receipt['files'] = [entry for entry in receipt['files'] if entry['path'] != str(path)]
        self.integration.receipt.write_text(json.dumps(receipt))
        bindings = self.integration.bindings.read_text() + '-- new user setting\n'
        self.integration.bindings.write_text(bindings)
        self.integration.install()
        self.assertEqual(self.integration.bindings.read_text(), bindings)
        self.assertTrue((self.integration.state / 'cleanup.py').exists())
        self.assertTrue((self.home / '.config/systemd/user' / setup.LIFECYCLE_UNIT).exists())
        self.integration.uninstall()
        self.assertIn('new user setting', self.integration.bindings.read_text())


if __name__ == '__main__':
    unittest.main()
