#!/usr/bin/env python3
"""Desktop checkpoints for Omarchy/Hyprland 0.55+ (Python standard library)."""
import argparse
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
import time
import urllib.parse
from agents import read_process, terminal_agent
from terminals import TERMINALS, terminal_launch

HOME = Path.home()
STATE = Path(os.environ.get('XDG_STATE_HOME', HOME / '.local/state')) / 'desktop-restore'
SHELLS = {'bash', 'zsh', 'fish', 'sh', 'nu'}
BROWSERS = {
    'google-chrome': 'google-chrome-stable', 'chromium': 'chromium',
    'brave-browser': 'brave', 'firefox': 'firefox', 'zen': 'zen-browser',
}


def run(args, timeout=10):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
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
            return
        latest = read_json(STATE / 'latest.json')
        shutdown = read_json(STATE / 'shutdown.json')
        if shutdown and shutdown.get('instance') == meta.get('instance'):
            latest = shutdown
        if latest and latest['windows']:
            write_json(STATE / 'restore.json', latest)
        write_json(STATE / 'instance.json', {'instance': instance()})


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


def sessions():
    result, cursor = [], None
    for _ in range(50):
        query = {'limit': 100}
        if cursor:
            query['cursor'] = cursor
        page = json.loads(run(['opencode2', 'api', 'get', '/api/session?' + urllib.parse.urlencode(query)], 20))
        result.extend(page['data'])
        cursor = (page.get('cursor') or {}).get('next')
        if not cursor or not page['data']:
            return result
    raise RuntimeError('Too many OpenCode sessions to identify windows reliably')


def session_for_title(title, available):
    label = title.split('OC | ', 1)[1].strip()
    truncated = label.endswith('…')
    prefix = label[:-1] if truncated else label
    matches = [s for s in available if (s['title'].startswith(prefix) if truncated else s['title'] == label)]
    if len(matches) != 1:
        raise ValueError(f'OpenCode title matches {len(matches)} sessions; rename it uniquely and save again')
    return matches[0]


def desktop_apps():
    apps = {}
    roots = [Path(os.environ.get('XDG_DATA_HOME', HOME / '.local/share'))]
    roots += [Path(p) for p in os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':')]
    for root in roots:
        for path in sorted((root / 'applications').glob('*.desktop')):
            config = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                config.read(path)
                entry = config['Desktop Entry']
                if entry.get('Hidden') == 'true' or entry.get('Terminal') == 'true':
                    continue
                command = shlex.split(entry.get('Exec', ''))
                keys = [path.stem, entry.get('StartupWMClass', '')]
                if command:
                    keys.append(Path(command[0]).name)
                for key in keys:
                    if key:
                        apps.setdefault(key.lower(), str(path))
            except (configparser.Error, ValueError, KeyError):
                continue
    return apps


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


def capture(fast=False):
    clients = hypr('clients')
    procs = processes()
    apps = desktop_apps()
    available, session_error = [], None
    if any('OC | ' in c['title'] for c in clients):
        try:
            if fast:
                available = read_json(STATE / 'sessions-cache.json', [])
            else:
                available = sessions()
                write_json(STATE / 'sessions-cache.json', [
                    {key: s[key] for key in ('id', 'title', 'location')} for s in available
                ])
        except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
            session_error = str(error)
    monitors = {m['id']: m['name'] for m in hypr('monitors')}
    windows = []
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
                direct_agents = [p for p in children(c['pid'], procs)
                                  if Path(p['cmd'][0]).name == 'opencode2']
                try:
                    agent = terminal_agent(c, procs, STATE / 'agents',
                                           shared=sum(other['pid'] == c['pid'] for other in clients) > 1,
                                           siblings=[other for other in clients if other['pid'] == c['pid']])
                except ValueError:
                    w['kind'] = 'agent-unresolved'
                    raise
                if agent:
                    w.update(agent)
                elif 'OC | ' in c['title']:
                    if session_error:
                        raise ValueError(session_error)
                    session = session_for_title(c['title'], available)
                    w.update(kind='opencode', session=session['id'], cwd=session['location']['directory'])
                    w['argv'] = ['opencode2', '--session', w['session'], w['cwd']]
                    # Preserve the existing launcher's approval mode only when identifiable.
                    if len(direct_agents) == 1 and '--auto' in direct_agents[0]['cmd']:
                        w['argv'].insert(1, '--auto')
                elif c['title'] == 'OpenCode' and len(direct_agents) == 1:
                    agent = direct_agents[0]
                    w.update(kind='opencode-home', cwd=agent['cwd'])
                    w['argv'] = ['opencode2'] + (['--auto'] if '--auto' in agent['cmd'] else []) + [w['cwd']]
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
                desktop = apps.get(c['class'].lower()) or apps.get(executable.lower())
                if not desktop:
                    raise ValueError('No desktop launcher found for this application')
                w['launch'] = ['gio', 'launch', desktop]
        except (ValueError, KeyError) as error:
            w['error'] = str(error)
        windows.append(w)
    return {'version': 1, 'instance': instance(), 'saved': time.time(), 'windows': windows}


def save(snapshot):
    if not snapshot['windows']:
        return
    with lock('state'):
        write_json(STATE / 'latest.json', snapshot)
        if not (STATE / 'restore.json').exists():
            write_json(STATE / 'restore.json', snapshot)


