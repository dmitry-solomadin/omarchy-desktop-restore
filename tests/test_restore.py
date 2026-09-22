"""Failure recovery and concurrent user interaction during restoration."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import desktop_restore as app


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for mock in (patch.object(app, 'STATE', Path(self.temp.name)),
                     patch.object(app, 'instance', return_value='new-login'),
                     patch.object(app, 'report')):
            mock.start()
            self.addCleanup(mock.stop)
        self.saved = {'kind': 'claude', 'key': 'saved-window', 'session': 'exact-id',
                      'class': 'foot', 'title': 'old title', 'pid': 1, 'address': 'old',
                      'workspace': '3', 'launch': ['foot']}
        self.actual = {**self.saved, 'address': 'new', 'pid': 2, 'title': 'startup',
                       'kind': 'agent-unresolved', 'workspace': {'name': '3'}, 'monitor': 0}
        self.snapshot = {'instance': 'old-login', 'windows': [self.saved]}

    def test_placement_failure_does_not_duplicate_a_successful_launch_on_retry(self):
        with patch.object(app, 'capture', side_effect=[{'windows': []}, {'windows': [self.actual]}]), \
             patch.object(app, 'hypr', return_value=[]), patch.object(app, 'launch') as launch, \
             patch.object(app, 'wait_for_window', return_value=(self.actual, False)), \
             patch.object(app, 'place', side_effect=RuntimeError('compositor rejected placement')):
            self.assertFalse(app.restore(self.snapshot))
            self.assertTrue(app.restore(self.snapshot))
        launch.assert_called_once()
        self.assertEqual(app.read_json(app.STATE / 'last-result.json')['already_open'], 1)

    def test_restore_preserves_a_window_moved_while_waiting_for_startup(self):
        with patch.object(app, 'capture', return_value={'windows': []}), \
             patch.object(app, 'hypr', return_value=[]), patch.object(app, 'launch'), \
             patch.object(app, 'wait_for_window', return_value=(self.actual, True)), \
             patch.object(app, 'place') as place:
            self.assertTrue(app.restore(self.snapshot))
        place.assert_not_called()

    def test_wait_detects_workspace_changes_during_startup(self):
        moved = {**self.actual, 'workspace': {'name': '7'}, 'title': self.saved['title']}
        with patch.object(app, 'hypr', side_effect=[[self.actual], [moved]]), \
             patch.object(app.time, 'sleep'):
            actual, changed = app.wait_for_window(self.saved, set(), set(), False)
        self.assertEqual(actual['workspace']['name'], '7')
        self.assertTrue(changed)

    def test_browser_wait_never_claims_an_existing_other_profile(self):
        existing = {**self.actual, 'address': 'existing-other-profile', 'title': self.saved['title']}
        new = {**self.actual, 'title': self.saved['title']}
        with patch.object(app, 'hypr', return_value=[existing, new]):
            actual, _ = app.wait_for_window(self.saved, {existing['address']}, set(), True)
        self.assertEqual(actual['address'], 'new')

    def test_another_browser_profile_does_not_block_launch(self):
        saved = {**self.saved, 'kind': 'browser', 'browser_group': 'one'}
        other = {**saved, 'browser_group': 'two', 'address': 'other', 'pid': 3}
        with patch.object(app, 'capture', return_value={'windows': [other]}), \
             patch.object(app, 'hypr', return_value=[other]), patch.object(app, 'launch') as launch, \
             patch.object(app, 'wait_for_window', return_value=(self.actual, False)), patch.object(app, 'place'):
            self.assertTrue(app.restore({**self.snapshot, 'windows': [saved]}))
        launch.assert_called_once_with(saved)

    def test_browser_multiwindow_restore_reuses_only_windows_from_its_launch(self):
        first = {**self.saved, 'kind': 'browser', 'browser_group': 'one'}
        second = {**first, 'key': 'second', 'address': 'old-second'}
        new_second = {**self.actual, 'address': 'new-second'}
        with patch.object(app, 'capture', return_value={'windows': []}), \
             patch.object(app, 'hypr', side_effect=[[{'address': 'unrelated'}], [self.actual, new_second]]), \
             patch.object(app, 'launch') as launch, patch.object(app, 'place'), \
             patch.object(app, 'wait_for_window', side_effect=[(self.actual, False), (new_second, False)]) as wait:
            self.assertTrue(app.restore({**self.snapshot, 'windows': [first, second]}))
        launch.assert_called_once()
        self.assertEqual(wait.call_args_list[1].args[1], {'unrelated'})


if __name__ == '__main__':
    unittest.main()
