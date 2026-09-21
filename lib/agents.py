#!/usr/bin/env python3
"""Local terminal-agent identity and silent session-hook recording."""
import json
import os
from pathlib import Path
import re
import sys
import time

KINDS = {'codex', 'claude', 'herdr'}
SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def read_process(pid, root=Path('/proc')):
    path = root / str(pid)
    stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'parent': int(stat[1]), 'start': stat[19], 'tty': int(stat[4]),
            'pgrp': int(stat[2]), 'tpgid': int(stat[5]),
            'cmd': [x.decode(errors='replace') for x in (path / 'cmdline').read_bytes().split(b'\0') if x],
            'cwd': os.readlink(path / 'cwd')}


def command(proc):
    argv = proc['cmd']
    if not argv:
        return None
    name = Path(argv[0]).name
    if name in KINDS:
        return name, argv[1:]
    if name in ('node', 'nodejs', 'bun') and len(argv) > 1:
        script = argv[1]
        if '/@openai/codex/' in script and script.endswith('/codex.js'):
            return 'codex', argv[2:]
        if '/@anthropic-ai/claude-code/' in script and script.endswith('/cli.js'):
            return 'claude', argv[2:]
    return None


def descendants(pid, procs):
    found, pending = set(), [pid]
    children = {}
    for child, proc in procs.items():
        children.setdefault(proc['parent'], []).append(child)
    while pending:
        current = pending.pop()
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def process_env(pid):
    # Never copy credentials or the agent's full environment into checkpoints.
    wanted = {b'CODEX_HOME', b'CLAUDE_CONFIG_DIR', b'HERDR_CONFIG_PATH', b'XDG_CONFIG_HOME'}
    try:
        return {key.decode(): value.decode() for entry in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
                if b'=' in entry for key, value in [entry.split(b'=', 1)] if key in wanted}
    except (OSError, UnicodeError):
        return {}


def record(kind, state, payload):
    """SessionStart/UserPromptSubmit only; never print or store conversation text."""
    if kind not in ('codex', 'claude') or payload.get('hook_event_name') not in ('SessionStart', 'UserPromptSubmit'):
        return
    if payload.get('agent_id') or payload.get('subagent_id'):
        return
    session = payload.get('session_id', '')
    cwd = payload.get('cwd', '')
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session) or not Path(cwd).is_absolute():
        return
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1:
            return
        proc = read_process(pid)
        identified = command(proc)
        if identified and identified[0] == kind:
            data = {'kind': kind, 'pid': pid, 'start': proc['start'], 'boot': BOOT_ID,
                    'session': session, 'cwd': cwd, 'updated': time.time()}
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = state / f'{kind}-{pid}.json'
            temporary = state / f'.{kind}-{pid}-{os.getpid()}.tmp'
            with temporary.open('w') as stream:
                os.chmod(temporary, 0o600)
                json.dump(data, stream)
            temporary.replace(path)
            return
        pid = proc['parent']


def options(args, valued, flags):
    result = []
    index = 0
    while index < len(args):
        value = args[index]
        key = value.split('=', 1)[0]
        if key in valued:
            if '=' in value:
                result.append(value)
            elif index + 1 < len(args):
                result.extend(args[index:index + 2])
                index += 1
        elif value in flags:
            result.append(value)
        index += 1
    return result


def herdr_session(args):
    if '--remote' in args or any(a.startswith('--remote=') for a in args) or '--no-session' in args:
        raise ValueError('Remote and --no-session herdr clients are not supported')
    for index, arg in enumerate(args):
        if arg.startswith('--session='):
            return arg.split('=', 1)[1]
        if arg == '--session' and index + 1 < len(args):
            return args[index + 1]
    if args[:2] == ['session', 'attach'] and len(args) == 3:
        return args[2]
    if not args or all(a in ('--handoff',) for a in args):
        return None
    raise ValueError('Cannot identify this herdr client session')


