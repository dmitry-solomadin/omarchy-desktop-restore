"""Post-launch failures, delayed core reports and process identity reuse."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import restore_diagnostics as diagnostics


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.proc = {'start_ticks': '123', 'state': 'S', 'executable': '/usr/bin/ghostty'}
        self.window = {'address': '0xabc', 'pid': 321,
                       'diagnostic': {'id': 'window-test', 'started': 100, 'wall_time': 1000,
                                      'kind': 'herdr', 'window_class': 'ghostty',
                                      'workspace': '2', 'process': self.proc}}
        self.mapping = {'windows': {'test': self.window}}
        self.observer = diagnostics.Observer(Path('/unused'))
        self.clock = patch.object(diagnostics.time, 'monotonic', return_value=101).start()
        self.process = patch.object(diagnostics, 'process', return_value=self.proc).start()
        self.core = patch.object(diagnostics, 'core_dump', return_value=None).start()
        self.log = patch.object(diagnostics.restore_log, 'record').start()
        self.addCleanup(patch.stopall)

    def events(self, name):
        return [call.kwargs for call in self.log.call_args_list if call.args[2] == name]

    def test_crash_before_first_watcher_census_and_delayed_core(self):
        self.process.return_value = None
        self.observer.observe(self.mapping, [])
        self.assertFalse(self.events('restored_window_observed')[0]['process_alive'])
        self.assertFalse(self.events('restored_process_coredump'))
        self.clock.return_value = 111
        self.core.return_value = {'COREDUMP_SIGNAL_NAME': 'SIGSEGV'}
        self.observer.observe(self.mapping, [])
        self.observer.observe(self.mapping, [])
        self.assertEqual(len(self.events('restored_process_coredump')), 1)
        self.assertEqual(self.core.call_count, 2)

    def test_shared_process_can_survive_window_close(self):
        self.observer.observe(self.mapping, [self.window])
        self.observer.observe(self.mapping, [])
        events = self.events('restored_window_observed')
        self.assertEqual(len(events), 2)
        self.assertFalse(events[-1]['window_present'])
        self.assertTrue(events[-1]['process_alive'])
        self.assertFalse(self.events('restored_process_coredump'))

    def test_reused_pid_is_not_reported_as_surviving_process(self):
        self.process.return_value = {**self.proc, 'start_ticks': '999'}
        self.observer.observe(self.mapping, [])
        self.assertFalse(self.events('restored_window_observed')[0]['process_alive'])

    def test_core_grace_and_expiration_bound_work(self):
        self.clock.return_value = 219
        self.process.return_value = None
        self.observer.observe(self.mapping, [])
        self.clock.return_value = 230
        self.core.return_value = {'COREDUMP_SIGNAL': '11'}
        self.observer.observe(self.mapping, [])
        self.assertEqual(len(self.events('restored_process_coredump')), 1)
        self.clock.return_value = 251
        self.process.reset_mock()
        self.core.reset_mock()
        self.observer.observe(self.mapping, [])
        self.process.assert_not_called()
        self.core.assert_not_called()
        self.assertFalse(self.observer.observations)

    def test_legacy_mapping_needs_no_diagnostics(self):
        self.observer.observe({'windows': {'old': {'address': 'a', 'pid': 2}}}, [])
        self.process.assert_not_called()
        self.log.assert_not_called()

    def test_poll_only_queries_compositor_during_observation_period(self):
        clients = Mock(return_value=[self.window])
        self.observer.poll(self.mapping, clients)
        clients.assert_called_once()
        clients.reset_mock()
        self.clock.return_value = 251
        self.observer.poll(self.mapping, clients)
        self.observer.poll({'windows': {}}, clients)
        clients.assert_not_called()
        self.assertFalse(self.observer.observations)


class CoreMetadataTests(unittest.TestCase):
    def test_journal_query_is_boot_and_time_scoped_and_excludes_sensitive_fields(self):
        row = {'COREDUMP_PID': '321', 'COREDUMP_SIGNAL_NAME': 'SIGSEGV',
               'MESSAGE': 'private stack', 'COREDUMP_CMDLINE': 'private arguments',
               'COREDUMP_ENVIRON': 'SECRET=value'}
        with patch.object(diagnostics.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(row), '')) as run:
            result = diagnostics.core_dump(321, 1000)
        self.assertEqual(result, {'COREDUMP_PID': '321', 'COREDUMP_SIGNAL_NAME': 'SIGSEGV'})
        args = run.call_args.args[0]
        self.assertIn('-b', args)
        self.assertIn('@1000.000000', args)
        self.assertIn('COREDUMP_PID=321', args)

    def test_journal_timeout_is_advisory(self):
        with patch.object(diagnostics.subprocess, 'run', side_effect=subprocess.TimeoutExpired('journalctl', 2)):
            self.assertIsNone(diagnostics.core_dump(321, 1000))
