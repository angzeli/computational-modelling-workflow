"""A held read always has an active retry driver; all processes are fictional."""
import errno
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cmw.jobs import ownership, ownership_hold as hold
from cmw.jobs.store import JobsError, Store
from cmw.jobs.suspension import DomainPending, RequestSuperseded


BOOT = 'a0dc8d4e-c809-4717-9dbe-c4b2fbece908'
WORKER = {'version': 2, 'host': 'fixture', 'boot_session': BOOT, 'pid': 10001, 'start_token': 1}
ANCHOR = {**WORKER, 'pid': 10002, 'start_token': 2}


class FixtureBound(BaseException):
    """Escape a broken retry loop without being mistaken for a read failure."""


class OwnershipHoldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-owned-hold-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state')
        self.job = self.store.add(argv=['/bin/true'], cwd=self.root, name='hold fixture', cpus=1)
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job.update(status='Run', order=None, claim='fixture', worker=WORKER, group=ANCHOR)
            self.store.save(con, job)
        self.clock, self.delays = 100., []
        self.child = Mock()
        self.child.poll.return_value = None
        self.authority = hold.WorkerAuthority(self.store, job, ANCHOR, self.child)
        self.enterContext(patch.object(hold.time, 'monotonic', side_effect=lambda: self.clock))
        self.enterContext(patch.object(hold.time, 'sleep', side_effect=self.advance))
        self.enterContext(patch.object(hold, 'clock_stamp', side_effect=lambda now=None: {
            'boot_session_id': BOOT, 'monotonic': self.clock if now is None else now}))
        self.enterContext(patch.object(hold, 'observe_owner', return_value=SimpleNamespace(
            status=ownership.OwnershipStatus.VERIFIED, deciding_check='identity')))

    def advance(self, delay):
        self.clock += delay
        self.delays.append(delay)
        if self.clock > 200:
            raise FixtureBound('ownership recovery exceeded the bounded virtual lifecycle')

    def current(self):
        with self.store.transaction(create=False) as con:
            return self.store.get(con, self.job['id'])

    def messages(self):
        with self.store.transaction(create=False) as con:
            return [row[0] for row in con.execute('SELECT message FROM events')]

    def assert_resolved(self):
        self.assertNotIn('ownership_holds', self.current())
        self.assertEqual(self.authority._reads, [])
        self.assertEqual(self.authority.wait()['status'], 'Run')
        self.child.stdin.close.assert_not_called()

    def test_one_unavailable_read_reobserves_after_recovery_and_returns(self):
        calls = []
        def read():
            calls.append(self.clock)
            if len(calls) == 1:
                raise ownership.OwnershipUnavailable('descendants', 'unreadable')
            return 'complete'
        self.assertEqual(self.authority.read_domain(read), 'complete')
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.delays, [.25])
        self.assert_resolved()

    def test_each_semantic_exit_clears_its_hold_before_propagating(self):
        for exception in (DomainPending('root exited'), RequestSuperseded('new cancellation'),
                          JobsError('contradictory evidence'), ownership.OwnershipContradiction('identity changed'),
                          ProcessLookupError(errno.ESRCH, 'original is gone')):
            with self.subTest(exception=type(exception).__name__):
                calls = []
                def read():
                    calls.append(1)
                    if len(calls) == 1:
                        raise ownership.OwnershipUnavailable('descendants', 'unreadable')
                    raise exception
                with self.assertRaises(type(exception)):
                    self.authority.read_domain(read)
                self.assertEqual(len(calls), 2)
                self.assert_resolved()

    def test_raw_and_wrapped_read_failures_keep_the_operation_until_recovery(self):
        queue = JobsError('Queue unavailable')
        queue.__cause__ = sqlite3.OperationalError('injected unavailable read')
        for error in (OSError(errno.EAGAIN, 'retry'), PermissionError(errno.EPERM, 'denied'),
                      queue, RuntimeError('unexpected observation failure')):
            with self.subTest(error=type(error).__name__):
                calls = []
                def read():
                    calls.append(1)
                    if len(calls) == 1:
                        raise error
                    return 42
                self.assertEqual(self.authority.read_domain(read), 42)
                self.assertEqual(len(calls), 3)
                self.assert_resolved()

    def test_nested_hold_restarts_the_enclosing_partial_observation(self):
        inner, outer = [], []
        def nested():
            inner.append(1)
            if len(inner) == 1:
                raise ownership.OwnershipUnavailable('nested', 'unavailable')
            return 1
        def enclosing():
            outer.append(1)
            return self.authority.read_domain(nested)
        self.assertEqual(self.authority.read_domain(enclosing), 1)
        self.assertEqual((len(outer), len(inner)), (2, 4))
        self.assert_resolved()

    def test_wait_drives_a_registered_pending_observation(self):
        calls = []
        def read():
            calls.append(1)
            if len(calls) == 1:
                raise ownership.OwnershipUnavailable('descendants', 'one more unavailable read')
            return []
        operation = hold._ReadOperation(read, held=True,
                                       unavailable=ownership.OwnershipUnavailable('descendants', 'pending'))
        self.authority._reads.append(operation)
        self.authority._domain_state()
        self.assertEqual(self.authority.wait()['status'], 'Run')
        self.assertEqual(len(calls), 3)
        self.assert_resolved()

    def test_hold_without_driver_reports_an_immediate_invariant_violation(self):
        with self.store.transaction() as con:
            hold.unavailable(self.store, con, self.store.get(con, self.job['id']),
                             'domain', 'descendants', self.clock)
        with self.assertRaisesRegex(hold.HoldDriverMissing, 'no re-evaluation driver'), \
                patch.object(hold.sys, 'stderr') as diagnostic:
            self.authority.wait()
        self.assertTrue(diagnostic.write.called)
        self.assertEqual(self.delays, [])
        self.assertIn('domain', self.current()['ownership_holds'])
        self.child.stdin.close.assert_not_called()

    def test_long_uncertainty_coalesces_attention_without_changing_state(self):
        held_states = []
        def read():
            if self.clock < 166:
                held_states.append(self.current()['status'])
                raise ownership.OwnershipUnavailable('descendants', 'still unreadable')
            return []
        self.assertEqual(self.authority.read_domain(read), [])
        self.assertEqual(set(held_states), {'Run'})
        self.assertEqual(sum('attention required' in row for row in self.messages()), 1)
        self.assertLessEqual(max(self.delays), 5.)
        self.assert_resolved()

    def test_unavailable_hold_publication_does_not_lose_the_registered_driver(self):
        get = self.store.get
        failed = []
        def unavailable_once(con, job_id):
            if self.authority._reads and not failed:
                failed.append(1)
                self.assertTrue(self.authority._reads[0].held)
                raise OSError(errno.EAGAIN, 'queue read unavailable')
            return get(con, job_id)
        calls = []
        def read():
            calls.append(1)
            if len(calls) == 1:
                raise ownership.OwnershipUnavailable('descendants', 'unavailable')
            return []
        with patch.object(self.store, 'get', side_effect=unavailable_once):
            self.assertEqual(self.authority.read_domain(read), [])
        self.assertEqual(failed, [1])
        self.assert_resolved()

    def test_wait_retries_a_queue_read_without_closing_the_pipe(self):
        get = self.store.get
        failures = []
        def read(con, job_id):
            if not failures:
                failures.append(1)
                raise PermissionError(errno.EPERM, 'queue denied once')
            return get(con, job_id)
        with patch.object(self.store, 'get', side_effect=read):
            self.assertEqual(self.authority.wait()['status'], 'Run')
        self.assertEqual(failures, [1])
        self.assertEqual(self.delays, [.25])
        self.assert_resolved()

    def test_completion_owner_ignores_only_its_own_completion_hold(self):
        with self.store.transaction() as con:
            hold.unavailable(self.store, con, self.store.get(con, self.job['id']),
                             'completion', 'receipt', self.clock)
        self.authority.completing = True
        self.assertEqual(self.authority.wait()['status'], 'Run')
        self.assertEqual(self.delays, [])
        with self.store.transaction() as con:
            hold.unavailable(self.store, con, self.store.get(con, self.job['id']),
                             'domain', 'missing driver', self.clock)
        with self.assertRaises(hold.HoldDriverMissing):
            self.authority.wait()

    def test_pure_retry_does_not_require_an_anchor_and_survives_reporting_failure(self):
        reads = []
        def read():
            reads.append(1)
            if len(reads) < 3:
                raise PermissionError(errno.EPERM, 'unavailable')
            return 'receipt'
        report = Mock(side_effect=[OSError(errno.EAGAIN, 'report unavailable'), None])
        self.assertEqual(hold.retry_read(read, wait=self.advance, on_unavailable=report), 'receipt')
        self.assertEqual(self.delays, [.25, .5])
        self.assertEqual(report.call_count, 2)
        self.child.poll.assert_not_called()

    def test_pure_retry_never_retries_an_explicit_contradiction(self):
        read = Mock(side_effect=ownership.OwnershipContradiction('receipt changed'))
        with self.assertRaises(ownership.OwnershipContradiction):
            hold.retry_read(read, wait=self.advance)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.delays, [])


