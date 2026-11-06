"""Choosing the workspace a restored Herdr window opens on."""
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import desktop_restore as app
import herdr_workspace as herdr


GAME = {'workspace_id': 'w1', 'label': '~Game', 'active_tab_id': 'w1:t1'}
MIX = {'workspace_id': 'w3', 'label': 'mix', 'active_tab_id': 'w3:t2'}


def server_control(kind, body):
    return herdr.frame(herdr.SERVER_CONTROL, kind, json.dumps(body))


def server_response(body, chunk_size=None):
    raw = json.dumps(body).encode()
    chunk_size = chunk_size or len(raw)
    frames = b''
    for start in range(0, len(raw), chunk_size):
        chunk = raw[start:start + chunk_size]
        final = start + chunk_size >= len(raw)
        payload = (herdr.varint(herdr.SERVER_RESPONSE) + b'\x04boot\x03req' + (b'\x01' if final else b'\x00')
                   + herdr.varint(len(chunk)) + chunk)
        frames += len(payload).to_bytes(4, 'little') + payload
    return frames


class FakeHerdr:
    """Answers both sockets, so tests can never reach a real session."""

    def __init__(self, directory, snapshot, welcome=None, reply=None):
        self.snapshot, self.client_frames = snapshot, []
        self.welcome = welcome or {'generation': 1, 'methods': ['workspace.focus']}
        self.reply = reply or {'id': 'desktop-restore', 'result': {'type': 'workspace_info'}}
        self.api_requests = []
        for name, handler in (('herdr.sock', self.serve_api), ('herdr-client.sock', self.serve_client)):
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(directory / name))
            listener.listen()
            threading.Thread(target=self.accept, args=(listener, handler), daemon=True).start()

    @staticmethod
    def accept(listener, handler):
        with listener:
            while True:
                connection, _ = listener.accept()
                with connection:
                    handler(connection)

    def serve_api(self, connection):
        data = b''
        while not data.endswith(b'\n'):
            data += connection.recv(65536)
        self.api_requests.append(json.loads(data))
        connection.sendall(json.dumps({'id': 'x', 'result': {'snapshot': self.snapshot}}).encode() + b'\n')

    def serve_client(self, connection):
        self.client_frames.append(self.read(connection))
        # Unknown messages must be skipped, and a rejected hello gets no snapshot.
        connection.sendall(herdr.frame(4, 'ignored') + server_control('endpoint.welcome.v1', self.welcome))
        if self.welcome.get('error'):
            return
        connection.sendall(server_control('shell.snapshot.v1', {'boot_id': 'boot-1'}))
        self.client_frames.append(self.read(connection))
        connection.sendall(herdr.frame(4, 'ignored') + server_response(self.reply, chunk_size=5))
        self.client_frames.append(self.read(connection))

    @staticmethod
    def read(connection):
        def exact(size):
            data = b''
            while len(data) < size:
                data += connection.recv(size - len(data))
            return data
        return exact(int.from_bytes(exact(4), 'little'))


class TitleTests(unittest.TestCase):
    def test_workspace_name_comes_from_the_end_of_the_title(self):
        self.assertEqual(herdr.workspace_in_title('omarchy: mix', [GAME, MIX]), MIX)

    def test_hostname_equal_to_a_workspace_name_is_not_a_match(self):
        host = {'workspace_id': 'w9', 'label': 'omarchy'}
        self.assertEqual(herdr.workspace_in_title('omarchy: mix', [host, MIX]), MIX)

    def test_longest_name_wins_over_its_suffix(self):
        short = {'workspace_id': 'w9', 'label': 'Game'}
        self.assertEqual(herdr.workspace_in_title('omarchy: ~Game', [short, GAME]), GAME)

    def test_name_must_start_at_a_boundary(self):
        self.assertIsNone(herdr.workspace_in_title('omarchy: remix', [MIX]))

    def test_title_may_be_only_the_workspace(self):
        self.assertEqual(herdr.workspace_in_title('mix', [MIX]), MIX)

    def test_shared_names_are_not_guessed(self):
        twin = {'workspace_id': 'w9', 'label': 'mix'}
        self.assertIsNone(herdr.workspace_in_title('omarchy: mix', [MIX, twin]))

    def test_empty_labels_never_match(self):
        self.assertIsNone(herdr.workspace_in_title('omarchy: mix', [{'workspace_id': 'w9', 'label': ''}]))


class SessionDirectoryTests(unittest.TestCase):
    def test_default_session_uses_the_config_directory(self):
        saved = {'session': 'default', 'agent_env': {'XDG_CONFIG_HOME': '/cfg'}}
        self.assertEqual(herdr.session_directory(saved), Path('/cfg/herdr'))

    def test_older_checkpoints_without_a_session_are_the_default(self):
        with patch.dict(herdr.os.environ, {'XDG_CONFIG_HOME': '/env'}):
            self.assertEqual(herdr.session_directory({}), Path('/env/herdr'))

    def test_named_session_has_its_own_directory(self):
        saved = {'session': 'work', 'agent_env': {'XDG_CONFIG_HOME': '/cfg'}}
        self.assertEqual(herdr.session_directory(saved), Path('/cfg/herdr/sessions/work'))

    def test_missing_config_home_falls_back_to_dot_config(self):
        with patch.dict(herdr.os.environ, clear=True), patch.object(herdr.Path, 'home', return_value=Path('/h')):
            self.assertEqual(herdr.session_directory({}), Path('/h/.config/herdr'))


