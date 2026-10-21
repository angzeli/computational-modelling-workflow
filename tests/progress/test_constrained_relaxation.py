"""Synthetic constrained-relaxation regressions; no live scientific files."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.results import _force_summary, inspect_result
from cmw.progress.formatting import cell_width, progress_text
from cmw.progress.models import RunContext, SettingObservation, SourceDescriptor
from cmw.progress.service import ObservationService
from cmw.progress.vasp_card import vasp_card_groups
from tests.periodic.vasp.result_fixtures import native_texts, structure_text, write_run


class ConstrainedRelaxationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-constrained-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def observe(self, *, isif=0, poscar='valid', forces=((.972751, 0, 0), (1.044040, 0, 0)),
                ediffg='-.02', settings=None, active=True, native_stop=False):
        # The vector norms reproduce rounded historical J21.1 expectations;
        # these two-atom vectors and all other output are deliberately synthetic.
        texts = native_texts(relaxation=True, evaluations=6, footer=False, forces=forces,
            ediffg=ediffg, settings={'ISIF': str(isif), 'NSW': '20', **(settings or {})})
        if not native_stop:
            texts['OUTCAR'] = texts['OUTCAR'].replace(
                ' reached required accuracy - stopping structural energy minimisation\n', '')
        if active:
            texts['OUTCAR'] += ' Iteration 7( 1)\n EDDAV: synthetic\n free energy    TOTEN = -2.000000000000 eV\n'
            texts['OSZICAR'] += 'DAV: 1 -2.000000000000 -2.00000000E+00 -0.02000000E+00 12 0.00100000\n'
        for role in ('OUTCAR', 'OSZICAR'):
            (self.root / role).write_text(texts[role])
        roles = ['OUTCAR', 'OSZICAR']
        if poscar != 'undeclared':
            roles.append('POSCAR')
            content = structure_text(flags=((True,) * 3, (False,) * 3))
            if poscar == 'partial':
                content = structure_text(flags=((True, False, True), (True,) * 3))
            elif poscar == 'malformed':
                content = 'not a POSCAR\n'
            elif poscar == 'incomplete':
                content = content.rsplit('\n', 2)[0]
            elif poscar == 'wrong-count':
                content = content.replace('1 1\n', '1 0\n').rsplit('\n', 2)[0] + '\n'
            (self.root / 'POSCAR').write_text(content)
        context = RunContext('synthetic-attempt-binding', 'VASP', tuple(
            SourceDescriptor(role.lower(), str(self.root / role), optional=role == 'POSCAR',
                             origins=('user-declared-at-add',)) for role in roles),
            attempt_id='synthetic-attempt')
        service = ObservationService(clock=lambda: 100)
        try:
            return service.poll(context)
        finally:
            service.close()

    @staticmethod
    def metrics(snapshot):
        return {item.latest.semantic_id.rsplit('.', 1)[-1]: item.latest for item in snapshot.metrics
                if item.latest.semantic_id.startswith('vasp.force.')}

    def test_isif_zero_and_two_preserve_current_evaluation_and_completed_force(self):
        for isif in (0, 2):
            with self.subTest(isif=isif):
                snapshot = self.observe(isif=isif)
                self.assertEqual(snapshot.status, 'OBSERVED')
                self.assertEqual(snapshot.calculation, 'fixed-cell-relaxation')
                forces = self.metrics(snapshot)
                self.assertAlmostEqual(forces['free'].value, .972751)
                self.assertAlmostEqual(forces['fixed'].value, 1.044040)
                self.assertAlmostEqual(forces['all'].value, 1.044040)
                self.assertEqual(forces['free'].criterion.value, .02)
                self.assertIsNone(forces['fixed'].criterion)
                self.assertIsNone(forces['all'].criterion)
                self.assertEqual(forces['free'].context[0].index, 6)
                self.assertEqual(snapshot.iteration[0].index, 7)
                self.assertEqual(snapshot.context.attempt_id, 'synthetic-attempt')
                self.assertEqual({ref.source_role for ref in forces['free'].evidence}, {'outcar', 'poscar'})
                self.assertIn('historical binding unestablished', forces['free'].definition)
                self.assertEqual(snapshot.scientific_validation, 'not-assessed')
                text = progress_text(snapshot, width=95, now=100)
                for expected in ('Eval 7/20', 'Fmax movable', '0.972751 eV/Å @E6',
                                 'Target 0.020 eV/Å*', 'EDIFF 1E-6 eV'):
                    self.assertIn(expected, text)
                self.assertEqual(text.count('Fmax'), 1)
                self.assertNotIn('Fmax fixed', text)
                self.assertNotIn('Fmax all', text)
                self.assertNotIn('Native EDIFF reached', snapshot.messages)

    def test_fixed_atom_force_cannot_control_the_constrained_comparison(self):
        snapshot = self.observe(forces=((.003, .004, 0), (3, 4, 0)))
        forces = self.metrics(snapshot)
        self.assertEqual(forces['free'].value, .005)
        self.assertEqual(forces['fixed'].value, 5)
        self.assertEqual(forces['free'].relation, 'PROVISIONAL_BELOW')
        self.assertIsNone(forces['all'].criterion)
        self.assertFalse(any(event.kind == 'ionic-stop' for event in snapshot.events))
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')

    def test_historical_mask_has_120_movable_and_40_fixed_atoms(self):
        fixed = set(range(25, 41)) | set(range(101, 125))
        flags = [[index not in fixed] * 3 for index in range(1, 161)]
        vectors = [[0, 0, 0] for _ in flags]
        vectors[11] = [.972751, 0, 0]
        vectors[108] = [1.044040, 0, 0]
        summary = _force_summary(vectors, flags)
        self.assertEqual(summary['free']['count'], 120)
        self.assertEqual(summary['fixed']['count'], 40)
        self.assertEqual(sum(sum(row) for row in flags), 360)
        self.assertAlmostEqual(summary['free']['maximum_norm'], .972751)
        self.assertAlmostEqual(summary['all']['maximum_norm'], 1.044040)

    def test_unavailable_or_partial_constraints_do_not_claim_movable_comparison(self):
        for poscar in ('undeclared', 'malformed', 'incomplete', 'wrong-count', 'partial'):
            with self.subTest(poscar=poscar):
                snapshot = self.observe(poscar=poscar)
                forces = self.metrics(snapshot)
                self.assertTrue(all(value.criterion is None for value in forces.values()))
                text = progress_text(snapshot, width=110, now=100)
                self.assertNotIn('Fmax movable', text)
                self.assertNotIn('Target 0.020', text)
                self.assertIn('EDIFFG -0.02 eV/Å (unapplied)', text)
                if poscar != 'partial':
                    self.assertEqual(set(forces), {'all'})

    def test_nsw_denominator_requires_effective_positive_integer(self):
        snapshot = self.observe(isif=2)
        without = tuple(setting for setting in snapshot.settings if setting.name != 'NSW')
        invalid = [None, 0, -1, True, 20.5, '20']
        cases = [without] + [(*without, SettingObservation('NSW', value, 'OUTCAR effective NSW')) for value in invalid]
        cases += [(*without, SettingObservation('NSW', 20, 'unqualified', qualification))
                  for qualification in ('provisional', 'conflict', 'other-segment', 'unavailable')]
        for settings in cases:
            with self.subTest(settings=settings[-1] if settings else None):
                changed = replace(snapshot, settings=settings)
                changed = replace(changed, card_groups=vasp_card_groups(changed))
                text = progress_text(changed, width=95, now=100)
                self.assertIn('Eval 7 ', text)
                self.assertNotIn('Eval 7/', text)
                self.assertIn('@E6', text)

    def test_existing_card_dimensions_and_single_force_row(self):
        snapshot = self.observe()
        for width in (68, 77, 80, 87, 95, 135):
            for height in (6, 7, 8):
                with self.subTest(width=width, height=height):
                    text = progress_text(snapshot, framed=True, width=width, max_lines=height, now=100)
                    self.assertLessEqual(len(text.splitlines()), height)
                    self.assertTrue(all(cell_width(line) <= width for line in text.splitlines()))
                    self.assertEqual(text.count('FORCE'), 1)
                    self.assertEqual(text.count('Fmax'), 1)
                    self.assertIn('Eval 7/20', text)
                    self.assertIn('@E6', text)

    def test_other_modes_and_special_controls_remain_unsupported_but_settings_known(self):
        for settings in ({'ISIF': '1'}, {'ISIF': '3'}, {'IBRION': '0'}, {'LHFCALC': 'T'}, {'IMAGES': '2'}):
            with self.subTest(settings=settings):
                snapshot = self.observe(settings=settings)
                self.assertEqual(snapshot.status, 'UNSUPPORTED')
                self.assertTrue(all(item.latest.criterion is None for item in snapshot.metrics))
                text = progress_text(snapshot, width=110, now=100)
                self.assertIn('EDIFF 1E-6 eV', text)
                self.assertIn('EDIFFG -0.02 eV/Å (unapplied)', text)
                self.assertNotIn('Target 0.020', text)

    def test_native_stopping_and_nsw_exhaustion_are_distinct(self):
        for native_stop in (False, True):
            with self.subTest(native_stop=native_stop):
                snapshot = self.observe(settings={'NSW': '6'}, active=False, native_stop=native_stop)
                self.assertIn('Eval 6/6', progress_text(snapshot, width=95, now=100))
                self.assertEqual(any('Native optimizer stopping' in message for message in snapshot.messages), native_stop)
                self.assertEqual(snapshot.scientific_validation, 'not-assessed')

    def test_ediffg_signs_do_not_turn_energy_or_budget_settings_into_force_targets(self):
        for value in ('0', '.02', '-.02', '-.00001', '-.020123'):
            with self.subTest(ediffg=value):
                snapshot = self.observe(ediffg=value)
                forces = self.metrics(snapshot)
                setting = next(setting for setting in snapshot.settings if setting.name == 'EDIFFG')
                self.assertEqual(setting.value, float(value))
                self.assertEqual(setting.qualification, 'effective')
                self.assertEqual(forces['free'].criterion is not None, float(value) < 0)
                text = progress_text(snapshot, width=95, now=100)
                if float(value) < 0:
                    self.assertNotIn('Target 0.000 eV', text)
                    if value == '-.020123':
                        self.assertIn('Target 0.020123 eV/Å', text)
                else:
                    self.assertNotIn('Target 0.020 eV/Å', text)

    def test_batch_scope_support_is_bounded_and_not_scientific_acceptance(self):
        for isif in (0, 2):
            with self.subTest(isif=isif):
                root = write_run(self.root / str(isif), relaxation=True, settings={'ISIF': str(isif)},
                                 flags=((True,) * 3, (False,) * 3))
                result = inspect_result(root)
                self.assertTrue(result['mode']['supported'])
                self.assertEqual(result['mode']['calculation'], 'fixed-cell-relaxation')


if __name__ == '__main__':
    unittest.main()
