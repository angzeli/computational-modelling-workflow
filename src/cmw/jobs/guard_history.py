"""Record external observations without treating a transient scan failure as a lifecycle event."""
import math
import os
import time

from .freshness import age_seconds, clock_stamp

DEFAULT_RECOVERY_SECONDS = 5.0
MAX_RECOVERY_SECONDS = 60.0
GUARD_ROWS = "job_id IS NULL AND message LIKE 'External guard:%'"


def configure_recovery(store, con, control):
    """Pin the controller's policy so its workers coalesce with the same window."""
    try:
        value = float(os.environ.get('CMW_JOBS_GUARD_RECOVERY_SECONDS', DEFAULT_RECOVERY_SECONDS))
    except ValueError:
        value = math.nan
    if not 0 <= value <= MAX_RECOVERY_SECONDS:
        value = DEFAULT_RECOVERY_SECONDS
        store.event(con, None, f'Controller warning: CMW_JOBS_GUARD_RECOVERY_SECONDS must be a number '
                    f'from 0 to {MAX_RECOVERY_SECONDS:g}; using {value:g} seconds')
    control['guard_recovery_seconds'] = value


def record_guard(store, con, guard):
    control = store.control(con)
    if 'guard_recovery_seconds' not in control:
        configure_recovery(store, con, control)
    previous = control.get('external_guard', {})
    control['external_guard'] = guard  # Admission sees every observation, including each failed scan.
    window = control.get('guard_event_window')
    state, reason = guard['state'], guard['reason']
    changed = (state, reason) != (previous.get('state'), previous.get('reason'))
    if state != 'UNAVAILABLE' and not changed and not window:
        store.set_control(con, control)
        return

    now, stamp = time.time(), clock_stamp()
    message = f'External guard: {state}: {reason}'
    duration = age_seconds(window['clock'], stamp) if window else None
    # Update the same event only inside a short, same-boot episode. Never join
    # an outage to a recovery seen across a boot boundary or a long gap.
    if window and duration is not None and duration < control['guard_recovery_seconds']:
        if state == 'UNAVAILABLE':
            window['count'] += 1
            window['last_reason'] = reason
        summary = (f"External guard: {window['count']} unavailable scan(s) "
                   f"from {window['started_at']:.3f} to {now:.3f} ({duration:.3f} s); "
                   f"first: {window['first_reason']}; latest failure: {window['last_reason']}; {state}: {reason}")
        updated = con.execute(f"UPDATE events SET message=? WHERE id=? AND {GUARD_ROWS}",
                              (summary, window['event_id'])).rowcount
        if updated and state == 'UNAVAILABLE':
            store.set_control(con, control)
            return
        if updated:
            control.pop('guard_event_window')
            store.set_control(con, control)
            return

    control.pop('guard_event_window', None)
    if changed:
        store.event(con, None, message)
        if state == 'UNAVAILABLE' and previous.get('state') != state:
            control['guard_event_window'] = {
                'event_id': con.execute("SELECT last_insert_rowid()").fetchone()[0],
                'clock': stamp, 'started_at': now, 'count': 1, 'first_reason': reason, 'last_reason': reason,
            }
        con.execute(f"DELETE FROM events WHERE {GUARD_ROWS} AND id NOT IN "
                    f"(SELECT id FROM events WHERE {GUARD_ROWS} ORDER BY id DESC LIMIT 100)")
    store.set_control(con, control)
