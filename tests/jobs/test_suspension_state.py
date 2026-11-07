"""Durable control/fault contracts; every process observation and signal is mocked."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import ExitStack, contextmanager
import errno
import json
from pathlib import Path
import sqlite3
from threading import Event
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from cmw.jobs import suspension as control
from cmw.jobs.store import Store, JobsError
from cmw.jobs.ownership_hold import WorkerAuthority
from cmw.jobs.suspension_protocol import RunnerControl


CLOCK = {'kind': 'fictional-continuous', 'boot': 100}
MEMBER = {'pid': 100001, 'birth': 101, 'host': 'fixture', 'boot': 100}
OTHER = {**MEMBER, 'pid': 100002}
WORKER = {**MEMBER, 'pid': 100003}
ANCHOR = {**MEMBER, 'pid': 100004}
RUNNER = {**MEMBER, 'pid': 100005}


REAL_SEND = control._send


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw suspension state ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.job = self.store.add(argv=['/unexecuted/fixture'], cwd=self.root,
                                  name='Fictional state-control attempt', cpus=2, memory_gib=1, hold=True)
        value = control.initial_state('local-command-v1')
        value.update(clock=CLOCK, evidence={'checked_clock': 100, 'computational': [MEMBER]},
                     capability={'protocol': 1, 'deadline': {'known': True}, 'runner': RUNNER,
                                 'state_identity': control._file_identity(self.store.path),
                                 'storage': [{'path': str(self.root), **control._file_identity(self.root)}]})
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job.update(status='Run', order=None, worker=WORKER, group=ANCHOR,
                       claim='fixture-claim', worker_protocol=3, suspension=value,
                       started_at=100, claimed_at=100)
            self.store.save(con, job)
            metadata = self.store.control(con)
            metadata.update(schema=3, dispatch=True)
            self.store.set_control(con, metadata)
        for target, replacement in (
                ('cmw.jobs.suspension._alive_authority', lambda *args: True),
                ('cmw.jobs.suspension.owner_alive', lambda *args: True),
                ('cmw.jobs.suspension.observe_owner', lambda record: SimpleNamespace(
                    status=(control.OwnershipStatus.VERIFIED if control.owner_alive(record)
                            else control.OwnershipStatus.GONE), deciding_check='pid')),
                ('cmw.jobs.suspension_protocol.clock_descriptor', lambda: CLOCK),
                ('cmw.jobs.suspension_protocol.now_clock', lambda: 100.0)):
            mocked = patch(target, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        signal_patch = patch('cmw.jobs.suspension._send', side_effect=AssertionError('Unexpected fixture signal'))
        self.send = signal_patch.start()
        self.addCleanup(signal_patch.stop)

    def current(self):
        with self.store.transaction(create=False) as con:
            return self.store.get(con, self.job['id'])

    def change(self, **changes):
        with self.store.transaction(create=False) as con:
            job = self.store.get(con, self.job['id'])
            job.update(changes)
            self.store.save(con, job)
        return self.current()

    def phase(self, phase, **values):
        record = deepcopy(self.current()['suspension'])
        record.update(phase=phase, **values)
        return self.change(suspension=record)

    def coordinator(self):
        coordinator = object.__new__(control.Coordinator)
        job = self.current()
        coordinator.store, coordinator.job_id, coordinator.claim = self.store, job['id'], job['claim']
        coordinator.anchor = ANCHOR
        coordinator.directory = self.store.root/'attempts'/job['attempt_id']/'suspension'
        coordinator.context = {'worker': WORKER, 'clock': CLOCK,
                               'state_identity': control._file_identity(self.store.path)}
        coordinator.registration = {'runner': RUNNER, 'root': MEMBER, 'strategy': 'direct-tree',
                                    'storage': job['suspension']['capability']['storage']}
        coordinator.channel = Mock()
        coordinator.channel.read.return_value = None
        coordinator.last_check, coordinator.started, coordinator.pending = 0, 100, None
        coordinator.completion_pending_since = None
        coordinator.domain_pending_since = None
        coordinator._validate = Mock(return_value=control.Domain((MEMBER,), (ANCHOR, RUNNER), ()))
        return coordinator

    def test_hold_rejected_update_is_retained_until_recovery(self):
        coordinator = self.coordinator()
        expected = self.current()
        self.change(ownership_holds={'domain': {'check': 'descendants'}})
        with self.assertRaises(control.RequestSuperseded):
            coordinator.update(expected, reason='Awaiting genuine completion', evidence=None)
        self.assertEqual(len(coordinator.pending_updates), 1)
        self.change(ownership_holds={})
        coordinator._tick = Mock()
        coordinator.tick(self.current())
        self.assertEqual(coordinator.pending_updates, [])
        self.assertEqual(self.current()['suspension']['reason'], 'Awaiting genuine completion')
        self.assertEqual(self.current()['status'], 'Run')
        self.send.assert_not_called()

    def test_completion_pending_refuses_readiness_and_requests_explicitly(self):
        self.change(ownership_holds={'completion': {'check': 'final absence', 'since_monotonic': 1}})
        before = self.current()
        for action in ('suspend', 'continue'):
            with self.subTest(action=action):
                preview = control.readiness(self.store, self.job['id'], action)
                self.assertFalse(preview['ready'])
                self.assertTrue(any(reason.startswith('Completion pending;') for reason in preview['reasons']))
                with self.assertRaisesRegex(JobsError, 'Completion pending;'):
                    control.submit(self.store, self.job['id'], action)
        self.assertEqual(self.current(), before)
        self.send.assert_not_called()

    def test_unobserved_domain_receipt_is_pending_before_any_control_or_registration(self):
        job = self.current()
        coordinator = self.coordinator()
        coordinator.registration = None
        coordinator.last_domain = None
        coordinator._registration = Mock(side_effect=AssertionError('retrospective registration'))
        reason = 'process domain never observed; receipt present; manual verification required'
        for action in (lambda: coordinator.tick(job, receipt_present=True), coordinator.ready_to_release):
            with self.subTest(action=action), self.assertRaises(control.CompletionDomainUnobserved) as caught:
                action()
            self.assertEqual(str(caught.exception), reason)
            self.assertEqual(caught.exception.deciding_check, 'completion.domain_never_observed')
            self.assertNotIsInstance(caught.exception, control.OwnershipContradiction)
        coordinator._registration.assert_not_called()
        coordinator.channel.write.assert_not_called()
        self.assertEqual(self.current(), job)
        self.send.assert_not_called()

    def test_runner_exit_before_first_domain_and_receipt_stays_pending(self):
        job = self.current()
        coordinator = self.coordinator()
        coordinator.registration = None
        coordinator.last_domain = None
        coordinator.channel.read.return_value = {'runner': RUNNER, 'root': MEMBER}
        with patch.object(control, 'lock_held', return_value=False), \
                patch.object(control, '_check_runner', side_effect=control.DomainPending(
                    'Registered runner and computation exited during observation')):
            coordinator.tick(job)
        value = self.current()['suspension']
        self.assertEqual(value['phase'], 'Run')
        self.assertFalse(value.get('accounting_uncertain', False))
        self.assertIsNone(coordinator.last_domain)
        self.assertIsNone(coordinator.registration)
        coordinator.channel.write.assert_not_called()
        self.send.assert_not_called()

    def test_missing_runner_lock_with_live_runner_remains_a_contradiction(self):
        coordinator = self.coordinator()
        coordinator.registration = None
        coordinator.channel.read.return_value = {'runner': RUNNER, 'root': MEMBER}
        with patch.object(control, 'lock_held', return_value=False), \
                patch.object(control, '_check_runner', return_value=None), \
                self.assertRaisesRegex(control.OwnershipContradiction, 'lock is not owned'):
            coordinator._registration(self.current())
        self.send.assert_not_called()

    def test_constructor_read_retry_does_not_recreate_directory_or_context(self):
        job = self.current()
        directory = self.store.root/'attempts'/job['attempt_id']/'suspension'
        directory.parent.mkdir(parents=True)
        mkdir, native_open = Path.mkdir, control.os.open
        for error in (errno.EAGAIN, errno.EPERM):
            with self.subTest(errno=error):
                creations, opened = [], []
                def create(path, *args, **options):
                    if path == directory:
                        creations.append(path)
                    return mkdir(path, *args, **options)
                def open_directory(path, *args, **options):
                    if path == directory:
                        opened.append(path)
                        if len(opened) == 1:
                            raise OSError(error, 'one constructor channel read')
                    return native_open(path, *args, **options)
                with patch.object(Path, 'mkdir', new=create), \
                        patch.object(control.os, 'open', side_effect=open_directory), \
                        patch.object(control.os, 'replace', wraps=control.os.replace) as publish, \
                        patch.object(control.time, 'sleep') as sleep:
                    coordinator = control.Coordinator(self.store, job, ANCHOR)
                self.assertEqual(creations, [directory])
                self.assertEqual(len(opened), 2)
                publish.assert_called_once()
                sleep.assert_called_once_with(.25)
                self.assertEqual(coordinator.channel.read('context.json'), coordinator.context)
                coordinator.discard_before_go()
        self.send.assert_not_called()

    def test_constructor_never_reuses_existing_channel_directory(self):
        job = self.current()
        directory = self.store.root/'attempts'/job['attempt_id']/'suspension'
        directory.mkdir(mode=0o700, parents=True)
        marker = directory/'foreign'
        marker.write_text('must remain unchanged')
        with self.assertRaises(FileExistsError):
            control.Coordinator(self.store, job, ANCHOR)
        self.assertEqual(marker.read_text(), 'must remain unchanged')
        self.assertFalse((directory/'context.json').exists())

    def test_discard_before_go_resumes_partial_removal_and_refuses_replacement(self):
        job = self.current()
        directory = self.store.root/'attempts'/job['attempt_id']/'suspension'
        directory.parent.mkdir(parents=True)
        coordinator = control.Coordinator(self.store, job, ANCHOR)
        native_rmdir = Path.rmdir
        attempts = []
        def remove(path):
            if path == directory:
                attempts.append(path)
                if len(attempts) == 1:
                    raise OSError(errno.EAGAIN, 'one directory-removal refusal')
            return native_rmdir(path)
        with patch.object(Path, 'rmdir', new=remove), \
                patch.object(control.os, 'unlink', wraps=control.os.unlink) as unlink:
            with self.assertRaises(OSError):
                coordinator.discard_before_go()
            self.assertTrue(directory.is_dir())
            self.assertFalse((directory/'context.json').exists())
            coordinator.discard_before_go()
            coordinator.discard_before_go()
        self.assertFalse(directory.exists())
        self.assertEqual([call.args[0] for call in unlink.call_args_list], ['context.json'])
        directory.mkdir(mode=0o700)
        with self.assertRaisesRegex(control.OwnershipContradiction, 'replaced'):
            coordinator.discard_before_go()
        self.assertTrue(directory.is_dir())

    def test_discard_before_go_refuses_unrecorded_absence(self):
        job = self.current()
        directory = self.store.root/'attempts'/job['attempt_id']/'suspension'
        directory.parent.mkdir(parents=True)
        coordinator = control.Coordinator(self.store, job, ANCHOR)
        (directory/'context.json').unlink()
        directory.rmdir()
        with self.assertRaisesRegex(control.OwnershipContradiction, 'disappeared'):
            coordinator.discard_before_go()

    def test_hold_rejected_update_cannot_overwrite_new_cancellation(self):
        coordinator = self.coordinator()
        expected = self.current()
        self.change(ownership_holds={'domain': {'check': 'descendants'}})
        with self.assertRaises(control.RequestSuperseded):
            coordinator.update(expected, reason='old request', phase='Run')
        latest = self.change(ownership_holds={}, status='Cancelling', cancel_requested=True)
        coordinator._tick = Mock()
        coordinator.tick(latest)
        self.assertEqual(coordinator.pending_updates, [])
        self.assertEqual(self.current()['status'], 'Cancelling')
        self.assertNotEqual(self.current()['suspension']['reason'], 'old request')
        self.send.assert_not_called()

    def test_revalidated_exit_keeps_delivered_signal_history_without_stale_evidence(self):
        coordinator = self.coordinator()
        job = self.current()
        history = [{'identity': MEMBER, 'signal': int(control.signal.SIGSTOP), 'delivered': True}]
        coordinator.pending_updates = [(deepcopy(job), {
            'evidence': {'computational': [MEMBER], 'signals': history}, 'signal_history': history})]
        coordinator._validate.side_effect = control.DomainPending('Original member has exited')
        coordinator._tick = Mock()
        coordinator.tick(job)
        current = self.current()['suspension']
        self.assertEqual(current['signal_history'], history)
        self.assertEqual(current['evidence'], job['suspension']['evidence'])
        self.assertEqual(coordinator.pending_updates, [])
        self.send.assert_not_called()

    def test_command_publication_read_failure_keeps_the_pending_command(self):
        coordinator = self.coordinator()
        job = self.phase('Suspending', revision=1, request={'id': 'request', 'action': 'suspend'})
        published = []
        attempts = []
        def publish(name, value, **options):
            attempts.append(name)
            if len(attempts) == 1:
                raise OSError(errno.EAGAIN, 'one metadata read failed before publication')
            published.append(value)
        coordinator.channel.write.side_effect = publish
        with self.assertRaises(OSError):
            coordinator.command(job, 'prepare_suspend')
        self.assertIsNotNone(coordinator.pending_command)
        coordinator._tick = Mock()
        coordinator.tick(self.current())
        self.assertIsNone(coordinator.pending_command)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]['operation'], 'prepare_suspend')
        self.send.assert_not_called()

    def test_command_already_published_is_not_written_again_after_lost_result(self):
        coordinator = self.coordinator()
        job = self.phase('Suspending', revision=1, request={'id': 'request', 'action': 'suspend'})
        def published(name, value, **options):
            coordinator.channel.read.return_value = value
            raise OSError(errno.EAGAIN, 'read failed after publication')
        coordinator.channel.write.side_effect = published
        with self.assertRaises(OSError):
            coordinator.command(job, 'prepare_suspend')
        coordinator._tick = Mock()
        coordinator.tick(self.current())
        self.assertIsNone(coordinator.pending_command)
        self.assertEqual(coordinator.channel.write.call_count, 1)
        self.send.assert_not_called()

    def test_signal_is_not_repeated_when_the_same_phase_is_revisited(self):
        coordinator = self.coordinator()
        job = self.phase('Resuming', revision=2, request={'id': 'request', 'action': 'continue'})
        with patch.object(control, '_send') as send:
            coordinator._deliver(job, MEMBER, control.signal.SIGCONT)
            coordinator._deliver(job, MEMBER, control.signal.SIGCONT)
        send.assert_called_once_with(MEMBER, ANCHOR, control.signal.SIGCONT)

    def test_pending_suspended_command_resumes_at_its_ack_stage(self):
        coordinator = self.coordinator()
        job = self.phase('Suspending', revision=2, request={'id': 'request', 'action': 'suspend'})
        coordinator.pending = {'id': 'request', 'stage': 'prepare_suspend', 'started': 0}
        coordinator.ack = Mock(return_value={'status': 'prepared'})
        coordinator._suspend = Mock(return_value=(coordinator._validate.return_value, 100))
        coordinator.channel.write.side_effect = [OSError(errno.EAGAIN, 'one publication read'), None]
        with patch.object(control, '_stopped', return_value=True), \
                patch.object(control.time, 'monotonic', return_value=0):
            coordinator.tick(job)
            self.assertEqual(coordinator.pending['stage'], 'suspended')
            coordinator.ack.return_value = {'status': 'paused'}
            coordinator.tick(self.current())
        self.assertEqual(self.current()['suspension']['phase'], 'Suspended')
        coordinator._suspend.assert_called_once()
        self.assertFalse(self.current()['suspension'].get('accounting_uncertain', False))
        self.send.assert_not_called()

    def test_post_release_observation_has_no_live_anchor_dependency_or_timeout(self):
        coordinator = self.coordinator()
        coordinator.current = Mock(return_value=self.current())
        coordinator.authority = Mock()
        coordinator.observed = {MEMBER['pid']: MEMBER}
        coordinator.observed_groups = {MEMBER['pid']}
        with patch.object(control, 'identity_for', side_effect=[
                OSError(errno.EAGAIN, 'one identity read failed'),
                control.psutil.NoSuchProcess(MEMBER['pid'])]), \
                patch.object(control, 'group_exists', return_value=False), \
                patch.object(control.time, 'sleep') as sleep:
            with self.assertRaises(control.OwnershipUnavailable):
                coordinator.verify_released()
            self.assertTrue(coordinator.verify_released())
        coordinator.authority.wait.assert_not_called()
        coordinator.authority.read_domain.assert_not_called()
        sleep.assert_not_called()
        self.send.assert_not_called()

    def test_cancel_during_target_read_supersedes_continue_before_signal(self):
        job = self.phase('Resuming', revision=3, request={'id': 'resume', 'action': 'continue'})
        coordinator = self.coordinator()
        coordinator.authority = WorkerAuthority(self.store, job, ANCHOR, Mock())
        coordinator.authority.wait = Mock(side_effect=coordinator.current)
        entered, released = Event(), Event()
        def observe(member, anchor):
            self.assertEqual((member, anchor), (MEMBER, ANCHOR))
            entered.set()
            self.assertTrue(released.wait(5), 'cancel did not complete during target observation')

        with patch.object(control, '_send', REAL_SEND), patch.object(control, '_check_identity', observe), \
                patch.object(control.os, 'kill') as kill, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(coordinator._deliver, job, MEMBER, control.signal.SIGCONT)
            try:
                self.assertTrue(entered.wait(5), 'target read did not start')
                self.store.change(job['id'], 'cancel', confirm=True)
            finally:
                released.set()
            with self.assertRaises(control.RequestSuperseded):
                future.result(timeout=5)
            kill.assert_not_called()
            cancelling = self.current()
            self.assertEqual(cancelling['status'], 'Cancelling')
            self.assertEqual(cancelling['suspension']['revision'], 3)
            coordinator.ack = Mock(return_value={'status': 'cancel_prepared'})
            self.assertTrue(coordinator.prepare_cancel(cancelling))
            prepared = self.current()
            self.assertEqual(prepared['suspension']['request']['action'], 'cancel')
            self.assertEqual(prepared['suspension']['revision'], 4)
            command = coordinator.channel.write.call_args.args[1]
            self.assertEqual((command['operation'], command['request_id']),
                             ('cancel', prepared['suspension']['request']['id']))
            kill.assert_not_called()

    def test_reused_target_while_waiting_for_queue_lock_never_receives_continue(self):
        job = self.phase('Resuming', revision=3, request={'id': 'resume', 'action': 'continue'})
        coordinator = self.coordinator()
        coordinator.authority = WorkerAuthority(self.store, job, ANCHOR, Mock())
        coordinator.authority.wait = Mock(return_value=job)
        acquiring, replaced = Event(), Event()
        gate = self.store.delivery_gate

        @contextmanager
        def transaction_start(**options):
            acquiring.set()
            with gate(**options) as con:
                yield con

        def observe(record):
            status = (control.OwnershipStatus.GONE if record == MEMBER and replaced.is_set()
                      else control.OwnershipStatus.VERIFIED)
            return SimpleNamespace(status=status, deciding_check='start_token')

        with patch.object(self.store, 'delivery_gate', transaction_start), \
                patch.object(control, '_send', REAL_SEND), patch.object(control, '_check_identity'), \
                patch.object(control, 'observe_owner', side_effect=observe), \
                patch.object(control.os, 'getsid', return_value=ANCHOR['pid']), \
                patch.object(control.os, 'getpgid', side_effect=lambda pid: pid), \
                patch.object(control.os, 'kill') as kill, ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.transaction(create=False):
                future = pool.submit(coordinator._deliver, job, MEMBER, control.signal.SIGCONT)
                self.assertTrue(acquiring.wait(5))
                replaced.set()
            with self.assertRaisesRegex(JobsError, 'Signal authority changed'):
                future.result(timeout=5)
            kill.assert_not_called()

    def test_busy_second_continue_gate_retries_only_that_target(self):
        intervals = [{'start': 90., 'end': 95.}]
        domain = control.Domain((MEMBER, OTHER), (ANCHOR, RUNNER), ())
        job = self.phase('Resuming', inhibited=True, revision=2,
                         request={'id': 'resume', 'action': 'continue'},
                         evidence=domain.evidence(), intervals=intervals)
        coordinator = self.coordinator()
        authority = coordinator.authority = WorkerAuthority(self.store, job, ANCHOR, Mock())
        authority.wait = Mock(return_value=job)
        coordinator.pending = {'id': 'resume', 'stage': 'prepare_continue', 'started': 100.}
        coordinator._validate.return_value = domain
        coordinator.ack = Mock(return_value={'intervals': intervals, 'suspended_seconds': 5.,
                                             'unsuspended_elapsed': 30.})
        locker = sqlite3.connect(self.store.path)
        self.addCleanup(locker.close)
        gate = self.store.delivery_gate
        sent, waits = [], []
        clock = [100.]

        @contextmanager
        def contend_at_second_target(**options):
            if len(sent) == 1 and not waits:
                locker.execute('BEGIN IMMEDIATE')
            with gate(**options) as con:
                yield con

        def release_writer(delay):
            self.assertTrue(locker.in_transaction)
            self.assertEqual(sent, [(OTHER['pid'], control.signal.SIGCONT)])
            waits.append(delay)
            clock[0] += 20.  # Longer than the entire transition deadline.
            locker.rollback()

        with patch.object(self.store, 'delivery_gate', contend_at_second_target), \
                patch.object(control, '_send', REAL_SEND), patch.object(control, '_check_identity'), \
                patch.object(control, '_stopped', side_effect=lambda m: (m['pid'], control.signal.SIGCONT) not in sent), \
                patch.object(control.os, 'getsid', return_value=ANCHOR['pid']), \
                patch.object(control.os, 'getpgid', side_effect=lambda pid: pid), \
                patch.object(control.os, 'kill', side_effect=lambda p, sig: sent.append((p, sig))), \
                patch.object(control.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(control.time, 'sleep', side_effect=release_writer):
            coordinator.tick(job)
            self.assertEqual(coordinator.pending['stage'], 'verify_continue')
            coordinator.tick(self.current())
            coordinator.tick(self.current())
        state = self.current()['suspension']
        self.assertEqual((state['phase'], state['intervals'], state['suspended_seconds']), ('Run', intervals, 5.))
        self.assertFalse(state.get('accounting_uncertain'))
        self.assertEqual(sent, [(p['pid'], control.signal.SIGCONT) for p in (OTHER, MEMBER)])
        self.assertEqual(len(waits), 1)
        self.assertEqual(authority.hold_seconds, 20.)

    def test_busy_evidence_commit_retains_phase_and_runs_observation_once(self):
        intervals = [{'start': 90., 'end': 95.}]
        job = self.phase('Run', revision=2, intervals=intervals, suspended_seconds=5.)
        coordinator = self.coordinator()
        authority = coordinator.authority = WorkerAuthority(self.store, job, ANCHOR, Mock())
        authority.wait = Mock(return_value=job)
        reader = sqlite3.connect(self.store.path)
        self.addCleanup(reader.close)
        evidence = {'computational': [MEMBER], 'checked_clock': 101.}

        def observe(domain, **kwargs):
            # A real SQLite reader allows the UPDATE but holds off its COMMIT.
            reader.execute('BEGIN')
            reader.execute('SELECT id FROM jobs').fetchall()
            return evidence

        def release_reader(delay):
            self.assertTrue(reader.in_transaction)
            reader.rollback()

        with patch.object(coordinator, '_evidence', side_effect=observe) as observation, \
                patch.object(authority, '_backoff', side_effect=release_reader) as wait:
            coordinator.tick(job)
        state = self.current()['suspension']
        self.assertEqual(state['evidence'], evidence)
        self.assertEqual((state['phase'], state['intervals'], state['suspended_seconds']), ('Run', intervals, 5.))
        self.assertFalse(state.get('accounting_uncertain'))
        observation.assert_called_once()
        wait.assert_called_once()
        self.send.assert_not_called()

    def test_denied_delivery_witness_releases_lock_before_hold_and_supersession(self):
        job = self.phase('Resuming', revision=3, request={'id': 'resume', 'action': 'continue'})
        coordinator = self.coordinator()
        authority = coordinator.authority = WorkerAuthority(self.store, job, ANCHOR, Mock())
        authority.wait = Mock(return_value=job)
        recovered = False

        def observe(record):
            status = (control.OwnershipStatus.UNVERIFIABLE if record == MEMBER and not recovered
                      else control.OwnershipStatus.VERIFIED)
            return SimpleNamespace(status=status, deciding_check='start_token')

        def recover(delay):
            nonlocal recovered
            self.assertEqual(self.current()['ownership_holds']['domain']['check'], 'start_token')
            self.store.change(job['id'], 'cancel', confirm=True)
            recovered = True

        with patch.object(control, '_send', REAL_SEND), patch.object(control, '_check_identity'), \
                patch.object(control, 'observe_owner', side_effect=observe), \
                patch.object(control.os, 'getsid', return_value=ANCHOR['pid']), \
                patch.object(control.os, 'getpgid', side_effect=lambda pid: pid), \
                patch.object(authority, '_backoff', side_effect=recover), patch.object(control.os, 'kill') as kill:
            with self.assertRaises(control.RequestSuperseded):
                coordinator._deliver(job, MEMBER, control.signal.SIGCONT)
        kill.assert_not_called()
        self.assertTrue(recovered)
        self.assertTrue(self.current()['cancel_requested'])

    def test_runner_deadline_during_target_check_prevents_continue(self):
        job = self.phase('Resuming', revision=3, request={'id': 'resume', 'action': 'continue'})
        coordinator = self.coordinator()
        coordinator.directory.mkdir(mode=0o700, parents=True)
        coordinator.context['directory_identity'] = control._file_identity(coordinator.directory)
        coordinator.channel = control.Channel(coordinator.directory, coordinator.context)
        coordinator.authority = WorkerAuthority(self.store, job, ANCHOR, Mock())
        coordinator.authority.wait = Mock(side_effect=coordinator.current)
        def expire(*args):
            coordinator.channel.write('deadline_expired.json', {**coordinator.context,
                'runner': RUNNER, 'reason': 'execution budget expired'})
        with patch.object(control, '_send', REAL_SEND), patch.object(control, '_check_identity', expire), \
                patch.object(control.os, 'kill') as kill:
            with self.assertRaises(control.RequestSuperseded):
                coordinator._deliver(job, MEMBER, control.signal.SIGCONT)
            coordinator.tick(job)
            kill.assert_not_called()
        current = self.current()
        self.assertEqual(current['status'], 'Cancelling')
        self.assertEqual(current['termination_requested']['reason'], 'execution budget expired')

    def test_cancel_during_command_file_write_prevents_publication(self):
        job = self.phase('Resuming', revision=3, request={'id': 'resume', 'action': 'continue'})
        coordinator = self.coordinator()
        coordinator.directory.mkdir(mode=0o700, parents=True)
        coordinator.context['directory_identity'] = control._file_identity(coordinator.directory)
        coordinator.channel = control.Channel(coordinator.directory, coordinator.context)
        with patch.object(control.os, 'fsync', side_effect=lambda fd: self.store.change(job['id'], 'cancel', confirm=True)):
            with self.assertRaises(control.RequestSuperseded):
                coordinator.command(job, 'run')
        self.assertEqual(list(coordinator.directory.iterdir()), [])
        self.assertEqual(self.current()['status'], 'Cancelling')

    def test_denied_root_probe_recovers_without_losing_controls_or_timing(self):
        from cmw.jobs import ownership_hold
        verified = SimpleNamespace(status=control.OwnershipStatus.VERIFIED, deciding_check='identity')
        denied = SimpleNamespace(status=control.OwnershipStatus.UNVERIFIABLE, deciding_check='start_token')
        for phase in ('Run', 'Resuming'):
            with self.subTest(phase=phase):
                intervals = [{'start': 80, 'end': 85}]
                job = self.phase(phase, revision=2, request={'id': 'continue', 'action': 'continue'},
                                 intervals=intervals, suspended_seconds=5, accounting_uncertain=False,
                                 evidence={'checked_clock': 100, 'computational': [MEMBER]})
                coordinator = self.coordinator()
                coordinator.authority = WorkerAuthority(self.store, job, ANCHOR, Mock())
                coordinator.pending = dict(id='continue', stage='verify_continue', started=100,
                                           domain=control.Domain((MEMBER,), (ANCHOR, RUNNER), ()))
                during_hold = []
                def inspect_hold(delay):
                    snapshot = self.current()
                    during_hold.append(snapshot['ownership_holds']['domain']['check'])
                    self.assertEqual(snapshot['suspension']['phase'], phase)
                    self.assertFalse(snapshot['suspension']['accounting_uncertain'])
                with patch.object(control, 'observe_owner', side_effect=[denied, verified, verified]) as observed, \
                        patch.object(ownership_hold, 'observe_owner', return_value=verified), \
                        patch.object(control, '_stopped', return_value=False), \
                        patch.object(control.time, 'sleep', side_effect=inspect_hold), \
                        patch.object(control.time, 'monotonic', return_value=100):
                    coordinator.tick(job)
                self.assertEqual(observed.call_count, 3)
                self.assertEqual(during_hold, ['start_token'])
                current = self.current()
                self.assertFalse(current.get('ownership_holds'))
                state = current['suspension']
                self.assertEqual((state['phase'], state['revision']), (phase, 2))
                self.assertEqual((state['intervals'], state['suspended_seconds']), (intervals, 5))
                self.assertFalse(state['accounting_uncertain'])
                if phase == 'Run':
                    self.assertTrue(control.readiness(self.store, job['id'], 'suspend')['ready'])
                else:
                    self.assertEqual(coordinator.channel.write.call_args.args[1]['operation'], 'run')
                self.send.assert_not_called()

    def test_confirmed_replaced_root_invalidates_suspension(self):
        job = self.phase('Run', accounting_uncertain=False)
        coordinator = self.coordinator()
        with patch.object(control, 'observe_owner', return_value=SimpleNamespace(
                status=control.OwnershipStatus.GONE, deciding_check='start_token')):
            coordinator.tick(job)
        state = self.current()['suspension']
        self.assertEqual(state['phase'], 'Unverified')
        self.assertTrue(state['accounting_uncertain'])
        self.assertIn('root identity changed', state['reason'])
        self.send.assert_not_called()

    def test_preview_never_creates_migrates_or_signals(self):
        missing = Store(self.root/'missing')
        with self.assertRaises(JobsError):
            control.readiness(missing, 'J1.1')
        self.assertFalse(missing.root.exists())
        self.change(suspension=None, worker_protocol=2)
        before = self.store.path.read_bytes()
        result = control.readiness(self.store, self.job['id'], 'suspend')
        self.assertFalse(result['supported'])
        self.assertFalse(result['ready'])
        self.assertIn('future job', ' '.join(result['reasons']))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.send.assert_not_called()

    def test_acceptance_retains_attempt_resources_dispatch_and_note(self):
        before = self.current()
        preview = control.readiness(self.store, self.job['id'], 'suspend')
        self.assertTrue(preview['ready'], preview)
        self.store.annotate(self.job['id'], 'Concurrent human note')
        result = control.submit(self.store, self.job['id'], 'suspend', reason='meeting [literal]',
                                expected_attempt_id=before['attempt_id'], expected_revision=0,
                                expected_phase='Run', expected_authority=preview['authority'])
        self.assertEqual((result['result'], result['phase']), ('ACCEPTED', 'Suspending'))
        after = self.current()
        for name in ('attempt_id', 'resources', 'argv', 'cwd', 'scheduling', 'worker', 'group'):
            self.assertEqual(after[name], before[name])
        self.assertEqual(after['status'], 'Run')
        self.assertEqual(after['note'], 'Concurrent human note')
        self.assertTrue(after['suspension']['inhibited'])
        self.assertTrue(self.store.snapshot()['controller']['dispatch'])
        self.send.assert_not_called()

    def test_concurrent_duplicate_requests_create_one_operation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: control.submit(self.store, self.job['id'], 'suspend'), range(2)))
        self.assertEqual(sorted(result['result'] for result in results), ['ACCEPTED', 'PENDING'])
        self.assertEqual(self.current()['suspension']['revision'], 1)
        with self.assertRaises(JobsError):
            control.submit(self.store, self.job['id'], 'continue')
        record = deepcopy(self.current()['suspension'])
        record.update(phase='Suspended', suspended_seconds=20)
        self.change(suspension=record)
        self.assertEqual(control.submit(self.store, self.job['id'], 'suspend')['result'], 'COMPLETED')
        self.assertEqual(self.current()['suspension']['revision'], 1)

    def test_stale_request_guards_and_literal_reason_validation(self):
        for arguments in ({'expected_attempt_id': 'foreign'}, {'expected_revision': 99},
                          {'expected_phase': 'Suspended'}, {'expected_authority': {'claim': 'foreign'}}):
            with self.subTest(arguments=arguments), self.assertRaises(JobsError):
                control.submit(self.store, self.job['id'], 'suspend', **arguments)
        for reason in ('bad\0reason', '\x1b[31m', 'x'*4097):
            with self.subTest(reason=reason[:10]), self.assertRaises(JobsError):
                control.submit(self.store, self.job['id'], 'suspend', reason=reason)
        self.assertEqual(self.current()['suspension']['revision'], 0)

    def test_terminal_and_cancel_races_dominate_submission(self):
        original = control.readiness
        for status in ('Done', 'Cancelling'):
            self.change(status='Run', cancel_requested=False)
            def race(*args, **kwargs):
                result = original(*args, **kwargs)
                self.change(status=status, cancel_requested=status == 'Cancelling')
                return result
            with patch.object(control, 'readiness', side_effect=race), self.assertRaises(JobsError):
                control.submit(self.store, self.job['id'], 'suspend')
            self.assertEqual(self.current()['status'], status)
            self.assertEqual(self.current()['suspension']['revision'], 0)

    def test_storage_replacement_refuses_without_repair(self):
        required = self.root/'attached-storage'
        required.mkdir()
        value = self.current()['suspension']
        value['capability']['storage'] = [{'path': str(required), **control._file_identity(required)}]
        self.change(suspension=value)
        required.rename(self.root/'original-storage')
        required.mkdir()
        result = control.readiness(self.store, self.job['id'])
        self.assertFalse(result['ready'])
        self.assertIn('replaced', ' '.join(result['reasons']))
        self.assertTrue((self.root/'original-storage').exists())

    def test_replaced_state_file_refuses_readiness_and_submission(self):
        self.assertTrue(control.readiness(self.store, self.job['id'])['ready'])
        original = control._file_identity(self.store.path)
        replacement = self.root/'replacement.sqlite3'
        replacement.write_bytes(self.store.path.read_bytes())
        replacement.replace(self.store.path)
        self.assertNotEqual(control._file_identity(self.store.path), original)
        before = self.store.path.read_bytes()
        result = control.readiness(self.store, self.job['id'])
        self.assertFalse(result['ready'])
        self.assertIn('state identity differs', ' '.join(result['reasons']))
        with self.assertRaisesRegex(JobsError, 'state identity differs'):
            control.submit(self.store, self.job['id'], 'suspend')
        self.assertEqual(self.store.path.read_bytes(), before)
        self.send.assert_not_called()

    def test_clock_discontinuity_and_stale_evidence_refuse(self):
        for clock, now in (({**CLOCK, 'boot': 200}, 100), (CLOCK, 99), (CLOCK, 104)):
            with patch('cmw.jobs.suspension_protocol.clock_descriptor', return_value=clock), \
                    patch('cmw.jobs.suspension_protocol.now_clock', return_value=now):
                self.assertFalse(control.readiness(self.store, self.job['id'])['ready'])

    def test_legacy_active_attempt_and_controller_block_migration(self):
        for active, controller in ((True, False), (False, True)):
            with self.store.transaction() as con:
                metadata = self.store.control(con)
                metadata.update(schema=2, runtime_protocol=2, owner=None)
                self.store.set_control(con, metadata)
                job = self.store.get(con, self.job['id'])
                job.update(status='Run' if active else 'Done', worker_protocol=2)
                self.store.save(con, job)
            with patch.object(control, 'lock_held', return_value=controller), \
                    patch.object(control, 'owner_alive', return_value=controller), \
                    self.assertRaises(JobsError):
                self.store.add(argv=['/unexecuted/fixture'], cwd=self.root, name='Refused enrollment',
                               suspension_backend='local-command-v1')
            self.assertEqual(self.store.snapshot()['schema'], 2)
            self.assertEqual(len(self.store.snapshot()['jobs']), 1)

    def test_worker_loss_reconciles_every_phase_without_continuation(self):
        from cmw.jobs.runtime import reconcile
        for phase in ('Run', 'Suspending', 'Suspended', 'Resuming', 'Unverified'):
            self.phase(phase, inhibited=True)
            self.change(status='Run')
            with patch('cmw.jobs.runtime.observe_owner', return_value=Mock(status='GONE', deciding_check='pid')), \
                    patch('cmw.jobs.runtime.lock_held', return_value=False):
                with self.store.transaction() as con:
                    reconcile(self.store, con, self.store.get(con, self.job['id']))
            job = self.current()
            self.assertEqual(job['status'], 'Unknown')
            self.assertEqual(job['suspension']['phase'], phase)
            self.assertTrue(job['suspension']['inhibited'])
        self.send.assert_not_called()

    def test_ack_update_preserves_notes_and_rejects_new_revision(self):
        coordinator = self.coordinator()
        before = self.current()
        self.store.annotate(self.job['id'], 'Changed while worker verifies')
        coordinator.update(before, reason='Worker update')
        self.assertEqual(self.current()['note'], 'Changed while worker verifies')
        self.phase('Suspending', revision=1)
        with self.assertRaises(JobsError):
            coordinator.update(before, phase='Suspended')
        self.assertEqual(self.current()['suspension']['phase'], 'Suspending')

    def test_new_request_supersedes_inflight_verification_and_invalidation(self):
        for invalidation in (False, True):
            with self.subTest(invalidation=invalidation):
                before = self.phase('Run', revision=0, request=None, inhibited=False,
                                    evidence={'checked_clock': 100, 'computational': [MEMBER]})
                coordinator = self.coordinator()
                accepted = []
                def verification():
                    accepted.append(control.submit(self.store, before['id'], 'suspend', reason='Concurrent user request'))
                    if invalidation:
                        raise JobsError('Observation failed after client submitted')
                    return control.Domain((MEMBER,), (ANCHOR, RUNNER), ())
                coordinator._validate.side_effect = verification
                with patch.object(control, '_stopped', return_value=False):
                    coordinator.tick(before)
                current = self.current()
                self.assertEqual(accepted[0]['result'], 'ACCEPTED')
                self.assertEqual(current['status'], 'Run')
                self.assertEqual(current['suspension']['phase'], 'Suspending')
                self.assertEqual(current['suspension']['revision'], 1)
                self.assertTrue(current['suspension']['inhibited'])
                self.assertEqual(current['suspension']['reason'], 'Concurrent user request')
                self.assertEqual(current['worker'], before['worker'])
                coordinator.channel.write.assert_not_called()
                coordinator.tick(current)
                self.assertEqual(coordinator._validate.call_count, 1)
                command = coordinator.channel.write.call_args.args[1]
                self.assertEqual(command['operation'], 'prepare_suspend')
                self.assertEqual(command['revision'], 1)
                self.assertEqual(command['request_id'], current['suspension']['request']['id'])
                self.assertEqual(coordinator.pending['id'], command['request_id'])
        self.send.assert_not_called()

    def test_authority_change_during_verification_is_not_a_superseded_request(self):
        coordinator = self.coordinator()
        before = self.current()
        def replaced_worker():
            self.change(worker=OTHER)
            return control.Domain((MEMBER,), (ANCHOR, RUNNER), ())
        coordinator._validate.side_effect = replaced_worker
        with patch.object(control, '_stopped', return_value=False), self.assertRaises(JobsError) as raised:
            coordinator.tick(before)
        self.assertNotIsInstance(raised.exception, control.RequestSuperseded)
        self.assertEqual(self.current()['worker'], OTHER)
        coordinator.channel.write.assert_not_called()
        self.send.assert_not_called()

    def test_root_exit_during_post_continue_validation_waits_for_completion(self):
        for observation in ('absent', 'zombie-topology-gone', 'foreign-session',
                            'live-topology-gone', 'denied', 'reused'):
            with self.subTest(observation=observation):
                job = self.phase('Run', revision=4, request={'id': 'continued', 'action': 'continue'},
                                 accounting_uncertain=False)
                coordinator = self.coordinator()
                del coordinator._validate
                root_probes = 0

                def alive(member):
                    nonlocal root_probes
                    if member == MEMBER:
                        root_probes += 1
                        return root_probes == 1
                    return True

                def session(pid):
                    if pid == MEMBER['pid']:
                        if observation in {'zombie-topology-gone', 'live-topology-gone'}:
                            raise ProcessLookupError()
                        if observation == 'foreign-session':
                            return OTHER['pid']
                    return ANCHOR['pid']

                errors = {'absent': control.psutil.NoSuchProcess(MEMBER['pid']),
                          'denied': control.psutil.AccessDenied(MEMBER['pid'])}
                observed = {**MEMBER, 'birth': MEMBER['birth']+1} if observation == 'reused' else MEMBER
                with patch.object(control, 'owner_alive', side_effect=alive), \
                        patch('cmw.jobs.ownership._legacy_identity', return_value=observed, side_effect=errors.get(observation)), \
                        patch.object(control.os, 'getsid', side_effect=session), \
                        patch.object(control.psutil, 'Process') as process, \
                        patch.object(control, '_stopped', return_value=False), \
                        patch.object(control.time, 'monotonic', return_value=1):
                    process.return_value.status.return_value = (control.psutil.STATUS_RUNNING
                        if observation == 'live-topology-gone' else control.psutil.STATUS_ZOMBIE)
                    coordinator.tick(job)
                current = self.current()
                value = current['suspension']
                self.assertEqual(current['status'], 'Run')
                self.assertEqual(value['revision'], 4)
                self.assertEqual(current['resources'], job['resources'])
                if observation in {'absent', 'zombie-topology-gone'}:
                    self.assertEqual(value['phase'], 'Run')
                    self.assertIsNone(value['evidence'])
                    self.assertIsNone(value['verified_at'])
                    self.assertFalse(value['accounting_uncertain'])
                    self.assertEqual(coordinator.domain_pending_since, 1)
                    coordinator.channel.write.assert_not_called()
                else:
                    self.assertEqual(value['phase'], 'Unverified')
                    self.assertTrue(value['accounting_uncertain'])
                    self.assertIsNone(coordinator.domain_pending_since)
                    self.assertEqual(coordinator.channel.write.call_args.args[1]['operation'], 'invalidate')
        self.send.assert_not_called()

    def test_exit_after_running_domain_validation_preserves_final_timing(self):
        from cmw.jobs.runtime import finish
        for boundary in ('before-status', 'during-status', 'matching-zombie', 'zombie-topology-gone'):
            with self.subTest(boundary=boundary):
                self.change(status='Run')
                job = self.phase('Run', revision=4, request={'id': 'continued', 'action': 'continue'},
                                 clock_started=80, intervals=[{'start': 85, 'end': 90}],
                                 accounting_uncertain=False, clock_finished=None)
                coordinator = self.coordinator()
                probes = 0

                def alive(member):
                    nonlocal probes
                    if member == MEMBER:
                        probes += 1
                        return boundary == 'during-status' or probes == 1
                    return True

                zombie = boundary in {'matching-zombie', 'zombie-topology-gone'}
                identify = (None if zombie
                            else control.psutil.NoSuchProcess(MEMBER['pid']))
                status = (None if zombie
                          else control.psutil.NoSuchProcess(MEMBER['pid']))
                topology = ProcessLookupError() if boundary == 'zombie-topology-gone' else None
                with patch.object(control, 'owner_alive', side_effect=alive), \
                        patch('cmw.jobs.ownership._legacy_identity', return_value=MEMBER, side_effect=identify), \
                        patch.object(control.os, 'getsid', return_value=ANCHOR['pid'], side_effect=topology), \
                        patch.object(control.psutil, 'Process') as process, \
                        patch.object(control.time, 'monotonic', return_value=1):
                    process.return_value.status.return_value = control.psutil.STATUS_ZOMBIE
                    process.return_value.status.side_effect = status
                    coordinator.tick(job)
                current = self.current()
                self.assertEqual(current['status'], 'Run')
                self.assertEqual(current['suspension']['phase'], 'Run')
                self.assertFalse(current['suspension'].get('accounting_uncertain'))
                self.assertIsNone(current['suspension']['evidence'])
                self.assertEqual(coordinator.domain_pending_since, 1)
                self.assertEqual(current['resources'], job['resources'])
                coordinator.channel.write.assert_not_called()
                finish(self.store, job['id'], job['claim'], 'Done', 0, 'Synthetic receipt and drainage verified')
                final = self.current()['suspension']
                self.assertEqual(final['unsuspended_elapsed'], 15)
                self.assertEqual(final['clock_finished'], 100)
                self.assertEqual(self.store.snapshot(now=1000)['jobs'][0]['suspension']['unsuspended_elapsed'], 15)
        self.send.assert_not_called()

    def test_stop_state_read_denial_and_confirmed_transition_loss_remain_distinct(self):
        cases = (('Run', 'reused'), ('Run', 'denied'), ('Run', 'unreadable-zombie'),
                 ('Run', 'escaped-zombie'), ('Run', 'topology-denied'), ('Run', 'status-denied'),
                 ('Run', 'live-topology-gone'),
                 ('Suspended', 'absent'), ('Resuming', 'absent'))
        for phase, observation in cases:
            with self.subTest(phase=phase, observation=observation):
                job = self.phase(phase, revision=4, request={'id': 'continued', 'action': 'continue'},
                                 evidence={'checked_clock': 100, 'computational': [MEMBER]},
                                 accounting_uncertain=False)
                coordinator = self.coordinator()
                coordinator.pending = dict(id='continued', stage='verify_continue', started=1,
                                           domain=control.Domain((MEMBER,), (ANCHOR, RUNNER), ()))
                probes = 0

                def alive(member):
                    nonlocal probes
                    if member == MEMBER:
                        probes += 1
                        return observation == 'status-denied' or (phase != 'Suspended' and probes == 1)
                    return True

                errors = {'denied': control.psutil.AccessDenied(MEMBER['pid']),
                          'unreadable-zombie': control.psutil.ZombieProcess(MEMBER['pid']),
                          'absent': control.psutil.NoSuchProcess(MEMBER['pid'])}
                observed = {**MEMBER, 'birth': MEMBER['birth']+1} if observation == 'reused' else MEMBER
                topology = (PermissionError() if observation == 'topology-denied' else
                            ProcessLookupError() if observation == 'live-topology-gone' else None)
                with patch.object(control, 'owner_alive', side_effect=alive), \
                        patch('cmw.jobs.ownership._legacy_identity', return_value=observed, side_effect=errors.get(observation)), \
                        patch.object(control.os, 'getsid', return_value=OTHER['pid'], side_effect=topology), \
                        patch.object(control.psutil, 'Process') as process, \
                        patch.object(control.time, 'monotonic', return_value=1):
                    process.return_value.status.return_value = control.psutil.STATUS_ZOMBIE
                    if observation == 'live-topology-gone':
                        process.return_value.status.return_value = control.psutil.STATUS_RUNNING
                    if observation == 'status-denied':
                        process.return_value.status.side_effect = control.psutil.AccessDenied(MEMBER['pid'])
                    coordinator.tick(job)
                value = self.current()['suspension']
                if observation == 'status-denied':
                    self.assertEqual(value['phase'], phase)
                    self.assertFalse(value['accounting_uncertain'])
                    coordinator.channel.write.assert_not_called()
                    continue
                self.assertEqual(value['phase'], 'Unverified')
                self.assertTrue(value['accounting_uncertain'])
                self.assertIsNone(value['unsuspended_elapsed'])
                self.assertIsNone(coordinator.domain_pending_since)
                self.assertEqual(coordinator.channel.write.call_args.args[1]['operation'], 'invalidate')
        self.send.assert_not_called()

    def test_unexpected_stop_precedes_a_later_running_member_exit(self):
        job = self.phase('Run', revision=4, request={'id': 'continued', 'action': 'continue'})
        coordinator = self.coordinator()
        coordinator._validate.return_value = control.Domain((MEMBER, OTHER), (ANCHOR, RUNNER), ())
        with patch.object(control, '_stopped', side_effect=[True, control.DomainPending('Later member exited')]) as stopped:
            coordinator.tick(job)
        value = self.current()['suspension']
        self.assertEqual(value['phase'], 'Unverified')
        self.assertEqual(value['reason'], 'Unexpected stopped computation outside a confirmed request')
        self.assertTrue(value['accounting_uncertain'])
        self.assertIsNone(coordinator.domain_pending_since)
        stopped.assert_called_once_with(MEMBER, exit_anchor=ANCHOR)
        self.send.assert_not_called()

    def test_incomplete_running_domain_keeps_its_driver_without_time_based_invalidation(self):
        job = self.phase('Run', revision=4, request={'id': 'continued', 'action': 'continue'})
        coordinator = self.coordinator()
        coordinator._validate.side_effect = control.DomainPending('Computational member exited during observation')
        with patch.object(control.time, 'monotonic', return_value=1):
            coordinator.tick(job)
        pending = self.current()
        self.assertEqual(pending['status'], 'Run')
        self.assertEqual(pending['suspension']['phase'], 'Run')
        self.assertIsNone(pending['suspension']['evidence'])
        self.assertIsNone(pending['suspension']['verified_at'])
        self.assertEqual(pending['resources'], job['resources'])
        coordinator.channel.write.assert_not_called()
        with patch.object(control.time, 'monotonic', return_value=1 + control.TRANSITION_TIMEOUT):
            coordinator.tick(self.current())
        current = self.current()
        self.assertEqual(coordinator.domain_pending_since, 1)
        self.assertEqual(current['status'], 'Run')
        self.assertEqual(current['suspension']['phase'], 'Run')
        self.assertEqual(current['resources'], job['resources'])
        coordinator.channel.write.assert_not_called()
        self.send.assert_not_called()

    def test_external_continue_or_member_change_invalidates_suspension(self):
        for stopped, member in ((False, MEMBER), (True, OTHER)):
            self.phase('Suspended', inhibited=True)
            coordinator = self.coordinator()
            coordinator._validate.return_value = control.Domain((member,), (ANCHOR, RUNNER), ())
            with patch.object(control, '_stopped', return_value=stopped):
                coordinator.tick(self.current())
            value = self.current()['suspension']
            self.assertEqual(value['phase'], 'Unverified')
            self.assertTrue(value['inhibited'])
        self.send.assert_not_called()

    def test_cancellation_preparation_preserves_each_transition_until_drain(self):
        for phase in ('Run', 'Suspending', 'Suspended', 'Resuming', 'Unverified'):
            with self.subTest(phase=phase):
                job = self.phase(phase, revision=2, inhibited=True,
                                 request={'id': 'previous', 'action': 'suspend'}, intervals=[{'start': 90, 'end': None}])
                job = self.change(status='Cancelling', cancel_requested=True)
                coordinator = self.coordinator()
                runner = object.__new__(RunnerControl)
                runner.binding = deepcopy(coordinator.context)
                runner._state, runner._last_command = 'hold', None
                runner._last_ack_clock, runner._last_ack_status = None, None
                runner.intervals = deepcopy(job['suspension']['intervals'])
                runner._ack = Mock()
                coordinator.channel.write.side_effect = lambda name, command, **_: runner._apply(command)
                coordinator.ack = Mock(side_effect=lambda *args: {'status': 'cancel_prepared',
                                       'intervals': deepcopy(runner.intervals),
                                       'suspended_seconds': runner.suspended_seconds})
                self.assertTrue(coordinator.prepare_cancel(job))
                self.assertEqual(runner._state, 'cancel')
                current = self.current()
                self.assertEqual(current['status'], 'Cancelling')
                self.assertEqual(current['suspension']['phase'], phase)
                self.assertTrue(current['suspension']['inhibited'])
                self.assertEqual(current['suspension']['intervals'], [{'start': 90, 'end': 100}])
                self.assertEqual(current['suspension']['suspended_seconds'], 10)
        self.send.assert_not_called()

    def test_continually_forking_domain_is_not_a_successful_stop(self):
        coordinator = self.coordinator()
        sequence = iter(range(101000, 101100))
        coordinator._validate.side_effect = lambda: control.Domain(
            (MEMBER, {**OTHER, 'pid': next(sequence)}), (ANCHOR, RUNNER), ())
        job = self.phase('Suspending', revision=1, request={'id': 'request', 'action': 'suspend'}, inhibited=True)
        coordinator.pending = {'id': 'request', 'stage': 'prepare_suspend', 'started': 0}
        coordinator.ack = Mock(return_value={'status': 'prepared'})
        stopped = []
        with patch.object(control, '_send', side_effect=lambda member, *_: stopped.append(member)), \
                patch.object(control, '_stopped', side_effect=lambda member: member in stopped), \
                patch.object(control.time, 'sleep'), \
                patch.object(control.time, 'monotonic', side_effect=[0, 0, 0, 1, 2, 20]):
            coordinator.tick(job)
        self.assertEqual(self.current()['suspension']['phase'], 'Suspending')
        self.assertIn('Awaiting computational stop verification', self.current()['suspension']['reason'])
        self.assertTrue(self.current()['suspension']['inhibited'])
        self.assertNotIn(ANCHOR, stopped)
        self.assertNotIn(RUNNER, stopped)

    def test_stale_paused_runner_ack_waits_without_invalidating_accounting(self):
        for observed in (None, 96, 101, 100):
            with self.subTest(observed_clock=observed):
                job = self.phase('Suspended', inhibited=True)
                coordinator = self.coordinator()
                coordinator.ack = Mock(return_value={'observed_clock': observed, 'intervals': []})
                with patch.object(control, '_stopped', return_value=True):
                    coordinator.tick(job)
                value = self.current()['suspension']
                self.assertEqual(value['phase'], 'Suspended')
                self.assertTrue(value['inhibited'])
                self.assertFalse(value.get('accounting_uncertain', False))
                if observed != 100:
                    self.assertEqual(value.get('verified_at'), job['suspension'].get('verified_at'))
                coordinator.channel.write.assert_not_called()

    def test_paused_ack_freshness_uses_clock_after_read(self):
        for observed, read_at, fresh in ((100.1, 100.2, True),
                                        (100, 104, False),
                                        (100.3, 100.2, False)):
            with self.subTest(observed_clock=observed, read_at=read_at):
                job = self.phase('Suspended', inhibited=True)
                coordinator = self.coordinator()
                with patch('cmw.jobs.suspension_protocol.now_clock', return_value=100.0) as clock, \
                        patch.object(control, '_stopped', return_value=True):
                    def read_ack(*args):
                        clock.return_value = read_at
                        return {'observed_clock': observed, 'intervals': []}
                    coordinator.ack = Mock(side_effect=read_ack)
                    coordinator.tick(job)
                value = self.current()['suspension']
                self.assertEqual(value['phase'], 'Suspended')
                self.assertTrue(value['inhibited'])
                self.assertFalse(value.get('accounting_uncertain', False))
                if not fresh:
                    self.assertEqual(value.get('verified_at'), job['suspension'].get('verified_at'))
                coordinator.channel.write.assert_not_called()
        self.send.assert_not_called()

    def test_nonstoppable_member_never_reports_suspended(self):
        coordinator = self.coordinator()
        job = self.phase('Suspending', revision=1, request={'id': 'request', 'action': 'suspend'}, inhibited=True)
        coordinator.pending = {'id': 'request', 'stage': 'prepare_suspend', 'started': 0}
        coordinator.ack = Mock(return_value={'status': 'prepared'})
        with patch.object(control, '_stopped', return_value=False), \
                patch.object(control, '_send'), patch.object(control.time, 'sleep'), \
                patch.object(control.time, 'monotonic', side_effect=[0, 0, 0, 20]):
            coordinator.tick(job)
        value = self.current()['suspension']
        self.assertEqual(value['phase'], 'Suspending')
        self.assertTrue(value['inhibited'])
        self.assertIn('Awaiting computational stop verification', value['reason'])
        self.assertFalse(value.get('accounting_uncertain', False))

    def test_partial_signal_failure_retains_delivered_evidence(self):
        coordinator = self.coordinator()
        domain = control.Domain((MEMBER, OTHER), (ANCHOR, RUNNER), ())
        coordinator._validate.return_value = domain
        job = self.phase('Suspending', revision=1, request={'id': 'request', 'action': 'suspend'}, inhibited=True)
        coordinator.pending = {'id': 'request', 'stage': 'prepare_suspend', 'started': 0}
        coordinator.ack = Mock(return_value={'status': 'prepared'})
        delivered = []
        def send(member, anchor, signum):
            if member == OTHER:
                raise JobsError('Fictional member became unobservable after first stop')
            delivered.append(member)
        with patch.object(control, '_send', side_effect=send), \
                patch.object(control, '_stopped', side_effect=lambda member: member in delivered), \
                patch.object(control.time, 'monotonic', return_value=0):
            coordinator.tick(job)
        record = self.current()['suspension']
        self.assertEqual(record['phase'], 'Unverified')
        self.assertIn(MEMBER, [item['identity'] for item in (record.get('evidence') or {}).get('signals', []) if item.get('delivered')])
        self.assertNotIn(ANCHOR, delivered)
        self.assertNotIn(RUNNER, delivered)

    def test_continue_identity_replacement_refuses_run_acknowledgement(self):
        coordinator = self.coordinator()
        original = control.Domain((MEMBER,), (ANCHOR, RUNNER), ())
        coordinator._validate.return_value = control.Domain((OTHER,), (ANCHOR, RUNNER), ())
        coordinator.pending = {'id': 'request', 'stage': 'verify_continue', 'started': 0, 'domain': original}
        job = self.phase('Resuming', revision=2, request={'id': 'request', 'action': 'continue'}, inhibited=True)
        with patch.object(control, '_stopped', return_value=False), patch.object(control.time, 'monotonic', return_value=0):
            coordinator.tick(job)
        self.assertEqual(self.current()['suspension']['phase'], 'Unverified')
        self.assertTrue(self.current()['suspension']['inhibited'])
        coordinator.channel.write.assert_called_once()
        self.assertEqual(coordinator.channel.write.call_args.args[1]['operation'], 'invalidate')

    def test_root_exit_keeps_receipt_driver_without_claiming_suspension(self):
        for phase in ('Run', 'Suspending', 'Resuming'):
            with self.subTest(phase=phase):
                job = self.phase(phase, revision=1,
                                 request={'id': 'request', 'action': 'suspend'}, inhibited=False,
                                 evidence={'checked_clock': 100, 'computational': [MEMBER]})
                coordinator = self.coordinator()
                with patch.object(control, 'owner_alive', side_effect=lambda item: item != MEMBER), \
                        patch('cmw.jobs.ownership._legacy_identity', side_effect=control.psutil.NoSuchProcess(MEMBER['pid'])), \
                        patch.object(control.time, 'monotonic', return_value=10):
                    coordinator.tick(job)
                waiting = self.current()
                self.assertEqual(waiting['status'], 'Run')
                self.assertEqual(waiting['suspension']['phase'], phase)
                self.assertTrue(waiting['suspension']['inhibited'])
                self.assertIsNone(waiting['suspension']['evidence'])
                self.assertFalse(control.readiness(self.store, job['id'])['ready'])
                coordinator._validate.assert_not_called()
                coordinator.channel.write.assert_not_called()
                with patch.object(control, 'owner_alive', side_effect=lambda item: item != MEMBER), \
                        patch('cmw.jobs.ownership._legacy_identity', side_effect=control.psutil.NoSuchProcess(MEMBER['pid'])), \
                        patch.object(control.time, 'monotonic', return_value=10+control.TRANSITION_TIMEOUT):
                    coordinator.tick(waiting)
                value = self.current()['suspension']
                self.assertEqual(value['phase'], phase)
                self.assertTrue(value['inhibited'])
                self.assertIn('Awaiting genuine runner completion', value['reason'])
                self.assertFalse(value.get('accounting_uncertain', False))
                coordinator.channel.write.assert_not_called()
        self.send.assert_not_called()

    def test_hard_deadline_is_processed_even_after_unverified_transition(self):
        coordinator = self.coordinator()
        coordinator.channel.read.return_value = {'runner': RUNNER, 'reason': 'fixed wall expired'}
        job = self.phase('Unverified', inhibited=True)
        coordinator.tick(job)
        current = self.current()
        self.assertEqual(current['status'], 'Cancelling')
        self.assertTrue(current['termination_requested'])
        self.assertTrue(current['suspension']['inhibited'])

    def test_cancel_before_first_suspension_has_valid_runner_protocol(self):
        coordinator = self.coordinator()
        runner = object.__new__(RunnerControl)
        runner.binding = deepcopy(coordinator.context)
        runner._state, runner._last_command = 'run', None
        runner._last_ack_clock, runner._last_ack_status = None, None
        runner.intervals = []
        runner._ack = Mock()
        coordinator.channel.write.side_effect = lambda name, command, **_: runner._apply(command)
        coordinator.ack = Mock(return_value={'status': 'cancel_prepared'})
        job = self.change(status='Cancelling', cancel_requested=True)
        self.assertTrue(coordinator.prepare_cancel(job))
        self.assertEqual(runner._state, 'cancel')


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw suspension channel ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'channel'
        self.root.mkdir(mode=0o700)
        self.context = {key: f'fixture-{key}' for key in control.BINDING}
        self.context['directory_identity'] = control._file_identity(self.root)
        self.channel = control.Channel(self.root, self.context)

    def test_forged_binding_permissions_symlinks_and_duplicate_records_refuse(self):
        for key in control.BINDING:
            with self.subTest(key=key):
                self.channel.write('ack.json', {**self.context, key: 'foreign'})
                with self.assertRaises(JobsError):
                    self.channel.read('ack.json')
        self.channel.write('ack.json', self.context)
        (self.root/'ack.json').chmod(0o644)
        with self.assertRaises(JobsError):
            self.channel.read('ack.json')
        (self.root/'ack.json').unlink()
        (self.root/'ack.json').symlink_to(self.root/'missing')
        with self.assertRaises(OSError):
            self.channel.read('ack.json')

    def test_control_directory_replacement_refuses(self):
        self.root.rename(self.root.with_name('original'))
        self.root.mkdir(mode=0o700)
        with self.assertRaises(JobsError):
            self.channel.write('ack.json', self.context)

    def test_directory_descriptor_is_closed_when_fstat_is_unreadable(self):
        native_open = control.os.open
        opened = []
        def open_directory(*args, **options):
            fd = native_open(*args, **options)
            opened.append(fd)
            return fd
        with patch.object(control.os, 'open', side_effect=open_directory), \
                patch.object(control.os, 'fstat', side_effect=OSError(errno.EAGAIN, 'one descriptor read')), \
                patch.object(control.os, 'close', wraps=control.os.close) as close:
            with self.assertRaises(OSError):
                self.channel._open_directory()
        self.assertEqual(len(opened), 1)
        close.assert_called_once_with(opened[0])


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.records = {item['pid']: item for item in (MEMBER, OTHER, WORKER, ANCHOR, RUNNER)}
        self.parents = {MEMBER['pid']: [RUNNER['pid'], ANCHOR['pid']],
                        OTHER['pid']: [MEMBER['pid'], RUNNER['pid'], ANCHOR['pid']],
                        RUNNER['pid']: [ANCHOR['pid']]}
        self.children = []
        self.members = [ANCHOR['pid'], RUNNER['pid'], MEMBER['pid']]
        self.registration = {'runner': RUNNER, 'root': MEMBER, 'strategy': 'direct-tree',
                             'runtime': {'local_only': True}}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(control, 'owner_alive', return_value=True))
        self.sid = self.stack.enter_context(patch.object(control.os, 'getsid', return_value=ANCHOR['pid']))
        self.pgid = self.stack.enter_context(patch.object(control.os, 'getpgid', return_value=MEMBER['pid']))
        self.stack.enter_context(patch.object(control, 'group_members', side_effect=lambda *a, **k: self.members))
        self.stack.enter_context(patch('cmw.jobs.ownership._legacy_identity', side_effect=lambda pid: self.records[pid]))
        self.stack.enter_context(patch.object(control, '_descends', side_effect=lambda pid, ancestor: pid == ancestor or ancestor in self.parents.get(pid, [])))
        self.process = self.stack.enter_context(patch.object(control.psutil, 'Process'))
        self.descendants = self.stack.enter_context(patch.object(
            control, 'descendant_pids', side_effect=lambda _: [child.pid for child in self.children]))
        self.signal = self.stack.enter_context(patch.object(control, '_send', side_effect=AssertionError('Domain binding signalled')))

    def child(self, record):
        value = Mock(pid=record['pid'])
        value.status.return_value = 'running'
        return value

    def test_same_session_separate_group_and_late_owned_fork_are_classified(self):
        # A child appearing after initial tree enumeration is still lineage-bound.
        self.members.append(OTHER['pid'])
        domain = control.bind_domain(self.registration, ANCHOR)
        self.assertEqual(domain.computational, (MEMBER, OTHER))
        self.assertEqual(domain.protected, (ANCHOR, RUNNER))
        self.signal.assert_not_called()

    def test_runner_exit_between_live_and_session_probes_waits_only_with_root_exit(self):
        def session(pid):
            if pid == RUNNER['pid']:
                raise ProcessLookupError(3, 'synthetic exit during lookup')
            return ANCHOR['pid']

        def observed(pid):
            if pid in {RUNNER['pid'], MEMBER['pid']}:
                raise control.psutil.NoSuchProcess(pid)
            return self.records[pid]

        self.sid.side_effect = session
        self.process.return_value.status.return_value = control.psutil.STATUS_RUNNING
        with patch('cmw.jobs.ownership._legacy_identity', side_effect=observed):
            with self.assertRaisesRegex(control.DomainPending, 'runner and computation exited'):
                control.bind_domain(self.registration, ANCHOR)
        self.signal.assert_not_called()

    def test_runner_exit_requires_independent_root_exit_and_live_anchor(self):
        for outcome in ('root-live', 'root-reused', 'root-denied', 'root-unreadable-zombie',
                        'anchor-lost', 'anchor-stopped'):
            with self.subTest(outcome=outcome):
                def observed(pid):
                    if pid == RUNNER['pid']:
                        raise control.psutil.NoSuchProcess(pid)
                    if pid == MEMBER['pid']:
                        if outcome == 'root-denied':
                            raise control.psutil.AccessDenied(pid)
                        if outcome == 'root-unreadable-zombie':
                            raise control.psutil.ZombieProcess(pid)
                        if outcome == 'root-reused':
                            return {**MEMBER, 'birth': MEMBER['birth']+1}
                        if outcome.startswith('anchor-'):
                            raise control.psutil.NoSuchProcess(pid)
                    return self.records[pid]

                self.process.return_value.status.return_value = (
                    control.psutil.STATUS_STOPPED if outcome == 'anchor-stopped'
                    else control.psutil.STATUS_RUNNING)
                with patch('cmw.jobs.ownership._legacy_identity', side_effect=observed), \
                     patch.object(control, 'owner_alive', side_effect=lambda member:
                                  member != RUNNER and not (outcome == 'anchor-lost' and member == ANCHOR)):
                    with self.assertRaises(JobsError) as raised:
                        control.bind_domain(self.registration, ANCHOR)
                self.assertNotIsInstance(raised.exception, control.DomainPending)
        self.signal.assert_not_called()

    def test_absent_or_zombie_computational_members_are_pending(self):
        for member in (MEMBER, OTHER):
            self.children = [self.child(OTHER)] if member == OTHER else []
            for outcome in ('absent', 'zombie'):
                with self.subTest(member=member['pid'], outcome=outcome), \
                        patch.object(control, 'owner_alive', side_effect=lambda item: item != member), \
                        patch('cmw.jobs.ownership._legacy_identity') as identify:
                    def observed(pid):
                        if pid == member['pid'] and outcome == 'absent':
                            raise control.psutil.NoSuchProcess(pid)
                        return self.records[pid]
                    identify.side_effect = observed
                    self.process.return_value.status.return_value = control.psutil.STATUS_ZOMBIE
                    with self.assertRaises(control.DomainPending):
                        control.bind_domain(self.registration, ANCHOR)
        self.signal.assert_not_called()

    def test_root_exit_between_identity_and_group_observation_is_pending(self):
        for outcome in ('absent', 'zombie', 'zombie-topology-gone'):
            with self.subTest(outcome=outcome), patch('cmw.jobs.ownership._legacy_identity') as identify:
                root_sessions = 0

                def session(pid):
                    nonlocal root_sessions
                    if pid == MEMBER['pid']:
                        root_sessions += 1
                        if root_sessions > 1 and outcome == 'zombie-topology-gone':
                            raise ProcessLookupError()
                    return ANCHOR['pid']

                self.sid.side_effect = session
                self.pgid.side_effect = ProcessLookupError(3, 'No such process')
                identify.return_value = MEMBER
                identify.side_effect = control.psutil.NoSuchProcess(MEMBER['pid']) if outcome == 'absent' else None
                self.process.return_value.status.return_value = control.psutil.STATUS_ZOMBIE
                with self.assertRaises(control.DomainPending) as raised:
                    control.bind_domain(self.registration, ANCHOR)
                self.assertIsInstance(raised.exception.__cause__, ProcessLookupError)
                self.pgid.assert_called_with(MEMBER['pid'])
                self.assertGreaterEqual(root_sessions, 1)
        self.signal.assert_not_called()

    def test_group_observation_loss_requires_positive_root_exit_proof(self):
        for outcome in ('live', 'reused', 'denied', 'unreadable-zombie',
                        'unreadable-status', 'escaped-session', 'topology-denied'):
            with self.subTest(outcome=outcome), patch('cmw.jobs.ownership._legacy_identity') as identify:
                root_sessions = 0

                def session(pid):
                    nonlocal root_sessions
                    if pid == MEMBER['pid']:
                        root_sessions += 1
                        if root_sessions > 1 and outcome == 'escaped-session':
                            return OTHER['pid']
                        if root_sessions > 1 and outcome == 'topology-denied':
                            raise PermissionError()
                    return ANCHOR['pid']

                self.sid.side_effect = session
                self.pgid.side_effect = ProcessLookupError(3, 'No such process')
                identify.return_value = ({**MEMBER, 'birth': MEMBER['birth']+1}
                                         if outcome == 'reused' else MEMBER)
                identify.side_effect = {'denied': control.psutil.AccessDenied(MEMBER['pid']),
                                        'unreadable-zombie': control.psutil.ZombieProcess(MEMBER['pid'])}.get(outcome)
                self.process.return_value.status.return_value = (control.psutil.STATUS_RUNNING
                    if outcome == 'live' else control.psutil.STATUS_ZOMBIE)
                self.process.return_value.status.side_effect = (control.psutil.NoSuchProcess(MEMBER['pid'])
                    if outcome == 'unreadable-status' else None)
                with self.assertRaises(ProcessLookupError):
                    control.bind_domain(self.registration, ANCHOR)
                self.pgid.assert_called_with(MEMBER['pid'])
        self.signal.assert_not_called()

    def test_protected_session_loss_cannot_become_exit_pending(self):
        for protected in (ANCHOR, RUNNER):
            with self.subTest(protected=protected['pid']):
                def session(pid):
                    if pid == protected['pid']:
                        raise ProcessLookupError(3, 'No such process')
                    return ANCHOR['pid']
                self.sid.side_effect = session
                with self.assertRaises(ProcessLookupError):
                    control.bind_domain(self.registration, ANCHOR)
        self.pgid.assert_not_called()
        self.signal.assert_not_called()

    def test_reused_denied_and_protected_losses_do_not_become_pending(self):
        for member, outcome in ((MEMBER, 'reused'), (MEMBER, 'denied'), (MEMBER, 'unreadable-zombie'), (MEMBER, 'unreadable-status'),
                                (RUNNER, 'absent'), (ANCHOR, 'absent')):
            with self.subTest(member=member['pid'], outcome=outcome), \
                    patch.object(control, 'owner_alive', side_effect=lambda item: item != member), \
                    patch('cmw.jobs.ownership._legacy_identity') as identify:
                if outcome == 'denied':
                    identify.side_effect = control.psutil.AccessDenied(member['pid'])
                elif outcome == 'unreadable-zombie':
                    identify.side_effect = control.psutil.ZombieProcess(member['pid'])
                elif outcome == 'unreadable-status':
                    identify.return_value = member
                    self.process.return_value.status.side_effect = control.psutil.NoSuchProcess(member['pid'])
                elif outcome == 'absent':
                    def observed(pid):
                        if pid == member['pid']:
                            raise control.psutil.NoSuchProcess(pid)
                        return self.records[pid]
                    identify.side_effect = observed
                else:
                    identify.return_value = {**member, 'birth': member['birth'] + 1}
                with self.assertRaises(JobsError) as raised:
                    control.bind_domain(self.registration, ANCHOR)
                self.assertNotIsInstance(raised.exception, control.DomainPending)
                self.process.return_value.status.side_effect = None
        self.signal.assert_not_called()

    def test_protected_subtree_overlap_cannot_become_exit_pending(self):
        self.registration['protected'] = [OTHER]
        self.children = [self.child(OTHER)]
        probes = 0

        def alive(member):
            nonlocal probes
            if member == OTHER:
                probes += 1
                return probes == 1
            return True

        self.process.return_value.status.return_value = control.psutil.STATUS_ZOMBIE
        with patch.object(control, 'owner_alive', side_effect=alive):
            with self.assertRaisesRegex(JobsError, 'overlaps protected') as raised:
                control.bind_domain(self.registration, ANCHOR)
            self.assertNotIsInstance(raised.exception, control.DomainPending)
        self.signal.assert_not_called()

    def test_escaped_new_session_and_protected_group_refuse(self):
        self.sid.side_effect = lambda pid: 999 if pid == MEMBER['pid'] else ANCHOR['pid']
        with self.assertRaisesRegex(JobsError, 'escaped') as raised:
            control.bind_domain(self.registration, ANCHOR)
        self.assertNotIsInstance(raised.exception, control.DomainPending)
        with patch.object(control, 'owner_alive', side_effect=lambda item: item != MEMBER):
            self.process.return_value.status.return_value = control.psutil.STATUS_ZOMBIE
            with self.assertRaisesRegex(JobsError, 'escaped') as raised:
                control.bind_domain(self.registration, ANCHOR)
            self.assertNotIsInstance(raised.exception, control.DomainPending)
        self.sid.side_effect = None
        self.pgid.return_value = ANCHOR['pid']
        with self.assertRaisesRegex(JobsError, 'protected ownership group'):
            control.bind_domain(self.registration, ANCHOR)
        self.signal.assert_not_called()

    def test_unregistered_leaderless_subgroup_and_external_lineage_refuse(self):
        self.members.append(OTHER['pid'])
        self.parents[OTHER['pid']] = []  # Lost parent proof, despite matching SID.
        with self.assertRaisesRegex(JobsError, 'Unregistered member'):
            control.bind_domain(self.registration, ANCHOR)
        self.members.remove(OTHER['pid'])
        self.parents[MEMBER['pid']] = []
        with self.assertRaisesRegex(JobsError, 'owned payload ancestry'):
            control.bind_domain(self.registration, ANCHOR)
        self.signal.assert_not_called()

    def test_unobservable_descendant_and_unsupported_topology_refuse(self):
        self.descendants.side_effect = control.psutil.AccessDenied(MEMBER['pid'])
        with self.assertRaisesRegex(control.OwnershipUnavailable, 'subtree'):
            control.bind_domain(self.registration, ANCHOR)
        self.descendants.side_effect = lambda _: []
        for changed in ({**self.registration, 'strategy': 'unknown'},
                        {**self.registration, 'runtime': {'local_only': False}}):
            with self.subTest(registration=changed), self.assertRaises(JobsError):
                control.bind_domain(changed, ANCHOR)
        self.signal.assert_not_called()

    def test_replaced_runtime_binary_refuses_before_process_classification(self):
        registration = {**self.registration, 'runtime': {'local_only': True, 'driver':
                        {'path': '/fictional/driver', 'device': 1, 'inode': 1}}}
        with patch.object(control, '_file_identity', return_value={'device': 1, 'inode': 2}), \
                self.assertRaisesRegex(JobsError, 'replaced'):
            control.bind_domain(registration, ANCHOR)
        self.process.assert_not_called()
        self.signal.assert_not_called()
