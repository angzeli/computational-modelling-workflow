"""Small adapters around the existing node, history and private-SSH fixtures."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import statistics
import re
import select
import sys
import time
from uuid import uuid4

import psutil

from cmw.jobs.catalogue_models import CatalogueQuery
from cmw.jobs.store import Store
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.identity import enroll
from cmw.nodes.protocol import encode, request
from cmw.nodes.transport import ProcessTransport
from cmw.progress.models import SourceDescriptor
from tests.jobs.test_catalogue_store import populate_history
from tests.periodic.vasp.result_fixtures import native_texts, structure_text


# Fixed before execution. These bounds describe this fixture, not production SLA.
BUDGETS = {
    'wall_seconds': 240, 'history_rows': [1000, 10000], 'page_rows': 50,
    'payloads_started': 0, 'endpoint_clients': 2, 'review_workers_at_once': 1,
    'owned_subprocesses': 16,
    'cycles_per_history': 40, 'cycle_pause_seconds': .25,
    'review_per_source_bytes': 256 * 1024, 'review_deadline_seconds': 15,
    'source_fixture_bytes': 64 * 1024, 'catalogue_page_bytes': 512 * 1024,
    'changing_log_initial_bytes': 256 * 1024, 'changing_log_append_bytes': 64,
    'changing_log_poll_bytes': 64 * 1024 + 256,
    'protocol_frame_bytes': 2 * 1024 * 1024, 'protocol_total_bytes': 64 * 1024 * 1024,
    'request_p95_seconds': 2.0, 'ui_tick_p95_seconds': .35,
    'ui_render_p95_seconds': .5, 'process_rss_bytes': 512 * 1024 * 1024,
    'process_fds': 160, 'frontend_fd_growth': 16, 'sftp_payload_bytes': 64 * 1024,
}


def summary(values):
    if not values:
        return {'count': 0}
    ordered = sorted(values)
    return {'count': len(ordered), 'median': statistics.median(ordered),
            'p95': ordered[min(len(ordered)-1, int(len(ordered)*.95))], 'max': max(ordered)}


def resource_sample(process):
    try:
        cpu = process.cpu_times()
        return {'pid': process.pid, 'birth': process.create_time(),
                'rss_bytes': process.memory_info().rss, 'fds': process.num_fds(),
                'threads': process.num_threads(), 'cpu_seconds': cpu.user + cpu.system}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def owned_sample(roots):
    """Only explicit fixture roots and descendants; no global process inventory."""
    records = {}
    for process in roots:
        try:
            children = process.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            children = []
        for child in [process, *children]:
            value = resource_sample(child)
            if value is not None:
                records[(value['pid'], value['birth'])] = value
    return list(records.values())


class MeterTransport(ProcessTransport):
    """Count encoded protocol frames, separately from encrypted SSH wire traffic."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples = []
        self.owners = []
        self.wire_sessions = []

    def _start(self):
        super()._start()
        value = resource_sample(psutil.Process(self.process.pid))
        if value and not any(row['pid'] == value['pid'] and row['birth'] == value['birth'] for row in self.owners):
            self.owners.append(value)

    def exchange(self, operation, params=None, target=None, *, request_id=None):
        request_id = request_id or uuid4().hex
        sent = len(encode(request(operation, params, target, request_id)))
        started = time.perf_counter()
        received, outcome = 0, 'ok'
        try:
            value = super().exchange(operation, params, target, request_id=request_id)
            received = len(encode({'protocol': 1, 'request_id': request_id, 'ok': True, 'result': value}))
            return value
        except Exception as exc:
            outcome = getattr(exc, 'code', type(exc).__name__)
            raise
        finally:
            self.samples.append({'operation': operation, 'target': target,
                'seconds': time.perf_counter()-started, 'request_bytes': sent,
                'response_bytes': received, 'outcome': outcome})

    def close(self):
        # OpenSSH emits its own encrypted channel byte counts at orderly EOF.
        # Drain only this owned channel; normal protocol/host-key checks stay in
        # ProcessTransport and no system-wide network counter is sampled.
        process = self.process
        if process is not None and process.poll() is None:
            process.stdin.close()
            deadline = time.monotonic()+1
            streams = [stream for stream in (process.stdout, process.stderr) if not stream.closed]
            while streams and time.monotonic() < deadline:
                ready, _, _ = select.select(streams, [], [], min(.1, max(0., deadline-time.monotonic())))
                for stream in ready:
                    try:
                        data = os.read(stream.fileno(), 65536)
                    except (BlockingIOError, OSError):
                        data = b''
                    if not data:
                        streams.remove(stream)
                    elif stream is process.stderr:
                        self._diagnostic(data)
        if process is not None:
            match = re.search(rb'Transferred: sent (\d+), received (\d+) bytes', bytes(self.stderr))
            if match:
                self.wire_sessions.append({'sent': int(match[1]), 'received': int(match[2])})
        super().close()


