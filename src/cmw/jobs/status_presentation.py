"""Read-only dashboard hierarchy; canonical admission decisions remain untouched."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math

from .activity import MAX_AGE
from .cli import guard_text, sharing_text
from .store import ACTIVE, PENDING


@dataclass(frozen=True)
class AdmissionPresentation:
    category: str
    tone: str
    summary: str
    external_summary: str
    external_tone: str


def admission_presentation(state):
    """Classify existing structured evidence without authorizing any execution.

    Non-blocking collector limitations are identified by their structured list,
    not by interpreting warning prose. Unclassified warnings retain caution.
    Freshness uses the already projected age, so rendering never scans or polls.
    """
    guard = state.get('external_activity') or {}
    decision = state.get('admission') or {}
    control = state.get('controller') or {}
    jobs = state.get('jobs', [])
    active = [job for job in jobs if job.get('status') in ACTIVE]
    pending = [job for job in jobs if job.get('status') in PENDING]
    observations = guard.get('observations', [])
    coverage = guard.get('coverage') or {}
    warnings = coverage.get('warnings', [])
    limitations = coverage.get('limitations', [])
    material = (coverage.get('complete') is not True
                or coverage.get('uncertainties', 0) != 0
                or any(warning not in limitations for warning in warnings))
    age = guard.get('age_seconds')
    fresh = (guard.get('observed_at') is not None and guard.get('stale') is False
             and isinstance(age, (int, float)) and not isinstance(age, bool)
             and math.isfinite(age) and 0 <= age <= MAX_AGE)
    guard_state = guard.get('state', 'UNAVAILABLE')
    mode = state.get('scheduler', {}).get('mode')
    if mode is None:
        mode = {'Sequential': 'sequential', 'Bounded Sharing': 'bounded-sharing'}.get(state.get('mode'))

    count = len(observations)
    warning_note = f" · {len(warnings)} coverage warnings" if warnings else ''
    if guard_state == 'UNAVAILABLE' or not fresh:
        label = 'STALE' if guard.get('stale') and guard.get('observed_at') is not None else 'UNAVAILABLE'
        external = f"External: {label} · {count} last observed{warning_note} · B details"
        external_tone = 'warning'
    elif guard_state == 'UNCERTAIN' or material:
        external = f"External: UNCERTAIN · {count} observed{warning_note} · B details"
        external_tone = 'caution'
    else:
        external = f"External: {count} observed{warning_note} · read only · B details"
        external_tone = 'caution' if warnings or count else 'neutral'

    def result(category, tone, summary):
        return AdmissionPresentation(category, tone, summary, external, external_tone)

    def blocked(reason, *, warning=False):
        # A permitted projection is still only awaiting the controller's checks.
        label = 'CAUTION' if decision.get('permitted') else 'BLOCKED'
        return result('WARNING' if warning else 'CAUTION', 'warning' if warning else 'caution',
                      f"Admission: {label} — {reason} · B details")

    if decision.get('conflict') or (mode == 'sequential' and active and observations):
        return blocked('Managed/external activity conflict', warning=True)
    if any(job.get('status') == 'Unknown' for job in active):
        return blocked('Managed execution ownership unknown', warning=True)
    if control.get('stale'):
        return blocked('Controller stale', warning=True)
    if control.get('stale') is not False:
        return blocked('Controller freshness unavailable')
    if control.get('online') is not True:
        return blocked('Controller offline', warning=bool(control.get('dispatch')))
    if guard_state == 'UNAVAILABLE' or not fresh:
        return blocked('External observation stale or unavailable', warning=True)
    if guard_state == 'UNCERTAIN' or material:
        return blocked('External observation uncertain; coverage limited')
    if guard_state not in {'NO_MATCH', 'BUSY'}:
        return blocked('External observation state not recognized')
    if guard_state == 'BUSY' or observations:
        return blocked(decision.get('reason') or guard.get('reason') or 'Recognized external computation observed')
    if decision.get('warnings'):
        return blocked(decision['warnings'][0])
    if any(job.get('status') == 'Cancelling' for job in active):
        return blocked('Managed cancellation is not confirmed')
    if control.get('stop'):
        return blocked(control.get('reason') or 'Controller stop requested')
    if control.get('dispatch') is not True:
        return result('WAITING', 'neutral', 'Scheduling: Dispatch paused · B details')
    if mode == 'sequential' and len(active) == 1 and active[0].get('status') == 'Run':
        running = active[0]
        if running.get('scheduling', {}).get('role', 'primary') == 'primary' and decision.get('permitted') is False:
            queued = sum(job.get('status') == 'Queue' for job in pending)
            suffix = f" · {queued} queued" if queued else ''
            return result('NORMAL_CAPACITY', 'neutral',
                          f"Scheduling: Sequential · Slot occupied by {running.get('display_id', 'managed job')}{suffix}")
    if not active and not pending and decision.get('permitted') is False and mode in {'sequential', 'bounded-sharing'}:
        return result('IDLE', 'neutral', 'Scheduling: No pending jobs · B details')
    if decision.get('permitted') is True:
        return result('WAITING', 'neutral', 'Admission: Awaiting controller check · B details')
    if mode == 'sequential' and not active and pending:
        head = min(pending, key=lambda job: job.get('order', 0))
        if head.get('status') == 'Hold':
            return result('WAITING', 'neutral', 'Scheduling: Queue head on Hold · B details')
    # Bounded refusals and incomplete/contradictory evidence are deliberately not
    # translated into ordinary capacity based on the human-readable reason.
    return blocked(decision.get('reason') or 'Admission evidence unavailable')


def scheduling_detail(state):
    """Keep full original diagnostics reachable from the compressed dashboard."""
    return (f"Mode: {state.get('mode', 'Sequential')}\n" + sharing_text(state) + "\n" + guard_text(state)
            + "\n\nRequested resources, admission commitments and observed usage are separate.\n"
            + "Declarations are not OS-enforced limits; external processes remain read-only.\n\n"
            + json.dumps({key: state.get(key) for key in
                          ('controller', 'scheduler', 'admission', 'external_activity', 'controller_guard')},
                         indent=2, ensure_ascii=False))
