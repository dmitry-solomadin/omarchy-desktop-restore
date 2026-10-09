#!/usr/bin/env python3
"""Launch Ghostty with a private, bounded native stderr log and exit metadata."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time

LIMIT = 4 * 1024 * 1024


def append_bounded(stream, data):
    if stream.tell() + len(data) > LIMIT:
        stream.seek(-min(stream.tell(), LIMIT // 2), os.SEEK_END)
        tail = stream.read()
        stream.seek(0)
        stream.truncate()
        stream.write(b'[earlier runtime log truncated]\n' + tail)
    stream.write(data)
    stream.flush()


def version(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
        return result.stdout[:16384] if result.returncode == 0 else f'exit {result.returncode}'
    except (OSError, subprocess.TimeoutExpired) as error:
        return type(error).__name__


def supervise(directory, command, launched=None):
    os.umask(0o077)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = {'started': datetime.now(timezone.utc).isoformat(),
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'kernel': os.uname().release, 'ghostty_build': version([command[0], '+version']),
                'herdr_version': version(['herdr', '--version'])}
    executable = shutil.which(command[0]) or command[0]
    metadata['executable'] = os.path.realpath(executable)
    notes = version(['readelf', '-n', executable])
    metadata['elf_build_id'] = next((line.split('Build ID:', 1)[1].strip()
                                     for line in notes.splitlines() if 'Build ID:' in line), None)
    path = directory / 'metadata.json'

    def save():
        path.write_text(json.dumps(metadata, indent=2) + '\n')

    save()
    started = time.monotonic()
    with (directory / 'runtime.log').open('w+b') as stream:
        # Force stderr logging even when the caller disabled it. This does not
        # enable compiled-out debug messages or record the terminal's PTY stream.
        child = subprocess.Popen(command, stderr=subprocess.PIPE,
                                 env={**os.environ, 'GHOSTTY_LOG': 'stderr'})
        if launched is not None:
            launched.append(child.pid)
        metadata['pid'] = child.pid
        save()
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(number, lambda signum, frame: child.poll() is None and child.send_signal(signum))
        with child.stderr, selectors.DefaultSelector() as selector:
            os.set_blocking(child.stderr.fileno(), False)
            selector.register(child.stderr, selectors.EVENT_READ)
            while True:
                for key, _ in selector.select(timeout=0.2):
                    data = os.read(key.fd, 65536)
                    if data:
                        stamp = datetime.now(timezone.utc).isoformat().encode()
                        append_bounded(stream, b'\n[' + stamp + b'] ' + data)
                    else:
                        selector.unregister(key.fd)
                if child.poll() is not None:
                    # Drain final diagnostics without waiting on inherited pipe
                    # descriptors that a surviving descendant may still hold.
                    while True:
                        try:
                            data = os.read(child.stderr.fileno(), 65536)
                        except BlockingIOError:
                            break
                        if not data:
                            break
                        append_bounded(stream, data)
                    break
        code = child.wait()
    metadata.update(exited=datetime.now(timezone.utc).isoformat(),
                    elapsed_seconds=round(time.monotonic() - started, 3), returncode=code,
                    signal=signal.Signals(-code).name if code < 0 else None)
    save()
    return 128 - code if code < 0 else code


def main(destination, argv):
    launched = []
    try:
        return supervise(destination, argv, launched)
    except OSError as error:
        # Diagnostics must not prevent a restore if its directory is unwritable.
        # Once launched, never start another copy in response to a logging error.
        if not launched:
            print(f'Ghostty diagnostics unavailable: {error}', file=sys.stderr)
            os.execvp(argv[0], argv)
        raise


if __name__ == '__main__':
    sys.exit(main(Path(sys.argv[1]), sys.argv[2:]))
