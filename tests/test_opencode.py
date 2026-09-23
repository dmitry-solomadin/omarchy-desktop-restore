import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import agents
import desktop_restore as app


class OpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.client = {'pid': 10, 'class': 'foot', 'title': 'OC | Same title', 'address': 'test',
                       'workspace': {'id': 3, 'name': '3'}, 'at': [0, 0], 'size': [500, 500],
                       'floating': False, 'fullscreen': 0, 'mapped': True, 'monitor': 0}
        self.procs = {10: {'parent': 1, 'cmd': ['foot'], 'cwd': self.temp.name, 'tty': 0},
                      11: {'parent': 10, 'cmd': ['zsh'], 'cwd': self.temp.name, 'tty': 1,
                           'pgrp': 11, 'tpgid': 12},
                      12: {'parent': 11, 'cmd': ['opencode'], 'cwd': self.temp.name, 'tty': 1,
                           'pgrp': 12, 'tpgid': 12}}
        self.v1 = [{'id': 'ses_exact_v1', 'title': 'Same title', 'location': {'directory': self.temp.name}}]
        self.v2 = [{'id': 'ses_exact_v2', 'title': 'Same title', 'location': {'directory': self.temp.name}}]
        for mock in (patch.object(app, 'STATE', self.state),
                     patch.object(app, 'instance', return_value='test'),
                     patch.object(app, 'hypr', side_effect=lambda query:
                                  [self.client] if query == 'clients' else [{'id': 0, 'name': 'DP-1'}]),
                     patch.object(app, 'processes', return_value=self.procs),
                     patch.object(app, 'desktop_apps', return_value={}),
                     patch.object(agents, 'process_env', return_value={})):
            mock.start()
            self.addCleanup(mock.stop)

    def capture(self, fast=False):
        return app.capture(fast=fast)['windows'][0]

    def test_versions_have_separate_metadata_commands_and_identities(self):
        with patch.object(app, 'sessions_v1', return_value=self.v1) as v1, \
             patch.object(app, 'sessions', return_value=self.v2) as v2:
            first = self.capture()
            v1.assert_called_once()
            v2.assert_not_called()
            self.procs[12]['cmd'] = ['opencode2', '--auto']
            second = self.capture()
        self.assertEqual(first['kind'], 'opencode1')
        self.assertEqual(first['argv'], ['opencode', '--session', 'ses_exact_v1', self.temp.name])
        self.assertEqual(second['argv'], ['opencode2', '--auto', '--session', 'ses_exact_v2', self.temp.name])
        self.assertNotEqual(app.identity(first), app.identity({**second, 'session': first['session']}))
        self.assertEqual(len(list(self.state.glob('sessions*.json'))), 2)

    def test_shutdown_uses_only_the_correct_version_cache(self):
        app.write_json(self.state / 'sessions-cache.json', self.v2)
        with patch.object(app, 'sessions_v1', return_value=self.v1):
            saved = self.capture()
        with patch.object(app, 'run', side_effect=AssertionError('no CLI at shutdown')), \
             patch.object(app, 'sessions_v1', side_effect=AssertionError('no discovery at shutdown')), \
             patch.object(app, 'sessions', side_effect=AssertionError('no API at shutdown')):
            self.assertEqual(self.capture(fast=True)['session'], saved['session'])
            self.procs[12]['cmd'] = ['opencode2']
            self.assertEqual(self.capture(fast=True)['session'], 'ses_exact_v2')

    def test_missing_v1_cache_does_not_fall_back_to_v2(self):
        app.write_json(self.state / 'sessions-cache.json', self.v2)
        saved = self.capture(fast=True)
        self.assertIn('error', saved)
        self.assertEqual(saved['kind'], 'agent-unresolved')
        self.assertNotIn('launch', saved)

    def test_untitled_v2_session_does_not_block_other_conversations(self):
        self.procs[12]['cmd'] = ['opencode2']
        untitled = {'id': 'ses_untitled', 'location': {'directory': self.temp.name}}
        with patch.object(app, 'sessions', return_value=[untitled, *self.v2]):
            self.assertEqual(self.capture()['session'], 'ses_exact_v2')
        self.assertEqual(app.read_json(self.state / 'sessions-cache.json'), self.v2)
        self.assertEqual(self.capture(fast=True)['session'], 'ses_exact_v2')

    def test_cli_metadata_is_normalized_and_version_checked(self):
        raw = [{'id': 'ses_exact_v1', 'title': 'Same title', 'directory': self.temp.name,
                'updated': 123, 'projectId': 'project'}]
        env = {'XDG_DATA_HOME': '/data with spaces'}
        with patch.object(app, 'run', side_effect=['1.18.31', json.dumps(raw)]) as run:
            self.assertEqual(app.sessions_v1(self.temp.name, env), self.v1)
        self.assertEqual(run.call_args.args[0], ['env', 'XDG_DATA_HOME=/data with spaces',
                         'opencode', 'session', 'list', '--format', 'json', '--max-count', '10000'])
        self.assertEqual(run.call_args.kwargs['cwd'], self.temp.name)
        with patch.object(app, 'run', return_value='2.0.0') as run:
            with self.assertRaisesRegex(ValueError, 'version 1'):
                app.sessions_v1(self.temp.name, {})
            self.assertEqual(run.call_count, 1)

    def test_empty_malformed_and_incomplete_listings(self):
        with patch.object(app, 'run', side_effect=['1.18.31', '']):
            self.assertEqual(app.sessions_v1(self.temp.name, {}), [])
        for data in ({}, [{'id': 'bad', 'title': 'x', 'directory': 'relative'}], [{}] * 10000):
            with self.subTest(data_type=type(data)), patch.object(app, 'run', side_effect=['1.18.31', json.dumps(data)]):
                with self.assertRaises(ValueError):
                    app.sessions_v1(self.temp.name, {})

    def test_ambiguous_titles_and_stale_session_argv_are_not_used(self):
        self.procs[12]['cmd'] = ['opencode', '--session', 'ses_old_session']
        with patch.object(app, 'sessions_v1', return_value=self.v1):
            self.assertEqual(self.capture()['session'], 'ses_exact_v1')
        with patch.object(app, 'sessions_v1', return_value=[*self.v1, {**self.v1[0], 'id': 'ses_duplicate'}]):
            self.assertIn('error', self.capture())
        self.client['title'] = 'OC | Same…'
        with patch.object(app, 'sessions_v1', return_value=self.v1):
            self.assertEqual(self.capture()['session'], 'ses_exact_v1')

    def test_v1_home_or_untitled_session_is_skipped(self):
        self.client['title'] = 'OpenCode'
        with patch.object(app, 'sessions_v1', side_effect=AssertionError('unnecessary query')):
            saved = self.capture()
        self.assertEqual(saved['kind'], 'agent-unresolved')
        self.assertIn('uniquely named', saved['error'])

    def test_failure_in_one_version_does_not_poison_the_other(self):
        with patch.object(app, 'sessions_v1', side_effect=RuntimeError('V1 configuration invalid')), \
             patch.object(app, 'sessions', return_value=self.v2):
            self.assertIn('configuration invalid', self.capture()['error'])
            self.procs[12]['cmd'] = ['opencode2']
            self.assertEqual(self.capture()['session'], 'ses_exact_v2')

    def test_custom_v1_data_paths_are_preserved_and_caches_isolated(self):
        env = {'XDG_DATA_HOME': '/custom/data', 'OPENCODE_CONFIG_DIR': '/custom/config'}
        with patch.object(agents, 'process_env', return_value=env), \
             patch.object(app, 'sessions_v1', return_value=self.v1) as query:
            saved = self.capture()
            query.assert_called_once_with(self.temp.name, env)
            self.assertEqual(saved['agent_env'], env)
            self.assertEqual(saved['argv'][:3], ['env', 'OPENCODE_CONFIG_DIR=/custom/config',
                                               'XDG_DATA_HOME=/custom/data'])
            self.assertNotEqual(app.identity(saved), app.identity({**saved, 'agent_env': {}}))
        self.assertIn('error', self.capture(fast=True))

    def test_v2_custom_environment_is_used_for_queries_cache_and_matching(self):
        self.procs[12]['cmd'] = ['opencode2']
        env = {'XDG_DATA_HOME': '/different-opencode-data'}
        with patch.object(agents, 'process_env', return_value=env), \
             patch.object(app, 'sessions', return_value=self.v2) as query:
            saved = self.capture()
            query.assert_called_once_with(env)
            self.assertEqual(self.capture(fast=True)['session'], 'ses_exact_v2')
        self.assertIn('error', self.capture(fast=True))
        self.assertNotEqual(app.identity(saved), app.identity({**saved, 'agent_env': {}}))
        self.assertFalse((self.state / 'sessions-cache.json').exists())
        with patch.object(app, 'run', return_value='{"data":[],"cursor":{}}') as run:
            app.sessions(env)
        self.assertEqual(run.call_args.args[0][:3], ['env', 'XDG_DATA_HOME=/different-opencode-data', 'opencode2'])

    def test_default_v2_environment_matches_older_checkpoints_and_cache(self):
        self.procs[12]['cmd'] = ['opencode2']
        env = {'XDG_DATA_HOME': os.environ.get('XDG_DATA_HOME', str(app.HOME / '.local/share'))}
        app.write_json(self.state / 'sessions-cache.json', self.v2)
        with patch.object(agents, 'process_env', return_value=env):
            saved = self.capture(fast=True)
        self.assertEqual(saved['session'], 'ses_exact_v2')
        self.assertEqual(app.identity(saved), app.identity({**saved, 'agent_env': {}}))

    def test_malformed_metadata_is_a_skipped_window_not_a_watcher_failure(self):
        self.procs[12]['cmd'] = ['opencode2']
        for malformed in ({'data': []}, [{'title': None}], ['invalid']):
            app.write_json(self.state / 'sessions-cache.json', malformed)
            saved = self.capture(fast=True)
            self.assertEqual(saved['kind'], 'agent-unresolved')
            self.assertIn('Invalid OpenCode', saved['error'])

    def test_background_jobs_private_ptys_and_attach_are_not_resumed(self):
        for change in ({'pgrp': 99}, {'tty': 2}, {'cmd': ['opencode', '--log-level', 'INFO', 'run', 'prompt']},
                       {'cmd': ['opencode', 'attach', 'http://localhost:1234']},
                       {'cmd': ['opencode2', '--standalone']},
                       {'cmd': ['opencode2', '--server=http://localhost:1234']}):
            with self.subTest(change=change), patch.dict(self.procs[12], change):
                self.assertIn('error', self.capture(fast=True))

    def test_both_versions_can_be_captured_together_with_the_same_title(self):
        other = {**self.client, 'pid': 20, 'address': 'other'}
        self.procs[20] = {**self.procs[10]}
        self.procs[21] = {**self.procs[12], 'parent': 20, 'cmd': ['opencode2'], 'tty': 2}
        with patch.object(app, 'hypr', side_effect=lambda query:
                          [self.client, other] if query == 'clients' else [{'id': 0, 'name': 'DP-1'}]), \
             patch.object(app, 'sessions_v1', return_value=self.v1), \
             patch.object(app, 'sessions', return_value=self.v2):
            saved = app.capture()['windows']
        self.assertEqual([(w['kind'], w['session']) for w in saved],
                         [('opencode1', 'ses_exact_v1'), ('opencode', 'ses_exact_v2')])

    def test_npm_wrapper_and_native_child_are_one_agent(self):
        self.procs[12]['cmd'] = ['node', '/usr/lib/node_modules/opencode-ai/bin/opencode']
        self.procs[13] = {**self.procs[12], 'parent': 12, 'cmd': ['/opt/opencode/bin/opencode']}
        with patch.object(app, 'sessions_v1', return_value=self.v1):
            self.assertEqual(self.capture()['session'], 'ses_exact_v1')

    def test_unmapped_shared_terminal_versions_are_not_guessed(self):
        other = {**self.client, 'address': 'other'}
        self.procs[20] = {**self.procs[12], 'parent': 10, 'cmd': ['opencode2'], 'tty': 2}
        with self.assertRaisesRegex(ValueError, 'shared-process'):
            agents.terminal_agent(self.client, self.procs, self.state, shared=True,
                                  siblings=[self.client, other])


if __name__ == '__main__':
    unittest.main()
