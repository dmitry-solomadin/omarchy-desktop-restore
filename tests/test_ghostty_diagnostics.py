"""Exercise the native-log wrapper without a compositor or a real crash."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import ghostty_diagnostics as diagnostics


class GhosttyDiagnosticsTests(unittest.TestCase):
    def run_child(self, script):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / 'logs'
        result = subprocess.run([sys.executable, str(Path(diagnostics.__file__)), str(directory),
                                 sys.executable, '-c', script],
                                capture_output=True, text=True, timeout=15)
        return result, directory

    def test_native_stderr_and_clean_exit_are_retained_privately(self):
        result, directory = self.run_child(
            "import os, sys; print('info(termio): initialized', file=sys.stderr); "
            "print(os.environ['GHOSTTY_LOG'], file=sys.stderr)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('info(termio): initialized', (directory / 'runtime.log').read_text())
        self.assertIn('stderr', (directory / 'runtime.log').read_text())
        metadata = json.loads((directory / 'metadata.json').read_text())
        self.assertEqual(metadata['returncode'], 0)
        self.assertIsNone(metadata['signal'])
        self.assertGreater(metadata['pid'], 0)
        self.assertEqual((directory / 'runtime.log').stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / 'metadata.json').stat().st_mode & 0o777, 0o600)

    def test_signal_exit_preserves_last_native_message(self):
        result, directory = self.run_child(
            "import os, signal, sys; print('last I/O message', file=sys.stderr, flush=True); "
            "os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual(result.returncode, 143)
        self.assertIn('last I/O message', (directory / 'runtime.log').read_text())
        metadata = json.loads((directory / 'metadata.json').read_text())
        self.assertEqual(metadata['signal'], 'SIGTERM')
        self.assertEqual(metadata['returncode'], -15)

    def test_bounded_log_retains_recent_output(self):
        stream = io.BytesIO()
        with patch.object(diagnostics, 'LIMIT', 1024):
            for _ in range(100):
                diagnostics.append_bounded(stream, b'old message\n' * 8)
            diagnostics.append_bounded(stream, b'final diagnostic')
        self.assertLessEqual(len(stream.getvalue()), 1024)
        self.assertTrue(stream.getvalue().endswith(b'final diagnostic'))

    def test_unwritable_diagnostics_falls_back_to_original_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / 'not-a-directory'
            file.write_text('occupied')
            result = subprocess.run([sys.executable, str(Path(diagnostics.__file__)), str(file),
                                     sys.executable, '-c', "print('launched')"],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('launched', result.stdout)

    def test_log_open_failure_after_metadata_does_not_block_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'runtime.log').mkdir()
            result = subprocess.run([sys.executable, str(Path(diagnostics.__file__)), str(directory),
                                     sys.executable, '-c', "print('launched')"],
                                    capture_output=True, text=True, timeout=15)
            self.assertTrue((directory / 'metadata.json').exists())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('launched', result.stdout)

    def test_logging_failure_after_child_launch_never_launches_again(self):
        def failed(directory, argv, launched):
            launched.append(123)
            raise OSError('logging failed')
        with patch.object(diagnostics, 'supervise', side_effect=failed), \
             patch.object(diagnostics.os, 'execvp') as launch:
            with self.assertRaises(OSError):
                diagnostics.main(Path('/unused'), ['ghostty'])
        launch.assert_not_called()
