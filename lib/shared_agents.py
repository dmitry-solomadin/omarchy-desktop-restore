"""Exact conversation preservation when terminal window placement is ambiguous."""
import hashlib
import json
from pathlib import Path

from agents import terminal_agent, terminal_branch, terminal_roots
from terminals import TERMINALS, terminal_launch


def capture_group(siblings, slots, procs, state, exact, identity, resolve_opencode):
    """Only relax window association, never session identity or visibility checks."""
    pid = siblings[0]['pid']
    command = procs.get(pid, {}).get('cmd', [])
    if not command:
        return None
    terminal = Path(command[0]).name
    roots = terminal_roots(pid, procs)
    # A foreground process in a hidden tab is also foreground on its own PTY.
    # Do not enumerate a group whose surfaces cannot all be visible windows.
    if (terminal not in TERMINALS or len(siblings) < 2 or len(roots) != len(siblings)
            or any(not w.get('mapped') or not w.get('class') for w in siblings)
            or len({p['tty'] for p in roots.values()}) != len(roots)):
        return None
    ambiguous = {w['address'] for w in slots}
    claimed_roots = set()
    for window in siblings:
        if window['address'] not in ambiguous:
            try:
                claimed_roots.add(terminal_branch(window, procs, siblings))
            except ValueError:
                pass
    key = hashlib.sha256(json.dumps([pid, procs[pid].get('start'),
                                    sorted(w['key'] for w in slots)]).encode()).hexdigest()[:16]
    group = {'key': key, 'pid': pid, 'start': procs[pid].get('start'), 'terminal': terminal,
             'window_keys': [w['key'] for w in slots], 'sessions': [], 'errors': []}
    native, opencode = [], []
    unknown_opencode = False
    for root in sorted(set(roots) - claimed_roots):
        try:
            agent = terminal_agent(siblings[0], procs, state, root=root)
            if agent:
                (opencode if agent['kind'] in ('opencode', 'opencode1') else native).append(agent)
        except (ValueError, OSError) as error:
            group['errors'].append(f'Shell {root}: {error}')
            if getattr(error, 'kind', None) not in ('claude', 'codex', 'herdr'):
                unknown_opencode = True
    if opencode and unknown_opencode:
        group['errors'].append('Cannot assign OpenCode titles while another local client/context is unidentified')
    elif opencode:
        resolved, errors = resolve_opencode(opencode, slots)
        native.extend(resolved)
        group['errors'].extend(errors)
    elif any('OC | ' in w['title'] for w in slots):
        group['errors'].append('OpenCode session could not be identified from the local clients')

    # Prefer the reliable path. Collapse duplicate conversations only if their
    # supported launch settings agree; never select arbitrary permission flags.
    known = {identity(w) for w in exact if w.get('session') and not w.get('error')}
    candidates, conflicts = {}, set()
    for session in native:
        ident = identity(session)
        if ident in known:
            continue
        previous = candidates.get(ident)
        if previous and (previous['argv'], previous['cwd']) != (session['argv'], session['cwd']):
            conflicts.add(ident)
        candidates[ident] = session
    for ident in sorted(candidates, key=repr):
        session = candidates[ident]
        if ident in conflicts:
            group['errors'].append(f"{session['kind']} session {session['session']}: conflicting launch options")
            continue
        session = dict(session)
        session['key'] = key + '-' + hashlib.sha256(repr(ident).encode()).hexdigest()[:16]
        group['sessions'].append(session)
    return group


def deduplicate_groups(groups, identity):
    """One fallback entry per conversation, including across terminal processes."""
    owners = {}
    for group in groups:
        for session in group['sessions']:
            owners.setdefault(identity(session), []).append((group, session))
    keep = set()
    for copies in owners.values():
        settings = {json.dumps([s['argv'], s['cwd']]) for _, s in copies}
        if len(settings) == 1:
            keep.add(copies[0][1]['key'])
        else:
            for group, session in copies:
                group['errors'].append(f"{session['kind']} session {session['session']}: "
                                       'conflicting launch options across shared terminals')
    for group in groups:
        group['sessions'] = [s for s in group['sessions'] if s['key'] in keep]


def restore_entries(snapshot):
    """Keep real windows separate from synthetic, explicitly approximate slots."""
    windows = snapshot['windows']
    entries = [w for w in windows if not w.get('agent_group')]
    seen = {w['key'] for w in entries}
    for group in snapshot.get('agent_groups', []):
        slots = [w for w in windows if w['key'] in group['window_keys']]
        if not slots:
            continue
        for index, session in enumerate(group['sessions']):
            if session['key'] in seen:
                continue
            seen.add(session['key'])
            slot = slots[index % len(slots)]
            # These addresses are inventory identities, not desktop windows.
            # In particular, never use the slot's real address to decide that a
            # different conversation is already open during same-login restore.
            entry = {key: slot[key] for key in ('class', 'workspace', 'monitor', 'at', 'size',
                                               'floating', 'fullscreen')}
            entry.update(session, address='group-session:' + session['key'], pid=group['pid'],
                         placement='approximate', agent_group=group['key'], terminal=group['terminal'])
            entry['title'] = session.get('title', f"{session['kind']}: {session['session']}")
            entry['launch'] = terminal_launch(group['terminal'], slot, session['cwd'], session['argv'])
            entries.append(entry)
    return entries