def terminal_agent(window, procs, state, shared=False):
    candidates = []
    for pid in descendants(window['pid'], procs):
        proc = procs[pid]
        identified = command(proc)
        if not identified:
            continue
        kind, args = identified
        if kind == 'herdr' and args and args[0] in ('server', 'api', 'status', 'integration'):
            continue
        # Exclude non-interactive CLI jobs, tool subprocesses and background jobs.
        if kind == 'codex' and args and args[0] in ('exec', 'review', 'app-server', 'mcp-server'):
            continue
        if kind == 'claude' and any(a in ('-p', '--print', '--background', '--bg') for a in args):
            continue
        if not proc.get('tty') or proc.get('pgrp') != proc.get('tpgid'):
            continue
        candidates.append((pid, kind, args))
    if not candidates:
        return None
    # A herdr client owns its inner agents; never turn a pane into a separate window.
    herdr = [c for c in candidates if c[1] == 'herdr']
    if len(herdr) == 1:
        inner = descendants(herdr[0][0], procs)
        candidates = [c for c in candidates if c[0] not in inner]
    # The npm Codex shim and its native child represent one interactive CLI.
    candidates = [c for c in candidates if not (Path(procs[c[0]]['cmd'][0]).name in ('node', 'nodejs', 'bun')
                  and any(other[1] == c[1] and other[0] in descendants(c[0], procs) for other in candidates if other != c))]
    if shared:
        title = str(Path(os.path.expanduser(window['title'])))
        matches = [c for c in candidates if procs[c[0]]['cwd'] == title]
        # A directory shared by multiple terminal branches is still ambiguous.
        branches = [p for p in procs.values() if p['parent'] == window['pid'] and p['cwd'] == title]
        if len(matches) != 1 or len(branches) != 1:
            raise ValueError('Cannot map agent to a shared-process terminal window; use an independent terminal process')
        candidates = matches
    if len(candidates) != 1:
        raise ValueError('Multiple interactive agents in this terminal; cannot identify the visible session')
    pid, kind, args = candidates[0]
    proc = procs[pid]
    env = process_env(pid)
    if kind == 'herdr':
        session = herdr_session(args)
        argv = ['herdr'] + (['--session', session] if session is not None else [])
        cwd = proc['cwd']
        env = {k: v for k, v in env.items() if k in ('HERDR_CONFIG_PATH', 'XDG_CONFIG_HOME')}
    else:
        if kind == 'codex' and any(a == '--remote' or a.startswith('--remote=') for a in args):
            raise ValueError('Remote Codex sessions are not supported')
        # Newer local Codex frontends may run hooks from a child app-server.
        owners = [pid]
        if kind == 'codex':
            servers = [child for child in descendants(pid, procs)
                       if command(procs[child]) and command(procs[child])[0] == 'codex'
                       and command(procs[child])[1][:1] == ['app-server']]
            if servers:
                owners = servers
        records = []
        for owner in owners:
            try:
                data = json.loads((state / f'{kind}-{owner}.json').read_text())
            except (OSError, ValueError):
                raise ValueError(f'No current {kind} session hook record; restart the agent or submit a prompt after enabling its hooks') from None
            if (not isinstance(data, dict) or data.get('kind') != kind or data.get('pid') != owner or data.get('boot') != BOOT_ID
                    or data.get('start') != procs[owner].get('start')):
                raise ValueError(f'Stale {kind} session hook record; waiting for a fresh event')
            records.append(data)
        if len({(data.get('session'), data.get('cwd')) for data in records}) != 1:
            raise ValueError(f'Multiple {kind} session hook records for this terminal')
        data = records[0]
        session, cwd = data.get('session', ''), data.get('cwd', '')
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session) or not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError(f'Invalid {kind} session hook record')
        if kind == 'codex':
            argv = ['codex', 'resume', session] + options(args,
                {'--profile', '-p', '--model', '-m', '--sandbox', '-s', '--ask-for-approval', '-a'},
                {'--no-alt-screen', '--dangerously-bypass-approvals-and-sandbox'})
            env = {k: v for k, v in env.items() if k == 'CODEX_HOME'}
        else:
            argv = ['claude', '--resume', session] + options(args,
                {'--model', '--permission-mode', '--agent'}, {'--dangerously-skip-permissions'})
            env = {k: v for k, v in env.items() if k == 'CLAUDE_CONFIG_DIR'}
    if env:
        argv = ['env'] + [f'{key}={value}' for key, value in sorted(env.items())] + argv
    return {'kind': kind, 'session': session, 'cwd': cwd, 'argv': argv, 'agent_env': env}


if __name__ == '__main__':
    os.umask(0o077)
    # Hooks are advisory, silent, and bounded by their shell command's timeout.
    try:
        if len(sys.argv) == 4 and sys.argv[1] == 'record':
            record(sys.argv[2], Path(sys.argv[3]), json.loads(sys.stdin.read(1024 * 1024)))
    except Exception:
        pass
