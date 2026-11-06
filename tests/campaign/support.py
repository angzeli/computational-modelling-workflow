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
import traceback
from unittest.mock import patch
from uuid import uuid4

import psutil

from cmw.nodes.protocol import encode, request
from cmw.nodes.transport import ProcessTransport
from tests.campaign.qualification_v2 import PLAN, FIELDS, coverage, field_sample, role_for


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
OBSERVATION_RULES = {key: PLAN[key] for key in ('sample_cadence_seconds', 'cleanup_seconds')}
OBSERVATION_RULES['contract_version'] = 2


def summary(values):
    if not values:
        return {'count': 0}
    ordered = sorted(values)
    return {'count': len(ordered), 'median': statistics.median(ordered),
            'p95': ordered[min(len(ordered)-1, int(len(ordered)*.95))], 'max': max(ordered)}


def resource_sample(process):
    """Legacy endpoint samples preserve partial values and their field evidence."""
    values, fields = field_sample(process, process.create_time())
    return {**values, 'field_observations': fields}


def process_identity(pid):
    """Match the independent psutil birth brackets in campaign qualification evidence."""
    process = psutil.Process(pid)
    return {'pid': process.pid, 'birth': process.create_time(), 'host': socket.gethostname(),
            'boot': psutil.boot_time()}


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
        self.opportunities, self.sampling_errors = [], []
        self.next_due = time.monotonic()
        self.last_sample_end = self.next_due
        self.lock = threading.RLock()
        root_role = role_for(owning_test.split(':'))
        self.frontend = self._register(psutil.Process(), 'frontend' if root_role == 'helper' else root_role, 'explicit-root')
        self._patch = None

    def _register(self, process, role, origin, direct=None):
        row = {'pid': process.pid, 'host': self.host, 'boot': self.boot, 'birth': None,
               'parent': os.getpid() if direct is not None else None,
               'role': role, 'owning_test': self.owning_test, 'phase': self.phase,
               'capture_origin': origin, 'recorded_at': time.monotonic()}
        identity_mismatch = False
        try:
            expected_birth = process.create_time()
            observed = process_identity(process.pid)
            if observed['birth'] != expected_birth:
                identity_mismatch = True
                raise psutil.NoSuchProcess(process.pid, msg='Birth changed while assembling ownership')
            parent = process.ppid()
        except (psutil.Error, OSError, AttributeError) as exc:
            # A partially read replacement identity never gains signal authority.
            row['identity_error'] = type(exc).__name__ + ': ' + str(exc)
            row['registration_disappearance'] = isinstance(exc, psutil.NoSuchProcess) and not identity_mismatch
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
                record = ledger._register(process, role_for(argv),
                                          'direct-launch', direct=self)
                with ledger.lock:
                    record['argv'] = ([os.fsdecode(part) for part in argv]
                                      if isinstance(argv, (list, tuple)) else os.fsdecode(argv))
        self._patch = patch('subprocess.Popen', RecordedPopen)
        self._patch.start()
        return self

    def __exit__(self, *_):
        self._patch.stop()

    def observe(self, row):
        observed = self._observe(row)
        if observed['classification'] == 'verified_exited':
            row.setdefault('exit', {key: observed[key] for key in ('at', 'exit_evidence', 'returncode') if key in observed})
        classification = {'genuinely_unobservable': 'unavailable_identity',
            'verified_exited': 'verified_exit', 'zombie_reaping_pending': 'zombie_pending_reap',
            'transient_read_error': 'transient_read_uncertainty'}.get(observed['classification'], observed['classification'])
        observed.setdefault('fields', {field: {'field': field, 'method': 'psutil.Process.' + method,
            'classification': classification, 'started_at': observed['at'], 'ended_at': time.monotonic(),
            'value': None, 'reason': observed.get('error', observed.get('exit_evidence'))}
            for field, method in FIELDS.items()})
        return observed

    def _observe(self, row):
        result = {'ledger_id': row['ledger_id'], 'pid': row['pid'], 'birth': row['birth'],
                  'phase': self.phase, 'role': row['role'], 'owning_test': self.owning_test,
                  'at': time.monotonic(), 'required_live': False}
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
            if direct is None and row.get('registration_disappearance'):
                try:
                    # Absence can close a vanished enumerated descendant; a
                    # present PID never supplies replacement birth authority.
                    psutil.Process(row['pid']).create_time()
                except psutil.NoSuchProcess:
                    result.update(classification='verified_exited', required_live=False,
                                  exit_evidence='registration disappearance and follow-up PID absence')
                    result.pop('error')
                    self.terminal.add(row['ledger_id'])
                except (psutil.Error, OSError) as exc:
                    result.update(classification='access_denied' if isinstance(exc, psutil.AccessDenied)
                                  else 'transient_read_error', error=type(exc).__name__ + ': ' + str(exc))
            return result
        try:
            process = psutil.Process(row['pid'])
            if process.create_time() != row['birth']:
                result.update(classification='identity_mismatch', required_live=True)
                return result
            result['pre_read_status'] = process.status()
            if result['pre_read_status'] == psutil.STATUS_ZOMBIE:
                result['classification'] = 'zombie_reaping_pending'
                return result
            result['required_live'] = True
            sample, fields = field_sample(process, row['birth'])
            result['fields'] = fields
            if psutil.Process(row['pid']).create_time() != row['birth']:
                result['classification'] = 'identity_mismatch'
                return result
            result.update(classification='sampled_live' if all(item['classification'] == 'sampled_live'
                for item in fields.values()) else 'partially_sampled_live', sample=sample)
            if any(item['classification'] == 'identity_mismatch' for item in fields.values()):
                result.update(classification='identity_mismatch')
                result.pop('sample')
        except psutil.NoSuchProcess:
            try:
                current = psutil.Process(row['pid'])
                same = current.create_time() == row['birth']
                result['post_error_status'] = current.status() if same else None
                if same and result['post_error_status'] == psutil.STATUS_ZOMBIE:
                    same = psutil.Process(row['pid']).create_time() == row['birth']
            except psutil.NoSuchProcess:
                result.update(classification='verified_exited', required_live=False,
                              exit_evidence='recorded identity absent on follow-up observation')
                self.terminal.add(row['ledger_id'])
            except (psutil.Error, OSError) as exc:
                result.update(classification='genuinely_unobservable', required_live=True, error=str(exc))
            else:
                if same and result['post_error_status'] == psutil.STATUS_ZOMBIE:
                    result.update(classification='zombie_reaping_pending', required_live=False,
                                  exit_evidence='same recorded birth observed zombie after resource read')
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
        started = time.monotonic()
        cadence = OBSERVATION_RULES['sample_cadence_seconds']
        while self.next_due + cadence <= started:
            self._missed_opportunity(self.next_due, 'skipped_lag')
            self.next_due += cadence
        scheduled = self.next_due if self.next_due <= started else None
        if scheduled is not None:
            self.next_due += cadence
        with self.lock:
            roots = [row for row in self.records if row['ledger_id'] not in self.terminal
                     and row['capture_origin'] in {'explicit-root', 'direct-launch'}]
        discovered_roots = set()
        for row in roots:
            # One recursive traversal already covers direct children it found.
            # Retain separate traversal for a known root absent from that tree
            # (reparenting, disappearance or an incomplete parent observation).
            if (row['pid'], row['birth']) in discovered_roots:
                continue
            if row['birth'] is None:
                continue
            try:
                process = psutil.Process(row['pid'])
                if process.create_time() != row['birth']:
                    continue  # The classified observation below records the mismatch.
                for child in process.children(recursive=True):
                    registered = self._register(child, 'owned descendant', 'first-observed-descendant')
                    if registered['birth'] is not None:
                        discovered_roots.add((registered['pid'], registered['birth']))
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, OSError) as exc:
                self.discovery_errors.append({'phase': self.phase, 'root': row['ledger_id'],
                    'classification': 'access_denied' if isinstance(exc, psutil.AccessDenied) else 'transient_read_error',
                    'error': type(exc).__name__ + ': ' + str(exc)})
        with self.lock:
            rows = [row for row in self.records if row['ledger_id'] not in self.terminal]
        try:
            observations = [self.observe(row) for row in rows]
        except BaseException as exc:
            self.sampling_errors.append({'at': time.monotonic(), 'exception': type(exc).__name__, 'message': str(exc)})
            if scheduled is not None:
                self._missed_opportunity(scheduled, 'sampler_crash')
            raise
        self.observations.extend(observations)
        self.last_sample_end = time.monotonic()
        if scheduled is not None:
            self.opportunities.append({'scheduled_at': scheduled, 'started_at': started,
                'ended_at': self.last_sample_end, 'classification': 'sampled', 'phase': self.phase})
        return {'at': self.last_sample_end, 'observer_seconds': self.last_sample_end-started, 'phase': self.phase, 'observations': observations,
                'processes': [item['sample'] for item in observations if 'sample' in item]}

    def _missed_opportunity(self, scheduled, classification):
        self.opportunities.append({'scheduled_at': scheduled, 'classification': classification, 'phase': self.phase})
        for row in self.records:
            if row['ledger_id'] in self.terminal or row['recorded_at'] > scheduled:
                continue
            self.observations.append({'ledger_id': row['ledger_id'], 'pid': row['pid'], 'birth': row['birth'],
                'role': row['role'], 'owning_test': self.owning_test, 'phase': self.phase, 'at': scheduled,
                'classification': classification, 'required_live': True,
                'fields': {field: {'field': field, 'method': method, 'classification': classification,
                    'started_at': None, 'ended_at': None, 'value': None} for field, method in FIELDS.items()}})

    def finish_sampling(self, ended_at=None):
        """Close the declared observation window, accounting for lag after a crash."""
        ended_at = time.monotonic() if ended_at is None else ended_at
        while self.next_due <= ended_at:
            self._missed_opportunity(self.next_due, 'skipped_lag')
            self.next_due += OBSERVATION_RULES['sample_cadence_seconds']
        self.last_sample_end = ended_at

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
                if observed['classification'] not in {'sampled_live', 'partially_sampled_live'} or row['ledger_id'] in signalled:
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
        classified = coverage(self.records, self.observations, self.opportunities,
                              ended_at=self.last_sample_end, sampling_errors=self.sampling_errors)
        if self.discovery_errors:
            classified['resource_observability'] = 'RESOURCE_QUALIFICATION_INCOMPLETE'
        return {**classified, 'records': self.records, 'launch_failures': self.launch_failures,
                'discovery_errors': self.discovery_errors, 'cleanup_errors': self.cleanup_errors,
                'required_live_observations': len(required), 'sampled_live_observations': covered,
                'live_coverage': covered / len(required) if required else None,
                'coverage_passed': classified['resource_observability'] == 'PASS',
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
    """Count encoded frames and retain request/close timing without changing outcomes."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples = []
        self.owners = []
        self.wire_sessions = []
        self.phase = 'unspecified'
        self._meter_active = {}
        self._meter_owner = None
        _install_frame_meter()

    def _meter_event(self, owner, event, **values):
        if owner is None:
            return
        events = owner.setdefault('transport_events', [])
        if len(events) >= 32:
            owner['transport_events_truncated'] = owner.get('transport_events_truncated', 0)+1
            return
        events.append({'event': event, 'at': time.monotonic(), 'wall_ns': time.time_ns(),
            'thread_ident': threading.get_ident(),
            'active_requests': [dict(item) for item in tuple(self._meter_active.values())], **values})

    def _start(self):
        previous_process = self.process
        super()._start()
        try:
            value = process_identity(self.process.pid)
        except (psutil.Error, OSError) as exc:
            value = {'pid': self.process.pid, 'birth': None, 'identity_error': type(exc).__name__}
        owner = next((row for row in self.owners
                      if row['pid'] == value['pid'] and row['birth'] == value['birth']), None)
        if owner is None:
            owner = value
            self.owners.append(owner)
        self._meter_owner = owner
        if previous_process is None:
            self._meter_event(owner, 'launch', pid=self.process.pid)

    @staticmethod
    def _meter_exception(exc):
        def detail(value):
            message = str(value)
            return {'type': type(value).__name__, 'code': getattr(value, 'code', None),
                'message': message[:2048], 'message_truncated': len(message) > 2048,
                'errno': getattr(value, 'errno', None)}
        return {**detail(exc), 'cause': detail(exc.__cause__) if exc.__cause__ is not None else None}

    def exchange(self, operation, params=None, target=None, *, request_id=None):
        request_id = request_id or uuid4().hex
        sent = len(encode(request(operation, params, target, request_id)))
        started = time.monotonic()
        started_wall_ns = time.time_ns()
        frames, outcome, error = [], 'ok', None
        cold = self.process is None
        previous = getattr(_frame_scope, 'current', None)
        _frame_scope.current = frames
        token = object()
        self._meter_active[id(token)] = {'operation': operation, 'request_id': request_id,
            'started_at': started, 'thread_ident': threading.get_ident()}
        try:
            return super().exchange(operation, params, target, request_id=request_id)
        except BaseException as exc:
            outcome = getattr(exc, 'code', type(exc).__name__)
            error = self._meter_exception(exc)
            raise
        finally:
            ended = time.monotonic()
            self._meter_active.pop(id(token), None)
            _frame_scope.current = previous
            self.samples.append({'operation': operation, 'target': target, 'request_id': request_id,
                'phase': self.phase, 'temperature': 'cold' if cold else 'warm',
                'started_at': started, 'ended_at': ended, 'started_wall_ns': started_wall_ns,
                'ended_wall_ns': time.time_ns(), 'thread_ident': threading.get_ident(),
                'seconds': ended-started, 'request_bytes': sent, 'error': error,
                'response_bytes': sum(frames), 'response_frame_bytes': frames,
                'response_scope': 'actual complete frames passed to the unchanged strict decoder',
                'outcome': outcome,
                'expected_refusal': outcome != 'ok' and bool(getattr(self, 'expected_refusal_reason', None)),
                'refusal_reason': getattr(self, 'expected_refusal_reason', None) if outcome != 'ok' else None})

    def close(self):
        # Preserve the existing channel drain. Its timing and consumed bytes are
        # evidence when shutdown overlaps an unchanged in-flight exchange.
        process, owner = self.process, self._meter_owner
        drained = {'stdout': 0, 'stderr': 0}
        error = None
        if process is not None:
            self._meter_event(owner, 'close_start', pid=process.pid,
                cached_returncode=process.returncode,
                caller=[{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
                        for frame in traceback.StackSummary.extract(
                            traceback.walk_stack(None), limit=6, lookup_lines=False)])
        try:
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
                        drained['stderr' if stream is process.stderr else 'stdout'] += len(data)
                        if not data:
                            streams.remove(stream)
                        elif stream is process.stderr:
                            self._diagnostic(data)
            if process is not None:
                match = re.search(rb'Transferred: sent (\d+), received (\d+) bytes', bytes(self.stderr))
                if match:
                    self.wire_sessions.append({'sent': int(match[1]), 'received': int(match[2])})
            super().close()
        except BaseException as exc:
            error = self._meter_exception(exc)
            raise
        finally:
            if process is not None:
                self._meter_event(owner, 'close_end', pid=process.pid,
                    returncode=process.returncode, drained_bytes=drained, error=error,
                    stderr_tail=bytes(self.stderr[-4096:]).decode('utf-8', errors='replace'))


def fixture(root, count, *, history_rows=None):
    """Build the existing native case with an explicit small readiness history.

    ``count`` retains the established 1000/10000 native-case/owner key. A reduced
    history is disclosed independently and never qualifies large-history work.
    """
    actual_rows = count if history_rows is None else history_rows
    if history_rows is not None and (isinstance(actual_rows, bool)
            or not isinstance(actual_rows, int) or not 2 <= actual_rows <= count):
        raise ValueError('history_rows must be an integer between 2 and the case count')
    from cmw.jobs.store import Store
    from cmw.nodes.identity import enroll
    from cmw.progress.models import SourceDescriptor
    from tests.jobs.test_catalogue_store import populate_history
    from tests.periodic.vasp.result_fixtures import native_texts, structure_text
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
    populate_history(state, root, actual_rows, first=native)
    config = enroll(root/'node', {'queues': {'research': str(state.root)},
        'storages': {'scratch': str(science)},
        'profiles': {'benign': {'engine': 'Command', 'runner': '/usr/bin/true', 'max_cpus': 1}}})
    entry = {'transport': 'local', 'config_root': str(root/'node'), 'node_id': config['node_id'],
        'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
        'queues': {'research': config['queues']['research']['queue_id']},
        'default_queue': 'research', 'read_only': False}
    target = {'node_id': entry['node_id'], 'queue_id': entry['queues']['research'],
              'job_id': native['id'], 'attempt_id': native['attempt_id']}
    return {'entry': entry, 'target': target, 'state': str(state.root), 'science': str(science), 'rows': count, 'actual_history_rows': actual_rows}


def endpoint_command(config):
    driver = Path(__file__).resolve().parents[2]
    return [sys.executable, '-I', '-B', '-c',
        'import sys;sys.path.insert(0,sys.argv.pop(1));from tests.jobs.isolated_runtime import main;main()',
        str(driver), 'cli', 'nodes', 'endpoint', '--config', config, '--queue', 'research']


def connect(record, *, server=None):
    from cmw.nodes.connections import ConnectedBackend
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
    from cmw.jobs.catalogue_models import CataloguePage, CatalogueQuery
    query = CatalogueQuery(limit=BUDGETS['page_rows'], cursor=cursor)
    value = client.call('catalogue.query', {'query': encode_typed(query)})['result']['page']
    page = decode_typed(value, CataloguePage)
    assert len(page.records) <= BUDGETS['page_rows']
    assert len(json.dumps(asdict(page)).encode()) <= BUDGETS['catalogue_page_bytes']
    return page


def changing_log(root):
    """Actual bounded reader I/O over one long native-shaped append-only log."""
    from cmw.progress.reader import RegularFileReader
    from cmw.progress.models import SourceDescriptor
    from tests.periodic.vasp.result_fixtures import native_texts
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
    sources = {source.role: source for source in snapshot.sources}
    assert set(sources) == {'outcar', 'oszicar', 'incar', 'stdout'}
    assert all(sources[role].health == 'OK' and not sources[role].has_more
               for role in ('outcar', 'oszicar', 'incar'))
    stdout = sources['stdout']
    assert stdout.health == 'MISSING' and not stdout.has_more and stdout.offset == 0
    declaration = next(source for source in snapshot.context.sources if source.role == 'stdout')
    assert declaration.optional and declaration.path == stdout.path
    assert stdout.path == str(Path(sources['outcar'].path).with_name('stdout.log'))
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
