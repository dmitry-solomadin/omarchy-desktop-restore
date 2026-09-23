"""Tiled ordering is independent of arrival order and never refocuses windows."""
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import desktop_restore as app
from tiling import slots, swaps, swap_script, TiledOrder


def saved(key, x, y=0, width=100, height=100):
    return {'key': key, 'workspace': '1', 'floating': False, 'fullscreen': 0,
            'at': [x, y], 'size': [width, height]}


def actual(window, number, **changes):
    return {**window, 'address': f'0x{number}', 'pid': number, 'workspace': {'name': '1'},
            'monitor': 0, 'mapped': True, 'hidden': False, 'grouped': [], **changes}


class TiledOrderTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = saved('browser', 0), saved('agent', 110)
        self.browser = actual(self.left, 1, at=[110, 0])
        self.agent = actual(self.right, 2, at=[0, 0])
        self.hypr = Mock(return_value=[self.browser, self.agent])
        self.run = Mock(return_value='ok')

    def controller(self, live=()):
        return TiledOrder([self.left, self.right], live, self.hypr, self.run, app.lua)

    def test_parallel_arrival_reversal_is_corrected_when_workspace_is_ready(self):
        order = self.controller()
        self.assertIsNone(order.placed(self.right, self.agent, False))
        self.run.assert_not_called()
        self.assertEqual(order.placed(self.left, self.browser, False), {'status': 'restored', 'swaps': 1})
        script = self.run.call_args.args[0][-1]
        self.assertIn('window = "address:0x1"', script)
        self.assertIn('target = "address:0x2"', script)
        self.assertNotIn('hl.dsp.focus', script)
        self.assertIn('no_warps = true', script)
        self.assertIn('no_warps = no_warps', script)
        self.assertIn('pcall(function()', script)
        self.assertIn('w.pid ~= 1', script)
        self.assertIsNone(order.placed(self.right, self.agent, False))
        self.assertEqual(self.run.call_count, 1)

    def test_already_correct_order_needs_no_dispatch(self):
        self.hypr.return_value = [actual(self.left, 1), actual(self.right, 2)]
        order = self.controller()
        order.placed(self.left, self.browser, False)
        self.assertEqual(order.placed(self.right, self.agent, False)['swaps'], 0)
        self.run.assert_not_called()

    def test_existing_window_disables_rearrangement_of_its_workspace(self):
        order = self.controller([self.left])
        self.assertIsNone(order.placed(self.left, self.browser, False))
        self.assertIsNone(order.placed(self.right, self.agent, False))
        self.hypr.assert_not_called()
        self.run.assert_not_called()

    def test_user_workspace_move_during_startup_disables_later_ordering(self):
        order = self.controller()
        self.assertEqual(order.placed(self.left, self.browser, True)['status'], 'skipped')
        self.assertIsNone(order.placed(self.right, self.agent, False))
        self.run.assert_not_called()

    def test_extra_missing_grouped_fullscreen_or_moved_windows_are_not_rearranged(self):
        for clients in ([self.browser],
                        [self.browser, self.agent, actual(saved('third', 220), 3)],
                        [self.browser, {**self.agent, 'grouped': ['0x2', '0x3']}],
                        [self.browser, {**self.agent, 'fullscreen': 2}],
                        [self.browser, {**self.agent, 'workspace': {'name': '2'}}],
                        [self.browser, {**self.agent, 'pid': 99}]):
            with self.subTest(clients=clients):
                self.hypr.return_value = clients
                order = self.controller()
                order.placed(self.left, self.browser, False)
                self.assertEqual(order.placed(self.right, self.agent, False)['status'], 'skipped')
        self.run.assert_not_called()

    def test_atomic_compositor_recheck_can_decline_a_racing_change(self):
        self.run.return_value = 'error: desktop-restore: tiled layout changed'
        order = self.controller()
        order.placed(self.left, self.browser, False)
        self.assertEqual(order.placed(self.right, self.agent, False)['status'], 'skipped')

    def test_actual_dispatch_failure_is_reported(self):
        self.run.return_value = 'error: compositor rejected swap'
        order = self.controller()
        order.placed(self.left, self.browser, False)
        with self.assertRaisesRegex(RuntimeError, 'rejected swap'):
            order.placed(self.right, self.agent, False)

    def test_vertical_order_and_different_monitor_scale(self):
        top, bottom = saved('top', 0), saved('bottom', 0, 110)
        now = [saved('top', 2000, 220, 200, 200), saved('bottom', 2000, 0, 200, 200)]
        self.assertEqual(swaps([top, bottom], now), [('top', 'bottom')])

    def test_three_window_permutation_preserves_existing_split_shape(self):
        layout = [saved('large', 0, 0, 100, 210), saved('upper', 110), saved('lower', 110, 110)]
        now = [{**layout[0], 'key': 'lower'}, {**layout[1], 'key': 'large'}, {**layout[2], 'key': 'upper'}]
        plan = swaps(layout, now)
        self.assertEqual(len(plan), 2)
        for first, second in plan:
            a, b = next(w for w in now if w['key'] == first), next(w for w in now if w['key'] == second)
            a['at'], b['at'] = b['at'], a['at']
            a['size'], b['size'] = b['size'], a['size']
        self.assertEqual(slots(layout), slots(now))

    def test_different_split_shape_or_overlapping_rectangles_are_skipped(self):
        horizontal = [self.left, self.right]
        vertical = [saved('browser', 0), saved('agent', 0, 110)]
        self.assertIsNone(swaps(horizontal, vertical))
        self.assertIsNone(swaps(horizontal, [saved('browser', 0), saved('agent', 50)]))

    def test_float_and_fullscreen_saved_workspaces_do_not_use_tile_ordering(self):
        for change in ({'floating': True}, {'fullscreen': 2}):
            self.left.update(change)
            order = self.controller()
            order.placed(self.left, self.browser, False)
            order.placed(self.right, self.agent, False)
        self.run.assert_not_called()

    @unittest.skipUnless(shutil.which('lua'), 'standalone Lua interpreter not installed')
    def test_cursor_setting_is_restored_even_when_swap_fails(self):
        windows = {'browser': self.browser, 'agent': self.agent}
        script = swap_script('1', windows, [('browser', 'agent')], app.lua)
        harness = '''
            local value = false
            local browser = {pid=1, mapped=true, workspace={name="1"}, fullscreen=0,
                             at={x=110,y=0}, size={x=100,y=100}}
            local agent = {pid=2, mapped=true, workspace={name="1"}, fullscreen=0,
                           at={x=0,y=0}, size={x=100,y=100}}
            hl = {
                get_workspace_windows = function() return {browser,agent} end,
                get_window = function(id) return id == "address:0x1" and browser or agent end,
                get_config = function() return value end,
                config = function(settings) value = settings.cursor.no_warps end,
                dsp = {window={swap=function(args) return args end}},
                dispatch = function() assert(value); return {ok=false,error="simulated"} end
            }
            local ok, err = pcall(function()
        ''' + script + '''
            end)
            assert(not ok and string.find(err, "simulated"))
            assert(value == false, "cursor setting leaked after failed swap")
        '''
        result = subprocess.run([shutil.which('lua'), '-'], input=harness, text=True, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
