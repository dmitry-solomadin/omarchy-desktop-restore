"""Regression coverage for pre-map placement and non-disruptive restoration."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('desktop_restore', Path(__file__).resolve().parents[1] / 'lib/desktop_restore.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class SilentRestoreTests(unittest.TestCase):
    def test_desktop_launcher_arms_class_rule_before_starting_process(self):
        saved = {'kind': 'app', 'class': 'signal', 'workspace': '5',
                 'launch': ['gio', 'launch', '/usr/share/applications/signal.desktop']}
        with patch.object(app, 'hypr', return_value=[]), patch.object(app, 'run', return_value='ok') as run:
            app.launch(saved)
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ['eval', 'dispatch'])
        rule = run.call_args_list[0].args[0][-1]
        self.assertIn('initial_class = "^signal$"', rule)
        self.assertIn('workspace = "5 silent"', rule)
        self.assertIn('no_initial_focus = true', rule)
        self.assertIn('timeout = 20000', rule)
        self.assertIn('entry.rule:set_enabled(false)', rule)

    def test_failed_mapping_rule_does_not_launch_in_the_wrong_workspace(self):
        saved = {'kind': 'app', 'class': 'signal', 'workspace': '5', 'launch': ['gio', 'launch', 'signal.desktop']}
        with patch.object(app, 'hypr', return_value=[]), patch.object(app, 'run', return_value='invalid rule') as run:
            with self.assertRaisesRegex(RuntimeError, 'invalid rule'):
                app.launch(saved)
        self.assertEqual(run.call_count, 1)

    def test_terminal_guard_is_dynamic_and_does_not_override_workspace(self):
        with patch.object(app, 'hypr', return_value=[]), patch.object(app, 'run', return_value='ok') as run:
            app.launch({'kind': 'terminal', 'class': 'ghostty', 'workspace': '5', 'launch': ['ghostty']})
        self.assertEqual(run.call_count, 2)
        guard = run.call_args_list[0].args[0][-1]
        self.assertIn('focus_on_activate = false', guard)
        self.assertNotIn('workspace =', guard)
        self.assertNotIn('suppress_event', guard)
        self.assertNotIn('focus_on_activate', run.call_args.args[0][-1])

    def test_launch_attaches_silent_rules_before_mapping(self):
        saved = {'class': 'ghostty', 'workspace': 'name:Project work', 'monitor': 'DP-1',
                 'launch': ['ghostty', '--working-directory=/a path']}
        with patch.object(app, 'hypr', return_value=[{'name': 'DP-1'}]), patch.object(app, 'run', return_value='ok') as run:
            app.launch(saved)
        expression = run.call_args.args[0][-1]
        self.assertIn('workspace = "name:Project work silent"', expression)
        self.assertIn('monitor = "DP-1 silent"', expression)
        self.assertIn('no_initial_focus = true', expression)
        self.assertNotIn('suppress_event', expression)
        self.assertNotIn('focus_on_activate', expression)
        self.assertIn("'--working-directory=/a path'", expression)

    def test_disconnected_monitor_is_not_used_as_launch_target(self):
        with patch.object(app, 'hypr', return_value=[]), patch.object(app, 'run', return_value='ok') as run:
            app.launch({'class': 'ghostty', 'workspace': 'special:work', 'monitor': 'gone', 'launch': ['ghostty']})
        self.assertNotIn('monitor =', run.call_args.args[0][-1])
        self.assertIn('workspace = "special:work silent"', run.call_args.args[0][-1])

    def test_correctly_mapped_window_needs_no_move_or_fullscreen_dispatch(self):
        saved = {'workspace': 'name:work', 'monitor': 'DP-1', 'floating': False, 'fullscreen': 0}
        actual = {'address': 'new', 'workspace': {'name': 'work'}, 'monitor': 0,
                  'floating': False, 'fullscreen': 0}
        with patch.object(app, 'hypr', return_value=[{'name': 'DP-1', 'id': 0}]), patch.object(app, 'dispatch') as dispatch:
            app.place(saved, actual)
        dispatch.assert_not_called()

    def restore_scenario(self, initial, final):
        saved = {'key': 'test', 'address': 'old', 'pid': 1, 'title': 'test', 'class': 'test',
                 'kind': 'terminal', 'workspace': '9', 'launch': ['ghostty']}
        actual = {**saved, 'address': 'new', 'pid': 2}
        launched = False

        def launch(_):
            nonlocal launched
            launched = True

        def hypr(query):
            if query == 'activewindow':
                return {'address': final if launched else initial}
            if query == 'activeworkspace':
                return {'name': '1'}
            if query == 'clients':
                return ([{'address': initial, 'class': 'original'}] if initial else []) + ([actual] if launched else [])
            raise AssertionError(query)

        with patch.object(app, 'instance', return_value='test-login'), \
             patch.object(app, 'capture', return_value={'windows': []}), \
             patch.object(app, 'read_json', return_value={}), \
             patch.object(app, 'write_json'), patch.object(app, 'report'), \
             patch.object(app, 'hypr', side_effect=hypr), \
             patch.object(app, 'launch', side_effect=launch), \
             patch.object(app, 'place'), patch.object(app, 'dispatch') as dispatch:
            self.assertTrue(app.restore({'instance': 'previous-login', 'windows': [saved]}))
        return dispatch

    def test_unchanged_focus_is_not_refocused_at_the_end(self):
        self.restore_scenario('original', 'original').assert_not_called()

    def test_empty_desktop_does_not_trigger_a_workspace_switch(self):
        self.restore_scenario(None, None).assert_not_called()

    def test_user_switch_to_another_existing_window_is_respected(self):
        self.restore_scenario('original', 'user-selected').assert_not_called()

    def test_user_selecting_a_restored_window_is_not_sent_back_at_completion(self):
        self.restore_scenario('original', 'new').assert_not_called()

    def test_user_leaving_an_initially_empty_workspace_is_not_sent_back(self):
        self.restore_scenario(None, 'new').assert_not_called()


if __name__ == '__main__':
    unittest.main()
