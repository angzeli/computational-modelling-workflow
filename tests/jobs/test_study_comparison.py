"""Independent synthetic energy arithmetic and genuine owner-local evidence."""
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cmw.core.provenance import stable_hash
from cmw.jobs.study_comparison import (compare_study, export_comparison, inspect_case,
                                       validate_comparison_spec)
from cmw.periodic.vasp.result_finalization import finalize_result
from cmw.periodic.vasp.results import inspect_result
from tests.periodic.vasp.test_inputs import potential
from tests.periodic.vasp.test_result_execution_native import run_fixture
from tests.periodic.vasp.test_result_policy import policy


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def numeric_case(case_id, value, *, encut=300, precision='0.000000005'):
    return {'case_id': case_id, 'status': 'QUALIFIED', 'metric': 'sigma_to_zero',
        'domains': dict.fromkeys(('process', 'completeness', 'native', 'policy', 'binding'), 'PASS'),
        'energy': {'value': float(value.replace('D', 'E')), 'printed': value, 'half_last_place': float(precision), 'units': 'eV'},
        'semantics': {'geometry': [[0, 0, 0], [1, 1, 1]], 'cell': [[5, 0, 0], [0, 5, 0], [0, 0, 5]],
            'composition': {'H': 1, 'He': 1}, 'species_order': ['H', 'He'], 'constraints': None,
            'charge': {'NELECT': 3}, 'spin': {'ISPIN': 1}, 'Hamiltonian': {'GGA': 'PE'},
            'potential_bytes': {'sha256': 'a' * 64, 'size_bytes': 100},
            'k_sampling': {'mode': 'Gamma', 'mesh': [1, 1, 1], 'shift': [0, 0, 0]},
            'numerical_controls': {'ENCUT': encut, 'EDIFF': 1e-6}, 'unknown_tags': []}, 'reasons': []}


def comparison_plan(*names, tolerance=.02, normalization='none'):
    return {'study_id': 'synthetic-study', 'digest': 'd' * 64, 'varied': ['ENCUT', 'k_sampling'],
        'metric': {'name': 'sigma_to_zero', 'unit': 'eV'}, 'normalization': {'kind': normalization},
        'comparison_rule': {'kind': 'absolute-to-reference', 'reference_case': names[0], 'tolerance': tolerance},
        'cases': [{'case_id': name, 'name': name, 'variables': {'ENCUT': 300 + i * 100}}
                  for i, name in enumerate(names)]}