class PresenceObservationTests(unittest.TestCase):
    def test_presence_absence_and_unavailability_are_distinct(self):
        for error, status in ((None, 'PRESENT'), (ProcessLookupError(), 'ABSENT'),
                              (OSError(errno.EAGAIN, 'retry'), 'UNAVAILABLE'),
                              (PermissionError(errno.EPERM, 'denied'), 'UNAVAILABLE'),
                              (RuntimeError('unreadable probe'), 'UNAVAILABLE')):
            with self.subTest(error=type(error).__name__), patch.object(ownership.os, 'killpg', side_effect=error) as probe:
                self.assertEqual(ownership.observe_group(123).status, status)
                probe.assert_called_once_with(123, 0)

    def test_compatibility_wrapper_preserves_the_unavailable_cause(self):
        for error in (OSError(errno.EAGAIN, 'retry'), PermissionError(errno.EPERM, 'denied')):
            with self.subTest(error=error), patch.object(ownership.os, 'killpg', side_effect=error):
                with self.assertRaises(ownership.OwnershipUnavailable) as caught:
                    ownership.group_exists(123)
                self.assertIs(caught.exception.__cause__, error)

    def test_lock_read_distinguishes_missing_from_unreadable(self):
        path = Mock()
        for error, absent in ((FileNotFoundError(), True), (PermissionError(), False),
                              (OSError(errno.EAGAIN, 'retry'), False)):
            with self.subTest(error=error):
                path.open.side_effect = error
                if absent:
                    self.assertIs(ownership.lock_held(path, strict=True), False)
                else:
                    with self.assertRaises(type(error)):
                        ownership.lock_held(path, strict=True)
        path.exists.assert_not_called()
