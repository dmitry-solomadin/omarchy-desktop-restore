"""Failure recovery and concurrent user interaction during restoration."""
from pathlib import Path
import sys
import tempfile
import threading
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
                       'kind': 'agent-unresolved', 'workspace': {'name': '3'}, 'monitor': 0, 'mapped': True}
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

    def test_wait_does_not_claim_hidden_or_unmapped_matching_windows(self):
        new = {**self.actual, 'title': self.saved['title']}
        hidden = {**new, 'address': 'hidden', 'hidden': True}
        unmapped = {**new, 'address': 'unmapped', 'mapped': False}
        with patch.object(app, 'hypr', side_effect=[[hidden, unmapped], [hidden, unmapped, new]]), \
             patch.object(app.time, 'sleep'):
            actual, _ = app.wait_for_window(self.saved, set(), set(), False)
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

    def test_stalled_app_does_not_delay_other_classes_or_shared_agent_entries(self):
        stocks = {**self.saved, 'kind': 'app', 'class': 'stocks', 'key': 'stocks', 'title': 'Stocks'}
        first = {**self.saved, 'key': 'agent-one', 'placement': 'approximate'}
        second = {**first, 'key': 'agent-two', 'session': 'another-id'}
        signal = {**self.saved, 'kind': 'app', 'class': 'signal', 'key': 'signal'}
        snapshot = {**self.snapshot, 'windows': [stocks, signal, first, second]}
        stalled, recovered = threading.Event(), threading.Event()
        placed, mutex = set(), threading.Lock()

        def launch(saved):
            if saved['key'] == 'agent-two':
                # Same-class windows stay sequential despite other active workers.
                self.assertIn('agent-one', placed)

        def wait(saved, *_):
            if saved['key'] == 'stocks':
                stalled.set()
                self.assertTrue(recovered.wait(3), 'Other apps waited for the stalled app')
                # Progress must already be durable while Stocks is still pending.
                progress = app.read_json(app.STATE / 'restored-windows.json')['windows']
                self.assertEqual(set(progress), {'signal', 'agent-one', 'agent-two'})
                raise RuntimeError('No matching window appeared within 15 seconds')
            self.assertTrue(stalled.wait(3))
            return {**self.actual, 'class': saved['class'], 'address': 'new-' + saved['key']}, False

        def place(saved, _):
            with mutex:
                placed.add(saved['key'])
                if len(placed) == 3:
                    recovered.set()

        with patch.object(app, 'capture', return_value={'windows': []}), \
             patch.object(app, 'hypr', return_value=[]), patch.object(app, 'launch', side_effect=launch), \
             patch.object(app, 'wait_for_window', side_effect=wait), patch.object(app, 'place', side_effect=place):
            self.assertFalse(app.restore(snapshot))
        result = app.read_json(app.STATE / 'last-result.json')
        self.assertEqual(result['restored'], 3)
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('Stocks: No matching window', result['errors'][0])

    def test_simultaneous_arrivals_keep_all_progress_and_retry_without_duplicates(self):
        windows = [{**self.saved, 'class': f'app-{i}', 'key': f'window-{i}', 'session': f'session-{i}'}
                   for i in range(4)]
        actual = [{**self.actual, 'class': w['class'], 'address': f'new-{i}', 'pid': 100 + i}
                  for i, w in enumerate(windows)]
        barrier = threading.Barrier(len(windows), timeout=3)

        def wait(saved, *_):
            barrier.wait()
            return actual[windows.index(saved)], False

        with patch.object(app, 'capture', side_effect=[{'windows': []}, {'windows': actual}]), \
             patch.object(app, 'hypr', return_value=[]), patch.object(app, 'launch') as launch, \
             patch.object(app, 'wait_for_window', side_effect=wait), patch.object(app, 'place'):
            self.assertTrue(app.restore({**self.snapshot, 'windows': windows}))
            progress = app.read_json(app.STATE / 'restored-windows.json')['windows']
            self.assertEqual(progress, {w['key']: {'address': a['address'], 'pid': a['pid']}
                                        for w, a in zip(windows, actual)})
            self.assertTrue(app.restore({**self.snapshot, 'windows': windows}))
        self.assertEqual(launch.call_count, 4)
        self.assertEqual(app.read_json(app.STATE / 'last-result.json')['already_open'], 4)

    def test_launch_failure_does_not_abort_its_lane_or_other_classes(self):
        broken = {**self.saved, 'key': 'broken'}
        next_window = {**self.saved, 'key': 'next', 'session': 'next-id'}
        other = {**self.saved, 'key': 'other', 'class': 'kitty', 'session': 'other-id'}

        def launch(saved):
            if saved['key'] == 'broken':
                raise RuntimeError('launch rejected')

        def wait(saved, *_):
            return {**self.actual, 'class': saved['class'], 'address': 'new-' + saved['key']}, False

        with patch.object(app, 'capture', return_value={'windows': []}), \
             patch.object(app, 'hypr', return_value=[]), patch.object(app, 'launch', side_effect=launch), \
             patch.object(app, 'wait_for_window', side_effect=wait), patch.object(app, 'place'):
            self.assertFalse(app.restore({**self.snapshot, 'windows': [broken, next_window, other]}))
        result = app.read_json(app.STATE / 'last-result.json')
        self.assertEqual(result['restored'], 2)
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('launch rejected', result['errors'][0])

    def test_tiled_order_is_reconciled_before_an_unrelated_app_times_out(self):
        common = {'floating': False, 'fullscreen': 0, 'size': [100, 100], 'workspace': '1'}
        left = {**self.saved, **common, 'key': 'browser', 'class': 'chrome', 'at': [0, 0]}
        right = {**self.saved, **common, 'key': 'agent', 'class': 'foot', 'at': [110, 0]}
        slow = {**self.saved, 'key': 'stocks', 'class': 'stocks', 'workspace': '4'}
        clients, mutex = [], threading.Lock()
        ordered = threading.Event()

        def wait(saved, *_):
            if saved['key'] == 'stocks':
                self.assertTrue(ordered.wait(3), 'Tile ordering waited for an unrelated application')
                raise RuntimeError('No matching window appeared within 15 seconds')
            actual = {**saved, 'address': 'new-' + saved['key'], 'pid': 100 if saved['key'] == 'browser' else 101,
                      'workspace': {'name': '1'}, 'monitor': 0, 'mapped': True,
                      'at': [110, 0] if saved['key'] == 'browser' else [0, 0]}
            with mutex:
                clients.append(actual)
            return actual, False

        def hypr(_):
            with mutex:
                return list(clients)

        def run(command):
            self.assertEqual(command[:2], ['hyprctl', 'eval'])
            self.assertIn('hl.dsp.window.swap', command[-1])
            ordered.set()
            return 'ok'

        with patch.object(app, 'capture', return_value={'windows': []}), patch.object(app, 'hypr', side_effect=hypr), \
             patch.object(app, 'launch'), patch.object(app, 'wait_for_window', side_effect=wait), \
             patch.object(app, 'place'), patch.object(app, 'run', side_effect=run) as dispatch:
            self.assertFalse(app.restore({**self.snapshot, 'windows': [slow, left, right]}))
        dispatch.assert_called_once()
        self.assertEqual(app.read_json(app.STATE / 'last-result.json')['restored'], 2)


if __name__ == '__main__':
    unittest.main()
