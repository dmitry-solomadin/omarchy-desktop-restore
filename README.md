# Desktop Restore

Save your Omarchy desktop automatically and restore it after reboot with
**Super+Shift+R**. Runs silently in the background, with no bar widget or notifications.

Version **0.6.3** · Omarchy 4 / Lua-based Hyprland · MIT license

[![Checks](https://github.com/dmitry-solomadin/omarchy-desktop-restore/actions/workflows/check.yml/badge.svg)](https://github.com/dmitry-solomadin/omarchy-desktop-restore/actions/workflows/check.yml)

![Desktop Restore — agent sessions, terminals, apps and workspaces](preview.png)

## Features

- **Terminals:** Ghostty, Foot/footclient, Kitty, Alacritty and WezTerm. Reopens
  each in its original emulator and working directory.
- **Agent harnesses:** exact-session resume for OpenCode 1 and 2, Claude Code and
  Codex; default and named herdr sessions.
- **Shared-process fallback:** preserves identifiable agent sessions even when
  several terminal windows share a PID and their individual shell associations
  are unknown. Unique Claude/OpenCode titles retain their saved window slots;
  unmatched sessions use explicitly approximate placement.
- **Regular application windows:** reopens apps such as Signal and browsers such
  as Chrome, Chromium, Brave, Firefox and Zen, using each app's native recovery.
- **Desktop layout:** numbered, named and special workspaces, connected monitors,
  tiled ordering in matching arrangements, floating window position/size and fullscreen state.
- **700 ms save limit:** the final power-menu save has a hard cutoff, so saving
  cannot hold up reboot or shutdown beyond 700 ms. Background checkpoints are automatic.

## Requirements

- Omarchy 4 with Lua-based Hyprland and Quickshell. Developed against
  **Omarchy 4.0.4 / Hyprland 0.56.2**; legacy `.conf` setups are unsupported.
- Python 3.10+, `hyprctl`, `uwsm-app`, `gio`, `systemctl`, `busctl` and GNU `timeout`.

## Install

Run inside your Omarchy session:

```sh
omarchy plugin add https://github.com/dmitry-solomadin/omarchy-desktop-restore --enable
```

Enabling the plugin automatically installs its services, shortcut, power-menu
entries and hooks for installed agents. Plugin updates refresh the integration
on the next load; ordinary shell reloads do not reinstall or restart it.
Existing settings are preserved. Setup conflicts appear in the Omarchy shell logs.

Setup runs without root. It manages two user services:
`omarchy-desktop-restore.service` saves checkpoints, and
`omarchy-desktop-restore-lifecycle.service` handles removal cleanup.
A user post-boot hook starts both; the restore shortcut and power-menu entries
are added to user configuration.

If you install a supported agent later, re-enable the plugin or restart the shell
to pick up its hooks. Agent-specific setup below still applies.

## Additional agent setup

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

### Shared-process terminals

Reliable window-to-agent matches take priority and retain their original layout.
When that association is ambiguous, Desktop Restore inventories the terminal's
foreground agent branches and saves recoverable sessions as a group. On restore,
each conversation opens once, using the group's saved window slots for best-effort
workspace and layout placement. Unique OpenCode window titles and Claude's explicit
local session-title metadata are matched to their original slots before assigning
any unmatched sessions. Claude activity indicators are ignored; ambiguous titles
and truncated Claude prefixes are not guessed. Claude title lookup reads at most
the last 1 MiB of its known session file, respects `CLAUDE_CONFIG_DIR`, and falls back to
approximate placement when title metadata is unavailable. Sessions already captured
through reliable matches are excluded from the fallback.

Claude Code and Codex still require current session-hook records; herdr uses its
named/default session. OpenCode 1 and 2 use their existing visible conversation
titles and version-specific session metadata. Titles must identify an exact
conversation, and all plausible clients must agree on its configuration and
supported launch options. Missing identities or conflicting options are reported
rather than replaced with the latest conversation.

The fallback requires one distinct terminal child/TTY per mapped window. Groups
with extra surfaces or unmapped windows remain unsupported. It runs during ordinary
snapshots and requires no title manipulation or launcher configuration changes.

## Saving and reboot coverage

The desktop is checked every 10 seconds and ordinary changes are saved after
20 seconds without changes. Hyprland window-close events trigger a prompt save,
without that debounce. The watcher reconnects if the event socket disappears and
keeps periodic polling as a fallback.

After login, the previous desktop's recovery target stays protected until either
you invoke restore or **ten minutes have elapsed since the first qualifying new
application window**. Merely booting, waiting, locking or unlocking does not start
the countdown. The watcher's initial window census is a baseline; subsequent open
events wake it promptly. Additional windows and watcher restarts do not reset an
armed deadline. The clock is independent of wall-clock adjustments and includes
time spent suspended.

A qualifying window is a new mapped, non-hidden, input-accepting application
window. Known shell/lock/screensaver windows, processes in systemd autostart units,
and recorded restore-generated windows are excluded. This is best-effort launch
detection: Hyprland does not identify manual versus scripted window creation, so
a script or agent opening a regular application can also start the timer. Windows
already present at the initial census do not count.

During the countdown, restore keeps newly opened windows and adds the missing
pre-reboot windows. If the countdown expires without a restore, the current
desktop replaces the old recovery target; old entries are not kept as pending
retries. A reboot before the countdown expires preserves the protected target.
Once recovery has been attempted or the countdown expires, the target follows
the current desktop, including intentional closes and closing the final window.
Failed explicit recovery entries remain pending for retry; successfully recovered
or already-open entries are fulfilled and are not reopened after you close them.
Shutdown teardown is excluded from close-event saves, including a short grace
period after the power-menu final save.

Setup integrates saving into the Omarchy system menu's Reboot and Shutdown actions.
It preserves custom entries and falls back to the original commands if the plugin
wrapper is missing.

| Reboot/shutdown path | What gets saved |
| --- | --- |
| System menu (Super+Escape or power key), or the plugin's power wrapper | A save is attempted **before windows close**, using cached agent metadata, with a **700 ms cutoff**. Failure never blocks reboot or shutdown. |
| CLI commands, update prompts or direct `systemctl` calls | Best-effort late save, with the last autosave as fallback |
| Forced reboot, crash or power loss | Existing checkpoint only |

**Use the system menu for the most reliable save.** Omarchy has no shared
pre-shutdown hook covering every reboot path.

## Restore scheduling

Independent window classes restore concurrently, with up to eight classes active
at once. A slow or failed application can wait up to 15 seconds for its window
without holding up other active classes. Windows of the same class remain ordered
so generic startup titles, browser recovery and temporary placement rules cannot
mix up simultaneous launches. Completed launches are recorded immediately for
duplicate-free retries, even while another application is still pending.

When all tiled windows of a previously empty workspace have been restored, their
saved ordering is reconciled against the current tile slots. This corrects
left/right or top/bottom reversals caused by parallel startup, without waiting
for unrelated applications. Matching split arrangements can be reordered; exact
split ratios and different split-tree shapes are not reconstructed. Workspaces
with pre-existing tiles, moved windows, extra/missing tiles, groups or fullscreen
windows are left alone. Ordering preserves focus and suppresses cursor warping
within the compositor's atomic swap callback.

Desktop entries need an `Exec` command or D-Bus activation to be considered
launchers. Application IDs matching desktop-entry filenames take priority over
`StartupWMClass` aliases, then unique executable matches. Ambiguous aliases are
reported instead of choosing the first entry. Shared shell executables are not
used to infer a plugin launcher. Generic `org.quickshell` windows lack a
per-application identity and are reported as unresolved, including entries from
older checkpoints. Custom applications exposing their own app ID can use their
matching desktop launcher without application-specific restore code.

## Limits

- **Desktop:** no process memory, running jobs, SSH connections, scrollback,
  unsaved buffers, exact tiling ratios or Hyprland groups. Other apps reopen
  through their desktop launchers and rely on their own document recovery.
- **Terminals:** no tabs/splits, external multiplexers or remote WezTerm domains.
  Normal terminal configuration applies; custom launch flags are not replayed.
  Foot server windows reopen as standalone Foot windows. Ambiguous shared-process
  windows use the agent-session fallback where possible; unmatched window-to-session
  placement remains approximate, and unidentified plain-shell slots cannot be recovered.
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
`restore.json` the recovery target protected until initial recovery or expiry of
the first-window grace period. Afterward, the rolling checkpoint and restore target
are synchronized. Files are private (`0600`) and include window titles, directories
and session IDs. Prompts, responses and credentials are not copied; browser tabs
remain in the browser's own storage.

Shared-process inventories are stored in the checkpoint's `agent_groups` field,
separately from window layout. Each group contains recoverable sessions and any
session-identification errors. A final shutdown save with unidentified agents keeps
the previous checkpoint; placement uncertainty alone does not prevent saving.

The last restore result, including skipped windows, errors and approximate-placement
warnings, is recorded in `last-result.json`.

Every restore invocation is also recorded in `restore-events.jsonl`: UTC timestamp,
run ID, shortcut/CLI source, parent-process IDs/names, checkpoint timestamp, per-window
outcomes and total duration. Busy/rejected invocations and failures are logged too.
The source tag identifies the shortcut command versus a normal CLI invocation; it
does not prove a physical keypress. Logs do not collect keystrokes, process argument
lists or environments. Watcher starts and unlocks do not invoke restoration.
The same log records `recovery_timer_started` with the triggering window's class,
address and PID, and `recovery_timer_expired` when the current desktop takes over.
The deadline and initial window census are persisted in `instance.json`.

For background-service diagnostics:

```sh
systemctl --user status omarchy-desktop-restore.service
journalctl --user -u omarchy-desktop-restore.service
```

## Remove

```sh
omarchy plugin remove io.github.dmitry-solomadin.desktop-restore
```

Automatically removes everything installed or generated by Desktop Restore,
including services, hooks, shortcuts, menu entries, checkpoints, caches and backups.

## License

Licensed under [MIT](LICENSE).
