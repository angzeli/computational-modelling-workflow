"""Main-card electronic notation leaves numerical evidence and other views intact."""
from dataclasses import replace
import math
import unittest

from cmw.jobs.snapshot import capture_snapshot, render_snapshot
from cmw.progress.formatting import progress_text
from cmw.progress.models import CardCell, CardGroup, Criterion, Metric, MetricSeries, ProgressSnapshot, RunContext
from tests.progress.test_scientific_card import example
from tests.progress.test_card_frame import separators


def without_value_format(snapshot):
    return replace(snapshot, card_groups=tuple(replace(group, rows=tuple(
        tuple(replace(cell, value_format='') if cell.kind == 'metric' else cell for cell in row) for row in group.rows))
        for group in snapshot.card_groups))


def electronic_example(**kwargs):
    snapshot = example(**kwargs)
    values = {'dE': (.00015254, 5e-9, '.15254E-03'),
              'd_eps': (.00000069071, 5e-12, '-.69071E-06'),
              'rms': (.000415, 5e-7, '.415E-03'),
              'rms_c': (.000415, 5e-7, '.415E-03')}
    metrics = []
    for series in snapshot.metrics:
        value, precision, printed = values[series.latest.semantic_id.rsplit('.', 1)[-1]]
        metrics.append(replace(series, latest=replace(series.latest, value=value, precision=precision,
            printed=printed, signed_value=-value if series.latest.semantic_id.endswith('.d_eps') else value)))
    return replace(snapshot, metrics=tuple(metrics))


