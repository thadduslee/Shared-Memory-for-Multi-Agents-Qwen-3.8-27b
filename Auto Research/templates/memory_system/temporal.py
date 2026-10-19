"""Shared temporal policy checks; unknown bounded time fails closed."""
from datetime import datetime, timezone


def within(start, end, at):
    if not start and not end:
        return True
    if not at:
        return False
    def point(value):
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    try:
        now=point(at)
        return (not start or point(start)<=now) and (not end or now<=point(end))
    except (TypeError,ValueError,AttributeError):
        return False
