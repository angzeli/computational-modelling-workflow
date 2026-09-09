"""Deterministic guard rollback/lock boundaries, without host process discovery."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cmw.jobs import activity, ownership, runtime
from cmw.jobs.store import Store
from tests.jobs import test_activity as fixtures
from tests.jobs.isolated_runtime import install


def clear():
    return {'state': 'NO_MATCH', 'reason': 'Fixture clear', 'observations': [],
            'observed_at': time.time(), 'stale': False, 'coverage': {'complete': True, 'warnings': []}}


class GuardBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-guard-boundary-')
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.directory = temporary.name
        self.addCleanup(install().close)

    def starting(self):
        row = self.store.add(argv=['/bin/echo', 'synthetic'], cwd=self.directory, name='fixture')
        with self.store.transaction() as con:
            row = self.store.get(con, row['id'])
            row.update(status='Starting', order=None, claim='original', claimed_at=time.time())
            self.store.save(con, row)
            control = self.store.control(con)
            control.update(dispatch=True, owner=fixtures.owner(700))
            self.store.set_control(con, control)
        return row

    def refused_worker(self, mutate):
        row = self.starting()
        leader = SimpleNamespace(pid=987654, stdin=Mock(), wait=Mock(return_value=0))
        preserved = {}
        def collect(store, state, deadline):
            mutate(store, row['id'])
            with store.transaction() as con:
                preserved.update(store.get(con, row['id']))
            return clear()
        with ExitStack() as stack:
            stack.enter_context(patch.object(activity, 'DEFAULT_OBSERVER', activity.Observer(collect)))
            stack.enter_context(patch.object(runtime.signal, 'signal'))
            stack.enter_context(patch.object(runtime.subprocess, 'Popen', return_value=leader))
            stack.enter_context(patch.object(runtime, 'identity', side_effect=lambda pid=None: fixtures.owner(pid or 700)))
            stack.enter_context(patch.object(runtime, 'group_exists', return_value=False))
            stack.enter_context(patch.object(runtime, 'owner_alive', return_value=True))
            # Only dispatch-authority lock evidence is replaced; the worker still
            # acquires/releases its real isolated attempt lock through exclusive.
            stack.enter_context(patch.object(runtime, 'lock_held', return_value=True))
            runtime.worker(self.store, row['id'], 'original')
        leader.stdin.write.assert_not_called()
        leader.stdin.close.assert_called()
        leader.wait.assert_called_once_with(timeout=5)
        self.assertFalse(ownership.lock_held(self.store.root / 'attempts' / row['attempt_id'] / 'worker.lock'))
        with self.store.transaction() as con:
            return self.store.get(con, row['id']), preserved

    def test_unknown_during_collection_is_not_rolled_back(self):
        def mutate(store, job_id):
            with store.transaction() as con:
                runtime.mark_unknown(store, con, store.get(con, job_id), 'Fixture ownership uncertainty')
        after, before = self.refused_worker(mutate)
        self.assertEqual(after, before)
        self.assertEqual(after['status'], 'Unknown')
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])

    def test_changed_claim_during_collection_is_not_overwritten(self):
        def mutate(store, job_id):
            with store.transaction() as con:
                row = store.get(con, job_id)
                row.update(claim='replacement-identity', reason='Replacement owns this row')
                store.save(con, row)
        after, before = self.refused_worker(mutate)
        self.assertEqual(after, before)
        self.assertEqual(after['claim'], 'replacement-identity')

    def test_user_pause_during_collection_restores_pending_without_launch(self):
        after, _ = self.refused_worker(lambda store, job_id: store.dispatch(False))
        self.assertEqual(after['status'], 'Queue')
        self.assertEqual(after['order'], 1)
        for key in ('worker', 'group', 'claim', 'claimed_at', 'started_at', 'finished_at'):
            self.assertIsNone(after[key])
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])

    def test_pending_head_with_releasing_worker_lock_is_not_claimed(self):
        row = self.store.add(argv=['/bin/echo', 'synthetic'], cwd=self.directory, name='fixture')
        lock = self.store.root / 'attempts' / row['attempt_id'] / 'worker.lock'
        with ownership.exclusive(lock), patch.object(runtime, 'dispatch_authority', return_value=True), \
                patch.object(runtime, 'detached') as launched:
            self.assertTrue(runtime.tick(self.store))
            launched.assert_not_called()
            with self.store.transaction() as con:
                after = self.store.get(con, row['id'])
            self.assertEqual(after, row)
        self.assertFalse(ownership.lock_held(lock))

    def test_unreadable_lock_is_not_verified_managed_ownership(self):
        engine = fixtures.process(11, group=10, session=10)
        state = {'jobs': [{'status': 'Run', 'group': fixtures.owner(10),
                           'worker': fixtures.owner(20), 'attempt_id': 'fixture'}]}
        with patch.object(activity, 'owner_alive', return_value=True), \
                patch.object(activity, 'lock_held', side_effect=PermissionError('fixture inaccessible lock')) as lock:
            result = fixtures.ActivityTests.collect(self, {11: engine}, state=state)
        lock.assert_called_once_with(self.store.root / 'attempts' / 'fixture' / 'worker.lock', strict=True)
        self.assertEqual(result['coverage']['managed'], 0)
        self.assertFalse(result['coverage']['complete'])
        self.assertFalse(activity.permits(result))
        self.assertIn('ownership unresolved', result['coverage']['warnings'][0])

    def test_strict_lock_access_error_does_not_become_positive_evidence(self):
        path = Path(self.directory) / 'inaccessible.lock'
        path.touch()
        with patch.object(Path, 'open', side_effect=PermissionError('fixture access denied')):
            self.assertTrue(ownership.lock_held(path))
            with self.assertRaises(PermissionError):
                ownership.lock_held(path, strict=True)

    def test_admission_projection_preserves_existing_executable_preflight(self):
        self.store.add(argv=['/definitely/missing/cmw-fixture'], cwd=self.directory, name='missing')
        state = self.store.snapshot()
        state['controller'].update(online=True, stale=False, dispatch=True)
        result = activity.admission(state, clear())
        self.assertFalse(result['permitted'])
        self.assertIn('Executable is missing', result['reason'])
