"""Restore tiled window order once a newly restored workspace is ready."""
import math


def has_rect(window):
    return all(isinstance(window.get(field), (list, tuple)) and len(window[field]) == 2
               and all(isinstance(n, (int, float)) and math.isfinite(n) for n in window[field])
               for field in ('at', 'size')) and all(n > 0 for n in window['size'])


def slots(windows):
    """Describe rectangle topology independently of monitor offset and split ratios."""
    if not all(has_rect(w) for w in windows):
        return None
    rectangles = [(w['at'][0], w['at'][1], w['at'][0] + w['size'][0], w['at'][1] + w['size'][1])
                  for w in windows]
    for i, a in enumerate(rectangles):
        for b in rectangles[i + 1:]:
            if min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1]):
                return None  # Overlapping/fullscreen/grouped windows are not tiled slots.
    xs = {value: index for index, value in enumerate(sorted({r[i] for r in rectangles for i in (0, 2)}))}
    ys = {value: index for index, value in enumerate(sorted({r[i] for r in rectangles for i in (1, 3)}))}
    return {(xs[r[0]], ys[r[1]], xs[r[2]], ys[r[3]]): w['key'] for r, w in zip(rectangles, windows)}


def swaps(saved, actual):
    wanted, occupied = slots(saved), slots(actual)
    if wanted is None or occupied is None or set(wanted) != set(occupied):
        return None  # Ordering cannot reconstruct a different split-tree shape.
    if set(wanted.values()) != set(occupied.values()) or len(set(occupied.values())) != len(actual):
        return None
    result = []
    positions = {key: slot for slot, key in occupied.items()}
    for slot in sorted(wanted):
        key, occupant = wanted[slot], occupied[slot]
        if key == occupant:
            continue
        other = positions[key]
        result.append((key, occupant))
        occupied[slot], occupied[other] = key, occupant
        positions[key], positions[occupant] = slot, other
    return result


def swap_script(workspace, windows, pairs, lua):
    """Check and swap in one compositor callback, without focus or cursor changes."""
    checks, actions = [], []
    for window in windows.values():
        selector = lua('address:' + window['address'])
        x, y = window['at']
        width, height = window['size']
        checks.append(f'''
            do
                local w = hl.get_window({selector})
                if not w or w.pid ~= {window['pid']} or not w.mapped or w.hidden
                    or w.floating or w.fullscreen ~= 0 or w.group
                    or not w.workspace or w.workspace.name ~= {lua(workspace.removeprefix('name:'))}
                    or w.at.x ~= {x} or w.at.y ~= {y}
                    or w.size.x ~= {width} or w.size.y ~= {height} then
                    error("desktop-restore: tiled layout changed")
                end
            end
        ''')
    for first, second in pairs:
        actions.append(f'''
            local result = hl.dispatch(hl.dsp.window.swap({{
                window = {lua('address:' + windows[first]['address'])},
                target = {lua('address:' + windows[second]['address'])}
            }}))
            if not result.ok then error(result.error or "Tiled window swap failed") end
        ''')
    return f'''
        local count = 0
        for _, w in ipairs(hl.get_workspace_windows({lua(workspace)})) do
            if w.mapped and not w.floating then count = count + 1 end
        end
        if count ~= {len(windows)} then error("desktop-restore: tiled layout changed") end
        {''.join(checks)}
        local no_warps = hl.get_config("cursor.no_warps")
        if type(no_warps) ~= "boolean" then error("Cannot preserve cursor warp setting") end
        local ok, err = pcall(function()
            hl.config({{ cursor = {{ no_warps = true }} }})
            {''.join(actions)}
        end)
        hl.config({{ cursor = {{ no_warps = no_warps }} }})
        if not ok then error(err) end
    '''


class TiledOrder:
    """Called under the restore progress lock after each successful placement."""
    def __init__(self, saved, live, hypr, run, lua):
        self.hypr, self.run, self.lua = hypr, run, lua
        self.groups, self.arrived = {}, {}
        occupied = {w['workspace'] for w in live if w.get('floating') is False}
        for window in saved:
            if window.get('floating') is False:
                self.groups.setdefault(window['workspace'], []).append(window)
        self.groups = {ws: group for ws, group in self.groups.items()
                       if ws not in occupied and len(group) > 1
                       and all(not w.get('error') and not w.get('fullscreen') and has_rect(w) for w in group)}

    def placed(self, saved, actual, moved):
        workspace = saved['workspace']
        group = self.groups.get(workspace)
        if not group or not any(w['key'] == saved['key'] for w in group):
            return None
        if moved:
            self.groups.pop(workspace)
            return {'status': 'skipped', 'reason': 'window moved during startup'}
        self.arrived[saved['key']] = (actual['address'], actual['pid'])
        if not all(w['key'] in self.arrived for w in group):
            return None
        self.groups.pop(workspace)  # One immediate ordering pass; no delayed rearrangement.
        clients = [w for w in self.hypr('clients') if w.get('mapped') and not w.get('floating')
                   and w['workspace']['name'] == workspace.removeprefix('name:')]
        expected = {self.arrived[w['key']]: w['key'] for w in group}
        if (len(clients) != len(group) or {(w['address'], w['pid']) for w in clients} != set(expected)
                or any(w.get('fullscreen') or w.get('grouped') or w.get('hidden') for w in clients)
                or len({w.get('monitor') for w in clients}) != 1):
            return {'status': 'skipped', 'reason': 'workspace changed during startup'}
        actual = [{**w, 'key': expected[(w['address'], w['pid'])]} for w in clients]
        plan = swaps(group, actual)
        if plan is None:
            return {'status': 'skipped', 'reason': 'different tiled arrangement'}
        if plan:
            script = swap_script(workspace, {w['key']: w for w in actual}, plan, self.lua)
            try:
                answer = self.run(['hyprctl', 'eval', script])
                if answer != 'ok':
                    raise RuntimeError(answer)
            except RuntimeError as error:
                if 'desktop-restore: tiled layout changed' in str(error):
                    return {'status': 'skipped', 'reason': 'workspace changed before ordering'}
                raise
        return {'status': 'restored', 'swaps': len(plan)}
