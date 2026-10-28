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
import socket
import subprocess
import threading
from unittest.mock import patch
from uuid import uuid4

import psutil

from cmw.jobs.catalogue_models import CatalogueQuery
from cmw.jobs.store import Store
from cmw.jobs.ownership import identity as process_identity
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


# New observation/cleanup rules, separate from the retained workload bounds.
OBSERVATION_RULES = {'sample_cadence_seconds': .1, 'required_live_coverage': 1.0,
                     'cleanup_seconds': 30}


def summary(values):
    if not values:
        return {'count': 0}
    ordered = sorted(values)
    return {'count': len(ordered), 'median': statistics.median(ordered),
            'p95': ordered[min(len(ordered)-1, int(len(ordered)*.95))], 'max': max(ordered)}


def resource_sample(process):
    """A live sample is never a fabricated zero for an exited/inaccessible PID."""
    cpu = process.cpu_times()
    return {'pid': process.pid, 'birth': process.create_time(),
            'rss_bytes': process.memory_info().rss, 'fds': process.num_fds(),
            'threads': process.num_threads(), 'cpu_seconds': cpu.user + cpu.system}


class ProcessLedger:
    """Task-local launch/reaping records and classified sampled observations.

    Direct Popen children are captured at launch. Descendants are recorded only
    when discovered from verified owned roots; this is not continuous census.
    """
    def __init__(self, owning_test):
        self.owning_test = owning_test
        self.phase = 'setup'
        self.host, self.boot = socket.gethostname(), psutil.boot_time()
        self.records, self.launch_failures, self.observations = [], [], []
        self.discovery_errors, self.cleanup_errors = [], []
        self.direct, self.terminal = {}, set()
        self.lock = threading.RLock()
        self.frontend = self._register(psutil.Process(), 'frontend', 'explicit-root')
        self._patch = None

    def _register(self, process, role, origin, direct=None):
        row = {'pid': process.pid, 'host': self.host, 'boot': self.boot, 'birth': None,
               'parent': os.getpid() if direct is not None else None,
               'role': role, 'owning_test': self.owning_test, 'phase': self.phase,
               'capture_origin': origin, 'recorded_at': time.monotonic()}
        try:
            expected_birth = process.create_time()
            observed = process_identity(process.pid)
            parent = process.ppid()
            if observed['birth'] != expected_birth:
                raise psutil.NoSuchProcess(process.pid, msg='Birth changed while assembling ownership')
        except (psutil.Error, OSError, AttributeError) as exc:
            # A partially read replacement identity never gains signal authority.
            row['identity_error'] = type(exc).__name__ + ': ' + str(exc)
        else:
            row.update(observed)
            row['parent'] = parent
        with self.lock:
            for prior in reversed(self.records):
                if prior['ledger_id'] in self.terminal or prior['pid'] != process.pid:
                    continue
                original = self.direct.get(prior['ledger_id'])
                if direct is not None:
                    # Every original Popen is a launch. PID reuse must not hide
                    # a new child while an old exit awaits the next sampler tick.
                    if original is direct:
                        return prior
                    if original is None and row['birth'] is not None and prior['birth'] == row['birth']:
                        prior.update(row)
                        self.direct[prior['ledger_id']] = direct
                        return prior
                elif (original is not None and prior['birth'] is None) or prior['birth'] == row['birth']:
                    # Unavailable launch identity is never repaired by later PID
                    # observation. Only that original Popen can close its record.
                    return prior
            row['ledger_id'] = len(self.records)
            self.records.append(row)
            if direct is not None:
                self.direct[row['ledger_id']] = direct
            return row

    def __enter__(self):
        ledger, original = self, subprocess.Popen
        class RecordedPopen(original):
            def __init__(self, *args, **kwargs):
                argv = kwargs.get('args', args[0] if args else None)
                try:
                    super().__init__(*args, **kwargs)
                except BaseException as exc:
                    ledger.launch_failures.append({'argv': argv, 'phase': ledger.phase,
                        'owning_test': ledger.owning_test, 'error': type(exc).__name__ + ': ' + str(exc)})
                    raise
                # Popen owns this child even if it exits before psutil can open it.
                try:
                    process = psutil.Process(self.pid)
                except psutil.NoSuchProcess:
                    process = type('ExitedChild', (), {'pid': self.pid})()
                ledger._register(process, Path(str(argv[0])).name if isinstance(argv, (list, tuple)) else 'subprocess',
                                 'direct-launch', direct=self)
        self._patch = patch('subprocess.Popen', RecordedPopen)
        self._patch.start()
        return self

    def __exit__(self, *_):
        self._patch.stop()

    def observe(self, row):
        result = {'ledger_id': row['ledger_id'], 'pid': row['pid'], 'birth': row['birth'],
                  'phase': self.phase, 'at': time.monotonic(), 'required_live': False}
        direct = self.direct.get(row['ledger_id'])
        if direct is not None and direct.poll() is not None:
            result.update(classification='verified_exited', returncode=direct.wait(timeout=0),
                          exit_evidence='original Popen wait/reap')
            row['exit'] = {k: result[k] for k in ('returncode', 'exit_evidence', 'at')}
            self.terminal.add(row['ledger_id'])
            return result
        if row['birth'] is None:
            result.update(classification='genuinely_unobservable', required_live=True,
                          error='Birth unavailable; no later PID adoption')
            return result
        try:
            process = psutil.Process(row['pid'])
            if process.create_time() != row['birth']:
                result.update(classification='identity_mismatch', required_live=True)
                return result
            if process.status() == psutil.STATUS_ZOMBIE:
                result['classification'] = 'zombie_reaping_pending'
                return result
            result['required_live'] = True
            sample = resource_sample(process)
            if psutil.Process(row['pid']).create_time() != row['birth']:
                result['classification'] = 'identity_mismatch'
                return result
            result.update(classification='sampled_live', sample=sample)
        except psutil.NoSuchProcess:
            try:
                current = psutil.Process(row['pid'])
                same = current.create_time() == row['birth']
            except psutil.NoSuchProcess:
                result.update(classification='verified_exited', required_live=False,
                              exit_evidence='recorded identity absent on follow-up observation')
                self.terminal.add(row['ledger_id'])
            except (psutil.Error, OSError) as exc:
                result.update(classification='genuinely_unobservable', required_live=True, error=str(exc))
            else:
                result.update(classification='transient_read_error' if same else 'identity_mismatch',
                              required_live=True, error='Process changed during resource read')
        except psutil.AccessDenied as exc:
            result.update(classification='access_denied', required_live=True, error=str(exc))
        except (psutil.Error, OSError) as exc:
            result.update(classification='transient_read_error', required_live=True,
                          error=type(exc).__name__ + ': ' + str(exc))
        return result

    def sample(self):
        with self.lock:
            roots = [row for row in self.records if row['ledger_id'] not in self.terminal
                     and row['capture_origin'] in {'explicit-root', 'direct-launch'}]
        for row in roots:
            if row['birth'] is None:
                continue
            try:
                process = psutil.Process(row['pid'])
                if process.create_time() != row['birth']:
                    continue  # The classified observation below records the mismatch.
                for child in process.children(recursive=True):
                    self._register(child, 'owned descendant', 'first-observed-descendant')
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, OSError) as exc:
                self.discovery_errors.append({'phase': self.phase, 'root': row['ledger_id'],
                    'classification': 'access_denied' if isinstance(exc, psutil.AccessDenied) else 'transient_read_error',
                    'error': type(exc).__name__ + ': ' + str(exc)})
        with self.lock:
            rows = [row for row in self.records if row['ledger_id'] not in self.terminal]
        observations = [self.observe(row) for row in rows]
        self.observations.extend(observations)
        return {'at': time.monotonic(), 'phase': self.phase, 'observations': observations,
                'processes': [item['sample'] for item in observations if item['classification'] == 'sampled_live']}

    def cleanup(self, deadline):
        """Reap direct children and stop only still-matching recorded owners."""
        self.phase = 'cleanup'
        signalled = set()
        while True:
            remaining = []
            for row in reversed(self.records):
                if row['ledger_id'] == self.frontend['ledger_id']:
                    continue
                observed = self.observe(row)
                row['cleanup_observation'] = observed
                if observed['classification'] == 'verified_exited':
                    continue
                remaining.append({**row, 'observation': observed})
                if observed['classification'] != 'sampled_live' or row['ledger_id'] in signalled:
                    continue
                try:
                    process = psutil.Process(row['pid'])
                    if process.create_time() == row['birth']:
                        process.terminate()
                        signalled.add(row['ledger_id'])
                except psutil.NoSuchProcess:
                    pass
                except (psutil.Error, OSError) as exc:
                    self.cleanup_errors.append({'ledger_id': row['ledger_id'], 'error': str(exc)})
            if not remaining or time.monotonic() >= deadline:
                break
            time.sleep(min(.05, max(0., deadline - time.monotonic())))
        return remaining

    def report(self):
        required = [row for row in self.observations if row['required_live']]
        covered = sum(row['classification'] == 'sampled_live' for row in required)
        return {'records': self.records, 'launch_failures': self.launch_failures,
                'discovery_errors': self.discovery_errors, 'cleanup_errors': self.cleanup_errors,
                'required_live_observations': len(required), 'sampled_live_observations': covered,
                'live_coverage': covered / len(required) if required else None,
                'coverage_passed': bool(required) and covered == len(required) and not self.discovery_errors,
                'scope': 'Launch records plus sampled owned descendants; not continuous short-lived descendant coverage'}


