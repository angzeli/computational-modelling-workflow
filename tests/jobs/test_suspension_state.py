"""Durable control/fault contracts; every process observation and signal is mocked."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cmw.jobs import suspension as control
from cmw.jobs.store import Store, JobsError
from cmw.jobs.suspension_protocol import RunnerControl


CLOCK = {'kind': 'fictional-continuous', 'boot': 100}
MEMBER = {'pid': 100001, 'birth': 101, 'host': 'fixture', 'boot': 100}
OTHER = {**MEMBER, 'pid': 100002}
WORKER = {**MEMBER, 'pid': 100003}
ANCHOR = {**MEMBER, 'pid': 100004}
RUNNER = {**MEMBER, 'pid': 100005}


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
            with patch('cmw.jobs.runtime.owner_alive', return_value=False), \
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
                        patch.object(control, 'identity', return_value=observed, side_effect=errors.get(observation)), \
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
                        patch.object(control, 'identity', return_value=MEMBER, side_effect=identify), \
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

    def test_stop_state_uncertainty_and_transition_exit_still_invalidate(self):
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
                        patch.object(control, 'identity', return_value=observed, side_effect=errors.get(observation)), \
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

    def test_incomplete_running_domain_expires_without_resetting_deadline(self):
        job = self.phase('Run', revision=4, request={'id': 'continued', 'action': 'continue'})
        coordinator = self.coordinator()
        coordinator._validate.side_effect = control.DomainPending('Computational member exited during observation')
        with patch.object(control.time, 'monotonic', return_value=1):
            coordinator.tick(job)
        with patch.object(control.time, 'monotonic', return_value=1 + control.TRANSITION_TIMEOUT):
            coordinator.tick(self.current())
        current = self.current()
        self.assertEqual(coordinator.domain_pending_since, 1)
        self.assertEqual(current['status'], 'Run')
        self.assertEqual(current['suspension']['phase'], 'Unverified')
        self.assertTrue(current['suspension']['inhibited'])
        self.assertEqual(current['resources'], job['resources'])
        self.assertEqual(coordinator.channel.write.call_args.args[1]['operation'], 'invalidate')
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
                coordinator.channel.write.side_effect = lambda name, command: runner._apply(command)
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
        self.assertEqual(self.current()['suspension']['phase'], 'Unverified')
        self.assertTrue(self.current()['suspension']['inhibited'])
        self.assertNotIn(ANCHOR, stopped)
        self.assertNotIn(RUNNER, stopped)

    def test_stale_paused_runner_ack_invalidates_verified_state(self):
        for observed, expected in ((None, 'Unverified'), (96, 'Unverified'), (101, 'Unverified'), (100, 'Suspended')):
            with self.subTest(observed_clock=observed):
                job = self.phase('Suspended', inhibited=True)
                coordinator = self.coordinator()
                coordinator.ack = Mock(return_value={'observed_clock': observed, 'intervals': []})
                with patch.object(control, '_stopped', return_value=True):
                    coordinator.tick(job)
                value = self.current()['suspension']
                self.assertEqual(value['phase'], expected)
                self.assertTrue(value['inhibited'])

    def test_paused_ack_freshness_uses_clock_after_read(self):
        for observed, read_at, expected in ((100.1, 100.2, 'Suspended'),
                                           (100, 104, 'Unverified'),
                                           (100.3, 100.2, 'Unverified')):
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
                self.assertEqual(value['phase'], expected)
                self.assertTrue(value['inhibited'])
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
        self.assertEqual(value['phase'], 'Unverified')
        self.assertTrue(value['inhibited'])
        self.assertIn('timed out', value['reason'])

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

    def test_root_exit_waits_bounded_receipt_without_claiming_suspension(self):
        for phase in ('Run', 'Suspending', 'Resuming'):
            with self.subTest(phase=phase):
                job = self.phase(phase, revision=1,
                                 request={'id': 'request', 'action': 'suspend'}, inhibited=False,
                                 evidence={'checked_clock': 100, 'computational': [MEMBER]})
                coordinator = self.coordinator()
                with patch.object(control, 'owner_alive', side_effect=lambda item: item != MEMBER), \
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
                        patch.object(control.time, 'monotonic', return_value=10+control.TRANSITION_TIMEOUT):
                    coordinator.tick(waiting)
                value = self.current()['suspension']
                self.assertEqual(value['phase'], 'Unverified')
                self.assertTrue(value['inhibited'])
                self.assertIn('without a completed runner receipt', value['reason'])
                self.assertEqual(coordinator.channel.write.call_args.args[1]['operation'], 'invalidate')
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
        coordinator.channel.write.side_effect = lambda name, command: runner._apply(command)
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
        self.stack.enter_context(patch.object(control, 'identity', side_effect=lambda pid: self.records[pid]))
        self.stack.enter_context(patch.object(control, '_descends', side_effect=lambda pid, ancestor: pid == ancestor or ancestor in self.parents.get(pid, [])))
        self.process = self.stack.enter_context(patch.object(control.psutil, 'Process'))
        self.process.return_value.children.side_effect = lambda **kwargs: self.children
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

    def test_absent_or_zombie_computational_members_are_pending(self):
        for member in (MEMBER, OTHER):
            self.children = [self.child(OTHER)] if member == OTHER else []
            for outcome in ('absent', 'zombie'):
                with self.subTest(member=member['pid'], outcome=outcome), \
                        patch.object(control, 'owner_alive', side_effect=lambda item: item != member), \
                        patch.object(control, 'identity') as identify:
                    def observed(pid):
                        if pid == member['pid'] and outcome == 'absent':
                            raise control.psutil.NoSuchProcess(pid)
                        return self.records[pid]
                    identify.side_effect = observed
                    self.process.return_value.status.return_value = control.psutil.STATUS_ZOMBIE
                    with self.assertRaises(control.DomainPending):
                        control.bind_domain(self.registration, ANCHOR)
        self.signal.assert_not_called()

    def test_reused_denied_and_protected_losses_do_not_become_pending(self):
        for member, outcome in ((MEMBER, 'reused'), (MEMBER, 'denied'), (MEMBER, 'unreadable-zombie'), (MEMBER, 'unreadable-status'),
                                (RUNNER, 'absent'), (ANCHOR, 'absent')):
            with self.subTest(member=member['pid'], outcome=outcome), \
                    patch.object(control, 'owner_alive', side_effect=lambda item: item != member), \
                    patch.object(control, 'identity') as identify:
                if outcome == 'denied':
                    identify.side_effect = control.psutil.AccessDenied(member['pid'])
                elif outcome == 'unreadable-zombie':
                    identify.side_effect = control.psutil.ZombieProcess(member['pid'])
                elif outcome == 'unreadable-status':
                    identify.return_value = member
                    self.process.return_value.status.side_effect = control.psutil.NoSuchProcess(member['pid'])
                elif outcome == 'absent':
                    identify.side_effect = control.psutil.NoSuchProcess(member['pid'])
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
        self.process.return_value.children.side_effect = control.psutil.AccessDenied(MEMBER['pid'])
        with self.assertRaises(control.psutil.AccessDenied):
            control.bind_domain(self.registration, ANCHOR)
        self.process.return_value.children.side_effect = lambda **kwargs: []
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
