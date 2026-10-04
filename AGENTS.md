# Desktop Restore development

- This Git checkout is the source of truth. Make plugin changes here, not in a
  separate installed copy under `~/.config/omarchy/plugins/`.
- For local development, the installed plugin directory should be a symlink to
  this checkout. Verify its resolved path before diagnosing missing fixes.
- Fetch and compare with `origin/main` before reconciling an apparently outdated
  installation. Preserve existing local work before updating it.
- Run `python3 -m unittest discover -s tests -v` for changes to restore or setup.
- After changing runtime code, run `python3 lib/setup.py start` to apply the
  revision and refresh the checkpoint service. Verify the service is active.
- Keep installation receipts and service paths attached to the installed plugin
  link so removing that link still triggers the normal integration cleanup.
- Report automated tests and live verification separately. A successful launch
  is not evidence that a full reboot has been tested.
