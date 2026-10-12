"""Literal operational suspension presentation; no ownership or signal authority."""
from __future__ import annotations

import json
import math


def duration(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        return 'uncertain'
    hours, remainder = divmod(int(value), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'


def effective_status(job):
    """Keep terminal/Unknown evidence authoritative over an operational phase."""
    record = job.get('suspension') or {}
    phase = record.get('phase')
    if job.get('status') == 'Run' and phase in {'Suspending', 'Suspended', 'Resuming', 'Unverified'}:
        return 'Suspended?' if phase == 'Suspended' and record.get('verification_fresh') is False else phase
    return job.get('status', 'Unknown')


def intended_action(job):
    phase = (job.get('suspension') or {}).get('phase', 'Run')
    return 'continue' if phase == 'Suspended' else 'suspend'


def suspension_summary(job, *, compact=False):
    record = job.get('suspension') or {}
    phase = effective_status(job)
    if not record:
        return ''
    if phase == 'Suspended?':
        return 'Stored suspension · Fresh worker verification required'
    if phase == 'Suspended':
        if compact:
            return f"Suspended — user requested · {duration(record.get('current_suspended_seconds'))}"
        return ('Suspended — user requested · Original attempt retained · '
                f"Suspended for: {duration(record.get('current_suspended_seconds'))}")
    if phase in {'Suspending', 'Resuming', 'Unverified'}:
        return f"{phase} — {record.get('reason') or 'worker verification pending'} · Resources retained"
    if record.get('intervals'):
        return f"Original attempt retained · Confirmed suspension: {duration(record.get('suspended_seconds'))}"
    return ''


def readiness_text(result):
    """Do not turn missing qualification or accounting into a positive claim."""
    resources = result.get('resources') or {}
    qualification = result.get('qualification') or {}
    action = result.get('action', 'suspend').title()
    reasons = result.get('reasons') or ([result['reason']] if result.get('reason') else [])
    lines = [f"{action} job · {result.get('display_id', '?')} / {result.get('name', '?')}",
             f"Immutable attempt: {result.get('attempt_id', '?')}",
             f"Readiness: {'READY' if result.get('ready') else 'UNAVAILABLE'} · Phase: {result.get('phase', '?')}",
             f"Runtime/backend: {result.get('backend') or 'unavailable'} · Protocol: {result.get('protocol') or 'unavailable'}"]
    lines.extend(str(reason) for reason in reasons)
    lines.extend(('Deadline policy: ' + json.dumps(result.get('deadline') or {'status': 'unknown'}, ensure_ascii=False),
                  f"Scientific-engine qualification: {'recorded' if qualification.get('engine') is True else 'pending'}",
                  f"Physical sleep/wake qualification: {'recorded' if qualification.get('sleep_wake') is True else 'pending'}",
                  f"RAM commitment retained: {resources.get('memory_gib') or 'undeclared'} GiB · CPU commitment: {resources.get('cpus') or 'undeclared'}",
                  f"Computational scope: {result.get('computational_scope') or 'not verified'}",
                  f"Protected controls: {result.get('protected_scope') or 'not verified'}",
                  'Required attached storage: ' + json.dumps(result.get('storage') or [], ensure_ascii=False),
                  f"Native MPI qualification: {qualification.get('native_mpi') or 'not established'}"))
    others = result.get('other_active') or []
    lines.append('Other known active computations: ' + (json.dumps(others, ensure_ascii=False) if others else 'none recorded in this Jobs state'))
    lines.append('Other applications are unaffected. Memory and storage remain required; no permission to disconnect storage or close the lid.')
    lines.append('Continue is manual; no restart, replacement attempt or automatic timer.')
    return '\n'.join(lines)


def submission_text(result):
    outcome = result.get('result', 'PENDING')
    if outcome == 'ACCEPTED':
        status = 'Request ACCEPTED; completion is pending worker verification.'
    elif outcome == 'COMPLETED':
        status = 'Request COMPLETED according to the recorded worker verification.'
    else:
        status = 'Request PENDING; completion has not been confirmed.'
    return f"{status}\n{result.get('display_id', '?')}: {result.get('phase', '?')}\nInspect jobs show/status for current evidence."


def suspension_detail(job):
    record = job.get('suspension')
    if not record:
        return ''
    from .cli import usage_cells
    _, ram = usage_cells(job.get('usage'))
    scope = (record.get('evidence') or {}).get('computational', [])
    label = 'Verified computational scope' if record.get('verification_fresh') is True else 'Stored computational scope; fresh verification required'
    lines = [suspension_summary(job), 'Original attempt retained: ' + job['attempt_id'],
             label + ': ' + json.dumps(scope, ensure_ascii=False),
             'Suspended for: ' + duration(record.get('current_suspended_seconds')),
             'Confirmed suspended total (recorded): ' + duration(record.get('suspended_seconds')),
             'Unsuspended elapsed (not CPU time): ' + duration(record.get('unsuspended_elapsed')),
             'RAM retained (observed RSS): ' + ram,
             'Deadline: ' + json.dumps((record.get('capability') or {}).get('deadline') or {'status': 'unknown'}, ensure_ascii=False),
             'Native evaluation/SCF and source age remain in the progress evidence below.',
             'Operational suspension evidence (native engine output is unchanged):',
             json.dumps(record, indent=2, ensure_ascii=False)]
    return '\n'.join(lines)
