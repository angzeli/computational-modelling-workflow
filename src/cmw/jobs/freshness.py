"""Comparable monotonic timestamps for evidence shared by Jobs processes."""
import math
import time


def clock_stamp(now=None):
    # Local import avoids a cycle through Store when projecting a heartbeat.
    from .ownership import boot_session_id
    try:
        session = boot_session_id()
    except OSError:
        return None
    return {'boot_session_id': session, 'monotonic': time.monotonic() if now is None else now}


def age_seconds(stamp, current):
    """Return None for a missing, malformed, future or different-boot stamp."""
    if not isinstance(stamp, dict) or not isinstance(current, dict):
        return None
    session = stamp.get('boot_session_id')
    if not isinstance(session, str) or not session or current.get('boot_session_id') != session:
        return None
    then, now = stamp.get('monotonic'), current.get('monotonic')
    if not all(type(t) in (float, int) and math.isfinite(t) and t >= 0 for t in (then, now)):
        return None
    return now - then if then <= now else None
