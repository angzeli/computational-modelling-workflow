"""Single-read fault fixtures; no real process is launched or signalled.

The worker fixture deliberately models native processes. Coverage is reported at
the caller of each wrapped read, so a stubbed callee is never credited as covered.
Standalone protocol/observer contracts have separate applicability in the manifest.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from dataclasses import dataclass
import ast
import errno
import fcntl
import functools
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from cmw.jobs import ownership, ownership_hold, runtime, suspension, suspension_protocol
from cmw.jobs import process_identity
from cmw.jobs.store import Store
from cmw.molecular.orca import suspension as orca_suspension
from tests.jobs.test_cancellation_anchor import WorkerCancellationModel


MODULES = (ownership, ownership_hold, suspension, suspension_protocol, runtime, orca_suspension)
SOURCES = {str(Path(module.__file__).resolve()): module.__name__ for module in MODULES}
FRAME_PATHS = {}
READ_NAMES = frozenset({
    'identity', 'identity_for', 'observe_owner', 'group_members', 'snapshot_owned_session',
    'lock_held', 'descendant_pids', 'ancestor_pids', 'group_exists', 'process_evidence',
    'boot_session_id', 'stat', 'lstat', 'fstat', 'getsid', 'getpgid', 'read', 'read_text',
    'read_bytes', 'open', 'exists', 'resolve', 'is_dir', 'is_file', 'poll', 'wait',
    'get', 'control', 'rows', 'snapshot', 'Process', 'pids', 'boot_time', 'gethostname',
    'create_time', 'status', 'ppid', 'exe', 'now_clock', 'clock_descriptor', 'describe',
    'read_registration', 'binding', 'ack', '_read', '_directory_identity', 'read_domain',
    'read_record', '_present', 'killpg', 'load', 'readlink', 'flock'})
# Do not turn incidental argparse/gettext/import reads into lifecycle claims.
# The lexical call must itself be a native/read-observation boundary.
READ_LINES = {}
for _source in SOURCES:
    _tree = ast.parse(Path(_source).read_text())
    READ_LINES[_source] = {line for call in ast.walk(_tree) if isinstance(call, ast.Call)
                          and ast.unparse(call.func).split('.')[-1] in READ_NAMES
                          for line in range(call.lineno, call.end_lineno + 1)}
REAL_LEGACY_IDENTITY = ownership._legacy_identity
REAL_OBSERVE_OWNER = ownership.observe_owner
REAL_OWNER_ALIVE = ownership.owner_alive
REAL_GROUP_MEMBERS = ownership.group_members
FAULTS = ('EAGAIN', 'EPERM', 'UNAVAILABLE')
OUTCOMES = ('normal', 'cancel', 'timeout')
UNOBSERVED_REASON = 'process domain never observed; receipt present; manual verification required'


class SweepBoundExceeded(BaseException):
    """Escape even a production Exception retry when a fixture cannot progress."""
    def __init__(self, message, *, kind, observed, limit):
        super().__init__(message)
        self.kind, self.observed, self.limit = kind, observed, limit


class PendingObserved(BaseException):
    """Stop only after capturing a live, safely retained pre-RELEASE worker."""


@dataclass(frozen=True)
class ReadPoint:
    module: str
    function: str
    line: int
    api: str
    occurrence: int

    @property
    def site(self):
        return self.module, self.function, self.line, self.api

    def label(self):
        return f'{self.module}:{self.function}:{self.line}:{self.api}#{self.occurrence}'


class SingleFault:
    """Select one read occurrence, then immediately restore ordinary results."""
    def __init__(self, point=None, fault=None):
        self.point, self.fault = point, fault
        self.counts = Counter()
        self.trace = []
        self.fired = 0
        self.calls = 0
        self.errors = []

    def caller(self, api):
        frame = sys._getframe(2)
        while frame:
            original = frame.f_code.co_filename
            filename = FRAME_PATHS.get(original)
            if filename is None:
                filename = FRAME_PATHS[original] = os.path.abspath(original)
            if filename in SOURCES:
                # The nearest target frame owns the read. Do not credit all
                # ancestors or functions whose implementation is mocked.
                if api == 'fcntl.flock' and frame.f_code.co_name != 'lock_held':
                    return None  # Exclusive lock acquisition is an effect.
                if frame.f_lineno in READ_LINES[filename]:
                    return SOURCES[filename], frame.f_code.co_qualname, frame.f_lineno, api
                return None
            if filename == __file__ and frame.f_code.co_name in {'open_path', 'factory', 'read'}:
                frame = frame.f_back
                continue
            if '/tests/' in filename:
                # Fixture setup/input transitions are not production reads,
                # even when the worker invoked a modeled process observation.
                return None
            frame = frame.f_back
        return None

    def wrap(self, api, original, predicate=None):
        @functools.wraps(original)
        def read(*args, **kwargs):
            self.calls += 1
            if self.calls > 50000:
                raise SweepBoundExceeded('read operation count exceeded fixture bound',
                                         kind='read_operations', observed=self.calls, limit=50000)
            site = self.caller(api)
            if site is not None and (predicate is None or predicate(args, kwargs)):
                self.counts[site] += 1
                point = ReadPoint(*site, self.counts[site])
                self.trace.append(point)
                if point == self.point and not self.fired:
                    self.fired += 1
                    if api.endswith('observe_owner'):
                        # This API returns structured uncertainty. Native errno
                        # normalization is tested below its boundary separately.
                        return SimpleNamespace(status=ownership.OwnershipStatus.UNVERIFIABLE,
                                               deciding_check='fault.' + self.fault,
                                               checks={})
                    if self.fault == 'UNAVAILABLE':
                        if api.startswith('Store.'):
                            raise sqlite3.OperationalError('single injected unavailable query')
                        raise ownership.OwnershipUnavailable(api, 'single injected unreadable observation')
                    raise OSError(getattr(errno, self.fault), 'single injected read failure')
            try:
                return original(*args, **kwargs)
            except Exception as exc:
                if site is not None and len(self.errors) < 30:
                    self.errors.append((site, type(exc).__name__, str(exc)))
                raise
        return read

    def install(self, stack, model=None):
        # Install after the process model. A wrapped model read has truthful
        # caller coverage but gives no coverage credit inside its fake callee.
        for module in MODULES:
            for name in ('identity', 'identity_for', 'observe_owner', 'group_members',
                         'snapshot_owned_session', 'lock_held', 'descendant_pids',
                         'ancestor_pids', 'group_exists', 'boot_session_id',
                         'now_clock', 'clock_descriptor'):
                if hasattr(module, name):
                    original = getattr(module, name)
                    stack.enter_context(patch.object(module, name, self.wrap(name, original)))
        for name in ('get', 'control', 'rows'):
            stack.enter_context(patch.object(Store, name,
                staticmethod(self.wrap('Store.' + name, getattr(Store, name)))))
        original_snapshot = Store.snapshot
        stack.enter_context(patch.object(Store, 'snapshot', self.wrap('Store.snapshot', original_snapshot)))
        for name in ('read_text', 'read_bytes'):
            stack.enter_context(patch.object(Path, name, self.wrap('Path.' + name, getattr(Path, name))))
        original_open = Path.open
        injector = self
        class ReadFile:
            def __init__(self, handle):
                self.handle = handle
                self.read = injector.wrap('file.read', handle.read)
            def __enter__(self):
                self.handle.__enter__()
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def __getattr__(self, name):
                return getattr(self.handle, name)
        read_open = self.wrap('Path.open', original_open)
        def open_path(path, mode='r', *args, **kwargs):
            if 'r' in mode and not any(letter in mode for letter in ('w', 'a', '+', 'x')):
                return ReadFile(read_open(path, mode, *args, **kwargs))
            return original_open(path, mode, *args, **kwargs)
        stack.enter_context(patch.object(Path, 'open', open_path))
        original_fdopen = os.fdopen
        def fdopen(fd, mode='r', *args, **kwargs):
            handle = original_fdopen(fd, mode, *args, **kwargs)
            if 'r' in mode and not any(letter in mode for letter in ('w', 'a', '+', 'x')):
                return ReadFile(handle)
            return handle
        stack.enter_context(patch.object(os, 'fdopen', fdopen))
        # Do not patch exists(): Python versions differ in whether it swallows
        # OSError. Inject its underlying stat, where the native error originates.
        for name in ('stat', 'lstat', 'fstat', 'getsid', 'getpgid', 'read', 'readlink'):
            stack.enter_context(patch.object(os, name, self.wrap('os.' + name, getattr(os, name))))
        stack.enter_context(patch.object(os, 'open', self.wrap('os.open', os.open,
            lambda args, kwargs: len(args) > 1 and args[1] & os.O_ACCMODE == os.O_RDONLY)))
        stack.enter_context(patch.object(os, 'killpg', self.wrap('os.killpg(0)', os.killpg,
            lambda args, kwargs: len(args) > 1 and args[1] == 0)))
        stack.enter_context(patch.object(fcntl, 'flock', self.wrap('fcntl.flock', fcntl.flock,
            lambda args, kwargs: len(args) > 1 and args[1] != fcntl.LOCK_UN)))
        for name in ('process_evidence', '_parent', '_children'):
            stack.enter_context(patch.object(process_identity, name,
                self.wrap('process_identity.' + name, getattr(process_identity, name))))
        for name in ('pids', 'boot_time'):
            stack.enter_context(patch.object(ownership.psutil, name,
                self.wrap('psutil.' + name, getattr(ownership.psutil, name))))
        stack.enter_context(patch.object(ownership.socket, 'gethostname',
            self.wrap('socket.gethostname', ownership.socket.gethostname)))
        process = self.wrap('psutil.Process', ownership.psutil.Process)

        def factory(*args, **kwargs):
            value = process(*args, **kwargs)
            for name in ('status', 'create_time', 'ppid', 'exe'):
                if hasattr(value, name):
                    setattr(value, name, self.wrap('Process.' + name, getattr(value, name)))
            return value
        stack.enter_context(patch.object(ownership.psutil, 'Process', factory))
        if model is not None:
            for name in ('poll', 'wait'):
                stack.enter_context(patch.object(model.leader, name,
                    self.wrap('child.' + name, getattr(model.leader, name))))


class LifecycleModel(WorkerCancellationModel):
    """Real worker/queue with a bounded, owned process and receipt model."""
    def __init__(self, root, outcome, route='group', identity_version=2):
        super().__init__(root, 'single-fault-sweep')
        self.outcome, self.route = outcome, route
        self.identity_version = identity_version
        self.descendant = 202
        if route in {'controlled', 'orca'}:
            self.owners[self.descendant] = dict(pid=self.descendant, birth=float(self.descendant),
                                                host='fixture', boot=1)
            self.alive.add(self.descendant)
        if identity_version == 2:
            self.owners = {pid: {'version': 2, 'pid': pid, 'host': 'fixture',
                'boot_session': '00000000-0000-0000-0000-000000000001', 'start_token': pid * 1000}
                for pid in self.owners}
        self.terminal_events = []
        self.terminal_saves = []
        self.saved_states = []
        self.active_ticks = 0
        self.runner_control = None
        self.finish_normal = False
        self.pending_capture = None
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            if route not in {'controlled', 'orca'}:
                job['suspension'] = None
                job['env'].pop('CMW_JOBS_OWN_SESSION', None)
                if route == 'session':
                    job['env']['CMW_JOBS_OWN_SESSION'] = '1'
            elif route == 'orca':
                job['suspension']['backend'] = 'orca-local-v1'
            self.store.save(con, job)

    def after_go(self):
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            if self.outcome == 'cancel':
                job.update(status='Cancelling', cancel_requested=True)
            elif self.outcome == 'timeout':
                job.update(status='Cancelling', termination_requested={'reason': 'fixture deadline'})
            self.store.save(con, job)
        if self.outcome == 'normal' and self.route not in {'controlled', 'orca'}:
            self.alive.difference_update((self.runner, self.compute))
            self.write_receipt()
        elif self.outcome == 'normal':
            self.finish_normal = True

    def make_coordinator(self, store, job, anchor):
        coordinator = self.coordinator_type(store, job, anchor)
        backend = 'orca-local-v1' if self.route == 'orca' else 'local-command-v1'
        control = suspension_protocol.RunnerControl(
            coordinator.context_path, backend=backend,
            deadline={'known': True, 'hard_timeout_seconds': None},
            storage=[str(self.root)], runtime={'local_only': True})
        self.runner_control = control
        control.register(self.compute, strategy='orca-tree' if self.route == 'orca' else 'direct-tree')
        acknowledge = coordinator.ack
        def ack(*args):
            control.poll()
            return acknowledge(*args)
        coordinator.ack = ack
        self.coordinator = coordinator
        self.events.append('channel-ready')
        return coordinator

    def parent(self, pid):
        if pid not in self.alive:
            raise ProcessLookupError(errno.ESRCH, 'fixture original is gone')
        return {self.compute: self.runner, self.runner: self.anchor,
                self.descendant: self.compute, self.writer: self.anchor}.get(pid, 0)

    def children(self, pid):
        self.parent(pid)
        return tuple(other for other in sorted(self.alive) if self.parent(other) == pid)

    def process(self, pid):
        process = super().process(pid)
        process.pid = pid
        def birth():
            self.checked.append(pid)
            return self.owners[pid].get('birth', float(pid))
        process.create_time = birth
        process.ppid = lambda: self.parent(pid)
        return process

    def observe(self, owner):
        if self.just_launched:
            self.just_launched = False
            self.after_go()
        return REAL_OBSERVE_OWNER(owner)

    def owner_alive(self, owner):
        return REAL_OWNER_ALIVE(owner)

    def process_evidence(self, pid):
        self.checked.append(pid)
        if pid not in self.alive:
            raise ProcessLookupError(errno.ESRCH, 'fixture original is gone')
        return self.owners[pid].get('start_token', pid * 1000), False

    def sid(self, pid):
        # RunnerControl is embedded in this process model, where PID zero means
        # the modeled runner rather than the Python test process.
        return self.anchor if pid == 0 else super().sid(pid)

    def pgid(self, pid):
        if pid == self.descendant:
            self.sid(pid)
            return self.compute
        if self.route == 'group':
            self.sid(pid)
            return self.sentinel if pid == self.sentinel else self.anchor
        return super().pgid(pid)

    def write_receipt(self):
        code = 0 if self.outcome == 'normal' else 143
        if self.route == 'orca':
            control = self.runner_control
            context = control.context
            record = self.root/'controlled-completion.json'
            # Fixture producer input is established before exercising the real
            # read-only shell verifier. Its actual exit-75 retry contract drives
            # unreadable handoffs; no verification mutation is replayed.
            record.write_text(json.dumps({**context, 'runner': self.owners[self.runner],
                'root': self.owners[self.compute], 'exit_code': code,
                'source': 'cmw-controlled-driver-drained-v1',
                'disposition': 'completed' if self.outcome == 'normal' else 'cancelled'}))
            record.chmod(0o600)
            with patch.dict(os.environ, {'TMPDIR': str(self.root),
                    'CMW_SUSPENSION_CONTEXT': str(control.path)}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                for _ in range(8):
                    result = orca_suspension.main(['verify-completion', '--completion-record', str(record),
                        '--process-status', str(code), '--runner-pid', str(self.runner)])
                    if result != 75:
                        break
                    self.clock += .25
                if result != 0:
                    raise AssertionError(f'ORCA handoff did not recover: exit {result}')
        self.receipt.write_text(json.dumps({'exit_code': code, 'recorded_at': 1}))
        self.events.append('receipt')

    def send_group(self, pgid, signum):
        if self.route == 'session' and pgid == self.anchor and signum == signal.SIGTERM:
            # PR21's post-payload anchor/receipt writer ignores TERM. The parent
            # fixture also models the historical bug; this sweep models current
            # payload.sh rather than intentionally killing that protected pair.
            self.signals.append((pgid, signum))
            self.events.append(('signal', pgid, signum))
            return
        if self.route == 'group' and pgid == self.anchor and signum == signal.SIGTERM:
            self.signals.append((pgid, signum))
            self.events.append(('signal', pgid, signum))
            self.alive.difference_update((self.compute, self.runner))
            self.alive.add(self.writer)
            self.events.append('receipt-writer-started')
            return
        result = super().send_group(pgid, signum)
        if pgid == self.compute and signum == signal.SIGTERM and self.compute not in self.alive:
            self.alive.discard(self.descendant)
        return result

    def send_pid(self, pid, signum):
        result = super().send_pid(pid, signum)
        if pid == self.compute and signum == signal.SIGTERM and self.compute not in self.alive:
            self.alive.discard(self.descendant)
        return result

    def sleep(self, interval):
        # All modeled waits advance virtual time, so failures cannot create
        # slow wall-clock tests or leave a real worker behind.
        unobserved = (self.coordinator is not None and self.coordinator.last_domain is None
                      and self.receipt.is_file())
        if unobserved and self.clock >= 65:
            with self.store.transaction() as con:
                job = self.store.get(con, self.job['id'])
                messages = [row[0] for row in con.execute('SELECT message FROM events')]
            # Capture the live worker before the test-only stop unwinds its
            # finally blocks. No actual process or control pipe is involved.
            self.pending_capture = dict(job=job, messages=messages,
                pipe_closed=self.leader.stdin.close.called, anchor_alive=self.anchor in self.alive,
                prior_domain=self.coordinator.last_domain)
            raise PendingObserved()
        if self.clock + interval > (75 if unobserved else 30):
            raise SweepBoundExceeded('single transient fault exceeded virtual-time budget',
                                     kind='virtual', observed=self.clock + interval,
                                     limit=75 if unobserved else 30)
        if self.finish_normal:
            self.finish_normal = False
            self.alive.difference_update((self.runner, self.compute, self.descendant))
            self.write_receipt()
        self.clock += interval
        if self.writer in self.alive:
            self.alive.remove(self.writer)
            self.write_receipt()


def run_lifecycle(outcome, route='group', point=None, fault=None, *, identity_version=2, diagnostics=None):
    if route == 'legacy_session':
        route, identity_version = 'session', 1
    injector = SingleFault(point, fault)
    with tempfile.TemporaryDirectory(prefix='cmw-single-fault-') as directory:
        model = LifecycleModel(Path(directory), outcome, route, identity_version)
        if diagnostics is not None:
            diagnostics(model, injector)
        original_worker = runtime.worker
        save, event = model.store.save, model.store.event

        def record_save(con, job):
            model.saved_states.append(job['status'])
            if job['status'] in {'Done', 'Fail', 'Cancelled'}:
                model.terminal_saves.append((job['status'], job.get('exit_code')))
            return save(con, job)

        def record_event(con, job_id, message):
            if any(message.startswith(name + ':') for name in ('Done', 'Fail', 'Cancelled')):
                model.terminal_events.append(message)
            return event(con, job_id, message)

        def worker(*args):
            with ExitStack() as stack:
                # Exercise the actual v1 normalization and session readers;
                # only their native process source is supplied by the model.
                stack.enter_context(patch.object(ownership, '_legacy_identity', REAL_LEGACY_IDENTITY))
                stack.enter_context(patch.object(ownership.socket, 'gethostname', return_value='fixture'))
                stack.enter_context(patch.object(ownership.psutil, 'boot_time', return_value=1.0))
                stack.enter_context(patch.object(ownership, 'boot_session_id',
                    return_value='00000000-0000-0000-0000-000000000001'))
                if identity_version == 2:
                    stack.enter_context(patch.object(process_identity, 'process_evidence',
                        side_effect=model.process_evidence))
                stack.enter_context(patch.object(suspension_protocol, 'identity',
                    side_effect=lambda pid=None: model.identify(model.runner if pid is None else pid)))
                stack.enter_context(patch.object(process_identity, '_parent', side_effect=model.parent))
                stack.enter_context(patch.object(process_identity, '_children', side_effect=model.children))
                def members(pgid, *, pids=None, session=False, **kwargs):
                    return REAL_GROUP_MEMBERS(pgid, pids=pids, session=session,
                                              process_factory=ownership.psutil.Process)
                for module in (ownership, runtime, suspension):
                    stack.enter_context(patch.object(module, 'group_members', members))
                injector.install(stack, model)
                stack.enter_context(patch.object(model.store, 'snapshot',
                    Store.snapshot.__get__(model.store, Store)))
                return original_worker(*args)

        try:
            with patch.object(runtime, 'worker', worker), \
                    patch.object(model.store, 'save', record_save), \
                    patch.object(model.store, 'event', record_event):
                try:
                    job = model.run()
                except PendingObserved:
                    if model.pending_capture is None:
                        raise
                    job = model.pending_capture['job']
        finally:
            if model.runner_control is not None:
                model.runner_control.close()
        with model.store.transaction(create=False) as con:
            messages = [row[0] for row in con.execute('SELECT message FROM events')]
        missing_timestamp_elapsed = None
        if model.pending_capture is None and job.get('finished_at') is None:
            current = model.store.snapshot()['jobs'][0]['elapsed']
            with patch.object(time, 'time', return_value=time.time() + 3600):
                later = model.store.snapshot()['jobs'][0]['elapsed']
            missing_timestamp_elapsed = current, later
        return dict(job=job, points=injector.trace, fired=injector.fired,
                    read_errors=injector.errors,
                    signals=model.signals, events=model.events,
                    terminal_saves=model.terminal_saves,
                    terminal_events=model.terminal_events,
                    saved_states=model.saved_states, pending=model.pending_capture,
                    messages=messages, missing_timestamp_elapsed=missing_timestamp_elapsed,
                    receipt=model.receipt.exists(), released=model.released,
                    clock=model.clock, pipe_closed=model.leader.stdin.close.called)


def selected_points(points, representative):
    """CI covers every reached occurrence; local selection spans each read API."""
    if not representative:
        return points
    chosen = {}
    for point in points:
        chosen.setdefault(point.api, point)
    return list(chosen.values())


def assert_terminal(test, result, outcome, baseline=None):
    if result['pending'] is not None:
        test.assertIsNotNone(baseline, 'fault-free baselines must complete')
        test.assertEqual(result['fired'], 1, 'fault target was not reached exactly once')
        test.assertEqual(outcome, 'normal', 'cancellation/timeout must already have a verified domain')
        test.assertEqual(result['job']['status'], 'Run')
        test.assertNotIn('Unknown', result['saved_states'])
        test.assertIsNone(result['job']['finished_at'])
        test.assertIsNone(result['job']['exit_code'])
        test.assertIn(UNOBSERVED_REASON, result['job']['reason'])
        test.assertTrue(result['job'].get('ownership_holds'))
        test.assertTrue(result['receipt'])
        test.assertTrue(result['pending']['anchor_alive'])
        test.assertIsNone(result['pending']['prior_domain'])
        test.assertFalse(result['pending']['pipe_closed'])
        test.assertFalse(result['released'])
        test.assertEqual(result['signals'], [])
        test.assertEqual(result['events'].count('GO'), 1)
        test.assertEqual(result['events'].count('RELEASE'), 0)
        test.assertEqual(result['terminal_saves'], [])
        test.assertEqual(result['terminal_events'], [])
        test.assertEqual(sum('attention required' in row for row in result['pending']['messages']), 1)
        test.assertLessEqual(result['clock'], 75)
        return 'pending_never_observed_domain'
    expected = {'normal': ('Done', 0), 'cancel': ('Cancelled', 143), 'timeout': ('Fail', 124)}[outcome]
    test.assertEqual((result['job']['status'], result['job']['exit_code']), expected)
    test.assertNotIn('Unknown', result['saved_states'])
    if result['job']['finished_at'] is None:
        test.assertTrue(any('completion timestamp unavailable at the established boundary' in message
                            for message in result['messages']))
        test.assertEqual(result['missing_timestamp_elapsed'], (None, None))
    if any('completion clock unavailable at the established boundary' in message
           for message in result['messages']):
        timing = result['job']['suspension']
        test.assertTrue(timing['accounting_uncertain'])
        test.assertIsNone(timing['unsuspended_elapsed'])
        test.assertNotIn('clock_finished', timing)
    test.assertFalse(result['job'].get('ownership_holds'))
    test.assertTrue(result['receipt'])
    test.assertTrue(result['released'])
    test.assertTrue(result['pipe_closed'])
    test.assertEqual(result['events'].count('GO'), 1)
    test.assertEqual(result['events'].count('RELEASE'), 1)
    test.assertEqual(len(result['terminal_saves']), 1)
    test.assertEqual(len(result['terminal_events']), 1)
    test.assertLessEqual(result['clock'], 30)
    if baseline is not None:
        test.assertEqual(result['fired'], 1, 'fault target was not reached exactly once')
        test.assertEqual(result['signals'], baseline['signals'])
    return 'terminal'


def summarize(points):
    return [dict(module=p.module, function=p.function, line=p.line, api=p.api,
                 occurrence=p.occurrence) for p in points]
