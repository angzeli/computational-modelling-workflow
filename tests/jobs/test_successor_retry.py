"""Resume the same published successor after loss of live parent evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs import successor_vasp
from cmw.jobs.runner_assets import file_identity
from cmw.jobs.store import JobsError
from cmw.jobs.successor import SuccessorService
from cmw.jobs.successor_models import SuccessorUnverifiable
from tests.jobs.successor_fixtures import make_vasp_fixture, successor_case


class SuccessorRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-successor-retry-')
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture = make_vasp_fixture(Path(temporary.name) / 'owner')

    def setUp(self):
        self.store = self.fixture['store']
        self.job = self.fixture['job']
        self.spec, self.spec_path = successor_case(self.fixture, uuid4().hex)
        self.output = Path(self.spec['adapter']['output'])
        self.service = SuccessorService(self.store)
        plan = self.service.plan(self.job['id'], str(self.spec_path), self.job['attempt_id'])
        self.assertEqual(plan.status, 'READY', plan.payload_json)
        self.digest = plan.plan_digest
        publication = json.loads(plan.payload_json)['adapter']['publication_plan']
        self.record = Path(publication['record_directory']) / 'successor.json'
        self.initial_jobs = self.store.snapshot()['jobs']

    def materialize(self):
        # Use an independent service for every retry, as separate CLI requests do.
        return SuccessorService(self.store).materialize(
            self.job['id'], str(self.spec_path), self.digest, self.job['attempt_id'])

    def register(self):
        return self.service.register(self.job['id'], str(self.record), self.digest,
                                     uuid4().hex, self.job['attempt_id'])

    def after_publication(self, action):
        publish = successor_vasp.materialize_vasp

        def publish_then_act(*args, **kwargs):
            result = publish(*args, **kwargs)
            self.assertTrue((self.output / 'INCAR').is_file())
            action()
            return result

        with patch.object(successor_vasp, 'materialize_vasp', side_effect=publish_then_act):
            return self.materialize()

    def pending(self):
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
            result = self.after_publication(lambda: writer.execute('BEGIN EXCLUSIVE'))
        self.assertEqual(result.status, 'PENDING_VERIFICATION', result.payload_json)
        self.assertEqual(Path(json.loads(result.payload_json)['record_path']), self.record)
        record = json.loads(self.record.read_text())
        self.assertEqual(record['status'], 'PENDING_VERIFICATION')
        self.assertEqual(record['plan_digest'], self.digest)
        for name in ('INCAR', 'KPOINTS', 'POSCAR', 'POTCAR'):
            self.assertEqual(record['prepared'][name]['sha256'], file_identity(self.output / name)['sha256'])
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def contradict_exit_receipt(self):
        # A readable row now contradicts the genuine disposable attempt's exit receipt.
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            original_exit = job['exit_code']
            self.assertEqual(original_exit, 0)
            job['exit_code'] = 17
            self.store.save(con, job)

        def restore():
            with self.store.transaction() as con:
                original = self.store.get(con, self.job['id'])
                original['exit_code'] = original_exit
                self.store.save(con, original)

        self.addCleanup(restore)

    def assert_incomplete(self):
        self.assertEqual(json.loads(self.record.read_text())['status'], 'INCOMPLETE')
        with self.assertRaises(JobsError):
            self.register()
        self.assertEqual(len(self.store.snapshot()['jobs']), len(self.initial_jobs))

    def test_pending_queue_recovery_completes_same_publication_then_registers_once(self):
        self.pending()
        # Recovered access alone must not release a still-pending publication.
        with self.assertRaises(SuccessorUnverifiable):
            self.register()
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)
        original = {path.name: path.stat().st_ino for path in self.output.iterdir()}
        result = self.materialize()
        self.assertEqual(result.status, 'MATERIALIZED')
        self.assertEqual(Path(json.loads(result.payload_json)['record_path']), self.record)
        self.assertEqual(json.loads(self.record.read_text())['status'], 'MATERIALIZED')
        self.assertEqual(original, {path.name: path.stat().st_ino for path in self.output.iterdir()})
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)
        registered = self.register()
        self.assertEqual(registered.status, 'REGISTERED')
        self.assertEqual(json.loads(registered.payload_json)['child']['status'], 'Hold')
        self.assertEqual(len(self.store.snapshot()['jobs']), len(self.initial_jobs) + 1)

    def test_contradiction_immediately_after_publication_is_incomplete(self):
        with self.assertRaises(JobsError):
            self.after_publication(self.contradict_exit_receipt)
        self.assert_incomplete()

    def test_contradiction_when_resuming_pending_is_incomplete(self):
        self.pending()
        self.contradict_exit_receipt()
        with self.assertRaises(JobsError):
            self.materialize()
        self.assert_incomplete()

    def test_bytes_changed_before_initial_post_publication_check_are_incomplete(self):
        def alter():
            with (self.output / 'INCAR').open('ab') as out:
                out.write(b'\n# not the approved prepared input\n')

        with self.assertRaises(JobsError):
            self.after_publication(alter)
        self.assert_incomplete()

    def test_altered_published_bytes_are_incomplete_even_if_queue_is_still_locked(self):
        self.pending()
        incar = self.output / 'INCAR'
        original = incar.read_bytes()
        incar.write_bytes(original + b'\n# altered after publication\n')
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(JobsError):
                self.materialize()
        self.assert_incomplete()
        # Restoring the bytes must not resurrect a terminal contradiction.
        incar.write_bytes(original)
        with self.assertRaises(JobsError):
            self.materialize()
        self.assert_incomplete()

    def test_copied_pending_record_directory_cannot_be_promoted(self):
        self.pending()
        location = self.record.parent
        original = location.with_name(location.name + '-original')
        location.rename(original)
        shutil.copytree(original, location)
        with self.assertRaisesRegex(JobsError, 'publication directory identity changed'):
            self.materialize()
        self.assertEqual(json.loads(self.record.read_text())['status'], 'PENDING_VERIFICATION')
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def test_legacy_completed_record_keeps_original_registration_route(self):
        self.materialize()
        record = json.loads(self.record.read_text())
        for field in ('working_identity', 'parent_selector', 'parent_display_id', 'publication_directory'):
            record.pop(field)
        self.record.write_text(json.dumps(record))
        with self.assertRaisesRegex(JobsError, 'does not support materialization replay'):
            self.materialize()
        self.assertEqual(self.register().status, 'REGISTERED')

    def test_two_retries_share_one_promotion(self):
        self.pending()
        ready = Barrier(2, timeout=15)

        def retry():
            ready.wait()
            result = self.materialize()
            self.assertEqual(result.status, 'MATERIALIZED')
            payload = json.loads(result.payload_json)
            self.assertEqual(Path(payload['record_path']), self.record)
            return payload['replayed']

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(retry) for _ in range(2)]
            self.assertCountEqual([f.result(timeout=30) for f in futures], [False, True])
        self.assertEqual(json.loads(self.record.read_text())['status'], 'MATERIALIZED')
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)