def fixture(root, count):
    root.mkdir(mode=0o700)
    science = root/'native-fixture'
    science.mkdir(mode=0o700)
    texts = native_texts(relaxation=count == 10000, evaluations=2 if count == 10000 else 1)
    endpoint = texts.pop('endpoint')
    texts['POSCAR'] = structure_text()
    texts['CONTCAR'] = structure_text(endpoint)
    for name, text in texts.items():
        (science/name).write_text(text)
    assert sum(p.stat().st_size for p in science.iterdir()) <= BUDGETS['source_fixture_bytes']
    state = Store(root/'state')
    sources = tuple(SourceDescriptor(role, str(science/role.upper())) for role in ('outcar', 'oszicar', 'poscar'))
    # Register native declarations once, then reuse the metadata-only history
    # fixture around that immutable first row. No source binding is rewritten.
    native = state.add(argv=['/unexecuted/run-vasp.sh', '--input', str(science),
        '--output', str(science), '--binary', 'std', '--ranks', '1', '--ncore', '1',
        '--kpar', '1', '--mpi-mode', 'synthetic', '--restart', 'none', '--timeout', '10',
        '--stop-before', '0', '--managed-foreground'], cwd=science,
        name='Synthetic native selected row', engine='VASP', hold=True, progress_sources=sources)
    populate_history(state, root, count, first=native)
    config = enroll(root/'node', {'queues': {'research': str(state.root)},
        'storages': {'scratch': str(science)},
        'profiles': {'benign': {'engine': 'Command', 'runner': '/usr/bin/true', 'max_cpus': 1}}})
    entry = {'transport': 'local', 'config_root': str(root/'node'), 'node_id': config['node_id'],
        'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
        'queues': {'research': config['queues']['research']['queue_id']},
        'default_queue': 'research', 'read_only': False}
    target = {'node_id': entry['node_id'], 'queue_id': entry['queues']['research'],
              'job_id': native['id'], 'attempt_id': native['attempt_id']}
    return {'entry': entry, 'target': target, 'state': str(state.root), 'science': str(science), 'rows': count}


def endpoint_command(config):
    driver = Path(__file__).resolve().parents[2]
    return [sys.executable, '-I', '-B', '-c',
        'import sys;sys.path.insert(0,sys.argv.pop(1));from tests.jobs.isolated_runtime import main;main()',
        str(driver), 'cli', 'nodes', 'endpoint', '--config', config, '--queue', 'research']


def connect(record, *, server=None):
    entry = dict(record['entry'])
    argv = endpoint_command(entry['config_root'])
    if server is not None:
        entry.update(transport='ssh', ssh_alias='fixture-loopback',
                     ssh_host_key=server.fingerprint, executable=sys.executable)
        argv = server.command(['/usr/bin/env', 'PYTHONDONTWRITEBYTECODE=1',
                               'TMPDIR='+str(server.root.parent/'tmp'), *argv])
        argv.insert(1, '-v')
    record['entry'] = entry
    record['command'] = argv
    transport = MeterTransport(argv, host_key=entry.get('ssh_host_key'), timeout=20)
    return ConnectedBackend(str(record['rows']), entry, transport=transport)


def catalogue_query(client, *, cursor=None):
    from cmw.nodes.codec import encode_typed, decode_typed
    from cmw.jobs.catalogue_models import CataloguePage
    query = CatalogueQuery(limit=BUDGETS['page_rows'], cursor=cursor)
    value = client.call('catalogue.query', {'query': encode_typed(query)})['result']['page']
    page = decode_typed(value, CataloguePage)
    assert len(page.records) <= BUDGETS['page_rows']
    assert len(json.dumps(asdict(page)).encode()) <= BUDGETS['catalogue_page_bytes']
    return page


