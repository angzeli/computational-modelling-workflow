"""Atomic target-local held registration through the real Store and SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from cmw.jobs import runtime
from cmw.jobs.store import JobsError, Store
from tests.jobs import test_activity as fixtures
from tests.jobs.isolated_runtime import install


class RegistrationFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-node-registration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.inputs = self.root / 'published'
        self.inputs.mkdir()
        self.store = Store(self.root / 'state')
        self.queue = self.store.enroll_queue(uuid4().hex, 'linux:synthetic-hardware')
        self.request = {'request_id': uuid4().hex, 'digest': 'a' * 64}
        self.metadata = {'schema': 1, 'node_id': self.queue['node_id'],
                         'queue_id': self.queue['queue_id'], **self.request,
                         'published_root': str(self.inputs), 'storage_id': uuid4().hex}

    def add(self, **changes):
        options = dict(argv=['/bin/echo', 'benign fixture'], cwd=self.inputs,
                       name='Transferred fixture', hold=False, remote_request=self.request,
                       node_submission=self.metadata)
        options.update(changes)
        return self.store.add(**options)

class RegistrationTests(RegistrationFixture, unittest.TestCase):
    def test_registration_forces_hold_and_preserves_original_outcome(self):
        job = self.add()
        self.assertEqual(job['status'], 'Hold')
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])
        self.assertEqual(self.store.request_lookup(self.request['request_id']), {
            'request_id': self.request['request_id'], 'operation': 'submit',
            'digest': self.request['digest'], 'result': job})
        self.store.change(job['id'], 'release')
        self.inputs.rmdir()
        self.assertEqual(self.add(), job)
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)
        self.assertEqual(self.store.snapshot()['jobs'][0]['status'], 'Queue')

    def test_concurrent_same_id_has_one_job_and_one_outcome(self):
        barrier = threading.Barrier(4)
        original = self.store.request_lookup
        def lookup(request_id):
            result = original(request_id)
            barrier.wait(timeout=5)
            return result
        with patch.object(self.store, 'request_lookup', lookup), ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda _: self.add(), range(4)))
        self.assertTrue(all(job == jobs[0] for job in jobs))
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)
        self.assertEqual([event['message'] for event in self.store.snapshot()['events']],
                         ['Queue explicitly enrolled for node access', 'Enqueued at order 1'])

    def test_concurrent_conflicting_semantics_are_refused(self):
        barrier = threading.Barrier(2)
        original = self.store.request_lookup
        def lookup(request_id):
            result = original(request_id)
            barrier.wait(timeout=5)
            return result
        def register(digest):
            try:
                return self.add(remote_request={**self.request, 'digest': digest},
                                node_submission={**self.metadata, 'digest': digest})
            except JobsError as exc:
                return str(exc)
        with patch.object(self.store, 'request_lookup', lookup), ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(register, ['a' * 64, 'b' * 64]))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertTrue(any(isinstance(item, str) and 'conflict' in item for item in outcomes))
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)

    def test_failure_immediately_before_commit_rolls_back_job_and_request(self):
        transaction = self.store.transaction
        @contextmanager
        def before_commit(**kwargs):
            with transaction(**kwargs) as con:
                yield con
                raise RuntimeError('injected before commit')
        with patch.object(self.store, 'transaction', before_commit):
            with self.assertRaisesRegex(RuntimeError, 'before commit'):
                self.add()
        self.assertEqual(self.store.snapshot()['jobs'], [])
        self.assertIsNone(self.store.request_lookup(self.request['request_id']))
        self.assertEqual(self.add()['status'], 'Hold')
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)

    def test_failure_after_commit_before_reply_returns_recorded_attempt(self):
        transaction = self.store.transaction
        @contextmanager
        def after_commit(**kwargs):
            with transaction(**kwargs) as con:
                yield con
            raise RuntimeError('injected lost reply after commit')
        with patch.object(self.store, 'transaction', after_commit):
            with self.assertRaisesRegex(RuntimeError, 'lost reply'):
                self.add()
        outcome = self.store.request_lookup(self.request['request_id'])
        self.assertIsNotNone(outcome)
        self.assertEqual(self.add(), outcome['result'])
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)

    def test_real_endpoint_like_process_exit_after_commit_retains_one_attempt(self):
        checkout = Path(__file__).resolve().parents[2]
        script = ("import json,os,sys;sys.path.insert(0,sys.argv[1]);"
                  "from cmw.jobs.store import Store;"
                  "Store(sys.argv[2]).add(**json.loads(sys.argv[3]));os._exit(73)")
        options = {'argv': ['/bin/echo', 'not executed'], 'cwd': str(self.inputs),
                   'name': 'Lost-reply registration', 'hold': False,
                   'remote_request': self.request, 'node_submission': self.metadata}
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(checkout / 'src'),
                                 str(self.store.root), json.dumps(options)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr.decode())
        outcome = self.store.request_lookup(self.request['request_id'])
        self.assertIsNotNone(outcome)
        self.assertEqual(self.add(), outcome['result'])
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)

    def test_metadata_cannot_be_added_replaced_or_rebound_after_registration(self):
        job = self.add()
        with self.store.transaction() as con:
            current = self.store.get(con, job['id'])
            del current['node_submission']
            current['reason'] = 'Existing lifecycle writer'
            self.store.save(con, current)
        self.assertEqual(self.store.snapshot()['jobs'][0]['node_submission'], self.metadata)
        for field, value in [('node_submission', {**self.metadata, 'published_root': '/wrong'}),
                             ('attempt_id', uuid4().hex), ('engine', 'VASP')]:
            with self.subTest(field=field), self.assertRaisesRegex(JobsError, 'immutable'):
                with self.store.transaction() as con:
                    current = self.store.get(con, job['id'])
                    current[field] = value
                    self.store.save(con, current)
        local = self.store.add(argv=['/unexecuted'], cwd=self.root, name='Local')
        with self.assertRaisesRegex(JobsError, 'immutable'):
            with self.store.transaction() as con:
                local['node_submission'] = self.metadata
                self.store.save(con, local)

    def test_invalid_or_wrong_queue_registration_creates_no_job(self):
        examples = [dict(remote_request=None), dict(node_submission=None),
                    dict(remote_request={**self.request, 'digest': 'invalid'}),
                    dict(node_submission={**self.metadata, 'schema': True}),
                    dict(node_submission={**self.metadata, 'queue_id': uuid4().hex}),
                    dict(node_submission={**self.metadata, 'digest': 'b' * 64}),
                    dict(node_submission={**self.metadata, 'large': 'x' * 65536})]
        for example in examples:
            with self.subTest(example=example), self.assertRaises(JobsError):
                self.add(**example)
        self.assertEqual(self.store.snapshot()['jobs'], [])
        self.assertIsNone(self.store.request_lookup(self.request['request_id']))

    def test_lookup_never_creates_or_writes_unenrolled_state(self):
        missing = Store(self.root / 'missing')
        self.assertIsNone(missing.request_lookup(uuid4().hex))
        self.assertFalse(missing.root.exists())
        before = self.store.path.read_bytes()
        self.assertIsNone(self.store.request_lookup(uuid4().hex))
        self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaises(JobsError):
            self.store.request_lookup('J1.1')

    def test_matching_request_identity_on_different_queues_is_independent(self):
        first = self.add()
        other = Store(self.root / 'other')
        identity = other.enroll_queue(self.queue['node_id'], self.queue['host_id'])
        second = other.add(argv=['/bin/echo'], cwd=self.inputs, name='Other queue',
                           remote_request=self.request,
                           node_submission={**self.metadata, 'queue_id': identity['queue_id']})
        self.assertEqual(first['display_id'], second['display_id'])
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        self.assertNotEqual(first['node_submission']['queue_id'], second['node_submission']['queue_id'])

    def test_ordinary_local_add_does_not_create_request_ledger(self):
        local = Store(self.root / 'local')
        job = local.add(argv=['/bin/echo'], cwd=self.root, name='Existing local behavior')
        self.assertEqual(job['status'], 'Queue')
        self.assertNotIn('node_submission', job)
        with local.transaction() as con:
            self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='node_requests'").fetchone())


class NodePreflightTests(RegistrationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(install().close)
        self.hook = Mock(return_value='Fixture required storage is unavailable')
        self.module = SimpleNamespace(launch_blocker=self.hook)
        replacement = patch.dict(sys.modules, {'cmw.nodes.submission': self.module})
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_ordinary_local_preflight_never_calls_node_hook(self):
        job = self.store.add(argv=['/bin/echo'], cwd=self.root, name='Local only')
        self.assertEqual(runtime.launch_blocker(job), '')
        self.hook.assert_not_called()

    def test_controller_preflight_refuses_before_detached_worker(self):
        job = self.add()
        self.store.change(job['id'], 'release')
        self.store.dispatch(True)
        with patch.object(runtime, 'dispatch_authority', return_value=True), \
                patch.object(runtime, 'detached') as detached:
            self.assertTrue(runtime.tick(self.store))
        detached.assert_not_called()
        self.hook.assert_called()
        with self.store.transaction() as con:
            current = self.store.get(con, 1)
        self.assertEqual(current['status'], 'Queue')
        self.assertIn('storage is unavailable', current['reason'])

    def test_worker_final_go_rechecks_node_preflight(self):
        row = self.add()
        with self.store.transaction() as con:
            row = self.store.get(con, row['id'])
            row.update(status='Starting', order=None, claim='fixture-claim', claimed_at=time.time())
            self.store.save(con, row)
            control = self.store.control(con)
            control.update(dispatch=True, owner=fixtures.owner(700))
            self.store.set_control(con, control)
        leader = SimpleNamespace(pid=987654, stdin=Mock(), wait=Mock(return_value=0))
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime.signal, 'signal'))
            stack.enter_context(patch.object(runtime.subprocess, 'Popen', return_value=leader))
            stack.enter_context(patch.object(runtime, 'identity', side_effect=lambda pid=None: fixtures.owner(pid or 700)))
            stack.enter_context(patch.object(runtime, 'group_exists', return_value=False))
            stack.enter_context(patch.object(runtime, 'owner_alive', return_value=True))
            stack.enter_context(patch.object(runtime, 'lock_held', return_value=True))
            runtime.worker(self.store, row['id'], 'fixture-claim')
        leader.stdin.write.assert_not_called()
        leader.stdin.close.assert_called()
        self.hook.assert_called_once()
        with self.store.transaction() as con:
            current = self.store.get(con, 1)
        self.assertEqual(current['status'], 'Queue')
        self.assertIn('storage is unavailable', current['reason'])
        self.assertIsNone(current['worker'])
        self.assertIsNone(current['started_at'])