class ElectronicNotationTests(unittest.TestCase):
    def render(self, snapshot, width=133, max_lines=8):
        return progress_text(snapshot, width=width, max_lines=max_lines, now=112, framed=True)

    def test_requested_values_preserve_units_context_footer_and_columns(self):
        for checked in (None, 6, 8):
            snapshot = electronic_example(checked=checked)
            before = snapshot.to_dict()
            for width in (70, 73, 93, 133):
                with self.subTest(checked=checked, width=width):
                    text = self.render(snapshot, width)
                    baseline = self.render(without_value_format(snapshot), width)
                    for value in ('1.5254e-04 eV @SCF8', '6.9071e-07 eV @SCF8', '4.15e-04 @SCF8'):
                        self.assertIn(value, text)
                    lines, previous = text.splitlines(), baseline.splitlines()
                    self.assertEqual(lines[:3], previous[:3])
                    self.assertEqual(lines[5:], previous[5:])
                    self.assertEqual([separators(line) for line in lines[1:5]],
                                     [separators(line) for line in previous[1:5]])
                    self.assertIn('EDIFF 1E-5 eV', lines[4])
            self.assertEqual(snapshot.to_dict(), before)

    def test_native_precision_zero_large_and_extreme_finite_values(self):
        for value, precision, expected in ((97.9, .05, '9.79e+01'), (23.1, .05, '2.31e+01'),
                (1.234567, .0005, '1.235e+00'), (1.2, .05, '1.2e+00'), (0., .0005, '0e+00'),
                (1e-300, 5e-305, '1e-300'), (1e300, 5e295, '1e+300')):
            with self.subTest(value=value):
                snapshot = electronic_example()
                series = snapshot.metrics[0]
                snapshot = replace(snapshot, metrics=(replace(series, latest=replace(series.latest,
                    value=value, precision=precision)), *snapshot.metrics[1:]))
                self.assertIn(expected + ' eV @SCF8', self.render(snapshot))
        snapshot = electronic_example()
        series = snapshot.metrics[0]
        snapshot = replace(snapshot, metrics=(replace(series, latest=replace(series.latest,
            value=math.ulp(0.), precision=None)), *snapshot.metrics[1:]))
        self.assertIn('4.9407e-324 eV @SCF8', self.render(snapshot))

    def test_long_scf_context_reduces_digits_without_clipping_labels(self):
        snapshot = electronic_example(active=301, complete=300, checked=299)
        text = self.render(snapshot, width=70, max_lines=6)
        self.assertIn('|d eps| 6.907e-07 eV @SCF300', text)
        self.assertIn('rms', text)
        self.assertNotIn('…', text.splitlines()[3])
        self.assertEqual(len(text.splitlines()), 6)

    def test_missing_and_nonfinite_values_keep_existing_handling(self):
        snapshot = electronic_example()
        missing = self.render(replace(snapshot, metrics=()))
        for value in ('|dE| —', '|d eps| —', 'rms —'):
            self.assertIn(value, missing)
        for value in (None, math.nan, math.inf, -math.inf):
            changed = replace(snapshot, metrics=tuple(replace(series, latest=replace(series.latest, value=value))
                                                      for series in snapshot.metrics))
            self.assertEqual(self.render(changed), self.render(without_value_format(changed)))

    def test_force_and_targets_are_identical_including_native_zero_precision(self):
        for value, printed in ((.014864, '0.014864'), (.02, '0.020000')):
            snapshot = electronic_example(calculation='fixed-cell-relaxation')
            force = Metric('vasp.force.maximum_atomic_norm.free', 'Fmax', 'Synthetic force', 'eV/Angstrom',
                value, snapshot.iteration[:1], printed=printed, precision=5e-7, observed_at=100,
                criterion=Criterion(.02, 'eV/Angstrom', '<', 'OUTCAR effective EDIFFG'))
            snapshot = replace(snapshot, metrics=(*snapshot.metrics, MetricSeries(force)))
            for width in (70, 133):
                text = self.render(snapshot, width)
                baseline = self.render(without_value_format(snapshot), width)
                self.assertEqual(text.splitlines()[2], baseline.splitlines()[2])
                self.assertIn(f'{value:g} eV/Å', text)
                self.assertIn('Target 0.020 eV/Å', text)
                self.assertIn('EDIFF 1E-5 eV', text)

    def test_generic_and_orca_default_formats_remain_decimal(self):
        for engine in ('ORCA', 'FICTIONAL'):
            metric = Metric('generic.change', 'change', 'Synthetic generic metric', 'Eh', .00015254)
            snapshot = ProgressSnapshot(RunContext('generic', engine), 'test', metrics=(MetricSeries(metric),),
                card_groups=(CardGroup('ENERGY', ((CardCell(('generic.change',), 'change'),),)),))
            text = self.render(snapshot)
            self.assertIn('0.00015254 Eh', text)
            self.assertNotIn('1.5254e-04', text)

    def test_rms_c_uses_opt_in_only_when_a_descriptor_displays_it(self):
        snapshot = electronic_example()
        electronic = snapshot.card_groups[-1]
        rms = replace(electronic.rows[1][0], metric_ids=('vasp.electronic.rms_c',), label='rms(c)')
        snapshot = replace(snapshot, card_groups=(*snapshot.card_groups[:-1], replace(electronic,
            rows=(electronic.rows[0], (rms, electronic.rows[1][1])))))
        text = self.render(snapshot)
        self.assertIn('rms(c)', text)
        self.assertIn('4.15e-04 @SCF8', text)

    def test_plain_details_and_copy_retain_existing_values_and_precision(self):
        snapshot = electronic_example()
        snapshot = replace(snapshot, context=replace(snapshot.context, job_id=1, attempt_id='synthetic'))
        baseline = without_value_format(snapshot)
        for detailed in (False, True):
            self.assertEqual(progress_text(snapshot, detailed=detailed, width=133, now=112),
                             progress_text(baseline, detailed=detailed, width=133, now=112))
        job = dict(id=1, attempt_id='synthetic', display_id='J1.1', name='Synthetic', status='Run', engine='VASP')
        captured = capture_snapshot(job, progress=snapshot, captured_at=112, runtime={})
        old = capture_snapshot(job, progress=baseline, captured_at=112, runtime={})
        for mode in ('summary', 'diagnostics'):
            self.assertEqual(render_snapshot(captured, mode=mode), render_snapshot(old, mode=mode))
        self.assertEqual(captured.to_dict()['progress']['metrics'], old.to_dict()['progress']['metrics'])
        before = render_snapshot(captured, mode='json')
        self.render(snapshot)
        self.assertEqual(render_snapshot(capture_snapshot(job, progress=snapshot, captured_at=112, runtime={}), mode='json'), before)
        self.assertEqual(snapshot.metrics[1].latest.signed_value, -.00000069071)


if __name__ == '__main__':
    unittest.main()
