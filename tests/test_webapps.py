import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import desktop_restore as app


class WebappTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'applications').mkdir()
        env = patch.dict(os.environ, XDG_DATA_HOME=str(self.root),
                         XDG_DATA_DIRS=str(self.root), XDG_CONFIG_HOME=str(self.root))
        env.start()
        self.addCleanup(env.stop)
        self.window = {'class': 'chrome-discord.com__channels_@me-Default',
                       'title': '(20) Discord | a different channel', 'kind': 'app',
                       'error': 'No desktop launcher found for this application'}

    def launcher(self, name, url, flags=''):
        path = self.root / 'applications' / (name + '.desktop')
        path.write_text('[Desktop Entry]\nType=Application\nName=' + name +
                        '\nExec=omarchy-launch-webapp ' + url + ' ' + flags + '\n')
        return str(path)

    def test_saved_failure_resolves_without_title_or_launcher_changes(self):
        path = self.launcher('Discord', 'https://discord.com/channels/@me')
        original = Path(path).read_text()
        saved = app.resolve_saved_app(self.window, app.desktop_apps())
        self.assertNotIn('error', saved)
        self.assertEqual(saved['launch'], ['gio', 'launch', path])
        self.assertNotIn('match_title', saved)
        self.assertEqual(Path(path).read_text(), original)

    def test_shared_browser_does_not_resolve_unknown_app_to_browser(self):
        with self.assertRaises(ValueError):
            app.app_launcher(self.window, 'chrome', {'chrome': '/apps/browser.desktop'})

    def test_ambiguous_url_launchers_are_not_guessed(self):
        self.launcher('Discord', 'https://discord.com/channels/@me')
        self.launcher('Other', 'https://discord.com/channels/@me')
        saved = app.resolve_saved_app(self.window, app.desktop_apps())
        self.assertIn('Multiple desktop launchers', saved['error'])

    def test_nondefault_profile_is_not_inferred(self):
        self.launcher('Discord', 'https://discord.com/channels/@me', '--profile-directory="Profile 2"')
        apps = app.desktop_apps()
        self.assertIn('error', app.resolve_saved_app(self.window, apps))
        saved = app.resolve_saved_app({**self.window,
                                      'class': 'chrome-discord.com__channels_@me-Profile 2'}, apps)
        self.assertIn('error', saved)

    def test_other_webapps_resolve_independently(self):
        discord = self.launcher('Discord', 'https://discord.com/channels/@me')
        slack = self.launcher('Slack', 'https://app.slack.com/client/workspace')
        apps = app.desktop_apps()
        self.assertEqual(app.resolve_saved_app(self.window, apps)['launch'][-1], discord)
        other = {**self.window, 'class': 'chrome-app.slack.com__client_workspace-Default'}
        self.assertEqual(app.resolve_saved_app(other, apps)['launch'][-1], slack)

    def test_hidden_launcher_is_not_used(self):
        path = Path(self.launcher('Discord', 'https://discord.com/channels/@me'))
        path.write_text(path.read_text() + 'Hidden=true\n')
        self.assertIn('error', app.resolve_saved_app(self.window, app.desktop_apps()))

    def test_removed_launcher_keeps_failure_retryable(self):
        saved = app.resolve_saved_app({**self.window, 'launch': ['obsolete'],
                                      'pending_restore': True}, app.desktop_apps())
        self.assertIn('error', saved)
        self.assertNotIn('launch', saved)
        self.assertTrue(saved['pending_restore'])

    def test_custom_data_directory_does_not_claim_default_profile(self):
        self.launcher('Discord', 'https://discord.com/channels/@me', '--user-data-dir=/other')
        self.assertIn('error', app.resolve_saved_app(self.window, app.desktop_apps()))

    def test_unrelated_commands_and_invalid_urls_do_not_gain_webapp_identity(self):
        for command in (['echo', 'https://discord.com/channels/@me'],
                        ['omarchy-launch-webapp'], ['omarchy-launch-webapp', 'not-a-url'],
                        ['omarchy-launch-webapp', 'file:///etc/passwd']):
            with self.subTest(command=command):
                self.assertIsNone(app.webapp_class(command))

    def test_user_hidden_override_masks_system_launcher(self):
        system = self.root / 'system' / 'applications'
        system.mkdir(parents=True)
        path = Path(self.launcher('Discord', 'https://discord.com/channels/@me'))
        (system / path.name).write_text(path.read_text())
        path.write_text('[Desktop Entry]\nHidden=true\n')
        with patch.dict(os.environ, XDG_DATA_DIRS=str(system.parent)):
            self.assertIn('error', app.resolve_saved_app(self.window, app.desktop_apps()))

    def test_capture_shared_chrome_pid_resolves_discord_during_shutdown(self):
        path = self.launcher('Discord', 'https://discord.com/channels/@me')
        window = {**self.window, 'pid': 123, 'address': 'discord', 'mapped': True,
                  'workspace': {'name': '5', 'id': 5}, 'monitor': 0,
                  'at': [0, 0], 'size': [800, 600], 'floating': False, 'fullscreen': 0}
        browser = {**window, 'class': 'google-chrome', 'address': 'browser'}
        with patch.object(app, 'hypr', side_effect=[[browser, window], [{'id': 0, 'name': 'DP-1'}]]), \
             patch.object(app, 'processes', return_value={123: {'cmd': ['/opt/google/chrome/chrome']}}), \
             patch.object(app.shutil, 'which', return_value='/usr/bin/google-chrome-stable'):
            snapshot = app.capture(fast=True)
        saved = next(w for w in snapshot['windows'] if w['class'] == window['class'])
        self.assertEqual(saved['kind'], 'app')
        self.assertEqual(saved['launch'], ['gio', 'launch', path])
        self.assertNotIn('error', saved)
        self.assertNotIn('browser_group', saved)

    def test_restore_old_failed_checkpoint_then_retry_does_not_duplicate(self):
        self.launcher('Discord', 'https://discord.com/channels/@me')
        saved = {**self.window, 'key': 'discord', 'address': 'old', 'pid': 1,
                 'workspace': '5', 'floating': False, 'fullscreen': 0}
        actual = {**saved, 'address': 'new', 'pid': 2, 'mapped': True}
        snapshot = {'instance': 'previous-login', 'windows': [saved]}
        with patch.object(app, 'STATE', self.root / 'state'), \
             patch.object(app, 'capture', side_effect=[{'windows': []}, {'windows': [actual]}]), \
             patch.object(app, 'hypr', return_value=[]), patch.object(app, 'launch') as launch, \
             patch.object(app, 'wait_for_window', return_value=(actual, False)), \
             patch.object(app, 'place'), patch.object(app, 'report'):
            self.assertTrue(app.restore(snapshot))
            self.assertTrue(app.restore(snapshot))
            result = app.read_json(app.STATE / 'last-result.json')
        launch.assert_called_once()
        self.assertNotIn('error', launch.call_args.args[0])
        self.assertEqual(result, {'restored': 0, 'already_open': 1, 'errors': []})


if __name__ == '__main__':
    unittest.main()
