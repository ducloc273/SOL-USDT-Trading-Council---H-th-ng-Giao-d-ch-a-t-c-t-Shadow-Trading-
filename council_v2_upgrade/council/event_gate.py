"""Deterministic fail-closed macro event check, prior to Council and paper.

Input schema from the user's original events.txt: YYYY-MM-DD HH:MM | event name.
Timestamps are local wall-clock times on the machine running the bot.
"""
import datetime as dt
import os
import re

BLOCK_BEFORE_HOURS = 24
# No assumed cool-down after event in this version; configurable if needed.
BLOCK_AFTER_HOURS = 0


def assess(path, now=None):
    now = now or dt.datetime.now()
    if not os.path.isfile(path):
        return {'allowed': False, 'reason': 'events.txt missing', 'event': None}
    entries = []
    invalid = []
    with open(path, encoding='utf-8-sig') as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})\s*\|\s*(.+)', line)
            if not m:
                invalid.append(n)
                continue
            try:
                when = dt.datetime.strptime(f'{m[1]} {m[2]}', '%Y-%m-%d %H:%M')
            except ValueError:
                invalid.append(n)
                continue
            entries.append((when, m[3].strip()))
    if invalid:
        return {'allowed': False, 'reason': f'events.txt invalid lines: {invalid}', 'event': None}
    if not entries:
        return {'allowed': False, 'reason': 'events.txt contains no events', 'event': None}
    future = sorted((w, x) for w, x in entries if w >= now)
    if not future:
        return {'allowed': False, 'reason': 'events.txt is outdated: no future events', 'event': None}
    w, name = future[0]
    hours = (w-now).total_seconds()/3600
    # Only declared events are blocked; their validity must be checked by user.
    if hours <= BLOCK_BEFORE_HOURS:
        return {'allowed': False, 'reason': f'event within {BLOCK_BEFORE_HOURS}h: {name} ({hours:.1f}h)',
                'event': name, 'hours': round(hours, 2)}
    return {'allowed': True, 'reason': f'next event in {hours:.1f}h',
            'event': name, 'hours': round(hours, 2)}


def as_council_text(gate):
    return f"{gate['event']} in {gate['hours']:.1f} hours" if gate.get('event') else 'none'
