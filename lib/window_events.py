"""Read-only Hyprland open/close wakeups, with polling after IPC failures."""
import os
from pathlib import Path
import select
import socket
import time


class WindowEvents:
    def __init__(self):
        self.socket = None
        self.buffer = b''

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    def connect(self):
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
        path = runtime / 'hypr' / os.environ.get('HYPRLAND_INSTANCE_SIGNATURE', '') / '.socket2.sock'
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(1)
            sock.connect(str(path))
            sock.setblocking(False)
            self.socket = sock
        except OSError:
            sock.close()

    def disconnect(self):
        if self.socket is not None:
            self.socket.close()
        self.socket, self.buffer = None, b''

    def wait(self, timeout):
        """Wake on open or close; return True only for close/reconciliation."""
        if self.socket is None:
            self.connect()
            if self.socket is None:
                time.sleep(timeout)
                return False
            return True  # Reconcile changes missed while disconnected.
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = max(0, deadline - time.monotonic())
                if not select.select([self.socket], [], [], remaining)[0]:
                    return False
                data = self.socket.recv(65536)
                if not data:
                    self.disconnect()
                    return True
                lines = (self.buffer + data).split(b'\n')
                self.buffer = lines.pop()
                if len(self.buffer) > 65536:
                    self.disconnect()
                    return True
                if any(line.startswith(b'closewindow>>') for line in lines):
                    return True
                if any(line.startswith(b'openwindow>>') for line in lines):
                    return False  # Prompt a census without forcing a close-event save.
                if time.monotonic() >= deadline:
                    return False
        except (OSError, ValueError):
            self.disconnect()
            return True
