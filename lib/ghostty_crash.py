"""Post-exit Ghostty evidence; no live tracing or terminal-stream capture."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time

from restore_diagnostics import core_dump

REPORT_LIMIT = 2 * 1024 * 1024
CORE_WAIT = 30
DEBUGGER_TIMEOUT = 60
MARKER = b'DESKTOP_RESTORE_CRASH_REPORT\n'
CRASH_SIGNALS = {signal.SIGABRT, signal.SIGBUS, signal.SIGFPE, signal.SIGILL,
                 signal.SIGSEGV, signal.SIGSYS, signal.SIGTRAP}


def archive_executable(executable, directory):
    """Keep one exact executable per SHA-256, even if packages are upgraded later."""
    with open(executable, 'rb') as source:
        digest = hashlib.sha256()
        while data := source.read(1024 * 1024):
            digest.update(data)
        identity = digest.hexdigest()
        target = directory.parent / 'binaries' / identity / 'ghostty'
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = target.with_name(f'ghostty.{os.getpid()}.tmp')
            source.seek(0)
            with temporary.open('wb') as destination:
                os.fchmod(destination.fileno(), 0o600)
                shutil.copyfileobj(source, destination)
            temporary.replace(target)
    return {'executable_sha256': identity, 'archived_executable': str(target)}


def debugger_command(metadata, core):
    # Do not load user GDB configuration or scripts embedded in binaries. Hide
    # argument values; the report deliberately excludes locals and memory dumps.
    arguments = ['--batch', '--nx', '--nh', '--quiet',
                 '-iex', 'set auto-load off', '-iex', 'set debuginfod enabled on',
                 '-ex', 'set pagination off', '-ex', 'set print frame-arguments none',
                 '-ex', 'set print entry-values no']
    if metadata.get('archived_executable'):
        executable = metadata['archived_executable'].replace('\\', '\\\\').replace('"', '\\"')
        arguments.extend(['-ex', f'file "{executable}"'])
    for command in ('echo ' + MARKER.decode().strip() + '\\n',
                    'info threads', 'thread apply all bt 24', 'info registers',
                    'info symbol $pc', 'info line *$pc', 'x/24i $pc-32',
                    'info sharedlibrary', 'info proc mappings'):
        arguments.extend(['-ex', command])
    return ['coredumpctl', '--quiet', '--no-pager', '--debugger=gdb',
            # systemd also unescapes backslashes inside single quotes. Use
            # double-quoted, escaped arguments rather than shell shlex quoting.
            '--debugger-arguments=' + ' '.join(json.dumps(arg, ensure_ascii=False) for arg in arguments), 'debug',
            f"COREDUMP_PID={metadata['pid']}",
            '_BOOT_ID=' + metadata['boot_id'].replace('-', ''),
            'COREDUMP_TIMESTAMP=' + core['COREDUMP_TIMESTAMP']]


def debugger_report(command, path, timeout=DEBUGGER_TIMEOUT):
    """Bound wall time and stored output; discard GDB's initial argv/core banner."""
    env = {**os.environ, 'DEBUGINFOD_TIMEOUT': '5'}
    env.setdefault('DEBUGINFOD_URLS', 'https://debuginfod.archlinux.org')
    started = time.monotonic()
    begun, pending, written, truncated, timed_out = False, b'', 0, False, False
    with path.open('wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        try:
            with child.stdout, selectors.DefaultSelector() as selector:
                os.set_blocking(child.stdout.fileno(), False)
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(timeout=min(0.2, remaining)):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fd)
                            continue
                        if not begun:
                            pending += data
                            index = pending.find(MARKER)
                            if index < 0:
                                pending = pending[-len(MARKER):]
                                continue
                            begun = True
                            data, pending = pending[index + len(MARKER):], b''
                        kept = data[:max(0, REPORT_LIMIT - written)]
                        stream.write(kept)
                        written += len(kept)
                        truncated |= len(kept) < len(data)
        finally:
            if timed_out:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
    return {'status': 'timeout' if timed_out else ('collected' if begun else 'debugger_failed'),
            'debugger_returncode': child.returncode, 'truncated': truncated,
            'bytes': written, 'file': str(path)}


def collect(directory, metadata):
    """Run only after a crash signal, including crashes before mapping or hours later."""
    if metadata.get('returncode', 0) >= 0 or -metadata['returncode'] not in CRASH_SIGNALS:
        return None
    missing = [name for name in ('coredumpctl', 'gdb') if not shutil.which(name)]
    if missing:
        return {'status': 'tools_missing', 'tools': missing}
    since = datetime.fromisoformat(metadata['started']).timestamp()
    until = datetime.fromisoformat(metadata['exited']).timestamp()
    deadline = time.monotonic() + CORE_WAIT
    while True:
        core = core_dump(metadata['pid'], since)
        if core and 'COREDUMP_TIMESTAMP' in core:
            timestamp = int(core['COREDUMP_TIMESTAMP']) / 1000000
            # systemd's crash timestamp may be rounded down to the whole second.
            if (int(since) <= timestamp <= until
                    and core.get('COREDUMP_EXE') == metadata['executable']
                    and core.get('_BOOT_ID') == metadata['boot_id'].replace('-', '')):
                result = debugger_report(debugger_command(metadata, core), directory / 'crash.txt')
                return {**result, 'core': core}
        if time.monotonic() >= deadline:
            return {'status': 'core_unavailable'}
        time.sleep(0.5)
