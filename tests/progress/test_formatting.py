"""Deterministic presentation of shared progress evidence; no engine execution."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from cmw.progress.formatting import (
    card_series, cell_width, comparison_reason, duration_text, fit_text,
    history_entry_text, progress_text, timing_text, trend_text,
)
from cmw.progress.models import (
    Criterion, HistoryEntry, IterationLevel, Metric, MetricSeries,
    ProgressSnapshot, RunContext, SourceDescriptor, Timing,
)


def level(index, kind='evaluation'):
    return IterationLevel(kind, index, 'SCF' if kind == 'electronic' else 'evaluation', 300 if kind == 'electronic' else None)


def metric(value, **kwargs):
    defaults = dict(context=(level(26),), comparison_context=(),
                    criterion=Criterion(.02, 'eV/Angstrom', '<', 'synthetic native criterion'),
                    priority=10, precision=.00001)
    defaults.update(kwargs)
    return Metric('fixture.force', 'Fmax free', 'Invented atomic vector norm', 'eV/Angstrom', value, **defaults)


def active_snapshot(*, pending=False):
    force = MetricSeries(metric(.12, qualification='provisional'),
                         metric(.08, context=(level(25),), qualification='provisional'))
    levels = (level(27), level(2, 'electronic'))
    electronic = Metric('fixture.energy_change', '|dE|', 'Invented electronic energy-change magnitude',
                        'eV', .004, levels, priority=20, comparison_context=levels[:1],
                        criterion=Criterion(.00001, 'eV', '<', 'synthetic EDIFF'), precision=.000001)
    previous = replace(electronic, value=.008, context=(level(27), level(1, 'electronic')))
    return ProgressSnapshot(RunContext('synthetic-card', 'FICTIONAL'), 'fixture',
        phase='Electronic SCF', iteration=levels,
        metrics=(force,) if pending else (force, MetricSeries(electronic, previous)),
        completed_durations=(Timing('wall', 'Evaluation duration', 7205, source='native',
                                   precision=.5, completeness='COMPLETE', context=(level(26),)),
                             Timing('wall', 'Evaluation duration', 3606, source='native',
                                    precision=.5, completeness='COMPLETE', context=(level(25),))),
        read_health='OK', last_new_evidence_at=100)


class DurationFormattingTests(unittest.TestCase):
    def test_seconds_minutes_hours_days_and_long_durations(self):
        for value, expected in ((0, '00:00:00'), (75, '00:01:15'), (6444, '01:47:24'),
                                (10390, '02:53:10'), (90061, '25:01:01'),
                                (3600000001, '1000000:00:01')):
            with self.subTest(value=value):
                self.assertEqual(duration_text(value, precision=.5), expected)

    def test_fractional_resolution_and_rounding_do_not_invent_precision(self):
        self.assertEqual(duration_text(.25, precision=.005), '00:00:00.25')
        self.assertEqual(duration_text(59.9999, precision=.005), '~00:01:00.00')
        self.assertEqual(duration_text(10390, precision=5), '~02:53:10')
        self.assertEqual(duration_text(75), '~00:01:15')
        self.assertEqual(duration_text(0, precision=None), '~00:00:00')
        self.assertEqual(duration_text(2, unit='native ticks'), '2 native ticks')

    def test_invalid_unavailable_and_nonfinite_values_are_explicit(self):
        self.assertEqual(duration_text(None), 'unavailable')
        for value in (-1, float('nan'), float('inf'), -float('inf'), 'invalid'):
            self.assertIn('unavailable', duration_text(value))
        for precision in (None, 0, -1, float('nan'), float('inf')):
            self.assertTrue(duration_text(75, precision=precision).startswith('~'))

    def test_clock_source_interval_and_raw_history_are_preserved(self):
        wall = Timing('wall', 'Native wall', 75, source='native', precision=.5, completeness='COMPLETE')
        cpu = replace(wall, timing_id='cpu', clock='cpu')
        observer = replace(wall, timing_id='seen', source='observer', clock='monotonic',
                           quantity='observation-interval', completeness='BOUNDED', precision=None,
                           reason='Buffered output; execution start unknown')
        self.assertEqual(timing_text(wall, compact=True), '00:01:15 wall/native')
        self.assertEqual(timing_text(cpu, compact=True), '00:01:15 CPU/native')
        interval = timing_text(observer, compact=True)
        for text in ('~00:01:15', 'monotonic', 'observer', 'observation-interval', 'bounded'):
            self.assertIn(text, interval)
        lower = replace(observer, value=None, lower_bound=2)
        self.assertIn('≥~00:00:02 observed', timing_text(lower))
        details = history_entry_text(HistoryEntry('entry', 1, timings=(wall, cpu, observer)))
        for text in ('Raw timing: 75 s', 'printed resolution 0.5', 'CPU', 'Buffered output'):
            self.assertIn(text, details)


class MetricTrendTests(unittest.TestCase):
    def test_increasing_decreasing_equal_missing_and_signed_values(self):
        for latest, previous, expected in ((.12, .08, '↑ +0.04'), (.08, .12, '↓ -0.04'),
                                           (.12, .12, '= 0'), (-.08, -.12, '↑ +0.04')):
            self.assertEqual(trend_text(metric(latest), metric(previous)), expected)
        self.assertEqual(trend_text(metric(.12), None), '')
        self.assertEqual(trend_text(metric(None), metric(.12)), '')
        self.assertEqual(trend_text(metric(float('nan')), metric(.12)), '')

    def test_precision_limited_and_small_real_difference(self):
        self.assertTrue(trend_text(metric(.120001), metric(.12)).startswith('≈ Δ'))
        tiny = trend_text(metric(2e-12, precision=1e-15), metric(1e-12, precision=1e-15))
        self.assertEqual(tiny, '↑ +1e-12')
        self.assertNotIn('+0 ', tiny)

    def test_incompatible_scope_does_not_produce_a_trend(self):
        current = metric(.12)
        changes = ({'unit': 'Eh/bohr'}, {'stage': 'different'}, {'segment': 2},
                   {'definition': 'Maximum component, not a vector norm'},
                   {'criterion': Criterion(.03, 'eV/Angstrom', '<', 'other criterion')},
                   {'comparison_scope': 'different constraints'},
                   {'comparison_context': (level(27),)})
        for change in changes:
            previous = replace(current, **change)
            self.assertEqual(trend_text(current, previous), '')
            self.assertIn('Not comparable', comparison_reason(current, previous))
            with self.assertRaises(ValueError):
                MetricSeries(current, previous)


class ProgressCardTests(unittest.TestCase):
    def test_current_scf_and_previous_complete_force_keep_their_contexts(self):
        snapshot = active_snapshot()
        before = snapshot.to_dict()
        for compact, width, count in ((True, 72, 6), (True, 75, 6), (True, 95, 7), (False, 135, 11)):
            with self.subTest(width=width):
                card = progress_text(snapshot, compact=compact, width=width, max_lines=count, now=110)
                self.assertLessEqual(len(card.splitlines()), count)
                self.assertTrue(all(cell_width(line) <= width for line in card.splitlines()))
                self.assertLess(card.index('|dE|'), card.index('Fmax free'))
                force_line = next(line for line in card.splitlines() if 'Fmax free' in line)
                self.assertIn('@26', force_line)
                self.assertNotIn('@27', force_line)
                self.assertIn('@25', force_line)
                self.assertIn('provisional target', card)
                self.assertIn('02:00:05', card)
                self.assertIn('01:00:06', card)
                self.assertIn('Evidence 10s ago', card)
                self.assertNotIn('CONVERGED', card)
                if not compact:
                    for heading in ('LATEST', 'PREVIOUS', 'TARGET', 'DELTA'):
                        self.assertIn(heading, card)
        self.assertEqual(snapshot.to_dict(), before)

    def test_pending_current_does_not_promote_previous_cycle_electronic_values(self):
        snapshot = active_snapshot(pending=True)
        old = Metric('fixture.energy_change', '|dE|', 'Invented electronic change', 'eV', .0004,
                     (level(26), level(8, 'electronic')), priority=20)
        snapshot = replace(snapshot, metrics=(*snapshot.metrics, MetricSeries(old)))
        card = progress_text(snapshot, compact=True, width=75, now=110)
        self.assertIn('Current metrics: — (pending / unavailable)', card)
        self.assertIn('@26', card)
        self.assertNotIn('.0004 @27', card)
        self.assertNotIn('0.12 @27', card)

    def test_complete_geometry_prefers_geometry_priority_and_static_has_no_target(self):
        snapshot = active_snapshot()
        complete_force = replace(snapshot.metrics[0].latest, context=(level(27),))
        snapshot = replace(snapshot, metrics=(MetricSeries(complete_force), snapshot.metrics[1]),
                           completeness='COMPLETE_EVALUATION')
        self.assertEqual(card_series(snapshot, 2)[0].latest.semantic_id, 'fixture.force')
        static = replace(complete_force, criterion=None, qualification='observed')
        card = progress_text(replace(snapshot, metrics=(MetricSeries(static),)), compact=True, width=75)
        self.assertIn('T:—', card)
        self.assertNotIn('T:<', card)

    def test_rounded_pair_values_remain_distinct_when_a_trend_is_shown(self):
        current = Metric('invented.energy', 'SCF energy', 'Invented native energy', 'Eh', -10.01,
                         (level(27), level(2, 'electronic')), comparison_context=(), priority=1)
        previous = replace(current, value=-10, context=(level(27), level(1, 'electronic')))
        snapshot = replace(active_snapshot(), metrics=(MetricSeries(current, previous),))
        for compact, width in ((True, 75), (False, 135)):
            card = progress_text(snapshot, compact=compact, width=width)
            self.assertIn('-10.01', card)
            self.assertIn('↓ -0.01', card)
        pending = progress_text(active_snapshot(pending=True), compact=True, width=75, max_lines=3)
        self.assertIn('metrics pending', pending.splitlines()[0])

    def test_compact_long_labels_keep_the_native_unit_complete(self):
        current = Metric('invented.rms', 'RMS density change', 'Invented native residual', 'unspecified', .004,
                         (level(27), level(2, 'electronic')), comparison_context=(), priority=1,
                         criterion=Criterion(.000001, 'unspecified', '<', 'synthetic native criterion'))
        previous = replace(current, value=.008, context=(level(27), level(1, 'electronic')))
        snapshot = replace(active_snapshot(), metrics=(MetricSeries(current, previous),))
        card = progress_text(snapshot, compact=True, width=72)
        self.assertIn('[unspecified]', card)
        self.assertIn('T:<1e-06', card)

    def test_unavailable_and_fictional_three_level_context_unit(self):
        levels = tuple(IterationLevel(kind, n, kind) for kind, n in (('geometry', 4), ('outer', 2), ('inner', 9)))
        flux = Metric('fictional.flux', 'Charge flux', 'Invented charge per simulated second', 'C/s', None, levels,
                      comparison_context=levels[:-1])
        snapshot = ProgressSnapshot(RunContext('invented', 'UNIMPLEMENTED'), 'invented',
                                    iteration=levels, metrics=(MetricSeries(flux),))
        card = progress_text(snapshot, compact=True, width=75)
        self.assertIn('C/s', card)
        self.assertIn('— @4/2/9', card)
        self.assertIn('T:—', card)
        self.assertNotIn('0 @4', card)

    def test_target_uncertainty_diagnostic_qualification_and_details(self):
        metric_value = metric(.02, qualification='provisional', relation='PROVISIONAL_UNCERTAIN',
                              diagnostic_targets=(Criterion(.01, 'eV/Angstrom', '<', 'caller', kind='diagnostic'),))
        snapshot = replace(active_snapshot(), metrics=(MetricSeries(metric_value),))
        card = progress_text(snapshot, compact=True, width=72)
        self.assertIn('T:<0.02*?', card)
        self.assertIn('* provisional target', card)
        self.assertIn('? uncertain', card)
        details = progress_text(snapshot, detailed=True)
        self.assertIn('Caller diagnostic target', details)
        self.assertIn('No comparable previous observation', details)
        self.assertIn('raw value 7205 s; printed resolution 0.5', details)
        caller = replace(metric_value, qualification='caller-declared', relation=None,
                         criterion=Criterion(.01, 'eV/Angstrom', '<', 'caller', kind='diagnostic'))
        self.assertIn('! caller-declared', progress_text(replace(snapshot, metrics=(MetricSeries(caller),)), compact=True, width=75))

    def test_conflicted_or_unsupported_metric_evidence_stays_qualified_without_targets(self):
        snapshot = active_snapshot()
        metric_value = replace(snapshot.metrics[0].latest, criterion=None, qualification='provisional')
        for status in ('SOURCE_CONFLICT', 'UNSUPPORTED'):
            observed = replace(snapshot, status=status, metrics=(MetricSeries(metric_value),),
                               messages=('Synthetic source correspondence conflict',))
            card = progress_text(observed, compact=True, width=72, now=110)
            self.assertIn(status, card)
            self.assertIn('* provisional', card)
            self.assertIn('Evidence 10s ago', card)
            self.assertIn('0.12* @26', card)
            self.assertIn('T:—', card)
            self.assertIn('Synthetic source correspondence conflict', progress_text(observed, detailed=True))

    def test_unresolved_card_is_bounded_actionable_and_keeps_distinct_errors_in_details(self):
        qualification = 'Location declarations do not establish process ownership or scientific validity.'
        reasons = ('No unique declared output/stage: LOCATION_UNKNOWN',
                   'outcar: permission denied', 'SOURCE_CONFLICT: input identity differs')
        context = RunContext('unresolved', 'VASP', binding_status='SOURCE_UNRESOLVED',
                             limitations=(*reasons, reasons[0], qualification))
        snapshot = ProgressSnapshot(context, 'fixture', status='SOURCE_UNRESOLVED',
                                    messages=(*reasons, reasons[0]), limitations=context.limitations)
        card = progress_text(snapshot, compact=True, width=72, max_lines=5)
        self.assertEqual(len(card.splitlines()), 5)
        self.assertTrue(all(cell_width(line) <= 72 for line in card.splitlines()))
        for reason in reasons:
            self.assertEqual(card.count(reason), 1)
        self.assertIn('Scientific: SOURCE_UNRESOLVED', card)
        self.assertIn('jobs progress --source', card)
        self.assertNotIn(qualification, card)
        details = progress_text(snapshot, detailed=True)
        for reason in reasons:
            self.assertEqual(details.count(reason), 1)
        self.assertIn(qualification, details)
        self.assertIn('--state STATE progress JOB --source', details)
        self.assertIn('Use the same Jobs state and attempt', details)
        self.assertIn('do not update the dashboard binding', details)
        narrow = progress_text(snapshot, compact=True, width=72, max_lines=3)
        self.assertIn(reasons[0], narrow)
        self.assertIn('+2; Enter details', narrow)
        self.assertEqual(len(narrow.splitlines()), 3)

    def test_unresolved_cached_metrics_retain_age_qualification_and_read_health(self):
        cached = replace(active_snapshot(), status='SOURCE_UNRESOLVED', read_health='UNAVAILABLE',
                         messages=('Declared source is unavailable',))
        card = progress_text(cached, compact=True, width=72, max_lines=6, now=130)
        self.assertIn('Cached scientific: SOURCE_UNRESOLVED', card)
        self.assertIn('* provisional', card)
        self.assertIn('Evidence 30s ago', card)
        self.assertIn('UNAVAILABLE', card)
        self.assertIn('0.12* @26', card)
        details = progress_text(cached, detailed=True, now=130)
        self.assertIn('Declared source is unavailable', details)
        self.assertIn('new evidence 30s ago', details)
        self.assertIn('provisional', details)

    def test_cell_aware_limits_and_tiny_budget_preserve_freshness(self):
        self.assertEqual(cell_width('a界e\u0301'), 4)
        self.assertEqual(fit_text('a界bc', 4), 'a界…')
        card = progress_text(active_snapshot(), compact=True, width=39, max_lines=3, now=110)
        self.assertEqual(len(card.splitlines()), 3)
        self.assertTrue(all(cell_width(line) <= 39 for line in card.splitlines()))
        self.assertIn('Evidence', card)

    def test_actual_native_adapters_keep_definitions_and_do_not_mutate_evidence(self):
        from cmw.progress.service import ObservationService
        from tests.periodic.vasp.result_fixtures import write_run
        from tests.progress.orca_fixtures import output
        with tempfile.TemporaryDirectory(prefix='cmw-formatting-') as name:
            root = Path(name).resolve()
            write_run(root, relaxation=True, evaluations=2)
            (root / 'orca.out').write_text(output())
            contexts = (
                RunContext('synthetic-vasp', 'VASP', tuple(SourceDescriptor(role.lower(), str(root / role))
                                                        for role in ('OUTCAR', 'OSZICAR', 'POSCAR'))),
                RunContext('synthetic-orca', 'ORCA', (SourceDescriptor('orca_output', str(root / 'orca.out'), stage='OPT'),), stage='OPT'),
            )
            for context in contexts:
                service = ObservationService()
                try:
                    snapshot = service.poll(context)
                    before = snapshot.to_dict()
                    details = progress_text(snapshot, detailed=True)
                    progress_text(snapshot, compact=True, width=75)
                    progress_text(snapshot, width=135)
                    self.assertEqual(snapshot.to_dict(), before)
                    self.assertEqual(snapshot.scientific_validation, 'not-assessed')
                    if context.engine == 'VASP':
                        self.assertIn('Maximum Euclidean atomic force-vector norm', details)
                        charge = next(item.latest for item in snapshot.metrics if item.latest.label == 'rms(c)')
                        self.assertIsNone(charge.criterion)
                    else:
                        self.assertIn('maximum gradient component; not a per-atom force-vector norm', details)
                        self.assertIn('Eh/bohr', details)
                finally:
                    service.close()


if __name__ == '__main__':
    unittest.main()
