"""Native terminal launch commands; never substitute a different emulator."""
from pathlib import Path

TERMINALS = {'ghostty', 'kitty', 'alacritty', 'foot', 'footclient', 'wezterm', 'wezterm-gui'}


def terminal_launch(executable, window, cwd, argv):
    name = Path(executable).name
    app_id = window['class']
    if name == 'ghostty':
        return ['ghostty', '--gtk-single-instance=false', '--class=' + app_id,
                '--working-directory=' + cwd] + (['-e'] + argv if argv else [])
    if name in ('foot', 'footclient'):
        # A standalone foot needs no surviving server/socket after reboot.
        return ['foot', '--app-id=' + app_id, '--working-directory=' + cwd] + (['-e'] + argv if argv else [])
    if name == 'kitty':
        return ['kitty', '--class', app_id, '--directory', cwd] + argv
    if name == 'alacritty':
        return ['alacritty', '--class', app_id, '--working-directory', cwd] + (['-e'] + argv if argv else [])
    if name in ('wezterm', 'wezterm-gui'):
        return ['wezterm', 'start', '--always-new-process', '--class', app_id,
                '--cwd', cwd] + (['--'] + argv if argv else [])
    raise ValueError(f'Unsupported terminal executable: {name or "unknown"}')
