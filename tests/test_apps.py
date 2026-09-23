"""Launchable desktop entries and application identity inside a shared shell."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import desktop_restore as app


class AppLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user = self.root / 'user'
        self.system = self.root / 'system'
        for root in (self.user, self.system):
            (root / 'applications').mkdir(parents=True)
        for mock in (patch.dict(os.environ, XDG_DATA_HOME=str(self.user), XDG_DATA_DIRS=str(self.system)),
                     patch.object(app, 'STATE', self.root / 'state'),
                     patch.object(app, 'instance', return_value='new-login'),
                     patch.object(app, 'report')):
            mock.start()
            self.addCleanup(mock.stop)
        self.desktop_id = 'org.example.tool'
        self.launcher = self.entry(self.desktop_id, 'Exec=omarchy-shell org.example.tool open\n')
        self.generic = self.entry('org.quickshell', 'NoDisplay=true\n', self.system)
        self.client = {'class': self.desktop_id, 'title': 'Custom tool', 'address': 'new', 'pid': 10,
                       'workspace': {'id': 3, 'name': '3'}, 'monitor': 0, 'mapped': True,
                       'at': [0, 0], 'size': [800, 600], 'floating': False, 'fullscreen': 0}
        self.legacy = {**self.client, 'class': 'org.quickshell', 'kind': 'app', 'key': 'saved', 'workspace': '3',
                       'address': 'old', 'pid': 1, 'launch': ['gio', 'launch', str(self.generic)]}

    def entry(self, name, fields, root=None):
        path = (root or self.user) / 'applications' / (name + '.desktop')
        path.write_text('[Desktop Entry]\nType=Application\nName=Example\n' + fields)
        return path

    def test_identity_only_desktop_entries_are_not_launchers(self):
        apps = app.desktop_apps()
        self.assertNotIn('org.quickshell', apps)
        self.assertEqual(apps[self.desktop_id], str(self.launcher))
        self.assertNotIn('omarchy-shell', apps)

    def test_dbus_and_nodisplay_entries_can_still_be_launchable(self):
        bus = self.entry('org.example.Bus', 'DBusActivatable=true\n')
        hidden_menu = self.entry('org.example.HiddenMenu', 'Exec=hidden-app\nNoDisplay=true\n')
        apps = app.desktop_apps()
        self.assertEqual(apps['org.example.bus'], str(bus))
        self.assertEqual(apps['hidden-app'], str(hidden_menu))

    def test_hidden_or_invalid_user_override_masks_system_launcher(self):
        self.entry('example', 'Exec=system-app\nStartupWMClass=ExampleClass\n', self.system)
        for fields in ('Hidden=true\n', 'Exec="unclosed\n', 'Terminal=true\nExec=terminal-app\n'):
            self.entry('example', fields)
            apps = app.desktop_apps()
            self.assertNotIn('example', apps)
            self.assertNotIn('system-app', apps)
            self.assertNotIn('exampleclass', apps)

    def test_non_application_entry_is_ignored(self):
        path = self.entry('link', 'Exec=not-an-app\n')
        path.write_text(path.read_text().replace('Type=Application', 'Type=Link'))
        self.assertNotIn('link', app.desktop_apps())

    def test_capture_associates_custom_application_id_with_its_launcher(self):
        procs = {10: {'cmd': ['quickshell', '-n', '-p', '/usr/share/omarchy/shell']}}
        with patch.object(app, 'processes', return_value=procs), \
             patch.object(app, 'hypr', side_effect=[[self.client], [{'id': 0, 'name': 'DP-1'}]]):
            saved, = app.capture()['windows']
        self.assertNotIn('error', saved)
        self.assertEqual(saved['launch'], ['gio', 'launch', str(self.launcher)])
        self.assertEqual(saved['desktop_id'], self.desktop_id)
        self.assertNotIn('match_title', saved)

    def test_unknown_shell_window_never_falls_back_to_a_generic_or_other_plugin_launcher(self):
        self.entry('org.quickshell', 'Exec=quickshell\n', self.system)
        self.entry('another-plugin', 'Exec=omarchy-shell another-plugin open\n')
        with self.assertRaisesRegex(ValueError, 'per-application ID'):
            app.app_launcher(self.legacy, 'quickshell', app.desktop_apps())
        with self.assertRaisesRegex(ValueError, 'No desktop launcher'):
            app.app_launcher({**self.client, 'class': 'unknown', 'title': 'Other'}, 'omarchy-shell', app.desktop_apps())

    def test_missing_custom_launcher_fails_instead_of_selecting_quickshell(self):
        self.entry(self.desktop_id, 'Hidden=true\n')
        with self.assertRaisesRegex(ValueError, 'No desktop launcher'):
            app.app_launcher(self.client, 'quickshell', app.desktop_apps())

    def test_legacy_generic_checkpoint_is_skipped_without_a_launch_or_wait(self):
        snapshot = {'instance': 'previous-login', 'windows': [self.legacy]}
        original = copy.deepcopy(snapshot)
        with patch.object(app, 'capture', return_value={'windows': []}), \
             patch.object(app, 'hypr', return_value=[]), patch.object(app, 'launch') as launch, \
             patch.object(app, 'wait_for_window') as wait:
            self.assertFalse(app.restore(snapshot))
        launch.assert_not_called()
        wait.assert_not_called()
        self.assertEqual(snapshot, original)

    def test_existing_other_quickshell_window_is_not_mistaken_for_custom_tool(self):
        saved = app.resolve_saved_app(self.legacy, app.desktop_apps())
        other = {**self.client, 'class': 'org.quickshell', 'kind': 'app', 'title': 'Settings'}
        self.assertIsNone(app.existing_window(saved, [other], set()))
        actual = {**saved, 'address': 'new', 'pid': 10}
        self.assertEqual(app.existing_window(saved, [other, actual], set()), actual)

    def test_removed_title_mapping_is_not_reused_from_a_checkpoint(self):
        saved = app.resolve_saved_app({**self.legacy, 'desktop_id': self.desktop_id,
                                      'match_title': 'Custom tool',
                                      'launch': ['gio', 'launch', str(self.launcher)]}, app.desktop_apps())
        self.assertIn('per-application ID', saved['error'])
        for key in ('desktop_id', 'match_title', 'launch'):
            self.assertNotIn(key, saved)

    def test_declared_startup_class_identifies_custom_launcher(self):
        launcher = self.entry('another', 'Exec=wrapper --tool\nStartupWMClass=CustomClass\n')
        result = app.app_launcher({**self.client, 'class': 'CustomClass'}, 'quickshell', app.desktop_apps())
        self.assertEqual(result['launch'][-1], str(launcher))

    def test_ambiguous_class_or_executable_never_chooses_first_launcher(self):
        for name in ('one', 'two'):
            self.entry(name, 'Exec=wrapper --' + name + '\nStartupWMClass=SharedClass\n')
        for window_class, executable in (('SharedClass', 'one'), ('unmapped', 'wrapper')):
            with self.subTest(window_class=window_class), self.assertRaisesRegex(ValueError, 'Multiple desktop launchers'):
                app.app_launcher({**self.client, 'class': window_class}, executable, app.desktop_apps())

    def test_exact_desktop_id_takes_precedence_over_class_and_executable_aliases(self):
        self.entry('alias', 'Exec=org.example.tool\nStartupWMClass=org.example.tool\n')
        self.assertEqual(app.app_launcher(self.client, 'quickshell', app.desktop_apps())['launch'][-1],
                         str(self.launcher))

    def test_window_title_does_not_influence_launcher_selection(self):
        apps = app.desktop_apps()
        expected = app.app_launcher(self.client, 'quickshell', apps)
        self.assertEqual(app.app_launcher({**self.client, 'title': 'Different document'}, 'quickshell', apps), expected)


if __name__ == '__main__':
    unittest.main()
