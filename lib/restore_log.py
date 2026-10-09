"""Private restore invocation diagnostics; no keystrokes, argv or environments."""
from datetime import datetime, timezone
import fcntl
from functools import lru_cache
import json
import os
from pathlib import Path
import sys


@lru_cache(maxsize=1)
def enabled():
    """Opt-in per-user diagnostics; restart the watcher after changing settings."""
    config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    try:
        settings = json.loads((config / 'desktop-restore/settings.json').read_text())
        return isinstance(settings, dict) and settings.get('diagnostics') is True
    except (OSError, ValueError):
        return False


def caller_chain():
    if not enabled():
        return []
    result, pid = [], os.getppid()
    for _ in range(8):
        if pid <= 0 or any(p['pid'] == pid for p in result):
            break
        path = Path('/proc') / str(pid)
        try:
            stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            result.append({'pid': pid, 'parent': int(stat[1]), 'start_ticks': stat[19],
                           'name': (path / 'comm').read_text().strip()})
            pid = int(stat[1])
        except (OSError, ValueError, IndexError):
            result.append({'pid': pid, 'unavailable': True})
            break
    return result


def record(state, run_id, event, **fields):
    if not enabled():
        return
    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        row = {'time': datetime.now(timezone.utc).isoformat(), 'run_id': run_id,
               'pid': os.getpid(), 'event': event, **fields}
        with (state / 'restore-events.jsonl').open('a') as stream:
            os.fchmod(stream.fileno(), 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        print(f'Could not log restore event: {error}', file=sys.stderr)
