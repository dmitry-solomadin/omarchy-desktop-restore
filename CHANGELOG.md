# Changelog

## 0.2.1 — 2026-09-21

- Fixed agent detection in shared Ghostty processes: pair directory-titled windows
  with their shell branches, then resolve a single remaining pair by elimination.
  A neighboring plain shell is no longer incorrectly marked as an unresolved agent.
- Retain explicit ambiguity errors for duplicate directories and hidden branches.

## 0.2.0 — 2026-09-21

- Added exact-session restoration for Codex CLI and Claude Code through silent,
  process-bound native hooks. Records track boot identity and process start time
  to reject stale sessions and refresh on session start and prompt submission.
- Added default/named herdr client restoration using herdr's native persistence.
- Added automatic installation and removal of this plugin's agent hooks, retaining
  other agent settings and hooks. Codex requires native `/hooks` trust review.
- Added foreground process detection, npm wrapper handling, local Codex app-server
  identity lookup, explicit ambiguity errors and cached-only shutdown recovery.

## 0.1.2 — 2026-09-21

- Removed the bar widget and popup code entirely. Desktop Restore is now a
  background-only service plugin, controlled through Super+Shift+R and the CLI.
- The nonvisual shell entry point starts existing systemd integration without
  restarting it or reinstalling removed integration.

## 0.1.1 — 2026-09-21

- Normal `omarchy plugin remove` now automatically removes the desktop integration
  within about six seconds, retaining saved checkpoints.
- An independent removal monitor and private cleanup copy survive deletion of the
  plugin folder. Shell reloads, widget disabling and brief folder replacements do
  not uninstall the integration.
- Re-running setup upgrades existing installations with automatic removal support.
- Added integration coverage for cleanup after the source folder has disappeared,
  temporary folder replacement, and upgrading existing setup receipts.

## 0.1.0 — 2026-09-21

Initial public source release.

### Features

- Automatic desktop checkpoints and on-demand restoration with Super+Shift+R.
- Workspace, monitor, floating geometry and fullscreen-state recovery.
- Exact OpenCode 2 session recovery and terminal working-directory recovery.
- Native browser session restoration and XDG desktop application launchers.
- Silent, fail-open shutdown saving with a 700 ms deadline.
- Omarchy bar panel, user-level setup/removal, setup guide and automated checks.

### Fixes included from local testing

- Preserve labels and icons on the reboot/shutdown menu overrides.
- Launch windows directly on saved workspaces rather than moving them from the
  active workspace after startup.
- Handle Signal and other desktop-launcher apps that lose launch metadata through
  a temporary class-matching rule.
- Use a reversible activation guard instead of permanently suppressing activation
  events, preserving browser focus when opening links after restoration.
- Respect manual workspace/window selection while restoration finishes; never
  reset focus to the starting window at completion.

See the README for browser, layout and application-state limitations.
