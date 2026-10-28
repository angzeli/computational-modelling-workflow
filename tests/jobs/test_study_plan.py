from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.core.preparation_publication import content_identity, inspect_publication
from cmw.core.workflow_graph import WorkflowGraph
from cmw.jobs.study_plan import StudyPlanError, materialize_study, plan_study
from cmw.periodic.vasp.inputs import INPUT_NAMES, check_inputs
from cmw.periodic.vasp.preparation import prepare
from tests.periodic.vasp.test_inputs import potential, structure


def make_study(root):
    """Synthetic, non-running four-input study reusable by installed acceptance."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ('scratch', 'outputs', 'library', 'runs'):
        (root / name).mkdir()
    (root / 'scratch' / 'study-records').mkdir()
    for element in ('H', 'He', 'Li'):
        (root / 'library' / element).mkdir()
        (root / 'library' / element / 'POTCAR').write_bytes(potential((element,)))
    geometry = root / 'reviewed.POSCAR'
    geometry.write_text(structure(selective=True))
    preparation = {'schema_version': 1, 'calculation': 'static', 'structure': str(geometry),
        'profile': {'name': 'synthetic reviewed baseline', 'incar': {
            'GGA': 'PE', 'ENCUT': 300, 'PREC': 'Accurate', 'ISPIN': 1,
            'ISMEAR': 0, 'SIGMA': .05, 'EDIFF': 1e-6, 'NELM': 60,
            'ALGO': 'Normal', 'ISTART': 0, 'ICHARG': 2,
        }, 'kpoints': {'mode': 'Gamma', 'mesh': [1, 1, 1], 'shift': [0, 0, 0]},
        'potentials': {'root': str(root / 'library'), 'variants': {'H': 'H', 'He': 'He', 'Li': 'Li'},
                       'requirements': {'family': 'FAKE'}}},
        'runtime': {'cpus': 1, 'mpi_ranks': 1, 'threads_per_rank': 1,
                    'incar': {'NCORE': 1, 'KPAR': 1}}}
    prep_path = root / 'preparation.json'
    prep_path.write_text(json.dumps(preparation))
    prepare(prep_path, output=root / 'baseline', scratch_root=root / 'scratch', record_directory='baseline')
    policy = {'schema_version': 1, 'name': 'synthetic-static-study', 'calculation': 'static',
        'electronic': {'history': 'all', 'basis': 'native-and-numeric', 'criterion': 'run-ediff'},
        'force': None, 'endpoint': {'position_tolerance_angstrom': 2e-5, 'cell_tolerance_angstrom': 2e-6},
        'require_execution': True, 'require_input_binding': True, 'artifact_roles': ['energy:sigma_to_zero']}
    policy_path = root / 'policy.json'
    policy_path.write_text(json.dumps(policy))
    manifest = {'schema': 1, 'study_id': 'a' * 32, 'name': 'Synthetic explicit convergence study', 'engine': 'VASP',
        'baseline': {'directory': str(root / 'baseline'), 'inputs': {
            name: content_identity((root / 'baseline' / name).read_bytes()) for name in INPUT_NAMES}},
        'preparation_spec': str(prep_path),
        'matrix': {'encut': [300, 400],
            'kmeshes': [{'name': 'g1', 'mode': 'Gamma', 'mesh': [1, 1, 1], 'shift': [0, 0, 0]},
                        {'name': 'g2', 'mode': 'Gamma', 'mesh': [2, 2, 2], 'shift': [0, 0, 0]}],
            'structures': [{'name': 'fixed', 'path': str(geometry),
                            'sha256': hashlib.sha256(geometry.read_bytes()).hexdigest()}]},
        'cases': [{'name': 'reference', 'structure': 'fixed', 'encut': 400, 'kmesh': 'g2'},
                  {'name': 'coarse', 'structure': 'fixed', 'encut': 300, 'kmesh': 'g1'}],
        'varied': ['ENCUT', 'k_sampling'],
        'scientific_policy': {'path': str(policy_path), 'sha256': hashlib.sha256(policy_path.read_bytes()).hexdigest()},
        'metric': {'name': 'sigma_to_zero', 'unit': 'eV'}, 'normalization': {'kind': 'none'},
        'comparison_rule': {'kind': 'absolute-to-reference', 'reference_case': 'reference', 'tolerance': .01},
        'execution': {'node_id': 'fixture', 'queue_id': 'fixture'},
        'output_root': str(root / 'outputs'), 'scratch_root': str(root / 'scratch'),
        'record_root': str(root / 'scratch' / 'study-records')}
    path = root / 'study.json'
    path.write_text(json.dumps(manifest))
    return {'root': root, 'spec_path': path, 'spec': manifest, 'preparation': preparation,
            'preparation_path': prep_path, 'geometry': geometry}


class StudyPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = make_study(temporary.name)
        self.root, self.path, self.spec = (self.fixture[key] for key in ('root', 'spec_path', 'spec'))

    def save(self):
        self.path.write_text(json.dumps(self.spec))

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob('*')}

    def test_full_dry_run_is_repeatable_write_free_and_uses_graph(self):
        before = self.snapshot()
        with patch('subprocess.Popen', side_effect=AssertionError('No scientific process')):
            first, second = plan_study(self.path), plan_study(self.path)
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(first['status'], 'READY')
        self.assertNotIn('profile', first['cases'][0]['preparation'])
        self.assertEqual(len(WorkflowGraph.from_mapping(first['workflow_graph']).nodes), 2)
        self.assertEqual(first['cases'][0]['variables']['ENCUT'], 400)
        self.assertEqual(first['cases'][0]['semantic_identity']['constraints'], [[True, False, True]] * 3)
        self.assertEqual({item['study_field'] for item in first['cases'][0]['comparison']['study_fields']},
                         {'ENCUT', 'k_sampling', 'missing_evidence'})

    def test_entire_declared_matrix_expands_only_when_bounded(self):
        self.spec.pop('cases')
        self.spec['comparison_rule']['reference_case'] = 'case-001-fixed-g1'
        self.save()
        plan = plan_study(self.path)
        self.assertEqual(plan['case_count'], 4)
        self.assertEqual([case['variables']['ENCUT'] for case in plan['cases']], [300, 300, 400, 400])
        self.spec['matrix']['encut'] = list(range(300, 333))
        self.spec['matrix']['structures'][0]['path'] = '/missing/unread-oversized.POSCAR'
        self.save()
        with self.assertRaisesRegex(StudyPlanError, '66 cases; maximum is 64'):
            plan_study(self.path)

    def test_baseline_and_structure_pins_are_never_refreshed(self):
        for source in (self.root / 'baseline' / 'INCAR', self.fixture['geometry']):
            original = source.read_bytes()
            source.write_bytes(original + b'\n')
            with self.assertRaisesRegex(StudyPlanError, 'reviewed identity|Reviewed structure changed'):
                plan_study(self.path)
            source.write_bytes(original)
        self.assertFalse(list((self.root / 'outputs').iterdir()))

    def test_invalid_missing_parameters_and_unknown_fields(self):
        mutations = [lambda s: s['matrix'].pop('encut'),
                     lambda s: s['matrix'].update(encut=[True]),
                     lambda s: s['matrix']['kmeshes'][0].update(mesh=[0, 1, 1]),
                     lambda s: s['cases'][0].update(encut=500),
                     lambda s: s.update(automatic_optimizer=True),
                     lambda s: s.update(study_id='named-not-uuid'),
                     lambda s: s['baseline']['inputs'].pop('POTCAR'),
                     lambda s: s.update(varied=[[]]),
                     lambda s: s['cases'][0].update(structure=[])]
        original = deepcopy(self.spec)
        for mutate in mutations:
            self.spec = deepcopy(original)
            mutate(self.spec)
            self.save()
            with self.assertRaises(StudyPlanError):
                plan_study(self.path)

    def test_exact_four_file_publication_and_distinct_identical_cases(self):
        self.spec['cases'][1] = {**self.spec['cases'][0], 'name': 'independent-repeat'}
        self.save()
        plan = plan_study(self.path)
        before = {name: (self.root / 'baseline' / name).read_bytes() for name in INPUT_NAMES}
        self.assertNotEqual(plan['cases'][0]['case_id'], plan['cases'][1]['case_id'])
        self.assertEqual(plan['cases'][0]['prepared_inputs'], plan['cases'][1]['prepared_inputs'])
        result = materialize_study(self.path, plan['digest'])
        self.assertEqual(result['status'], 'MATERIALIZED')
        for receipt in result['cases']:
            output = Path(receipt['output'])
            self.assertEqual(set(path.name for path in output.iterdir()), set(INPUT_NAMES))
            self.assertEqual(check_inputs(output)['status'], 'valid')
            record = json.loads(Path(receipt['record_path']).read_text())
            self.assertEqual(record['study']['case_id'], receipt['case_id'])
            self.assertEqual(record['profile']['name'], 'synthetic reviewed baseline')
            self.assertIn('settings_origins', record)
            self.assertIn('selected_potentials', record)
            self.assertEqual(inspect_publication(receipt['record_path'])['status'], 'valid')
        for name, content in before.items():
            self.assertEqual((self.root / 'baseline' / name).read_bytes(), content)
        with self.assertRaises(ValueError):
            materialize_study(self.path, plan['digest'])

    def test_changed_source_or_manifest_before_materialize_refuses_all_writes(self):
        plan = plan_study(self.path)
        self.spec['comparison_rule']['tolerance'] = .02
        self.save()
        with self.assertRaisesRegex(StudyPlanError, 'changed since approval'):
            materialize_study(self.path, plan['digest'])
        self.assertFalse(list((self.root / 'outputs').iterdir()))
        self.assertFalse(list((self.root / 'scratch' / 'study-records').iterdir()))
        policy = Path(self.spec['scientific_policy']['path'])
        policy.write_bytes(policy.read_bytes() + b'\n')
        with self.assertRaisesRegex(StudyPlanError, 'Scientific policy differs'):
            plan_study(self.path)

    def test_fixed_spin_charge_numerical_controls_and_potentials_cannot_drift(self):
        original = deepcopy(self.fixture['preparation'])
        for delta in ({'ISPIN': 2, 'MAGMOM': [1, 0, 0]}, {'NELECT': 2}, {'ALGO': 'Fast'}, {'LREAL': 'Auto'}):
            changed = deepcopy(original)
            changed['profile']['incar'].update(delta)
            self.fixture['preparation_path'].write_text(json.dumps(changed))
            with self.assertRaisesRegex(StudyPlanError, 'changes fixed'):
                plan_study(self.path)
        self.fixture['preparation_path'].write_text(json.dumps(original))
        potential_path = self.root / 'library' / 'H' / 'POTCAR'
        potential_path.write_bytes(potential_path.read_bytes() + b'\n')
        with self.assertRaisesRegex(StudyPlanError, 'exact reviewed potential'):
            plan_study(self.path)

    def test_explicit_reviewed_geometry_cell_and_constraints_require_variation(self):
        original = self.fixture['geometry'].read_text()
        changed = self.root / 'variant.POSCAR'
        changed.write_text(original.replace('T F T', 'T T T'))
        self.spec['matrix']['structures'].append({'name': 'changed', 'path': str(changed),
            'sha256': hashlib.sha256(changed.read_bytes()).hexdigest()})
        self.spec['cases'][1]['structure'] = 'changed'
        self.save()
        with self.assertRaisesRegex(StudyPlanError, 'fixed constraints'):
            plan_study(self.path)
        self.spec['varied'].append('constraints')
        self.save()
        plan = plan_study(self.path)
        self.assertEqual(plan['cases'][1]['semantic_identity']['constraints'], [[True] * 3] * 3)

    def test_unknown_baseline_tag_is_unassessed_and_blocks_materialization(self):
        path = self.root / 'baseline' / 'INCAR'
        path.write_bytes(path.read_bytes() + b'UNRECOGNIZED_FUTURE_TAG = 1\n')
        self.spec['baseline']['inputs']['INCAR'] = content_identity(path.read_bytes())
        self.save()
        plan = plan_study(self.path)
        self.assertEqual(plan['status'], 'UNASSESSED')
        self.assertFalse(plan['cases'][0]['comparison']['prepared_input_comparison_complete'])
        with self.assertRaisesRegex(StudyPlanError, 'Unassessed fixed categories'):
            materialize_study(self.path, plan['digest'])

    def test_partial_publication_retains_completed_case_and_failed_split_record(self):
        from cmw.core import preparation_publication as publication
        plan = plan_study(self.path)
        real = publication._write_record
        def fail_second_complete(path, record):
            if path.parent.name == 'coarse' and record['publication']['state'] == 'complete':
                raise OSError('synthetic final record sync failure')
            real(path, record)
        with patch.object(publication, '_write_record', side_effect=fail_second_complete):
            result = materialize_study(self.path, plan['digest'])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual([case['status'] for case in result['cases']], ['MATERIALIZED', 'FAILED'])
        self.assertTrue(Path(result['cases'][1]['output']).exists())
        incomplete = json.loads(Path(result['cases'][1]['record_path']).read_text())
        self.assertEqual(incomplete['publication']['state'], 'incomplete')
        self.assertEqual(inspect_publication(result['cases'][0]['record_path'])['status'], 'valid')

    def test_composition_and_species_changes_remain_explicit_and_qualified(self):
        alternate = self.root / 'larger.POSCAR'
        alternate.write_text(structure(counts=[2, 1, 1], selective=True))
        self.spec['matrix']['structures'].append({'name': 'larger', 'path': str(alternate),
            'sha256': hashlib.sha256(alternate.read_bytes()).hexdigest()})
        self.spec['cases'][1]['structure'] = 'larger'
        self.save()
        with self.assertRaisesRegex(StudyPlanError, 'changes fixed'):
            plan_study(self.path)
        self.spec['varied'] += ['geometry', 'composition', 'constraints']
        self.save()
        plan = plan_study(self.path)
        self.assertEqual(plan['cases'][1]['semantic_identity']['composition'], [2, 1, 1])
        self.assertEqual(plan['cases'][1]['semantic_identity']['atom_count'], 4)
        alternate.write_text(structure(species=('He', 'H', 'Li'), selective=True))
        self.spec['matrix']['structures'][1]['sha256'] = hashlib.sha256(alternate.read_bytes()).hexdigest()
        self.spec['varied'].append('species_order')
        self.save()
        with self.assertRaisesRegex(StudyPlanError, 'exact reviewed potential'):
            plan_study(self.path)

    def test_relative_sources_normalize_and_destinations_cannot_nest_in_baseline(self):
        for key in ('preparation_spec', 'output_root', 'scratch_root', 'record_root'):
            self.spec[key] = str(Path(self.spec[key]).relative_to(self.root))
        self.spec['baseline']['directory'] = 'baseline'
        self.save()
        plan = plan_study(self.path)
        self.assertEqual(plan['spec']['record_root'], str(self.root / 'scratch' / 'study-records'))
        self.assertEqual(plan['baseline']['directory'], str(self.root / 'baseline'))
        self.spec['output_root'] = 'baseline'
        self.save()
        with self.assertRaisesRegex(ValueError, 'overlaps a protected source'):
            plan_study(self.path)

    def test_reference_resolves_reviewed_name_or_identity_and_refuses_unknown(self):
        plan = plan_study(self.path)
        reference_id = plan['cases'][0]['case_id']
        self.assertEqual(plan['comparison_rule']['reference_case'], reference_id)
        self.assertEqual(plan['spec']['comparison_rule']['reference_case'], reference_id)
        self.assertEqual(json.loads(self.path.read_text())['comparison_rule']['reference_case'], 'reference')
        self.spec['comparison_rule']['reference_case'] = reference_id
        self.save()
        self.assertEqual(plan_study(self.path)['comparison_rule']['reference_case'], reference_id)
        self.spec['comparison_rule']['reference_case'] = 'missing-reference'
        self.save()
        with self.assertRaisesRegex(StudyPlanError, 'exactly one declared case'):
            plan_study(self.path)
        self.assertFalse(list((self.root / 'outputs').iterdir()))

    def test_malformed_preparation_overrides_and_json_nesting_fail_cleanly(self):
        self.fixture['preparation']['overrides'] = {'incar': []}
        self.fixture['preparation_path'].write_text(json.dumps(self.fixture['preparation']))
        with self.assertRaisesRegex(StudyPlanError, 'Preparation overrides'):
            plan_study(self.path)
        self.path.write_text('[' * 2000 + '0' + ']' * 2000)
        with self.assertRaises(StudyPlanError):
            plan_study(self.path)

    def test_relaxation_and_source_record_inheritance_are_out_of_scope(self):
        self.fixture['preparation']['calculation'] = 'fixed-cell-relaxation'
        self.fixture['preparation_path'].write_text(json.dumps(self.fixture['preparation']))
        with self.assertRaisesRegex(StudyPlanError, 'fixed-geometry static'):
            plan_study(self.path)


if __name__ == '__main__':
    unittest.main()
