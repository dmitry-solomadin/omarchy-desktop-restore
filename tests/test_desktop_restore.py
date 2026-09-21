import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import time
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
spec = importlib.util.spec_from_file_location('desktop_restore', ROOT / 'lib/desktop_restore.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = patch.object(app, 'STATE', Path(self.temp.name) / 'state')
        self.state.start()
        self.addCleanup(self.state.stop)
        self.env = patch.dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE='test-instance')
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_shutdown_snapshot_wins_over_window_teardown(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'STATE', Path(directory)):
            with patch.dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE='old-login'):
                app.initialize()
                full = {'windows': [{'title': 'all windows'}], 'instance': 'old-login'}
                app.write_json(app.STATE / 'shutdown.json', full)
                app.save({'windows': [{'title': 'last window closing'}], 'instance': 'old-login'})
            with patch.dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE='new-login'):
                app.initialize()
                self.assertEqual(app.read_json(app.STATE / 'restore.json'), full)

    def test_shutdown_capture_never_calls_opencode_api(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'STATE', Path(directory)):
            client = {'title': 'OC | Example'}
            with patch.object(app, 'hypr', side_effect=[[client], []]), \
                 patch.object(app, 'processes', return_value={}), \
                 patch.object(app, 'desktop_apps', return_value={}), \
                 patch.object(app, 'sessions', side_effect=AssertionError('API must not run')):
                # An unmapped window skips per-window reconstruction, but exercises metadata loading.
                client.update(workspace={'id': 1}, at=[0, 0])
                app.capture(fast=True)

    def test_shutdown_lock_contention_returns_immediately(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'STATE', Path(directory)):
            with app.lock('restore'):
                start = time.monotonic()
                with self.assertRaises(BlockingIOError):
                    app.save_shutdown()
                self.assertLess(time.monotonic() - start, 0.1)

    def test_uncached_conversation_keeps_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'STATE', Path(directory)):
            old = {'windows': [{'title': 'old'}]}
            app.write_json(app.STATE / 'shutdown.json', old)
            with patch.object(app, 'capture', return_value={'windows': [{'title': 'OC | New', 'error': 'not cached'}]}):
                app.save_shutdown()
            self.assertEqual(app.read_json(app.STATE / 'shutdown.json'), old)

    def test_service_restart_does_not_create_shutdown_snapshot(self):
        with patch.object(app, 'run', return_value='b false'), patch.object(app, 'capture') as capture:
            app.save_shutdown(if_shutting_down=True)
            capture.assert_not_called()

    def test_power_action_continues_after_failed_or_hung_save(self):
        source = (ROOT / 'bin/power-action').read_text()
        for action in ('reboot', 'shutdown'):
            for saver in ('#!/bin/sh\nexit 1\n', '#!/bin/sh\nexec /usr/bin/sleep 30\n'):
                with self.subTest(action=action, saver=saver), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    # Replace BOTH power binaries before executing any copy. No real power action in tests.
                    safe_source = source.replace('${OMARCHY_PATH:-/usr/share/omarchy}/bin/omarchy-system-', str(root / 'power-'))
                    safe_source = safe_source.replace('0.7s python3', '0.7s ' + str(root / 'saver'))
                    (root / 'wrapper').write_text(safe_source)
                    (root / 'saver').write_text(saver)
                    (root / ('power-' + action)).write_text('#!/bin/sh\nprintf "power reached\\n"\n')
                    for p in root.iterdir():
                        p.chmod(0o700)
                    start = time.monotonic()
                    result = subprocess.run(['/bin/sh', str(root / 'wrapper'), action], capture_output=True,
                                            text=True, timeout=3)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, 'power reached\n')
                    self.assertLess(time.monotonic() - start, 1.5)

    def test_json_status_is_read_only_and_works_without_a_checkpoint(self):
        with patch.object(app, 'CONFIG', Path(self.temp.name) / 'config'):
            result = app.status(app.STATE / 'restore.json')
        self.assertEqual(result['windows'], [])
        self.assertIsNone(result['saved'])
        self.assertFalse(app.STATE.exists())

    def test_new_login_keeps_previous_snapshot_despite_new_autosaves(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'STATE', Path(directory)):
            with patch.dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE='old-login'):
                app.initialize()
                old = {'windows': [{'title': 'work'}], 'instance': 'old-login'}
                app.save(old, explicit=True)
            with patch.dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE='new-login'):
                app.initialize()
                app.save({'windows': [{'title': 'new terminal'}], 'instance': 'new-login'})
                app.initialize()  # watcher restart must not rotate again
                self.assertEqual(app.read_json(app.STATE / 'restore.json'), old)
                app.save({'windows': []})  # closing all windows must not erase state
                self.assertEqual(app.read_json(app.STATE / 'restore.json'), old)

    def test_ambiguous_opencode_titles_never_choose_latest(self):
        sessions = [{'id': 'one', 'title': 'Fix project tests'}, {'id': 'two', 'title': 'Fix project build'}]
        with self.assertRaises(ValueError):
            app.session_for_title('OC | Fix project…', sessions)
        self.assertEqual(app.session_for_title('OC | Fix project tests', sessions)['id'], 'one')

    def test_ghostty_shared_pid_uses_each_windows_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            one, two = Path(directory) / 'one', Path(directory) / 'two'
            one.mkdir()
            two.mkdir()
            procs = {10: {'parent': 1, 'cmd': ['zsh'], 'cwd': str(one)},
                     11: {'parent': 1, 'cmd': ['zsh'], 'cwd': str(two)}}
            self.assertEqual(app.terminal_cwd({'pid': 1, 'title': str(two)}, procs), str(two))
            with self.assertRaises(ValueError):
                app.terminal_cwd({'pid': 1, 'title': 'Custom title'}, procs)

    def test_open_windows_matched_one_to_one(self):
        saved = {'kind': 'terminal', 'class': 'ghostty', 'cwd': '/work', 'title': '/work', 'address': 'old', 'pid': 1}
        first = {**saved, 'address': 'new1', 'pid': 2}
        second = {**saved, 'address': 'new2', 'pid': 3}
        self.assertEqual(app.existing_window(saved, [first, second], {'new1'})['address'], 'new2')
        self.assertIsNone(app.existing_window(saved, [first, second], {'new1', 'new2'}))

    def test_multiple_opencode_sessions_in_same_directory_do_not_collide(self):
        saved = {'kind': 'opencode', 'session': 'one', 'title': 'OC | one', 'address': 'old', 'pid': 1}
        other = {**saved, 'session': 'two', 'address': 'new', 'pid': 2}
        self.assertIsNone(app.existing_window(saved, [other], set()))

    def test_paginated_api_uses_next_cursor(self):
        pages = ['{"data":[{"id":"one"}],"cursor":{"next":"abc","previous":null}}',
                 '{"data":[],"cursor":{"next":null}}']
        with patch.object(app, 'run', side_effect=pages) as run:
            self.assertEqual(app.sessions(), [{'id': 'one'}])
            self.assertIn('cursor=abc', run.call_args[0][0][-1])


if __name__ == '__main__':
    unittest.main()
