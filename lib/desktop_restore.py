#!/usr/bin/env python3
"""Desktop checkpoints for Omarchy/Hyprland 0.55+ (Python standard library)."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import configparser
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from agents import read_process, terminal_agent, valid_session, options, SharedWindowAmbiguity
from shared_agents import capture_group, deduplicate_groups, restore_entries
from terminals import TERMINALS, terminal_launch
from window_events import WindowEvents
from tiling import TiledOrder
import restore_log
import recovery

HOME = Path.home()
STATE = Path(os.environ.get('XDG_STATE_HOME', HOME / '.local/state')) / 'desktop-restore'
SHELLS = {'bash', 'zsh', 'fish', 'sh', 'nu'}
BROWSERS = {
    'google-chrome': 'google-chrome-stable', 'chromium': 'chromium',
    'brave-browser': 'brave', 'firefox': 'firefox', 'zen': 'zen-browser',
}
# Host executables identify the runtime, not an individual plugin's launcher.
SHELL_HOSTS = {'quickshell', 'qs', 'omarchy-shell'}


def run(args, timeout=10, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or str(args))
    return result.stdout.strip()


def hypr(what):
    return json.loads(run(['hyprctl', '-j', what]))


def lua(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    # Lua and JSON share these escapes for ordinary UTF-8 strings.
    return json.dumps(str(value), ensure_ascii=False)


def dispatch(method, **args):
    fields = ', '.join(f'{key} = {lua(value)}' for key, value in args.items())
    answer = run(['hyprctl', 'dispatch', f'hl.dsp.{method}({{ {fields} }})'])
    if answer != 'ok':
        raise RuntimeError(answer)


def report(message):
    print(message, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextlib.contextmanager
def lock(name, blocking=True):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / f'{name}.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def instance():
    value = os.environ.get('HYPRLAND_INSTANCE_SIGNATURE')
    if not value:
        raise RuntimeError('Run this inside your Hyprland desktop session.')
    return value


def initialize():
    """Freeze the previous desktop BEFORE any new-login autosave can overwrite it."""
    with lock('state'):
        meta = read_json(STATE / 'instance.json', {})
        if meta.get('instance') == instance():
            if 'recovery_started' not in meta:
                # Upgrade an existing login without replaying its frozen target.
                progress = read_json(STATE / 'restored-windows.json', {})
                meta['recovery_started'] = (progress.get('instance') == instance()
                                            and bool(progress.get('windows')))
                write_json(STATE / 'instance.json', meta)
            return
        latest = read_json(STATE / 'latest.json')
        shutdown = read_json(STATE / 'shutdown.json')
        if (shutdown and shutdown.get('instance') == meta.get('instance')
                and (not latest or shutdown.get('saved', 0) >= latest.get('saved', 0))):
            latest = shutdown
        protected = read_json(STATE / 'restore.json')
        expired_at_shutdown = (shutdown and shutdown.get('instance') == meta.get('instance')
                               and shutdown.get('recovery_grace_expired'))
        if (meta.get('recovery_policy') == recovery.POLICY and not meta.get('recovery_started')
                and protected is not None and protected.get('instance') != meta.get('instance')
                and not expired_at_shutdown):
            # Rebooting without beginning a new desktop must not replace the
            # protected target with idle-login/autostart windows.
            latest = protected
        if latest is not None:
            write_json(STATE / 'restore.json', latest)
        write_json(STATE / 'instance.json', {'instance': instance(), 'recovery_started': False,
                                           'recovery_policy': recovery.POLICY})


def observe_recovery_windows(clients):
    with lock('state'):
        meta = read_json(STATE / 'instance.json', {})
        if meta.get('instance') != instance() or meta.get('recovery_started'):
            return
        previous = json.dumps(meta, sort_keys=True)
        progress = read_json(STATE / 'restored-windows.json', {})
        restored = progress.get('windows', {}).values() if progress.get('instance') == instance() else ()
        trigger = recovery.observe(meta, clients, restored)
        if previous != json.dumps(meta, sort_keys=True):
            write_json(STATE / 'instance.json', meta)
        if trigger:
            restore_log.record(STATE, 'recovery-' + instance(), 'recovery_timer_started',
                               trigger=trigger, seconds=recovery.GRACE_SECONDS,
                               deadline_boot_seconds=meta['recovery_deadline'])


def recovery_due(meta):
    return meta.get('instance') == instance() and recovery.due(meta)


def processes():
    result = {}
    for path in Path('/proc').glob('[0-9]*'):
        try:
            proc = read_process(int(path.name))
            if proc['cmd']:
                result[int(path.name)] = proc
        except (OSError, ValueError):
            continue
    return result


def children(pid, procs):
    return [p for p in procs.values() if p['parent'] == pid]


def terminal_cwd(window, procs):
    # Ghostty's GTK single-instance process can own several independent shells.
    # Its shell-integration title identifies a window, whereas the GUI PID cannot.
    title = window['title']
    path = Path(os.path.expanduser(title))
    if path.is_absolute() and path.is_dir():
        return str(path)
    shells = [p for p in children(window['pid'], procs)
              if Path(p['cmd'][0]).name in SHELLS]
    if len(shells) == 1 and Path(shells[0]['cwd']).is_dir():
        return shells[0]['cwd']
    raise ValueError('Cannot identify this terminal window’s working directory')


def sessions(env=None):
    result, cursor = [], None
    for _ in range(50):
        query = {'limit': 100}
        if cursor:
            query['cursor'] = cursor
        page = json.loads(run(with_env(
            ['opencode2', 'api', 'get', '/api/session?' + urllib.parse.urlencode(query)], env), 20))
        result.extend(page['data'])
        cursor = (page.get('cursor') or {}).get('next')
        if not cursor or not page['data']:
            return result
    raise RuntimeError('Too many OpenCode sessions to identify windows reliably')


def title_sessions(title, available):
    if not isinstance(available, list) or any(
            not isinstance(s, dict) or not isinstance(s.get('title'), str) for s in available):
        raise ValueError('Invalid OpenCode session metadata')
    label = title.split('OC | ', 1)[1].strip()
    truncated = label.endswith('…')
    prefix = label[:-1] if truncated else label
    return [s for s in available if (s['title'].startswith(prefix) if truncated else s['title'] == label)]


def session_metadata(available):
    """Cache only identity fields; V2 permits sessions without a title."""
    if not isinstance(available, list):
        raise ValueError('Invalid OpenCode session metadata')
    result = []
    for session in available:
        if (not isinstance(session, dict) or not isinstance(session.get('location'), dict)
                or not valid_session(session.get('id'), session['location'].get('directory'))
                or ('title' in session and not isinstance(session['title'], str))):
            raise ValueError('Invalid OpenCode session metadata')
        # An unnamed session cannot match an OC | conversation title. It must
        # not make unrelated, named conversations impossible to capture.
        if 'title' in session:
            result.append({key: session[key] for key in ('id', 'title', 'location')})
    return result


def session_for_title(title, available):
    matches = title_sessions(title, available)
    if len(matches) != 1:
        raise ValueError(f'OpenCode title matches {len(matches)} sessions; keep conversation titles unique')
    return matches[0]


def with_env(argv, env):
    return ['env', *[f'{key}={value}' for key, value in sorted(env.items())], *argv] if env else argv


def opencode_context(env):
    """Treat explicit default XDG paths like older checkpoints without an environment."""
    defaults = {'XDG_CONFIG_HOME': str(HOME / '.config'), 'XDG_DATA_HOME': str(HOME / '.local/share')}
    return {key: value for key, value in env.items() if value != os.environ.get(key, defaults.get(key))}


def sessions_v1(cwd, env):
    # Query the public CLI, not a version-dependent database schema. Request a
    # bounded full listing and reject the cap rather than hiding duplicate titles.
    if not run(with_env(['opencode', '--version'], env), 10, cwd=cwd).startswith('1.'):
        raise ValueError('OpenCode 1 recovery requires opencode version 1.x')
    output = run(with_env(['opencode', 'session', 'list', '--format', 'json',
                           '--max-count', '10000'], env), 20, cwd=cwd)
    data = json.loads(output) if output else []
    if not isinstance(data, list) or len(data) >= 10000:
        raise ValueError('Cannot obtain a complete OpenCode 1 session listing')
    result = []
    for session in data:
        if (not isinstance(session, dict) or not valid_session(session.get('id'), session.get('directory'))
                or not isinstance(session.get('title'), str)):
            raise ValueError('Invalid OpenCode 1 session metadata')
        result.append({'id': session['id'], 'title': session['title'],
                       'location': {'directory': session['directory']}})
    return result


def opencode_window(title, agent, available):
    kind, cwd = agent['kind'], agent['cwd']
    args, env = agent['opencode_args'], agent['agent_env']
    binary = 'opencode' if kind == 'opencode1' else 'opencode2'
    if 'OC | ' in title:
        session = session_for_title(title, available)
        if not valid_session(session.get('id'), session.get('location', {}).get('directory')):
            raise ValueError('Invalid OpenCode session metadata')
        cwd = session['location']['directory']
        result = {'kind': kind, 'session': session['id'], 'cwd': cwd}
        argv = [binary, '--session', session['id'], cwd]
    elif title == 'OpenCode' and kind == 'opencode':
        result = {'kind': 'opencode-home', 'cwd': cwd}
        argv = [binary, cwd]
    else:
        # V1 also uses "OpenCode" for untitled conversations, not just home.
        raise ValueError('OpenCode needs a visible, uniquely named conversation for restoration')
    flags = options(args, {'--model', '-m', '--agent'}, {'--auto'} if kind == 'opencode' else set())
    argv[1:1] = flags
    return {**result, 'argv': with_env(argv, env), 'agent_env': env}


def shared_opencode(agents, windows, available_sessions):
    """Resolve visible titles only when all plausible client settings agree."""
    resolved, errors, choices = [], [], []
    for window in windows:
        if 'OC | ' not in window['title']:
            continue
        possible, owners = {}, set()
        try:
            for index, agent in enumerate(agents):
                # A failed context lookup cannot be interpreted as no match.
                available = available_sessions(agent)
                matches = title_sessions(window['title'], available)
                if not matches:
                    continue
                session = opencode_window(window['title'], agent, available)
                key = json.dumps([identity(session), session['argv'], session['cwd']], sort_keys=True)
                possible[key] = {**session, 'title': window['title']}
                owners.add(index)
            if len(possible) != 1:
                raise ValueError('OpenCode session/context or launch options are ambiguous' if possible
                                 else 'No matching OpenCode session in the local client contexts')
            resolved.append(next(iter(possible.values())))
            choices.append(owners)
        except (ValueError, KeyError) as error:
            errors.append(f"{window['title']}: {error}")
    # A single client cannot justify several different visible conversations.
    # Check a one-to-one assignment exists without pretending to know which
    # assignment is real when clients have identical supported launch settings.
    assigned = {}

    def assign(window, visited):
        for owner in sorted(choices[window]):
            if owner in visited:
                continue
            visited.add(owner)
            if owner not in assigned or assign(assigned[owner], visited):
                assigned[owner] = window
                return True
        return False

    if any(not assign(window, set()) for window in range(len(choices))):
        return [], errors + ['OpenCode titles cannot be assigned to distinct local clients']
    if len(assigned) < len(agents):
        errors.append(f'{len(agents) - len(assigned)} OpenCode clients lack an identifiable visible conversation; '
                      'keep conversation titles unique')
    return resolved, errors


def desktop_apps():
    ids, classes, executables = {}, {}, {}
    seen = set()

    def index(table, key, path):
        if key:
            table.setdefault(key.lower(), set()).add(str(path))
    roots = [Path(os.environ.get('XDG_DATA_HOME', HOME / '.local/share'))]
    roots += [Path(p) for p in os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':')]
    for root in roots:
        for path in sorted((root / 'applications').glob('*.desktop')):
            # A user override, including Hidden=true, masks the system entry.
            if path.name in seen:
                continue
            seen.add(path.name)
            config = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                config.read(path)
                entry = config['Desktop Entry']
                if (entry.get('Type') != 'Application' or entry.get('Hidden') == 'true'
                        or entry.get('Terminal') == 'true'):
                    continue
                command = shlex.split(entry.get('Exec', ''))
                if not command and entry.get('DBusActivatable') != 'true':
                    continue  # Icon/identity-only entries cannot launch a window.
                index(ids, path.stem, path)
                index(classes, entry.get('StartupWMClass', ''), path)
                if command and Path(command[0]).name not in SHELL_HOSTS:
                    index(executables, Path(command[0]).name, path)
            except (configparser.Error, ValueError, KeyError):
                continue
    # Explicit desktop IDs outrank declared WM classes, then executable aliases.
    # Retain ambiguous aliases as None so lookup cannot fall through and guess.
    apps = {}
    for table in (executables, classes, ids):
        apps.update({key: next(iter(paths)) if len(paths) == 1 else None for key, paths in table.items()})
    return apps


def app_launcher(window, executable, apps):
    """Match standard application identity, never window titles or plugin names."""
    window_class = window['class'].lower()
    if window_class == 'org.quickshell':
        raise ValueError('Shared Quickshell window lacks a per-application ID; cannot identify its launcher')
    keys = [window_class]
    if executable not in SHELL_HOSTS:
        keys.append(executable.lower())
    for key in keys:
        if key not in apps:
            continue
        desktop = apps[key]
        if desktop is None:
            raise ValueError(f'Multiple desktop launchers match {key}; cannot identify this application')
        return {'desktop_id': Path(desktop).stem, 'launch': ['gio', 'launch', desktop]}
    raise ValueError('No desktop launcher found for this application')


def resolve_saved_app(saved, apps):
    """Revalidate legacy shared-host entries without retaining hardcoded guesses."""
    saved = dict(saved)
    saved.pop('desktop_id', None)
    saved.pop('match_title', None)
    try:
        saved.update(app_launcher(saved, 'quickshell', apps))
        saved.pop('error', None)
    except ValueError as error:
        saved.pop('launch', None)
        saved['error'] = str(error)
    return saved


def browser_flags(argv):
    """Preserve supported profile flags in both --key=value and --key value forms."""
    result = []
    args = iter(argv)
    for arg in args:
        key, separator, value = arg.partition('=')
        if key in ('--profile-directory', '--user-data-dir'):
            value = value if separator else next(args, '')
            if value:
                result.append(key + '=' + value)
    return result


def capture(fast=False, track_recovery=False):
    clients = hypr('clients')
    if track_recovery:
        observe_recovery_windows(clients)
    procs = processes()
    apps = desktop_apps()
    session_cache = {}

    def available_sessions(agent):
        if agent['kind'] == 'opencode1':
            context = json.dumps([agent['cwd'], agent['agent_env']], sort_keys=True)
            filename = 'sessions-v1-' + hashlib.sha256(context.encode()).hexdigest()[:16] + '.json'
        else:
            context = opencode_context(agent['agent_env'])
            filename = ('sessions-v2-' + hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:16] + '.json'
                        if context else 'sessions-cache.json')
        if filename not in session_cache:
            try:
                if fast:
                    data = read_json(STATE / filename)
                    if data is None:
                        raise ValueError('OpenCode session metadata is not cached for this client context')
                    data = session_metadata(data)
                else:
                    data = (sessions_v1(agent['cwd'], agent['agent_env']) if agent['kind'] == 'opencode1'
                            else sessions(agent['agent_env']))
                    data = session_metadata(data)
                    write_json(STATE / filename, data)
                session_cache[filename] = data
            except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
                session_cache[filename] = str(error)
        data = session_cache[filename]
        if isinstance(data, str):
            raise ValueError(data)
        return data
    monitors = {m['id']: m['name'] for m in hypr('monitors')}
    windows = []
    ambiguous = set()
    for c in sorted(clients, key=lambda c: (c['workspace']['id'], c['at'][0], c['at'][1])):
        if not c.get('mapped') or not c.get('class'):
            continue
        ws = c['workspace']['name']
        if not ws.isdecimal() and not ws.startswith('special:'):
            ws = 'name:' + ws
        w = {k: c[k] for k in ('address', 'pid', 'class', 'title', 'at', 'size', 'floating', 'fullscreen')}
        w.update(workspace=ws, monitor=monitors.get(c['monitor']), kind='app')
        w['key'] = hashlib.sha256((instance() + c['address']).encode()).hexdigest()[:16]
        proc = procs.get(c['pid'], {})
        command = proc.get('cmd', [])
        executable = Path(command[0]).name if command else ''
        try:
            if executable in TERMINALS or 'terminal*' in c.get('tags', []):
                w['kind'] = 'terminal'
                try:
                    agent = terminal_agent(c, procs, STATE / 'agents',
                                           shared=sum(other['pid'] == c['pid'] for other in clients) > 1,
                                           siblings=[other for other in clients if other['pid'] == c['pid']])
                except SharedWindowAmbiguity:
                    ambiguous.add(c['address'])
                    w['kind'] = 'agent-unresolved'
                    raise
                except ValueError:
                    w['kind'] = 'agent-unresolved'
                    raise
                if agent:
                    if agent['kind'] in ('opencode1', 'opencode'):
                        w['kind'] = 'agent-unresolved'
                        available = available_sessions(agent) if 'OC | ' in c['title'] else []
                        w.update(opencode_window(c['title'], agent, available))
                    else:
                        w.update(agent)
                elif 'OC | ' in c['title']:
                    w['kind'] = 'agent-unresolved'
                    raise ValueError('Cannot identify the visible local OpenCode process and version')
                else:
                    w['cwd'] = terminal_cwd(c, procs)
                    w['argv'] = []
                w['terminal'] = executable
                w['launch'] = terminal_launch(executable, c, w['cwd'], w['argv'])
            elif c['class'].lower() in BROWSERS:
                w['kind'] = 'browser'
                binary = BROWSERS[c['class'].lower()]
                if not shutil.which(binary):
                    raise ValueError(f'Browser executable not found: {binary}')
                flags = browser_flags(command[1:])
                w['launch'] = [binary] + flags
                w['launch'] += ['--restore-last-session']
                w['browser_group'] = json.dumps([c['class'], flags])
            else:
                w.update(app_launcher(c, executable, apps))
        except (ValueError, KeyError) as error:
            w['error'] = str(error)
        windows.append(w)
    groups = []
    for pid in sorted({w['pid'] for w in windows if w['address'] in ambiguous}):
        siblings = [c for c in clients if c['pid'] == pid]
        slots = [w for w in windows if w['pid'] == pid and w['address'] in ambiguous]
        group = capture_group(siblings, slots, procs, STATE / 'agents', windows, identity,
                              lambda agents, slots: shared_opencode(agents, slots, available_sessions))
        if group is not None:
            groups.append(group)
            for slot in slots:
                slot['agent_group'] = group['key']
    deduplicate_groups(groups, identity)
    snapshot = {'version': 1, 'instance': instance(), 'saved': time.time(), 'windows': windows}
    if groups:
        snapshot['agent_groups'] = groups
    return snapshot


def save(snapshot, closed=False):
    with lock('state'):
        meta = read_json(STATE / 'instance.json', {})
        expired = recovery_due(meta)
        if expired:
            meta.update(recovery_started=True, recovery_synced=True, recovery_reason='grace_expired')
        active = meta.get('instance') == instance() and meta.get('recovery_started', False)
        if not snapshot['windows'] and not ((closed or expired) and active):
            return
        if active:
            # Once recovery has been attempted, the restore target follows the
            # desktop. Keep only unsuccessful recovery entries as retry work.
            target = {**snapshot, 'windows': list(snapshot['windows'])}
            previous = read_json(STATE / 'restore.json', {'windows': []})
            progress = read_json(STATE / 'restored-windows.json', {})
            completed = progress.get('windows', {}) if progress.get('instance') == instance() else {}
            live = restore_entries(snapshot)
            for entry in ([] if expired else restore_entries(previous)):
                if meta.get('recovery_synced') and not entry.get('pending_restore'):
                    continue
                if entry['key'] in completed or existing_window(entry, live, set()):
                    continue
                pending = {**entry, 'pending_restore': True}
                pending.pop('agent_group', None)  # A standalone retry entry, not a real group slot.
                target['windows'].append(pending)
            snapshot = target
            write_json(STATE / 'restore.json', snapshot)
            meta['recovery_synced'] = True
            write_json(STATE / 'instance.json', meta)
        write_json(STATE / 'latest.json', snapshot)
        if not (STATE / 'restore.json').exists():
            write_json(STATE / 'restore.json', snapshot)
        if expired:
            restore_log.record(STATE, 'recovery-' + instance(), 'recovery_timer_expired',
                               trigger=meta.get('recovery_trigger'), checkpoint_saved=snapshot.get('saved'))


def preparing_for_shutdown():
    return run(['busctl', '--system', 'get-property', 'org.freedesktop.login1',
                '/org/freedesktop/login1', 'org.freedesktop.login1.Manager',
                'PreparingForShutdown'], timeout=0.15) == 'b true'


def checkpoint_paused():
    if preparing_for_shutdown():
        return True
    final = read_json(STATE / 'shutdown.json', {})
    # The power menu saves before it starts closing windows, which can precede
    # logind's shutdown flag. Preserve that full snapshot through teardown.
    return final.get('instance') == instance() and 0 <= time.time() - final.get('saved', 0) < 30


def save_shutdown(if_shutting_down=False):
    """Best effort only; caller enforces a process-group-wide 700 ms KILL deadline."""
    if if_shutting_down:
        if not preparing_for_shutdown():
            return
        # The menu saved before closing windows. Never replace that with teardown.
        saved = read_json(STATE / 'shutdown.json', {})
        latest = read_json(STATE / 'latest.json', {})
        if saved.get('instance') == instance() and saved.get('saved', 0) >= latest.get('saved', 0):
            return
    # No initialization/rotation, waiting for locks, OpenCode requests, or notifications.
    with lock('restore', blocking=False), lock('state', blocking=False):
        snapshot = capture(fast=True)
        if not snapshot['windows']:
            return
        if any(('OC | ' in w['title'] or w.get('kind') == 'agent-unresolved') and w.get('error')
               and not w.get('agent_group') for w in snapshot['windows']):
            return  # retain the previous checkpoint rather than losing conversations
        if any(group['errors'] for group in snapshot.get('agent_groups', [])):
            return  # Placement uncertainty is fine; unknown session identities are not.
        if recovery_due(read_json(STATE / 'instance.json', {})):
            snapshot['recovery_grace_expired'] = True
        write_json(STATE / 'shutdown.json', snapshot)


def identity(w):
    if w['kind'] == 'browser':
        return ('browser', w['class'], w.get('browser_group'))
    if w['kind'] in ('codex', 'claude', 'herdr', 'opencode1'):
        session = w.get('session') or ('default' if w['kind'] == 'herdr' else None)
        return (w['kind'], session, tuple(sorted(w.get('agent_env', {}).items())))
    if w['kind'] == 'opencode':
        return ('opencode', w.get('session'), tuple(sorted(opencode_context(w.get('agent_env', {})).items())))
    if w['kind'] in ('terminal', 'opencode-home'):
        return (w['kind'], w['class'], w.get('cwd'))
    if w['kind'] == 'app' and w['class'].lower() == 'org.quickshell':
        return ('app', w['class'], w['title'])
    return (w['kind'], w['class'])


def existing_window(saved, live, claimed, same_instance=False):
    candidates = [w for w in live if w['address'] not in claimed]
    if same_instance:
        exact = next((w for w in candidates if w['address'] == saved['address'] and w['pid'] == saved['pid']), None)
        if exact:
            return exact
    candidates = [w for w in candidates if identity(w) == identity(saved)]
    if saved['kind'] in ('opencode', 'opencode1') and not saved.get('session'):
        return None
    return next((w for w in candidates if w['title'] == saved['title']), candidates[0] if candidates else None)


def arm_mapping_rule(saved, rules):
    """Temporarily guard activation and desktop-launcher placement."""
    name = 'desktop-restore-spawn-v2-' + hashlib.sha256(saved['class'].encode()).hexdigest()[:16]
    # Reuse one named rule per class. Old timers must not disable a newer launch.
    # Hyprland owns the deadline, so even a killed restore leaves no active rule.
    fields = ', '.join(f'{key} = {lua(value)}' for key, value in rules.items())
    pattern = '^' + re.escape(saved['class']) + '$'
    script = f'''
        _desktop_restore_spawns = _desktop_restore_spawns or {{}}
        local key = {lua(name)}
        local entry = {{}}
        entry.rule = hl.window_rule({{ name = key, enabled = true,
            match = {{ initial_class = {lua(pattern)} }}, {fields} }})
        _desktop_restore_spawns[key] = entry
        hl.timer(function()
            if _desktop_restore_spawns[key] == entry then
                entry.rule:set_enabled(false)
                _desktop_restore_spawns[key] = nil
            end
        end, {{ timeout = 20000, type = "oneshot" }})
    '''
    answer = run(['hyprctl', 'eval', script])
    if answer != 'ok':
        raise RuntimeError(answer)


def launch(saved):
    # Rules are attached to the launch token, before the first window maps.
    # uwsm forwards that token even though the app is launched by systemd.
    rules = {'workspace': saved['workspace'] + ' silent', 'no_initial_focus': True}
    if saved.get('monitor') in {m['name'] for m in hypr('monitors')}:
        rules['monitor'] = saved['monitor'] + ' silent'
    # suppress_event is STATIC: disabling its rule does not undo it on mapped
    # windows. Use the dynamic focus_on_activate property only in the timed rule,
    # so later link clicks can activate the browser normally again.
    temporary = dict(rules) if saved.get('kind') == 'app' else {}
    arm_mapping_rule(saved, {**temporary, 'focus_on_activate': False})
    cmd = shlex.join(['uwsm-app', '--'] + saved['launch'])
    fields = ', '.join(f'{key} = {lua(value)}' for key, value in rules.items())
    answer = run(['hyprctl', 'dispatch', f'hl.dsp.exec_cmd({lua(cmd)}, {{ {fields} }})'])
    if answer != 'ok':
        raise RuntimeError(answer)


def place(saved, actual):
    selector = 'address:' + actual['address']
    target = saved['workspace'].removeprefix('name:')
    if actual['workspace']['name'] != target:
        # Browser session recovery may create several windows from one launch.
        dispatch('window.move', window=selector, workspace=saved['workspace'], follow=False)
    monitors = {m['name']: m['id'] for m in hypr('monitors')}
    if (saved.get('monitor') in monitors and actual.get('monitor') != monitors[saved['monitor']]
            and not saved['workspace'].startswith('special:')):
        dispatch('workspace.move', workspace=saved['workspace'], monitor=saved['monitor'])
    if actual['floating'] != saved['floating']:
        dispatch('window.float', window=selector, action='on' if saved['floating'] else 'off')
    if saved['floating']:
        dispatch('window.resize', window=selector, x=saved['size'][0], y=saved['size'][1])
        if saved.get('monitor') in monitors:
            dispatch('window.move', window=selector, x=saved['at'][0], y=saved['at'][1])
    if actual['fullscreen'] != saved['fullscreen']:
        dispatch('window.fullscreen_state', window=selector, internal=saved['fullscreen'], client=-1, action='set')


def wait_for_window(saved, before, claimed, browser_launched):
    deadline = time.monotonic() + 15
    first_seen, locations = None, {}
    while time.monotonic() < deadline:
        candidates = [c for c in hypr('clients') if c['class'] == saved['class']
                      and c.get('mapped') and not c.get('hidden')
                      and c['address'] not in claimed
                      and c['address'] not in before]
        for candidate in candidates:
            locations.setdefault(candidate['address'], (candidate.get('workspace'), candidate.get('monitor')))
        if candidates:
            first_seen = first_seen if first_seen is not None else time.monotonic()
            exact = next((c for c in candidates if c['title'] == saved['title']), None)
            if exact or time.monotonic() - first_seen > (3 if browser_launched else 0.5):
                actual = exact or candidates[0]
                moved = locations[actual['address']] != (actual.get('workspace'), actual.get('monitor'))
                return actual, moved
        time.sleep(0.2)
    raise RuntimeError('No matching window appeared within 15 seconds')


def restore(snapshot, on_event=None):
    def event(name, saved, **fields):
        if on_event:
            on_event(name, key=saved['key'], kind=saved['kind'], window_class=saved['class'],
                     workspace=saved['workspace'], **fields)

    live = restore_entries(capture())
    claimed, errors = set(), []
    warnings = []
    entries = restore_entries(snapshot)
    for group in snapshot.get('agent_groups', []):
        errors.extend(f"Shared terminal {group['key']}: {error}" for error in group['errors'])
        approximate = sum(w.get('agent_group') == group['key'] and w.get('placement') == 'approximate'
                          for w in entries)
        if approximate:
            warnings.append(f"Shared terminal {group['key']}: {len(group['sessions'])} exact sessions preserved; "
                            f'window placement is approximate for {approximate} session(s)')
    same = snapshot['instance'] == instance()
    restored = already = completed = 0
    mapping_path = STATE / 'restored-windows.json'
    mapping = read_json(mapping_path, {})
    if mapping.get('instance') != instance():
        mapping = {'instance': instance(), 'windows': {}}
    lanes = {}
    apps = None
    for saved in entries:
        if saved['kind'] == 'app' and saved['class'].lower() == 'org.quickshell':
            if apps is None:
                apps = desktop_apps()
            saved = resolve_saved_app(saved, apps)
        lanes.setdefault(saved['class'], []).append(saved)
    progress_lock = threading.Lock()
    tile_order = TiledOrder([saved for lane in lanes.values() for saved in lane], live, hypr, run, lua)

    def restore_class(windows):
        nonlocal restored, already, completed
        # Window discovery and temporary rules are class-based. Keep launches
        # within each class ordered, including a browser's multi-window recovery.
        launched_browsers = {}
        for saved in windows:
            label = f"Workspace {saved['workspace']}: {saved['title']}"
            with progress_lock:
                remembered = mapping['windows'].get(saved['key'], {})
                actual = next((w for w in live if w['address'] == remembered.get('address')
                               and w['pid'] == remembered.get('pid') and w['address'] not in claimed), None)
                actual = actual or existing_window(saved, live, claimed, same)
                if actual:
                    claimed.add(actual['address'])
                    mapping['windows'][saved['key']] = {k: actual[k] for k in ('address', 'pid')}
                    write_json(mapping_path, mapping)
                    # Do not move a window the user has already reopened or rearranged.
                    already += 1
                    event('already_open', saved)
                    continue
                if remembered:
                    # A completed recovery is not undone by later closing its window.
                    completed += 1
                    event('already_completed', saved)
                    continue
                if saved.get('error'):
                    errors.append(label + ': ' + saved['error'])
                    event('window_failed', saved, error=saved['error'])
                    continue
                browser_open = saved.get('browser_group') and any(
                    w.get('browser_group') == saved['browser_group'] for w in live)
                taken = set(claimed)
            try:
                before = {w['address'] for w in hypr('clients')}
                group = saved.get('browser_group')
                if not group or group not in launched_browsers:
                    if browser_open:
                        raise RuntimeError('Browser is already open; restore its missing windows from History')
                    event('launch_requested', saved)
                    launch(saved)
                    if group:
                        launched_browsers[group] = before
                before = launched_browsers.get(group, before)
                actual, moved = wait_for_window(saved, before, taken, group in launched_browsers)
                with progress_lock:
                    claimed.add(actual['address'])
                    # Remember the launch before optional placement. Serialize
                    # progress writes so simultaneous arrivals cannot lose records
                    # or race over write_json's per-process temporary file.
                    mapping['windows'][saved['key']] = {k: actual[k] for k in ('address', 'pid')}
                    write_json(mapping_path, mapping)
                    restored += 1
                    # Match later entries without another session API call.
                    live.append({**saved, 'address': actual['address'], 'pid': actual['pid']})
                event('window_restored', saved, address=actual['address'], window_pid=actual['pid'])
                if not moved:
                    place(saved, actual)
                with progress_lock:
                    ordered = tile_order.placed(saved, actual, moved)
                    if ordered:
                        event('tiled_order_' + ordered['status'], saved,
                              **{key: value for key, value in ordered.items() if key != 'status'})
                print('RESTORED ' + label, flush=True)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                with progress_lock:
                    errors.append(label + ': ' + str(error))
                event('window_failed', saved, error=str(error))

    try:
        # A slow or failed application only holds up its own window class.
        # Bound parallel startup work while allowing independent apps to recover.
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(lanes)))) as pool:
            for _ in pool.map(restore_class, lanes.values()):
                pass
    finally:
        # Persist partial progress even if restoration is interrupted. Never
        # refocus the starting window: the user may have selected a restored one.
        # Initial focus protection belongs to the pre-map launch rules instead.
        result = {'restored': restored, 'already_open': already, 'errors': errors}
        if completed:
            result['already_completed'] = completed
        if warnings:
            result['warnings'] = warnings
        write_json(STATE / 'last-result.json', result)
    report(f'Reopened {restored}; already open {already}; previously recovered {completed}; skipped/failed {len(errors)}.')
    for error in errors:
        print(error, file=sys.stderr)
    for warning in warnings:
        print(warning, file=sys.stderr)
    return not errors


def signature(snapshot):
    # Ignore frequently-changing browser titles; track terminal directories and sessions.
    windows = [(w['address'], w['workspace'], w['at'], w['size'], w['floating'], w['fullscreen'],
                w.get('session'), w.get('cwd'), w.get('error'),
                w.get('title') if w.get('agent_group') else None) for w in snapshot['windows']]
    if snapshot.get('agent_groups'):
        windows.append(json.dumps(snapshot['agent_groups'], sort_keys=True))
    return windows


def watch():
    """Save close events promptly; debounce other changes and exclude shutdown teardown."""
    with lock('watch', blocking=False), WindowEvents() as events:
        previous, changed = None, time.monotonic()
        closed = False
        while True:
            try:
                if checkpoint_paused():
                    previous = None  # Never autosave a partially torn-down desktop.
                    closed = False
                else:
                    with lock('restore', blocking=False):
                        snapshot = capture(track_recovery=True)
                        current = signature(snapshot)
                        meta = read_json(STATE / 'instance.json', {})
                        sync = (meta.get('recovery_started') and not meta.get('recovery_synced')) or recovery_due(meta)
                        if checkpoint_paused():
                            previous, closed = None, False
                        elif closed or sync:
                            save(snapshot, closed=closed or sync)
                            previous, changed, closed = current, time.monotonic(), False
                        elif current != previous:
                            previous, changed = current, time.monotonic()
                        elif time.monotonic() - changed >= 20:
                            save(snapshot)
            except BlockingIOError:
                previous = None
            except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
                print(f'Checkpoint delayed: {error}', file=sys.stderr, flush=True)
                previous = None
            meta = read_json(STATE / 'instance.json', {})
            remaining = recovery.remaining(meta) if meta.get('instance') == instance() else None
            closed = events.wait(min(10, remaining) if remaining else 10) or closed


def invoke_restore(source):
    run_id = f'{time.time_ns():x}-{os.getpid()}'
    start = time.monotonic()

    def event(name, **fields):
        restore_log.record(STATE, run_id, name, **fields)

    event('invoked', source=source, instance=os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'),
          callers=restore_log.caller_chain())
    try:
        initialize()
        with lock('restore', blocking=False):
            if recovery_due(read_json(STATE / 'instance.json', {})):
                # Honour the deadline even if the watcher has not reached its
                # next census yet. Never replay the abandoned pre-boot target.
                save(capture(), closed=True)
            snapshot = read_json(STATE / 'restore.json')
            if not snapshot:
                raise RuntimeError('No automatic desktop checkpoint is available yet.')
            event('started', checkpoint_saved=snapshot.get('saved'), checkpoint_instance=snapshot.get('instance'))
            with lock('state'):
                meta = read_json(STATE / 'instance.json', {})
                meta.update(instance=instance(), recovery_started=True)
                meta.setdefault('recovery_reason', 'restore')
                write_json(STATE / 'instance.json', meta)
            ok = restore(snapshot, on_event=event)
            event('finished', ok=ok, elapsed_seconds=round(time.monotonic() - start, 3),
                  result=read_json(STATE / 'last-result.json', {}))
            return 0 if ok else 1
    except BaseException as error:
        event('busy' if isinstance(error, BlockingIOError) else 'failed',
              error_type=type(error).__name__, error=str(error),
              elapsed_seconds=round(time.monotonic() - start, 3))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['save-shutdown', 'restore', 'watch'])
    parser.add_argument('--if-shutting-down', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--source', choices=['shortcut', 'cli'], default='cli', help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'save-shutdown':
        # Failure is deliberately silent and successful from the shutdown caller's view.
        try:
            save_shutdown(if_shutting_down=args.if_shutting_down)
        except Exception:
            pass
        return 0
    if args.command == 'restore':
        return invoke_restore(args.source)
    initialize()
    if args.command == 'watch':
        watch()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BlockingIOError:
        report('Desktop restore is already running.')
        sys.exit(1)
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
        report(str(error))
        sys.exit(1)
