# Release preparation

## Identity

- Name: Desktop Restore
- Repository: https://github.com/dmitry-solomadin/omarchy-desktop-restore
- Plugin ID: `io.github.dmitry-solomadin.desktop-restore`
- Author: `dmitry-solomadin`
- Version: `0.1.1`
- License: MIT
- Category: System

The repository can be installed directly with `omarchy plugin add`. Marketplace
submission is a separate step; see the publishing instructions below.

## Validation

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q lib
sh -n bin/desktop-restore
sh -n bin/power-action
omarchy plugin validate .
```

GitHub Actions runs the portable Python, manifest-JSON, and shell checks. Omarchy
manifest validation and live desktop checks require an Omarchy installation.

Local packaging checks on Omarchy 4.0.4 / Hyprland 0.56.2 include manifest
validation, loading the QML component in a separate Quickshell process, and
isolated tests of checkpoint selection, title matching, shutdown deadlines and
installer rollback/removal. The original helper was also exercised with live
disposable Ghostty and exact-session OpenCode windows.

The silent-launch update was additionally tested through the active helper's full
restore path with two disposable Ghostty windows. Hyprland's IPC reported them
opening directly on workspaces 8 and 9 with no subsequent move events. The active
window and workspace remained unchanged, and repeating restore did not duplicate
the windows. Eleven focused regression tests cover launch rules, placement,
desktop-launcher handoffs, and focus preservation, including selecting a restored
window while restoration is still running. A live check confirmed that the timed
activation guard expires and restores the prior activation behavior.

The original helper has also been used through real reboot/restore cycles. The
complete packaged install/uninstall lifecycle on a fresh Omarchy user profile,
actual bar popup interaction, and comprehensive multi-window browser recovery
still need broader testing. Those are not established by manifest validation
or mocked setup tests. Also test removed monitors, duplicate conversation titles,
OpenCode unavailable, and paths containing spaces. Confirm compatibility with
the OpenCode 2 API version you intend to support.

## Publishing

1. Review README limitations and setup instructions.
2. Confirm plugin ID/repository availability in the current marketplace registry.
3. Add an optional `preview.png` using sample window titles.
4. Check executable bits on both `bin/` scripts. Do not package private checkpoint
   state, personal logs, Python bytecode, or installation backups.
5. Update the manifest version and changelog; push the reviewed source after checks pass.
6. Tag the release and publish release notes. Keep the two-step README installation
   instructions current: installing the bar plugin and installing desktop integration.
7. Follow the current marketplace submission instructions:
   https://github.com/omacom/omarchy-plugin-marketplace/blob/main/SUBMISSION.md

The marketplace workflow and accepted plugin requirements can change; consult
the current submission form when preparing the actual listing.
