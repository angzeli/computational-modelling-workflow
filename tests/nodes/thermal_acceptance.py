"""Installed-capable thermal endpoint acceptance using only disposable held jobs.

The endpoint is real LocalBackend/stdio with actual psutil machine accounting.
The injected provider emits synthetic macmon JSON; the activity inventory is
explicitly empty so this harness never enumerates or operates research processes.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import statistics
import sys
import time
from uuid import uuid4

from cmw.jobs.store import Store
from cmw.nodes.codec import decode_typed
from cmw.nodes.connections import ConnectedBackend, observed_machine_usage
from cmw.nodes.identity import enroll
from cmw.nodes.protocol import NodeError
from cmw.nodes.transport import ProcessTransport
from cmw.progress.models import ProgressSnapshot, SourceDescriptor
from tests.jobs.thermal_fixtures import write_fake_macmon
from tests.periodic.vasp.result_fixtures import native_texts


def endpoint(config, provider):
    from cmw.jobs.activity import Observer, collect
    from cmw.jobs.telemetry import Sampler
    from cmw.jobs.thermal import ThermalProvider
    from cmw.nodes.backend import LocalBackend
    from cmw.nodes.endpoint import serve

    def factory(config_root, queue):
        backend = LocalBackend(config_root, queue)
        backend._activity_observer = Observer(collector=lambda store, state, deadline:
            collect(store, state, deadline, pids_provider=lambda: []))
        backend._sampler = Sampler(thermal=ThermalProvider(
            None if provider == 'disabled' else provider, system='Darwin', machine='arm64'))
        return backend
    return serve(config, 'research', backend_factory=factory)


def thermal_node_surface(root, *, backend_builder=None):
    """Two fixed-temperature endpoints and disabled control, using real held Stores.

    backend_builder(entry, argv, env) may wrap the exact trusted endpoint command
    in the existing private SSH harness. No physical node or scientific engine
    is contacted. All returned measurements are synthetic fixture acceptance.
    """
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True)
    channels, endpoints, samples = {}, [], {}
    checks, timings = {}, {}
    def make(alias, temperature):
        here = root / alias
        here.mkdir(mode=0o700)
        storage = here / 'calculation'
        storage.mkdir(mode=0o700)
        config = enroll(here / 'node', {'queues': {'research': str(here / 'queue')},
            'storages': {'scratch': str(storage)},
            'profiles': {'benign': {'engine': 'Command', 'runner': '/bin/echo', 'max_cpus': 1}}})
        store = Store(here / 'queue')
        texts = native_texts()
        for name in ('OUTCAR', 'OSZICAR'):
            (storage / name).write_text(texts[name])
        job = store.add(argv=['/bin/echo', 'never executed'], cwd=storage, name=alias,
            engine='VASP', hold=True, progress_sources=tuple(SourceDescriptor(role, str(storage / name))
                for role, name in (('outcar', 'OUTCAR'), ('oszicar', 'OSZICAR'))))
        path = write_fake_macmon(here / 'fake-macmon', temperature=temperature) if temperature is not None else None
        entry = {'transport': 'local', 'config_root': str(here / 'node'), 'node_id': config['node_id'],
            'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
            'queues': {'research': config['queues']['research']['queue_id']},
            'default_queue': 'research', 'read_only': True}
        argv = [sys.executable, '-B', '-m', 'tests.nodes.thermal_acceptance', 'endpoint',
                str(here / 'node'), str(path) if path else 'disabled']
        env = {k: v for k, v in os.environ.items() if not k.startswith('CMW_THERMAL_')}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        def connect():
            if backend_builder is not None:
                return backend_builder(entry, argv, env)
            return ConnectedBackend(alias, entry, transport=ProcessTransport(argv, env=env, timeout=15))
        backend = connect()
        channels[alias] = backend
        return {'backend': backend, 'connect': connect, 'store': store, 'job': job,
                'before': store.path.read_bytes(), 'provider': path}
    try:
        records = {name: make(name, value) for name, value in (('MBP', 63.2), ('MINI', 76.4), ('OFF', None))}
        assert len({record['job']['display_id'] for record in records.values()}) == 1
        checks['same_display_job_id_fixtures'] = True
        for alias, record in records.items():
            backend = record['backend']
            capabilities = backend.call('capabilities')
            assert capabilities['result']['readiness']['identity_ready']
            before = time.perf_counter()
            status = backend.call('status')
            first_latency = time.perf_counter()-before
            deadline = time.monotonic()+7
            while alias != 'OFF' and status['result']['state']['machine_usage']['thermal']['quality'] != 'fresh':
                if time.monotonic() >= deadline:
                    raise AssertionError('Synthetic endpoint thermal fixture did not become fresh: '+repr(status))
                time.sleep(.05)
                status = backend.call('status')
            target = status['result']['state']['jobs'][0]['target']
            machine = status['result']['state']['machine_usage']
            if alias == 'OFF':
                assert machine['thermal']['quality'] == 'unavailable'
            else:
                assert machine['thermal']['temperature_celsius'] == {'MBP': 63.2, 'MINI': 76.4}[alias]
            assert machine['memory_source'] == 'psutil.virtual_memory'
            assert machine['ram_used_bytes'] == machine['ram_total_bytes']-machine['ram_available_bytes']
            statuses, progress = [], []
            for _ in range(5):
                before = time.perf_counter()
                status = backend.call('status')
                statuses.append(time.perf_counter()-before)
                before = time.perf_counter()
                science = backend.call('progress', target=target)
                progress.append(time.perf_counter()-before)
            snapshot = decode_typed(science['result']['snapshot'], ProgressSnapshot)
            assert snapshot.context.attempt_id == target['attempt_id']
            assert 'thermal' not in snapshot.to_dict()
            assert all(str(root/alias) in item.path for item in snapshot.context.sources)
            preview = backend.call('control.preview', {'action': 'release'}, target)['result']
            assert preview['target'] == target
            assert backend.call('show', target=target)['result']['job']['status'] == 'Hold'
            files = backend.call('locations', target=target)['result']['locations']
            assert all(item['node_id'] == target['node_id'] for item in files)
            assert backend.call('capabilities')['result']['readiness'] == capabilities['result']['readiness']
            timings[alias] = {'first_status_seconds': first_latency, 'status_seconds': statuses,
                'status_median_seconds': statistics.median(statuses), 'progress_seconds': progress,
                'progress_median_seconds': statistics.median(progress)}
            samples[alias] = {'node_id': status['node_id'], 'queue_id': status['queue_id'],
                'machine_usage': machine, 'target': target, 'status_envelope': deepcopy(status)}
            if record['provider']:
                calls = [json.loads(line) for line in Path(str(record['provider'])+'.calls').read_text().splitlines()]
                assert calls == [['--version'], ['pipe', '--help'], ['pipe', '--samples', '1', '--interval', '1000']], calls
            checks[alias+'_machine_science_controls_files_isolated'] = True
        left, right = samples['MBP'], samples['MINI']
        assert left['target'] != right['target']
        try:
            channels['MBP'].call('control.preview', {'action': 'release'}, right['target'])
        except NodeError as error:
            assert error.code == 'WRONG_NODE'
        else:
            raise AssertionError('Cross-node control preview was not refused')
        checks['wrong_node_control_target_refused'] = True
        old = right['status_envelope']
        remote = channels['MINI']
        endpoints.append(remote.transport.process)
        remote.close()
        cached = observed_machine_usage(old, 1000., 1001., connected=False)
        assert cached['thermal']['quality'] == 'stale'
        assert observed_machine_usage(old, 1000., 1061., connected=False)['thermal']['quality'] == 'unavailable'
        assert channels['MBP'].call('status')['result']['state']['machine_usage']['thermal']['temperature_celsius'] == 63.2
        remote = records['MINI']['connect']()
        channels['MINI'] = remote
        new = remote.call('status')
        assert new['session_id'] != old['session_id']
        assert new['result']['state']['machine_usage']['thermal']['quality'] == 'unavailable'
        deadline = time.monotonic()+7
        while new['result']['state']['machine_usage']['thermal']['quality'] != 'fresh':
            if time.monotonic() >= deadline:
                raise AssertionError('Reconnected synthetic provider did not become fresh')
            time.sleep(.05)
            new = remote.call('status')
        assert new['result']['state']['machine_usage']['thermal']['sample_timestamp'] >= old['result']['state']['machine_usage']['thermal']['sample_timestamp']
        checks['disconnect_local_updates_and_new_session_refresh'] = True
        for record in records.values():
            assert record['store'].path.read_bytes() == record['before']
            state = record['store'].snapshot()
            assert not state['controller']['dispatch'] and state['jobs'][0]['status'] == 'Hold'
        checks['all_stores_unchanged_no_execution'] = True
    finally:
        for backend in channels.values():
            process = getattr(getattr(backend, 'transport', None), 'process', None)
            if process is not None:
                endpoints.append(process)
            backend.close()
    assert all(process.poll() is not None for process in endpoints)
    checks['owned_endpoint_cleanup'] = True
    return {'checks': checks, 'timings': timings, 'samples': samples,
            'package_origin': __import__('cmw').__file__, 'synthetic_temperature_only': True,
            'scientific_engine_executed': False, 'endpoint_returncodes': [p.poll() for p in endpoints]}


if __name__ == '__main__':
    if sys.argv[1] == 'endpoint':
        raise SystemExit(endpoint(sys.argv[2], sys.argv[3]))
    result = thermal_node_surface(sys.argv[1])
    print(json.dumps(result, indent=2))
