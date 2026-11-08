"""Completion retries record established evidence without replaying control."""
import errno
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cmw.jobs import ownership_hold, runtime, suspension, suspension_protocol
from cmw.jobs.ownership import OwnershipContradiction, OwnershipUnavailable
from cmw.jobs.store import JobsError, Store


BOOT = '4f16cf1d-77f9-4d40-96dd-953363b0c5aa'
WORKER = {'version': 2, 'host': 'fixture', 'boot_session': BOOT, 'pid': 90001, 'start_token': 1}
ANCHOR = {**WORKER, 'pid': 90002, 'start_token': 2}
CLOCK = {'kind': 'fixture-monotonic', 'boot_session_id': BOOT}


class RetryBudgetExceeded(BaseException):
    """Bound a broken retry loop without turning the test limit into a read fault."""


class CompletionRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='cmw-completion-retry-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'queue')
        self.clock = 100.0
        self.delays = []
        self.job = self.make_job()
        self.receipt = self.root / 'payload-exit.json'
        self.receipt.write_text(json.dumps({'exit_code': 0, 'recorded_at': 1099.0}))
        self.evidence = runtime._receipt(self.receipt)
        self.leader = Mock(pid=ANCHOR['pid'])
        self.leader.wait.return_value = 0
        self.authority = SimpleNamespace(
            completing=False, generation=0, queue_wait=self.advance, _report_read=Mock(),
            claim=self.job['claim'], worker=WORKER, anchor=ANCHOR,
            wait=Mock(side_effect=self.current),
            read_domain=Mock(side_effect=lambda operation: operation()))
        self.enterContext(patch.object(runtime.time, 'time', side_effect=lambda: 1000.0 + self.clock))
        self.enterContext(patch.object(runtime.time, 'monotonic', side_effect=lambda: self.clock))
        self.enterContext(patch.object(ownership_hold, 'clock_stamp', side_effect=lambda now=None: {
            'boot_session_id': BOOT, 'monotonic': self.clock if now is None else now}))
        self.enterContext(patch.object(suspension_protocol, 'now_clock', side_effect=lambda: self.clock))
        self.enterContext(patch.object(suspension_protocol, 'clock_descriptor', return_value=CLOCK))
        self.enterContext(patch.object(runtime, 'group_members', return_value=[ANCHOR['pid']]))
        self.enterContext(patch.object(runtime, 'group_exists', return_value=False))

    def make_job(self):
        job = self.store.add(argv=['/bin/true'], cwd=self.root, name='completion fixture', cpus=1)
        with self.store.transaction() as con:
            job = self.store.get(con, job['id'])
            job.update(status='Run', order=None, claim='fixture-claim', worker=WORKER,
                       group=ANCHOR, started_at=1010.0)
            self.store.save(con, job)
        return job

    def current(self, job=None):
        with self.store.transaction(create=False) as con:
            return self.store.get(con, (job or self.job)['id'])

    def messages(self, job=None):
        with self.store.transaction(create=False) as con:
            return [row[0] for row in con.execute(
                'SELECT message FROM events WHERE job_id=? ORDER BY id', ((job or self.job)['id'],))]

    def advance(self, delay):
        self.delays.append(delay)
        self.clock += delay
        if len(self.delays) > 40:
            raise RetryBudgetExceeded('Completion did not recover within forty virtual retries')

    def publish(self, job=None, **kwargs):
        job = job or self.job
        runtime.finish(self.store, job['id'], job['claim'], 'Done', 0, 'Established completion',
                       attempt_id=job['attempt_id'], wait=self.advance, **kwargs)

    def completion(self):
        return runtime._Completion(self.store, self.job, self.authority, self.leader)

    def refusal(self, *, blocker=None, coordinator=None):
        with self.store.transaction() as con:
            self.job = self.store.get(con, self.job['id'])
            self.job.update(status='Starting', started_at=None)
            self.store.save(con, self.job)
        return runtime._AdmissionRefusal(self.store, self.job, self.authority, self.leader,
            attempt_id=self.job['attempt_id'], blocker=blocker, reason='Admission controls/order changed',
            coordinator=coordinator)

    def assert_completed_once(self, job=None):
        job = job or self.job
        result = self.current(job)
        self.assertEqual((result['status'], result['exit_code']), ('Done', 0))
        self.assertNotIn('ownership_holds', result)
        self.assertEqual(sum(message.startswith('Done:') for message in self.messages(job)), 1)
        self.leader.stdin.close.assert_not_called()
        return result

    def test_completion_retries_each_unreadable_queue_read(self):
        for error in (OSError(errno.EAGAIN, 'temporarily unavailable'),
                      PermissionError(errno.EPERM, 'temporarily denied'),
                      sqlite3.OperationalError('injected read failure'),
                      OwnershipUnavailable('queue', 'injected unavailable observation')):
            with self.subTest(error=type(error).__name__):
                job = self.make_job()
                original_get = self.store.get
                reads = []
                def read(con, job_id):
                    reads.append(job_id)
                    if len(reads) == 1:
                        raise error
                    return original_get(con, job_id)
                with patch.object(self.store, 'get', side_effect=read):
                    self.publish(job)
                self.assertEqual(len(reads), 2)
                self.assert_completed_once(job)
        self.assertEqual(self.delays, [.25] * 4)
        self.leader.stdin.write.assert_not_called()

    def test_failed_control_read_rolls_back_without_terminal_mutation(self):
        original = self.store.control
        reads = []
        def read(con):
            reads.append(1)
            if len(reads) == 1:
                raise sqlite3.OperationalError('injected control read failure')
            return original(con)
        with patch.object(self.store, 'control', side_effect=read), \
                patch.object(self.store, 'save', wraps=self.store.save) as save:
            self.publish()
        self.assertEqual(len(reads), 2)
        self.assertEqual(save.call_count, 1)
        self.assert_completed_once()

    def test_ambiguous_commit_reads_back_without_replaying_terminal_write_or_event(self):
        original = self.store._commit
        commits = []
        def commit(con, wait):
            original(con, wait)
            commits.append(1)
            if len(commits) == 1:
                raise sqlite3.OperationalError('COMMIT succeeded but its acknowledgement was lost')
        with patch.object(self.store, '_commit', side_effect=commit), \
                patch.object(self.store, 'save', wraps=self.store.save) as save, \
                patch.object(self.store, 'event', wraps=self.store.event) as event:
            self.publish(finished_at=1100.0)
        self.assertEqual(commits, [1, 1])
        self.assertEqual(save.call_count, 1)
        self.assertEqual(event.call_count, 1)
        self.assertEqual(self.assert_completed_once()['finished_at'], 1100.0)
        self.assertEqual(self.delays, [.25])

    def test_repeated_completion_for_same_attempt_and_claim_is_idempotent(self):
        self.publish(finished_at=1100.0)
        before = self.current()
        with patch.object(self.store, 'save', wraps=self.store.save) as save, \
                patch.object(self.store, 'event', wraps=self.store.event) as event:
            self.publish(finished_at=1200.0)
        save.assert_not_called()
        event.assert_not_called()
        self.assertEqual(self.current(), before)
        self.assert_completed_once()

    def test_changed_attempt_or_claim_cannot_complete_the_original_job(self):
        for field in ('attempt_id', 'claim'):
            with self.subTest(field=field):
                job = self.make_job()
                with self.store.transaction() as con:
                    current = self.store.get(con, job['id'])
                    current[field] = 'replacement'
                    self.store.save(con, current)
                with self.assertRaises(OwnershipContradiction):
                    self.publish(job)
                self.assertEqual(self.current(job)['status'], 'Run')
                self.assertFalse(any(message.startswith('Done:') for message in self.messages(job)))
        self.assertEqual(self.delays, [])

    def test_conflicting_terminal_or_unknown_state_is_preserved(self):
        for status in ('Fail', 'Unknown'):
            with self.subTest(status=status):
                job = self.make_job()
                with self.store.transaction() as con:
                    current = self.store.get(con, job['id'])
                    current.update(status=status, exit_code=7, reason='Contradictory evidence')
                    self.store.save(con, current)
                with self.assertRaises(OwnershipContradiction):
                    self.publish(job)
                observed = self.current(job)
                self.assertEqual((observed['status'], observed['exit_code'], observed['reason']),
                                 (status, 7, 'Contradictory evidence'))
        self.assertEqual(self.delays, [])

    def test_completion_pending_refuses_controls_with_specific_reason(self):
        completion = self.completion()
        completion.report('final absence observation')
        pending = self.current()
        self.assertEqual(pending['status'], 'Run')
        self.assertIn('Completion pending', pending['reason'])
        with self.assertRaisesRegex(JobsError, 'Completion pending'):
            self.store.change(self.job['id'], 'cancel', confirm=True)
        self.assertEqual(self.current(), pending)
        self.leader.stdin.write.assert_not_called()

    def test_timing_is_frozen_before_unreadable_final_absence_and_queue_retries(self):
        value = suspension.initial_state('local-command-v1')
        value.update(clock=CLOCK, clock_started=10.0, intervals=[{'start': 20.0, 'end': 30.0}])
        with self.store.transaction() as con:
            self.job = self.store.get(con, self.job['id'])
            self.job['suspension'] = value
            self.store.save(con, self.job)
        completion = self.completion()
        completion.release(self.receipt, self.evidence)
        self.authority.wait.side_effect = RetryBudgetExceeded('Post-RELEASE code rechecked a live anchor')
        self.authority.read_domain.side_effect = RetryBudgetExceeded('Post-RELEASE code entered a live hold')
        original_control = self.store.control
        publication_failed = []
        def read(con):
            if not publication_failed:
                publication_failed.append(True)
                raise sqlite3.OperationalError('publication read temporarily unavailable')
            return original_control(con)
        with patch.object(runtime, 'group_exists', side_effect=[
                PermissionError(errno.EPERM, 'absence temporarily unreadable'), False]), \
                patch.object(self.store, 'control', side_effect=read):
            completion.finish('Done', 0, 'Established completion')
        result = self.assert_completed_once()
        self.assertGreater(self.clock, 100.0)
        self.assertEqual(result['finished_at'], 1100.0)
        self.assertEqual(result['suspension']['clock_finished'], 100.0)
        self.assertEqual(result['suspension']['unsuspended_elapsed'], 80.0)
        self.leader.stdin.write.assert_called_once_with(b'RELEASE\n')
        self.leader.stdin.flush.assert_called_once_with()

    def test_receipt_retry_after_release_does_not_replay_release(self):
        original = runtime._receipt
        reads = []
        def read(path):
            reads.append(1)
            if len(reads) == 2:
                raise OSError(errno.EAGAIN, 'receipt temporarily unreadable after RELEASE')
            return original(path)
        completion = self.completion()
        with patch.object(runtime, '_receipt', side_effect=read):
            completion.release(self.receipt, self.evidence)
        completion.finish('Done', 0, 'Established completion')
        self.assertEqual(len(reads), 3)
        self.assert_completed_once()
        self.leader.stdin.write.assert_called_once_with(b'RELEASE\n')
        self.assertEqual(self.delays, [.25])

    def test_unreadable_first_completion_clock_keeps_duration_unverifiable(self):
        value = suspension.initial_state('local-command-v1')
        value.update(clock=CLOCK, clock_started=10.0, intervals=[])
        with self.store.transaction() as con:
            self.job = self.store.get(con, self.job['id'])
            self.job['suspension'] = value
            self.store.save(con, self.job)
        completion = self.completion()
        with patch.object(suspension_protocol, 'now_clock', side_effect=[
                OSError(errno.EAGAIN, 'completion clock unavailable'), 130.0]) as clock:
            completion.freeze_timing()
            self.clock += 30.0
            completion.release(self.receipt, self.evidence)
            completion.finish('Done', 0, 'Established completion')
        current = self.assert_completed_once()
        self.assertEqual(clock.call_count, 1)
        self.assertIs(current['suspension']['accounting_uncertain'], True)
        self.assertIsNone(current['suspension']['unsuspended_elapsed'])
        self.assertNotIn('clock_finished', current['suspension'])
        self.assertIn('boundary', current['suspension']['reason'])
        self.assertEqual(current['finished_at'], 1100.0)
        self.leader.stdin.write.assert_called_once_with(b'RELEASE\n')

    def test_unreadable_completion_wall_time_stays_null_and_elapsed_never_grows(self):
        completion = self.completion()
        original = runtime.time.time
        calls = []
        def wall_time():
            calls.append(1)
            if len(calls) == 1:
                raise OSError(errno.EAGAIN, 'completion timestamp unavailable')
            return original()
        with patch.object(runtime.time, 'time', side_effect=wall_time):
            completion.freeze_timing()
        self.clock += 30.0
        completion.release(self.receipt, self.evidence)
        completion.finish('Done', 0, 'Established completion')
        current = self.assert_completed_once()
        self.assertIsNone(current['finished_at'])
        for wall_now in (1200.0, 1800.0):
            result = next(job for job in self.store.snapshot(now=wall_now)['jobs']
                          if job['id'] == self.job['id'])
            self.assertIsNone(result['elapsed'])
        self.assertTrue(any('completion timestamp unavailable' in message for message in self.messages()))
        self.leader.stdin.write.assert_called_once_with(b'RELEASE\n')

    def test_completion_diagnostic_wait_requires_fresh_drainage_before_release(self):
        completion = self.completion()
        freeze = completion.freeze_timing
        def boundary():
            if not getattr(completion, '_timing_frozen', False):
                self.authority.generation += 1
                self.advance(.25)
            freeze()
        with patch.object(completion, 'freeze_timing', side_effect=boundary), \
                patch.object(completion, '_drained', return_value=True) as drained:
            completion.release(self.receipt, self.evidence)
        self.assertEqual(drained.call_count, 2)
        self.leader.stdin.write.assert_called_once_with(b'RELEASE\n')

    def test_abandoning_preparation_clears_only_its_hold_and_keeps_new_cancel(self):
        completion = self.completion()
        completion.report('release preparation')
        with self.store.transaction() as con:
            current = self.store.get(con, self.job['id'])
            current.update(status='Cancelling', cancel_requested=True, reason='New cancellation accepted')
            current['ownership_holds']['controller'] = {'check': 'worker.lock', 'since_clock': {
                'boot_session_id': BOOT, 'monotonic': self.clock}}
            self.store.save(con, current)
        completion.abandon_preparation()
        current = self.current()
        self.assertEqual((current['status'], current['cancel_requested'], current['reason']),
                         ('Cancelling', True, 'New cancellation accepted'))
        self.assertEqual(set(current['ownership_holds']), {'controller'})
        self.assertFalse(self.authority.completing)
        self.leader.stdin.write.assert_not_called()
        self.leader.stdin.close.assert_not_called()

    def test_release_cannot_be_abandoned_back_into_signal_supervision(self):
        completion = self.completion()
        completion.release(self.receipt, self.evidence)
        with self.assertRaises(OwnershipContradiction):
            completion.abandon_preparation()
        self.assertTrue(self.authority.completing)
        self.leader.stdin.write.assert_called_once_with(b'RELEASE\n')
        self.leader.stdin.close.assert_not_called()

    def test_post_release_reap_and_absence_retry_without_live_anchor_authority(self):
        completion = self.completion()
        completion.release(self.receipt, self.evidence)
        self.authority.wait.side_effect = RetryBudgetExceeded('Released anchor cannot authorize completion')
        self.authority.read_domain.side_effect = RetryBudgetExceeded('Released anchor cannot own a held read')
        self.leader.wait.side_effect = [PermissionError(errno.EPERM, 'wait temporarily unreadable'), 0]
        with patch.object(runtime, 'group_exists', side_effect=[
                OSError(errno.EAGAIN, 'absence temporarily unreadable'), False]):
            completion.finish('Done', 0, 'Established completion')
        self.assert_completed_once()
        self.leader.stdin.write.assert_called_once_with(b'RELEASE\n')
        self.assertEqual(self.delays, [.25, .25])

    def test_persistent_completion_attention_is_coalesced_without_unknown(self):
        completion = self.completion()
        completion.report('final absence observation')
        self.clock += 61.0
        completion.report('final absence observation')
        completion.report('final absence observation')
        current = self.current()
        self.assertEqual(current['status'], 'Run')
        self.assertTrue(current['ownership_holds']['completion']['attention'])
        self.assertEqual(sum('attention required' in message for message in self.messages()), 1)
        self.assertFalse(any('Unknown' in message for message in self.messages()))
        self.leader.stdin.close.assert_not_called()
        self.leader.stdin.write.assert_not_called()

    def test_unadmitted_exit_and_absence_retry_without_returning_to_anchor_authority(self):
        refusal = self.refusal()
        self.authority.wait.side_effect = RetryBudgetExceeded('Closed no-GO anchor cannot authorize a retry')
        self.authority.read_domain.side_effect = RetryBudgetExceeded('No-GO refusal has its own read driver')
        def closed():
            # The marker is committed before EOF; this also checks no writer
            # transaction remains locked while closure/absence is observed.
            with self.store.transaction(create=False, wait=lambda _: self.fail('Refusal retained a queue lock')) as con:
                current = self.store.get(con, self.job['id'])
            self.assertIn('admission', current['ownership_holds'])
            self.assertEqual(current['status'], 'Starting')
        self.leader.stdin.close.side_effect = closed
        self.leader.wait.side_effect = [PermissionError(errno.EPERM, 'wait unreadable'), 125]
        with patch.object(runtime, 'group_exists', side_effect=[
                OSError(errno.EAGAIN, 'absence unreadable'), False]):
            refusal.finish()
        current = self.current()
        self.assertEqual(current['status'], 'Queue')
        self.assertIsNone(current['worker'])
        self.assertIsNone(current['group'])
        self.assertIsNone(current['claim'])
        self.assertNotIn('ownership_holds', current)
        self.leader.stdin.close.assert_called_once_with()
        self.leader.stdin.write.assert_not_called()
        self.assertFalse(any('Unknown' in message for message in self.messages()))
        self.assertEqual(self.delays, [.25, .25])

    def test_unadmitted_helper_self_exit_after_two_seconds_is_never_signalled(self):
        refusal = self.refusal()
        started = self.clock

        def wait_for_exit(*, timeout):
            remaining = max(0.0, 2.0 - (self.clock - started))
            self.clock += min(timeout, remaining)
            if self.clock - started < 2.0:
                raise runtime.subprocess.TimeoutExpired('unstarted helper', timeout)
            return 0

        self.leader.wait.side_effect = wait_for_exit
        with patch.object(runtime, 'signal_session') as session_signal, \
                patch.object(runtime.os, 'killpg') as group_signal:
            refusal.finish()
        self.assertGreaterEqual(self.clock - started, 2.0)
        self.assertLessEqual(self.clock - started, 3.0)
        self.assertGreater(self.leader.wait.call_count, 1)
        self.assertTrue(all(call.kwargs == {'timeout': .25} for call in self.leader.wait.call_args_list))
        self.assertEqual(self.current()['status'], 'Queue')
        self.assertNotIn('ownership_holds', self.current())
        self.assertFalse(any('Unknown' in message for message in self.messages()))
        self.leader.stdin.close.assert_called_once_with()
        self.leader.stdin.write.assert_not_called()
        self.leader.send_signal.assert_not_called()
        self.leader.terminate.assert_not_called()
        self.leader.kill.assert_not_called()
        session_signal.assert_not_called()
        group_signal.assert_not_called()

    def test_unadmitted_persistent_helper_retains_signal_free_grace_beyond_five_seconds(self):
        refusal = self.refusal()
        started = self.clock

        def still_running(*, timeout):
            self.clock += timeout
            raise runtime.subprocess.TimeoutExpired('unstarted helper', timeout)

        def bounded_observation(delay):
            self.advance(delay)
            if self.clock - started > 5.0:
                raise RetryBudgetExceeded('Observed beyond the base five-second wait')

        self.leader.wait.side_effect = still_running
        self.authority.queue_wait = bounded_observation
        with patch.object(runtime, 'signal_session') as session_signal, \
                patch.object(runtime.os, 'killpg') as group_signal:
            with self.assertRaisesRegex(RetryBudgetExceeded, 'base five-second wait'):
                refusal.finish()
            pending = self.current()
            self.assertGreater(self.clock - started, 5.0)
            self.assertEqual((pending['status'], pending['claim']), ('Starting', self.job['claim']))
            self.assertIn('admission', pending['ownership_holds'])
            self.assertIsNone(pending['finished_at'])
            self.assertFalse(any('Unknown' in message for message in self.messages()))
            self.leader.stdin.close.assert_called_once_with()
            self.leader.stdin.write.assert_not_called()
            self.leader.send_signal.assert_not_called()
            self.leader.terminate.assert_not_called()
            self.leader.kill.assert_not_called()
            session_signal.assert_not_called()
            group_signal.assert_not_called()

            # An eventual EOF exit completes the same operation, without
            # closing again, replaying GO or retaining an abandoned hold.
            self.leader.wait.side_effect = None
            refusal.finish()
        self.assertEqual(self.current()['status'], 'Queue')
        self.assertNotIn('ownership_holds', self.current())
        self.leader.stdin.close.assert_called_once_with()
        self.leader.stdin.write.assert_not_called()
        session_signal.assert_not_called()
        group_signal.assert_not_called()

    def test_cancellation_during_unadmitted_absence_retry_is_preserved(self):
        refusal = self.refusal()
        probes = []
        def absence(pgid):
            probes.append(1)
            if len(probes) == 1:
                self.store.change(self.job['id'], 'cancel', confirm=True)
                raise PermissionError(errno.EPERM, 'absence temporarily unreadable')
            return False
        with patch.object(runtime, 'group_exists', side_effect=absence):
            refusal.finish()
        current = self.current()
        self.assertEqual(current['status'], 'Cancelled')
        self.assertTrue(current['cancel_requested'])
        self.assertEqual(current['reason'], 'Cancelled before payload admission')
        self.assertIsNotNone(current['finished_at'])
        self.assertNotIn('ownership_holds', current)
        self.leader.stdin.write.assert_not_called()

    def test_unadmitted_ambiguous_publication_does_not_reorder_twice(self):
        waiting = self.store.add(argv=['/bin/true'], cwd=self.root, name='waiting', cpus=1)
        refusal = self.refusal(blocker='Preparation no longer valid')
        original = self.store._commit
        lost = []
        def commit(con, wait):
            original(con, wait)
            current = self.store.get(con, self.job['id'])
            if current['claim'] is None and not lost:
                lost.append(True)
                raise sqlite3.OperationalError('Refusal commit acknowledged late')
        with patch.object(self.store, '_commit', side_effect=commit):
            refusal.finish()
        first = self.current()
        queued = self.current(waiting)
        self.assertEqual((first['status'], first['order'], queued['order']), ('Hold', 1, 2))
        self.assertEqual(first['reason'], 'Preparation no longer valid')
        refusal.finish()
        self.assertEqual(self.current(), first)
        self.assertEqual(self.current(waiting), queued)
        self.assertEqual(lost, [True])
        self.leader.stdin.close.assert_called_once_with()
        self.leader.stdin.write.assert_not_called()

    def test_unadmitted_refusal_keeps_changed_claim_and_independent_unknown(self):
        for change in ({'claim': 'new-owner'}, {'status': 'Unknown', 'reason': 'Independent contradiction'}):
            with self.subTest(change=change):
                self.job = self.make_job()
                self.authority.claim = self.job['claim']
                refusal = self.refusal()
                with self.store.transaction() as con:
                    current = self.store.get(con, self.job['id'])
                    current.update(change)
                    self.store.save(con, current)
                before = self.current()
                refusal.finish()
                self.assertEqual(self.current(), before)
        self.leader.stdin.write.assert_not_called()

    def test_unadmitted_channel_cleanup_read_fault_keeps_refusal_driver(self):
        coordinator = SimpleNamespace(discard_before_go=Mock(side_effect=[
            OwnershipUnavailable('channel', 'cleanup read unavailable'), None]))
        refusal = self.refusal(coordinator=coordinator)
        refusal.finish()
        self.assertEqual(coordinator.discard_before_go.call_count, 2)
        self.assertEqual(self.current()['status'], 'Queue')
        self.leader.stdin.close.assert_called_once_with()
        self.leader.stdin.write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
