"""Shared Beijing update cycles; independent of the machine's local timezone."""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


def cycle_start(now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(BEIJING)
    start = now.replace(hour=21, minute=0, second=0, microsecond=0)
    return start if now >= start else start - timedelta(days=1)


def in_cycle(value, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return bool(stamp.tzinfo and cycle_start(now) <= stamp <= now)
    except (ValueError, TypeError):
        return False


def retry_delay(failures):
    return (300, 900, 1800, 3600)[min(max(int(failures) - 1, 0), 3)]
