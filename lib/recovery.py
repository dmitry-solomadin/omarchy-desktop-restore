"""A bounded recovery grace period, armed by a new regular application window."""
from pathlib import Path
import time

GRACE_SECONDS = 600
POLICY = 'first-window-v1'
SYSTEM_CLASSES = {'org.quickshell', 'org.omarchy.screensaver', 'org.omarchy.lock',
                  'hyprlock', 'org.hyprland.hyprlock'}


def clock():
    # Stable across watcher restarts and wall-clock adjustments; includes suspend.
    # Deadlines are only used within their recorded Hyprland instance/boot.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def window_key(window):
    return f"{window.get('stableId') or window['address']}:{window['pid']}"


def eligible(window, restored=(), proc_root=Path('/proc')):
    """Best-effort provenance exclusions, not proof of a physical user action."""
    if (not window.get('mapped') or window.get('hidden') or not window.get('acceptsInput', True)
            or not window.get('class') or window['class'].lower() in SYSTEM_CLASSES
            or window.get('pid', 0) <= 0):
        return False
    if any(w.get('address') == window['address'] and w.get('pid') == window['pid'] for w in restored):
        return False
    try:
        cgroup = (proc_root / str(window['pid']) / 'cgroup').read_text()
    except OSError:
        return False  # An exited/unreadable process cannot establish a new app.
    return '@autostart.service' not in cgroup


def observe(meta, windows, restored=(), qualifies=None):
    """Update observation state; return the first qualifying window, if any."""
    if meta.get('recovery_started') or meta.get('recovery_deadline') is not None:
        return None
    qualifies = qualifies or eligible
    current = {window_key(w) for w in windows if w.get('mapped')}
    if 'recovery_seen_windows' not in meta:
        # The first census is a baseline, not evidence that the user opened apps.
        meta.update(recovery_policy=POLICY, recovery_seen_windows=sorted(current))
        return None
    seen = set(meta['recovery_seen_windows'])
    meta['recovery_seen_windows'] = sorted(seen | current)
    for window in windows:
        if window_key(window) in seen or not qualifies(window, restored):
            continue
        started = clock()
        trigger = {key: window[key] for key in ('address', 'pid', 'class')}
        meta.update(recovery_policy=POLICY, recovery_first_window_at=time.time(),
                    recovery_deadline=started + GRACE_SECONDS, recovery_trigger=trigger)
        return trigger
    return None


def remaining(meta):
    if meta.get('recovery_started') or meta.get('recovery_deadline') is None:
        return None
    return max(0, meta['recovery_deadline'] - clock())


def due(meta):
    return remaining(meta) == 0