def owned_sample(roots):
    """Legacy helper: errors now propagate instead of erasing live observations."""
    records = {}
    for process in roots:
        for child in [process, *process.children(recursive=True)]:
            value = resource_sample(child)
            records[(value['pid'], value['birth'])] = value
    return list(records.values())


_frame_scope = threading.local()
_frame_hook_lock = threading.Lock()


def _install_frame_meter():
    # One transparent hook, with thread-local attribution, preserves concurrent
    # native exchanges and strict decoding. No call is serialized or bypassed.
    import cmw.nodes.transport as transport
    with _frame_hook_lock:
        if getattr(transport.decode, '_campaign_frame_meter', False):
            return
        original = transport.decode
        def measured(frame):
            current = getattr(_frame_scope, 'current', None)
            if current is not None:
                current.append(len(frame))
            return original(frame)
        measured._campaign_frame_meter = True
        transport.decode = measured


def protocol_report(samples):
    operations = sorted({sample['operation'] for sample in samples})
    return {'encoded_attempted_request_bytes': sum(s['request_bytes'] for s in samples),
            'encoded_received_response_bytes': sum(s['response_bytes'] for s in samples),
            'encoded_successful_response_bytes': sum(s['response_bytes'] for s in samples if s['outcome'] == 'ok'),
            'latency_by_operation': {op: summary([s['seconds'] for s in samples
                if s['operation'] == op and s['outcome'] == 'ok']) for op in operations},
            'all_attempt_latency_by_operation': {op: summary([s['seconds'] for s in samples
                if s['operation'] == op]) for op in operations},
            'outcomes_by_operation': {op: {outcome: sum(s['operation'] == op and s['outcome'] == outcome for s in samples)
                for outcome in sorted({s['outcome'] for s in samples if s['operation'] == op})} for op in operations}}


