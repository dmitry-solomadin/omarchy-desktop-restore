"""Reopen a Herdr window on the workspace it was showing.

A new client starts on the server's default workspace, and the public
`workspace.focus` moves every attached window. A client's own request moves only
that client while updating the default, so a brief client can choose where the next
window opens and leave open windows alone. This speaks Herdr's client endpoint
generation 1, whose wire tags and method shapes Herdr freezes as a compatibility
contract (src/protocol/endpoint.rs).
"""
import json
import os
from pathlib import Path
import socket
import time

TIMEOUT = 3
MAX_FRAME = 32 * 1024 * 1024
# Used when the workspace's active tab has no layout yet; Herdr's headless default.
DEFAULT_SIZE = (120, 40)
CLIENT_DETACH, CLIENT_REQUEST, CLIENT_CONTROL = 4, 15, 20
SERVER_RESPONSE, SERVER_CONTROL = 18, 20


class Unavailable(Exception):
    """The next window will open on the default workspace instead."""


def session_directory(saved):
    """Follow herdr src/session.rs and src/config.rs for the restored client's sockets."""
    config = saved.get('agent_env', {}).get('XDG_CONFIG_HOME') or os.environ.get('XDG_CONFIG_HOME')
    root = (Path(config) if config else Path.home() / '.config') / 'herdr'
    session = saved.get('session') or 'default'
    return root if session == 'default' else root / 'sessions' / session


def workspace_in_title(title, workspaces):
    """Herdr's default window title is '{hostname}: {workspace}', so the name comes last."""
    def ends_title(label):
        if not label or not title.endswith(label):
            return False
        return len(title) == len(label) or not title[-len(label) - 1].isalnum()

    matches = [w for w in workspaces if ends_title(w['label'])]
    longest = max((len(w['label']) for w in matches), default=0)
    matches = [w for w in matches if len(w['label']) == longest]
    # Workspaces can share a name, and guessing could open the wrong project.
    return matches[0] if len(matches) == 1 else None


def varint(value):
    if value < 251:
        return bytes([value])
    if value <= 0xffff:
        return b'\xfb' + value.to_bytes(2, 'little')
    if value <= 0xffffffff:
        return b'\xfc' + value.to_bytes(4, 'little')
    return b'\xfd' + value.to_bytes(8, 'little')


def read_varint(data, index):
    first = data[index]
    if first < 251:
        return first, index + 1
    size = {251: 2, 252: 4, 253: 8}[first]
    return int.from_bytes(data[index + 1:index + 1 + size], 'little'), index + 1 + size


def read_bytes(data, index):
    size, index = read_varint(data, index)
    return data[index:index + size], index + size


def frame(tag, *strings):
    """Herdr frames are a u32le length followed by bincode (standard config)."""
    payload = varint(tag)
    for value in strings:
        raw = value.encode()
        payload += varint(len(raw)) + raw
    return len(payload).to_bytes(4, 'little') + payload


def read_frame(sock, deadline):
    def exact(size):
        data = b''
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Herdr did not answer in time')
            sock.settimeout(remaining)
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError('Herdr closed the connection')
            data += chunk
        return data

    size = int.from_bytes(exact(4), 'little')
    if size > MAX_FRAME:
        raise ValueError('Herdr sent an oversized frame')
    return exact(size)


def api_request(path, method):
    """Read from the public socket, which takes one JSON request per line."""
    deadline = time.monotonic() + TIMEOUT
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(TIMEOUT)
        sock.connect(str(path))
        sock.sendall(json.dumps({'id': 'desktop-restore', 'method': method, 'params': {}}).encode() + b'\n')
        data = b''
        while not data.endswith(b'\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Herdr did not answer in time')
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data)
    if 'error' in reply:
        raise ValueError(reply['error'].get('message', 'Herdr rejected the request'))
    return reply['result']


def focus_as_client(path, workspace_id, size):
    """Herdr only accepts navigation from a client with an active surface.

    That surface sets the size of the tab it views, so callers pass the tab's
    current size to leave its panes as they are.
    """
    hello = {
        'generation': 1, 'cell_width_px': 0, 'cell_height_px': 0,
        'surface_size': {'cols': size[0], 'rows': size[1]}, 'pixel_mouse': False,
        'direct_graphics': False, 'endpoint_keybindings': False, 'mouse_capture': False,
        'surface_active': True, 'surface_reuse': False, 'surface_delta': False,
        'snapshot_codecs': ['shell.snapshot.v1'], 'surface_codecs': ['shell.surface.v1'],
        'input_codecs': ['shell.input.semantic.v1'], 'blob_codecs': ['shell.blob.v1'],
    }
    deadline = time.monotonic() + TIMEOUT
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(TIMEOUT)
        sock.connect(str(path))
        sock.sendall(frame(CLIENT_CONTROL, 'endpoint.hello.v1', json.dumps(hello)))
        boot_id = None
        while boot_id is None:
            data = read_frame(sock, deadline)
            tag, index = read_varint(data, 0)
            if tag != SERVER_CONTROL:
                continue
            kind, index = read_bytes(data, index)
            body, index = read_bytes(data, index)
            if kind == b'endpoint.welcome.v1':
                error = json.loads(body).get('error')
                if error:
                    raise ValueError(error.get('message', 'Herdr rejected this client'))
            elif kind == b'shell.snapshot.v1':
                boot_id = json.loads(body)['boot_id']
        request = {'id': 'desktop-restore', 'method': 'workspace.focus',
                   'params': {'workspace_id': workspace_id}}
        sock.sendall(frame(CLIENT_REQUEST, boot_id, json.dumps(request)))
        response, final = b'', False
        while not final:
            data = read_frame(sock, deadline)
            tag, index = read_varint(data, 0)
            if tag != SERVER_RESPONSE:
                continue
            _, index = read_bytes(data, index)
            _, index = read_bytes(data, index)
            final = data[index] == 1
            chunk, index = read_bytes(data, index + 1)
            response += chunk
        sock.sendall(frame(CLIENT_DETACH))
    reply = json.loads(response)
    if 'error' in reply:
        raise ValueError(reply['error'].get('message', 'Herdr rejected the request'))


def tab_size(snapshot, workspace):
    for layout in snapshot['layouts']:
        if layout.get('tab_id') == workspace['active_tab_id']:
            return layout['area']['width'], layout['area']['height']
    return DEFAULT_SIZE


def select(saved):
    """Make saved's workspace the one the next window opens on, and return its name."""
    directory = session_directory(saved)
    try:
        snapshot = api_request(directory / 'herdr.sock', 'session.snapshot')['snapshot']
        workspace = workspace_in_title(saved['title'], snapshot['workspaces'])
        if workspace is None:
            raise Unavailable('no single workspace matches this window title')
        if snapshot['focused_workspace_id'] != workspace['workspace_id']:
            focus_as_client(directory / 'herdr-client.sock', workspace['workspace_id'],
                            tab_size(snapshot, workspace))
    except OSError as error:
        raise Unavailable(f'Herdr server is not reachable: {error}') from error
    except (ValueError, KeyError, TypeError, IndexError) as error:
        raise Unavailable(f'unexpected Herdr response: {error}') from error
    return workspace['label']
