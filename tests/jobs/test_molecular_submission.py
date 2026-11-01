"""Shared reviewed-submission boundaries; scientific behavior lives in engine tests."""
from copy import deepcopy
from pathlib import Path
import json
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs import molecular
from cmw.jobs.runner_assets import file_identity, runner_dependencies, runner_path
from cmw.jobs.store import JobsError, Store


class MolecularSubmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-reviewed-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.input = self.root / 'reviewed.txt'
        self.input.write_text('reviewed synthetic source\n')
        self.spec = {'schema': 1, 'request_id': str(uuid4()), 'engine': 'ORCA', 'name': 'synthetic review',
                     'intent': 'held', 'resources': {'cpus': 1, 'memory_gib': 1, 'mpi_ranks': 1, 'threads_per_rank': 1},
                     'workdir': str(self.root / 'prepared'), 'runtime': {}, 'scientific': {}}
        self.store = Store(self.root / 'state')
        self.engine = SimpleNamespace(check=self.checked, prepare=lambda spec, checked: checked['plan'])
        self.patch = patch.object(molecular, '_engine', return_value=self.engine)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def checked(self, spec):
        return {'status': 'READY', 'findings': [], 'plan': {
            'argv': [sys.executable, '-c', "print('harmless')"], 'env': {}, 'cwd': spec['workdir'],
            'progress_sources': [], 'dependencies': [file_identity(self.input)]}}

    def test_check_and_preview_create_no_workdir_or_store(self):
        self.assertEqual(molecular.check(self.spec)['status'], 'READY')
        self.assertEqual(molecular.submit(self.store, self.spec, preview=True)['status'], 'READY')
        self.assertFalse(self.store.root.exists())
        self.assertFalse(Path(self.spec['workdir']).exists())

    def test_hold_idempotence_and_request_conflict(self):
        first = molecular.submit(self.store, self.spec)
        self.assertEqual(first['job']['status'], 'Hold')
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])
        repeated = molecular.submit(self.store, self.spec)
        self.assertTrue(repeated['replayed'])
        self.assertEqual(repeated['job']['attempt_id'], first['job']['attempt_id'])
        with self.assertRaisesRegex(JobsError, 'different molecular'):
            molecular.submit(self.store, {**self.spec, 'name': 'changed'})
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)

    def test_dependency_hashes_only_at_actual_admission(self):
        job = molecular.submit(self.store, self.spec)['job']
        self.input.write_text('changed after registration\n')
        with patch.object(molecular, 'file_identity', side_effect=AssertionError('UI must not hash')):
            self.assertIsNone(molecular.launch_blocker(job))
        self.assertIn('dependency changed', molecular.launch_blocker(job, final=True))

    def test_execution_and_preparation_identity_cannot_be_replaced(self):
        job = molecular.submit(self.store, self.spec)['job']
        for field, value in [('argv', ['/bin/echo', 'substituted']), ('resources', {**job['resources'], 'cpus': 2})]:
            wrong = {**job, field: value}
            self.assertIn('differs', molecular.launch_blocker(wrong, final=True))
        old = Path(self.spec['workdir'])
        old.rename(old.with_name('retained'))
        old.mkdir(mode=0o700)
        self.assertIn('directory identity changed', molecular.launch_blocker(job))

    def test_binding_is_immutable_across_lifecycle_saves(self):
        job = molecular.submit(self.store, self.spec)['job']
        with self.store.transaction() as con:
            altered = self.store.get(con, job['id'])
            altered['molecular_submission']['spec']['name'] = 'tampered'
            with self.assertRaisesRegex(JobsError, 'metadata is immutable'):
                self.store.save(con, altered)
        with self.store.transaction() as con:
            old_caller = self.store.get(con, job['id'])
            del old_caller['molecular_submission']
            self.store.save(con, old_caller)
            self.assertEqual(self.store.get(con, job['id'])['molecular_submission'], job['molecular_submission'])

    def test_malformed_resource_and_input_envelope_do_not_create_state(self):
        invalid = [dict(self.spec, intent='run'), dict(self.spec, unexpected=True),
                   dict(self.spec, resources={**self.spec['resources'], 'cpus': True}),
                   dict(self.spec, resources={**self.spec['resources'], 'mpi_ranks': 2})]
        for spec in invalid:
            with self.subTest(spec=spec):
                self.assertEqual(molecular.check(spec)['status'], 'ERROR')
        self.assertFalse(self.store.root.exists())

    def test_preparation_does_not_overwrite_existing_work(self):
        root = Path(self.spec['workdir']); root.mkdir(mode=0o700)
        existing = root / 'user-work'; existing.write_text('preserve')
        with self.assertRaisesRegex(JobsError, 'existing evidence'):
            molecular.submit(self.store, self.spec)
        self.assertEqual(existing.read_text(), 'preserve')
        self.assertFalse(self.store.path.exists())

    def test_terminal_inspection_exposes_record_without_acceptance_from_done(self):
        job = molecular.submit(self.store, self.spec)['job']
        output = Path(self.spec['workdir']) / 'result.json'
        job['molecular_submission']['plan']['result_path'] = str(output)
        job.update(status='Done', exit_code=0)
        self.assertEqual(molecular.inspect_result(job)['status'], 'UNAVAILABLE')
        output.write_text(json.dumps({'scientific': {'status': 'INVALID'}, 'reusable': False}))
        inspected = molecular.inspect_result(job)
        self.assertEqual(inspected['status'], 'RECORDED')
        self.assertEqual(inspected['jobs_process_outcome'], 'Done')
        self.assertEqual(inspected['record']['scientific']['status'], 'INVALID')
        self.assertFalse(inspected['record']['reusable'])
        self.assertIn('neither finalizes nor revalidates', inspected['qualification'])
        job['status'] = 'Run'
        self.assertIsNone(molecular.inspect_result(job)['record'])

    def test_runner_closure_and_bounded_nonregular_identity(self):
        paths = runner_dependencies('workflows/run_opt_freq_sp.sh')
        self.assertTrue(runner_path('orca/run_orca.sh').is_file())
        self.assertEqual(len(paths), 3)
        self.assertTrue(any(item['path'].endswith('runtime_python.sh') for item in paths))
        with self.assertRaisesRegex(ValueError, 'at most'):
            file_identity(self.input, max_bytes=1)
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            file_identity(fifo)
