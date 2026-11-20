"""Owner-side listing of native and backfilled manifest jobs (#41 Phase 3c, #74; backfilled #76).

Rows come from one read-only pass of the Phase 3b checker, so each row carries
its integrity status and shows fields only from sources the checker accepted:
an edited job.json or annotations.json contributes nothing. Nothing is cached or
written, so a listing can never become an integrity anchor. Remote jobs'
state.json lives on their target, so their status and SCF result are unavailable.
"""
from __future__ import annotations

from .catalogue_models import text_value
from .manifest_annotations import OUTCOMES
from .manifest_check import check
from .manifest_legacy import QUALIFIED, order_key
from .manifest_state import STATUS
from .manifests import NATIVE_ID, ManifestError

STATUSES = tuple(dict.fromkeys(value for value in STATUS.values() if value))
# The SCF classifier's vocabulary (cmw.periodic.vasp.scf_classifier.RESULTS), kept
# literal so listing never imports the VASP output readers.
SCF_RESULTS = ('converged', 'labort', 'nelm_reached', 'crashed', 'unknown')
FILTERS = ('system', 'status', 'scf_result', 'outcome', 'parent')


def _filters(filters):
    unknown = set(filters) - set(FILTERS)
    if unknown:
        raise ManifestError(f'Manifest listing filters are {", ".join(FILTERS)}')
    chosen = {key: value for key, value in filters.items() if value is not None}
    allowed = {'status': STATUSES, 'scf_result': SCF_RESULTS, 'outcome': OUTCOMES}
    for key, values in allowed.items():
        if key in chosen and chosen[key] not in values:
            raise ManifestError(f'{key} must be one of {", ".join(values)}')
    if 'parent' in chosen and not (isinstance(chosen['parent'], str) and (
            NATIVE_ID.fullmatch(chosen['parent']) or QUALIFIED.fullmatch(chosen['parent']))):
        raise ManifestError('parent must be a native project ID (J<n>) or a queue-qualified legacy ID')
    if 'system' in chosen:
        try:
            chosen['system'] = text_value(chosen['system'], 'System', empty=False)
        except ValueError as exc:
            raise ManifestError(str(exc)) from exc
    return chosen


def _row(job):
    notes = job.get('annotations') or {}
    codes = [item['code'] for item in job['findings']]
    # A state.json the checker rejected (unreadable, wrong identity or binding) contributes nothing.
    state_ok = not any(item['level'] == 'error' and item['code'].startswith('state') for item in job['findings'])
    return {'id': job['id'], 'integrity': job['status'], 'engine': job.get('engine'),
            'status': job.get('state') if state_ok else None,
            'scf_result': job.get('scf_result') if state_ok else None,
            'system': notes.get('system'), 'outcome': notes.get('outcome'), 'label': notes.get('label'),
            'parent': job.get('parent'), 'remote': 'remote' in codes,
            'errors': sum(item['level'] == 'error' for item in job['findings'])}


def list_jobs(store, root, *, volume_uuid_of=None, **filters):
    """Known native and backfilled jobs in numeric ID order, each with its integrity status; exact AND filters."""
    chosen = _filters(filters)
    result = check(store, root, volume_uuid_of=volume_uuid_of)
    rows = sorted((_row(job) for job in result['jobs']), key=lambda row: order_key(row['id']))
    records = [row for row in rows if all(row[key] == value for key, value in chosen.items())]
    return {'project': result['project'], 'root': result['root'], 'integrity': result['status'],
            'errors': result['errors'], 'findings': result['findings'], 'filters': chosen,
            'matching_count': len(records), 'total_count': len(rows), 'records': records}


def summary_text(result):
    lines = [f'{result["project"]}: {result["matching_count"]} matching / {result["total_count"]} total jobs '
             f'· integrity {result["integrity"]} at {result["root"]}']
    for row in result['records']:
        status = 'remote' if row['remote'] else row['status'] or '—'
        lines.append('  '.join([row['id'], row['integrity'], row['engine'] or '—', status, row['scf_result'] or '—',
                                row['system'] or '—', row['outcome'] or '—',
                                f'parent {row["parent"]}' if row['parent'] else '', row['label'] or '']).rstrip())
    if result['errors']:
        lines.append(f'{result["errors"]} integrity error(s); run `cmw jobs check --manifest-project` for details')
    return '\n'.join(lines)
