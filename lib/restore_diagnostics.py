"""Bounded post-launch observations; never collect terminal output or environments."""
import json
import os
from pathlib import Path
import subprocess
import time

import restore_log

WINDOW_SECONDS = 120
CORE_GRACE_SECONDS = 30


def process(pid):
    path = Path('/proc') / str(pid)
    try:
        stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'start_ticks': stat[19], 'state': stat[0],
                'executable': os.readlink(path / 'exe')}
    except (OSError, ValueError, IndexError):
        return None


def registration(saved, actual):
    return {'id': f"window-{saved['key']}-{time.time_ns():x}",
            'started': time.monotonic(), 'wall_time': time.time(),
            'kind': saved['kind'], 'window_class': saved['class'],
            'workspace': saved['workspace'], 'process': process(actual['pid'])}


def core_dump(pid, since):
    """Read only crash metadata, not MESSAGE (which may contain argv or memory)."""
    try:
        result = subprocess.run(
            ['journalctl', '-b', '--since', f'@{since:.6f}', '--no-pager', '-o', 'json',
             '-n', '1', f'COREDUMP_PID={pid}'],
            capture_output=True, text=True, timeout=2)
        if result.returncode or not result.stdout.strip():
            return None
        row = json.loads(result.stdout.strip().splitlines()[-1])
        return {key: row[key] for key in (
            'COREDUMP_PID', 'COREDUMP_SIGNAL', 'COREDUMP_SIGNAL_NAME', 'COREDUMP_EXE',
            'COREDUMP_TIMESTAMP', 'COREDUMP_FILENAME', '_BOOT_ID') if key in row}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


class Observer:
    def __init__(self, state):
        self.state = state
        self.observations = {}

    def poll(self, mapping, clients):
        now = time.monotonic()
        if not any(0 <= now - w['diagnostic']['started'] <= WINDOW_SECONDS + CORE_GRACE_SECONDS
                   for w in mapping.get('windows', {}).values() if w.get('diagnostic')):
            self.observations.clear()
            return
        self.observe(mapping, clients())

    def observe(self, mapping, clients):
        now = time.monotonic()
        addresses = {(c['address'], c['pid']) for c in clients}
        active = set()
        for key, window in mapping.get('windows', {}).items():
            diagnostic = window.get('diagnostic')
            if not diagnostic:
                continue
            age = now - diagnostic['started']
            if age < 0 or age > WINDOW_SECONDS + CORE_GRACE_SECONDS:
                continue
            identity = diagnostic['id']
            active.add(identity)
            observed = self.observations.setdefault(identity, {})

            def event(name, **fields):
                restore_log.record(self.state, identity, name, key=key,
                                   window_pid=window['pid'], address=window['address'],
                                   kind=diagnostic['kind'], window_class=diagnostic['window_class'],
                                   seconds_since_restore=round(age, 3), **fields)

            if age <= WINDOW_SECONDS:
                current = process(window['pid'])
                original = diagnostic.get('process')
                alive = bool(current and original and current['start_ticks'] == original['start_ticks']
                             and current['state'] != 'Z')
                status = ((window['address'], window['pid']) in addresses, alive)
                if observed.get('status') != status:
                    event('restored_window_observed', window_present=status[0], process_alive=alive,
                          process=current, initial_process=original)
                    observed['status'] = status
                if not all(status):
                    observed['missing'] = True
            # Coredump journal records can arrive after the window disappears.
            if observed.get('missing') and not observed.get('core'):
                core = core_dump(window['pid'], diagnostic['wall_time'])
                if core:
                    event('restored_process_coredump', core=core)
                    observed['core'] = True
            if age > WINDOW_SECONDS and not observed.get('finished'):
                event('restore_observation_finished', last_status=observed.get('status'),
                      coredump_found=bool(observed.get('core')))
                observed['finished'] = True
        self.observations = {key: value for key, value in self.observations.items() if key in active}
