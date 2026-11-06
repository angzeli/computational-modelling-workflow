"""Deterministic worker/receipt races; every signalled identity is task-owned."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs import ownership, runtime, suspension
from cmw.jobs.store import JobsError, Store


class WorkerCancellationModel:
    """A bounded process model around the real worker and private Store."""
    def __init__(self, root, mode):
        self.root, self.mode, self.clock = root, mode, 0.0
        self.store = Store(root/'state')
        self.job = self.store.add(argv=['/bin/true'], cwd=root, name='controlled cancellation',
                                  cpus=2, memory_gib=.25, suspension_backend='local-command-v1')
        self.claim = 'fixture-claim'
        self.events, self.signals, self.checked = [], [], []
        self.anchor, self.runner, self.compute, self.writer, self.sentinel = 101, 102, 201, 103, 999
        self.owners = {pid: dict(pid=pid, birth=float(pid), host='fixture', boot=1)
                       for pid in (self.anchor, self.runner, self.compute, self.writer, self.sentinel, 99)}
        self.alive = {self.anchor, self.runner, self.compute, self.sentinel, 99}
        self.receipt = self.store.root/'attempts'/self.job['attempt_id']/'payload-exit.json'
        self.released = False
        self.held_states = []
        self.armed = False
        self.just_launched = False
        self.release_checks = 0
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job.update(status='Starting', claim=self.claim, claimed_at=1, order=None)
            self.store.save(con, job)
            control = self.store.control(con)
            control.update(dispatch=True, stop=False)
            self.store.set_control(con, control)
        self.leader = Mock(pid=self.anchor)
        self.leader.stdin.write.side_effect = self.pipe
        self.leader.wait.side_effect = self.wait
        self.leader.poll.side_effect = lambda: None if self.anchor in self.alive else 125
        self.coordinator = None

    def make_coordinator(self, store, job, anchor):
        coordinator = self.coordinator_type(store, job, anchor)
        coordinator.registration = {'root': self.owners[self.compute], 'runner': self.owners[self.runner],
                                    'protected': [], 'strategy': 'direct-tree'}
        def domain():
            coordinator.observe_session()
            return suspension.Domain((self.owners[self.compute],),
                                     (self.owners[self.anchor], self.owners[self.runner]), ())
        coordinator._validate = domain
        coordinator.last_domain = domain()
        def acknowledge(*args):
            if self.mode == 'receipt-during-ack':
                self.alive.difference_update((self.runner, self.compute))
                self.write_receipt()
                return None
            return {'status': 'cancel_prepared'}
        coordinator.ack = Mock(side_effect=acknowledge)
        prepare_cancel = coordinator.prepare_cancel
        def prepare(job):
            result = prepare_cancel(job)
            self.prepare(job)
            return result
        coordinator.prepare_cancel = prepare
        self.coordinator = coordinator
        self.events.append('channel-ready')
        if self.mode == 'late-go':
            self.armed = True
        if self.mode == 'late-release':
            ready = coordinator.ready_to_release
            def at_release():
                drained = ready()
                if drained:
                    self.release_checks += 1
                    if self.release_checks == 2:
                        self.armed = True
                return drained
            coordinator.ready_to_release = at_release
        return coordinator

    def process(self, pid):
        model = self
        if pid not in model.alive:
            raise psutil.NoSuchProcess(pid)
        class Process:
            def status(self):
                if pid not in model.alive:
                    raise psutil.NoSuchProcess(pid)
                return psutil.STATUS_RUNNING
            def ppid(self):
                return model.runner if pid == model.compute else model.anchor
        return Process()

    def send_pid(self, pid, signum):
        assert pid in self.alive, 'Unverified numeric PID targeted'
        assert pid not in (self.anchor, self.runner, self.writer, self.sentinel), 'Protected control was targeted'
        assert pid in self.checked, 'Target birth was not checked'
        self.signals.append((pid, signum))
        self.events.append(('signal', pid, signum))
        if pid == self.compute and signum == signal.SIGTERM and self.mode != 'no-receipt-escalation':
            self.alive.difference_update((self.compute, self.runner))
            self.alive.add(self.writer)
            self.events.append('receipt-writer-started')
        if signum == signal.SIGKILL:
            self.alive.remove(pid)

    def current(self):
        with self.store.transaction() as con:
            return self.store.get(con, self.job['id'])

    def prepare(self, job):
        self.events.append('prepared')
        if self.mode == 'anchor-lost':
            self.alive.remove(self.anchor)
        if self.mode == 'worker-replaced':
            with self.store.transaction() as con:
                latest = self.store.get(con, self.job['id'])
                latest['worker'] = {**latest['worker'], 'birth': 999.0}
                self.store.save(con, latest)
        return True

    def pipe(self, data):
        self.events.append(data.decode().strip())
        if data == b'GO\n':
            self.just_launched = True
        elif data == b'RELEASE\n':
            assert self.receipt.exists(), 'RELEASE without a receipt'
            assert self.members(self.anchor, session=True) == [self.anchor], 'RELEASE with live descendants'
            assert self.anchor in self.alive, 'RELEASE after authority loss'
            self.released = True
            self.alive.remove(self.anchor)

    def after_go(self):
        # The client can acquire its write transaction after GO returns.
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            if self.mode == 'independent-unknown':
                runtime.mark_unknown(self.store, con, job, 'Independent reconciliation required')
            elif self.mode == 'members-unreadable':
                self.armed = True
            elif self.mode != 'release-cancel':
                job.update(status='Cancelling', cancel_requested=True)
            self.store.save(con, job)
        if self.mode in {'receipt-first', 'late-release', 'independent-unknown', 'members-unreadable', 'release-cancel'}:
            self.alive.difference_update((self.runner, self.compute))
            self.write_receipt()

    def wait(self, timeout=None):
        assert self.anchor not in self.alive, 'Reaping a live authority'
        return 0

    def write_receipt(self):
        self.receipt.write_text(json.dumps(dict(exit_code=143, recorded_at=1)))
        self.events.append('receipt')

    def identify(self, pid=None):
        pid = 99 if pid is None else pid
        self.checked.append(pid)
        if pid not in self.alive:
            raise psutil.NoSuchProcess(pid)
        return self.owners[pid]

    def owner_alive(self, owner):
        self.checked.append(owner['pid'])
        return owner == self.owners.get(owner['pid']) and owner['pid'] in self.alive

    def observe(self, owner):
        if self.just_launched:
            self.just_launched = False
            self.after_go()
        if self.armed and self.mode in {'late-go', 'late-release'} and owner['pid'] == self.anchor:
            return Mock(status='UNVERIFIABLE', deciding_check='boot_session')
        if self.mode.startswith('unavailable-') and owner['pid'] == self.anchor and 'GO' in self.events:
            if self.clock < 66:
                return Mock(status='UNVERIFIABLE', deciding_check='start_token')
            if self.mode == 'unavailable-then-gone':
                return Mock(status='GONE', deciding_check='start_token')
        return Mock(status='VERIFIED' if self.owner_alive(owner) else 'GONE', deciding_check='pid')

    def sid(self, pid):
        if pid not in self.alive:
            raise ProcessLookupError()
        return self.sentinel if pid == self.sentinel else self.anchor

    def pgid(self, pid):
        self.sid(pid)
        return self.compute if pid == self.compute else self.sentinel if pid == self.sentinel else self.anchor

    def members(self, pgid, *, session=False):
        if self.armed and self.mode == 'members-unreadable':
            raise ownership.OwnershipUnavailable('group_members', 'Managed process group is not fully observable')
        return sorted(pid for pid in self.alive if pid != 99 and
                      (self.sid(pid) if session else self.pgid(pid)) == pgid)

    def send_group(self, pgid, signum):
        targets = [pid for pid in self.alive if pid != 99 and self.pgid(pid) == pgid]
        if not targets:
            raise ProcessLookupError()
        if not signum:
            return
        assert self.sentinel not in targets, 'Unrelated sentinel was targeted'
        assert all(pid in self.checked for pid in targets if pid in (self.anchor, self.compute))
        self.signals.append((pgid, signum))
        self.events.append(('signal', pgid, signum))
        if pgid == self.compute and signum == signal.SIGTERM and self.mode != 'no-receipt-escalation':
            self.alive.difference_update((self.compute, self.runner))
            self.alive.add(self.writer)
            self.events.append('receipt-writer-started')
        if pgid == self.anchor and signum == signal.SIGTERM and self.writer in self.alive:
            self.alive.difference_update((self.writer, self.anchor))
            self.events.append('receipt-writer-term-anchor-exit125')
        if signum == signal.SIGKILL:
            self.alive.difference_update(targets)

    def sleep(self, interval):
        self.clock += interval
        if self.mode.startswith('unavailable-') and self.clock-interval < 66:
            job = self.current()
            assert not self.signals and not self.released
            assert not self.leader.stdin.close.called
            assert job['ownership_holds']['guardian']['check'] == 'anchor.start_token'
            self.held_states.append((job['status'], interval))
        if self.mode in {'independent-unknown', 'signal-superseded'}:
            assert self.current()['status'] == 'Unknown'
            assert not self.released and not self.leader.stdin.close.called
            assert not self.signals
            self.events.append('held-independent-unknown')
            self.alive.discard(self.anchor)
        if self.armed and self.mode in {'late-go', 'late-release', 'members-unreadable'}:
            assert not self.released and not self.leader.stdin.close.called
            assert not self.signals
            if self.mode == 'late-go':
                assert 'GO' not in self.events
            self.events.append('observed-hold')
            self.armed = False
        if self.clock > (80 if self.mode.startswith('unavailable-') else 10):
            raise AssertionError('Modeled cancellation exceeded its bounded lifecycle')
        if self.writer in self.alive:
            self.alive.remove(self.writer)
            self.write_receipt()

    def run(self, *, full_admission=False):
        guard = dict(state='NO_MATCH', observations=[], reason='', observed_at=1)
        def scan(*args, **kwargs):
            self.events.append('admission-scan')
            return guard
        self.coordinator_type = suspension.Coordinator
        with ExitStack() as stack:
            for target, value in (
                ('cmw.jobs.runtime.subprocess.Popen', self.leader),
                ('cmw.jobs.runtime.dispatch_authority', True),
            ):
                stack.enter_context(patch(target, return_value=value))
            if not full_admission:
                stack.enter_context(patch.object(runtime.sharing, 'evaluate', return_value={'permitted': True}))
            stack.enter_context(patch('cmw.jobs.runtime.activity.DEFAULT_OBSERVER.scan', side_effect=scan))
            stack.enter_context(patch('cmw.jobs.suspension.Coordinator', side_effect=self.make_coordinator))
            stack.enter_context(patch.object(self.store, 'snapshot', side_effect=lambda: {'jobs': [self.current()]}))
            stack.enter_context(patch('cmw.jobs.runtime.record_guard'))
            stack.enter_context(patch.object(ownership, '_legacy_identity', side_effect=self.identify))
            stack.enter_context(patch('cmw.jobs.runtime.signal.signal'))
            stack.enter_context(patch('cmw.jobs.ownership_hold.observe_owner', side_effect=self.observe))
            stack.enter_context(patch.object(suspension, 'observe_owner', side_effect=self.observe))
            for module in (runtime, ownership, suspension):
                if module is not suspension:
                    stack.enter_context(patch.object(module, 'identity', side_effect=self.identify))
                stack.enter_context(patch.object(module, 'owner_alive', side_effect=self.owner_alive))
                stack.enter_context(patch.object(module, 'group_members', side_effect=self.members))
            stack.enter_context(patch('os.getsid', side_effect=self.sid))
            stack.enter_context(patch('os.getpgid', side_effect=self.pgid))
            stack.enter_context(patch('os.killpg', side_effect=self.send_group))
            stack.enter_context(patch('os.kill', side_effect=self.send_pid))
            stack.enter_context(patch.object(ownership.psutil, 'pids', side_effect=lambda: sorted(self.alive-{99})))
            stack.enter_context(patch.object(ownership.psutil, 'Process', side_effect=self.process))
            stack.enter_context(patch('time.monotonic', side_effect=lambda: self.clock))
            stack.enter_context(patch('time.sleep', side_effect=self.sleep))
            runtime.worker(self.store, self.job['id'], self.claim)
        assert self.sentinel in self.alive
        return self.current()


class CancellationAnchorTests(unittest.TestCase):
    def model(self, mode):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-anchor-model-')
        self.addCleanup(temporary.cleanup)
        return WorkerCancellationModel(Path(temporary.name), mode)

    def ordinary(self, mode, *, session=False):
        model = self.model(mode)
        with model.store.transaction() as con:
            job = model.store.get(con, model.job['id'])
            job['suspension'] = None
            job['env'].pop('CMW_JOBS_OWN_SESSION', None)
            if session:
                job['env']['CMW_JOBS_OWN_SESSION'] = '1'
            model.store.save(con, job)
        return model

    def test_go_commits_run_and_group_before_writing_while_queue_is_locked(self):
        model = self.ordinary('receipt-first')
        connect = sqlite3.connect
        commits = []
        class Observed(sqlite3.Connection):
            def commit(connection):
                row = connection.execute('SELECT data FROM jobs WHERE id=?', (model.job['id'],)).fetchone()
                super().commit()
                commits.append(json.loads(row[0]))
        def pipe(data):
            if data == b'GO\n':
                self.assertEqual(commits[-1]['group'], model.owners[model.anchor])
                self.assertEqual(commits[-1]['status'], 'Run')
                connection = connect(model.store.path, timeout=0)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                        connection.execute('BEGIN IMMEDIATE')
                finally:
                    connection.close()
            model.pipe(data)
        model.leader.stdin.write.side_effect = pipe
        def study_preflight(job):
            # The study verifier looks up its registration through an independent
            # read-only connection during sharing.evaluate's final admission.
            with model.store.read_transaction() as con:
                self.assertEqual(model.store.get(con, job['id'])['status'], 'Starting')
            model.events.append('nested preflight')
            return ''
        with patch.object(sqlite3, 'connect', side_effect=lambda *a, **kw: connect(*a, **kw, factory=Observed)), \
                patch.object(runtime, 'launch_blocker', side_effect=study_preflight), \
                patch.object(runtime.sharing, 'guard_fresh', return_value=True):
            job = model.run(full_admission=True)
        self.assertEqual(job['status'], 'Cancelled', job['reason'])
        self.assertLess(model.events.index('nested preflight'), model.events.index('GO'))

    def test_failed_run_commit_cannot_launch_the_payload(self):
        model = self.ordinary('receipt-first')
        connect = sqlite3.connect
        failures = []
        class Full(sqlite3.Connection):
            def commit(connection):
                row = connection.execute('SELECT data FROM jobs WHERE id=?', (model.job['id'],)).fetchone()
                if not failures and json.loads(row[0])['status'] == 'Run':
                    failures.append(True)
                    raise sqlite3.OperationalError('database or disk is full')
                super().commit()
        with patch.object(sqlite3, 'connect', side_effect=lambda *a, **kw: connect(*a, **kw, factory=Full)):
            self.assertEqual(model.run()['status'], 'Unknown')
        self.assertEqual(failures, [True])
        self.assertNotIn('GO', model.events)
        self.assertEqual(model.signals, [])
        model.leader.stdin.close.assert_called_once()

    def test_delivery_gate_excludes_writers_allows_readers_and_disallows_sql_writes(self):
        store = self.model('receipt-first').store
        peer = sqlite3.connect(store.path, timeout=0, isolation_level=None)
        self.addCleanup(peer.close)
        with store.delivery_gate() as con:
            self.assertEqual(peer.execute('SELECT count(*) FROM jobs').fetchone(), (1,))
            with self.assertRaises(sqlite3.OperationalError) as writer:
                peer.execute('BEGIN IMMEDIATE')
            self.assertEqual(writer.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            with self.assertRaises(sqlite3.OperationalError) as write:
                con.execute('UPDATE jobs SET data=data')
            self.assertEqual(write.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
        peer.execute('BEGIN IMMEDIATE')
        peer.rollback()

    def test_release_waits_out_competing_writer_and_reobserves_drained_job(self):
        model = self.ordinary('release-cancel')
        peer = sqlite3.connect(model.store.path, timeout=0, isolation_level=None)
        self.addCleanup(peer.close)
        original = runtime._worker_intent
        retries = []
        @contextmanager
        def disputed(store, job, coordinator, wait):
            if not retries:
                peer.execute('BEGIN IMMEDIATE')
                def release_writer(delay):
                    self.assertFalse(model.leader.stdin.close.called)
                    self.assertFalse(model.released)
                    retries.append(delay)
                    peer.rollback()
                    wait(delay)
                with original(store, job, coordinator, release_writer):
                    yield
                self.assertFalse(model.released)  # Pre-wait drainage was discarded.
            else:
                peer.execute('BEGIN')
                self.assertEqual(peer.execute('SELECT count(*) FROM jobs').fetchone(), (1,))
                try:
                    with original(store, job, coordinator, wait):
                        yield
                    self.assertTrue(model.released)  # RELEASE coexists with a reader.
                finally:
                    peer.rollback()
        with patch.object(runtime, '_worker_intent', disputed):
            job = model.run()
        self.assertEqual(retries, [.05])
        self.assertEqual(model.events.count('RELEASE'), 1)
        self.assertEqual((job['status'], job['exit_code']), ('Fail', 143))

    def test_contention_in_transaction_body_is_not_replayed(self):
        store = self.model('receipt-first').store
        waits = Mock()
        with self.assertRaisesRegex(JobsError, 'database is locked'):
            with store.transaction(wait=waits):
                # The typed exception is produced by SQLite, not message matching.
                other = sqlite3.connect(store.path, timeout=0)
                try:
                    other.execute('BEGIN IMMEDIATE')
                finally:
                    other.close()
        waits.assert_not_called()

    def test_cancel_winning_final_admission_keeps_go_closed(self):
        model = self.ordinary('cancel-before-go')
        # The blocked pre-GO shell exits on EOF; no payload has started.
        model.leader.stdin.close.side_effect = lambda: model.alive.difference_update(
            (model.anchor, model.runner, model.compute))
        original = runtime.ownership_hold.WorkerAuthority.wait
        def cancel_after_read(authority, **kwargs):
            job = original(authority, **kwargs)
            if kwargs.get('before_admission'):
                model.store.change(job['id'], 'cancel', confirm=True)
            return job
        with patch.object(runtime.ownership_hold.WorkerAuthority, 'wait', cancel_after_read):
            self.assertEqual(model.run()['status'], 'Cancelled', model.events)
        self.assertNotIn('GO', model.events)
        self.assertEqual(model.signals, [])

    def test_release_superseded_during_drainage_reobserves_accepted_cancel(self):
        model = self.ordinary('release-cancel')
        reads = 0
        original = runtime.ownership_hold.WorkerAuthority.read_domain
        def cancel_after_drainage(authority, observation):
            nonlocal reads
            value = original(authority, observation)
            reads += 1
            if reads == 2:
                model.store.change(model.job['id'], 'cancel', confirm=True)
            return value
        with patch.object(runtime.ownership_hold.WorkerAuthority, 'read_domain', cancel_after_drainage):
            self.assertEqual(model.run()['status'], 'Cancelled', model.events)
        self.assertGreaterEqual(reads, 4)
        self.assertLess(model.events.index(('signal', model.anchor, signal.SIGTERM)), model.events.index('RELEASE'))

    def test_superseded_ordinary_signal_after_last_read_never_reaches_kernel(self):
        for session in (False, True):
            with self.subTest(owned_session=session):
                model = self.ordinary('signal-superseded', session=session)
                def revoke():
                    with model.store.transaction() as con:
                        job = model.store.get(con, model.job['id'])
                        runtime.mark_unknown(model.store, con, job, 'Independent reconciliation required')
                    model.events.append('revoked')
                calls = 0
                def after_read():
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        revoke()
                wait = runtime.ownership_hold.WorkerAuthority.wait
                def checked_wait(authority, **kwargs):
                    value = wait(authority, **kwargs)
                    if not session and 'GO' in model.events:
                        after_read()
                    return value
                read = ownership._session_read
                def checked_witness(check, observation):
                    value = read(check, observation)
                    if check == 'session_member':
                        after_read()  # Second read is the final subgroup witness.
                    return value
                with patch.object(runtime.ownership_hold.WorkerAuthority, 'wait', checked_wait), \
                        patch.object(ownership, 'observe_owner', side_effect=model.observe), \
                        patch.object(ownership, '_session_read', checked_witness):
                    self.assertEqual(model.run()['status'], 'Unknown', model.events)
                self.assertIn('revoked', model.events)
                self.assertIn('held-independent-unknown', model.events)
                self.assertEqual(model.signals, [])
                self.assertFalse(model.released)

    def test_replaced_subgroup_witness_while_acquiring_queue_lock_gets_no_signal(self):
        model = self.ordinary('no-receipt-escalation', session=True)
        original = runtime._worker_intent
        replaced = []
        @contextmanager
        def acquiring(*args):
            with original(*args):
                if not replaced:
                    model.owners[model.compute] = {**model.owners[model.compute], 'birth': 999.0}
                    replaced.append(True)
                yield
        with patch.object(runtime, '_worker_intent', acquiring), \
                patch.object(ownership, 'observe_owner', side_effect=model.observe):
            self.assertEqual(model.run()['status'], 'Unknown', model.events)
        self.assertEqual(replaced, [True])
        self.assertEqual(model.signals, [])

    def test_locked_subgroup_read_denial_releases_queue_before_hold_and_delivery(self):
        model = self.ordinary('no-receipt-escalation', session=True)
        original = ownership._session_read
        denied = []
        def once(check, read):
            if check == 'signal_witness' and not denied:
                denied.append(True)
                raise ownership.OwnershipUnavailable('signal_witness', 'temporarily denied')
            return original(check, read)
        holds = []
        backoff = runtime.ownership_hold.WorkerAuthority._backoff
        def held(authority, delay):
            current = model.current()  # Acquires a new SQLite writer.
            holds.append(current['ownership_holds']['domain']['check'])
            self.assertEqual(model.signals, [])
            backoff(authority, delay)
        with patch.object(ownership, '_session_read', once), \
                patch.object(runtime.ownership_hold.WorkerAuthority, '_backoff', held), \
                patch.object(ownership, 'observe_owner', side_effect=model.observe):
            self.assertEqual(model.run()['status'], 'Cancelled', model.events)
        self.assertEqual(holds, ['signal_witness'])
        self.assertEqual(model.signals.count((model.compute, signal.SIGTERM)), 1)

    def test_partial_session_signal_is_not_replayed_when_final_witness_finds_new_intent(self):
        model = self.ordinary('no-receipt-escalation', session=True)
        original = ownership._session_read
        reads = 0
        def change_during_anchor_read(check, observation):
            nonlocal reads
            value = original(check, observation)
            if check == 'session_member' and model.signals == [(model.compute, signal.SIGTERM)]:
                reads += 1
                if reads == 2:  # The first group was already signalled; the anchor has not been.
                    with model.store.transaction() as con:
                        job = model.store.get(con, model.job['id'])
                        job['termination_requested'] = {'reason': 'new termination intent'}
                        model.store.save(con, job)
            return value
        with patch.object(ownership, 'observe_owner', side_effect=model.observe), \
                patch.object(ownership, '_session_read', change_during_anchor_read):
            self.assertEqual(model.run()['status'], 'Cancelled', model.events)
        self.assertGreater(reads, 2)  # The final witness was reacquired under the new intent.
        self.assertEqual(model.signals.count((model.compute, signal.SIGTERM)), 1)
        self.assertEqual(model.signals.count((model.anchor, signal.SIGTERM)), 1)

    def test_receipt_writer_in_anchor_group_survives_computational_cancellation(self):
        model = self.model('receipt-race')
        job = model.run()
        self.assertEqual(job['status'], 'Cancelled', model.events)
        self.assertTrue(model.released)
        self.assertFalse(any(group == model.anchor for group, sig in model.signals if sig))
        self.assertLess(model.events.index('receipt'), model.events.index('RELEASE'))
        self.assertEqual(job['attempt_id'], model.job['attempt_id'])

    def test_genuine_receipt_racing_cancel_releases_without_signalling_anchor(self):
        model = self.model('receipt-first')
        job = model.run()
        self.assertEqual(job['status'], 'Cancelled', model.events)
        self.assertTrue(model.released)
        self.assertEqual(model.signals, [])
        self.assertLess(model.events.index('receipt'), model.events.index('RELEASE'))

    def test_genuine_receipt_during_cancel_ack_wait_wins_without_signal(self):
        model = self.model('receipt-during-ack')
        job = model.run()
        self.assertEqual(job['status'], 'Cancelled', model.events)
        self.assertTrue(model.released)
        self.assertEqual(model.signals, [])
        self.assertLess(model.events.index('receipt'), model.events.index('RELEASE'))
        self.assertEqual(job['attempt_id'], model.job['attempt_id'])
        self.assertFalse(model.alive & {model.anchor, model.runner, model.compute, model.writer})

    def test_hold_after_channel_setup_requires_fresh_admission_before_go(self):
        model = self.model('late-go')
        self.assertEqual(model.run()['status'], 'Cancelled')
        held = model.events.index('observed-hold')
        self.assertLess(model.events.index('channel-ready'), held)
        self.assertIn('admission-scan', model.events[held+1:model.events.index('GO')])

    def test_hold_after_final_drainage_rechecks_drainage_before_release(self):
        model = self.model('late-release')
        self.assertEqual(model.run()['status'], 'Cancelled')
        self.assertIn('observed-hold', model.events)
        self.assertGreater(model.release_checks, 2)
        self.assertTrue(model.released)

    def test_unrelated_unknown_keeps_pipe_and_does_not_authorize_release(self):
        model = self.model('independent-unknown')
        job = model.run()
        self.assertIn('held-independent-unknown', model.events)
        self.assertEqual((job['status'], job['reason']), ('Unknown', 'Independent reconciliation required'))
        self.assertFalse(model.released)

    def test_ordinary_worker_retries_unreadable_members_without_closing_pipe(self):
        model = self.model('members-unreadable')
        with model.store.transaction() as con:
            job = model.store.get(con, model.job['id'])
            job['suspension'] = None
            model.store.save(con, job)
        self.assertEqual(model.run()['status'], 'Fail')  # synthetic exit receipt is 143
        self.assertIn('observed-hold', model.events)
        self.assertTrue(model.released)

    def test_controller_uncertainty_blocks_dispatch_and_preserves_operator_pause_on_recovery(self):
        model = self.model('controller-hold')
        store, job_id = model.store, model.job['id']
        with store.transaction() as con:
            job = store.get(con, job_id)
            job.update(status='Run', worker=model.owners[99], group=model.owners[model.anchor])
            store.save(con, job)
            with patch.object(runtime, 'observe_owner', return_value=Mock(status='UNVERIFIABLE', deciding_check='boot')):
                runtime.reconcile(store, con, job)
            self.assertEqual(job['status'], 'Run')
            self.assertEqual(job['ownership_holds']['controller']['check'], 'worker.boot')
            with self.assertRaises(JobsError):
                store._dispatch(con, True)
            store._dispatch(con, False)
            with patch.object(runtime, 'observe_owner', return_value=Mock(status='VERIFIED')), \
                    patch.object(runtime, 'lock_held', return_value=True):
                runtime.reconcile(store, con, job)
            self.assertNotIn('ownership_holds', job)
            self.assertFalse(store.control(con)['dispatch'])

    def test_unverifiable_anchor_retains_pipe_retries_and_resumes_after_unknown(self):
        model = self.model('unavailable-then-verified')
        job = model.run()
        self.assertTrue(model.released)
        self.assertEqual(job['status'], 'Cancelled')
        self.assertNotIn('ownership_holds', job)
        self.assertIn('Unknown', [state for state, _ in model.held_states])
        self.assertEqual([delay for _, delay in model.held_states[:6]], [.25, .5, 1, 2, 4, 5])
        with model.store.transaction() as con:
            messages = [row[0] for row in con.execute('SELECT message FROM events ORDER BY id')]
        self.assertTrue(any('guardian cannot verify anchor.start_token' in msg for msg in messages))
        self.assertTrue(any('guardian control hold cleared' in msg for msg in messages))

    def test_unverifiable_then_gone_never_signals_or_releases(self):
        model = self.model('unavailable-then-gone')
        job = model.run()
        self.assertEqual(job['status'], 'Unknown')
        self.assertIn('Group leader disappeared', job['reason'])
        self.assertEqual(model.signals, [])
        self.assertFalse(model.released)
        self.assertIn('Unknown', [state for state, _ in model.held_states])

    def test_unexpected_anchor_loss_retains_unknown_and_commitment(self):
        model = self.model('anchor-lost')
        job = model.run()
        self.assertEqual(job['status'], 'Unknown')
        self.assertTrue(job['cancel_requested'])
        self.assertEqual(job['resources'], model.job['resources'])
        self.assertIsNone(job['finished_at'])
        self.assertFalse(model.released)
        self.assertEqual(model.signals, [])
        with model.store.transaction() as con:
            self.assertFalse(model.store.control(con)['dispatch'])

    def test_replaced_worker_authority_cannot_continue_signalling(self):
        model = self.model('worker-replaced')
        job = model.run()
        self.assertEqual(job['status'], 'Unknown')
        self.assertEqual(model.signals, [])
        self.assertFalse(model.released)
        self.assertIsNone(job['finished_at'])

    def test_escalation_cannot_claim_cancelled_without_genuine_payload_receipt(self):
        model = self.model('no-receipt-escalation')
        job = model.run()
        self.assertEqual(job['status'], 'Unknown', model.events)
        self.assertFalse(model.receipt.exists())
        self.assertIsNone(job['finished_at'])
        self.assertFalse(model.released)
        self.assertTrue(job['cancel_requested'])
        self.assertEqual(job['resources'], model.job['resources'])


class PayloadReceiptTopologyTests(unittest.TestCase):
    def test_anchor_group_term_can_kill_receipt_writer_before_release(self):
        """Controlled demonstration of the shell hazard, not historical attribution."""
        with tempfile.TemporaryDirectory(prefix='cmw-receipt-topology-') as temporary, \
                tempfile.TemporaryDirectory(prefix='cmw-socket-', dir='/tmp') as socket_directory:
            root = Path(temporary)
            # AF_UNIX paths must fit even when the campaign's TMPDIR is long.
            endpoint = Path(socket_directory)/'control.sock'
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(endpoint))
            listener.listen(1)
            listener.settimeout(5)
            executable = root/'receipt-writer'
            executable.write_text('#!'+sys.executable+'\n'+'''import json,os,socket,sys
import psutil
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.connect(os.environ['FIXTURE_RECEIPT_SOCKET'])
s.sendall(json.dumps({'pid':os.getpid(),'birth':psutil.Process().create_time(),'pgid':os.getpgrp(),'sid':os.getsid(0)}).encode()+b'\\n')
s.recv(1)
raise SystemExit(0)
''')
            executable.chmod(0o700)
            receipt = root/'receipt.json'
            helper = Path(runtime.__file__).with_name('payload.sh')
            process = subprocess.Popen(['/bin/bash', str(helper), str(executable), '', str(receipt),
                                        str(root/'stdout'), str(root/'stderr'), '/bin/true'],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True,
                                       env={**os.environ, 'FIXTURE_RECEIPT_SOCKET': str(endpoint)})
            anchor = ownership.identity(process.pid)
            connection = None
            child = None
            try:
                process.stdin.write(b'GO\n'); process.stdin.flush()
                connection, _ = listener.accept(); connection.settimeout(5)
                child = json.loads(connection.recv(4096))
                self.assertEqual(child['pgid'], process.pid)
                self.assertEqual(child['sid'], process.pid)
                self.assertEqual(ownership.identity(process.pid), anchor)
                self.assertEqual(psutil.Process(child['pid']).create_time(), child['birth'])
                os.killpg(process.pid, signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 125)
                self.assertFalse(receipt.exists())
                self.assertFalse(ownership.group_exists(process.pid))
            finally:
                if connection is not None:
                    connection.close()
                if process.poll() is None:
                    # An assertion failure releases only this recorded fixture.
                    process.stdin.write(b'RELEASE\n'); process.stdin.flush()
                    process.wait(timeout=5)
                process.stdin.close()
                if child is not None:
                    with self.assertRaises(psutil.NoSuchProcess):
                        psutil.Process(child['pid']).create_time()
                    print('Fixture census: '+json.dumps({'anchor': anchor, 'receipt_child': child,
                        'anchor_exit_code': process.returncode, 'receipt_child_absent': True,
                        'group_absent': not ownership.group_exists(process.pid)}), flush=True)


if __name__ == '__main__':
    unittest.main()
