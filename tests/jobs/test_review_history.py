"""Selected review evidence stays bounded independently of unrelated history."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.lifecycle import read_job
from cmw.jobs.review_worker import inspect
from cmw.jobs.store import JobsError, Store
from cmw.periodic.vasp.result_execution import inspect_execution_snapshot
from tests.jobs.test_catalogue_store import populate_history


class SelectedReviewHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-review-history-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def fixture(self, count):
        directory = self.root / str(count)
        directory.mkdir()
        store = Store(directory / 'state')
        job = populate_history(store, directory, count)
        store.enroll_queue(uuid4().hex, 'synthetic-review-host')
        request = {'settings': {}, 'state': str(store.root), 'origin': {},
                   'job_id': job['id'], 'attempt_id': job['attempt_id'], 'review_id': uuid4().hex}
        return store, job, request

    def test_unrelated_history_cannot_turn_missing_receipt_into_operational_failure(self):
        for count in (1000, 10000):
            with self.subTest(count=count):
                store, _, request = self.fixture(count)
                before = store.path.read_bytes()
                record = inspect(request)
                self.assertEqual(record.status, 'COMPLETE')
                execution = json.loads(record.evidence_json)['execution']
                self.assertEqual(next(d.status for d in record.domains if d.name == 'operational'),
                                 'UNKNOWN', execution['findings'])
                self.assertIsNone(execution['operational_success'])
                self.assertEqual(store.path.read_bytes(), before)

    def test_selected_snapshot_retains_owner_controller_and_exact_attempt_without_history_scan(self):
        store, job, request = self.fixture(10000)
        selected, controller = read_job(store, job['id'])
        owner = store.queue_identity()
        captured = []

        def observe(snapshot, *args, **kwargs):
            captured.append(deepcopy(snapshot))
            return inspect_execution_snapshot(snapshot, *args, **kwargs)

        with patch.object(Store, 'snapshot', side_effect=AssertionError('full queue snapshot read')):
            with patch('cmw.periodic.vasp.result_execution.inspect_execution_snapshot', side_effect=observe):
                record = inspect(request)
        self.assertEqual(len(captured), 1)
        snapshot = captured[0]
        self.assertEqual(snapshot['jobs'], [selected])
        self.assertEqual(snapshot['controller'], controller)
        self.assertEqual(snapshot['schema'], controller['schema'])
        self.assertEqual(snapshot['state_directory'], str(store.root))
        self.assertEqual(snapshot['queue_identity'], owner)
        self.assertEqual(snapshot['scope'], 'selected-attempt')
        self.assertGreater(snapshot['observed_at'], 0)
        self.assertEqual(json.loads(record.evidence_json)['execution']['snapshot_scope'], 'selected-attempt')

    def test_selected_snapshot_rechecks_attempt_before_inspection(self):
        store, job, request = self.fixture(1)
        selected, controller = read_job(store, job['id'])
        changed = {**selected, 'attempt_id': uuid4().hex}
        with patch('cmw.jobs.review_worker.read_job', return_value=(changed, controller)):
            with self.assertRaisesRegex(JobsError, 'attempt changed'):
                inspect(request)
