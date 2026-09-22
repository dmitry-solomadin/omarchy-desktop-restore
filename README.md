# Desktop Restore

Save your Omarchy desktop automatically and restore it after reboot with
**Super+Shift+R**. Runs silently in the background, with no bar widget or notifications.

Version **0.3.2** · Omarchy 4 / Lua-based Hyprland · MIT license

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
"${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/io.github.dmitry-solomadin.desktop-restore/bin/desktop-restore" install
```

Both commands are required: the first adds the plugin; the second installs its
services, shortcut, CLI and power-menu entries. Setup preserves existing settings
and reports conflicting shortcuts or custom power actions.

After updating the plugin or installing another supported agent, run
`desktop-restore install` again to refresh the integration.

## Use

1. Work normally; changes are checkpointed after about 20–30 seconds of stability.
2. Reboot or shut down through Omarchy's menu for a final save before windows close.
3. After login, press **Super+Shift+R** or run `desktop-restore restore`.

Already-open matching windows stay where you put them. You can keep working and
switch workspaces while restoration runs.

| Command | Purpose |
| --- | --- |
| `desktop-restore restore` | Restore missing windows |
| `desktop-restore restore --dry-run` | Preview the restore |
| `desktop-restore status` | Show the checkpoint and last restore result |
| `desktop-restore status --json` | Machine-readable status |
| `desktop-restore save` | Save now and replace the current restore target |

Use `--file` for a named snapshot:

```sh
desktop-restore save --file "$HOME/work-desktop.json"
desktop-restore restore --file "$HOME/work-desktop.json"
```

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

Desktop Restore reopens the same default or named session, including sessions
selected through `HERDR_SESSION`. Herdr restores its own workspaces, tabs and panes.

**To recover agent conversations after reboot**, install herdr's own integrations:

```bash
herdr integration status
herdr integration install claude
herdr integration install codex
```

Restart the agents inside herdr afterward and follow any Codex hook-trust prompts.
Keep `session.resume_agents_on_restore` enabled. Herdr 0.8.2 requires Claude
integration v6+ and Codex integration v5+. Without a valid native session reference,
an agent pane returns as a shell after a server restart.

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

For skipped windows or restore errors:

```sh
desktop-restore status
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

To remove integration while keeping the plugin files, run `desktop-restore uninstall`.
Conflicts with edited managed files are reported in
`journalctl --user -u omarchy-desktop-restore-lifecycle.service`.

## Development

```sh
python3 -m unittest discover -s tests -v
omarchy plugin validate .
sh -n bin/desktop-restore
sh -n bin/power-action
```

The tests isolate state and replace power commands with harmless substitutes.
Live checks cover Ghostty, Foot (including server mode) and herdr. Kitty, Alacritty
and WezTerm have regression coverage but have not been tested live on the development machine.

## Feedback

[Report a bug or request a feature](https://github.com/dmitry-solomadin/omarchy-desktop-restore/issues).
Include your Omarchy and Hyprland versions, the app involved, and whether you
restored through the shortcut or CLI. Review diagnostic output before sharing:
checkpoint files can contain private window titles, directories and session IDs.

Licensed under [MIT](LICENSE).
