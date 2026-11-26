"""The scheduler-neutral report of a scheduler-backed node's jobs (#81 S07).

An adapter behind `scheduler.SchedulerBackend` fills it; the frontend, the CLI
and the portal lane only read it, whatever scheduler produced it. Rows are
scheduler-reported and read-only: they carry no CMW target and authorize nothing.
"""
from __future__ import annotations

from .protocol import NodeError

PHASES = ('queued', 'running', 'finished', 'other')
QUALIFICATION = 'Scheduler-reported, read-only; not CMW-managed and never adopted'
TEXT_FIELDS = ('name', 'state', 'queue', 'server', 'elapsed', 'comment')


def _text(value, limit, *, optional=True):
    if value is None:
        return optional
    return isinstance(value, str) and len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def validate_report(report):
    """Refuse a malformed report before it can replace a lane's rows."""
    if (not isinstance(report, dict) or type(report.get('schema')) is not int or report['schema'] != 1
            or not _text(report.get('kind'), 32, optional=False) or not isinstance(report.get('jobs'), list)
            or len(report['jobs']) > 1000 or not isinstance(report.get('counts'), dict)
            or set(report['counts']) != set(PHASES)
            or any(type(count) is not int or count < 0 for count in report['counts'].values())):
        raise NodeError('INVALID_RESPONSE', 'Invalid scheduler report')
    for job in report['jobs']:
        if (not isinstance(job, dict) or not _text(job.get('id'), 255, optional=False) or not job['id']
                or any(c.isspace() for c in job['id']) or job.get('phase') not in PHASES
                or not all(_text(job.get(key), 512) for key in TEXT_FIELDS)
                or not all(isinstance(job.get(key, {}), dict) for key in ('requested', 'used'))):
            raise NodeError('INVALID_RESPONSE', 'Invalid scheduler-reported job row')
    return report


def _value(value):
    return '—' if value is None else str(value)


def job_status(job):
    """A row's phase with the scheduler's own state code, e.g. `running R`."""
    return job['phase'] + (f" {job['state']}" if job.get('state') else '')


def counts_text(report):
    counts = report['counts']
    return f"{report['kind'].upper()} " + ' · '.join(f'{phase} {counts[phase]}' for phase in PHASES[:3]) + (
        f" · other {counts['other']}" if counts['other'] else '')


def coverage_text(report):
    """What the listed rows do and do not cover."""
    shown, total = len(report['jobs']), report.get('total', len(report['jobs']))
    notes = [f'{shown} of {total} shown']
    if report.get('truncated'):
        notes.append('older jobs beyond the listing bound are not shown')
    if report.get('missing'):
        notes.append(f"{report['missing']} left the scheduler's history between calls")
    return ' · '.join(notes)


def job_facts(job):
    substate = f"/{job['substate']}" if job.get('substate') is not None else ''
    return (f"{job['phase']} ({_value(job.get('state'))}{substate}) · queue {_value(job.get('queue'))} · "
            f"server {_value(job.get('server'))} · exit {_value(job.get('exit_status'))} · "
            f"runs {_value(job.get('run_count'))}")


def job_resources(job):
    def pairs(values):
        return ' '.join(f'{key}={value}' for key, value in values.items()) or '—'
    text = f"requested {pairs(job.get('requested') or {})} · used {pairs(job.get('used') or {})}"
    return text + (f" · {job['comment']}" if job.get('comment') else '')


def report_text(report):
    """Plain text for `cmw jobs --node ALIAS status` on a scheduler-backed node."""
    from cmw.jobs.cli import name_text
    version = f" {report['version']}" if report.get('version') else ''
    lines = [f"SCHEDULER JOBS — {report['kind'].upper()}{version} · {report.get('qualification') or QUALIFICATION}",
             counts_text(report) + ' · ' + coverage_text(report),
             f"{'JOB ID':<24}  {'STATUS':<10}  {'NAME':<24}  {'QUEUE':<12}  {'ELAPSED':>10}  EXIT"]
    for job in report['jobs']:
        lines.append(f"{name_text(job['id'])[:24]:<24}  {job_status(job)[:10]:<10}  "
                     f"{name_text(_value(job.get('name')))[:24]:<24}  {name_text(_value(job.get('queue')))[:12]:<12}  "
                     f"{name_text(_value(job.get('elapsed')))[:10]:>10}  {_value(job.get('exit_status'))}")
    if not report['jobs']:
        lines.append('No scheduler jobs reported for this user.')
    if report.get('next_offset') is not None:
        lines.append(f"Rows from offset {report['next_offset']} did not fit this bounded status page.")
    return '\n'.join(lines)
