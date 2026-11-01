"""Small explicit review oracle and retained durable-registration oracle."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from cmw.jobs.review import ReviewService
from cmw.jobs.store import JobsError, Store
from contextlib import contextmanager
from unittest.mock import patch

from cmw.jobs.successor import SuccessorService
from tests.nodes.test_registration import RegistrationFixture
from tests.jobs.successor_fixtures import make_vasp_fixture, successor_case
from tests.periodic.vasp.handoff_case import wait_for_fixture_sources


class WrongAttemptReviewTests(unittest.TestCase):
    def test_selected_attempt_is_required_before_review(self):
        with tempfile.TemporaryDirectory(prefix='cmw-mutation-review-') as temporary:
            root = Path(temporary).resolve()
            store = Store(root / 'state')
            job = store.add(argv=['/usr/bin/true'], cwd=root, name='held review identity fixture',
                            engine='Command', hold=True)
            def projection():
                with store.read_transaction() as con:
                    return {'job': Store._job_control_state(store.get(con, job['id'])),
                            'control': Store.control(con)}
            before = projection()
            service = ReviewService(store)
            try:
                current = service.preview(job['id'], job['attempt_id'])
                self.assertEqual(current.attempt_id, job['attempt_id'])
                self.assertTrue(all(domain.status == 'NOT ASSESSED' for domain in current.domains))
                with self.assertRaisesRegex(JobsError, 'attempt changed'):
                    service.preview(job['id'], uuid4().hex)
                self.assertEqual(projection(), before)
            finally:
                service.close()


class DurableAssociationTests(RegistrationFixture, unittest.TestCase):
    def durable_rows(self):
        # Open an independent managed read transaction: no client response/cache.
        with Store(self.store.root).read_transaction() as con:
            jobs = [json.loads(row[0]) for row in con.execute('SELECT data FROM jobs')]
            present = con.execute("SELECT 1 FROM sqlite_master WHERE name='node_requests'").fetchone()
            receipts = [dict(zip(('request_id', 'operation', 'digest', 'result'), row))
                        for row in con.execute('SELECT request_id,operation,digest,result FROM node_requests')] if present else []
        return jobs, receipts

    def assert_exact_outcome(self, job):
        jobs, receipts = self.durable_rows()
        self.assertEqual(len(jobs), 1, 'ATOMIC_REQUEST_ASSOCIATION: one durable held job')
        self.assertEqual(len(receipts), 1, 'ATOMIC_REQUEST_ASSOCIATION: exact durable receipt required')
        receipt = receipts[0]
        self.assertEqual(receipt['request_id'], self.request['request_id'])
        self.assertEqual(receipt['operation'], 'submit')
        self.assertEqual(receipt['digest'], self.request['digest'])
        self.assertEqual(json.loads(receipt['result']), job)
        self.assertEqual(jobs[0]['attempt_id'], job['attempt_id'])
        self.assertEqual(job['status'], 'Hold')

    def test_durable_request_transaction_boundary(self):
        transaction = self.store.transaction
        observed = []

        @contextmanager
        def before_commit(**kwargs):
            with transaction(**kwargs) as con:
                yield con
                jobs = Store.rows(con)
                receipt = con.execute("SELECT 1 FROM sqlite_master WHERE name='node_requests'").fetchone()
                self.assertIsNotNone(receipt, 'ATOMIC_REQUEST_ASSOCIATION: receipt absent inside job transaction')
                receipt = con.execute('SELECT result FROM node_requests WHERE request_id=?',
                                      (self.request['request_id'],)).fetchone()
                self.assertIsNotNone(receipt, 'ATOMIC_REQUEST_ASSOCIATION: exact request absent before commit')
                self.assertEqual(json.loads(receipt[0]), jobs[0])
                self.assertEqual(self.durable_rows(), ([], []),
                                 'ATOMIC_REQUEST_ASSOCIATION: unpublished transaction leaked')
                observed.append(jobs[0]['attempt_id'])
                raise RuntimeError('injected immediately before commit')

        with patch.object(self.store, 'transaction', before_commit):
            with self.assertRaisesRegex(RuntimeError, 'immediately before commit'):
                self.add()
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.durable_rows(), ([], []),
                         'ATOMIC_REQUEST_ASSOCIATION: rollback retained job or receipt')
        self.assert_exact_outcome(self.add())


class SuccessorApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-mutation-successor-')
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture = make_vasp_fixture(Path(temporary.name) / 'owner')

    def setUp(self):
        self.store = self.fixture['store']
        self.parent = self.fixture['job']
        self.service = SuccessorService(self.store)
        self.spec, self.path = successor_case(self.fixture, uuid4().hex)
        # Settle generated scientific inputs BEFORE review; never refresh approval
        # after the injected source change or weaken production source identity.
        sources = [path for path in self.fixture['run'].iterdir() if path.is_file()]
        sources += [self.path, Path(self.spec['adapter']['preparation_spec'])]
        sources += list((self.fixture['root'] / 'library').glob('*/POTCAR'))
        wait_for_fixture_sources(sources, quiet_seconds=12, timeout=40)
        self.plan = self.service.plan(self.parent['id'], str(self.path), self.parent['attempt_id'])
        self.assertEqual(self.plan.status, 'READY', self.plan.payload_json)

    def materialize(self):
        return self.service.materialize(self.parent['id'], str(self.path), self.plan.plan_digest,
                                        self.parent['attempt_id'])

    def test_unchanged_reviewed_plan_publishes(self):
        result = self.materialize()
        self.assertEqual(result.status, 'MATERIALIZED')
        self.assertTrue(Path(json.loads(result.payload_json)['record_path']).is_file())

    def test_lost_registration_reply_recovers_original_attempt(self):
        result = self.materialize()
        record = json.loads(result.payload_json)['record_path']
        request_id = uuid4().hex
        transaction = self.store.transaction
        @contextmanager
        def lost_reply(**kwargs):
            with transaction(**kwargs) as con:
                yield con
            raise ConnectionError('injected endpoint disconnect after commit before reply')
        with patch.object(self.store, 'transaction', lost_reply):
            with self.assertRaisesRegex(ConnectionError, 'after commit before reply'):
                self.service.register(self.parent['id'], record, self.plan.plan_digest,
                                      request_id, self.parent['attempt_id'])
        del self.service
        fresh = Store(self.store.root)
        with fresh.read_transaction() as con:
            receipt = con.execute('SELECT result FROM node_requests WHERE request_id=?', (request_id,)).fetchone()
            original = json.loads(receipt[0])
            before_count = con.execute('SELECT count(*) FROM jobs').fetchone()[0]
        replay = SuccessorService(fresh).register(self.parent['id'], record, self.plan.plan_digest,
                                                 request_id, self.parent['attempt_id'])
        payload = json.loads(replay.payload_json)
        self.assertTrue(payload['replayed'])
        self.assertEqual(payload['child']['attempt_id'], original['attempt_id'])
        self.assertEqual(payload['child']['status'], 'Hold')
        with fresh.read_transaction() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], before_count)

    def test_changed_source_refuses_frozen_approval_before_publication(self):
        source = self.fixture['run'] / 'OUTCAR'
        original = source.read_bytes()
        with self.store.read_transaction() as con:
            before_jobs = Store.rows(con)
        try:
            source.write_bytes(original + b'\nsynthetic later native evidence\n')
            try:
                self.materialize()
            except JobsError as exc:
                self.assertEqual(str(exc), 'Successor plan changed or is blocked; review the new plan digest',
                                 'SUCCESSOR_SOURCE_APPROVAL: refusal must be the reviewed approval comparison')
            else:
                self.fail('SUCCESSOR_SOURCE_APPROVAL: changed source accepted the frozen reviewed digest')
            self.assertFalse(Path(self.spec['adapter']['output']).exists())
            self.assertFalse(Path(self.spec['runtime']['run_directory']).exists())
            with self.store.read_transaction() as con:
                self.assertEqual(Store.rows(con), before_jobs)
        finally:
            source.write_bytes(original)
