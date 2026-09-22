# Desktop Restore

Save your Omarchy desktop automatically and restore it after reboot with
**Super+Shift+R**. Runs silently in the background, with no bar widget or notifications.

Version **0.5.0** · Omarchy 4 / Lua-based Hyprland · MIT license

[![Checks](https://github.com/dmitry-solomadin/omarchy-desktop-restore/actions/workflows/check.yml/badge.svg)](https://github.com/dmitry-solomadin/omarchy-desktop-restore/actions/workflows/check.yml)

## Features

- **Terminals:** Ghostty, Foot/footclient, Kitty, Alacritty and WezTerm. Reopens
  each in its original emulator and working directory.
- **Agent harnesses:** exact-session resume for OpenCode 2, Claude Code and Codex;
  default and named herdr sessions. OpenCode 1 is not currently supported.
- **Regular application windows:** reopens apps such as Signal and browsers such
  as Chrome, Chromium, Brave, Firefox and Zen, using each app's native recovery.
- **Desktop layout:** numbered, named and special workspaces, connected monitors,
  floating window position/size and fullscreen state.
- **700 ms save limit:** the final power-menu save has a hard cutoff, so saving
  cannot hold up reboot or shutdown beyond 700 ms. Background checkpoints are automatic.

## Requirements

- Omarchy 4 with Lua-based Hyprland and Quickshell. Developed against
  **Omarchy 4.0.4 / Hyprland 0.56.2**; legacy `.conf` setups are unsupported.
- Python 3.10+, `hyprctl`, `uwsm-app`, `gio`, `systemctl`, `busctl` and GNU `timeout`.
  No pip dependencies.
- The terminals and applications you want to restore. OpenCode recovery requires `opencode2`.

## Install

Run inside your Omarchy session:

```sh
omarchy plugin add https://github.com/dmitry-solomadin/omarchy-desktop-restore --enable
```

Enabling the plugin automatically installs its services, shortcut, power-menu
entries and hooks for installed agents. Plugin updates refresh the integration
on the next load; ordinary shell reloads do not reinstall or restart it.
Existing settings are preserved. Setup conflicts appear in the Omarchy shell logs.

If you install a supported agent later, re-enable the plugin or restart the shell
to pick up its hooks. Agent-specific setup below still applies.

## Agent setup

### OpenCode 2

Sessions are identified from their window titles. Keep conversation titles unique;
ambiguous matches are skipped rather than guessed. No additional hooks are needed.

### Claude Code and Codex

Setup adds silent session-ID hooks to `~/.claude/settings.json` and
`~/.codex/hooks.json`, preserving existing settings. Custom `CLAUDE_CONFIG_DIR`
and `CODEX_HOME` paths are respected.

- **Claude Code:** restart existing clients to load the hooks.
- **Codex:** review and trust the hooks in `/hooks`.

Hooks track exact conversation IDs and working directories, not prompts or
responses. Missing, stale or ambiguous records are skipped.

### herdr

Restores default and named sessions. Agent conversations inside panes use herdr's
native session recovery and require the relevant herdr integrations.

## Saving and reboot coverage

The watcher checks every 10 seconds and saves after 20 seconds of stable window
state. Empty desktops do not overwrite checkpoints. On login, the previous
session's checkpoint is kept as the restore target, separate from new autosaves.

The power-menu save runs **before windows close**, uses cached agent metadata,
and stops after **700 ms**. A failed save never blocks the power action.

| Reboot/shutdown path | Coverage |
| --- | --- |
| Omarchy system menu, including Super+Escape or the power key | Bounded save attempt before windows close |
| Plugin's `bin/power-action reboot` or `shutdown` | Same bounded save |
| `omarchy reboot`, `omarchy system reboot`, shutdown equivalents, update prompts, direct `systemctl` commands | Best-effort late save; rolling checkpoint fallback |
| Forced reboot, crash or power loss | Previously saved checkpoint only |

Omarchy currently has no shared pre-shutdown hook, so a fresh save is not guaranteed
for every reboot path. Use the system menu for the most reliable result.

### Power-menu integration

Setup adds managed Reboot/Shutdown entries to
`~/.config/omarchy/extensions/omarchy-menu.jsonc`, preserving their labels and icons.
If the plugin wrapper is missing, they fall back to Omarchy's original commands.

Existing custom power entries are not overwritten. To integrate your own action,
run `desktop-restore save-shutdown` immediately before it.

## Limits

- **Desktop:** no process memory, running jobs, SSH connections, scrollback,
  unsaved buffers, exact tiling ratios or Hyprland groups. Other apps reopen
  through their desktop launchers and rely on their own document recovery.
- **Terminals:** no tabs/splits, external multiplexers or remote WezTerm domains.
  Normal terminal configuration applies; custom launch flags are not replayed.
  Foot server windows reopen as standalone Foot windows. Ambiguous shared-process
  windows are skipped.
- **Agents:** visible local sessions only; no hidden tabs, background jobs or
  remote Codex/OpenCode servers. Supported model/profile/permission options and
  agent home paths are retained, not arbitrary arguments or environment variables.
  Remote, `--no-session` and custom-socket-only herdr clients are unsupported.
- **Browsers:** native session storage is required; private windows are unsupported.
  Multi-window placement is approximate, and profiles sharing a process may be
  indistinguishable. If a profile is already running, restore its missing windows
  through browser History.

## State and troubleshooting

State is stored in `${XDG_STATE_HOME:-~/.local/state}/desktop-restore/`.
`latest.json` is the rolling checkpoint, `shutdown.json` the final save, and
`restore.json` the current restore target. Files are private (`0600`) and include
window titles, directories and session IDs. Prompts, responses and credentials
are not copied; browser tabs remain in the browser's own storage.

The last restore result, including skipped windows and errors, is recorded in
`last-result.json`. For background-service diagnostics:

```sh
systemctl --user status omarchy-desktop-restore.service
journalctl --user -u omarchy-desktop-restore.service
```

## Remove

```sh
omarchy plugin remove io.github.dmitry-solomadin.desktop-restore
```

Within about six seconds, managed services, hooks, shortcuts and menu entries are
removed. Checkpoints and unrelated settings are kept. Disabling the plugin or
restarting the shell does not trigger cleanup.

Conflicts with edited managed files are reported in
`journalctl --user -u omarchy-desktop-restore-lifecycle.service`.

## License

Licensed under [MIT](LICENSE).
