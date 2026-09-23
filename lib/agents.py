#!/usr/bin/env python3
"""Local terminal-agent identity and silent session-hook recording."""
import json
import os
from pathlib import Path
import re
import sys
import time

KINDS = {'codex', 'claude', 'herdr'}
OPENCODE = {'opencode': 'opencode1', 'opencode2': 'opencode'}
SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id').read_text().strip()


class SharedWindowAmbiguity(ValueError):
    """The terminal is known, but its window-to-shell association is not."""


class AgentIdentityError(ValueError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


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
    if name in OPENCODE:
        return OPENCODE[name], argv[1:]
    if name in KINDS:
        return name, argv[1:]
    if name in ('node', 'nodejs', 'bun') and len(argv) > 1:
        script = argv[1]
        if '/@openai/codex/' in script and script.endswith('/codex.js'):
            return 'codex', argv[2:]
        if '/@anthropic-ai/claude-code/' in script and script.endswith('/cli.js'):
            return 'claude', argv[2:]
        if '/opencode-ai/' in script and script.endswith('/bin/opencode'):
            return 'opencode1', argv[2:]
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
    wanted = {b'CODEX_HOME', b'CLAUDE_CONFIG_DIR', b'HERDR_CONFIG_PATH', b'XDG_CONFIG_HOME',
              b'HERDR_SESSION', b'HERDR_SOCKET_PATH', b'XDG_DATA_HOME',
              b'OPENCODE_CONFIG', b'OPENCODE_CONFIG_DIR'}
    try:
        result = {}
        for entry in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0'):
            key, separator, value = entry.partition(b'=')
            if separator and key in wanted:
                result[key.decode()] = value.decode()
        return result
    except (OSError, UnicodeError):
        return {}


def valid_session(session, cwd):
    return (isinstance(session, str) and SESSION_ID.fullmatch(session) is not None
            and isinstance(cwd, str) and Path(cwd).is_absolute())


def recorded_session(kind, owners, procs, state):
    """Resolve one identity from live-process hook records, never stale argv."""
    identities = set()
    for pid in owners:
        try:
            data = json.loads((state / f'{kind}-{pid}.json').read_text())
        except (OSError, ValueError):
            raise ValueError(f'No current {kind} session hook record; restart the agent or submit a prompt after enabling its hooks') from None
        if (not isinstance(data, dict) or data.get('kind') != kind or data.get('pid') != pid
                or data.get('boot') != BOOT_ID or data.get('start') != procs[pid].get('start')):
            raise ValueError(f'Stale {kind} session hook record; waiting for a fresh event')
        if not valid_session(data.get('session'), data.get('cwd')):
            raise ValueError(f'Invalid {kind} session hook record')
        identities.add((data['session'], data['cwd']))
    if len(identities) != 1:
        raise ValueError(f'Multiple {kind} session hook records for this terminal')
    return identities.pop()


def record(kind, state, payload):
    """SessionStart/UserPromptSubmit only; never print or store conversation text."""
    if kind not in ('codex', 'claude') or payload.get('hook_event_name') not in ('SessionStart', 'UserPromptSubmit'):
        return
    if payload.get('agent_id') or payload.get('subagent_id'):
        return
    session = payload.get('session_id', '')
    cwd = payload.get('cwd', '')
    if not valid_session(session, cwd):
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
        if value == '--':
            break
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


def herdr_session(args, env=None):
    """Follow herdr 0.8.2 src/session.rs session-selection precedence."""
    env = env or {}
    if '--remote' in args or any(a.startswith('--remote=') for a in args) or '--no-session' in args:
        raise ValueError('Remote and --no-session herdr clients are not supported')
    selected = None
    if args[:2] == ['session', 'attach']:
        if len(args) != 3:
            raise ValueError('Cannot identify this herdr client session')
        selected = args[2]
    else:
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == '--session' and index + 1 < len(args):
                index += 1
                selected = args[index]
            elif arg.startswith('--session='):
                selected = arg.split('=', 1)[1]
            elif arg != '--handoff':
                raise ValueError('Cannot identify this herdr client session')
            index += 1
    # Explicit names override the inherited socket; a socket-only client cannot
    # be safely converted into a named local session after reboot.
    if selected is None:
        if 'HERDR_SOCKET_PATH' in env:
            raise ValueError('Custom-socket herdr clients require an explicit --session for restoration')
        selected = env.get('HERDR_SESSION', 'default')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', selected) or selected in ('.', '..'):
        raise ValueError('Invalid herdr session name')
    return selected


def first_positional(args, valued):
    """Find a subcommand/project after global options, skipping their values."""
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == '--':
            return None
        elif arg in valued:
            skip = True
        elif not arg.startswith('-'):
            return arg
    return None


def codex_mode(args):
    return first_positional(args, {
        '-c', '--config', '-p', '--profile', '-m', '--model', '-s', '--sandbox',
        '-a', '--ask-for-approval', '-C', '--cd', '--enable', '--disable',
        '-i', '--image', '--local-provider', '--add-dir', '--remote', '--remote-auth-token-env'})


def terminal_roots(pid, procs):
    return {child: proc for child, proc in procs.items()
            if proc['parent'] == pid and proc.get('tty')}


def terminal_branch(window, procs, siblings):
    """Match windows to terminal child branches, not to the shared GUI PID."""
    roots = terminal_roots(window['pid'], procs)
    windows = list(siblings)
    assignments = {}
    choices = {}
    for index, sibling in enumerate(windows):
        title = os.path.expanduser(sibling['title'])
        if Path(title).is_absolute():
            choices[index] = {pid for pid, proc in roots.items() if proc['cwd'] == title}
    # Assign only unique pairs. Two windows/branches with the same directory
    # cannot be distinguished by iteration order.
    for index, possible in choices.items():
        if len(possible) == 1:
            pid = next(iter(possible))
            if sum(pid in values for values in choices.values()) == 1:
                assignments[index] = pid
    remaining_windows = [i for i in range(len(windows)) if i not in assignments]
    remaining_roots = set(roots) - set(assignments.values())
    if len(windows) == len(roots) and len(remaining_windows) == len(remaining_roots) == 1:
        assignments[remaining_windows[0]] = remaining_roots.pop()
    for index, sibling in enumerate(windows):
        if sibling is window or (window.get('address') and sibling.get('address') == window['address']):
            if index in assignments:
                return assignments[index]
            break
    raise SharedWindowAmbiguity('Cannot map agent to a shared-process terminal window')


def opencode_mode(args):
    return first_positional(args, {
        '--session', '-s', '--model', '-m', '--agent', '--prompt', '--port',
        '--hostname', '--log-level', '--password', '--username', '--server'})


def terminal_agent(window, procs, state, shared=False, siblings=None, root=None):
    candidates = []
    roots = terminal_roots(window['pid'], procs)
    terminal_ttys = {proc['tty'] for proc in roots.values()}
    for pid in descendants(window['pid'], procs):
        proc = procs[pid]
        identified = command(proc)
        if not identified:
            continue
        kind, args = identified
        if kind in OPENCODE.values() and opencode_mode(args) in (
                'run', 'serve', 'web', 'acp', 'api', 'session', 'db', 'debug', 'mcp',
                'auth', 'providers', 'models', 'agent', 'upgrade', 'uninstall',
                'completion', 'stats', 'export', 'import', 'github', 'pr', 'plugin'):
            continue
        if kind == 'herdr' and args and args[0] in ('server', 'api', 'status', 'integration'):
            continue
        # Exclude non-interactive CLI jobs, tool subprocesses and background jobs.
        if kind == 'codex' and codex_mode(args) in ('exec', 'e', 'review', 'app-server', 'mcp-server',
                                                  'login', 'logout', 'mcp', 'completion', 'debug'):
            continue
        if kind == 'claude' and any(a in ('-p', '--print', '--background', '--bg') for a in args):
            continue
        if not proc.get('tty') or proc.get('pgrp') != proc.get('tpgid'):
            continue
        if proc['tty'] not in terminal_ttys:
            continue  # A tool's private PTY is not the visible terminal.
        candidates.append((pid, kind, args))
    if not candidates:
        return None
    if root is not None or shared or len(roots) > 1:
        if root is None:
            root = terminal_branch(window, procs, siblings or [window])
        if root not in roots:
            raise ValueError('Terminal shell is no longer present')
        branch = descendants(root, procs) | {root}
        candidates = [c for c in candidates if c[0] in branch]
        if not candidates:
            return None  # This is a plain shell; another window owns the agent.
    # A herdr client owns its inner agents; never turn a pane into a separate window.
    herdr = [c for c in candidates if c[1] == 'herdr']
    if len(herdr) == 1:
        inner = descendants(herdr[0][0], procs)
        candidates = [c for c in candidates if c[0] not in inner]
    # An npm shim and its native child represent one interactive CLI.
    candidates = [c for c in candidates if not (Path(procs[c[0]]['cmd'][0]).name in ('node', 'nodejs', 'bun')
                  and any(other[1] == c[1] and other[0] in descendants(c[0], procs) for other in candidates if other != c))]
    if len(candidates) != 1:
        raise ValueError('Multiple interactive agents in this terminal; cannot identify the visible session')
    pid, kind, args = candidates[0]
    try:
        return agent_session(pid, kind, args, procs, state)
    except ValueError as error:
        raise AgentIdentityError(kind, str(error)) from error


def agent_session(pid, kind, args, procs, state):
    proc = procs[pid]
    env = process_env(pid)
    if kind in OPENCODE.values():
        if opencode_mode(args) == 'attach' or any(
                arg == '--standalone' or arg.split('=', 1)[0] == '--server' for arg in args):
            raise ValueError('Attached, remote and standalone OpenCode servers are not supported')
        env = {k: v for k, v in env.items() if k in (
            'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'OPENCODE_CONFIG', 'OPENCODE_CONFIG_DIR')}
        # Session argv can become stale when the user switches conversations.
        # The caller resolves the current title against this version's metadata.
        return {'kind': kind, 'cwd': proc['cwd'], 'opencode_args': args, 'agent_env': env}
    if kind == 'herdr':
        session = herdr_session(args, env)
        argv = ['herdr', '--session', session]
        cwd = proc['cwd']
        env = {k: v for k, v in env.items() if k in ('HERDR_CONFIG_PATH', 'XDG_CONFIG_HOME')}
    else:
        if kind == 'codex' and any(a == '--remote' or a.startswith('--remote=') for a in args):
            raise ValueError('Remote Codex sessions are not supported')
        # Newer local Codex frontends may run hooks from a child app-server.
        owners = [pid]
        if kind == 'codex':
            servers = [child for child in descendants(pid, procs)
                       if (identified := command(procs[child])) and identified[0] == 'codex'
                       and codex_mode(identified[1]) == 'app-server']
            if servers:
                owners = servers
        session, cwd = recorded_session(kind, owners, procs, state)
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
