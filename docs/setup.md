# Setup, updates, and removal

## Managed integration

`bin/desktop-restore install` works inside the running Omarchy desktop, without
root. It checks dependencies and Super+Shift+R conflicts before changing files.

It adds:

- `~/.local/bin/desktop-restore`: launcher pointing to this plugin's `bin/`.
- `~/.config/systemd/user/omarchy-desktop-restore.service`: background watcher
  and bounded shutdown-save hook.
- `~/.config/systemd/user/omarchy-desktop-restore-lifecycle.service`: detects
  removal of the plugin directory and cleans up the desktop integration.
- `~/.local/state/desktop-restore/cleanup.py`: a private copy of the setup code
  that survives plugin-folder deletion long enough to perform cleanup.
- `~/.config/omarchy/hooks/post-boot.d/desktop-restore`: starts the watcher after
  the desktop environment is ready.
- A marked block in `~/.config/hypr/bindings.lua`: Super+Shift+R restore binding.
- A marked block in `~/.config/omarchy/extensions/omarchy-menu.jsonc`: reboot and
  shutdown overrides, with fallback to the normal power command if the plugin
  wrapper is missing.

Configuration paths honor `XDG_CONFIG_HOME` in the setup process; the shell and
Hyprland must also use that location. The CLI launcher follows Omarchy's standard
`~/.local/bin` location. Keep XDG variables consistent across the desktop, user
systemd manager and CLI.

Existing edited configuration files receive timestamped `.bak.desktop-restore-*`
backups. JSONC comments and unrelated menu entries are preserved. Setup reloads
Hyprland, checks `hyprctl configerrors`, reloads user systemd units and starts the
watcher. A failed initial installation rolls back the applied files.

Re-running setup at the same plugin path refreshes the cleanup code and restarts
both services. This also upgrades installations made before automatic removal was
available. Update source in place, then run setup again. If a future release changes the generated integration
format, uninstall and reinstall to regenerate it. To move the plugin, uninstall
first, move it, and install from the new path.

## Existing power-menu customization

Setup refuses to overwrite existing `system.reboot` or `system.shutdown` entries.
To use its standard integration, back up your menu, move those custom entries
aside, install, then retain any desired labels/icons outside the managed block.

If you maintain your own power commands, you can instead manually configure the
watcher, hook and restore binding, using `lib/setup.py` as a reference. Call
`/permanent/plugin/path/bin/desktop-restore save-shutdown` before your existing
power action. That command is silent and bounded; do not call the Python engine
directly from an unbounded shutdown path. A manual installation is also removed
manually rather than through the setup receipt.

## Uninstall behavior

Run `omarchy plugin remove io.github.dmitry-solomadin.desktop-restore` normally.
The independent removal monitor checks once per second and waits for the plugin
directory to remain absent for five seconds, allowing brief directory replacements
during updates. It then stops the checkpoint watcher and removes all managed
integration, including its own service and cleanup script. It exits successfully
instead of restarting. Saved checkpoints and configuration backups remain.

This works even if the shell plugin is disabled, the shell restarts, or the plugin is
removed while the shell is not running. If the graphical session is stopped,
cleanup runs after the post-boot hook starts the services on the next login.
Disabling the shell plugin alone does not uninstall desktop integration.

`desktop-restore uninstall` remains available for immediate cleanup while keeping
the plugin folder. Existing 0.1.0 installations gain the removal monitor by running
`desktop-restore install` after updating the plugin.

The receipt records the original text and the exact installed blocks. Uninstall
restores unchanged managed files and removes its blocks while preserving later
unrelated additions. If the managed content itself was edited, it stops with the
file path so those edits can be preserved explicitly; it does not overwrite them.
Automatic cleanup reports such conflicts in
`journalctl --user -u omarchy-desktop-restore-lifecycle.service`. The surviving
`cleanup.py` can be invoked with `python3 ~/.local/state/desktop-restore/cleanup.py uninstall`
after resolving the conflicting edits.

For manual cleanup, stop `omarchy-desktop-restore.service` and
`omarchy-desktop-restore-lifecycle.service`, remove their units, the cleanup script,
post-boot hook and launcher, and remove the two marked configuration blocks.
Then run `systemctl --user daemon-reload`, `hyprctl reload`, and
`hyprctl configerrors`. Delete `installation.json` only once cleanup is complete.
Saved checkpoint files may be retained or deleted separately.
