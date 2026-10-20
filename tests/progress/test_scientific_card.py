"""Synthetic scientific-card evidence and generic three-level presentation."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from cmw.progress.formatting import cell_width, progress_text
from cmw.progress.models import (CardCell, CardGroup, Criterion, IterationLevel, Metric,
    MetricSeries, ProgressEvent, ProgressSnapshot, RunContext, SettingObservation, SourceDescriptor, Verification)
from cmw.progress.registry import Registration
from cmw.progress.service import ObservationService
from cmw.progress.vasp_card import vasp_card_groups


def example(*, active=9, complete=8, checked=8, calculation='static'):
    """Invented values matching the shape of the supplied example, not live data."""
    evaluation = IterationLevel('evaluation', 1, 'evaluation 1 (native 1)')
    current = (evaluation, IterationLevel('electronic', active, f'SCF {active}', 300))
    row = (evaluation, IterationLevel('electronic', complete, f'SCF {complete}', 300))
    criterion = Criterion(1e-5, 'eV', '<', 'OUTCAR effective EDIFF')
    values = (('dE', '|dE|', 3.1087, 'eV'), ('d_eps', '|d eps|', 2.9332, 'eV'),
              ('rms', 'rms', 1.89, 'native'), ('rms_c', 'rms(c)', 1.41, 'native'))
    metrics = tuple(MetricSeries(Metric('vasp.electronic.' + key, label, 'Synthetic ' + label,
        unit, value, row, criterion=criterion if key in ('dE', 'd_eps') else None,
        signed_value=-value if key == 'd_eps' else value, observed_at=100)) for key, label, value, unit in values)
    verification = Verification('electronic-row', 'MATCHED' if checked == complete else 'PARTIAL',
        'stdout', 'outcar', complete, checked, active, row,
        fields=('label', 'algorithm', 'energy'), reason='Printed intervals overlap')
    snapshot = ProgressSnapshot(RunContext('synthetic-card', 'VASP'), 'test',
        status='OBSERVED', calculation=calculation, iteration=current, metrics=metrics,
        verification=(verification,), settings=(SettingObservation('EDIFF', 1e-5, 'OUTCAR effective'),),
        read_health='OK', last_new_evidence_at=100, last_success_at=112)
    return replace(snapshot, card_groups=vasp_card_groups(snapshot))


class ScientificCardTests(unittest.TestCase):
    def test_three_stable_groups_aligned_at_all_required_widths(self):
        snapshot = example()
        before = snapshot.to_dict()
        for width in (68, 71, 72, 75, 95, 135):
            with self.subTest(width=width):
                text = progress_text(snapshot, compact=width < 100, width=width, max_lines=6, now=112)
                lines = text.splitlines()
                self.assertEqual(len(lines), 6)
                self.assertEqual([line.split()[0] for line in lines[:4]], ['SCIENTIFIC', 'SCF', 'FORCE', 'ELECTRONIC'])
                self.assertTrue(all(cell_width(line) <= width for line in lines))
                self.assertIn('OUTCAR SCF 9/300', text)
                self.assertIn('3.1087 eV @SCF8', text)
                self.assertIn('2.9332 eV @SCF8', text)
                self.assertIn('rms', text)
                self.assertIn('1.89 @SCF8', text)
                self.assertIn('EDIFF (both) <1e-05 eV', text)
                self.assertIn('Target N/A · static', text)
                self.assertIn('data 12s ago', text)
                self.assertNotIn('rms(c)', text)
                left = lines[3].index('|dE|')
                self.assertEqual(lines[4].index('rms'), left)
                self.assertEqual(lines[3].index('|d eps|'), lines[4].index('EDIFF'))
        self.assertEqual(snapshot.to_dict(), before)

    def test_outcar_label_uses_chronological_evaluation_and_real_budget(self):
        for chronological, native, budget in ((1, 1, 120), (2, 7, 73)):
            with self.subTest(chronological=chronological, native=native, budget=budget):
                snapshot = replace(example(active=3, complete=4, checked=3), iteration=(
                    IterationLevel('evaluation', chronological, f'evaluation {chronological} (native {native})'),
                    IterationLevel('electronic', 3, 'SCF 3', budget)))
                legacy = replace(snapshot, card_groups=(CardGroup('SCF', ((
                    CardCell(kind='text', text=f'Eval {chronological} (native {native})'),
                    CardCell(kind='context', context_kinds=('electronic',))),)), *snapshot.card_groups[1:]))
                snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
                before = snapshot.to_dict()
                for width in (80, 140):
                    text = progress_text(snapshot, width=width, now=112)
                    self.assertEqual(' '.join(text.splitlines()[1].split()),
                                     f'SCF Eval {chronological} OUTCAR SCF 3/{budget}')
                    self.assertNotIn('allowed', text)
                    self.assertNotIn('(native', text)
                    self.assertIn('3.1087 eV @SCF4', text)
                    self.assertIn('2.9332 eV @SCF4', text)
                    self.assertEqual(text.splitlines()[2:],
                                     progress_text(legacy, width=width, now=112).splitlines()[2:])
                self.assertEqual(snapshot.to_dict(), before)

    def test_outcar_label_missing_or_invalid_budget_is_explicit(self):
        for budget in (None, 0, -1, True, 1.5, '120'):
            with self.subTest(budget=budget):
                snapshot = replace(example(active=3), iteration=(
                    IterationLevel('evaluation', 1, 'evaluation 1 (native 1)'),
                    IterationLevel('electronic', 3, 'SCF 3', budget)))
                snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
                self.assertIn('OUTCAR SCF 3/—', progress_text(snapshot, width=80))
        snapshot = replace(example(active=3), settings=(SettingObservation('NELM', 300, 'OUTCAR', 'conflict'),))
        snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
        self.assertIn('OUTCAR SCF 3/—', progress_text(snapshot, width=80))

    def test_outcar_label_never_relabels_source_local_iteration(self):
        for levels in ((), (IterationLevel('evaluation', 1, 'evaluation 1 (native 1)'),),
                       (IterationLevel('electronic', 4, 'SCF 4'),)):
            with self.subTest(levels=levels):
                snapshot = replace(example(complete=4), iteration=levels)
                snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
                text = progress_text(snapshot, width=80)
                self.assertIn('OUTCAR SCF unavailable', text)
                self.assertNotIn('OUTCAR SCF 4', text)
                self.assertIn('@SCF4', text)

    def test_native_reader_context_remains_in_details_history_and_json(self):
        from cmw.progress.formatting import history_entry_text
        from tests.periodic.vasp.result_fixtures import native_texts
        from tests.progress.test_vasp_crosscheck import electronic_row, native_row
        with tempfile.TemporaryDirectory(prefix='cmw-outcar-label-') as directory:
            root = Path(directory)
            prefix = native_texts(nelm=120)['OUTCAR'].split('--------------------------------------- Iteration')[0]
            (root / 'OUTCAR').write_text(prefix + ''.join(native_row(i).replace(
                'Iteration      1(', 'Iteration      7(') for i in range(1, 4)))
            (root / 'stdout.log').write_text(''.join(electronic_row(i) for i in range(1, 5)))
            context = RunContext('synthetic-outcar-label', 'VASP', (
                SourceDescriptor('outcar', str(root / 'OUTCAR')),
                SourceDescriptor('stdout', str(root / 'stdout.log'))))
            service = ObservationService(clock=lambda: 100)
            try:
                snapshot = service.poll(context)
                before = snapshot.to_dict()
                text = progress_text(snapshot, width=80, now=112)
                self.assertEqual(' '.join(text.splitlines()[1].split()), 'SCF Eval 1 OUTCAR SCF 3/120')
                self.assertIn('3.1087 eV @SCF4', text)
                self.assertIn('2.9332 eV @SCF4', text)
                self.assertIn('evaluation 1 (native 7)', progress_text(snapshot, detailed=True))
                self.assertEqual(snapshot.to_dict()['iteration'][0]['label'], 'evaluation 1 (native 7)')
                page = service.history(context, limit=20, parent_id='*')
                self.assertTrue(any('evaluation 1 (native 7)' in history_entry_text(entry) for entry in page.entries))
                self.assertEqual(snapshot.to_dict(), before)
            finally:
                service.close()

    def test_latest_force_keeps_older_evaluation_and_target_qualification(self):
        snapshot = example(calculation='fixed-cell-relaxation')
        force = Metric('vasp.force.maximum_atomic_norm.free', 'Fmax free',
            'Maximum Euclidean atomic force-vector norm, current-input grouping', 'eV/Angstrom', .012,
            (IterationLevel('evaluation', 26, 'evaluation 26 (native 26)'),), qualification='provisional',
            criterion=Criterion(.02, 'eV/Angstrom', '<', 'OUTCAR effective EDIFFG'))
        snapshot = replace(snapshot, metrics=(*snapshot.metrics, MetricSeries(force)))
        text = progress_text(snapshot, width=72, now=112)
        self.assertIn('0.012 eV/Å @E26', text)
        self.assertIn('Target <0.02 eV/Å*', text)
        self.assertIn('* provisional', text)
        self.assertIn('Eval 1', text)
        self.assertNotIn('(native 1)', text)
        self.assertNotIn('0.012 eV/Å @E1', text)

    def test_partial_and_conflict_scope_and_read_error_remain_visible(self):
        snapshot = example(checked=6)
        text = progress_text(snapshot, width=72, now=112)
        self.assertIn('outcar checked 6', text)
        self.assertIn('8 source-local', text)
        self.assertIn('data 12s ago', text)
        conflict = replace(snapshot.verification[0], status='CONFLICT', reason='Complete row energies disagree')
        bad = replace(snapshot, verification=(conflict,), read_health='ERROR', last_success_at=112)
        text = progress_text(bad, width=72, now=120)
        self.assertIn('stdout/outcar', text)
        self.assertIn('ERROR', text)
        self.assertIn('data 20s ago', text)
        details = progress_text(bad, detailed=True, now=120)
        self.assertIn('fields: label, algorithm, energy', details)
        self.assertIn('Complete row energies disagree', details)
        self.assertIn('OUTCAR effective', details)
        self.assertIn('signed value -2.9332', details)
        self.assertIn('rms(c)', details)

    def test_conflict_keeps_independent_force_target_and_native_warning(self):
        snapshot = example(calculation='fixed-cell-relaxation')
        force = Metric('vasp.force.maximum_atomic_norm.free', 'Fmax free', 'Synthetic free-atom maximum norm',
            'eV/Angstrom', .012, (IterationLevel('evaluation', 26, 'evaluation 26 (native 26)'),),
            qualification='provisional', criterion=Criterion(.02, 'eV/Angstrom', '<', 'OUTCAR effective EDIFFG'))
        snapshot = replace(snapshot, status='SOURCE_CONFLICT', metrics=(*snapshot.metrics, MetricSeries(force)),
            verification=(replace(snapshot.verification[0], status='CONFLICT'),),
            events=(ProgressEvent('fatal', 'native-fatal', None, 1, message='Synthetic native warning'),))
        for width in (68, 71, 95, 135):
            text = progress_text(snapshot, width=width, now=112, max_lines=6)
            self.assertEqual(len(text.splitlines()), 6)
            self.assertIn('native warnings 1', text)
            self.assertIn('* provisional target', text)
            self.assertIn('SOURCE CONFLICT', text)
            self.assertIn('stdout/outcar', text)
            self.assertIn('0.012 eV/Å @E26', text)
            self.assertIn('data 12s ago', text)

    def test_missing_evidence_never_asserts_native_evaluation_has_not_happened(self):
        snapshot = replace(example(), status='UNSUPPORTED', read_health='UNAVAILABLE', metrics=(), verification=())
        text = progress_text(snapshot, width=68, now=112)
        self.assertIn('— (not observed)', text)
        self.assertIn('UNSUPPORTED', text)
        self.assertIn('UNAVAILABLE', text)
        self.assertNotIn('not yet evaluated', text)
        from cmw.progress.orca import OrcaAdapter
        orca = OrcaAdapter(RunContext('unsupported', 'ORCA'))
        groups = orca._card_groups()
        snapshot = replace(snapshot, context=orca.context, card_groups=groups)
        text = progress_text(snapshot, width=68, now=112)
        self.assertEqual(text.count('— (not observed)'), 2)
        self.assertNotIn('not yet evaluated', text)

    def test_optional_empty_duplicate_does_not_downgrade_native_row_match(self):
        snapshot = example()
        duplicate = replace(snapshot.verification[0], domain='electronic-stream', counterpart_role='oszicar',
                            status='PENDING', checked_frontier=None)
        text = progress_text(replace(snapshot, verification=(*snapshot.verification, duplicate)), width=72, now=112)
        self.assertIn('outcar matched through SCF 8', text)
        self.assertNotIn('source-local', text)
        self.assertNotIn('source-only', text)

    def test_input_only_criterion_remains_explicitly_provisional(self):
        snapshot = example()
        origin = 'Declared INCAR (historical linkage unestablished)'
        metrics = tuple(MetricSeries(replace(item.latest,
            criterion=replace(item.latest.criterion, origin=origin) if item.latest.criterion else None))
            for item in snapshot.metrics)
        snapshot = replace(snapshot, metrics=metrics, settings=(SettingObservation('EDIFF', 1e-5, origin, 'provisional'),))
        text = progress_text(snapshot, width=72, now=112)
        self.assertIn('EDIFF (both) <1e-05 eV*', text)
        self.assertIn('* provisional target', text)

    def test_source_only_unavailable_counterpart_is_not_matched(self):
        snapshot = example()
        check = replace(snapshot.verification[0], status='UNAVAILABLE', checked_frontier=None)
        text = progress_text(replace(snapshot, verification=(check,)), width=72, now=112)
        self.assertIn('outcar unavailable', text)
        self.assertNotIn('matched', text)
        self.assertIn('3.1087 eV @SCF8', text)

    def test_static_completed_force_has_no_target_and_zero_ediff_is_explicit(self):
        snapshot = example()
        force = Metric('vasp.force.maximum_atomic_norm.all', 'Fmax all', 'Maximum atomic norm',
                       'eV/Angstrom', .1, snapshot.iteration[:1])
        metrics = tuple(MetricSeries(replace(item.latest, criterion=None)) for item in snapshot.metrics)
        snapshot = replace(snapshot, metrics=(*metrics, MetricSeries(force)),
                           settings=(SettingObservation('EDIFF', 0, 'OUTCAR effective'),))
        snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
        text = progress_text(snapshot, width=72, now=112)
        self.assertIn('0.1 eV/Å @E1', text)
        self.assertIn('Target N/A · static', text)
        self.assertIn('EDIFF=0 · fixed iterations', text)
        self.assertNotIn('EDIFF (both) <0', text)
        provisional = replace(snapshot, settings=(SettingObservation('EDIFF', 0, 'Current declared INCAR', 'provisional'),))
        provisional = replace(provisional, card_groups=vasp_card_groups(provisional))
        text = progress_text(provisional, width=72, now=112)
        self.assertIn('EDIFF=0 · fixed iterations*', text)
        self.assertIn('* provisional settings', text)

    def test_other_segment_settings_cannot_label_the_active_segment(self):
        snapshot = example(calculation=None)
        snapshot = replace(snapshot, segment=2,
            metrics=tuple(MetricSeries(replace(item.latest, criterion=None, segment=2)) for item in snapshot.metrics),
            settings=(SettingObservation('EDIFF', 0, 'OUTCAR segment 1', 'other-segment'),
                      SettingObservation('EDIFFG', 0, 'OUTCAR segment 1', 'other-segment')))
        snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
        text = progress_text(snapshot, width=68, now=112)
        self.assertIn('EDIFF unavailable', text)
        self.assertIn('Target unavailable', text)
        self.assertNotIn('fixed iterations', text)
        self.assertNotIn('EDIFFG=0', text)

    def test_positive_ediffg_is_never_a_force_target(self):
        snapshot = replace(example(calculation='fixed-cell-relaxation'),
                           settings=(SettingObservation('EDIFFG', .02, 'OUTCAR effective'),))
        snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
        self.assertIn('Target N/A · ionic energy', progress_text(snapshot, width=72))

    def test_effective_input_discrepancy_visible_and_full_origin_in_details(self):
        snapshot = replace(example(), settings=(SettingObservation('EDIFF', 1e-5, 'OUTCAR effective',
                            compared_value=1e-6, discrepancy=True),))
        text = progress_text(snapshot, width=72, now=112)
        self.assertIn('settings differ: EDIFF', text)
        self.assertIn('data 12s ago', text)
        details = progress_text(snapshot, detailed=True, now=112)
        self.assertIn('origin: OUTCAR effective', details)
        self.assertIn('compared input: 1e-06; discrepancy: True', details)

    def test_native_orca_uses_same_grid_with_native_semantics(self):
        from tests.progress.orca_fixtures import output
        with tempfile.TemporaryDirectory(prefix='cmw-orca-card-') as name:
            path = Path(name) / 'orca.out'
            path.write_text(output())
            context = RunContext('orca-card', 'ORCA', (SourceDescriptor('orca_output', str(path), stage='OPT'),), stage='OPT')
            service = ObservationService(clock=lambda: 100)
            try:
                snapshot = service.poll(context)
                before = snapshot.to_dict()
                for width in (68, 71, 72, 75, 95, 135):
                    text = progress_text(snapshot, width=width, now=112, max_lines=6)
                    self.assertEqual(len(text.splitlines()), 6)
                    self.assertTrue(all(cell_width(line) <= width for line in text.splitlines()))
                    for value in ('GRADIENT', 'DISPLACE', 'Eh/bohr', 'bohr', 'ELECTRONIC'):
                        self.assertIn(value, text)
                    for absent in ('Fmax', 'rms(c)', 'EDIFF'):
                        self.assertNotIn(absent, text)
                self.assertEqual(snapshot.to_dict(), before)
            finally:
                service.close()

    def test_fictional_three_level_adapter_needs_no_generic_widget_change(self):
        levels = (IterationLevel('geometry', 3, 'geometry'), IterationLevel('outer', 2, 'outer SCF'),
                  IterationLevel('inner', 8, 'inner SCF'))
        flux_cell = CardCell(('fictional.flux',), 'Charge flux',
                             context_labels=(('geometry', 'G'), ('outer', 'O'), ('inner', 'I')))
        groups = (CardGroup('CONTEXT', ((CardCell(kind='context'),),)),
                  CardGroup('流量', ((flux_cell, CardCell(kind='text', text='Target-free observation')),)))
        class Fictional:
            def __init__(self, context):
                self.context = context
            def update(self, updates, now):
                metric = Metric('fictional.flux', 'Charge flux', 'Invented charge per simulated second',
                                'C/s', .25, levels)
                return ProgressSnapshot(self.context, 'fictional', iteration=levels,
                    metrics=(MetricSeries(metric),), card_groups=groups)
        with tempfile.TemporaryDirectory(prefix='cmw-fictional-card-') as name:
            path = Path(name) / 'invented.txt'
            path.write_text('synthetic\n')
            service = ObservationService(registration_getter=lambda _: Registration(Fictional))
            try:
                snapshot = service.poll(RunContext('three-level', 'FICTIONAL', (SourceDescriptor('invented', str(path)),)))
                for width in (68, 71, 72, 75, 95, 135):
                    text = progress_text(snapshot, width=width)
                    self.assertIn('geometry 3 → outer SCF 2 → inner SCF 8', text)
                    self.assertIn('C/s @G3/O2/I8', text)
                    self.assertIn('Target-free observation', text)
                    self.assertNotIn('Timing', text)
                    self.assertTrue(all(cell_width(line) <= width for line in text.splitlines()))
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