class StudyComparisonArithmeticTests(unittest.TestCase):
    def test_independent_known_non_monotonic_values_and_named_energy(self):
        rows = [numeric_case('r', '-3.00000000'), numeric_case('a', '-2.99000000', encut=400),
                numeric_case('b', '-3.01500000', encut=500)]
        report = compare_study(comparison_plan('r', 'a', 'b'), rows)
        self.assertEqual([row['delta'] for row in report['rows']], [0.0, .01, -.015])
        self.assertEqual(report['rule_result'], 'PASS')
        self.assertEqual(report['rows'][1]['delta_interval'], [.00999999, .01000001])
        self.assertTrue(report['rows'][1]['changes'][0]['declared'])
        rows[1]['metric'] = 'free_energy'
        self.assertIsNone(compare_study(comparison_plan('r', 'a', 'b'), rows)['rows'][1]['delta'])

    def test_precision_straddles_threshold_and_failed_bound(self):
        rows = [numeric_case('r', '-3.00000000'), numeric_case('a', '-2.98000000', encut=400)]
        report = compare_study(comparison_plan('r', 'a'), rows)
        self.assertEqual(report['rows'][1]['rule_result'], 'UNKNOWN')
        rows[1] = numeric_case('a', '-2.97000000', encut=400)
        self.assertEqual(compare_study(comparison_plan('r', 'a'), rows)['rule_result'], 'FAIL')
        rows[1] = numeric_case('a', '-2.99D+00', encut=400, precision='.005')
        rows[1]['energy']['value'] = -2.99
        self.assertAlmostEqual(compare_study(comparison_plan('r', 'a'), rows)['rows'][1]['delta'], .01)

    def test_per_atom_only_fixed_same_composition_and_geometry(self):
        rows = [numeric_case('r', '-3.00000000'), numeric_case('a', '-2.98000000', encut=400)]
        plan = comparison_plan('r', 'a', normalization='per_atom')
        self.assertEqual(compare_study(plan, rows)['rows'][1]['delta'], .01)
        for field, changed in [('geometry', [[0, 0, 0], [1, 1, 1.1]]), ('composition', {'H': 2, 'He': 2}),
                               ('cell', [[6, 0, 0], [0, 5, 0], [0, 0, 5]])]:
            with self.subTest(field=field):
                other = deepcopy(rows)
                other[1]['semantics'][field] = changed
                plan['varied'].append(field)
                self.assertIsNone(compare_study(plan, other)['rows'][1]['delta'])

    def test_fixed_species_spin_charge_constraints_potentials_and_numerical_controls(self):
        reference, sample = numeric_case('r', '-3.00000000'), numeric_case('a', '-2.99000000', encut=400)
        for field, changed in [('species_order', ['He', 'H']), ('spin', {'ISPIN': 2}),
                ('charge', {'NELECT': 4}), ('constraints', [[True] * 3, [False] * 3]),
                ('potential_bytes', {'sha256': 'b' * 64, 'size_bytes': 100}),
                ('numerical_controls', {'ENCUT': 400, 'EDIFF': 1e-5}), ('Hamiltonian', {'GGA': 'PS'})]:
            with self.subTest(field=field):
                other = deepcopy(sample)
                other['semantics'][field] = changed
                report = compare_study(comparison_plan('r', 'a'), [reference, other])
                self.assertFalse(report['rows'][1]['comparable'])
                self.assertIsNone(report['rows'][1]['delta'])

    def test_unknown_failed_missing_unreachable_and_wrong_joins_preserved(self):
        rows = [numeric_case('r', '-3.00000000'), numeric_case('a', '-2.99000000'),
                {'case_id': 'c', 'status': 'UNREACHABLE', 'reason': 'Owner unavailable; cached result not refreshed'}]
        rows[1]['semantics']['unknown_tags'] = ['INVENTED_UNKNOWN']
        result = compare_study(comparison_plan('r', 'a', 'b', 'c'), rows)
        self.assertEqual(len(result['rows']), 4)
        self.assertEqual(result['rows'][2]['status'], 'MISSING')
        self.assertEqual(result['rows'][3]['status'], 'UNREACHABLE')
        self.assertIn('Owner unavailable; cached result not refreshed', result['rows'][3]['reasons'])
        self.assertIsNone(result['rows'][1]['delta'])
        self.assertEqual(result['rule_result'], 'UNASSESSED')
        for invalid in ([rows[0], rows[0]], [numeric_case('wrong', '-3.0')]):
            with self.assertRaises(ValueError): compare_study(comparison_plan('r', 'a'), invalid)
        rows[0]['status'] = 'UNASSESSED'
        rows[0]['domains']['native'] = 'FAIL'
        self.assertIsNone(compare_study(comparison_plan('r', 'a', 'c'), rows)['rows'][0]['delta'])

    def test_no_unit_conversion_or_implicit_reference(self):
        rows = [numeric_case('r', '-3.00000000'), numeric_case('a', '-2.99000000')]
        rows[1]['energy']['units'] = 'Eh'
        self.assertIsNone(compare_study(comparison_plan('r', 'a'), rows)['rows'][1]['delta'])
        plan = comparison_plan('r', 'a')
        plan['comparison_rule']['reference_case'] = 'missing'
        with self.assertRaises(ValueError): compare_study(plan, rows)

    def test_fresh_export_preserves_json_provenance_and_defuses_csv_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            rows = [numeric_case('r', '-3.00000000'), numeric_case('a', '-2.99000000', encut=400)]
            plan = comparison_plan('r', 'a')
            plan['cases'][1]['name'] = ' \t=HYPERLINK("bad")|<script>\n'
            report = compare_study(plan, rows)
            report['protected_directories'] = [str(root / 'science')]
            paths = export_comparison(report, root / 'export')
            self.assertEqual(json.loads(Path(paths['json']).read_text()), report)
            with Path(paths['csv']).open() as stream:
                data = list(csv.DictReader(stream))
            self.assertTrue(data[1]['name'].startswith("'"))
            self.assertIn('&lt;script&gt;', Path(paths['md']).read_text())
            self.assertEqual(float(data[1]['delta']), .01)
            with self.assertRaises(FileExistsError): export_comparison(report, root / 'export')
            with self.assertRaises(ValueError): export_comparison(report, root / 'science' / 'report')


class StudyComparisonEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='cmw-study-comparison-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.store, cls.snapshot, cls.receipt, cls.run_directory = run_fixture(cls.root / 'native')
        cls.job = cls.snapshot['jobs'][0]
        cls.target = {key: cls.snapshot['queue_identity'][key] for key in ('node_id', 'queue_id')}
        cls.target.update(job_id=cls.job['id'], attempt_id=cls.job['attempt_id'])
        (cls.run_directory / 'KPOINTS').write_text('invented\n0\nGamma\n1 1 1\n0 0 0\n')
        (cls.run_directory / 'POTCAR').write_bytes(potential(('H', 'He')))
        cls.expected = {name: {'sha256': digest(cls.run_directory / name), 'size_bytes': (cls.run_directory / name).stat().st_size}
                        for name in ('INCAR', 'POSCAR', 'POTCAR', 'KPOINTS')}
        cls.policy = policy()
        cls.policy['artifact_roles'] = ['energy:sigma_to_zero', 'energy:free_energy', 'energy:without_entropy']
        cls.policy_path = cls.root / 'policy.json'
        cls.policy_path.write_text(json.dumps(cls.policy))
        cls.snapshot_path = cls.root / 'snapshot.json'
        cls.snapshot_path.write_text(json.dumps(cls.snapshot))
        cls.spec_path = cls.root / 'spec.json'
        cls.refresh_spec()

    @classmethod
    def refresh_spec(cls):
        evidence = inspect_result(cls.run_directory)
        spec = {'schema_version': 1, 'intent': 'retrospective', 'producing_calculation': 'invented study writer',
            'snapshot_id': evidence['snapshot_id'], 'segment_id': evidence['selected_segment']['segment_id'],
            'policy_id': stable_hash(cls.policy), 'source_identities': {s['role']: s['sha256'] for s in evidence['sources']},
            'artifact_roles': cls.policy['artifact_roles'], 'execution': {'snapshot': str(cls.snapshot_path),
                'receipt': str(cls.receipt), 'job_id': cls.job['id'], 'attempt_id': cls.job['attempt_id'],
                'source_identities': {'jobs_snapshot': digest(cls.snapshot_path), 'jobs_receipt': digest(cls.receipt)}}}
        cls.spec_path.write_text(json.dumps(spec))

    def inspect(self, **options):
        return inspect_case(self.run_directory, policy_path=self.policy_path, spec_path=self.spec_path,
            expected_target=options.pop('expected_target', self.target), expected_inputs=options.pop('expected_inputs', self.expected),
            metric=options.pop('metric', 'sigma_to_zero'), expected_policy_id=stable_hash(self.policy), **options)

    def test_genuine_owner_receipt_independent_energy_definitions_and_read_only(self):
        before = self.store.path.read_bytes()
        files = sorted(self.root.rglob('*'))
        for metric, expected in [('sigma_to_zero', -2.995), ('free_energy', -3.0), ('without_entropy', -2.99)]:
            result = self.inspect(metric=metric)
            self.assertEqual(result['status'], 'QUALIFIED', result)
            self.assertEqual(result['energy']['value'], expected)
            self.assertEqual(set(result['domains'].values()), {'PASS'})
            self.assertEqual(result['finalization'], 'NOT ASSESSED')
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(files, sorted(self.root.rglob('*')))

    def test_selected_finalization_reverified_and_wrong_global_or_input_identity_refused(self):
        scratch = self.root / 'scratch'
        scratch.mkdir()
        record = finalize_result(self.run_directory, policy_path=self.policy_path, spec_path=self.spec_path,
                                 scratch_root=scratch, record_directory='finalized')
        result = self.inspect(finalization_path=record['publication']['record_path'])
        self.assertEqual(result['status'], 'QUALIFIED', result)
        self.assertEqual(result['finalization'], 'PASS')
        for key in self.target:
            target = dict(self.target, **{key: 999 if key == 'job_id' else 'wrong'})
            result = self.inspect(expected_target=target)
            self.assertNotEqual(result['status'], 'QUALIFIED')
            self.assertEqual(result['domains']['binding'], 'FAIL')
        inputs = deepcopy(self.expected)
        inputs['POTCAR']['sha256'] = 'a' * 64
        self.assertEqual(self.inspect(expected_inputs=inputs)['domains']['binding'], 'FAIL')

    def test_runtime_overlay_requires_exact_preparation_and_effective_values(self):
        original_incar = (self.run_directory / 'INCAR').read_bytes()
        original_outcar = (self.run_directory / 'OUTCAR').read_bytes()
        prepared = self.root / 'prepared-runtime-case'
        prepared.mkdir()
        for name in self.expected:
            (prepared / name).write_bytes((self.run_directory / name).read_bytes())
        runtime = {'cpus': 1, 'mpi_ranks': 1, 'threads_per_rank': 1, 'incar': {'NCORE': 1, 'KPAR': 1}}
        self.assertEqual(self.inspect(expected_runtime=runtime)['domains']['binding'], 'FAIL')
        from cmw.periodic.vasp.inputs import parse_incar
        record = {'schema_version': 1, 'record_kind': 'vasp-input-preparation',
            'publication': {'state': 'complete', 'input_path': str(prepared)},
            'prepared_inputs': self.expected, 'expected_runtime': runtime,
            'resolved_settings': {'incar': parse_incar(original_incar.decode())['settings']}}
        prep_path = self.root / 'runtime-preparation.json'
        prep_path.write_text(json.dumps(record))
        try:
            (self.run_directory / 'INCAR').write_bytes(original_incar + b'NCORE = 1\nKPAR = 1\n')
            (self.run_directory / 'OUTCAR').write_bytes(original_outcar.replace(b'ENCUT = 300', b'ENCUT = 300\nNCORE = 1\nKPAR = 1'))
            self.refresh_spec()
            spec = json.loads(self.spec_path.read_text())
            spec.update(expected_runtime_overlay=runtime['incar'],
                preparation_record={'path': str(prep_path), 'sha256': digest(prep_path)})
            self.spec_path.write_text(json.dumps(spec))
            good = self.inspect(expected_runtime=runtime)
            self.assertEqual(good['status'], 'QUALIFIED', good)
            self.assertEqual(self.inspect()['domains']['binding'], 'FAIL')
            wrong = deepcopy(runtime)
            wrong['incar']['NCORE'] = 2
            self.assertEqual(self.inspect(expected_runtime=wrong)['domains']['binding'], 'FAIL')
        finally:
            (self.run_directory / 'INCAR').write_bytes(original_incar)
            (self.run_directory / 'OUTCAR').write_bytes(original_outcar)
            self.refresh_spec()

    def test_unknown_unconverged_and_unsupported_results_remain_unqualified(self):
        path = self.run_directory / 'OUTCAR'
        original = path.read_text()
        for before, after in [('EDIFF is reached', 'EDIFF was not reached (unconverged)'),
                              ('LHFCALC = F', 'LHFCALC = T')]:
            try:
                path.write_text(original.replace(before, after))
                self.refresh_spec()
                result = self.inspect()
                self.assertNotEqual(result['status'], 'QUALIFIED')
                self.assertIn(result['domains']['native'], {'FAIL', 'UNSUPPORTED'})
            finally:
                path.write_text(original)
                self.refresh_spec()

    def test_explicit_manifest_policy_and_units_validation(self):
        spec = comparison_plan('r', 'a') | {'scientific_policy': {'path': str(self.policy_path), 'sha256': digest(self.policy_path)}}
        self.assertEqual(validate_comparison_spec(spec)['policy_id'], stable_hash(self.policy))
        for key, value in [('metric', {'name': 'sigma_to_zero', 'unit': 'Eh'}),
                           ('normalization', {'kind': 'per_formula_unit'}),
                           ('metric', {'name': [], 'unit': 'eV'}),
                           ('normalization', {'kind': []}),
                           ('comparison_rule', {'kind': 'absolute-to-reference', 'reference_case': 'r', 'tolerance': 0})]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError): validate_comparison_spec(spec | {key: value})
