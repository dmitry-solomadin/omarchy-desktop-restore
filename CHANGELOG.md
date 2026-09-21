# Changelog

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
