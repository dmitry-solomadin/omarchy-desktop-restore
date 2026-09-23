"""Recovery lifecycle, close-event saves and invocation diagnostics."""
import json
import os
from pathlib import Path
import select
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import desktop_restore as app
import restore_log
from window_events import WindowEvents


class RecoveryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        for mock in (patch.object(app, 'STATE', self.state),
                     patch.object(app, 'instance', return_value='current'), patch.object(app, 'report')):
            mock.start()
            self.addCleanup(mock.stop)
        self.one, self.two = self.window(1), self.window(2)

    def window(self, number):
        return {'key': f'key-{number}', 'address': f'address-{number}', 'pid': number,
                'kind': 'claude', 'session': f'exact-session-{number}', 'class': 'foot',
                'title': f'Conversation {number}', 'workspace': str(number), 'at': [0, 0],
                'size': [800, 600], 'floating': False, 'fullscreen': 0, 'monitor': 'DP-1',
                'cwd': str(self.state), 'argv': ['claude', '--resume', f'exact-session-{number}'],
                'launch': ['foot']}

    def snapshot(self, *windows):
        return {'instance': 'current', 'saved': 10, 'windows': list(windows)}

    def metadata(self, **values):
        app.write_json(self.state / 'instance.json', {'instance': 'current', **values})

    def complete(self, *windows):
        app.write_json(self.state / 'restored-windows.json', {
            'instance': 'current', 'windows': {w['key']: {'address': w['address'], 'pid': w['pid']} for w in windows}})

    def test_close_before_initial_recovery_does_not_erase_preboot_target(self):
        old = {**self.snapshot(self.one, self.two), 'instance': 'previous'}
        app.write_json(self.state / 'restore.json', old)
        self.metadata(recovery_started=False)
        app.save(self.snapshot(self.one), closed=True)
        self.assertEqual(app.read_json(self.state / 'restore.json'), old)
        self.assertEqual(app.read_json(self.state / 'latest.json')['windows'], [self.one])

    def test_after_recovery_target_follows_closures_but_retains_failed_entries(self):
        app.write_json(self.state / 'restore.json', self.snapshot(self.one, self.two))
        self.metadata(recovery_started=True)
        self.complete(self.one)
        live = {**self.one, 'key': 'live-one', 'address': 'new-one', 'pid': 50}
        app.save(self.snapshot(live))
        target = app.read_json(self.state / 'restore.json')
        self.assertEqual([w['key'] for w in target['windows']], ['live-one', self.two['key']])
        self.assertTrue(target['windows'][1]['pending_restore'])
        app.save(self.snapshot(), closed=True)
        self.assertEqual([w['key'] for w in app.read_json(self.state / 'restore.json')['windows']], [self.two['key']])
        self.complete(self.one, self.two)
        app.save(self.snapshot(), closed=True)
        self.assertEqual(app.read_json(self.state / 'restore.json')['windows'], [])
        self.assertEqual(app.read_json(self.state / 'restore.json'), app.read_json(self.state / 'latest.json'))

    def test_completed_window_closed_before_first_sync_is_not_retained_as_failed(self):
        app.write_json(self.state / 'restore.json', self.snapshot(self.one))
        self.metadata(recovery_started=True)
        self.complete(self.one)
        app.save(self.snapshot(), closed=True)
        self.assertEqual(app.read_json(self.state / 'restore.json')['windows'], [])

    def test_closing_last_window_stays_empty_at_next_login(self):
        self.metadata(recovery_started=True, recovery_synced=True)
        app.write_json(self.state / 'restore.json', self.snapshot(self.one))
        app.save(self.snapshot(), closed=True)
        with patch.object(app, 'instance', return_value='next-login'):
            app.initialize()
        self.assertEqual(app.read_json(self.state / 'restore.json')['windows'], [])
        self.assertFalse(app.read_json(self.state / 'instance.json')['recovery_started'])

    def test_upgrade_recognizes_recovery_already_performed_this_login(self):
        self.metadata()
        self.complete(self.one)
        app.initialize()
        self.assertTrue(app.read_json(self.state / 'instance.json')['recovery_started'])

    def test_failed_shared_group_session_remains_a_restorable_pending_entry(self):
        slot = {**self.one, 'kind': 'agent-unresolved', 'agent_group': 'group', 'error': 'ambiguous'}
        group = {'key': 'group', 'pid': 1, 'terminal': 'foot', 'window_keys': [slot['key']], 'errors': [],
                 'sessions': [{**self.one, 'key': 'session-one'}, {**self.two, 'key': 'session-two'}]}
        app.write_json(self.state / 'restore.json', {**self.snapshot(slot), 'agent_groups': [group]})
        self.metadata(recovery_started=True)
        self.complete({**self.one, 'key': 'session-one'})
        app.save(self.snapshot(), closed=True)
        pending, = app.restore_entries(app.read_json(self.state / 'restore.json'))
        self.assertEqual(pending['session'], self.two['session'])
        self.assertTrue(pending['pending_restore'])
        self.assertNotIn('agent_group', pending)

    def test_close_event_saves_without_waiting_for_stability_timer(self):
        self.metadata(recovery_started=True, recovery_synced=True)
        app.write_json(self.state / 'restore.json', self.snapshot(self.one, self.two))
        with patch.object(app, 'WindowEvents') as events, \
             patch.object(app, 'checkpoint_paused', return_value=False), \
             patch.object(app, 'capture', side_effect=[self.snapshot(self.one, self.two), self.snapshot(self.one)]):
            events.return_value.__enter__.return_value.wait.side_effect = [True, InterruptedError('stop')]
            with self.assertRaises(InterruptedError):
                app.watch()
        self.assertEqual(app.read_json(self.state / 'restore.json')['windows'], [self.one])

    def test_shutdown_starting_during_capture_blocks_event_save(self):
        with patch.object(app, 'WindowEvents') as events, \
             patch.object(app, 'checkpoint_paused', side_effect=[False, False, False, True]), \
             patch.object(app, 'capture', return_value=self.snapshot(self.one)), patch.object(app, 'save') as save:
            events.return_value.__enter__.return_value.wait.side_effect = [True, InterruptedError('stop')]
            with self.assertRaises(InterruptedError):
                app.watch()
        save.assert_not_called()

    def test_power_menu_save_protects_teardown_before_logind_flag(self):
        app.write_json(self.state / 'shutdown.json', {**self.snapshot(self.one), 'saved': 100})
        with patch.object(app, 'preparing_for_shutdown', return_value=False), patch.object(app.time, 'time', return_value=105):
            self.assertTrue(app.checkpoint_paused())
        with patch.object(app, 'preparing_for_shutdown', return_value=False), patch.object(app.time, 'time', return_value=131):
            self.assertFalse(app.checkpoint_paused())

    def test_repeated_restore_does_not_reopen_a_completed_then_closed_window(self):
        self.complete(self.one)
        with patch.object(app, 'capture', return_value=self.snapshot()), patch.object(app, 'launch') as launch:
            self.assertTrue(app.restore(self.snapshot(self.one)))
        launch.assert_not_called()
        self.assertEqual(app.read_json(self.state / 'last-result.json')['already_completed'], 1)

    def test_already_open_windows_are_also_marked_completed(self):
        with patch.object(app, 'capture', side_effect=[self.snapshot(self.one), self.snapshot()]), \
             patch.object(app, 'launch') as launch:
            self.assertTrue(app.restore(self.snapshot(self.one)))
            self.assertTrue(app.restore(self.snapshot(self.one)))
        launch.assert_not_called()
        self.assertIn(self.one['key'], app.read_json(self.state / 'restored-windows.json')['windows'])

    def log(self):
        return [json.loads(line) for line in (self.state / 'restore-events.jsonl').read_text().splitlines()]

    def test_restore_invocation_logs_source_caller_checkpoint_and_outcome(self):
        self.metadata(recovery_started=False)
        app.write_json(self.state / 'restore.json', self.snapshot(self.one))
        with patch.object(app, 'restore', return_value=True), \
             patch.object(app.os, 'umask'), \
             patch.object(restore_log, 'caller_chain', return_value=[{'pid': 123, 'name': 'Hyprland'}]), \
             patch.object(sys, 'argv', ['desktop-restore', 'restore', '--source', 'shortcut']):
            self.assertEqual(app.main(), 0)
        rows = self.log()
        self.assertEqual([r['event'] for r in rows], ['invoked', 'started', 'finished'])
        self.assertEqual(rows[0]['source'], 'shortcut')
        self.assertEqual(rows[0]['callers'][0]['name'], 'Hyprland')
        self.assertEqual(rows[1]['checkpoint_saved'], 10)
        self.assertEqual(len({r['run_id'] for r in rows}), 1)
        self.assertEqual((self.state / 'restore-events.jsonl').stat().st_mode & 0o777, 0o600)
        self.assertTrue(app.read_json(self.state / 'instance.json')['recovery_started'])

    def test_cli_and_lock_contention_are_logged(self):
        self.metadata(recovery_started=False)
        with app.lock('restore'), self.assertRaises(BlockingIOError):
            app.invoke_restore('cli')
        self.assertEqual([r['event'] for r in self.log()], ['invoked', 'busy'])
        self.assertEqual(self.log()[0]['source'], 'cli')
        self.assertTrue(all('argv' not in p for p in self.log()[0]['callers']))

    def test_missing_checkpoint_failure_is_logged(self):
        self.metadata(recovery_started=False)
        with self.assertRaisesRegex(RuntimeError, 'No automatic desktop checkpoint'):
            app.invoke_restore('cli')
        self.assertEqual([r['event'] for r in self.log()], ['invoked', 'failed'])


