import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import agents
import desktop_restore as app
from terminals import terminal_launch


class TerminalTests(unittest.TestCase):
    def test_capture_resumes_each_agent_in_its_original_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'agents').mkdir()
            for terminal in ('ghostty', 'foot', 'footclient', 'kitty', 'alacritty', 'wezterm-gui'):
                for kind in ('codex', 'claude', 'herdr', 'shell'):
                    with self.subTest(terminal=terminal, kind=kind):
                        client = {'pid': 10, 'class': 'custom.app.id', 'title': 'custom title', 'address': 'test',
                                  'workspace': {'id': 3, 'name': '3'}, 'at': [0, 0], 'size': [500, 500],
                                  'floating': False, 'fullscreen': 0, 'mapped': True, 'monitor': 0}
                        procs = {10: {'parent': 1, 'cmd': [terminal], 'cwd': directory, 'tty': 0},
                                 11: {'parent': 10, 'cmd': ['zsh'], 'cwd': directory, 'tty': 1,
                                      'pgrp': 11, 'tpgid': 12}}
                        if kind != 'shell':
                            procs[12] = {'parent': 11, 'cmd': [kind], 'cwd': directory, 'tty': 1,
                                         'pgrp': 12, 'tpgid': 12, 'start': '100'}
                        if kind in ('codex', 'claude'):
                            (state / 'agents' / f'{kind}-12.json').write_text(json.dumps({
                                'kind': kind, 'pid': 12, 'start': '100', 'boot': agents.BOOT_ID,
                                'session': 'exact-session-id', 'cwd': directory}))
                        with patch.object(app, 'STATE', state), patch.object(app, 'instance', return_value='test'), \
                             patch.object(app, 'hypr', side_effect=[[client], [{'id': 0, 'name': 'DP-1'}]]), \
                             patch.object(app, 'processes', return_value=procs), \
                             patch.object(app, 'desktop_apps', return_value={}), \
                             patch.object(agents, 'process_env', return_value={}):
                            saved = app.capture(fast=True)['windows'][0]
                        self.assertNotIn('error', saved)
                        binary = {'footclient': 'foot', 'wezterm-gui': 'wezterm'}.get(terminal, terminal)
                        self.assertEqual(saved['launch'][0], binary)
                        self.assertEqual(saved['kind'], 'terminal' if kind == 'shell' else kind)
                        self.assertEqual(saved['cwd'], directory)
                        if kind in ('codex', 'claude'):
                            self.assertEqual(saved['session'], 'exact-session-id')
                            self.assertEqual(saved['launch'][-3:], saved['argv'])

    def test_launch_arguments_preserve_spaces_without_shell_interpolation(self):
        for name in ('ghostty', 'foot', 'footclient', 'kitty', 'alacritty', 'wezterm-gui'):
            argv = ['claude', '--resume', 'exact-session-id']
            launch = terminal_launch(name, {'class': 'custom-id'}, '/work with spaces', argv)
            self.assertEqual(launch[-3:], argv)
            self.assertTrue(any('/work with spaces' in arg for arg in launch))
            self.assertTrue(any('custom-id' in arg for arg in launch))

    def test_unknown_tagged_terminal_is_not_silently_replaced_with_ghostty(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported terminal'):
            terminal_launch('unknown', {'class': 'custom'}, '/work', [])


if __name__ == '__main__':
    unittest.main()