class WireTests(unittest.TestCase):
    def test_frame_layout_matches_herdrs_frozen_encoding(self):
        self.assertEqual(herdr.frame(20, 'a', 'bc'), b'\x06\x00\x00\x00\x14\x01a\x02bc')
        self.assertEqual(herdr.frame(4), b'\x01\x00\x00\x00\x04')

    def test_long_strings_use_bincode_varints(self):
        self.assertEqual(herdr.varint(250), b'\xfa')
        self.assertEqual(herdr.varint(251), b'\xfb\xfb\x00')
        self.assertEqual(herdr.read_varint(b'\xfb\xfb\x00', 0), (251, 3))
        self.assertEqual(herdr.read_varint(herdr.varint(70000), 0), (70000, 5))


class SelectTests(unittest.TestCase):
    def setUp(self):
        # Unix socket paths are limited to 108 bytes, so stay clear of a long TMPDIR.
        self.root = Path(tempfile.mkdtemp(prefix='hw', dir='/tmp'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.directory = self.root / 'herdr'
        self.directory.mkdir()
        self.saved = {'title': 'omarchy: mix', 'session': 'default',
                      'agent_env': {'XDG_CONFIG_HOME': str(self.root)}}
        self.snapshot = {'workspaces': [GAME, MIX], 'focused_workspace_id': 'w1',
                         'layouts': [{'tab_id': 'w3:t2', 'area': {'x': 0, 'y': 0, 'width': 131, 'height': 64}}]}

    def fake(self, **kwargs):
        return FakeHerdr(self.directory, self.snapshot, **kwargs)

    def wait_for_detach(self, server):
        for _ in range(200):
            if len(server.client_frames) == 3:
                return
            threading.Event().wait(0.01)
        self.fail(f'client stopped after {len(server.client_frames)} frames')

    def test_selects_the_workspace_as_a_client_and_detaches(self):
        server = self.fake()
        self.assertEqual(herdr.select(self.saved), 'mix')
        self.wait_for_detach(server)
        self.assertEqual(server.api_requests[0]['method'], 'session.snapshot')

        tag, index = herdr.read_varint(server.client_frames[0], 0)
        self.assertEqual(tag, herdr.CLIENT_CONTROL)
        kind, index = herdr.read_bytes(server.client_frames[0], index)
        hello = json.loads(herdr.read_bytes(server.client_frames[0], index)[0])
        self.assertEqual(kind, b'endpoint.hello.v1')
        self.assertEqual(hello['generation'], 1)
        self.assertTrue(hello['surface_active'], 'Herdr refuses navigation from an inactive surface')
        self.assertEqual(hello['surface_size'], {'cols': 131, 'rows': 64}, 'must not resize the tab')

        tag, index = herdr.read_varint(server.client_frames[1], 0)
        self.assertEqual(tag, herdr.CLIENT_REQUEST)
        boot_id, index = herdr.read_bytes(server.client_frames[1], index)
        request = json.loads(herdr.read_bytes(server.client_frames[1], index)[0])
        self.assertEqual(boot_id, b'boot-1')
        self.assertEqual(request['method'], 'workspace.focus')
        self.assertEqual(request['params'], {'workspace_id': 'w3'})
        self.assertEqual(server.client_frames[2], bytes([herdr.CLIENT_DETACH]))

    def test_already_default_workspace_needs_no_client(self):
        self.snapshot['focused_workspace_id'] = 'w3'
        server = self.fake()
        self.assertEqual(herdr.select(self.saved), 'mix')
        self.assertEqual(server.client_frames, [])

    def test_tab_without_a_layout_uses_the_headless_size(self):
        self.assertEqual(herdr.tab_size({'layouts': []}, MIX), herdr.DEFAULT_SIZE)

    def test_unknown_title_changes_nothing(self):
        server = self.fake()
        with self.assertRaisesRegex(herdr.Unavailable, 'no single workspace'):
            herdr.select({**self.saved, 'title': 'omarchy: renamed'})
        self.assertEqual(server.client_frames, [])

    def test_stopped_server_is_reported(self):
        with self.assertRaisesRegex(herdr.Unavailable, 'not reachable'):
            herdr.select(self.saved)

    def test_rejected_hello_is_reported(self):
        self.fake(welcome={'generation': 2, 'error': {'code': 'incompatible', 'message': 'too old'}})
        with self.assertRaisesRegex(herdr.Unavailable, 'too old'):
            herdr.select(self.saved)

    def test_rejected_focus_is_reported(self):
        self.fake(reply={'id': 'desktop-restore', 'error': {'code': 'workspace_not_found', 'message': 'gone'}})
        with self.assertRaisesRegex(herdr.Unavailable, 'gone'):
            herdr.select(self.saved)

    def test_unexpected_snapshot_is_reported(self):
        self.snapshot.pop('workspaces')
        self.fake()
        with self.assertRaisesRegex(herdr.Unavailable, 'unexpected'):
            herdr.select(self.saved)

    def test_silent_server_cannot_hold_up_the_restore(self):
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.directory / 'herdr.sock'))
        listener.listen()
        with patch.object(herdr, 'TIMEOUT', 0.05), self.assertRaisesRegex(herdr.Unavailable, 'not reachable'):
            herdr.select(self.saved)


class RestoreOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for mock in (patch.object(app, 'STATE', Path(self.temp.name)),
                     patch.object(app, 'instance', return_value='new-login'),
                     patch.object(app.restore_log, 'enabled', return_value=False),
                     patch.object(app, 'report')):
            mock.start()
            self.addCleanup(mock.stop)
        self.steps = []

    def window(self, key, title, kind='herdr', window_class='ghostty'):
        return {'kind': kind, 'key': key, 'session': 'default', 'class': window_class, 'title': title,
                'pid': 1, 'address': 'old-' + key, 'workspace': '2', 'launch': [window_class],
                'at': [0, 0], 'size': [1, 1], 'floating': False, 'fullscreen': 0}

    def restore(self, windows, select=None, opened_title=None):
        def wait(saved, before, taken, slow_title):
            self.steps.append(('attached', saved['key'], slow_title))
            return ({**saved, 'address': 'new-' + saved['key'], 'pid': 2, 'mapped': True, 'monitor': 0,
                     'workspace': {'name': '2'}, 'title': opened_title or saved['title']}, False)

        select = select or (lambda saved: self.steps.append(('select', saved['key'])) or 'mix')
        with patch.object(app, 'capture', return_value={'windows': []}), \
             patch.object(app, 'hypr', return_value=[]), patch.object(app, 'place'), \
             patch.object(app, 'launch', side_effect=lambda saved: self.steps.append(('launch', saved['key']))), \
             patch.object(app, 'wait_for_window', side_effect=wait), \
             patch.object(app.herdr_workspace, 'select', side_effect=select):
            result = app.restore({'instance': 'old-login', 'windows': windows})
        return result, app.read_json(app.STATE / 'last-result.json')

    def test_each_window_attaches_before_the_next_selection(self):
        ok, result = self.restore([self.window('a', 'host: mix'), self.window('b', 'host: ~Game')])
        self.assertTrue(ok)
        self.assertEqual(self.steps, [('select', 'a'), ('launch', 'a'), ('attached', 'a', True),
                                      ('select', 'b'), ('launch', 'b'), ('attached', 'b', True)])
        self.assertNotIn('warnings', result)

    def test_other_windows_never_contact_herdr(self):
        ok, _ = self.restore([self.window('a', 'zsh', kind='terminal')])
        self.assertTrue(ok)
        self.assertEqual(self.steps, [('launch', 'a'), ('attached', 'a', False)])

    def test_failed_selection_still_opens_the_window_and_warns(self):
        def unavailable(_):
            raise app.herdr_workspace.Unavailable('Herdr server is not reachable')

        ok, result = self.restore([self.window('a', 'host: mix')], select=unavailable)
        self.assertTrue(ok)
        self.assertEqual(self.steps, [('launch', 'a'), ('attached', 'a', False)])
        self.assertIn('not reachable', result['warnings'][0])
        self.assertEqual(result['restored'], 1)

    def test_window_on_another_workspace_is_reported(self):
        ok, result = self.restore([self.window('a', 'host: mix')], opened_title='host: ~Game')
        self.assertTrue(ok)
        self.assertIn("'host: ~Game'", result['warnings'][0])

    def test_windows_in_different_terminals_do_not_overlap(self):
        inside, overlaps, guard = [], [], threading.Lock()

        def select(saved):
            with guard:
                overlaps.extend(inside)
                inside.append(saved['key'])
            threading.Event().wait(0.05)
            return 'mix'

        def wait(saved, *_):
            with guard:
                inside.remove(saved['key'])
            return ({**saved, 'address': 'new-' + saved['key'], 'pid': 2, 'mapped': True, 'monitor': 0,
                     'workspace': {'name': '2'}}, False)

        windows = [self.window('a', 'host: mix'), self.window('b', 'host: ~Game', window_class='foot')]
        with patch.object(app, 'capture', return_value={'windows': []}), \
             patch.object(app, 'hypr', return_value=[]), patch.object(app, 'place'), patch.object(app, 'launch'), \
             patch.object(app, 'wait_for_window', side_effect=wait), \
             patch.object(app.herdr_workspace, 'select', side_effect=select):
            self.assertTrue(app.restore({'instance': 'old-login', 'windows': windows}))
        self.assertEqual(overlaps, [])


if __name__ == '__main__':
    unittest.main()
