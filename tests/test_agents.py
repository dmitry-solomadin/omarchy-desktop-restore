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


def proc(parent, cmd, cwd='/work', start='100', **extra):
    return {'parent': parent, 'cmd': cmd, 'cwd': cwd, 'start': start,
            'tty': 1, 'pgrp': 5, 'tpgid': 5, **extra}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.window = {'pid': 10, 'title': '/work'}
        self.procs = {10: proc(1, ['ghostty']), 11: proc(10, ['zsh']),
                      12: proc(11, ['codex'])}
        self.environment = patch.object(agents, 'process_env', return_value={})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def metadata(self, kind='codex', session='12345678-1234-1234-1234-123456789abc', start='100', pid=12):
        (self.state / f'{kind}-{pid}.json').write_text(json.dumps({
            'kind': kind, 'pid': pid, 'start': start, 'boot': agents.BOOT_ID, 'cwd': '/work', 'session': session}))

    def test_codex_resumes_exact_id_not_latest(self):
        self.metadata()
        saved = agents.terminal_agent(self.window, self.procs, self.state)
        self.assertEqual(saved['argv'], ['codex', 'resume', '12345678-1234-1234-1234-123456789abc'])
        self.assertEqual(saved['cwd'], '/work')

    def test_conversation_switch_refreshes_identity_in_the_same_process(self):
        self.metadata(session='first-session')
        first = agents.terminal_agent(self.window, self.procs, self.state)
        self.metadata(session='second-session')
        second = agents.terminal_agent(self.window, self.procs, self.state)
        self.assertNotEqual(first['session'], second['session'])

    def test_reused_pid_cannot_resume_a_previous_process_session(self):
        self.metadata(start='previous-process')
        with self.assertRaisesRegex(ValueError, 'Stale'):
            agents.terminal_agent(self.window, self.procs, self.state)

    def test_missing_hook_never_falls_back_to_continue_or_command_line_id(self):
        self.procs[12]['cmd'] = ['claude', '--resume', 'old-session-id']
        with self.assertRaisesRegex(ValueError, 'No current claude'):
            agents.terminal_agent(self.window, self.procs, self.state)

    def test_claude_preserves_explicit_mode_without_replaying_prompt(self):
        self.procs[12]['cmd'] = ['claude', '--permission-mode', 'plan', 'secret user prompt']
        self.metadata(kind='claude')
        result = agents.terminal_agent(self.window, self.procs, self.state)
        self.assertEqual(result['argv'], ['claude', '--resume', result['session'], '--permission-mode', 'plan'])

    def test_codex_npm_wrapper_does_not_count_as_two_agents(self):
        self.procs[12] = proc(11, ['node', '/npm/@openai/codex/bin/codex.js'])
        self.procs[13] = proc(12, ['/npm/vendor/codex'])
        self.metadata(pid=13)
        self.assertEqual(agents.terminal_agent(self.window, self.procs, self.state)['kind'], 'codex')

    def test_local_codex_app_server_hook_identifies_the_frontend(self):
        self.procs[13] = proc(12, ['codex', 'app-server'])
        self.metadata(pid=13)
        self.assertEqual(agents.terminal_agent(self.window, self.procs, self.state)['kind'], 'codex')

    def test_previous_boot_hook_is_not_reused(self):
        self.metadata()
        with patch.object(agents, 'BOOT_ID', 'another-boot'):
            with self.assertRaisesRegex(ValueError, 'Stale'):
                agents.terminal_agent(self.window, self.procs, self.state)

    def test_multiple_sessions_in_shared_terminal_are_rejected(self):
        self.procs[20] = proc(10, ['zsh'])
        self.procs[21] = proc(20, ['claude'])
        with self.assertRaisesRegex(ValueError, 'shared-process'):
            agents.terminal_agent(self.window, self.procs, self.state, shared=True)

    def test_shared_terminal_unique_directory_is_identifiable(self):
        self.procs[20] = proc(10, ['zsh'], cwd='/other')
        self.metadata()
        self.assertEqual(agents.terminal_agent(self.window, self.procs, self.state, shared=True)['kind'], 'codex')

    def test_shared_claude_window_and_plain_shell_are_matched_one_to_one(self):
        self.procs[12]['cmd'] = ['claude']
        self.procs[20] = proc(10, ['zsh'], cwd='/other')
        self.metadata(kind='claude')
        claude = {**self.window, 'address': 'agent', 'title': '✳ my named conversation'}
        shell = {**self.window, 'address': 'shell', 'title': '/other'}
        for siblings in ([claude, shell], [shell, claude]):
            result = agents.terminal_agent(claude, self.procs, self.state, shared=True, siblings=siblings)
            self.assertEqual(result['kind'], 'claude')
            self.assertIsNone(agents.terminal_agent(shell, self.procs, self.state, shared=True, siblings=siblings))

    def test_elimination_does_not_guess_when_a_hidden_terminal_branch_exists(self):
        self.procs[20] = proc(10, ['zsh'], cwd='/other')
        self.procs[21] = proc(10, ['zsh'], cwd='/hidden')
        agent = {**self.window, 'title': 'agent title'}
        shell = {**self.window, 'title': '/other'}
        with self.assertRaisesRegex(ValueError, 'shared-process'):
            agents.terminal_agent(agent, self.procs, self.state, shared=True, siblings=[agent, shell])

    def test_background_and_noninteractive_agents_are_not_restored(self):
        self.procs[12]['tpgid'] = 99
        self.assertIsNone(agents.terminal_agent(self.window, self.procs, self.state))
        self.procs[12] = proc(11, ['codex', 'exec', 'some task'])
        self.assertIsNone(agents.terminal_agent(self.window, self.procs, self.state))

    def test_herdr_default_and_named_sessions(self):
        for args, expected in (([], ['herdr', '--session', 'default']), (['--session', 'work'], ['herdr', '--session', 'work']),
                               (['session', 'attach', 'work'], ['herdr', '--session', 'work'])):
            self.procs[12]['cmd'] = ['herdr'] + args
            result = agents.terminal_agent(self.window, self.procs, self.state)
            self.assertEqual(result['argv'], expected)

    def test_herdr_owns_its_agent_panes(self):
        self.procs[12]['cmd'] = ['herdr', '--session', 'project']
        self.procs[13] = proc(12, ['claude'])
        self.assertEqual(agents.terminal_agent(self.window, self.procs, self.state)['kind'], 'herdr')

    def test_codex_exec_with_global_options_is_not_an_interactive_session(self):
        self.procs[12]['cmd'] = ['codex', '-p', 'work', 'exec', 'task']
        self.assertIsNone(agents.terminal_agent(self.window, self.procs, self.state))

    def test_agent_tool_private_pty_does_not_become_a_desktop_session(self):
        self.metadata()
        self.procs[13] = proc(12, ['claude'], tty=2)
        self.assertEqual(agents.terminal_agent(self.window, self.procs, self.state)['kind'], 'codex')

    def test_hidden_agent_tab_does_not_replace_the_visible_plain_shell(self):
        self.procs[20] = proc(10, ['zsh'], cwd='/other', tty=2)
        visible = {**self.window, 'title': '/other'}
        self.assertIsNone(agents.terminal_agent(visible, self.procs, self.state))

    def test_corrupt_hook_cannot_crash_capture(self):
        self.metadata()
        path = self.state / 'codex-12.json'
        data = json.loads(path.read_text())
        data['session'] = ['invalid']
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            agents.terminal_agent(self.window, self.procs, self.state)

    def test_default_herdr_identity_matches_older_checkpoints(self):
        self.assertEqual(app.identity({'kind': 'herdr', 'session': None}),
                         app.identity({'kind': 'herdr', 'session': 'default'}))

    def test_herdr_environment_selected_session_is_restored_explicitly(self):
        self.procs[12]['cmd'] = ['herdr']
        with patch.object(agents, 'process_env', return_value={'HERDR_SESSION': 'project'}):
            result = agents.terminal_agent(self.window, self.procs, self.state)
        self.assertEqual(result['session'], 'project')
        self.assertEqual(result['argv'], ['herdr', '--session', 'project'])
        self.assertEqual(result['agent_env'], {})

    def test_herdr_explicit_session_overrides_environment_and_socket(self):
        env = {'HERDR_SESSION': 'inherited', 'HERDR_SOCKET_PATH': '/tmp/inherited.sock'}
        for args in (['--session', 'work'], ['session', 'attach', 'work'], ['--session=work']):
            self.assertEqual(agents.herdr_session(args, env), 'work')
        self.assertEqual(agents.herdr_session(['--session', 'default'], env), 'default')

    def test_herdr_last_session_option_wins_like_native_parser(self):
        self.assertEqual(agents.herdr_session(['--session', 'old', '--session=new', '--handoff']), 'new')

    def test_herdr_custom_socket_is_not_replaced_by_default_session(self):
        with self.assertRaisesRegex(ValueError, 'Custom-socket'):
            agents.herdr_session([], {'HERDR_SOCKET_PATH': '/tmp/custom.sock', 'HERDR_SESSION': 'work'})

    def test_herdr_management_commands_are_not_misread_as_clients(self):
        for args in (['--session', 'work', 'server'], ['--session=work', 'api', 'snapshot'],
                     ['agent', 'start', '--', 'claude', '--session', 'child']):
            with self.assertRaisesRegex(ValueError, 'Cannot identify'):
                agents.herdr_session(args)

    def test_herdr_invalid_names_are_rejected(self):
        for name in ('', '.', '..', '../work', 'a b', 'a' * 65):
            with self.assertRaisesRegex(ValueError, 'Invalid'):
                agents.herdr_session(['--session', name])

    def test_remote_and_monolithic_herdr_are_explicitly_rejected(self):
        for args in (['--remote', 'host'], ['--no-session']):
            self.procs[12]['cmd'] = ['herdr'] + args
            with self.assertRaisesRegex(ValueError, 'not supported'):
                agents.terminal_agent(self.window, self.procs, self.state)

    def test_custom_agent_home_is_retained_without_other_environment(self):
        self.metadata()
        with patch.object(agents, 'process_env', return_value={'CODEX_HOME': '/custom home', 'OTHER': 'private'}):
            result = agents.terminal_agent(self.window, self.procs, self.state)
        self.assertEqual(result['argv'][:2], ['env', 'CODEX_HOME=/custom home'])
        self.assertNotIn('private', json.dumps(result))

    def test_hook_records_nearest_agent_identity_without_prompt_content(self):
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'new-session-123',
                   'cwd': '/work', 'prompt': 'do not store this'}
        with patch.object(agents.os, 'getppid', return_value=13), \
             patch.object(agents, 'read_process', side_effect=lambda pid: {13: proc(12, ['sh']), 12: self.procs[12]}[pid]):
            agents.record('codex', self.state, payload)
        path = self.state / 'codex-12.json'
        self.assertEqual(json.loads(path.read_text())['session'], 'new-session-123')
        self.assertNotIn('prompt', path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_subagent_hooks_do_not_overwrite_parent_session(self):
        with patch.object(agents, 'read_process') as read:
            agents.record('codex', self.state, {'hook_event_name': 'SessionStart',
                          'session_id': 'subagent-123', 'cwd': '/work', 'agent_id': 'child'})
        read.assert_not_called()

    def test_failed_agent_mapping_preserves_previous_shutdown_checkpoint(self):
        old = {'windows': [{'title': 'keep this'}]}
        with patch.object(app, 'STATE', self.state):
            app.write_json(self.state / 'shutdown.json', old)
            with patch.object(app, 'capture', return_value={'windows': [
                    {'title': 'Claude Code', 'kind': 'agent-unresolved', 'error': 'No current hook'}]}):
                app.save_shutdown()
            self.assertEqual(app.read_json(self.state / 'shutdown.json'), old)

    def test_native_agents_match_by_session_and_profile_not_only_directory(self):
        first = {'kind': 'codex', 'session': 'one', 'cwd': '/work', 'agent_env': {}}
        self.assertNotEqual(app.identity(first), app.identity({**first, 'session': 'two'}))
        self.assertNotEqual(app.identity(first), app.identity({**first, 'agent_env': {'CODEX_HOME': '/other'}}))

    def test_fast_capture_uses_local_records_without_agent_api_calls(self):
        self.state = self.state / 'agents'
        self.state.mkdir()
        self.metadata()
        client = {**self.window, 'class': 'ghostty', 'title': 'Codex', 'address': 'test',
                  'workspace': {'id': 3, 'name': '3'}, 'at': [0, 0], 'size': [500, 500],
                  'floating': False, 'fullscreen': 0, 'mapped': True, 'monitor': 0}
        with patch.object(app, 'STATE', self.state.parent), \
             patch.object(app, 'instance', return_value='test-login'), \
             patch.object(app, 'hypr', side_effect=[[client], [{'id': 0, 'name': 'DP-1'}]]), \
             patch.object(app, 'processes', return_value=self.procs), \
             patch.object(app, 'desktop_apps', return_value={}), \
             patch.object(app, 'sessions', side_effect=AssertionError('API called during shutdown')):
            saved = app.capture(fast=True)['windows'][0]
        self.assertEqual(saved['kind'], 'codex')
        self.assertEqual(saved['workspace'], '3')
        self.assertIn('resume', saved['launch'])
        self.assertNotIn('error', saved)


if __name__ == '__main__':
    unittest.main()
