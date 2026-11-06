from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.successor_models import SuccessorUnverifiable
from cmw.jobs.successor_vasp import VaspSuccessorError, materialize_vasp, preview_vasp
from cmw.periodic.vasp.inputs import check_inputs
from cmw.periodic.vasp.preparation import PreparationError, prepare
from cmw.periodic.vasp.result_finalization import finalize_result
from tests.periodic.vasp.result_case import make_case
from tests.periodic.vasp.result_fixtures import write_run
from tests.periodic.vasp.test_inputs import potential


class VaspSuccessorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run = write_run(self.root / 'run', relaxation=True, evaluations=2,
                             flags=[[True] * 3, [False] * 3])
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()
        self._inputs(self.run)
        self.spec = self.root / 'next.json'
        self.parent = {'node_id': 'owner', 'queue_id': 'research', 'job_id': 7, 'attempt_id': 'a' * 32}
        self.state = self.root / 'state'
        self.data = {'schema_version': 1, 'calculation': 'static', 'structure': str(self.run / 'CONTCAR'),
            'profile': {'name': 'explicit synthetic successor', 'incar': {
                'GGA': 'PE', 'ENCUT': 300, 'PREC': 'Accurate', 'ISPIN': 1, 'ISMEAR': 0,
                'SIGMA': .05, 'EDIFF': 1e-6, 'NELM': 20, 'ALGO': 'Normal', 'ISTART': 0, 'ICHARG': 2,
                'NELECT': 3,
            }, 'kpoints': {'mode': 'Gamma', 'mesh': [1, 1, 1], 'shift': [0, 0, 0]},
                'potentials': {'root': str(self.root / 'library'), 'variants': {'H': 'H', 'He': 'He'},
                               'requirements': {'family': 'FAKE'}}}}
        self.options = {'directory': str(self.run), 'source_mode': 'observed',
                        'preparation_spec': str(self.spec), 'output': str(self.root / 'new-inputs'),
                        'scratch_root': str(self.scratch), 'record_directory': 'next'}
        self.save()

    def _inputs(self, directory):
        (directory / 'KPOINTS').write_text('invented mesh\n0\nGamma\n1 1 1\n0 0 0\n')
        (directory / 'POTCAR').write_bytes(potential(('H', 'He')))
        for symbol in ('H', 'He'):
            path = self.root / 'library' / symbol
            path.mkdir(parents=True, exist_ok=True)
            (path / 'POTCAR').write_bytes(potential((symbol,)))

    def save(self):
        self.spec.write_text(json.dumps(self.data))

    def preview(self):
        return preview_vasp(self.options, self.parent, state_directory=self.state)

    def materialize(self, approved):
        return materialize_vasp(self.options, self.parent, approved, state_directory=self.state)

    def test_observed_complete_geometry_is_not_promoted_to_scientific_acceptance(self):
        (self.run / 'OUTCAR').write_text((self.run / 'OUTCAR').read_text().replace(
            ' reached required accuracy - stopping structural energy minimisation\n', ''))
        before = {p.name: p.read_bytes() for p in self.run.iterdir()}
        with patch('subprocess.Popen', side_effect=AssertionError('No engine may launch')):
            preview = self.preview()
        self.assertEqual(preview['source']['qualification'], 'observed-complete')
        self.assertEqual(preview['source']['scientific_acceptance'], 'not established')
        self.assertFalse(preview['source']['ionic_native_stop_observed'])
        self.assertEqual(preview['source']['native_evaluation'], 2)
        self.assertFalse(Path(self.options['output']).exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.run.iterdir()})

    def test_materialization_checks_four_inputs_no_checkpoint_copy_and_no_clobber(self):
        for name in ('WAVECAR', 'CHGCAR', 'STOPCAR'):
            (self.run / name).write_bytes(b'never reused')
        before = {p.name: p.read_bytes() for p in self.run.iterdir()}
        approved = self.preview()
        record = self.materialize(approved)
        output = Path(self.options['output'])
        self.assertEqual({p.name for p in output.iterdir()}, {'INCAR', 'KPOINTS', 'POTCAR', 'POSCAR'})
        self.assertEqual((output / 'POSCAR').read_bytes(), (self.run / 'CONTCAR').read_bytes())
        self.assertEqual(check_inputs(output)['status'], 'valid')
        self.assertEqual(record['successor']['parent'], self.parent)
        self.assertEqual(record['publication']['state'], 'complete')
        self.assertEqual({p.name for p in (self.scratch / 'next').iterdir()}, {'preparation.json'})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.run.iterdir()})
        with self.assertRaises(ValueError):
            self.materialize(approved)

    def test_partial_output_and_later_unevaluated_geometry_are_refused(self):
        original = (self.run / 'CONTCAR').read_text()
        for changed in (original.rsplit('\n', 2)[0] + '\n', original.replace('0.5400000000', '0.5600000000')):
            (self.run / 'CONTCAR').write_text(changed)
            with self.assertRaises(VaspSuccessorError):
                self.preview()
        (self.run / 'CONTCAR').write_text(original)
        with (self.run / 'OUTCAR').open('a') as stream:
            stream.write(' Iteration 3( 1)\n')
        with self.assertRaises(VaspSuccessorError):
            self.preview()

    def test_cell_constraints_and_order_changes_are_explicit_or_blocked(self):
        structure = self.root / 'reviewed.POSCAR'
        original = (self.run / 'CONTCAR').read_text()
        self.data['structure'] = str(structure)
        self.save()
        for category, changed in (('cell', original.replace('4.0000000000', '4.1000000000', 1)),
                                  ('constraints', original.replace('F F F', 'T T T'))):
            structure.write_text(changed)
            self.options['allow_structure_changes'] = []
            with self.assertRaisesRegex(VaspSuccessorError, 'explicitly allow'):
                self.preview()
            self.options['allow_structure_changes'] = [category]
            result = self.preview()
            self.assertTrue(result['structure_changes'])
        structure.write_text(original.replace('H He', 'He H'))
        self.options['allow_structure_changes'] = ['geometry', 'cell', 'constraints']
        with self.assertRaises(ValueError):
            self.preview()

    def test_potential_spin_charge_and_numerical_differences_are_not_runtime_only(self):
        variant = self.root / 'library' / 'H_alt'
        variant.mkdir()
        (variant / 'POTCAR').write_bytes(potential(('H',)).replace(b'FAKE H ', b'FAKE H_alt '))
        self.data['profile']['potentials']['variants']['H'] = 'H_alt'
        self.data['profile']['incar'].update(ISPIN=2, MAGMOM=[1, 0], NELECT=2, ALGO='Fast', LREAL='Auto', EDIFF=1e-7)
        self.save()
        changes = self.preview()['differences']
        categories = {change.get('field'): change['successor_category'] for change in changes}
        for field in ('ISPIN', 'MAGMOM', 'NELECT', 'LREAL'):
            self.assertEqual(categories[field], 'Hamiltonian/spin/charge')
        for field in ('ALGO', 'EDIFF'):
            self.assertEqual(categories[field], 'solver/convergence controls')
        self.assertIn('potentials', [change['successor_category'] for change in changes])

    def test_source_mutation_and_identical_content_distinct_parent_change_approval(self):
        first = self.preview()
        other = {**self.parent, 'job_id': 8, 'attempt_id': 'b' * 32}
        second = preview_vasp(self.options, other, state_directory=self.state)
        self.assertNotEqual(first['digest'], second['digest'])
        self.assertEqual(first['preparation']['prepared_inputs'], second['preparation']['prepared_inputs'])
        with (self.run / 'OUTCAR').open('a') as stream:
            stream.write('\n')
        with self.assertRaisesRegex(VaspSuccessorError, 'changed since approval'):
            self.materialize(first)
        self.assertFalse(Path(self.options['output']).exists())

    def test_input_retry_can_use_explicit_initial_structure_without_native_outputs(self):
        self.options['source_mode'] = 'input'
        self.data['structure'] = str(self.run / 'POSCAR')
        self.save()
        (self.run / 'OUTCAR').unlink()
        (self.run / 'OSZICAR').unlink()
        result = self.preview()
        self.assertEqual(result['source']['qualification'], 'reviewed-parent-input')
        self.assertIsNone(result['source']['evaluation_index'])

    def test_checkpoint_requests_are_refused_even_when_files_exist(self):
        (self.run / 'WAVECAR').write_bytes(b'not a checkpoint validator')
        for change in ({'ISTART': 1}, {'ICHARG': 1}):
            self.data['overrides'] = {'incar': change}
            self.save()
            with self.assertRaisesRegex(PreparationError, 'fresh electronic initialization'):
                self.preview()

    def test_accepted_structure_uses_existing_finalizer_and_exact_parent(self):
        case = make_case(self.root / 'accepted', relaxation=True, constrained=True)
        self._inputs(case['run'])
        # The saved fixture sources already bind INCAR/POSCAR; adding the other
        # two explicit baseline inputs does not alter that native observation.
        record = finalize_result(case['run'], policy_path=case['policy_path'], spec_path=case['spec_path'],
                                 scratch_root=case['scratch'], record_directory='final')
        self.options.update(directory=str(case['run']), source_mode='accepted',
                            accepted_record=record['publication']['record_path'],
                            accepted_artifact_id=record['artifact_bundle']['artifacts'][0]['artifact_id'])
        self.state = case['state']
        self.data['structure'] = str(case['run'] / 'CONTCAR')
        self.save()
        result = self.preview()
        self.assertEqual(result['source']['qualification'], 'accepted-periodic-structure')
        with patch('cmw.jobs.successor_vasp.verify_finalization',
                   return_value={'valid': None, 'findings': ['original queue unavailable']}):
            with self.assertRaisesRegex(SuccessorUnverifiable, 'unverifiable.*original queue unavailable'):
                self.materialize(result)
        self.assertFalse(Path(self.options['output']).exists())
        self.parent['attempt_id'] = 'b' * 32
        with self.assertRaisesRegex(VaspSuccessorError, 'different parent attempt'):
            self.preview()

    def test_binary_safe_potential_identity_revalidation(self):
        for path in (self.run / 'POTCAR', self.root / 'library' / 'H' / 'POTCAR'):
            path.write_bytes(path.read_bytes().replace(b'synthetic', b'synth\xe9tic'))
        approved = self.preview()
        record = self.materialize(approved)
        self.assertEqual(record['publication']['state'], 'complete')

    def test_static_and_fixed_cell_successors_keep_supported_explicit_modes(self):
        self.data['calculation'] = 'fixed-cell-relaxation'
        self.data['profile']['incar'].update(IBRION=2, NSW=5, POTIM=.4, EDIFFG=-.02)
        self.save()
        result = self.preview()
        self.assertEqual(result['preparation']['calculation'], 'fixed-cell-relaxation')
        self.assertEqual(result['preparation']['resolved_settings']['incar']['ISIF'], 2)
        write_run(self.run)
        self._inputs(self.run)
        self.data['structure'] = str(self.run / 'POSCAR')
        self.save()
        result = self.preview()
        self.assertEqual(result['source']['source_role'], 'POSCAR')

    def test_parent_directory_is_never_a_publication_container(self):
        self.options['output'] = str(self.run / 'new-inputs')
        with self.assertRaisesRegex(VaspSuccessorError, 'outside the parent'):
            self.preview()
        self.assertFalse(Path(self.options['output']).exists())

    def test_preparer_guard_refuses_changed_resolution(self):
        approved = self.preview()
        self.data['profile']['incar']['NELM'] = 21
        self.save()
        with self.assertRaisesRegex(PreparationError, 'differs from approved resolution'):
            prepare(self.spec, output=self.options['output'], scratch_root=self.scratch,
                    record_directory='next', expected_resolution=approved['resolution_digest'])
        self.assertFalse(Path(self.options['output']).exists())


if __name__ == '__main__':
    unittest.main()
