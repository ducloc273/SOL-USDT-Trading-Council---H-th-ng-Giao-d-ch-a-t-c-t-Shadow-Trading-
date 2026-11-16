"""Deterministic macro-event guard for paper trading only.

Input: YYYY-MM-DD HH:MM | event name; interpreted in COUNCIL_EVENT_TZ
(default Asia/Ho_Chi_Minh) even when the server runs in another timezone.

AUTOMATIC: fail closed on missing, corrupt or exhausted calendar.
MANUAL: --ack-stale-events can override *only* OUTDATED; never an active event.
"""
import datetime as dt
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BLOCK_BEFORE_HOURS = 24
BLOCK_AFTER_HOURS = 2  # Research hypothesis, not a proven safe cooldown.
DEFAULT_TIMEZONE = 'Asia/Ho_Chi_Minh'


def _timezone():
    name = os.getenv('COUNCIL_EVENT_TZ', DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f'Timezone {name!r} is unavailable (install tzdata)') from exc


def assess(path, now=None, *, allow_stale_manual=False):
    """Return allowed, status, reason, event, hours; no network requests.

    'now' may be timezone-aware or naive (naive = event timezone, only for tests).
    """
    try:
        tz = _timezone()
    except ValueError as exc:
        return {'allowed': False, 'status': 'BAD_TIMEZONE', 'reason': str(exc), 'event': None}
    if now is None:
        now = dt.datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    if not os.path.isfile(path):
        return {'allowed': False, 'status': 'MISSING', 'reason': 'events.txt missing', 'event': None}
    entries, invalid = [], []
    try:
        with open(path, encoding='utf-8-sig') as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})\s*\|\s*(.+)', line)
                if not m:
                    invalid.append(lineno)
                    continue
                try:
                    parsed = dt.datetime.strptime(f'{m[1]} {m[2]}', '%Y-%m-%d %H:%M')
                    entries.append((parsed.replace(tzinfo=tz), m[3].strip()))
                except ValueError:
                    invalid.append(lineno)
    except OSError as exc:
        return {'allowed': False, 'status': 'READ_ERROR', 'reason': str(exc), 'event': None}
    if invalid:
        return {'allowed': False, 'status': 'INVALID', 'reason': f'events.txt invalid lines {invalid}', 'event': None}
    if not entries:
        return {'allowed': False, 'status': 'EMPTY', 'reason': 'events.txt has no valid events', 'event': None}
    # Evaluate EVERY declared event, including past events in their post-release cooldown.
    active = []
    for when, name in entries:
        hours = (when - now).total_seconds() / 3600
        if -BLOCK_AFTER_HOURS <= hours <= BLOCK_BEFORE_HOURS:
            active.append((abs(hours), when, name, hours))
    if active:
        _, when, name, hours = min(active)
        return {'allowed': False, 'status': 'EVENT_WINDOW',
                'reason': f'macro window [{-BLOCK_AFTER_HOURS:+}h, +{BLOCK_BEFORE_HOURS}h before] {name}: {hours:+.2f}h until event',
                'event': name, 'hours': round(hours, 3),
                'when': when.isoformat()}
    future = sorted((when, name) for when, name in entries if when > now)
    if not future:
        if allow_stale_manual:
            return {'allowed': True, 'status': 'OUTDATED_MANUAL_ACK',
                    'reason': 'MANUAL OVERRIDE: calendar has no future events; user acknowledged stale event risk',
                    'event': None}
        return {'allowed': False, 'status': 'OUTDATED',
                'reason': 'events.txt has no future events; update calendar or use manual paper acknowledgment',
                'event': None}
    when, name = future[0]
    hours = (when - now).total_seconds() / 3600
    return {'allowed': True, 'status': 'CLEAR',
            'reason': f'next declared event in {hours:.1f}h ({name})',
            'event': name, 'hours': round(hours, 3), 'when': when.isoformat()}


def as_council_text(gate):
    if gate.get('status') == 'CLEAR' and gate.get('event'):
        return f"{gate['event']} in {gate['hours']:.1f} hours"
    return 'none'