def changing_log(root):
    """Actual bounded reader I/O over one long native-shaped append-only log."""
    from cmw.progress.reader import RegularFileReader
    root.mkdir(mode=0o700)
    path = root/'OUTCAR'
    original = native_texts()['OUTCAR'].encode()
    remaining = BUDGETS['changing_log_initial_bytes']-len(original)
    padding = (b' campaign padding only\n' * (remaining//22+2))[:remaining-1]+b'\n'
    path.write_bytes(original+padding)
    assert path.stat().st_size == BUDGETS['changing_log_initial_bytes']
    reader = RegularFileReader(SourceDescriptor('outcar', str(path)))
    samples = []
    try:
        while True:
            update = reader.poll()
            samples.append(asdict(update.state))
            assert update.state.bytes_read <= BUDGETS['changing_log_poll_bytes']
            if not update.state.has_more:
                break
            assert len(samples) <= 5
        assert len(samples) == 4
        idle = reader.poll()
        assert idle.state.bytes_read == 0
        parts = (b' campaign tail 1\n campaign partial', b' complete\n')
        assert sum(map(len, parts)) <= BUDGETS['changing_log_append_bytes']
        for index, part in enumerate(parts):
            with path.open('ab') as stream:
                stream.write(part)
            update = reader.poll()
            samples.append(asdict(update.state))
            assert update.state.bytes_read <= len(part)+256
            if index == 0:
                assert update.state.pending_bytes and not any('partial' in line.text for line in update.lines)
            else:
                assert update.state.pending_bytes == 0
                assert [line.text for line in update.lines] == [' campaign partial complete']
        return {'initial_bytes': len(original+padding), 'append_bytes': sum(map(len, parts)),
            'idle_bytes_read': idle.state.bytes_read, 'reader_polls': samples,
            'actual_pread_bytes': samples[-1]['total_bytes_read'], 'mutable_log_bytes_hashed': 0}
    finally:
        reader.close()


def assert_native(snapshot, evaluation):
    """Literal invented values, independent of parser-produced expectations."""
    assert snapshot.status == 'OBSERVED', snapshot.status
    assert snapshot.completeness == 'COMPLETE_EVALUATION', snapshot.completeness
    assert snapshot.scientific_validation == 'not-assessed'
    assert [(level.kind, level.index) for level in snapshot.iteration] == [('evaluation', evaluation), ('electronic', 3)]
    assert snapshot.context.binding_status == 'BOUND', snapshot.context.binding_status
    assert snapshot.read_health == 'OK', snapshot.read_health
    assert {source.role for source in snapshot.sources} == {'outcar', 'oszicar', 'incar'}
    assert all(source.health == 'OK' and not source.has_more for source in snapshot.sources)
    metrics = {metric.latest.semantic_id: metric.latest for metric in snapshot.metrics}
    assert all(metric.qualification == 'row-correspondence'
               for name, metric in metrics.items() if name.startswith('vasp.electronic.'))
    expected = {'vasp.force.maximum_atomic_norm.all': .5, 'vasp.electronic.dE': 5e-8,
        'vasp.electronic.d_eps': 4e-8, 'vasp.electronic.free_energy': -2.10000005,
        'vasp.electronic.rms': 1e-4}
    for name, value in expected.items():
        metric = metrics[name]
        assert math.isclose(metric.value, value, rel_tol=1e-12, abs_tol=1e-14), (name, metric.value)
        assert metric.evidence and metric.qualification not in {'conflict', 'unsupported', 'unavailable'}
        assert next(context.index for context in metric.context if context.kind == 'evaluation') == evaluation
    for name, signed in (('vasp.electronic.dE', -5e-8), ('vasp.electronic.d_eps', -4e-8)):
        metric = metrics[name]
        assert metric.signed_value == signed
        assert metric.criterion.value == 1e-6 and metric.relation == 'BELOW'
        assert next(context.index for context in metric.context if context.kind == 'electronic') == 3
    assert metrics['vasp.electronic.rms_c'].value is None
    return expected


def assert_review(record, *, relaxation=False):
    assert record.status == 'COMPLETE', record.message
    expected = {'operational': 'UNKNOWN', 'source_binding': 'NOT ASSESSED',
        'electronic': 'PASS', 'ionic_analysis': 'NOT ASSESSED' if relaxation else 'N/A',
        'endpoint': 'NOT ASSESSED', 'policy': 'NOT ASSESSED', 'finalization': 'NOT ASSESSED'}
    actual = {domain.name: domain.status for domain in record.domains}
    assert actual == expected, actual
    sources = json.loads(record.sources_json)
    native = [source for source in sources if Path(source.get('absolute_path') or source.get('path', '')).name in {'OUTCAR', 'OSZICAR'}]
    assert len(native) == 2 and all(source.get('sha256') and source.get('coverage') == 'complete' for source in native)
    coverage = json.loads(record.coverage)
    assert coverage == {'all_sources_complete': True, 'all_sources_stable': True, 'full_electronic_history': True}, coverage
    comparisons = json.loads(record.evidence_json)['domains']['endpoint']['comparisons']
    assert all(comparisons[name]['status'] == 'match'
               for name in ('initial_poscar', 'endpoint_contcar', 'fixed_cell'))
    assert comparisons['endpoint_poscar']['status'] == ('mismatch' if relaxation else 'match')
    assert math.isclose(comparisons['endpoint_poscar']['max_displacement_angstrom'],
                        .04 if relaxation else 0., abs_tol=1e-12)
