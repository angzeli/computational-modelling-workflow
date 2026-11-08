"""Owner service exercised with real benign native receipts and no engines."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.store import JobsError
from cmw.jobs import runtime
from cmw.jobs.successor import SuccessorService, _closure, launch_blocker as successor_launch_blocker
from cmw.jobs.successor_models import SuccessorUnverifiable
from cmw.jobs.runner_assets import file_identity
from tests.jobs.successor_fixtures import make_vasp_fixture, successor_case


class SuccessorServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-successor-service-')
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture = make_vasp_fixture(Path(temporary.name) / 'owner')

    def setUp(self):
        self.store = self.fixture['store']
        self.parent = self.fixture['job']
        self.service = SuccessorService(self.store)
        self.spec, self.path = successor_case(self.fixture, uuid4().hex)

    def plan(self):
        return self.service.plan(self.parent['id'], str(self.path), self.parent['attempt_id'])

    def materialize(self):
        plan = self.plan()
        self.assertEqual(plan.status, 'READY', plan.payload_json)
        result = self.service.materialize(self.parent['id'], str(self.path), plan.plan_digest, self.parent['attempt_id'])
        record = Path(json.loads(result.payload_json)['record_path'])
        return plan, result, record

    def test_plan_is_read_only_reproducible_and_geometry_remains_unaccepted(self):
        database = self.store.path.read_bytes()
        source = {p: p.read_bytes() for p in self.fixture['run'].iterdir()}
        first, second = self.plan(), self.plan()
        self.assertEqual(first.status, 'READY', first.payload_json)
        self.assertEqual(first.plan_digest, second.plan_digest)
        self.assertEqual(database, self.store.path.read_bytes())
        self.assertEqual(source, {p: p.read_bytes() for p in self.fixture['run'].iterdir()})
        self.assertFalse(Path(self.spec['adapter']['output']).exists())
        payload = json.loads(first.payload_json)
        self.assertEqual(payload['runtime']['env'], {'CMW_JOBS_OWN_SESSION': '1', 'OMP_NUM_THREADS': '1'})
        self.assertTrue(payload['closure']['closed'])
        self.assertEqual(payload['adapter']['source']['scientific_acceptance'], 'not established')
        self.assertEqual(self.fixture['cleanup']['remaining_owned_processes'], [])

    def test_preparation_resources_and_overlay_must_match_supported_runner(self):
        original = json.loads(Path(self.spec['adapter']['preparation_spec']).read_text())
        preparation_path = self.path.parent / 'reviewed-preparation.json'
        database = self.store.path.read_bytes()
        cases = [
            ('cpus', {'cpus': 2}, {}, 'resources differ'),
            ('ranks', {'cpus': 2, 'mpi_ranks': 2}, {'cpus': 2}, 'resources differ'),
            ('threads', {'cpus': 2, 'threads_per_rank': 2}, {'cpus': 2}, 'resources differ'),
            ('ncore', {'cpus': 2, 'mpi_ranks': 2, 'incar': {'NCORE': 2, 'KPAR': 1}},
             {'cpus': 2, 'mpi_ranks': 2}, 'overlay differs'),
            ('kpar', {'cpus': 2, 'mpi_ranks': 2, 'incar': {'NCORE': 1, 'KPAR': 2}},
             {'cpus': 2, 'mpi_ranks': 2}, 'overlay differs'),
            ('missing-overlay', {'incar': {}}, {}, 'overlay differs'),
            ('unsupported-threads', {'cpus': 2, 'threads_per_rank': 2},
             {'cpus': 2, 'threads_per_rank': 2}, 'one explicit OpenMP thread'),
        ]
        for name, prepared_changes, resource_changes, message in cases:
            with self.subTest(name=name):
                preparation = deepcopy(original)
                preparation['runtime'].update(prepared_changes)
                preparation_path.write_text(json.dumps(preparation))
                specification = deepcopy(self.spec)
                specification['adapter']['preparation_spec'] = str(preparation_path)
                specification['resources'].update(resource_changes)
                self.path.write_text(json.dumps(specification))
                with self.assertRaisesRegex(JobsError, message):
                    self.plan()
                self.assertFalse(Path(specification['adapter']['output']).exists())
                self.assertFalse(Path(specification['runtime']['run_directory']).exists())
        self.assertEqual(database, self.store.path.read_bytes())

    def test_materialize_then_lost_response_register_produces_one_held_attempt(self):
        before = self.store.snapshot()
        plan, result, record = self.materialize()
        self.assertEqual(result.status, 'MATERIALIZED')
        output = Path(self.spec['adapter']['output'])
        self.assertEqual({p.name for p in output.iterdir()}, {'INCAR', 'POSCAR', 'POTCAR', 'KPOINTS'})
        declared_runtime = json.loads(plan.payload_json)['runtime']
        native = Path(declared_runtime['argv'][declared_runtime['argv'].index('--output') + 1])
        self.assertFalse(native.exists())
        self.assertEqual(before['jobs'], self.store.snapshot()['jobs'])
        request = uuid4().hex
        first = self.service.register(self.parent['id'], str(record), plan.plan_digest, request, self.parent['attempt_id'])
        replay = SuccessorService(self.store).register(self.parent['id'], str(record), plan.plan_digest,
                                                      request, self.parent['attempt_id'])
        a, b = json.loads(first.payload_json), json.loads(replay.payload_json)
        self.assertFalse(a['replayed'])
        self.assertTrue(b['replayed'])
        self.assertEqual(a['child'], b['child'])
        self.assertEqual(a['child']['status'], 'Hold')
        self.assertNotEqual(a['child']['attempt_id'], self.parent['attempt_id'])
        self.assertEqual(len(self.store.snapshot()['jobs']), len(before['jobs']) + 1)
        self.assertFalse(self.store.snapshot()['controller']['online'])
        self.assertFalse(native.exists())
        child = next(job for job in self.store.snapshot()['jobs'] if job['id'] == a['child']['job_id'])
        self.assertIsNone(successor_launch_blocker(child))
        self.assertEqual(runtime.launch_blocker(child, final=True), '')
        with self.assertRaises(JobsError):
            self.service.register(self.parent['id'], str(record), plan.plan_digest,
                                  uuid4().hex, self.parent['attempt_id'])
        self.assertEqual(len(self.store.snapshot()['jobs']), len(before['jobs']) + 1)
        prepared = output / 'INCAR'
        original = prepared.read_bytes()
        try:
            prepared.write_bytes(original + b'\n# changed after held registration\n')
            self.assertTrue(runtime.launch_blocker(child, final=True))
        finally:
            prepared.write_bytes(original)

    def test_mutated_source_cannot_materialize_an_approved_plan(self):
        first = self.plan()
        source = self.fixture['run'] / 'OUTCAR'
        original = source.read_bytes()
        try:
            source.write_bytes(original + b'\n')
            with self.assertRaisesRegex(JobsError, 'changed'):
                self.service.materialize(self.parent['id'], str(self.path), first.plan_digest, self.parent['attempt_id'])
            self.assertFalse(Path(self.spec['adapter']['output']).exists())
        finally:
            source.write_bytes(original)

    def test_active_and_unknown_states_block_before_engine_inspection(self):
        with self.store.transaction() as con:
            original = deepcopy(self.store.get(con, self.parent['id']))
        try:
            for status in ('Run', 'Unknown'):
                with self.store.transaction() as con:
                    current = self.store.get(con, self.parent['id'])
                    current['status'] = status
                    self.store.save(con, current)
                with patch('cmw.jobs.successor_vasp.preview_vasp', side_effect=AssertionError('Blocked parent opened science')):
                    result = self.plan()
                self.assertEqual(result.status, 'BLOCKED')
                self.assertIs(json.loads(result.payload_json)['closure']['closed'], False)
        finally:
            with self.store.transaction() as con:
                self.store.save(con, original)

    def test_forged_dependency_list_and_copied_record_cannot_register(self):
        plan, _, record = self.materialize()
        original = record.read_bytes()
        unrelated = record.parent / 'unrelated.txt'
        unrelated.write_text('Not a prepared input')
        forged = json.loads(original)
        forged['prepared'] = {'unrelated': file_identity(unrelated)}
        try:
            record.write_text(json.dumps(forged))
            with self.assertRaises(JobsError):
                self.service.register(self.parent['id'], str(record), plan.plan_digest, uuid4().hex, self.parent['attempt_id'])
        finally:
            record.write_bytes(original)
        copied = record.parent / 'copied-successor.json'
        shutil.copyfile(record, copied)
        with self.assertRaises(JobsError):
            self.service.register(self.parent['id'], str(copied), plan.plan_digest, uuid4().hex, self.parent['attempt_id'])

    def test_queue_unavailable_during_vasp_profile_readiness_stays_retryable(self):
        unavailable = {'ready': False, 'queue_read_unavailable': True, 'errors': ['research: database locked']}
        with patch('cmw.nodes.identity.check_node', return_value=unavailable):
            plan = self.plan()
            self.assertEqual(plan.status, 'UNVERIFIABLE')
            self.assertIn('database locked', json.loads(plan.payload_json)['reason'])
        plan, _, record = self.materialize()
        request_id = uuid4().hex
        with patch('cmw.nodes.identity.check_node', return_value=unavailable):
            with self.assertRaisesRegex(SuccessorUnverifiable, 'profile queue temporarily unreadable'):
                self.service.register(self.parent['id'], str(record), plan.plan_digest, request_id)
        self.assertIsNone(self.store.request_lookup(request_id))
        self.assertEqual(self.service.register(self.parent['id'], str(record), plan.plan_digest, request_id).status, 'REGISTERED')

    def test_locked_parent_is_retryable_at_entry_planning_and_registration(self):
        plan, _, record = self.materialize()
        request_id = uuid4().hex

        def lock_during_closure(*args):
            # The initial owner/target read has succeeded. Only the live
            # comparison for the already selected parent is unavailable.
            with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
                writer.execute('BEGIN EXCLUSIVE')
                return _closure(*args)

        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            with self.assertRaisesRegex(SuccessorUnverifiable, 'Parent queue temporarily unreadable'):
                self.plan()
        with patch('cmw.jobs.successor._closure', side_effect=lock_during_closure):
            unavailable = self.plan()
            self.assertEqual(unavailable.status, 'UNVERIFIABLE')
            closure = json.loads(unavailable.payload_json)['closure']
            self.assertIsNone(closure['closed'])
            self.assertIn('parent queue temporarily unreadable', closure['reason'])
            with self.assertRaisesRegex(SuccessorUnverifiable, 'unverifiable, retry') as refused:
                self.service.register(self.parent['id'], str(record), plan.plan_digest, request_id)
            self.assertEqual(refused.exception.code, 'SUCCESSOR_UNVERIFIABLE')
        self.assertIsNone(self.store.request_lookup(request_id))
        self.assertEqual(json.loads(record.read_text())['status'], 'MATERIALIZED')
        registered = self.service.register(self.parent['id'], str(record), plan.plan_digest, request_id)
        self.assertEqual(registered.status, 'REGISTERED')
        with self.assertRaises(JobsError) as missing:
            self.service._parent(self.parent['id'] + 100, None)
        self.assertNotIsInstance(missing.exception, SuccessorUnverifiable)

    def test_accepted_geometry_uses_genuine_native_receipt_and_existing_finalizer(self):
        from cmw.core.provenance import stable_hash
        from cmw.periodic.vasp.results import inspect_result
        from cmw.periodic.vasp.result_finalization import finalize_result
        from tests.periodic.vasp.handoff_case import wait_for_fixture_sources
        from tests.periodic.vasp.test_result_policy import policy
        fixture = make_vasp_fixture(self.fixture['root'] / ('accepted-' + uuid4().hex), ionic_stop=True)
        snapshot = fixture['root'] / 'saved-native-snapshot.json'
        snapshot.write_text(json.dumps(fixture['store'].snapshot()))
        evidence = inspect_result(fixture['run'])
        selected = policy(relaxation=True)
        policy_path = fixture['root'] / 'explicit-policy.json'
        policy_path.write_text(json.dumps(selected))
        binding = {'schema_version': 1, 'intent': 'retrospective',
            'producing_calculation': 'invented native-shaped output under genuine benign receipt',
            'snapshot_id': evidence['snapshot_id'], 'segment_id': evidence['selected_segment']['segment_id'],
            'policy_id': stable_hash(selected), 'source_identities': {s['role']: s['sha256'] for s in evidence['sources']},
            'artifact_roles': selected['artifact_roles'], 'parents': [],
            'execution': {'snapshot': str(snapshot), 'receipt': str(fixture['receipt']),
                'job_id': fixture['job']['id'], 'attempt_id': fixture['job']['attempt_id'],
                'source_identities': {'jobs_snapshot': file_identity(snapshot)['sha256'],
                                      'jobs_receipt': file_identity(fixture['receipt'])['sha256']}}}
        binding_path = fixture['root'] / 'explicit-binding.json'
        binding_path.write_text(json.dumps(binding))
        wait_for_fixture_sources([*fixture['run'].iterdir(), snapshot, policy_path,
                                  binding_path, fixture['receipt']])
        finalized = finalize_result(fixture['run'], policy_path=policy_path, spec_path=binding_path,
                                    scratch_root=fixture['root'] / 'scratch', record_directory='accepted-record')
        spec = fixture['spec']
        spec['adapter'].update(source_mode='accepted', accepted_record=finalized['publication']['record_path'],
                               accepted_artifact_id=finalized['artifact_bundle']['artifacts'][0]['artifact_id'])
        fixture['spec_path'].write_text(json.dumps(spec))
        wait_for_fixture_sources([fixture['spec_path'], Path(finalized['publication']['record_path'])])
        service = SuccessorService(fixture['store'])
        plan = service.plan(fixture['job']['id'], str(fixture['spec_path']), fixture['job']['attempt_id'])
        self.assertEqual(plan.status, 'READY', plan.payload_json)
        self.assertEqual(json.loads(plan.payload_json)['adapter']['source']['qualification'], 'accepted-periodic-structure')
        materialized = service.materialize(fixture['job']['id'], str(fixture['spec_path']), plan.plan_digest,
                                           fixture['job']['attempt_id'])
        self.assertEqual(materialized.status, 'MATERIALIZED')
        self.assertEqual(fixture['cleanup']['remaining_owned_processes'], [])

    def test_identical_inputs_do_not_conflate_distinct_genuine_parent_attempts(self):
        other = make_vasp_fixture(self.fixture['root'] / ('other-' + uuid4().hex))
        first = json.loads(self.plan().payload_json)
        second = SuccessorService(other['store']).plan(other['job']['id'], str(other['spec_path']), other['job']['attempt_id'])
        self.assertEqual(second.status, 'READY', second.payload_json)
        second = json.loads(second.payload_json)
        self.assertNotEqual(first['parent'], second['parent'])
        self.assertNotEqual(first['adapter']['digest'], second['adapter']['digest'])
        self.assertEqual(first['adapter']['preparation']['prepared_inputs'], second['adapter']['preparation']['prepared_inputs'])
        self.assertEqual(other['cleanup']['remaining_owned_processes'], [])


if __name__ == '__main__':
    unittest.main()
