"""Atomic desired actions against real disposable queues; no engines or signals."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from cmw.jobs import suspension
from cmw.jobs.store import JobsError, Store
from cmw.nodes.control import dispatch, request_digest
from cmw.nodes.protocol import NodeError
from tests.jobs.test_suspension_state import CLOCK, MEMBER, WORKER, ANCHOR, RUNNER


class ControlFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-node-control-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'queue')
        self.queue = self.store.enroll_queue(uuid4().hex, 'linux:synthetic-only')
        self.backend = SimpleNamespace(store=self.store, queue=self.queue,
            config={'node_id': self.queue['node_id']}, clock_id='a' * 64)
        self.job = self.store.add(argv=['/unexecuted/fixture'], cwd=self.root, name='Control fixture', hold=True)
        self.target = {key: self.queue[key] for key in ('node_id', 'queue_id')}
        self.target.update(job_id=self.job['id'], attempt_id=self.job['attempt_id'])
        for name in ('cmw.jobs.runtime.signal_session', 'cmw.jobs.suspension._send'):
            signal = patch(name, side_effect=AssertionError('No fixture may signal a process'))
            signal.start()
            self.addCleanup(signal.stop)
        spawn = patch('cmw.jobs.runtime._launch_controller')
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)

    def preview(self, action, **params):
        target = None if '.' in action else self.target
        return dispatch(self.backend, 'control.preview', {'action': action, **params}, target=target)

    def params(self, preview, **changes):
        result = {key: preview[key] for key in ('action', 'expected_clock_id', 'expected_revision')}
        if 'position' in preview:
            result['position'] = preview['position']
        return {**result, 'confirm': True, 'reason': '', **changes}

    def apply(self, preview, *, request_id=None, **changes):
        return dispatch(self.backend, 'control', self.params(preview, **changes),
                        target=preview['target'], request_id=request_id or uuid4().hex)

    def current(self):
        return self.store.snapshot()['jobs'][0]

    def change(self, **changes):
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job.update(changes)
            self.store.save(con, job)

    def running(self):
        value = suspension.initial_state('local-command-v1')
        value.update(clock=CLOCK, evidence={'checked_clock': 100, 'computational': [MEMBER]},
                     capability={'protocol': 1, 'deadline': {'known': True}, 'runner': RUNNER,
                                 'state_identity': suspension._file_identity(self.store.path),
                                 'storage': [{'path': str(self.root), **suspension._file_identity(self.root)}]})
        self.change(status='Run', order=None, worker=WORKER, group=ANCHOR, claim='fixture-claim',
                    worker_protocol=3, suspension=value, started_at=100, claimed_at=100)
        for target, replacement in (
                ('cmw.jobs.suspension._alive_authority', lambda *args: True),
                ('cmw.jobs.suspension.owner_alive', lambda *args: True),
                ('cmw.jobs.suspension_protocol.clock_descriptor', lambda: CLOCK),
                ('cmw.jobs.suspension_protocol.now_clock', lambda: 100.0)):
            mocked = patch(target, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)


class ControlTests(ControlFixture, unittest.TestCase):
    def test_preview_is_read_only_and_explicit(self):
        before = self.store.path.read_bytes()
        preview = self.preview('release')
        self.assertTrue(preview['ready'])
        self.assertEqual(preview['target'], self.target)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.spawn.assert_not_called()
        for action in ('toggle', 'P', 'H', 'U'):
            with self.assertRaises(NodeError):
                self.preview(action)

    def test_client_lost_reply_reports_generated_request_id_and_lookup_guidance(self):
        from cmw.nodes.client import apply_control
        plan = self.preview('release')
        for operation in ('request', 'control'):
            with self.subTest(operation=operation):
                root = self.root / ('frontend-' + operation)
                failure = NodeError('OUTCOME_UNKNOWN' if operation == 'control' else 'UNREACHABLE',
                                    'Injected endpoint disconnect')
                backend = SimpleNamespace(node_id=self.queue['node_id'], queue_id=self.queue['queue_id'],
                    call=Mock(side_effect=[failure] if operation == 'request' else
                              [{'result': {'request': None}}, failure]))
                with self.assertRaises(NodeError) as result:
                    apply_control(backend, plan, root=root)
                records = list((root / 'requests').glob('*/request.json'))
                self.assertEqual(len(records), 1)
                record = json.loads(records[0].read_text())
                self.assertEqual(result.exception.code, failure.code)
                self.assertIn(record['request_id'], str(result.exception))
                self.assertIn('Inspect that request on the same node and queue', str(result.exception))
                self.assertEqual(record['params']['expected_clock_id'], plan['expected_clock_id'])
                self.assertEqual(record['params']['expected_revision'], plan['expected_revision'])
                self.assertEqual(backend.call.call_count, 1 if operation == 'request' else 2)

    def test_normalized_digest_matches_defaults_and_excludes_endpoint_binding(self):
        params = self.params(self.preview('release'))
        args = (self.queue['node_id'], self.queue['queue_id'], self.target)
        self.assertEqual(request_digest(*args, params), request_digest(*args, {**params, '_endpoint': {}}))
        self.assertEqual(request_digest(*args, {**params, 'reason': 'a\r\nb'}),
                         request_digest(*args, {**params, 'reason': 'a\nb'}))
        with self.assertRaises(NodeError):
            request_digest(*args, {**params, 'expected_revision': {'job': True, 'queue': 1}})

    def test_release_hold_move_are_desired_actions_and_do_not_start_dispatch(self):
        self.assertEqual(self.apply(self.preview('release'))['status'], 'COMPLETED')
        self.assertEqual(self.current()['status'], 'Queue')
        self.apply(self.preview('hold'))
        self.assertEqual(self.current()['status'], 'Hold')
        other = self.store.add(argv=['/unexecuted'], cwd=self.root, name='Other', hold=True)
        self.apply(self.preview('move', position=2))
        state = self.store.snapshot()
        self.assertEqual([job['id'] for job in sorted(state['jobs'], key=lambda job: job['order'])],
                         [other['id'], self.job['id']])
        self.assertFalse(state['controller']['dispatch'])
        self.spawn.assert_not_called()

    def test_duplicate_returns_original_receipt_after_later_state_change(self):
        preview = self.preview('release'); request_id = uuid4().hex
        receipt = self.apply(preview, request_id=request_id)
        self.store.change(self.job['id'], 'hold')
        self.assertEqual(self.apply(preview, request_id=request_id), receipt)
        self.assertEqual(self.current()['status'], 'Hold')
        self.assertEqual(self.store.request_lookup(request_id)['result'], receipt)
        with self.assertRaisesRegex(NodeError, 'different semantics'):
            self.apply(preview, request_id=request_id, reason='Changed semantics')

    def test_concurrent_duplicate_requests_insert_one_control(self):
        preview = self.preview('release'); request_id = uuid4().hex
        barrier = threading.Barrier(4)
        lookup = self.store.request_lookup
        def synchronized(value):
            result = lookup(value); barrier.wait(timeout=5); return result
        with patch.object(self.store, 'request_lookup', synchronized), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.apply(preview, request_id=request_id), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(sum(event['message'] == 'Release requested' for event in self.store.snapshot()['events']), 1)

    def test_two_unique_requests_from_one_confirmation_accept_only_one(self):
        preview = self.preview('release')
        def apply(_):
            try:
                return self.apply(preview)['status']
            except NodeError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(apply, range(2))), ['COMPLETED', 'STALE_PREVIEW'])

    def test_job_and_dispatch_aba_invalidate_original_preview(self):
        preview = self.preview('release')
        self.store.change(self.job['id'], 'release')
        self.store.change(self.job['id'], 'hold')
        with self.assertRaisesRegex(NodeError, 'Relevant state changed'):
            self.apply(preview)
        preview = self.preview('dispatch.resume')
        self.store.dispatch(True); self.store.dispatch(False)
        with self.assertRaisesRegex(NodeError, 'Relevant state changed'):
            self.apply(preview)

    def test_observation_noise_and_notes_preserve_revisions(self):
        preview = self.preview('release')
        self.store.annotate(self.job['id'], 'Independent note')
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job['reason'] = 'Transient presentation reason'; self.store.save(con, job)
            control = self.store.control(con)
            control.update(heartbeat=12345, reason='Transient', external_guard={'checked_at': 1})
            self.store.set_control(con, control)
        self.assertEqual(self.preview('release')['expected_revision'], preview['expected_revision'])
        self.apply(preview)
        self.assertEqual(self.current()['note'], 'Independent note')

    def test_counter_cannot_be_overwritten_by_stale_writer(self):
        with self.store.transaction() as con:
            stale_control = self.store.control(con)
        self.store.change(self.job['id'], 'release')
        before = self.preview('hold')['expected_revision']
        with self.store.transaction() as con:
            stale_control['heartbeat'] = 100
            self.store.set_control(con, stale_control)
            job = self.store.get(con, self.job['id']); job['node_control_revision'] = 0
            self.store.save(con, job)
        self.assertEqual(self.preview('hold')['expected_revision'], before)

    def test_wrong_node_queue_attempt_and_clock_are_refused_without_receipt(self):
        preview = self.preview('release')
        for field in ('node_id', 'queue_id', 'attempt_id'):
            altered = {**preview, 'target': {**self.target, field: uuid4().hex}}
            with self.subTest(field=field), self.assertRaises(NodeError):
                self.apply(altered)
        request_id = uuid4().hex
        self.backend.clock_id = 'b' * 64
        with self.assertRaisesRegex(NodeError, 'clock differs'):
            self.apply(preview, request_id=request_id)
        self.assertIsNone(self.store.request_lookup(request_id))
        self.assertEqual(self.current()['status'], 'Hold')

    def test_before_commit_failure_rolls_back_action_and_ledger(self):
        preview = self.preview('release'); request_id = uuid4().hex
        transaction = self.store.transaction
        @contextmanager
        def fail(**kwargs):
            with transaction(**kwargs) as con:
                yield con
                raise RuntimeError('before commit')
        with patch.object(self.store, 'transaction', fail), self.assertRaisesRegex(RuntimeError, 'before commit'):
            self.apply(preview, request_id=request_id)
        self.assertEqual(self.current()['status'], 'Hold')
        self.assertIsNone(self.store.request_lookup(request_id))
        self.assertEqual(self.apply(preview, request_id=request_id)['status'], 'COMPLETED')

    def test_after_commit_lost_reply_returns_recorded_action_without_replay(self):
        preview = self.preview('release'); request_id = uuid4().hex
        transaction = self.store.transaction
        @contextmanager
        def fail(**kwargs):
            with transaction(**kwargs) as con:
                yield con
            raise RuntimeError('lost reply')
        with patch.object(self.store, 'transaction', fail), self.assertRaisesRegex(RuntimeError, 'lost reply'):
            self.apply(preview, request_id=request_id)
        self.assertEqual(self.current()['status'], 'Queue')
        self.assertEqual(self.apply(preview, request_id=request_id), self.store.request_lookup(request_id)['result'])

    def test_pending_cancel_requires_confirmation_and_completes_atomically(self):
        preview = self.preview('cancel')
        self.assertTrue(preview['confirmation_required'])
        with self.assertRaisesRegex(NodeError, 'Explicit confirmation'):
            self.apply(preview, confirm=False)
        self.assertEqual(self.apply(preview)['status'], 'COMPLETED')
        self.assertEqual(self.current()['status'], 'Cancelled')
        self.assertEqual(self.apply(self.preview('cancel'))['status'], 'COMPLETED')

    def test_active_cancel_is_only_accepted_intent_and_worker_remains_authority(self):
        self.running(); preview = self.preview('cancel')
        self.assertEqual(self.apply(preview)['status'], 'ACCEPTED')
        current = self.current()
        self.assertEqual(current['status'], 'Cancelling')
        self.assertTrue(current['cancel_requested'])
        self.assertEqual(current['worker'], WORKER)
        self.assertEqual(current['group'], ANCHOR)
        self.assertEqual(self.apply(self.preview('cancel'))['status'], 'ACCEPTED')
        self.spawn.assert_not_called()

    def test_suspend_continue_receipts_never_claim_worker_completion(self):
        self.running(); preview = self.preview('suspend'); request_id = uuid4().hex
        self.assertTrue(preview['ready'], preview)
        with self.assertRaises(NodeError):
            self.apply(preview, confirm=False)
        receipt = self.apply(preview, request_id=request_id)
        self.assertEqual((receipt['status'], self.current()['suspension']['phase']), ('ACCEPTED', 'Suspending'))
        value = deepcopy(self.current()['suspension']); value['phase'] = 'Suspended'
        self.change(suspension=value)
        self.assertEqual(self.apply(preview, request_id=request_id), receipt)
        self.assertEqual(self.apply(self.preview('suspend'))['status'], 'COMPLETED')
        self.assertEqual(self.apply(self.preview('continue'))['status'], 'ACCEPTED')
        self.assertEqual(self.current()['suspension']['phase'], 'Resuming')

    def test_cancel_invalidates_continue_and_prevents_fresh_continue(self):
        self.running()
        value = deepcopy(self.current()['suspension']); value['phase'] = 'Suspended'
        self.change(suspension=value)
        continuation = self.preview('continue')
        self.apply(self.preview('cancel'))
        with self.assertRaises(NodeError):
            self.apply(continuation)
        self.assertFalse(self.preview('continue')['ready'])
        self.assertEqual(self.current()['status'], 'Cancelling')

    def test_suspension_evidence_aging_during_lock_wait_is_rechecked(self):
        self.running(); preview = self.preview('suspend'); request_id = uuid4().hex
        transaction = self.store.transaction
        clock = [100.0]
        @contextmanager
        def delayed(**kwargs):
            with transaction(**kwargs) as con:
                clock[0] = 105.0
                yield con
        with patch('cmw.jobs.suspension_protocol.now_clock', lambda: clock[0]), \
                patch.object(self.store, 'transaction', delayed), self.assertRaisesRegex(JobsError, 'Fresh worker'):
            self.apply(preview, request_id=request_id)
        self.assertIsNone(self.store.request_lookup(request_id))
        self.assertEqual(self.current()['suspension']['phase'], 'Run')

    def test_worker_evidence_refresh_preserves_control_revision(self):
        self.running(); before = self.preview('suspend')['expected_revision']
        value = deepcopy(self.current()['suspension'])
        value.update(evidence={'checked_clock': 100.5, 'computational': [MEMBER]}, reason='Verified')
        self.change(suspension=value)
        self.assertEqual(self.preview('suspend')['expected_revision'], before)

    def test_start_records_intent_and_never_spawns_again_on_duplicate(self):
        preview = self.preview('controller.start'); request_id = uuid4().hex
        receipt = self.apply(preview, request_id=request_id)
        self.assertEqual((receipt['status'], receipt['launch_status']), ('ACCEPTED', 'UNCONFIRMED'))
        self.spawn.assert_called_once_with(self.store)
        self.assertEqual(self.apply(preview, request_id=request_id), receipt)
        self.spawn.assert_called_once()
        with self.assertRaises(NodeError):
            self.apply(preview)
        self.assertEqual(self.current()['status'], 'Hold')

    def test_start_crash_after_commit_before_spawn_does_not_replay_spawn(self):
        preview = self.preview('controller.start'); request_id = uuid4().hex
        transaction = self.store.transaction
        @contextmanager
        def fail(**kwargs):
            with transaction(**kwargs) as con:
                yield con
            raise RuntimeError('after commit before spawn')
        with patch.object(self.store, 'transaction', fail), self.assertRaises(RuntimeError):
            self.apply(preview, request_id=request_id)
        self.spawn.assert_not_called()
        self.assertEqual(self.apply(preview, request_id=request_id)['status'], 'ACCEPTED')
        self.spawn.assert_not_called()
        self.apply(self.preview('controller.start'))
        self.spawn.assert_called_once()

    def test_controller_stop_does_not_interrupt_running_job(self):
        self.running(); before = self.current()
        receipt = self.apply(self.preview('controller.stop'))
        after = self.current()
        self.assertEqual(receipt['status'], 'ACCEPTED')
        for key in ('status', 'worker', 'group', 'claim', 'cancel_requested', 'suspension'):
            self.assertEqual(before[key], after[key])
        self.assertTrue(self.store.snapshot()['controller']['stop'])
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])
        self.spawn.assert_not_called()

    def test_old_schema_local_operations_do_not_gain_node_fields_or_ledger(self):
        local = Store(self.root / 'ordinary')
        job = local.add(argv=['/unexecuted'], cwd=self.root, name='Local', hold=True)
        local.change(job['id'], 'release'); local.dispatch(True)
        state = local.snapshot()
        self.assertEqual(state['schema'], 2)
        self.assertNotIn('node_control_revision', state['controller'])
        self.assertNotIn('node_control_revision', state['jobs'][0])
        with local.transaction() as con:
            self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='node_requests'").fetchone())

    def test_existing_enrolled_queue_without_counters_projects_zero_without_migration(self):
        with self.store.transaction() as con:
            control = self.store.control(con); control.pop('node_control_revision', None)
            job = self.store.get(con, self.job['id']); job.pop('node_control_revision', None)
            con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(control),))
            con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), job['id']))
        before = self.store.path.read_bytes()
        preview = self.preview('release')
        self.assertEqual(preview['expected_revision'], {'job': 0, 'queue': 0})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.apply(preview)
        self.assertGreater(self.current()['node_control_revision'], 0)
