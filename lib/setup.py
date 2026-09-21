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
            raise RuntimeError(f'{key} is already customized. Integrate the power wrapper manually; see docs/setup.md.')
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

    def plan(self):
        if self.receipt.exists():
            receipt = json.loads(self.receipt.read_text())
            if receipt['root'] == str(self.root):
                return None  # an update at the same path needs no config rewrite
            raise RuntimeError('Uninstall the existing Desktop Restore integration before moving it.')
        if (self.config / 'systemd/user/desktop-restore.service').exists():
            raise RuntimeError('The original local helper is installed. Follow docs/setup.md to migrate it first.')
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

        def add(path, after, mode=0o600, block=None):
            if path.is_symlink():
                raise RuntimeError(f'Configuration path is a symlink: {path}')
            before = path.read_text() if path.exists() else None
            if before is not None:
                mode = path.stat().st_mode & 0o777
            if block is None and before is not None:
                raise RuntimeError(f'An unmanaged file already exists: {path}')
            entries.append({'path': str(path), 'before': before, 'after': after,
                            'mode': mode, 'block': block})

        bindings = self.bindings.read_text() if self.bindings.exists() else ''
        block = '\n-- >>> ' + PLUGIN_ID + '\n'
        command = shlex.join([str(helper), 'restore'])
        block += 'o.bind("SUPER + SHIFT + R", "Restore saved desktop windows", ' + json.dumps(command, ensure_ascii=False) + ')\n'
        block += '-- <<< ' + PLUGIN_ID + '\n'
        add(self.bindings, bindings + block, block=block)
        menu, block = menu_block(self.menu.read_text() if self.menu.exists() else '{}\n', self.root / 'bin/power-action')
        add(self.menu, menu, block=block)
        add(self.config / 'systemd/user' / UNIT, unit)
        add(self.config / 'omarchy/hooks/post-boot.d/desktop-restore',
            '#!/bin/sh\n# ' + PLUGIN_ID + '\nexec systemctl --user start ' + UNIT + '\n', 0o700)
        add(self.home / '.local/bin/desktop-restore',
            '#!/bin/sh\n# ' + PLUGIN_ID + '\nexec ' + shlex.quote(str(helper)) + ' "$@"\n', 0o700)
        return entries

    def install(self):
        entries = self.plan()
        if entries is None:
            run(['systemctl', '--user', 'restart', UNIT])
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
            run(['systemctl', '--user', 'start', UNIT])
            write(self.receipt, json.dumps({'root': str(self.root), 'files': entries}, indent=2) + '\n')
        except Exception:
            subprocess.run(['systemctl', '--user', 'stop', UNIT], capture_output=True, timeout=5)
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

    def uninstall(self):
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
            if current == item['after']:
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
    parser.add_argument('command', choices=['install', 'uninstall'])
    args = parser.parse_args()
    try:
        getattr(Setup(), args.command)()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
