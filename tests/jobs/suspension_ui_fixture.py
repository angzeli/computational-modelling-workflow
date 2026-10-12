"""PTY-only fictional authority; deliberately no process backend or OS signals."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

from cmw.jobs.store import Store


def readiness(store, job_id, action):
    state = store.snapshot()
    job = next(job for job in state['jobs'] if str(job_id) in {str(job['id']), job['display_id']})
    record = job['suspension']
    return dict(supported=True, ready=True, reasons=['Fictional UI authority; no processes controlled'],
                job_id=job['id'], display_id=job['display_id'], name=job['name'], attempt_id=job['attempt_id'],
                action=action, phase=record['phase'], revision=record['revision'],
                authority={'worker': job['worker'], 'claim': 'ui-fixture-only'}, backend='fictional-supported-v1',
                protocol=1, computational_scope='fictional state only', protected_scope='not applicable; no process control',
                deadline={'policy': 'fictional pause-aware', 'hard_wall_deadline': None},
                storage=[str(store.root.parent)], resources={'cpus': 1, 'memory_gib': 1},
                qualification={'engine': False, 'sleep_wake': False, 'native_mpi': 'not tested by this UI fixture'},
                other_active=[])


def submit(store, job_id, action, **expected):
    from cmw.jobs.store import JobsError
    result = readiness(store, job_id, action)
    if (expected['expected_attempt_id'] != result['attempt_id'] or expected['expected_revision'] != result['revision']
            or expected['expected_phase'] != result['phase'] or expected['expected_authority'] != result['authority']):
        raise JobsError('Stale fictional request; reopen confirmation')
    path = store.root/'ui-submissions.json'
    requests = json.loads(path.read_text()) if path.exists() else []
    requests.append({'action': action, 'reason': expected['reason'], 'attempt_id': result['attempt_id']})
    path.write_text(json.dumps(requests))
    with store.transaction() as con:
        job = store.get(con, job_id)
        job['suspension'].update(phase='Suspended' if action == 'suspend' else 'Run',
                                 revision=result['revision'] + 1, inhibited=action == 'suspend')
        store.save(con, job)
    return {**result, 'result': 'COMPLETED', 'phase': 'Suspended' if action == 'suspend' else 'Run'}


def make_fixture(root):
    store = Store(Path(root)/'state')
    job = store.add(argv=['/unexecuted/fictional'], cwd=Path(root), name='Fictional UI-only attempt', hold=True)
    with store.transaction() as con:
        current = store.get(con, job['id'])
        current.update(status='Run', order=None, worker={'fixture': 'no-live-authority'},
                       suspension={'phase': 'Run', 'revision': 1, 'inhibited': False,
                                   'reason': 'UI-only fixture', 'suspended_seconds': 0,
                                   'current_suspended_seconds': 0})
        store.save(con, current)
    return store, job


def main():
    from cmw.jobs.tui import JobsApp
    with patch('cmw.jobs.suspension.readiness', readiness), patch('cmw.jobs.suspension.submit', submit), \
            patch.object(JobsApp, 'collect_activity'):
        JobsApp(Store(sys.argv[1])).run()


if __name__ == '__main__':
    main()
