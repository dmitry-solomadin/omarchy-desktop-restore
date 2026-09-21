#!/usr/bin/env python3
"""Install/remove the explicit desktop integration; never requires root."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

PLUGIN_ID = 'io.github.dmitry-solomadin.desktop-restore'
UNIT = 'omarchy-desktop-restore.service'
LIFECYCLE_UNIT = 'omarchy-desktop-restore-lifecycle.service'
ROOT = Path(__file__).resolve().parents[1]
BEGIN = '// >>> ' + PLUGIN_ID
END = '// <<< ' + PLUGIN_ID


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or str(args))
    return result.stdout.strip()


def jsonc_text(text):
    """Blank comments without changing offsets or touching quoted URLs/escapes."""
    out, i, quoted = list(text), 0, False
    while i < len(text):
        if quoted:
            if text[i] == '\\':
                i += 2
                continue
            if text[i] == '"':
                quoted = False
        elif text[i] == '"':
            quoted = True
        elif text.startswith('//', i) or text.startswith('/*', i):
            end = text.find('\n', i) if text[i + 1] == '/' else text.find('*/', i + 2)
            if end < 0:
                if text[i + 1] == '*':
                    raise ValueError('Unterminated JSONC comment')
                end = len(text)
            elif text[i + 1] == '*':
                end += 2
            for j in range(i, end):
                if text[j] not in '\r\n':
                    out[j] = ' '
            i = end
            continue
        i += 1
    return ''.join(out)


def parse_jsonc(text):
    cleaned = jsonc_text(text)
    # Remove trailing commas only outside strings.
    cleaned = re.sub(r'("(?:\\.|[^"\\])*"|(,)(?=\s*[}\]]))',
                     lambda m: ' ' if m.group(2) else m.group(1), cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSONC object')
    return value


def menu_block(text, wrapper):
    existing = parse_jsonc(text)
    for key in ('system.reboot', 'system.shutdown'):
        if key in existing:
            raise RuntimeError(f'{key} is already customized. Integrate the power wrapper manually; see README.md under Power-menu integration.')
    end = jsonc_text(text).rfind('}')
    last = jsonc_text(text[:end]).rstrip()[-1]
    rows = []
    for action in ('reboot', 'shutdown'):
        path = shlex.quote(str(wrapper))
        # A plugin removed before uninstall cannot break the power menu.
        command = f'if [ -x {path} ]; then exec {path} {action}; fi; exec omarchy-system-{action}'
        icon, label = ('󰜉', 'Reboot') if action == 'reboot' else ('󰐥', 'Shutdown')
        rows.append('  ' + json.dumps('system.' + action) + ': ' +
                    json.dumps({'icon': icon, 'label': label, 'action': command}, ensure_ascii=False) + ',\n')
    block = '\n  ' + BEGIN + '\n' + ('  ,\n' if last not in '{,' else '')
    block += ''.join(rows) + '  ' + END + '\n'
    result = text[:end] + block + text[end:]
    parse_jsonc(result)
    return result, block


def systemd_arg(value):
    # ExecStart is not a shell: escape systemd's own specifiers and expansion.
    return json.dumps(str(value).replace('%', '%%').replace('$', '$$'), ensure_ascii=False)


def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f'.tmp.{os.getpid()}')
    with temporary.open('w') as stream:
        os.chmod(temporary, mode)
        stream.write(text)
    temporary.replace(path)


class Setup:
    def __init__(self, root=ROOT):
        self.root = root
        self.home = Path.home()
        self.config = Path(os.environ.get('XDG_CONFIG_HOME', self.home / '.config'))
        self.state = Path(os.environ.get('XDG_STATE_HOME', self.home / '.local/state')) / 'desktop-restore'
        self.receipt = self.state / 'installation.json'
        self.bindings = self.config / 'hypr/bindings.lua'
        self.menu = self.config / 'omarchy/extensions/omarchy-menu.jsonc'

    def agent_hook_files(self):
        result = []
        paths = {
            'claude': Path(os.environ.get('CLAUDE_CONFIG_DIR', self.home / '.claude')) / 'settings.json',
            'codex': Path(os.environ.get('CODEX_HOME', self.home / '.codex')) / 'hooks.json',
        }
        for kind, path in paths.items():
            if not shutil.which(kind):
                continue
            if path.is_symlink():
                raise RuntimeError(f'Agent hook config is a symlink: {path}')
            before = path.read_text() if path.exists() else None
            config = json.loads(before) if before is not None else {}
            if not isinstance(config, dict) or not isinstance(config.get('hooks', {}), dict):
                raise RuntimeError(f'Invalid hooks object in {path}')
            hooks = config.setdefault('hooks', {})
            helper = shlex.quote(str(self.root / 'lib/agents.py'))
            cmd = ('if [ -f ' + helper + ' ]; then /usr/bin/timeout --signal=KILL 0.5s ' +
                   shlex.join([shutil.which('python3'), str(self.root / 'lib/agents.py'),
                               'record', kind, str(self.state / 'agents')]) +
                   ' >/dev/null 2>&1 || :; fi')
            additions = {}
            for event in ('SessionStart', 'UserPromptSubmit'):
                rows = hooks.setdefault(event, [])
                if not isinstance(rows, list):
                    raise RuntimeError(f'Invalid {event} hooks in {path}')
                group = {'hooks': [{'type': 'command', 'command': cmd, 'timeout': 1}]}
                if group not in rows:
                    rows.append(group)
                additions[event] = group
            result.append((path, json.dumps(config, indent=2) + '\n', additions))
        return result

    @staticmethod
    def remove_agent_hooks(current, item):
        config = json.loads(current)
        hooks = config.get('hooks', {})
        for event, group in item['agent_hooks'].items():
            rows = hooks.get(event, [])
            if group not in rows:
                raise RuntimeError(f'Managed agent hook was edited in {item["path"]}')
            rows.remove(group)
            if not rows:
                hooks.pop(event, None)
        if not hooks:
            config.pop('hooks', None)
        return None if not config and item['before'] is None else json.dumps(config, indent=2) + '\n'

    def lifecycle_files(self):
        # This copy survives deletion of the plugin and can remove its own files.
        cleanup = self.state / 'cleanup.py'
        unit = f'''# Managed by {PLUGIN_ID}
[Unit]
Description=Desktop Restore plugin removal cleanup
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart={systemd_arg(shutil.which('python3'))} {systemd_arg(cleanup)} watch-removal
Environment={json.dumps(('XDG_CONFIG_HOME=' + str(self.config)).replace('%', '%%'), ensure_ascii=False)}
Environment={json.dumps(('XDG_STATE_HOME=' + str(self.state.parent)).replace('%', '%%'), ensure_ascii=False)}
Restart=on-failure
RestartSec=5
TimeoutStopSec=1s
UMask=0077
'''
        hook = '#!/bin/sh\n# ' + PLUGIN_ID + '\nexec systemctl --user start ' + LIFECYCLE_UNIT + ' ' + UNIT + '\n'
        return [(cleanup, Path(__file__).read_text(), 0o600),
                (self.config / 'systemd/user' / LIFECYCLE_UNIT, unit, 0o600),
                (self.config / 'omarchy/hooks/post-boot.d/desktop-restore', hook, 0o700)]

    def refresh_lifecycle(self):
        """Upgrade existing receipts without removing/recreating user integration."""
        receipt = json.loads(self.receipt.read_text())
        changes = []
        for path, text, mode in self.lifecycle_files():
            entry = next((item for item in receipt['files'] if item['path'] == str(path)), None)
            if path.is_symlink() or (path.exists() and (entry is None or path.read_text() != entry['after'])):
                raise RuntimeError(f'Managed content was edited in {path}; preserve it before updating.')
            if entry is None:
                entry = {'path': str(path), 'before': None, 'block': None, 'mode': mode}
                receipt['files'].append(entry)
            entry['after'] = text
            changes.append((path, text, mode))
        for path, text, hooks in self.agent_hook_files():
            entry = next((item for item in receipt['files'] if item['path'] == str(path)), None)
            if entry is None:
                entry = {'path': str(path), 'before': path.read_text() if path.exists() else None,
                         'mode': path.stat().st_mode & 0o777 if path.exists() else 0o600, 'block': None}
                receipt['files'].append(entry)
            elif entry.get('agent_hooks') != hooks:
                raise RuntimeError(f'Agent hook definition changed in {path}; uninstall before updating.')
            elif path.exists():
                current_hooks = json.loads(path.read_text()).get('hooks', {})
                if any(group not in current_hooks.get(event, []) for event, group in hooks.items()):
                    raise RuntimeError(f'Managed agent hook was edited in {path}; preserve it before updating.')
            entry.update(after=text, agent_hooks=hooks)
            changes.append((path, text, entry['mode']))
        for path, text, mode in changes:
            write(path, text, mode)
        write(self.receipt, json.dumps(receipt, indent=2) + '\n')
        run(['systemctl', '--user', 'daemon-reload'])

    def watch_removal(self):
        """Ignore shell unloads; clean up only after the source folder disappears."""
        missing_since = None
        while self.receipt.exists():
            receipt = json.loads(self.receipt.read_text())
            root = Path(receipt['root'])
            if root.is_dir():
                missing_since = None
            elif missing_since is None:
                missing_since = time.monotonic()
            elif time.monotonic() - missing_since >= 5:
                self.uninstall(from_monitor=True)
                return
            time.sleep(1)

    def plan(self):
        if self.receipt.exists():
            receipt = json.loads(self.receipt.read_text())
            if receipt['root'] == str(self.root):
                return None  # an update at the same path needs no config rewrite
            raise RuntimeError('Uninstall the existing Desktop Restore integration before moving it.')
        if (self.config / 'systemd/user/desktop-restore.service').exists():
            raise RuntimeError('An existing desktop-restore.service is installed. Remove its integration before installing this plugin.')
        for command in ('python3', 'ghostty', 'hyprctl', 'uwsm-app', 'gio', 'systemctl', 'timeout', 'busctl'):
            if not shutil.which(command):
                raise RuntimeError('Missing required command: ' + command)
        binds = json.loads(run(['hyprctl', '-j', 'binds']))
        if any(b.get('modmask') == 65 and b.get('key', '').upper() == 'R' for b in binds):
            raise RuntimeError('Super+Shift+R is already bound. Resolve that binding before installation.')
        helper = self.root / 'bin/desktop-restore'
        python = shutil.which('python3')
        engine = self.root / 'lib/desktop_restore.py'
        unit = f'''# Managed by {PLUGIN_ID}
[Unit]
Description=Desktop Restore automatic checkpoints
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart={systemd_arg(python)} {systemd_arg(engine)} watch
ExecStop=-/usr/bin/timeout --signal=KILL 0.7s {systemd_arg(python)} {systemd_arg(engine)} save-shutdown --if-shutting-down
TimeoutStopSec=1s
Restart=on-failure
RestartSec=5
UMask=0077
'''
        entries = []

        def add(path, after, mode=0o600, block=None, hooks=None):
            if path.is_symlink():
                raise RuntimeError(f'Configuration path is a symlink: {path}')
            before = path.read_text() if path.exists() else None
            if before is not None:
                mode = path.stat().st_mode & 0o777
            if block is None and hooks is None and before is not None:
                raise RuntimeError(f'An unmanaged file already exists: {path}')
            entries.append({'path': str(path), 'before': before, 'after': after,
                            'mode': mode, 'block': block})
            if hooks is not None:
                entries[-1]['agent_hooks'] = hooks

        bindings = self.bindings.read_text() if self.bindings.exists() else ''
        block = '\n-- >>> ' + PLUGIN_ID + '\n'
        command = shlex.join([str(helper), 'restore'])
        block += 'o.bind("SUPER + SHIFT + R", "Restore saved desktop windows", ' + json.dumps(command, ensure_ascii=False) + ')\n'
        block += '-- <<< ' + PLUGIN_ID + '\n'
        add(self.bindings, bindings + block, block=block)
        menu, block = menu_block(self.menu.read_text() if self.menu.exists() else '{}\n', self.root / 'bin/power-action')
        add(self.menu, menu, block=block)
        add(self.config / 'systemd/user' / UNIT, unit)
        for path, text, mode in self.lifecycle_files():
            add(path, text, mode)
        add(self.home / '.local/bin/desktop-restore',
            '#!/bin/sh\n# ' + PLUGIN_ID + '\nexec ' + shlex.quote(str(helper)) + ' "$@"\n', 0o700)
        for path, text, hooks in self.agent_hook_files():
            add(path, text, hooks=hooks)
        return entries

    def start(self):
        # A shell service entry point must not recreate integration after uninstall.
        if self.receipt.exists():
            run(['systemctl', '--user', 'start', UNIT, LIFECYCLE_UNIT])

    def install(self):
        entries = self.plan()
        if entries is None:
            self.refresh_lifecycle()
            run(['systemctl', '--user', 'restart', UNIT, LIFECYCLE_UNIT])
            print('Desktop Restore integration already installed; watcher restarted.')
            return
        stamp = str(time.time_ns())
        applied = []
        try:
            for item in entries:
                path = Path(item['path'])
                if item['before'] is not None:
                    shutil.copy2(path, path.with_name(path.name + '.bak.desktop-restore-' + stamp))
                write(path, item['after'], item['mode'])
                applied.append(item)
            run(['hyprctl', 'reload'])
            errors = run(['hyprctl', 'configerrors'])
            if errors:
                raise RuntimeError(errors)
            run(['systemctl', '--user', 'daemon-reload'])
            write(self.receipt, json.dumps({'root': str(self.root), 'files': entries}, indent=2) + '\n')
            run(['systemctl', '--user', 'start', UNIT, LIFECYCLE_UNIT])
        except Exception:
            subprocess.run(['systemctl', '--user', 'stop', UNIT, LIFECYCLE_UNIT], capture_output=True, timeout=5)
            self.receipt.unlink(missing_ok=True)
            for item in reversed(applied):
                path = Path(item['path'])
                if item['before'] is None:
                    path.unlink(missing_ok=True)
                else:
                    write(path, item['before'], item['mode'])
            subprocess.run(['hyprctl', 'reload'], capture_output=True, timeout=5)
            subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, timeout=5)
            raise
        print('Installed. Super+Shift+R restores; reboot/shutdown save silently with a 700 ms cutoff.')

    def uninstall(self, from_monitor=False):
        if not self.receipt.exists():
            print('Desktop Restore integration is not installed.')
            return
        receipt = json.loads(self.receipt.read_text())
        changes = []
        for item in receipt['files']:
            path = Path(item['path'])
            if not path.exists():
                continue
            current = path.read_text()
            if item.get('agent_hooks'):
                updated = self.remove_agent_hooks(current, item)
                if json.loads(updated or '{}') == json.loads(item['before'] or '{}'):
                    updated = item['before']
                changes.append((path, updated, item['mode']))
            elif current == item['after']:
                changes.append((path, item['before'], item['mode']))
            elif item['block'] and item['block'] in current:
                updated = current.replace(item['block'], '', 1)
                if path == self.menu:
                    try:
                        parse_jsonc(updated)
                    except ValueError:
                        # New rows may now follow the managed block's final comma.
                        updated = current.replace(item['block'], ',\n', 1)
                        parse_jsonc(updated)
                changes.append((path, updated, item['mode']))
            else:
                raise RuntimeError(f'Managed content was edited in {path}; preserve those changes before uninstalling.')
        run(['systemctl', '--user', 'stop', UNIT])
        if not from_monitor and (self.config / 'systemd/user' / LIFECYCLE_UNIT).exists():
            run(['systemctl', '--user', 'stop', LIFECYCLE_UNIT])
        for path, text, mode in changes:
            if text is None:
                path.unlink(missing_ok=True)
            else:
                write(path, text, mode)
        run(['systemctl', '--user', 'daemon-reload'])
        run(['hyprctl', 'reload'])
        errors = run(['hyprctl', 'configerrors'])
        if errors:
            raise RuntimeError(errors)
        self.receipt.unlink()
        print('Removed the watcher, startup hook, shortcut and power-menu integration. Checkpoints retained.')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'uninstall', 'start', 'watch-removal'])
    args = parser.parse_args()
    try:
        getattr(Setup(), args.command.replace('-', '_'))()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
