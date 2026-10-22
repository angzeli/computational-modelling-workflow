"""Installed-capable, benign node workflow; all states and processes are owned.

The injected runtime launcher scopes process discovery and admission to fixtures.
It never changes production settings or launches a scientific engine.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import psutil

from cmw.jobs import runtime
from cmw.jobs.ownership import identity, owner_alive
from cmw.jobs.store import Store
from cmw.nodes.client import apply_control, fetch, submit
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.identity import enroll
from cmw.nodes.transport import ProcessTransport
from tests.jobs.isolated_runtime import environment, install


def wait_for(predicate, *, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Owned node fixture did not reach its bounded condition')


def node_surface(root, *, backend_builder=None):
    """Actual endpoint -> transfer -> Hold -> explicit controls -> replica.

    backend_builder is an acceptance-harness-only alternative for actual SSH.
    Default traffic uses two real stdio endpoint sessions, not a mocked Store.
    """
    root = Path(root).resolve()
    root.mkdir(mode=0o700)
    storage = root / 'calculation'
    storage.mkdir(mode=0o700)
    script = root / 'selected-fixture.sh'
    script.write_text('''#!/bin/sh
set -eu
printf 'ready\\n' > started
count=0
while [ ! -f release ]; do
    /bin/sleep 0.05
    count=$((count + 1))
    if [ "$count" -gt 600 ]; then exit 73; fi
done
printf 'benign installed node result\\n' > result.txt
printf 'completed\\n'
''')
    config_root = root / 'node'
    config = enroll(config_root, {'queues': {'research': str(root / 'queue')},
        'storages': {'scratch': str(storage)},
        'profiles': {'benign': {'engine': 'Command', 'runner': '/bin/sh', 'max_cpus': 1,
                                'dependencies': ['/bin/sleep']}}})
    entry = {'transport': 'local', 'config_root': str(config_root), 'node_id': config['node_id'],
             'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
             'queues': {'research': config['queues']['research']['queue_id']},
             'default_queue': 'research', 'read_only': False}
    store = Store(root / 'queue')
    frontend = root / 'frontend'
    owners, endpoint_owners, payload_owners, checks = [], [], [], {}
    payload_census = None
    started_at = time.time()
    backend, work = None, None
    def connect():
        if backend_builder is not None:
            return backend_builder(entry)
        argv = [sys.executable, '-B', '-m', 'tests.jobs.isolated_runtime', 'cli',
                'nodes', 'endpoint', '--config', str(config_root), '--queue', 'research']
        return ConnectedBackend('fixture-mini', entry,
            transport=ProcessTransport(argv, timeout=30, env=environment()))
    def close_backend():
        process = getattr(getattr(backend, 'transport', None), 'process', None)
        if process is not None and process.poll() is None:
            endpoint_owners.append(identity(process.pid))
        backend.close()
    def control(action, target=None, confirm=False):
        plan = backend.call('control.preview', {'action': action}, target)['result']
        return apply_control(backend, plan, root=frontend, confirm=confirm)['result']
    try:
        # Existing fixture-only quiet-window handling avoids binding metadata
        # while Desktop indexing is still settling a newly written source.
        from tests.periodic.vasp.handoff_case import wait_for_fixture_sources
        wait_for_fixture_sources((script,), quiet_seconds=6, timeout=20)
        backend = connect()
        spec = {'schema': 1, 'request_id': uuid4().hex, 'node_id': config['node_id'],
            'queue_id': entry['queues']['research'], 'storage': 'scratch', 'profile': 'benign',
            'engine': 'Command', 'name': 'Installed benign remote workflow',
            'resources': {'cpus': 1, 'memory_gib': .1}, 'intent': 'held', 'dependencies_complete': True,
            'files': [{'source': str(script), 'path': 'support/fixture.sh', 'role': 'support'}],
            'arguments': [{'bundle': 'support/fixture.sh'}]}
        preview = submit(backend, spec, root=frontend, preview=True)
        assert preview['result']['intent'] == 'Hold'
        registered = submit(backend, spec, root=frontend)['result']
        job = registered['job']
        work = Path(job['cwd'])
        target = {'node_id': config['node_id'], 'queue_id': entry['queues']['research'],
                  'job_id': job['id'], 'attempt_id': job['attempt_id']}
        assert job['status'] == 'Hold' and not (work / 'started').exists()
        checks['verified_transfer_default_hold'] = True
        replay = submit(backend, spec, root=frontend)['result']
        assert replay['replayed'] and replay['job']['attempt_id'] == job['attempt_id']
        checks['durable_registration_retry'] = True
        control('release', target)
        assert not store.snapshot()['controller']['dispatch']
        assert not (work / 'started').exists()
        checks['release_does_not_enable_dispatch'] = True
        control('hold', target)
        start_receipt = control('controller.start')
        wait_for(lambda: store.snapshot()['controller']['online'])
        time.sleep(.25)
        assert store.snapshot()['jobs'][0]['status'] == 'Hold' and not (work / 'started').exists()
        checks['online_controller_does_not_execute_hold'] = True
        control('release', target)
        wait_for(lambda: (work / 'started').exists())
        active = store.snapshot()
        assert active['jobs'][0]['status'] == 'Run'
        active_job = active['jobs'][0]
        owners.extend(item for item in (active['controller']['owner'], active_job['worker'],
                                       active_job['group']) if item)
        anchor = active_job['group']
        assert owner_alive(anchor)
        parent = psutil.Process(anchor['pid'])
        assert parent.create_time() == anchor['birth']
        observed_at = time.time()
        for child in parent.children(recursive=True):
            try:
                observed = identity(child.pid)
                if observed['birth'] == child.create_time():
                    payload_owners.append(observed)
            except psutil.NoSuchProcess:
                pass  # A short-lived fixture descendant may end during census.
        assert owner_alive(anchor)
        payload_census = {'scope': 'Observed descendants of the verified payload anchor; one census',
                          'observed_at': observed_at, 'anchor': anchor}
        close_backend()
        backend = None
        time.sleep(.15)
        assert all(owner_alive(owner) for owner in owners)
        assert store.snapshot()['jobs'][0]['status'] == 'Run'
        checks['detached_execution_survives_endpoint_exit'] = True
        (work / 'release').write_text('explicit fixture completion\n')
        wait_for(lambda: store.snapshot()['jobs'][0]['status'] == 'Done')
        backend = connect()
        status = backend.call('status')
        assert status['result']['state']['jobs'][0]['attempt_id'] == target['attempt_id']
        progress = backend.call('progress', target=target)
        assert progress['result']['snapshot'] is not None
        locations = backend.call('locations', target=target)['result']['locations']
        location = next(item for item in locations if item['node_path'] == str(work))
        wait_for_fixture_sources((work / 'result.txt',), quiet_seconds=6, timeout=20)
        result = fetch(backend, target, {'schema': 1, 'storage': 'scratch', 'purpose': 'summaries',
            'capture': 'stable', 'large_files': False,
            'files': [{'location_id': location['location_id'], 'relative': 'result.txt', 'path': 'result.txt'}]},
            root / 'replica', root=frontend)['result']
        assert (root / 'replica/result.txt').read_text() == 'benign installed node result\n'
        replica = json.loads((root / 'replica/fetch-manifest.json').read_text())
        assert target['attempt_id'] in json.dumps(replica)
        assert result['status'] == 'FETCHED'
        assert result.get('acknowledgment_pending') is False
        checks['completed_snapshot_and_verified_replica'] = True
        control('controller.stop')
        wait_for(lambda: not store.snapshot()['controller']['online'])
        wait_for(lambda: not any(owner_alive(owner) for owner in [*owners, *payload_owners]))
        checks['owned_runtime_processes_exited'] = True
        return {'checks': checks, 'node_id': config['node_id'], 'queue_id': entry['queues']['research'],
                'target': target, 'runtime': config['runtime'], 'package_origin': __import__('cmw').__file__,
                'owners': owners, 'endpoint_owners': endpoint_owners,
                'payload_owners': payload_owners, 'payload_census': payload_census,
                'start_receipt': start_receipt, 'fetch': result,
                'scientific_engine_executed': False}
    except BaseException as exc:
        # Preserve this isolated queue before the caller's temporary-directory
        # cleanup, including an admission timeout that never started a worker.
        import tempfile
        from tests.jobs.lifecycle_evidence import capture
        evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
        if evidence:
            Path(evidence).mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = Path(tempfile.mkdtemp(prefix='node-workflow-failure-', dir=evidence))
        capture(store, destination, outcome=f'{type(exc).__name__}: {exc}', started_at=started_at)
        print(f'Node workflow failure evidence: {destination}', flush=True)
        raise
    finally:
        if work is not None and work.is_dir():
            (work / 'release').touch(exist_ok=True)
        # Only this newly enrolled queue is reachable through this cleanup path.
        if store.path.exists():
            cleanup_state = store.snapshot()
            for owner in [cleanup_state['controller'].get('owner'),
                          *[job.get(kind) for job in cleanup_state['jobs']
                            for kind in ('worker', 'group')]]:
                if owner and owner not in owners:
                    owners.append(owner)
            with install():
                runtime.stop(store)
            deadline = time.monotonic() + 8
            while (any(owner_alive(owner) for owner in [*owners, *payload_owners])
                   and time.monotonic() < deadline):
                time.sleep(.05)
        if backend is not None:
            close_backend()
        remaining = [owner for owner in [*owners, *endpoint_owners, *payload_owners] if owner_alive(owner)]
        (root / 'cleanup.json').write_text(json.dumps({
            'owners': owners, 'endpoint_owners': endpoint_owners, 'payload_owners': payload_owners,
            'payload_census': payload_census, 'remaining_verified_owners': remaining}, indent=2) + '\n')
        if remaining:
            raise AssertionError('Verified owned fixture runtime remained live; evidence retained')