class WindowEventsTests(unittest.TestCase):
    def setUp(self):
        self.events = WindowEvents()
        self.events.socket, self.writer = socket.socketpair()
        self.events.socket.setblocking(False)
        self.addCleanup(self.events.disconnect)
        self.addCleanup(self.writer.close)

    def test_fragmented_close_events_and_unrelated_events(self):
        self.writer.sendall(b'activewindow>>example,title\nclosewin')
        self.assertFalse(self.events.wait(0))
        self.writer.sendall(b'dow>>abc123\n')
        self.assertTrue(self.events.wait(0))
        self.assertFalse(self.events.wait(0))

    def test_disconnect_requests_reconciliation(self):
        self.writer.close()
        self.assertTrue(self.events.wait(0))
        self.assertIsNone(self.events.socket)

    def test_open_event_wakes_census_without_reporting_a_close(self):
        self.writer.sendall(b'openwindow>>abc123,1,foot,Terminal\n')
        with patch('window_events.select.select', wraps=select.select) as poll:
            self.assertFalse(self.events.wait(10))
        self.assertEqual(poll.call_count, 1)

    def test_unavailable_socket_keeps_periodic_polling(self):
        self.events.disconnect()
        with patch.object(self.events, 'connect'), patch('window_events.time.sleep') as sleep:
            self.assertFalse(self.events.wait(10))
        sleep.assert_called_once_with(10)

    def test_successful_reconnect_requests_reconciliation(self):
        reader, writer = socket.socketpair()
        self.addCleanup(writer.close)
        self.events.disconnect()
        with patch.object(self.events, 'connect', side_effect=lambda: setattr(self.events, 'socket', reader)):
            self.assertTrue(self.events.wait(10))


if __name__ == '__main__':
    unittest.main()
