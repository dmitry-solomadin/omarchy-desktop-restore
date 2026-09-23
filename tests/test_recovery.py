"""A ten-minute recovery period starts with a new app, not login or idle time."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import desktop_restore as app
import recovery


def window(number=1, **extra):
    return {'address': f'0x{number}', 'stableId': str(number), 'pid': number, 'mapped': True,
            'hidden': False, 'acceptsInput': True, 'class': 'foot', **extra}


class RecoveryTimerTests(unittest.TestCase):
    def setUp(self):
        self.meta = {'recovery_started': False}
        mock = patch.object(recovery, 'clock', return_value=0)
        self.clock = mock.start()
        self.addCleanup(mock.stop)

    def observe(self, *windows):
        return recovery.observe(self.meta, windows, qualifies=lambda *_: True)

    def test_lunch_before_first_window_does_not_use_up_recovery_period(self):
        self.observe()
        self.clock.return_value = 7200
        self.observe()
        self.assertIsNone(recovery.remaining(self.meta))
        self.assertFalse(recovery.due(self.meta))
        self.observe(window())
        self.assertEqual(self.meta['recovery_deadline'], 7800)
        self.clock.return_value = 7799
        self.assertFalse(recovery.due(self.meta))
        self.clock.return_value = 7800
        self.assertTrue(recovery.due(self.meta))

    def test_existing_windows_and_focus_or_title_changes_do_not_start_timer(self):
        self.observe(window())
        self.clock.return_value = 7200
        self.observe(window(title='Changed title', focusHistoryID=0))
        self.assertNotIn('recovery_deadline', self.meta)
        self.observe(window(), window(2))
        self.assertEqual(self.meta['recovery_trigger']['pid'], 2)

    def test_additional_windows_closures_and_watcher_restart_do_not_reset_timer(self):
        self.observe()
        self.clock.return_value = 100
        self.observe(window())
        self.meta = json.loads(json.dumps(self.meta))  # Persistent state after restart.
        self.clock.return_value = 350
        self.observe(window(2))
        self.observe()
        self.assertEqual(self.meta['recovery_deadline'], 700)
        self.assertEqual(recovery.remaining(self.meta), 350)

    def test_restore_finishes_protection_without_starting_another_countdown(self):
        self.observe()
        self.meta['recovery_started'] = True
        self.assertIsNone(self.observe(window()))
        self.assertIsNone(recovery.remaining(self.meta))

    def test_wall_clock_changes_do_not_expire_timer(self):
        self.observe()
        with patch.object(recovery.time, 'time', return_value=100):
            self.observe(window())
        with patch.object(recovery.time, 'time', return_value=9999999999):
            self.clock.return_value = 10
            self.assertEqual(recovery.remaining(self.meta), 590)

    def test_reused_window_address_with_new_stable_id_is_a_new_window(self):
        self.observe(window())
        self.observe(window(stableId='new-surface'))
        self.assertIn('recovery_deadline', self.meta)

    def test_window_seen_before_mapping_can_trigger_when_it_maps(self):
        self.observe()
        qualifies = lambda candidate, _: candidate['mapped']
        recovery.observe(self.meta, [window(mapped=False)], qualifies=qualifies)
        self.assertNotIn('recovery_deadline', self.meta)
        recovery.observe(self.meta, [window()], qualifies=qualifies)
        self.assertEqual(self.meta['recovery_deadline'], 600)


class RecoveryEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        (self.proc / '1').mkdir()
        self.cgroup = self.proc / '1/cgroup'
        self.cgroup.write_text('0::/user.slice/app.slice/app-Hyprland-foot.scope\n')

    def eligible(self, candidate, restored=()):
        return recovery.eligible(candidate, restored, proc_root=self.proc)

    def test_regular_application_can_start_timer(self):
        self.assertTrue(self.eligible(window()))

    def test_identifiable_autostart_service_is_excluded(self):
        self.cgroup.write_text('0::/user.slice/app.slice/app-example@autostart.service/child\n')
        self.assertFalse(self.eligible(window()))

    def test_unmapped_hidden_noninteractive_and_system_windows_are_excluded(self):
        for change in ({'mapped': False}, {'hidden': True}, {'acceptsInput': False}, {'class': ''},
                       {'class': 'org.quickshell'}, {'class': 'org.omarchy.screensaver'},
                       {'class': 'hyprlock'}, {'pid': 0}):
            with self.subTest(change=change):
                self.assertFalse(self.eligible(window(**change)))

    def test_unreadable_or_exited_process_is_excluded(self):
        self.assertFalse(self.eligible(window(2)))

    def test_restore_generated_window_is_excluded(self):
        self.assertFalse(self.eligible(window(), [{'address': '0x1', 'pid': 1}]))
        self.assertTrue(self.eligible(window(), [{'address': '0x1', 'pid': 2}]))


class RecoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        for mock in (patch.object(app, 'STATE', self.state), patch.object(app, 'instance', return_value='current'),
                     patch.object(app, 'report')):
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(recovery, 'clock', return_value=100)
        self.clock = mock.start()
        self.addCleanup(mock.stop)
        self.meta = {'instance': 'current', 'recovery_started': False, 'recovery_policy': recovery.POLICY,
                     'recovery_deadline': 700}
        app.write_json(self.state / 'instance.json', self.meta)
        self.old = self.snapshot('old', 'previous')
        self.new = self.snapshot('new', 'current')
        app.write_json(self.state / 'restore.json', self.old)

    def snapshot(self, key, instance):
        return {'instance': instance, 'saved': 200, 'windows': [
            {'key': key, 'address': key, 'pid': 1, 'title': key, 'kind': 'app', 'class': key,
             'workspace': '1', 'at': [0, 0], 'size': [800, 600], 'floating': False, 'fullscreen': 0,
             'launch': ['example']}]}

    def test_before_deadline_only_rolling_checkpoint_changes(self):
        self.clock.return_value = 699
        app.save(self.new)
        self.assertEqual(app.read_json(self.state / 'restore.json'), self.old)
        self.assertEqual(app.read_json(self.state / 'latest.json'), self.new)

    def test_expiry_adopts_current_desktop_without_retaining_old_retry_entries(self):
        self.old['windows'][0]['pending_restore'] = True
        app.write_json(self.state / 'restore.json', self.old)
        self.clock.return_value = 700
        app.save(self.new)
        self.assertEqual(app.read_json(self.state / 'restore.json'), self.new)
        self.assertEqual(app.read_json(self.state / 'latest.json'), self.new)
        meta = app.read_json(self.state / 'instance.json')
        self.assertEqual(meta['recovery_reason'], 'grace_expired')
        self.assertTrue(meta['recovery_started'])
        app.save(self.new)
        rows = [json.loads(line) for line in (self.state / 'restore-events.jsonl').read_text().splitlines()]
        self.assertEqual([row['event'] for row in rows], ['recovery_timer_expired'])

    def test_expiry_can_adopt_empty_desktop_after_first_window_was_closed(self):
        self.clock.return_value = 700
        empty = {**self.new, 'windows': []}
        app.save(empty)
        self.assertEqual(app.read_json(self.state / 'restore.json'), empty)

    def test_before_deadline_restore_uses_previous_target_and_ends_timer(self):
        self.clock.return_value = 699
        with patch.object(app, 'restore', return_value=True) as restore, patch.object(app, 'capture') as capture:
            self.assertEqual(app.invoke_restore('shortcut'), 0)
        restore.assert_called_once()
        self.assertEqual(restore.call_args.args[0], self.old)
        capture.assert_not_called()
        self.assertIsNone(recovery.remaining(app.read_json(self.state / 'instance.json')))

    def test_restore_at_deadline_does_not_wait_for_watcher_to_expire_target(self):
        self.clock.return_value = 700
        with patch.object(app, 'restore', return_value=True) as restore, patch.object(app, 'capture', return_value=self.new):
            self.assertEqual(app.invoke_restore('shortcut'), 0)
        self.assertEqual(restore.call_args.args[0], self.new)

    def test_watcher_expiry_does_not_wait_for_twenty_seconds_of_stability(self):
        self.clock.return_value = 700
        with patch.object(app, 'WindowEvents') as events, \
             patch.object(app, 'checkpoint_paused', return_value=False), \
             patch.object(app, 'capture', return_value=self.new):
            events.return_value.__enter__.return_value.wait.side_effect = InterruptedError('stop')
            with self.assertRaises(InterruptedError):
                app.watch()
        self.assertEqual(app.read_json(self.state / 'restore.json'), self.new)

    def test_watcher_wakes_at_deadline_if_less_than_ten_seconds_remain(self):
        self.clock.return_value = 697
        with patch.object(app, 'WindowEvents') as events, \
             patch.object(app, 'checkpoint_paused', return_value=False), \
             patch.object(app, 'capture', return_value=self.new):
            wait = events.return_value.__enter__.return_value.wait
            wait.side_effect = InterruptedError('stop')
            with self.assertRaises(InterruptedError):
                app.watch()
        wait.assert_called_once_with(3)

    def test_shutdown_pause_does_not_replace_target_with_teardown(self):
        self.clock.return_value = 700
        with patch.object(app, 'WindowEvents') as events, \
             patch.object(app, 'checkpoint_paused', return_value=True), patch.object(app, 'capture') as capture:
            events.return_value.__enter__.return_value.wait.side_effect = InterruptedError('stop')
            with self.assertRaises(InterruptedError):
                app.watch()
        capture.assert_not_called()
        self.assertEqual(app.read_json(self.state / 'restore.json'), self.old)

    def test_initial_baseline_is_persisted_and_first_new_window_is_logged_once(self):
        self.meta.pop('recovery_deadline')
        app.write_json(self.state / 'instance.json', self.meta)
        with patch.object(recovery, 'eligible', return_value=True):
            app.observe_recovery_windows([window()])
            app.initialize()  # Watcher restart must not clear the baseline.
            app.observe_recovery_windows([window(), window(2)])
            self.clock.return_value = 300
            app.observe_recovery_windows([window(3)])
        meta = app.read_json(self.state / 'instance.json')
        self.assertEqual(meta['recovery_deadline'], 700)
        self.assertEqual(meta['recovery_trigger']['pid'], 2)
        rows = [json.loads(line) for line in (self.state / 'restore-events.jsonl').read_text().splitlines()]
        self.assertEqual([row['event'] for row in rows], ['recovery_timer_started'])
        self.assertEqual(rows[0]['seconds'], 600)

    def test_capture_only_observes_windows_when_requested_by_watcher(self):
        clients = []
        with patch.object(app, 'hypr', side_effect=lambda _: clients), \
             patch.object(app, 'processes', return_value={}), patch.object(app, 'desktop_apps', return_value={}), \
             patch.object(app, 'observe_recovery_windows') as observe:
            app.capture()
            app.capture(fast=True)
            observe.assert_not_called()
            app.capture(track_recovery=True)
        observe.assert_called_once_with(clients)

    def test_idle_reboot_preserves_protected_target_and_resets_observation_state(self):
        app.write_json(self.state / 'latest.json', self.new)
        app.write_json(self.state / 'shutdown.json', {**self.new, 'saved': 300})
        with patch.object(app, 'instance', return_value='next-login'):
            app.initialize()
        self.assertEqual(app.read_json(self.state / 'restore.json'), self.old)
        meta = app.read_json(self.state / 'instance.json')
        self.assertNotIn('recovery_deadline', meta)
        self.assertNotIn('recovery_seen_windows', meta)

    def test_shutdown_after_deadline_promotes_final_snapshot_on_next_login(self):
        self.clock.return_value = 700
        with patch.object(app, 'capture', return_value=copy.deepcopy(self.new)):
            app.save_shutdown()
        final = app.read_json(self.state / 'shutdown.json')
        self.assertTrue(final['recovery_grace_expired'])
        with patch.object(app, 'instance', return_value='next-login'):
            app.initialize()
        self.assertEqual(app.read_json(self.state / 'restore.json'), final)


if __name__ == '__main__':
    unittest.main()
