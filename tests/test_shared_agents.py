"""Group fallback preserves conversations without inventing window associations."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import agents
import desktop_restore as app
from shared_agents import restore_entries


class SharedAgentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        (self.state / 'agents').mkdir()
        self.windows = []
        self.procs = {10: self.proc(1, ['ghostty'], 0)}
        self.add_window(1, ['claude', '--permission-mode', 'plan'])
        self.add_window(2, ['codex', '--profile', 'work'])
        self.hook(21, 'claude', 'claude-exact-123')
        self.hook(22, 'codex', 'codex-exact-123')
        for mock in (patch.object(app, 'STATE', self.state),
                     patch.object(app, 'instance', return_value='login-one'),
                     patch.object(app, 'hypr', side_effect=lambda what: copy.deepcopy(self.windows)
                                  if what == 'clients' else [{'id': 0, 'name': 'DP-1'}]),
                     patch.object(app, 'processes', side_effect=lambda: copy.deepcopy(self.procs)),
                     patch.object(app, 'desktop_apps', return_value={}),
                     patch.object(agents, 'process_env', return_value={}),
                     patch.object(app, 'report')):
            mock.start()
            self.addCleanup(mock.stop)

    def proc(self, parent, cmd, tty):
        return {'parent': parent, 'cmd': cmd, 'tty': tty, 'start': '100',
                'cwd': str(self.state), 'pgrp': 5, 'tpgid': 5}

    def add_window(self, index, command, title='Agent'):
        self.windows.append({'pid': 10, 'class': 'com.mitchellh.ghostty', 'title': title,
                             'address': f'0x{index}', 'workspace': {'id': index, 'name': str(index)},
                             'at': [0, 0], 'size': [500, 500], 'floating': False,
                             'fullscreen': 0, 'mapped': True, 'monitor': 0})
        self.procs[10 + index] = self.proc(10, ['zsh'], index)
        self.procs[20 + index] = self.proc(10 + index, command, index)

    def hook(self, pid, kind, session, **extra):
        data = {'kind': kind, 'pid': pid, 'start': self.procs[pid]['start'], 'boot': agents.BOOT_ID,
                'cwd': str(self.state), 'session': session, **extra}
        (self.state / 'agents' / f'{kind}-{pid}.json').write_text(json.dumps(data))

    def oc(self, index, binary='opencode2', title=None, args=()):
        self.procs[20 + index]['cmd'] = [binary, *args]
        self.windows[index - 1]['title'] = 'OC | ' + (title or f'Conversation {index}')

    def sessions(self, *indices):
        return [{'id': f'ses_exact_{i}', 'title': f'Conversation {i}',
                 'location': {'directory': str(self.state)}} for i in indices]

    def second_group(self):
        self.windows.extend([{**w, 'pid': 110, 'address': w['address'] + '-second'} for w in self.windows])
        self.procs.update({pid + 100: {**p, 'parent': p['parent'] + 100 if pid != 10 else 1,
                                      'tty': p['tty'] + 100 if p['tty'] else 0}
                           for pid, p in list(self.procs.items())})
        self.hook(121, 'claude', 'claude-exact-123')
        self.hook(122, 'codex', 'codex-exact-123')

    def test_two_native_agents_with_identical_titles_and_directories_are_preserved(self):
        snapshot = app.capture()
        group, = snapshot['agent_groups']
        self.assertEqual(group['errors'], [])
        self.assertEqual({s['session'] for s in group['sessions']}, {'claude-exact-123', 'codex-exact-123'})
        entries = restore_entries(snapshot)
        self.assertEqual([e['placement'] for e in entries], ['approximate', 'approximate'])
        self.assertEqual([e['workspace'] for e in entries], ['1', '2'])
        self.assertEqual(entries[0]['argv'][-2:], ['--permission-mode', 'plan'])
        self.assertEqual(entries[1]['argv'][-2:], ['--profile', 'work'])
        self.assertTrue(all(e['address'].startswith('group-session:') for e in entries))
        self.assertTrue(all('--gtk-single-instance=false' in e['launch'] for e in entries))

    def test_reliable_window_mapping_does_not_use_group_fallback(self):
        for i, window in enumerate(self.windows, 1):
            path = self.state / str(i)
            path.mkdir()
            window['title'] = str(path)
            self.procs[10 + i]['cwd'] = str(path)
        with patch.object(app, 'capture_group', side_effect=AssertionError('unexpected fallback')):
            snapshot = app.capture()
        self.assertNotIn('agent_groups', snapshot)
        self.assertEqual([w['kind'] for w in snapshot['windows']], ['claude', 'codex'])
        self.assertTrue(all('placement' not in w for w in snapshot['windows']))

    def test_known_window_session_is_not_recaptured_by_group(self):
        self.add_window(3, ['claude', '--permission-mode', 'plan'])
        self.hook(23, 'claude', 'claude-exact-123')
        path = self.state / 'unique'
        path.mkdir()
        self.windows[0]['title'] = str(path)
        self.procs[11]['cwd'] = str(path)
        snapshot = app.capture()
        self.assertEqual(snapshot['windows'][0]['kind'], 'claude')
        self.assertNotIn('agent_group', snapshot['windows'][0])
        self.assertEqual([s['session'] for s in snapshot['agent_groups'][0]['sessions']], ['codex-exact-123'])
        self.assertEqual(len(restore_entries(snapshot)), 2)

    def test_missing_session_identity_keeps_other_conversation_and_reports_error(self):
        self.hook(22, 'codex', 'stale-session', boot='previous-boot')
        group, = app.capture()['agent_groups']
        self.assertEqual([s['session'] for s in group['sessions']], ['claude-exact-123'])
        self.assertTrue(any('Stale codex' in error for error in group['errors']))

    def test_same_session_with_conflicting_launch_options_is_not_arbitrarily_selected(self):
        self.procs[22]['cmd'] = ['claude']
        self.hook(22, 'claude', 'claude-exact-123')
        group, = app.capture()['agent_groups']
        self.assertEqual(group['sessions'], [])
        self.assertIn('conflicting launch options', group['errors'][0])
        self.procs[22]['cmd'] = list(self.procs[21]['cmd'])
        group, = app.capture()['agent_groups']
        self.assertEqual(len(group['sessions']), 1)
        self.assertEqual(group['errors'], [])

    def test_agent_home_contexts_remain_separate(self):
        self.procs[22]['cmd'] = list(self.procs[21]['cmd'])
        self.hook(22, 'claude', 'claude-exact-123')
        with patch.object(agents, 'process_env', side_effect=lambda pid: {'CLAUDE_CONFIG_DIR': f'/home-{pid}'}):
            group, = app.capture()['agent_groups']
        self.assertEqual(len(group['sessions']), 2)
        self.assertEqual({s['argv'][1] for s in group['sessions']},
                         {'CLAUDE_CONFIG_DIR=/home-21', 'CLAUDE_CONFIG_DIR=/home-22'})

    def test_herdr_owns_its_inner_agents(self):
        self.procs[22]['cmd'] = ['herdr', '--session', 'named']
        self.procs[30] = self.proc(22, ['codex'], 9)
        group, = app.capture()['agent_groups']
        self.assertEqual({s['kind'] for s in group['sessions']}, {'claude', 'herdr'})
        herdr = next(s for s in group['sessions'] if s['kind'] == 'herdr')
        self.assertEqual(herdr['argv'], ['herdr', '--session', 'named'])

    def test_codex_frontend_uses_app_server_hook_and_keeps_profile(self):
        self.procs[30] = self.proc(22, ['codex', 'app-server'], 2)
        self.hook(30, 'codex', 'server-session-123')
        group, = app.capture()['agent_groups']
        codex = next(s for s in group['sessions'] if s['kind'] == 'codex')
        self.assertEqual(codex['session'], 'server-session-123')
        self.assertEqual(codex['argv'][-2:], ['--profile', 'work'])

    def test_extra_terminal_surface_disables_fallback(self):
        self.procs[13] = self.proc(10, ['zsh'], 3)
        self.procs[23] = self.proc(13, ['claude'], 3)
        self.hook(23, 'claude', 'hidden-session-123')
        snapshot = app.capture()
        self.assertNotIn('agent_groups', snapshot)
        self.assertTrue(all(w.get('error') for w in snapshot['windows']))

    def test_unmapped_sibling_disables_fallback(self):
        self.windows[1]['mapped'] = False
        self.assertNotIn('agent_groups', app.capture())

    def test_unreadable_terminal_process_does_not_crash_capture(self):
        self.procs.pop(10)
        for window in self.windows:
            window['tags'] = ['terminal*']
        snapshot = app.capture()
        self.assertNotIn('agent_groups', snapshot)
        self.assertTrue(all(w.get('error') for w in snapshot['windows']))

    def test_duplicate_sessions_across_shared_processes_restore_once(self):
        self.second_group()
        snapshot = app.capture()
        self.assertEqual(len(snapshot['agent_groups']), 2)
        self.assertEqual(len(restore_entries(snapshot)), 2)
        self.assertFalse(any(g['errors'] for g in snapshot['agent_groups']))

    def test_conflicting_options_across_shared_processes_are_reported(self):
        self.second_group()
        self.procs[121]['cmd'] = ['claude', '--permission-mode', 'default']
        snapshot = app.capture()
        self.assertEqual([e['kind'] for e in restore_entries(snapshot)], ['codex'])
        self.assertTrue(all('conflicting launch options' in g['errors'][0] for g in snapshot['agent_groups']))

    def test_private_tool_pty_and_noninteractive_jobs_are_excluded(self):
        self.procs[30] = self.proc(21, ['claude'], 9)
        for cmd in (['codex', 'exec', 'task'], ['claude', '--print', 'task']):
            self.procs[22]['cmd'] = cmd
            group, = app.capture()['agent_groups']
            self.assertEqual([s['session'] for s in group['sessions']], ['claude-exact-123'])
        self.procs[22]['cmd'] = ['codex']
        self.procs[22]['tpgid'] = 123  # Not foreground on its own PTY.
        group, = app.capture()['agent_groups']
        self.assertEqual([s['session'] for s in group['sessions']], ['claude-exact-123'])

    def test_opencode_regression_and_cache_only_shutdown(self):
        self.oc(1, args=['--auto', '--session', 'ses_stale'])
        self.oc(2, args=['--auto', '--session', 'another_stale'])
        with patch.object(app, 'sessions', return_value=self.sessions(1, 2)):
            snapshot = app.capture()
        group, = snapshot['agent_groups']
        self.assertEqual({s['session'] for s in group['sessions']}, {'ses_exact_1', 'ses_exact_2'})
        self.assertTrue(all('--auto' in s['argv'] for s in group['sessions']))
        self.assertEqual(group['errors'], [])
        with patch.object(app, 'sessions', side_effect=AssertionError('API queried during shutdown')):
            self.assertEqual(app.capture(fast=True)['agent_groups'], snapshot['agent_groups'])
            app.save_shutdown()
        self.assertEqual(app.read_json(self.state / 'shutdown.json')['agent_groups'], snapshot['agent_groups'])

    def test_mixed_native_and_opencode_groups(self):
        self.oc(2)
        with patch.object(app, 'sessions', return_value=self.sessions(2)):
            group, = app.capture()['agent_groups']
        self.assertEqual({s['kind'] for s in group['sessions']}, {'claude', 'opencode'})
        self.assertEqual(group['errors'], [])

    def test_opencode_conflicting_client_options_are_not_guessed(self):
        self.oc(1, args=['--auto'])
        self.oc(2)
        with patch.object(app, 'sessions', return_value=self.sessions(1, 2)):
            group, = app.capture()['agent_groups']
        self.assertEqual(group['sessions'], [])
        self.assertTrue(any('ambiguous' in error for error in group['errors']))

    def test_untitled_opencode_client_is_reported_alongside_preserved_conversation(self):
        self.oc(1)
        self.oc(2)
        self.windows[1]['title'] = 'OpenCode'
        with patch.object(app, 'sessions', return_value=self.sessions(1)):
            group, = app.capture()['agent_groups']
        self.assertEqual([s['session'] for s in group['sessions']], ['ses_exact_1'])
        self.assertTrue(any('1 OpenCode clients lack' in error for error in group['errors']))

    def test_missing_context_cache_does_not_make_other_context_appear_unique(self):
        self.oc(1, binary='opencode')
        self.oc(2)
        app.write_json(self.state / 'sessions-cache.json', self.sessions(1, 2))
        group, = app.capture(fast=True)['agent_groups']
        self.assertEqual(group['sessions'], [])
        self.assertTrue(any('not cached' in error for error in group['errors']))

    def test_mixed_opencode_versions_require_unique_context_match(self):
        self.oc(1, binary='opencode')
        self.oc(2)
        with patch.object(app, 'sessions_v1', return_value=self.sessions(1)), \
             patch.object(app, 'sessions', return_value=self.sessions(2)):
            group, = app.capture()['agent_groups']
        self.assertEqual({(s['kind'], s['session']) for s in group['sessions']},
                         {('opencode1', 'ses_exact_1'), ('opencode', 'ses_exact_2')})
        self.windows[1]['title'] = self.windows[0]['title']
        with patch.object(app, 'sessions_v1', return_value=self.sessions(1)), \
             patch.object(app, 'sessions', return_value=self.sessions(1)):
            group, = app.capture()['agent_groups']
        self.assertEqual(group['sessions'], [])
        self.assertTrue(group['errors'])

    def test_failed_context_query_is_not_treated_as_no_match(self):
        self.oc(1, binary='opencode')
        self.oc(2)
        with patch.object(app, 'sessions_v1', side_effect=ValueError('context unavailable')), \
             patch.object(app, 'sessions', return_value=self.sessions(1, 2)):
            group, = app.capture()['agent_groups']
        self.assertEqual(group['sessions'], [])
        self.assertTrue(any('context unavailable' in error for error in group['errors']))

    def test_unidentified_remote_client_blocks_opencode_guessing(self):
        self.oc(1, args=['--server', 'http://other-server'])
        self.oc(2)
        with patch.object(app, 'sessions', return_value=self.sessions(1, 2)):
            group, = app.capture()['agent_groups']
        self.assertEqual(group['sessions'], [])
        self.assertTrue(any('unidentified' in error for error in group['errors']))

    def test_one_opencode_client_cannot_justify_two_conversations(self):
        self.oc(1, binary='opencode')
        self.oc(2)
        with patch.object(app, 'sessions_v1', return_value=self.sessions(1, 2)), \
             patch.object(app, 'sessions', return_value=[]):
            group, = app.capture()['agent_groups']
        self.assertEqual(group['sessions'], [])
        self.assertIn('distinct local clients', group['errors'][-1])

    def test_duplicate_opencode_title_metadata_is_rejected(self):
        self.oc(2)
        duplicate = self.sessions(2) + [{**self.sessions(2)[0], 'id': 'ses_other'}]
        with patch.object(app, 'sessions', return_value=duplicate):
            group, = app.capture()['agent_groups']
        self.assertEqual([s['kind'] for s in group['sessions']], ['claude'])
        self.assertTrue(any('matches 2 sessions' in error for error in group['errors']))

    def test_group_session_switch_changes_checkpoint_signature(self):
        before = app.capture()
        self.hook(21, 'claude', 'switched-session-456')
        after = app.capture()
        self.assertNotEqual(app.signature(before), app.signature(after))
        self.assertEqual(before['windows'], after['windows'])

    def test_unknown_identity_preserves_previous_shutdown_checkpoint(self):
        app.write_json(self.state / 'shutdown.json', {'keep': 'previous'})
        self.hook(22, 'codex', 'stale-session', start='old-process')
        app.save_shutdown()
        self.assertEqual(app.read_json(self.state / 'shutdown.json'), {'keep': 'previous'})

    def test_restore_uses_fallback_and_reports_placement_warning(self):
        snapshot = app.capture()
        entries = restore_entries(snapshot)
        actual = [{**entry, 'address': f'new-{i}', 'pid': 100 + i} for i, entry in enumerate(entries)]
        with patch.object(app, 'capture', return_value={'windows': []}), \
             patch.object(app, 'launch') as launch, patch.object(app, 'place'), \
             patch.object(app, 'wait_for_window', side_effect=[(w, False) for w in actual]):
            self.assertTrue(app.restore(snapshot))
        self.assertEqual(launch.call_count, 2)
        result = app.read_json(self.state / 'last-result.json')
        self.assertEqual(result['restored'], 2)
        self.assertEqual(result['errors'], [])
        self.assertIn('placement is approximate', result['warnings'][0])

    def test_live_group_inventory_prevents_duplicate_launches(self):
        snapshot = app.capture()
        with patch.object(app, 'capture', return_value=snapshot), patch.object(app, 'launch') as launch:
            self.assertTrue(app.restore(snapshot))
        launch.assert_not_called()
        self.assertEqual(app.read_json(self.state / 'last-result.json')['already_open'], 2)

    def test_restore_retry_remembers_real_windows_even_before_session_metadata_is_ready(self):
        snapshot = app.capture()
        entries = restore_entries(snapshot)
        actual = [{**entry, 'address': f'new-{i}', 'pid': 100 + i} for i, entry in enumerate(entries)]
        pending = [{**w, 'kind': 'agent-unresolved', 'error': 'hook pending'} for w in actual]
        for w in pending:
            w.pop('agent_group')
        with patch.object(app, 'capture', side_effect=[{'windows': []}, {'windows': pending}]), \
             patch.object(app, 'launch') as launch, patch.object(app, 'place'), \
             patch.object(app, 'wait_for_window', side_effect=[(w, False) for w in actual]):
            self.assertTrue(app.restore(snapshot))
            self.assertTrue(app.restore(snapshot))
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(app.read_json(self.state / 'last-result.json')['already_open'], 2)

    def test_partial_group_restores_good_session_and_reports_unidentified_one(self):
        self.hook(22, 'codex', 'old-session', boot='old-boot')
        snapshot = app.capture()
        entry, = restore_entries(snapshot)
        actual = {**entry, 'address': 'new', 'pid': 100}
        with patch.object(app, 'capture', return_value={'windows': []}), patch.object(app, 'launch') as launch, \
             patch.object(app, 'place'), patch.object(app, 'wait_for_window', return_value=(actual, False)):
            self.assertFalse(app.restore(snapshot))
        launch.assert_called_once()
        result = app.read_json(self.state / 'last-result.json')
        self.assertEqual(result['restored'], 1)
        self.assertTrue(any('Stale codex' in error for error in result['errors']))


if __name__ == '__main__':
    unittest.main()