class MeterTransport(ProcessTransport):
    """Count encoded protocol frames, separately from encrypted SSH wire traffic."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples = []
        self.owners = []
        self.wire_sessions = []
        self.phase = 'unspecified'
        _install_frame_meter()

    def _start(self):
        super()._start()
        try:
            value = process_identity(self.process.pid)
        except psutil.Error as exc:
            value = {'pid': self.process.pid, 'birth': None, 'identity_error': type(exc).__name__}
        if not any(row['pid'] == value['pid'] and row['birth'] == value['birth'] for row in self.owners):
            self.owners.append(value)

    def exchange(self, operation, params=None, target=None, *, request_id=None):
        request_id = request_id or uuid4().hex
        sent = len(encode(request(operation, params, target, request_id)))
        started = time.perf_counter()
        frames, outcome = [], 'ok'
        cold = self.process is None
        previous = getattr(_frame_scope, 'current', None)
        _frame_scope.current = frames
        try:
            return super().exchange(operation, params, target, request_id=request_id)
        except BaseException as exc:
            outcome = getattr(exc, 'code', type(exc).__name__)
            raise
        finally:
            _frame_scope.current = previous
            self.samples.append({'operation': operation, 'target': target, 'request_id': request_id,
                'phase': self.phase, 'temperature': 'cold' if cold else 'warm',
                'seconds': time.perf_counter()-started, 'request_bytes': sent,
                'response_bytes': sum(frames), 'response_frame_bytes': frames,
                'response_scope': 'actual complete frames passed to the unchanged strict decoder',
                'outcome': outcome,
                'expected_refusal': outcome != 'ok' and bool(getattr(self, 'expected_refusal_reason', None)),
                'refusal_reason': getattr(self, 'expected_refusal_reason', None) if outcome != 'ok' else None})

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
