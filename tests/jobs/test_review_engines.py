"""Synthetic result-review semantics; no scientific executable is invoked."""
from copy import deepcopy
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs import molecular
from cmw.jobs.review_engines import inspect_engine
from cmw.periodic.vasp.results import inspect_result
from tests.periodic.vasp.result_case import make_case
from tests.periodic.vasp.result_fixtures import write_run
from tests.molecular.orca.jobs_fixture import make_spec as orca_spec
from tests.molecular.multiwfn.jobs_fixtures import make_spec as multiwfn_spec


LIMIT = 8 * 1024**2


class EngineReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-review-engines-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def review(self, job, **kwargs):
        result = inspect_engine(job, max_bytes=LIMIT, **kwargs)
        json.dumps(result, allow_nan=False)
        self.assertNotIn('status', result, 'No universal scientific PASS')
        return result

    def prepared(self, spec, *, execute=True):
        checked = molecular.check(spec)
        self.assertEqual(checked['status'], 'READY', checked)
        plan = molecular.prepare(spec, checked)
        value = molecular.binding(spec, plan)
        job = {'engine': spec['engine'], 'status': 'Done', 'exit_code': 0,
               'resources': spec['resources'], 'argv': molecular._stored_argv(plan['argv']),
               'env': plan['env'], 'cwd': plan['cwd'], 'molecular_submission': value}
        if execute:
            run = subprocess.run(plan['argv'], cwd=plan['cwd'],
                env={**os.environ, **plan['env'], 'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, text=True, timeout=40)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return job, plan

    def policy(self, job, *, frequency=None):
        path = self.root / (job['engine'] + '-policy.json')
        path.write_text(json.dumps({'schema_version': 1, 'name': 'synthetic exact task',
            'engine': job['engine'], 'profile': job['molecular_submission']['spec']['scientific']['profile'],
            'frequency': frequency}))
        return str(path)

    def test_vasp_default_has_no_acceptance_and_static_ionic_na(self):
        run = write_run(self.root / 'vasp')
        before = {p: p.stat().st_mtime_ns for p in run.iterdir()}
        result = self.review({'engine': 'VASP', 'status': 'Done', 'exit_code': 0}, directory=str(run))
        self.assertEqual(result['domains']['policy']['status'], 'NOT ASSESSED')
        self.assertEqual(result['domains']['source_binding']['status'], 'NOT ASSESSED')
        self.assertEqual(result['domains']['electronic']['status'], 'PASS')
        self.assertEqual(result['domains']['ionic_analysis']['status'], 'NOT APPLICABLE')
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in run.iterdir()})

    def test_done_does_not_override_nonconvergence_or_incomplete_reads(self):
        run = write_run(self.root / 'nonconverged', native=False, terminal_de='-.10000000E-02')
        result = self.review({'engine': 'VASP', 'status': 'Done', 'exit_code': 0}, directory=str(run))
        self.assertEqual(result['domains']['electronic']['status'], 'FAIL')
        result = inspect_engine({'engine': 'VASP'}, directory=str(run), max_bytes=30)
        self.assertNotEqual(result['domains']['electronic']['status'], 'PASS')
        self.assertFalse(result['coverage'].get('all_sources_complete', result['coverage'].get('complete')))

    def test_vasp_policy_and_force_scope_reuse_existing_binding(self):
        case = make_case(self.root / 'constrained', relaxation=True, constrained=True)
        result = self.review({'engine': 'VASP'}, directory=str(case['run']),
            policy_path=str(case['policy_path']), spec_path=str(case['spec_path']))
        self.assertEqual(result['domains']['policy']['status'], 'PASS', result['domains'])
        self.assertEqual(result['domains']['source_binding']['status'], 'PASS')
        forces = result['domains']['ionic_analysis']['evidence']['force_summary']
        self.assertGreater(forces['fixed']['maximum_norm'], forces['free']['maximum_norm'])
        check = next(c for c in result['domains']['ionic_analysis']['evidence']['checks'] if c['code'] == 'forces.threshold')
        self.assertEqual(check['evidence']['scope'], 'free')
        self.assertEqual(check['status'], 'PASS')
        self.assertTrue(result['policy_identity']['policy_id'])
        self.assertEqual(list(case['scratch'].iterdir()), [])

    def test_unsupported_vasp_mode_is_not_promoted_by_done(self):
        run = write_run(self.root / 'unsupported')
        outcar = run / 'OUTCAR'
        outcar.write_text(outcar.read_text().replace('LHFCALC = F', 'LHFCALC = T'))
        result = self.review({'engine': 'VASP', 'status': 'Done'}, directory=str(run))
        self.assertEqual(result['domains']['electronic']['status'], 'UNSUPPORTED')

    def test_unverifiable_vasp_binding_remains_unknown_in_review(self):
        run = write_run(self.root / 'vasp')
        binding = {'valid': None, 'execution': {'live_queue': {'status': 'UNAVAILABLE'}}}
        with patch('cmw.periodic.vasp.result_finalization.inspect_with_policy',
                   return_value={'binding': binding}):
            result = self.review({'engine': 'VASP'}, directory=str(run))
        self.assertEqual(result['domains']['source_binding']['status'], 'UNKNOWN')
        self.assertEqual(result['domains']['source_binding']['evidence'], binding)

    def test_orca_review_reuses_validation_without_finalizing(self):
        job, plan = self.prepared(orca_spec(self.root / 'orca'))
        before = Path(plan['result_path']).read_bytes()
        with patch('cmw.molecular.orca.job.finalize_attempt', side_effect=AssertionError('no finalization')):
            result = self.review(job)
        self.assertEqual(result['domains']['electronic']['status'], 'PASS', result)
        self.assertEqual(result['domains']['ionic_analysis']['status'], 'NOT APPLICABLE')
        self.assertEqual(result['domains']['endpoint']['status'], 'PASS')
        self.assertEqual(result['domains']['policy']['status'], 'NOT ASSESSED')
        assessed = self.review(job, policy_path=self.policy(job))
        self.assertEqual(assessed['domains']['policy']['status'], 'PASS', assessed)
        self.assertEqual(Path(plan['result_path']).read_bytes(), before)
        self.assertTrue(all(s['sha256'] and s['stat_before'] == s['stat_after'] for s in assessed['sources']))
        self.assertTrue(next(s for s in assessed['sources'] if s['path'] == job['molecular_submission']['spec']['scientific']['input'])['scientific_input'])

    def test_orca_frequency_requires_explicit_current_policy(self):
        job, _ = self.prepared(orca_spec(self.root / 'freq', profile='freq'))
        result = self.review(job)
        self.assertEqual(result['domains']['ionic_analysis']['status'], 'NOT ASSESSED')
        policy = self.policy(job, frequency={'require_minimum': True, 'imaginary_tolerance_cm1': 0})
        result = self.review(job, policy_path=policy)
        self.assertEqual(result['domains']['ionic_analysis']['status'], 'PASS', result)
        self.assertEqual(result['domains']['policy']['status'], 'PASS')
        data = json.loads(Path(policy).read_text()); data['frequency'] = None
        Path(policy).write_text(json.dumps(data))
        self.assertEqual(self.review(job, policy_path=policy)['domains']['policy']['status'], 'FAIL')

    def test_orca_workflow_exact_stage_records_are_reviewed(self):
        job, _ = self.prepared(orca_spec(self.root / 'workflow', profile='opt+freq+sp', workflow=True))
        result = self.review(job, policy_path=self.policy(job, frequency={'require_minimum': False, 'imaginary_tolerance_cm1': 0}))
        self.assertEqual(result['domains']['policy']['status'], 'PASS', result['domains'])
        self.assertEqual([item['stage'] for item in result['evidence']], ['OPT', 'FREQ', 'SP'])

    def test_all_multiwfn_profiles_revalidate_without_writes(self):
        for profile in ('FMO', 'ESP', 'IGMH', 'IFCT'):
            with self.subTest(profile=profile):
                job, plan = self.prepared(multiwfn_spec(self.root / profile, profile))
                before = {p: p.stat().st_mtime_ns for p in Path(plan['cwd']).rglob('*') if p.is_file()}
                with patch('cmw.molecular.workflows.multiwfn_analysis.atomic_write_json', side_effect=AssertionError('no finalizer writes')):
                    result = self.review(job, policy_path=self.policy(job))
                self.assertEqual(result['domains']['policy']['status'], 'PASS', result['domains'])
                self.assertEqual(result['domains']['electronic']['status'], 'NOT APPLICABLE')
                self.assertEqual(before, {p: p.stat().st_mtime_ns for p in Path(plan['cwd']).rglob('*') if p.is_file()})
                Path(plan['native_log']).write_text('incomplete replacement\n')
                changed = self.review(job, policy_path=self.policy(job))
                if profile == 'IFCT':
                    self.assertNotEqual(changed['domains']['policy']['status'], 'PASS')

    def test_multiwfn_bad_products_and_changed_donor_cannot_pass(self):
        job, plan = self.prepared(multiwfn_spec(self.root / 'FMO', 'FMO'))
        policy = self.policy(job)
        output = Path(plan['attempt_directory']) / plan['expected_outputs'][0]['output_path']
        output.write_text('incomplete cube\n')
        self.assertEqual(self.review(job, policy_path=policy)['domains']['policy']['status'], 'FAIL')
        Path(plan['source_wavefunction']).write_text('changed donor\n')
        self.assertEqual(self.review(job, policy_path=policy)['domains']['source_binding']['status'], 'FAIL')

    def test_budget_cancellation_and_unknown_profile_fail_closed(self):
        job, plan = self.prepared(orca_spec(self.root / 'bounds'))
        limited = inspect_engine(job, max_bytes=4)
        self.assertEqual(limited['domains']['source_binding']['status'], 'UNKNOWN')
        def cancel():
            raise RuntimeError('observer cancelled')
        with self.assertRaisesRegex(RuntimeError, 'observer cancelled'):
            inspect_engine(job, max_bytes=LIMIT, check_cancelled=cancel)
        self.assertEqual(self.review({'engine': 'CP2K'})['domains']['electronic']['status'], 'UNSUPPORTED')
        changed = deepcopy(job)
        changed['molecular_submission']['spec']['scientific']['profile'] = 'unreviewed'
        self.assertEqual(self.review(changed)['domains']['source_binding']['status'], 'FAIL')

    def test_orca_nonconvergence_cancellation_and_wrong_dialect_stay_separate(self):
        job, plan = self.prepared(orca_spec(self.root / 'orca-negative'))
        policy = self.policy(job)
        cancelled = {**job, 'status': 'Cancelled', 'cancel_requested': True}
        self.assertNotEqual(self.review(cancelled, policy_path=policy)['domains']['policy']['status'], 'PASS')
        output = Path(plan['cwd']) / 'stage.out'
        output.write_text(output.read_text().replace('SCF CONVERGED AFTER 2 CYCLES', 'SCF DID NOT CONVERGE'))
        result = self.review(job, policy_path=policy)
        self.assertEqual(result['domains']['electronic']['status'], 'FAIL')
        self.assertEqual(result['domains']['endpoint']['status'], 'FAIL')
        self.assertEqual(result['domains']['policy']['status'], 'FAIL')
        output.write_text(output.read_text().replace('Program Version 6.1.1', 'Program Version 9.9'))
        self.assertEqual(self.review(job)['domains']['electronic']['status'], 'UNSUPPORTED')
        self.assertEqual(self.review({'engine': 'ORCA'})['domains']['source_binding']['status'], 'UNSUPPORTED')

    def test_ifct_pure_inspection_never_normalizes_outputs(self):
        from cmw.molecular.multiwfn.ifct_job import inspect
        job, plan = self.prepared(multiwfn_spec(self.root / 'ifct', 'IFCT'))
        attempt = Path(plan['attempt_directory'])
        with patch('cmw.molecular.multiwfn.ifct_job.normalize_output_paths', side_effect=AssertionError('no move')):
            result = inspect(attempt, 0)
        self.assertEqual(result['validation_status'], 'PASSED')


if __name__ == '__main__':
    unittest.main()