def save_shutdown(if_shutting_down=False):
    """Best effort only; caller enforces a process-group-wide 700 ms KILL deadline."""
    if if_shutting_down:
        preparing = run(['busctl', '--system', 'get-property', 'org.freedesktop.login1',
                         '/org/freedesktop/login1', 'org.freedesktop.login1.Manager',
                         'PreparingForShutdown'], timeout=0.15)
        if preparing != 'b true':
            return
        # The menu saved before closing windows. Never replace that with teardown.
        saved = read_json(STATE / 'shutdown.json', {})
        if saved.get('instance') == instance():
            return
    # No initialization/rotation, waiting for locks, OpenCode requests, or notifications.
    with lock('restore', blocking=False), lock('state', blocking=False):
        snapshot = capture(fast=True)
        if not snapshot['windows']:
            return
        if any(('OC | ' in w['title'] or w.get('kind') == 'agent-unresolved') and w.get('error') for w in snapshot['windows']):
            return  # retain the previous checkpoint rather than losing conversations
        write_json(STATE / 'shutdown.json', snapshot)


def identity(w):
    if w['kind'] == 'browser':
        return ('browser', w['class'], w.get('browser_group'))
    if w['kind'] in ('codex', 'claude', 'herdr'):
        session = w.get('session') or ('default' if w['kind'] == 'herdr' else None)
        return (w['kind'], session, tuple(sorted(w.get('agent_env', {}).items())))
    if w['kind'] == 'opencode':
        return ('opencode', w.get('session'))
    if w['kind'] in ('terminal', 'opencode-home'):
        return (w['kind'], w['class'], w.get('cwd'))
    return (w['kind'], w['class'])


def existing_window(saved, live, claimed, same_instance=False):
    candidates = [w for w in live if w['address'] not in claimed]
    if same_instance:
        exact = next((w for w in candidates if w['address'] == saved['address'] and w['pid'] == saved['pid']), None)
        if exact:
            return exact
    candidates = [w for w in candidates if identity(w) == identity(saved)]
    if saved['kind'] == 'opencode' and not saved.get('session'):
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


def restore(snapshot):
    live = capture()['windows']
    claimed, launched_browsers, errors = set(), {}, []
    same = snapshot['instance'] == instance()
    restored = already = 0
    mapping_path = STATE / 'restored-windows.json'
    mapping = read_json(mapping_path, {})
    if mapping.get('instance') != instance():
        mapping = {'instance': instance(), 'windows': {}}
    try:
        for saved in snapshot['windows']:
            label = f"Workspace {saved['workspace']}: {saved['title']}"
            remembered = mapping['windows'].get(saved['key'], {})
            actual = next((w for w in live if w['address'] == remembered.get('address')
                           and w['pid'] == remembered.get('pid') and w['address'] not in claimed), None)
            actual = actual or existing_window(saved, live, claimed, same)
            if actual:
                claimed.add(actual['address'])
                # Do not move a window the user has already reopened or rearranged.
                already += 1
                continue
            if saved.get('error'):
                errors.append(label + ': ' + saved['error'])
                continue
            try:
                before = {w['address'] for w in hypr('clients')}
                group = saved.get('browser_group')
                if not group or group not in launched_browsers:
                    if group and any(w.get('browser_group') == group for w in live):
                        raise RuntimeError('Browser is already open; restore its missing windows from History')
                    launch(saved)
                    if group:
                        launched_browsers[group] = before
                before = launched_browsers.get(group, before)
                actual, moved = wait_for_window(saved, before, claimed, group in launched_browsers)
                claimed.add(actual['address'])
                # Remember the launch before optional placement. A compositor
                # error must not make the next restore launch the window again.
                mapping['windows'][saved['key']] = {k: actual[k] for k in ('address', 'pid')}
                write_json(mapping_path, mapping)
                restored += 1
                # Keep metadata for matching the rest of this restore without another API call.
                live.append({**saved, 'address': actual['address'], 'pid': actual['pid']})
                if not moved:
                    place(saved, actual)
                print('RESTORED ' + label, flush=True)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                errors.append(label + ': ' + str(error))
    finally:
        # Persist partial progress even if restoration is interrupted. Never
        # refocus the starting window: the user may have selected a restored one.
        # Initial focus protection belongs to the pre-map launch rules instead.
        write_json(STATE / 'last-result.json', {'restored': restored, 'already_open': already, 'errors': errors})
    report(f'Reopened {restored}; already open {already}; skipped/failed {len(errors)}.')
    for error in errors:
        print(error, file=sys.stderr)
    return not errors


def signature(snapshot):
    # Ignore frequently-changing browser titles; track terminal directories and sessions.
    return [(w['address'], w['workspace'], w['at'], w['size'], w['floating'], w['fullscreen'],
             w.get('session'), w.get('cwd'), w.get('error')) for w in snapshot['windows']]


def watch():
    """Debounce closing windows so Omarchy's 2-second shutdown preserves a full snapshot."""
    with lock('watch', blocking=False):
        previous, changed = None, time.monotonic()
        while True:
            try:
                with lock('restore', blocking=False):
                    snapshot = capture()
                    current = signature(snapshot)
                    if current != previous:
                        previous, changed = current, time.monotonic()
                    elif time.monotonic() - changed >= 20:
                        save(snapshot)
            except BlockingIOError:
                previous = None
            except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
                print(f'Checkpoint delayed: {error}', file=sys.stderr, flush=True)
                previous = None
            time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['save-shutdown', 'restore', 'watch'])
    parser.add_argument('--if-shutting-down', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'save-shutdown':
        # Failure is deliberately silent and successful from the shutdown caller's view.
        try:
            save_shutdown(if_shutting_down=args.if_shutting_down)
        except Exception:
            pass
        return 0
    initialize()
    if args.command == 'watch':
        watch()
    else:
        snapshot = read_json(STATE / 'restore.json')
        if not snapshot:
            raise RuntimeError('No automatic desktop checkpoint is available yet.')
        with lock('restore', blocking=False):
            return 0 if restore(snapshot) else 1
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
