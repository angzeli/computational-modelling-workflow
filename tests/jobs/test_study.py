"""Actual study lifecycle in disposable enrolled queues; never a science engine."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.core.provenance import stable_hash
from cmw.jobs.study import StudyService, launch_blocker
from cmw.jobs.store import JobsError
from tests.jobs.study_fixtures import make_study, complete_study
from tests.periodic.vasp.result_case import write_json


def payload(result):
    return json.loads(result.payload_json)


class StudyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-study-service-')
        self.addCleanup(self.temp.cleanup)
        self.fixture = make_study(Path(self.temp.name).resolve() / 'fixture')
        self.root, self.store = self.fixture['root'], self.fixture['store']
        self.service = StudyService(self.store)
        self.manifest = self.fixture['manifest']

    def materialize(self):
        plan = self.service.plan(self.fixture['manifest_path'])
        prepared = self.service.materialize(self.fixture['manifest_path'], plan.plan_digest)
        self.assertEqual(prepared.status, 'MATERIALIZED', payload(prepared))
        return plan, payload(prepared)['record_path']

    def register(self, record, digest):
        return self.service.register(record, digest, self.manifest['study_id'])

    def test_actual_plan_prepare_held_register_complete_compare_and_exact_join(self):
        before = self.store.path.read_bytes()
        paths = sorted(self.root.rglob('*'))
        plan = self.service.plan(self.fixture['manifest_path'])
        self.assertEqual(plan.plan_digest, self.service.plan(self.fixture['manifest_path']).plan_digest)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(paths, sorted(self.root.rglob('*')))
        prepared = self.service.materialize(self.fixture['manifest_path'], plan.plan_digest)
        record = payload(prepared)['record_path']
        for case in payload(plan)['preparation']['cases']:
            self.assertEqual({p.name for p in Path(case['output']).iterdir()}, {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'})
        self.assertEqual(self.store.snapshot()['jobs'], [])
        registered = self.register(record, plan.plan_digest)
        self.assertEqual(registered.status, 'REGISTERED', payload(registered))
        self.assertEqual([job['status'] for job in self.store.snapshot()['jobs']], ['Hold', 'Hold'])
        replay = self.register(record, plan.plan_digest)
        self.assertTrue(all(row['replayed'] for row in payload(replay)['cases']))
        self.assertEqual(len(self.store.snapshot()['jobs']), 2)
        self.assertEqual(self.service.view(record).status, 'COMPLETE')
        job = self.store.snapshot()['jobs'][0]
        selected = self.service.view_for_job(job['id'], job['attempt_id'])
        self.assertEqual(payload(selected)['selected_target']['attempt_id'], job['attempt_id'])
        missing = self.root / 'missing-selection.json'
        write_json(missing, {'schema': 1, 'study_id': self.manifest['study_id'], 'plan_digest': plan.plan_digest, 'cases': {}})
        report = payload(self.service.compare(record, str(missing)))
        self.assertEqual(report['qualified_count'], 0)
        self.assertEqual([row['status'] for row in report['rows']], ['MISSING', 'MISSING'])
        selection = complete_study(self.fixture, record)
        compared = self.service.compare(record, selection, str(self.root / 'comparison'))
        report = payload(compared)
        self.assertEqual(compared.status, 'COMPLETE', report)
        self.assertEqual(report['qualified_count'], 2, report)
        self.assertEqual([row['energy']['value'] for row in report['rows']], [-2.995, -2.985])
        self.assertEqual([row['delta'] for row in report['rows']], [0, .01])
        self.assertEqual(report['rule_result'], 'PASS')
        self.assertEqual(set(report['export']), {'json', 'csv', 'md'})
        self.assertEqual(self.fixture['cleanup']['remaining_owned_processes'], [])
        self.assertFalse(self.store.snapshot()['controller']['online'])
        wrong = json.loads(Path(selection).read_text())
        ids = list(wrong['cases'])
        wrong['cases'][ids[0]]['target'] = deepcopy(wrong['cases'][ids[1]]['target'])
        wrong_path = self.root / 'wrong-selection.json'
        write_json(wrong_path, wrong)
        refused = payload(self.service.compare(record, str(wrong_path)))
        self.assertNotEqual(refused['rows'][0]['status'], 'QUALIFIED')
        self.assertIsNone(refused['rows'][0]['delta'])
        original = json.loads(Path(selection).read_text())
        original['cases'][ids[0]]['target']['node_id'] = 'another-owner'
        write_json(wrong_path, original)
        unreachable = payload(self.service.compare(record, str(wrong_path)))
        self.assertNotEqual(unreachable['rows'][0]['status'], 'QUALIFIED')
        native = Path(report['rows'][0]['protected_directories'][0]) / 'OUTCAR'
        native.write_text(native.read_text() + '\n')
        changed = payload(self.service.compare(record, selection))
        self.assertNotEqual(changed['rows'][0]['status'], 'QUALIFIED')
        self.assertIsNone(changed['rows'][0]['delta'])

    def test_partial_registration_and_committed_response_loss_resume_without_duplicates(self):
        plan, record = self.materialize()
        original = self.store.add
        calls = 0
        def interrupted(**kwargs):
            nonlocal calls
            calls += 1
            result = original(**kwargs)
            if calls == 1:
                raise OSError('Invented lost response after durable native commit')
            return result
        with patch.object(self.store, 'add', side_effect=interrupted):
            partial = self.register(record, plan.plan_digest)
        self.assertEqual(partial.status, 'PARTIAL')
        self.assertEqual(len(self.store.snapshot()['jobs']), 2)
        resumed = self.register(record, plan.plan_digest)
        self.assertEqual(resumed.status, 'REGISTERED', payload(resumed))
        self.assertTrue(all(row['replayed'] for row in payload(resumed)['cases']))
        self.assertEqual(len(self.store.snapshot()['jobs']), 2)
        self.assertEqual(len(list(Path(self.manifest['record_root']).glob('registration-*.json'))), 2)

    def test_stored_case_cap_and_duplicate_ids_refuse_before_journal_access(self):
        _plan, record_path = self.materialize()
        record = json.loads(Path(record_path).read_text())
        for count in (2, 65):
            invalid = deepcopy(record)
            first = invalid['plan']['preparation']['cases'][0]
            invalid['plan']['preparation']['cases'] = [deepcopy(first) for _ in range(count)]
            invalid['plan']['preparation']['case_count'] = count
            invalid['plan_digest'] = stable_hash(invalid['plan'])
            write_json(record_path, invalid)
            with self.subTest(count=count), patch.object(self.store, 'request_lookup', side_effect=AssertionError('No journal access')):
                with self.assertRaisesRegex(ValueError, '1..64 unique'):
                    self.service.view(record_path)
        self.assertEqual(self.store.snapshot()['jobs'], [])

    def test_failed_publication_receipt_never_promotes_matching_input_bytes(self):
        from cmw.jobs.study_plan import materialize_study
        plan = self.service.plan(self.fixture['manifest_path'])
        def reported_failure(*args):
            published = materialize_study(*args)
            published['cases'][0].update(status='FAILED', error='Invented publisher post-publication failure')
            published['status'] = 'PARTIAL'
            return published
        with patch('cmw.jobs.study_plan.materialize_study', side_effect=reported_failure):
            result = self.service.materialize(self.fixture['manifest_path'], plan.plan_digest)
        self.assertEqual(result.status, 'PARTIAL')
        value = payload(result)
        self.assertEqual(value['cases'][0]['status'], 'INCOMPLETE')
        first = payload(plan)['preparation']['cases'][0]
        self.assertEqual({p.name for p in Path(first['output']).iterdir()}, {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'})
        registered = self.register(value['record_path'], plan.plan_digest)
        self.assertEqual(registered.status, 'PARTIAL')
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)
        self.assertNotEqual(self.store.snapshot()['jobs'][0]['node_submission']['study']['case_id'], first['case_id'])

    def test_invalid_reference_is_rejected_before_materialization(self):
        manifest = deepcopy(self.manifest)
        manifest['comparison_rule']['reference_case'] = 'e' * 64
        write_json(self.fixture['manifest_path'], manifest)
        with self.assertRaises(ValueError):
            self.service.plan(self.fixture['manifest_path'])
        self.assertEqual(list(Path(self.manifest['output_root']).iterdir()), [])
        self.assertEqual(self.store.snapshot()['jobs'], [])

    def test_changed_baseline_refuses_plan_bound_materialization(self):
        plan = self.service.plan(self.fixture['manifest_path'])
        baseline = Path(self.manifest['baseline']['directory']) / 'INCAR'
        baseline.write_text(baseline.read_text() + '# changed after reviewed plan\n')
        with self.assertRaises(ValueError):
            self.service.materialize(self.fixture['manifest_path'], plan.plan_digest)
        self.assertEqual(list(Path(self.manifest['output_root']).iterdir()), [])
        self.assertEqual(self.store.snapshot()['jobs'], [])

    def test_incomplete_actual_publication_blocks_registration_and_admission(self):
        plan, record_path = self.materialize()
        case = payload(plan)['preparation']['cases'][0]
        actual_path = Path(case['record_directory']) / 'preparation.json'
        original = actual_path.read_text()
        incomplete = json.loads(original)
        incomplete['publication']['state'] = 'incomplete'
        write_json(actual_path, incomplete)
        aggregate = json.loads(Path(record_path).read_text())
        self.assertEqual(aggregate['status'], 'MATERIALIZED')
        self.assertTrue(all(row['status'] == 'MATERIALIZED' for row in aggregate['cases']))
        refused = self.register(record_path, plan.plan_digest)
        self.assertEqual(refused.status, 'PARTIAL', payload(refused))
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)
        self.assertNotEqual(self.store.snapshot()['jobs'][0]['node_submission']['study']['case_id'], case['case_id'])
        actual_path.write_text(original)
        restored = self.register(record_path, plan.plan_digest)
        self.assertEqual(restored.status, 'REGISTERED', payload(restored))
        job = next(job for job in self.store.snapshot()['jobs'] if job['node_submission']['study']['case_id'] == case['case_id'])
        self.assertIsNone(launch_blocker(job))
        write_json(actual_path, incomplete)
        self.assertIn('incomplete', launch_blocker(job))
        self.assertTrue(all(job['status'] == 'Hold' for job in self.store.snapshot()['jobs']))
        self.assertFalse(self.store.snapshot()['controller']['online'])

    def test_prepared_mutation_is_refused_at_registration_and_actual_admission(self):
        plan, record = self.materialize()
        case = payload(plan)['preparation']['cases'][0]
        source = Path(case['output']) / 'INCAR'
        original = source.read_text()
        source.write_text(original + '# changed before registration\n')
        partial = self.register(record, plan.plan_digest)
        self.assertEqual(partial.status, 'PARTIAL')
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)
        source.write_text(original)
        self.assertEqual(self.register(record, plan.plan_digest).status, 'REGISTERED')
        job = next(job for job in self.store.snapshot()['jobs'] if job['node_submission']['study']['case_id'] == case['case_id'])
        self.assertIsNone(launch_blocker(job))
        source.write_text(original + '# changed before dispatch\n')
        self.assertIn('changed', launch_blocker(job))
        self.assertTrue(all(job['status'] == 'Hold' for job in self.store.snapshot()['jobs']))
        self.assertFalse(self.store.snapshot()['controller']['online'])
