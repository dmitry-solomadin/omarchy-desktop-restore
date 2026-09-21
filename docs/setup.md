# Setup, updates, and migration

## Managed integration

`bin/desktop-restore install` works inside the running Omarchy desktop, without
root. It checks dependencies and Super+Shift+R conflicts before changing files.

It adds:

- `~/.local/bin/desktop-restore`: launcher pointing to this plugin's `bin/`.
- `~/.config/systemd/user/omarchy-desktop-restore.service`: background watcher
  and bounded shutdown-save hook.
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

Re-running setup at the same plugin path restarts the watcher. Update source in
place, then run setup again. If a future release changes the generated integration
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

## Migrate from the original local desktop-restore helper

The package uses the original checkpoint format and state directory. There is
no need to recreate saved layouts. The installer detects the original service
and asks for migration rather than running both watchers.

1. Back up the existing bindings, menu, service, CLI launcher, and
   `~/.local/state/desktop-restore/`.
2. Stop the original watcher:

   ```sh
   systemctl --user stop desktop-restore.service
   ```

3. Remove only the original helper's integration:
   - `~/.config/systemd/user/desktop-restore.service`
   - `~/.config/omarchy/hooks/post-boot.d/start-watcher`, after verifying that it
     starts this service
   - `~/.local/bin/desktop-restore`, after verifying it points to the original
     `~/.local/share/desktop-restore/desktop_restore.py`
   - The original Desktop Restore binding at the end of `bindings.lua`
   - The two power-menu overrides pointing to the original
     `~/.local/share/desktop-restore/power-action`
4. Reload and check the edited configuration:

   ```sh
   systemctl --user daemon-reload
   hyprctl reload
   hyprctl configerrors
   ```

5. Follow the README installation steps. Keep the original helper source and
   backups until the packaged version is working. Retain the checkpoint state.

## Uninstall behavior

Run `desktop-restore uninstall` before `omarchy plugin remove`. The bar widget
and the separately installed desktop integration have independent lifecycles.

The receipt records the original text and the exact installed blocks. Uninstall
restores unchanged managed files and removes its blocks while preserving later
unrelated additions. If the managed content itself was edited, it stops with the
file path so those edits can be preserved explicitly; it does not overwrite them.

For manual cleanup, stop `omarchy-desktop-restore.service`, remove its unit,
post-boot hook and launcher, and remove the two marked configuration blocks.
Then run `systemctl --user daemon-reload`, `hyprctl reload`, and
`hyprctl configerrors`. Delete `installation.json` only once cleanup is complete.
Saved checkpoint files may be retained or deleted separately.
