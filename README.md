# Desktop Restore

Bring your Omarchy desktop back after reboot: workspaces, terminal directories,
browser windows, applications, and **OpenCode, Codex, Claude Code and herdr sessions**.

Restore when you want with **Super+Shift+R** or the CLI. Saving is automatic
and silent. The reboot/shutdown menu gives the saver **700 ms**, then continues
with Omarchy's normal power action even if saving fails or hangs.

Version **0.3.2** · Omarchy 4 / Lua-based Hyprland · MIT license

[![Checks](https://github.com/dmitry-solomadin/omarchy-desktop-restore/actions/workflows/check.yml/badge.svg)](https://github.com/dmitry-solomadin/omarchy-desktop-restore/actions/workflows/check.yml)

## Features

- Checkpoints across all workspaces, including named and special workspaces.
- Restores workspaces, connected monitors, floating geometry and fullscreen state.
- Reopens terminal working directories and agents in their original emulator:
  Ghostty, Foot, Kitty, Alacritty or WezTerm.
- Resolves OpenCode 2 window titles to exact session IDs, including truncated
  titles; ambiguous matches are reported instead of choosing the newest session.
- Resumes Codex and Claude Code by exact IDs recorded by their native session
  hooks, and reattaches herdr's default or named persistent sessions.
- Uses browsers' native last-session recovery and other apps' desktop launchers.
- Restores missing windows without duplicating already-matched windows.
- Launches windows directly on their saved workspaces using silent launch rules,
  preventing initial focus. A reversible activation guard expires after 20 seconds
  so later link clicks can focus the browser normally.
- Desktop-launcher apps such as Signal also receive a temporary class-matching
  rule, covering launchers or existing background processes that lose the launch
  token. The compositor disables this rule after 20 seconds, even if restore exits.
- Background-only operation with shortcut and CLI controls; no bar widget or popup.
- User-level installation, backups, rollback on setup failure, and uninstall.
- Python standard library only; no pip dependencies.

## Requirements

Developed against **Omarchy 4.0.4 / Hyprland 0.56.2** and its Lua dispatch API.
Legacy Hyprland `.conf` configurations are not supported.

Required commands: `python3` (3.10+), `hyprctl`, `uwsm-app`, `gio`,
`systemctl`, `busctl`, and GNU `timeout`. The plugin's nonvisual service entry point
uses Omarchy's Quickshell shell. **`opencode2` is optional**, required only for OpenCode recovery;
the adapter targets its V2 session API. Keep the terminal emulators used by your
checkpoint installed; Ghostty is not required for a Foot-only desktop.

## Install

Run inside your Omarchy desktop session:

```sh
omarchy plugin add https://github.com/dmitry-solomadin/omarchy-desktop-restore --enable
"$HOME/.config/omarchy/plugins/io.github.dmitry-solomadin.desktop-restore/bin/desktop-restore" install
```

The first command installs and enables the background plugin. The second installs the
watcher, startup hook, restore shortcut, CLI launcher, and power-menu integration.
Existing custom power actions or a conflicting shortcut must be resolved first.
See [power-menu integration](#power-menu-integration) for custom power actions.

### Install a local checkout

From the project directory, copy it to its permanent location before running
setup, since the installed service refers to that location:

```sh
plugin="$HOME/.config/omarchy/plugins/io.github.dmitry-solomadin.desktop-restore"
mkdir -p "$plugin"
cp -a manifest.json Service.qml LICENSE README.md bin lib "$plugin/"
omarchy plugin validate "$plugin"
omarchy-shell shell rescanPlugins
omarchy plugin enable io.github.dmitry-solomadin.desktop-restore
"$plugin/bin/desktop-restore" install
```

Enabling the plugin starts an already-configured integration without creating one.
Use the setup command above for first-time installation, and
`desktop-restore uninstall` to stop and remove the integration. The watcher runs independently of
the shell, including shell restarts.

## Use

1. Work normally. Automatic checkpoints settle about 20–30 seconds after changes.
2. Reboot or shut down through Omarchy's menu for a final pre-teardown checkpoint.
3. After logging in, press **Super+Shift+R** or run `desktop-restore restore`.

There is no bar widget, save shortcut or desktop notification. Already-open
matching windows stay where you have placed them.
Restoration never returns focus to the starting window when it finishes. You can
switch to another workspace or a newly restored window while restoration runs.

```sh
desktop-restore status
desktop-restore status --json
desktop-restore restore --dry-run
desktop-restore restore
systemctl --user status omarchy-desktop-restore.service
journalctl --user -u omarchy-desktop-restore.service
```

For an explicit checkpoint, run `desktop-restore save`. This replaces the current
restore target. Named snapshots use `--file`:

```sh
desktop-restore save --file "$HOME/work-desktop.json"
desktop-restore restore --file "$HOME/work-desktop.json"
```

## Terminal support

| Terminal | Restore behavior |
| --- | --- |
| Ghostty | Independent process, original class and directory |
| Foot / footclient | Standalone Foot window, original app ID and directory; no pre-existing server needed |
| Kitty | New Kitty process, original class and directory |
| Alacritty | New Alacritty process, original class and directory |
| WezTerm | `wezterm start --always-new-process`, original class and directory |

The same terminal adapters wrap OpenCode, Codex, Claude Code and herdr resume
commands. Shell windows reopen at their working directory using the emulator's
configured default shell. Commands and paths are passed as separate arguments,
including paths containing spaces. Unknown tagged terminals are reported rather
than silently converted to Ghostty.

Shared-process windows (including Foot server mode) use the one-to-one shell
branch matching described below. Terminal tabs/splits, remote WezTerm domains,
and external multiplexers are not reconstructed. Custom terminal launch flags,
alternate config files and transient environment overrides are not replayed;
the emulator's normal configuration applies.

Live close/reopen checks covered Foot shell windows and herdr in Foot, including
shared Foot server windows, silent workspace placement and repeat-restore deduplication. Kitty, Alacritty and
WezTerm have capture/launch regression tests and documented CLI checks; they have
not been exercised live on the development machine.

## Agent sessions

| Agent | How it is restored |
| --- | --- |
| OpenCode 2 | Unique window title → exact session ID; `opencode2 --session ID DIRECTORY` |
| Codex CLI | Process-bound hook record → `codex resume ID` in the recorded directory |
| Claude Code | Process-bound hook record → `claude --resume ID` in the recorded directory |
| herdr | `herdr --session NAME` (including `default`); herdr owns the persisted workspace/tab/pane contents |

When the relevant command is installed, setup adds small **SessionStart** and
**UserPromptSubmit** hooks to `~/.codex/hooks.json` and `~/.claude/settings.json`.
`CODEX_HOME` and `CLAUDE_CONFIG_DIR` are respected when running setup. Existing
hooks/settings are preserved, and uninstall removes only this plugin's entries.
After upgrading or installing an agent later, rerun `desktop-restore install`.

**Codex:** review and trust the new hooks through its `/hooks` screen. The plugin
does not bypass Codex hook trust. **Claude Code:** restart an already-running
client so it loads the new hooks. New sessions are recorded at startup, and prompt
events refresh the record when working in an existing session.

Hooks are silent, bounded to 500 ms, and store only the agent kind, process/boot
identity, session ID, working directory and timestamp under the private
`~/.local/state/desktop-restore/agents/` directory. Prompts, responses and credentials
are not copied. The shutdown saver only reads these local records; it does not
query or launch an agent. A missing, stale or ambiguous identity is reported,
never replaced with a "resume latest" guess. An unresolved agent also prevents a
shutdown save from replacing the previous shutdown checkpoint.

Herdr uses its own persistence. Reopening a client reattaches to a running server,
or starts its saved session after reboot. Agent-pane conversation recovery depends
on herdr's official agent integrations and `session.resume_agents_on_restore`
setting. This plugin does not reconstruct panes or send commands into them.
Remote and `--no-session` herdr clients are not supported.

Herdr session selection follows its native precedence: an explicit `--session`
or `session attach` name wins; otherwise `HERDR_SESSION` selects the session,
falling back to `default`. Repeated `--session` options use the last value.
Custom-socket clients using `HERDR_SOCKET_PATH` without an explicit session are
reported as unsupported rather than reopened in an unrelated default session.

**Herdr pane recovery needs separate setup.** Desktop Restore's Codex/Claude hooks
identify standalone terminal conversations; they do not register native pane
session references with herdr. Check and install herdr's official integrations:

```bash
herdr integration status
herdr integration install claude
herdr integration install codex
```

Restart the agents inside herdr after installation so they report their session
IDs; follow any native Codex hook trust prompts. For herdr 0.8.2, Claude integration
version 6+ and Codex integration version 5+ are required. These integrations are
managed by herdr, separately from this plugin. Agents without a valid native
session reference return as shells after a server restart, even if reconnecting
to the still-running server previously worked.

The herdr adapter was cross-checked against
[wbarakat/omarchy-session-restore](https://github.com/wbarakat/omarchy-session-restore/blob/main/bin/omarchy-session-restore-agents)
and [herdr 0.8.2 session selection](https://github.com/herdrdev/herdr/blob/v0.8.2/src/session.rs).
That plugin saves agent kind/pane/cwd and runs Claude with `--continue` (other
agents start fresh). Desktop Restore delegates pane recovery to herdr's native
exact-session references to avoid selecting a different conversation by cwd.
An isolated live test also verified an environment-selected named session after
stopping its server and reopening it, with silent workspace placement.

Native agent detection follows the terminal's foreground process tree. Prefer
independent Ghostty windows (`ghostty --gtk-single-instance=false -e codex`, or
replace `codex` with `claude`/`herdr`). Shared-process windows are matched one-to-one
using shell directory titles; a single remaining window and shell can then be
paired even when the agent has its own title. Ambiguous windows, hidden unmatched
terminal branches, background/noninteractive agents and remote
Codex servers are not resumed. Explicit supported model/profile/permission flags
and custom agent home/config paths are retained; arbitrary launch arguments and
environment variables are not replayed. Non-persisted conversations cannot be
recovered after exit. Hidden agent tabs and arbitrary terminal multiplexers are
outside this adapter's scope.

## How saving works

The watcher polls every 10 seconds and saves after 20 seconds of stable window
state. Empty desktops never overwrite a checkpoint. Intentional closures left
stable for that interval become the new rolling checkpoint.

The power-menu wrapper saves before Omarchy starts closing windows. It uses only
cached OpenCode metadata, never starts/contacts OpenCode, and takes locks without
waiting. GNU `timeout --signal=KILL 0.7s` bounds the saver and its process group.
The wrapper always proceeds to the original power command. If the cache cannot
identify an OpenCode conversation, the previous shutdown checkpoint is retained.
The cutoff bounds the save attempt, not the duration of reboot itself.

A systemd stop hook attempts a final save only when logind reports an actual
system shutdown, with a one-second service-stop limit. It preserves the menu's
checkpoint. Direct reboot/poweroff paths are best effort: windows may already
have closed before this hook, so menu shutdown is the preferred path.

On a new login, the preceding login's shutdown checkpoint—or its rolling
checkpoint—is frozen as the restore target before new autosaves begin. Opening
a terminal after login therefore does not erase the desktop you want to restore.

State lives in `${XDG_STATE_HOME:-~/.local/state}/desktop-restore/`:

| File | Purpose |
| --- | --- |
| `latest.json` | Rolling automatic checkpoint |
| `restore.json` | Frozen restore target for this login |
| `shutdown.json` | Last successful shutdown checkpoint |
| `sessions-cache.json` | OpenCode session IDs, titles and locations |
| `restored-windows.json` | Window matching across repeated restores |
| `last-result.json` | Restore counts and errors |
| `installation.json` | Setup receipt for removing managed integration |
| `agents/*.json` | Process-bound Codex/Claude session IDs supplied by hooks |

Checkpoint files are written atomically with mode `0600`; the state directory is
created with mode `0700`. They contain window titles, directories and launch
metadata. Browser tabs remain in the browser's own session storage.

## Restoration limits

- This relaunches applications, not process memory: no terminal jobs, SSH
  connections, scrollback, unsaved buffers or terminal splits.
- Tiled windows use the current layout and spatial launch order. Exact split
  trees, ratios and Hyprland groups are not reconstructed.
- OpenCode captures the visible TUI conversation. Hidden TUI tabs, remote and
  standalone servers are outside this adapter's scope. Duplicate/truncated titles
  that identify multiple sessions need unique names before saving.
- Terminal directory recovery uses shell-integration directory titles, or one
  identifiable direct shell child. Ambiguous shared-process windows are skipped.
- Supported browser classes include Chrome, Chromium, Brave, Firefox and Zen.
  Recovery depends on what the browser saved during shutdown; this plugin does
  not preserve browser process memory or force a clean multi-window browser exit.
  Profiles passed through supported command-line flags are retained. Private
  windows are not recoverable through normal browser session storage.
- Browser placement uses active-tab titles, then window order, so it can be
  approximate. Multiple windows recovered by one browser launch initially share
  that launch's saved workspace; any other saved destinations are corrected
  without following the windows. Matching distinguishes recorded browser profile
  flags; profiles sharing one browser process may still be indistinguishable.
  If the recorded browser profile is already running, missing windows are reported for
  recovery through History rather than launching another whole-browser restore.
- Other applications need identifiable XDG desktop launchers. Internal documents
  and views depend on each app's own recovery support.
- A launch can wait up to 15 seconds for a matching window. Errors appear in CLI
  status and restore output. Successfully opened windows remain tracked even if
  placement fails. Workspace/monitor moves observed while waiting for startup
  are respected instead of being reverted by placement.

## Power-menu integration

Setup adds a marked block to `~/.config/omarchy/extensions/omarchy-menu.jsonc`.
The **Reboot** and **Shutdown** entries keep their normal labels and icons, but
run `bin/power-action` first. It silently attempts a checkpoint for at most
700 ms, then runs Omarchy's original power command regardless of save success.
If the wrapper is missing, the menu falls back directly to the original command.

### Which reboot paths are covered?

| Path | Save before Omarchy closes windows? |
| --- | --- |
| Reboot/Shutdown in the Omarchy system menu | Yes, bounded to 700 ms |
| Super+Escape or the power key, then choosing Reboot/Shutdown | Yes; these open the same system menu |
| A custom shortcut calling this plugin's `bin/power-action reboot` or `shutdown` | Yes |
| `omarchy reboot`, `omarchy system reboot`, or their shutdown equivalents | Best-effort service save and rolling checkpoint fallback |
| Omarchy's reboot-after-updates prompt and other scripts calling its power commands directly | Best-effort service save and rolling checkpoint fallback |
| Direct `systemctl reboot` / `poweroff` | Best-effort service save and rolling checkpoint fallback |
| Forced reboot, power loss, or a system crash | Previously saved checkpoint only |

On the inspected Omarchy 4 implementation, the normal power scripts schedule the
power action for two seconds later and immediately close application windows.
They do not expose a shared pre-reboot/pre-shutdown hook. The service's final save
may therefore happen after windows have closed. The plugin does not currently
guarantee a fresh pre-teardown checkpoint for every reboot path.

Setup refuses to overwrite existing custom reboot/shutdown entries. If you keep
your own power actions, call the installed `bin/desktop-restore save-shutdown`
before them to perform the same bounded save. Direct terminal power commands and
other shortcuts bypass the menu wrapper; the service's late shutdown save is
best effort. Normal plugin removal cleans up the marked menu block.

## Remove

Use Omarchy's normal removal command:

```sh
omarchy plugin remove io.github.dmitry-solomadin.desktop-restore
```

Within about six seconds, the removal monitor stops the watcher and removes the
managed shortcut, menu overrides, services, startup hook, CLI launcher, and its own
cleanup code. Saved checkpoints and unrelated configuration are retained.
Disabling the shell plugin or restarting the shell does not trigger removal.

You can still run `desktop-restore uninstall` to remove desktop integration while
keeping the plugin installed. If you edited managed integration itself, cleanup
preserves it and reports the conflict in
`journalctl --user -u omarchy-desktop-restore-lifecycle.service`.

**Upgrading from 0.1.0:** after updating the plugin, run `desktop-restore install`
once to add automatic removal support to the existing installation.

## Development

```sh
python3 -m unittest discover -s tests -v
omarchy plugin validate .
sh -n bin/desktop-restore
sh -n bin/power-action
```

The tests isolate state and replace power commands with harmless substitutes.
See [release preparation](RELEASING.md) for validation coverage and publishing.

## Feedback

[Report a bug or request a feature](https://github.com/dmitry-solomadin/omarchy-desktop-restore/issues).
Include your Omarchy and Hyprland versions, the app involved, and whether you
restored through the shortcut or CLI. Review diagnostic output before sharing:
checkpoint files can contain private window titles, directories and session IDs.

See [CHANGELOG.md](CHANGELOG.md) for changes. Licensed under [MIT](LICENSE).
