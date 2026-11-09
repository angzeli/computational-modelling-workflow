"""Generate schema-4 records through the pinned deployed Store, not current Store."""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import socket
import types
from uuid import uuid4

import psutil


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'jobs_31616be'
COMMIT = '31616be474b2e2fd04ab6257c990983d46ddfcde'


def _source(name):
    manifest = json.loads((FIXTURE / 'provenance.json').read_text())
    if manifest['commit'] != COMMIT or manifest['tag'] != 'deployed/31616be-j4-1':
        raise AssertionError('Historical retirement fixture provenance changed')
    source = (FIXTURE / name).read_bytes()
    if hashlib.sha256(source).hexdigest() != manifest['files'][name]['sha256']:
        raise AssertionError(f'Historical retirement fixture source changed: {name}')
    return source


@lru_cache(maxsize=1)
def _legacy_store():
    module = types.ModuleType('cmw.jobs._retirement_legacy_store')
    module.__package__ = 'cmw.jobs'
    exec(compile(_source('store.py.txt'), str(FIXTURE / 'store.py.txt'), 'exec'),
         module.__dict__)
    return module.Store


def capture_v1(pid):
    """Run the exact deployed identity function against a synthetic process."""
    namespace = {'os': os, 'psutil': psutil, 'socket': socket}
    exec(compile(_source('identity.py.txt'), str(FIXTURE / 'identity.py.txt'), 'exec'),
         namespace)
    return namespace['identity'](pid)


def create(root, *, worker, group, controller=None, members=()):
    """Return (Unknown job, queue identity) written by the deployed schema-4 API.

    Callers own the synthetic processes: capture identities while they are alive,
    call this helper, and end/reap them before candidate retirement. The historical
    Store is unmodified. Its empty progress declarations and non-VASP add-time
    validation use installed CMW helpers; they do not write the queue or alter its
    historical job/identity format. No historical runner or real engine is run.
    """
    root = Path(root).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    store = _legacy_store()(root)
    queue_identity = store.enroll_queue('synthetic-retirement-node', 'synthetic-retirement-host')
    job = store.add(argv=['/bin/true'], cwd=root.parent, name='deployed 31616be Unknown',
                    engine='Command')
    with store.transaction() as con:
        job.update(status='Unknown', worker=dict(worker), group=dict(group),
                   recorded_descendants=[dict(member) for member in members],
                   claim=uuid4().hex, worker_protocol=3, started_at=1.0,
                   reason='Synthetic historical worker lost observation')
        store.save(con, job)
        if controller is not None:
            control = store.control(con)
            control['owner'] = dict(controller)
            store.set_control(con, control)
        store.event(con, job['id'], 'Synthetic Unknown fixture created with deployed 31616be Store')
    (root / 'attempts' / job['attempt_id']).mkdir(parents=True, mode=0o700)
    return job, queue_identity
