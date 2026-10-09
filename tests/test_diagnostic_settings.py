"""Diagnostics are opt-in and do not add watcher or launcher work by default."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import desktop_restore as app
import restore_log


class DiagnosticSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        restore_log.enabled.cache_clear()
        self.addCleanup(restore_log.enabled.cache_clear)
        self.settings = self.root / 'desktop-restore/settings.json'

    def configure(self, text):
        self.settings.parent.mkdir(exist_ok=True)
        self.settings.write_text(text)
        restore_log.enabled.cache_clear()

    def test_absent_setting_disables_logging_and_caller_inspection(self):
        self.assertFalse(restore_log.enabled())
        state = self.root / 'state'
        with patch.object(restore_log.os, 'getppid') as parent:
            self.assertEqual(restore_log.caller_chain(), [])
        parent.assert_not_called()
        restore_log.record(state, 'test', 'invoked')
        self.assertFalse(state.exists())

    def test_only_explicit_json_true_enables_diagnostics(self):
        for text in ('{}', '{"diagnostics": false}', '{"diagnostics": "true"}',
                     '{"diagnostics": 1}', '[]', 'null', 'invalid'):
            with self.subTest(text=text):
                self.configure(text)
                self.assertFalse(restore_log.enabled())
        self.configure('{"diagnostics": true}')
        self.assertTrue(restore_log.enabled())
        state = self.root / 'state'
        restore_log.record(state, 'test', 'invoked')
        self.assertEqual(json.loads((state / 'restore-events.jsonl').read_text())['event'], 'invoked')

    def test_ghostty_supervisor_is_only_used_when_enabled(self):
        saved = {'kind': 'herdr', 'class': 'ghostty', 'workspace': '2', 'launch': ['ghostty']}
        for enabled in (False, True):
            self.configure(json.dumps({'diagnostics': enabled}))
            with patch.object(app, 'hypr', return_value=[]), \
                 patch.object(app, 'arm_mapping_rule'), patch.object(app, 'run', return_value='ok') as run:
                result = app.launch(saved)
            self.assertEqual('ghostty_diagnostics.py' in run.call_args.args[0][-1], enabled)
            self.assertEqual(result is not None, enabled)

    def test_disabled_watcher_does_not_construct_observer_or_query_compositor(self):
        with patch.object(app, 'STATE', self.root / 'state'), patch.object(app, 'lock'), \
             patch.object(app, 'WindowEvents') as events, \
             patch.object(app, 'checkpoint_paused', return_value=True), \
             patch.object(app.restore_diagnostics, 'Observer') as observer, \
             patch.object(app, 'hypr') as hypr, patch.object(app, 'instance', return_value='test'):
            events.return_value.__enter__.return_value.wait.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                app.watch()
        observer.assert_not_called()
        hypr.assert_not_called()
