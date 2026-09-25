"""Optional placement evidence; session identity still comes from live hooks."""
import json
from pathlib import Path
import re


TITLE_BYTES = 1024 * 1024
# Claude changes the leading activity indicator without changing the title.
CLAUDE_STATUS = re.compile(r'^[✳✻✽✶✢·◐◑◒◓]\s+')


def claude_label(title):
    return CLAUDE_STATUS.sub('', title).strip()


def claude_title(session):
    """Read only explicit title records from a bounded tail of the known session.

    Missing/older metadata only reduces placement accuracy. Never infer a title
    from prompts, summaries, another session, or a truncated prefix.
    """
    home = Path(session.get('agent_env', {}).get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
    project = re.sub(r'[^a-zA-Z0-9]', '-', session['cwd'])
    path = home / 'projects' / project / (session['session'] + '.jsonl')
    try:
        with path.open('rb') as stream:
            end = stream.seek(0, 2)
            start = max(0, end - TITLE_BYTES)
            stream.seek(start)
            data = stream.read(TITLE_BYTES)
        if start:
            data = data.partition(b'\n')[2]  # Discard a potentially partial record.
        titles = {}
        for line in data.splitlines():
            # Avoid decoding conversation payloads; only title metadata is useful.
            if not any(marker in line[:100] for marker in (b'"ai-title"', b'"custom-title"')):
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict) or record.get('sessionId') != session['session']:
                continue
            kind = record.get('type')
            if kind not in ('ai-title', 'custom-title'):
                continue
            field = 'aiTitle' if kind == 'ai-title' else 'customTitle'
            title = record.get(field)
            if isinstance(title, str) and title.strip():
                titles[field] = title.strip()
        return titles.get('customTitle') or titles.get('aiTitle')
    except OSError:
        return None


def matched_slots(sessions, slots):
    """Only mutually unique title matches may reserve a window slot."""
    choices = {}
    for session in sessions:
        title = session.get('title')
        if not title:
            continue
        if session['kind'] == 'claude':
            matches = {w['key'] for w in slots if claude_label(w['title']) == title}
        elif session['kind'] in ('opencode', 'opencode1'):
            matches = {w['key'] for w in slots if w['title'] == title}
        else:
            continue
        choices[session['key']] = matches
    return {key: next(iter(matches)) for key, matches in choices.items()
            if len(matches) == 1
            and sum(bool(matches & other) for other in choices.values()) == 1}
