"""Bounded crash collection without crashing a process or touching real cores."""
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import ghostty_crash as crash


class GhosttyCrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'attempt'
        self.directory.mkdir()
        self.metadata = {'pid': 321, 'boot_id': '1234-5678', 'executable': '/usr/bin/ghostty',
                         'started': '1970-01-01T00:16:40.250000+00:00',
                         'exited': '1970-01-01T00:16:42.500000+00:00', 'returncode': -11}
        self.core = {'COREDUMP_PID': '321', 'COREDUMP_TIMESTAMP': '1000000000',
                     'COREDUMP_EXE': '/usr/bin/ghostty', '_BOOT_ID': '12345678'}

    def test_exact_executable_is_retained_once_and_survives_an_upgrade(self):
        executable = self.root / 'installed-ghostty'
        executable.write_bytes(b'original executable')
        first = crash.archive_executable(executable, self.directory)
        archive = Path(first['archived_executable'])
        self.assertEqual(archive.read_bytes(), b'original executable')
        inode = archive.stat().st_ino
        self.assertEqual(crash.archive_executable(executable, self.directory), first)
        self.assertEqual(archive.stat().st_ino, inode)
        executable.write_bytes(b'upgraded executable')
        second = crash.archive_executable(executable, self.directory)
        self.assertNotEqual(first['executable_sha256'], second['executable_sha256'])
        self.assertEqual(archive.read_bytes(), b'original executable')
        self.assertEqual(Path(second['archived_executable']).read_bytes(), b'upgraded executable')
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_normal_exit_and_user_signals_never_query_cores_or_launch_debugger(self):
        with patch.object(crash.shutil, 'which') as which, \
             patch.object(crash, 'core_dump') as core, patch.object(crash, 'debugger_report') as report:
            for code in (0, 1, 127, -signal.SIGTERM, -signal.SIGHUP, -signal.SIGINT):
                self.assertIsNone(crash.collect(self.directory, {**self.metadata, 'returncode': code}))
        which.assert_not_called()
        core.assert_not_called()
        report.assert_not_called()

    def test_missing_tools_are_reported_without_waiting_for_a_core(self):
        with patch.object(crash.shutil, 'which', return_value=None), patch.object(crash, 'core_dump') as core:
            result = crash.collect(self.directory, self.metadata)
        self.assertEqual(result['status'], 'tools_missing')
        self.assertEqual(result['tools'], ['coredumpctl', 'gdb'])
        core.assert_not_called()

    def test_delayed_core_and_subsecond_start_are_matched_to_this_crash(self):
        with patch.object(crash.shutil, 'which', return_value='/usr/bin/tool'), \
             patch.object(crash, 'core_dump', side_effect=[None, self.core]) as core, \
             patch.object(crash.time, 'sleep'), \
             patch.object(crash, 'debugger_report', return_value={'status': 'collected'}) as report:
            result = crash.collect(self.directory, self.metadata)
        self.assertEqual(result['status'], 'collected')
        self.assertEqual(result['core'], self.core)
        self.assertEqual(core.call_count, 2)
        command = report.call_args.args[0]
        self.assertIn('COREDUMP_PID=321', command)
        self.assertIn('_BOOT_ID=12345678', command)
        self.assertIn('COREDUMP_TIMESTAMP=1000000000', command)

    def test_wrong_boot_executable_or_time_is_not_attached_to_the_crash(self):
        for change in ({'_BOOT_ID': 'other'}, {'COREDUMP_EXE': '/bin/other'},
                       {'COREDUMP_TIMESTAMP': '999000000'}, {'COREDUMP_TIMESTAMP': '1003000000'}):
            with self.subTest(change=change), patch.object(crash.shutil, 'which', return_value='/bin/tool'), \
                 patch.object(crash, 'CORE_WAIT', 0), \
                 patch.object(crash, 'core_dump', return_value={**self.core, **change}), \
                 patch.object(crash, 'debugger_report') as report:
                self.assertEqual(crash.collect(self.directory, self.metadata)['status'], 'core_unavailable')
                report.assert_not_called()

    def test_debugger_uses_archived_executable_and_disables_scripts_and_argument_values(self):
        metadata = {**self.metadata, 'archived_executable': '/state with spaces/binaries/ghostty'}
        command = crash.debugger_command(metadata, self.core)
        arguments = shlex.split(next(arg.split('=', 1)[1] for arg in command
                                    if arg.startswith('--debugger-arguments=')))
        self.assertIn('--nx', arguments)
        self.assertIn('set auto-load off', arguments)
        self.assertIn('set print frame-arguments none', arguments)
        self.assertIn('file "/state with spaces/binaries/ghostty"', arguments)
        self.assertIn('thread apply all bt 24', arguments)
        self.assertNotIn('bt full', arguments)

    def test_report_discards_startup_banner_handles_split_marker_and_caps_output(self):
        path = self.directory / 'crash.txt'
        script = ("import os, time; os.write(1, b'Core was generated by private arguments\\n'); "
                  f"os.write(1, {crash.MARKER[:9]!r}); time.sleep(0.05); "
                  f"os.write(1, {crash.MARKER[9:]!r} + b'frame data\\n' * 1000)")
        with patch.object(crash, 'REPORT_LIMIT', 1024):
            result = crash.debugger_report([sys.executable, '-c', script], path, timeout=5)
        self.assertEqual(result['status'], 'collected')
        self.assertEqual(result['debugger_returncode'], 0)
        self.assertTrue(result['truncated'])
        self.assertEqual(path.stat().st_size, 1024)
        self.assertNotIn(b'private arguments', path.read_bytes())
        self.assertTrue(path.read_bytes().startswith(b'frame data\n'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_debugger_failure_does_not_save_potentially_private_startup_errors(self):
        path = self.directory / 'crash.txt'
        result = crash.debugger_report([sys.executable, '-c',
                                        "print('private invocation'); raise SystemExit(1)"], path, timeout=5)
        self.assertEqual(result['status'], 'debugger_failed')
        self.assertEqual(result['debugger_returncode'], 1)
        self.assertEqual(path.read_bytes(), b'')

    def test_hung_debugger_is_terminated_and_partial_evidence_is_kept(self):
        path = self.directory / 'crash.txt'
        pidfile = self.directory / 'pid.json'
        script = ("import json, os, time; "
                  f"open({str(pidfile)!r}, 'w').write(json.dumps(os.getpid())); "
                  f"os.write(1, {crash.MARKER!r} + b'partial stack\\n'); time.sleep(60)")
        result = crash.debugger_report([sys.executable, '-c', script], path, timeout=0.5)
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(path.read_bytes(), b'partial stack\n')
        with self.assertRaises(ProcessLookupError):
            os.kill(json.loads(pidfile.read_text()), 0)
