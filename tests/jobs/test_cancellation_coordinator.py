"""Actual cancellation coordinator with deterministic process/receipt races."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import errno
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs import ownership, suspension
from cmw.jobs.store import JobsError, Store
from tests.jobs.test_cancellation_ownership import ProcessTable


class CancellationCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw-cancel-coordinator-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.table = ProcessTable(self)
        self.table.rows[20]['ppid'] = 11
        self.table.rows[21]['ppid'] = 11
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.table.install(self.stack)
        self.store = Store(self.root/'state')
        self.job = self.store.add(argv=['/unexecuted/controlled-fixture'], cwd=self.root, name='cancellation fixture',
                                  cpus=2, memory_gib=.25, hold=True,
                                  suspension_backend='local-command-v1')
        (self.store.root/'attempts'/self.job['attempt_id']).mkdir(parents=True)
        self.worker = dict(pid=99, birth=99.0, host='fixture-host', boot=1.0)
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job.update(status='Cancelling', cancel_requested=True, order=None,
                       worker=self.worker, group=self.table.anchor, claim='fixture-claim')
            job['suspension'].update(phase='Suspended', inhibited=True)
            self.store.save(con, job)
        self.before = deepcopy(self.current())
        self.coordinator = suspension.Coordinator(self.store, self.current(), self.table.anchor)
        self.coordinator.registration = dict(root=self.table.record(11), runner=self.table.record(13),
                                            protected=[], strategy='direct-tree')
        self.mpi = False
        self.coordinator._validate = self.domain
        # Only the independent runner's ACK is injected. Store intent,
        # command writing, preparation, observation and signalling stay real.
        self.coordinator.ack = Mock(return_value={'status': 'cancel_prepared'})
        self.sent = []
        self.on_signal = None
        self.stack.enter_context(patch.object(suspension.os, 'kill', side_effect=self.send))

    def current(self):
        with self.store.transaction(create=False) as con:
            return self.store.get(con, self.job['id'])

    def domain(self):
        self.coordinator.observe_session()
        launcher = (self.table.record(11),) if self.mpi else ()
        compute = (self.table.record(20), self.table.record(21))
        if not self.mpi:
            compute = (self.table.record(11), *compute)
        domain = suspension.Domain(compute, (self.table.anchor, self.table.record(13)), launcher)
        self.coordinator.last_domain = domain
        return domain

    def send(self, pid, signum):
        self.assertNotIn(pid, {10, 13, 14, 90}, 'protected control or sentinel signalled')
        row = self.table.row(pid)
        self.assertEqual(row['sid'], 10)
        self.assertNotEqual(row['pgid'], 10)
        self.assertEqual(ownership.identity(pid), self.coordinator.observed[pid])
        self.sent.append((pid, signum))
        if self.on_signal:
            self.on_signal(pid, signum)

    def step(self, grace=2.0):
        self.coordinator.cancel_step(self.current(), grace)

    def assert_retained(self):
        job = self.current()
        self.assertEqual(job['status'], 'Cancelling')
        self.assertTrue(job['cancel_requested'])
        self.assertIsNone(job['finished_at'])
        for field in ('attempt_id', 'resources', 'scheduling', 'group', 'claim'):
            self.assertEqual(job[field], self.before[field])
        self.assertTrue(self.table.rows[10]['present'])
        self.assertTrue(self.table.rows[90]['present'])
        self.assertFalse(any(signum for _, signum in self.table.signals()))

    def test_stopped_direct_root_gets_term_then_termination_cont_once(self):
        self.table.rows[11]['status'] = psutil.STATUS_STOPPED
        self.step()
        self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM), (11, signal.SIGCONT)])
        self.assertEqual(self.current()['suspension']['phase'], 'Suspended')
        self.assertFalse(self.coordinator.ready_to_release())
        self.assert_retained()

    def test_mpi_launcher_owns_initial_shutdown_without_rank_or_anchor_signal(self):
        self.mpi = True
        self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assertFalse(self.coordinator.ready_to_release())
        self.assert_retained()

    def test_launcher_exit_and_rank_reaping_in_signal_callback_needs_no_duplicate_delivery(self):
        self.mpi = True
        self.on_signal = lambda pid, sig: self.table.remove(11, 20, 21)
        self.step()
        self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assertFalse(self.coordinator.ready_to_release())  # runner and writer still alive
        self.table.remove(13, 14)
        self.assertTrue(self.coordinator.ready_to_release())
        self.assert_retained()

    def test_runner_exit_leaves_descendants_committed_until_explicit_fallback(self):
        self.mpi = True
        self.step()
        self.table.remove(11, 13)
        self.assertFalse(self.coordinator.ready_to_release())
        self.table.clock = 2.1
        self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM), (20, signal.SIGKILL), (21, signal.SIGKILL)])
        self.assertFalse(self.coordinator.ready_to_release())
        self.assert_retained()

    def test_late_descendant_of_known_member_is_observed_before_fallback(self):
        self.mpi = True
        self.step()
        self.table.rows[31] = {**self.table.rows[20], 'pid': 31, 'birth': 31.0, 'pgid': 31, 'ppid': 20}
        self.assertFalse(self.coordinator.ready_to_release())
        self.table.clock = 2.1
        self.step()
        self.assertIn((31, signal.SIGKILL), self.sent)
        self.assertIn(31, self.coordinator.observed)
        self.assert_retained()

    def test_unregistered_late_orphan_refuses_fallback(self):
        self.mpi = True
        self.step()
        self.table.rows[31] = {**self.table.rows[20], 'pid': 31, 'birth': 31.0, 'pgid': 31, 'ppid': 1}
        self.table.clock = 2.1
        with self.assertRaisesRegex(JobsError, 'Unregistered late'):
            self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assert_retained()

    def test_readable_birth_esrch_and_zombie_both_block_release(self):
        self.mpi = True
        self.step()
        self.table.remove(11, 13, 14, 21)
        for changes in ({'sid_missing': True}, {'sid_missing': False, 'status': psutil.STATUS_ZOMBIE}):
            with self.subTest(changes=changes):
                self.table.rows[20].update(changes)
                self.assertFalse(self.coordinator.ready_to_release())
                self.assertIn(20, self.coordinator.observed)
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assert_retained()

    def test_denied_primary_is_never_retried_or_followed_by_continue(self):
        def deny(pid, sig):
            raise PermissionError(errno.EPERM, 'fixture denied original send')
        self.on_signal = deny
        self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.table.clock = .6
        with self.assertRaisesRegex(JobsError, 'Denied termination signal'):
            self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assert_retained()

    def test_denied_primary_requires_original_absence_and_group_absence(self):
        def deny_reap(pid, sig):
            self.table.remove(pid)
            raise PermissionError(errno.EPERM, 'fixture exit race at original send')
        self.on_signal = deny_reap
        self.step()
        self.table.zero_results = ['denied', 'absent']
        self.step()
        self.assertEqual(len(self.coordinator.denied_signals), 1)
        self.step()
        self.assertEqual(self.coordinator.denied_signals, [])
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assertFalse(self.coordinator.ready_to_release())
        self.assert_retained()

    def test_reused_original_or_inaccessible_rank_blocks_fallback(self):
        self.mpi = True
        self.step()
        self.table.clock = 2.1
        for changes in ({'birth': 999.0}, {'birth': 20.0, 'inaccessible': True}):
            with self.subTest(changes=changes):
                self.table.rows[20].update(changes)
                with self.assertRaises(JobsError):
                    self.step()
                self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assert_retained()

    def test_changed_worker_after_primary_prevents_every_further_signal(self):
        self.mpi = True
        self.step()
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job['worker'] = {**self.worker, 'birth': 999.0}
            self.store.save(con, job)
        self.table.clock = 2.1
        with self.assertRaisesRegex(JobsError, 'authority changed'):
            self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assert_retained()

    def test_cancel_preserves_run_and_transitional_phases(self):
        for phase in ('Run', 'Suspending', 'Suspended', 'Resuming', 'Unverified'):
            with self.subTest(phase=phase):
                with self.store.transaction() as con:
                    job = self.store.get(con, self.job['id'])
                    job['suspension']['phase'] = phase
                    self.store.save(con, job)
                self.step()
                self.assertEqual(self.current()['suspension']['phase'], phase)
                self.assert_retained()
        self.assertEqual(self.sent, [(11, signal.SIGTERM), (11, signal.SIGCONT)])

    def test_concurrent_cancel_accepts_once_then_repeated_steps_keep_same_request(self):
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job.update(status='Run', cancel_requested=False)
            self.store.save(con, job)
        def cancel():
            try:
                return self.store.change(self.job['id'], 'cancel', confirm=True)['status']
            except JobsError:
                return 'already-cancelling'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: cancel(), range(2)))
        self.assertCountEqual(outcomes, ['Cancelling', 'already-cancelling'])
        self.step()
        request = deepcopy(self.current()['suspension']['request'])
        for _ in range(3):
            self.step()
        self.assertEqual(self.current()['suspension']['request'], request)
        self.assertEqual(self.sent, [(11, signal.SIGTERM), (11, signal.SIGCONT)])
        with self.store.transaction() as con:
            self.assertFalse(self.store.control(con)['dispatch'])
        self.assert_retained()

    def test_group_zero_probe_denial_blocks_release_after_all_members_are_absent(self):
        self.mpi = True
        self.step()
        self.table.remove(11, 13, 14, 20, 21)
        self.table.zero_results = ['denied']
        self.assertFalse(self.coordinator.ready_to_release())
        self.assertTrue(self.coordinator.ready_to_release())
        self.assert_retained()

    def test_kernel_visible_group_after_absence_does_not_authorize_signal_or_release(self):
        self.mpi = True
        self.step()
        self.table.remove(11, 13, 14, 20, 21)
        self.table.zero_results = ['visible']
        self.assertFalse(self.coordinator.ready_to_release())
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assert_retained()


    def test_pending_or_zombie_root_before_cancel_uses_previously_verified_domain(self):
        self.domain()
        self.coordinator._validate = Mock(side_effect=AssertionError('Exiting root rebound as live'))
        self.table.rows[11]['sid_missing'] = True
        self.step()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.coordinator.ready_to_release())
        self.table.rows[11].update(sid_missing=False, status=psutil.STATUS_ZOMBIE)
        self.step()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.coordinator.ready_to_release())
        self.coordinator._validate.assert_not_called()
        self.assert_retained()

    def test_zombie_root_before_first_cancel_uses_previously_verified_domain(self):
        self.domain()
        self.coordinator._validate = Mock(side_effect=AssertionError('Zombie root rebound as live'))
        self.table.rows[11]['status'] = psutil.STATUS_ZOMBIE
        self.step()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.coordinator.ready_to_release())
        self.coordinator._validate.assert_not_called()
        self.assert_retained()

    def test_pending_term_does_not_authorize_cont_after_fresh_next_observation(self):
        def acknowledge(*args):
            self.table.rows[11]['sid_missing'] = True
            return {'status': 'cancel_prepared'}
        self.coordinator.ack.side_effect = acknowledge
        observe = self.coordinator.observe_session
        def pending_then_fresh():
            snapshot = observe()
            if any(row['identity']['pid'] == 11 for row in snapshot['pending']):
                self.table.rows[11]['sid_missing'] = False
            return snapshot
        self.coordinator.observe_session = pending_then_fresh
        self.step()
        self.assertFalse(self.table.rows[11]['sid_missing'])
        self.assertEqual(self.sent, [])
        self.assertTrue(self.coordinator.cancellation_prepared)
        self.assert_retained()

    def test_root_becomes_pending_after_term_without_immediate_unknown_or_cont(self):
        self.on_signal = lambda pid, sig: self.table.rows[11].update(sid_missing=True)
        self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM)])
        self.assertFalse(self.coordinator.ready_to_release())
        self.assert_retained()

    def test_denied_fallback_is_not_repeated_and_blocks_further_fallback_targets(self):
        self.mpi = True
        self.step()
        self.table.remove(11)
        self.table.clock = 2.1
        def deny(pid, signum):
            raise PermissionError(errno.EPERM, 'fixture denied fallback')
        self.on_signal = deny
        self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM), (20, signal.SIGKILL)])
        self.step()
        self.assertEqual(self.sent, [(11, signal.SIGTERM), (20, signal.SIGKILL)])
        self.table.clock = 2.7
        with self.assertRaisesRegex(JobsError, 'Denied termination signal'):
            self.step()
        self.assert_retained()


    def test_receipt_filename_cannot_release_without_verified_enrolled_domain(self):
        receipt = self.coordinator.directory.parent/'payload-exit.json'
        receipt.write_text('{"exit_code":143,"recorded_at":1}')
        self.table.remove(11, 13, 14, 20, 21)
        for registration in (self.coordinator.registration, None):
            with self.subTest(registration=registration):
                self.coordinator.registration = registration
                self.coordinator.last_domain = None
                with self.assertRaises(JobsError):
                    self.coordinator.ready_to_release()
                self.assertEqual(self.sent, [])
        self.assert_retained()
