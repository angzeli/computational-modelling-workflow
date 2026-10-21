"""Optional terminal framing preserves descriptor evidence and handoff payloads."""
from dataclasses import replace
import unittest

from cmw.jobs.snapshot import capture_snapshot, render_snapshot
from cmw.progress.formatting import cell_width, progress_text
from cmw.progress.models import CardCell, CardGroup, Criterion, Metric, MetricSeries, ProgressEvent, ProgressSnapshot, RunContext
from cmw.progress.vasp_card import vasp_card_groups
from tests.progress.test_scientific_card import example


BOX = '─│┌┐└┘├┤┴'


def separators(line):
    return tuple(cell_width(line[:index]) for index, char in enumerate(line) if char == '│')


class CardFrameTests(unittest.TestCase):
    def render(self, snapshot, *, width=135, max_lines=11):
        return progress_text(snapshot, framed=True, width=width, max_lines=max_lines, now=112)

    def assert_frame(self, text, width, rows=4, extra=4):
        lines = text.splitlines()
        self.assertEqual(len(lines), rows + extra)
        self.assertEqual({cell_width(line) for line in lines}, {width})
        self.assertTrue(lines[0].startswith('┌─'))
        self.assertTrue(lines[0].endswith('┐'))
        self.assertTrue(lines[-1].startswith('└') and lines[-1].endswith('┘'))
        self.assertTrue(all(line.startswith('│') and line.endswith('│') for line in lines[1:rows + 1]))
        self.assertEqual(len({separators(line) for line in lines[1:rows + 1]}), 1)
        self.assertEqual(sum(line.startswith('├') for line in lines), int(extra == 4))
        if extra == 4:
            self.assertTrue(lines[-3].startswith('├') and lines[-3].endswith('┤'))
            for offset in separators(lines[1])[1:-1]:
                self.assertEqual(lines[-3][offset], '┴')
        if extra > 2:
            self.assertEqual(lines[-1], '└' + '─' * (width - 2) + '┘')
        else:
            self.assertIn('data 12s ago', lines[-1])
        self.assertFalse(any(char in text for char in '━┃┏┓┗┛═║╔╗╚╝'))

    def test_normal_card_preserves_groups_values_context_and_static_target(self):
        snapshot = example()
        before = snapshot.to_dict()
        text = self.render(snapshot)
        self.assert_frame(text, 135)
        self.assertEqual([line.split('│')[1].strip() for line in text.splitlines()[1:5]],
                         ['SCF', 'FORCE', 'ELECTRONIC', ''])
        for value in ('SCIENTIFIC', 'Eval 1', 'OUTCAR SCF 9/300', '3.1087e+00 eV @SCF8',
                      '2.9332e+00 eV @SCF8', '1.89e+00 @SCF8', 'EDIFF 1E-5 eV',
                      '— (not observed)', 'Target N/A · static', 'data 12s ago'):
            self.assertIn(value, text)
        self.assertEqual(snapshot.to_dict(), before)

    def test_source_local_partial_and_matched_qualifications_survive(self):
        snapshot = example(checked=6)
        cases = (
            (replace(snapshot, verification=(replace(snapshot.verification[0], status='PENDING', checked_frontier=None),)),
             ('stdout SCF 8 source-local', 'outcar pending')),
            (snapshot, ('outcar checked 6', '8 source-local')),
            (example(), ('outcar matched through SCF 8',)),
        )
        for observation, expected in cases:
            with self.subTest(expected=expected):
                text = self.render(observation)
                self.assert_frame(text, 135)
                for value in expected:
                    self.assertIn(value, text.splitlines()[-2])
                self.assertIn('data 12s ago', text.splitlines()[-2])

    def test_missing_metrics_keeps_all_groups_and_independent_targets(self):
        snapshot = replace(example(), metrics=())
        snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
        text = self.render(snapshot)
        self.assert_frame(text, 135)
        for value in ('SCF', 'FORCE', 'ELECTRONIC', '|dE| —', '|d eps| —', 'rms —',
                      'EDIFF 1E-5 eV', 'Target N/A · static'):
            self.assertIn(value, text)
        self.assertNotIn('3.1087', text)

    def test_relaxation_force_and_applicable_target_remain_qualified(self):
        snapshot = example(calculation='fixed-cell-relaxation')
        force = Metric('vasp.force.maximum_atomic_norm.free', 'Fmax free', 'Synthetic free-atom norm',
                       'eV/Angstrom', .012, snapshot.iteration[:1], observed_at=100,
                       criterion=Criterion(.02, 'eV/Angstrom', '<', 'OUTCAR effective EDIFFG'))
        snapshot = replace(snapshot, metrics=(*snapshot.metrics, MetricSeries(force)))
        text = self.render(snapshot)
        self.assert_frame(text, 135)
        self.assertIn('0.012 eV/Å @E1', text)
        self.assertIn('Target 0.020 eV/Å', text)
        self.assertNotIn('Target N/A', text)

    def test_narrow_movable_force_preserves_digits_units_and_evaluation_before_label(self):
        snapshot = example(calculation='fixed-cell-relaxation')
        force = Metric('vasp.force.maximum_atomic_norm.free', 'Fmax free', 'Synthetic free-atom norm',
                       'eV/Angstrom', .014864,
                       (replace(snapshot.iteration[0], index=26),), precision=5e-7, observed_at=100,
                       criterion=Criterion(.02, 'eV/Angstrom', '<', 'OUTCAR effective EDIFFG'))
        snapshot = replace(snapshot, metrics=(*snapshot.metrics, MetricSeries(force)))
        snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
        for width in (70, 73, 75, 77):
            with self.subTest(width=width):
                text = self.render(snapshot, width=width, max_lines=6)
                self.assertIn('0.014864 eV/Å @E26', text)
                self.assertNotIn('0.0149 eV/Å', text)
                self.assertEqual(len(text.splitlines()), 6)
                self.assertTrue(all(cell_width(line) <= width for line in text.splitlines()))
                self.assertEqual(text.count('FORCE'), 1)

    def test_conflict_read_error_and_native_warning_remain_visible(self):
        snapshot = example()
        snapshot = replace(snapshot, status='SOURCE_CONFLICT', read_health='ERROR',
            verification=(replace(snapshot.verification[0], status='CONFLICT'),),
            events=(ProgressEvent('synthetic-fatal', 'native-fatal', None, 1, message='Synthetic warning'),))
        text = self.render(snapshot)
        self.assert_frame(text, 135)
        for value in ('SOURCE CONFLICT', 'stdout/outcar', 'ERROR', 'data 12s ago', 'native warnings 1; Details'):
            self.assertIn(value, text)
        self.assertIn('3.1087e+00 eV @SCF8', text)

    def test_generic_varied_rows_and_unicode_use_display_cell_alignment(self):
        cell = lambda text: CardCell(kind='text', text=text)
        groups = (CardGroup('能量 α', ((cell('ΔE 1.2e-06 eV'),),
                                     (cell('Å 界'), cell('β 中文'), cell('e\u0301 3.1')))),
                  CardGroup('状态', ((cell('[bold]literal[/bold]'), cell('未完成')),)))
        snapshot = ProgressSnapshot(RunContext('generic-frame', 'FICTIONAL'), 'synthetic',
                                    card_groups=groups, read_health='OK')
        text = self.render(snapshot, width=140)
        self.assert_frame(text, 140, rows=3)
        self.assertEqual(len(separators(text.splitlines()[1])), 5)
        for value in ('能量 α', 'ΔE 1.2e-06 eV', 'Å 界', 'β 中文', 'e\u0301 3.1',
                      '[bold]literal[/bold]', '状态', '未完成', 'source observation'):
            self.assertIn(value, text)
        self.assertNotIn('OUTCAR', text)

    def test_progressive_padding_separator_simplification_and_narrow_fallback(self):
        snapshot = example()
        padded = self.render(snapshot, width=95)
        compact = self.render(snapshot, width=75)
        simple = self.render(snapshot, width=70)
        for width, text in ((95, padded), (75, compact), (70, simple)):
            self.assert_frame(text, width)
            self.assertIn('3.1087e+00 eV @SCF8', text)
            self.assertIn('2.9332e+00 eV @SCF8', text)
        self.assertTrue(padded.splitlines()[1].startswith('│ SCF'))
        self.assertTrue(compact.splitlines()[1].startswith('│SCF'))
        self.assertEqual(len(separators(compact.splitlines()[1])), 4)
        self.assertEqual(len(separators(simple.splitlines()[1])), 4)
        for width in (69, 53, 40):
            with self.subTest(width=width):
                actual = self.render(snapshot, width=width)
                plain = progress_text(snapshot, width=width, max_lines=11, now=112)
                legacy = replace(snapshot, card_groups=tuple(replace(group, rows=tuple(
                    tuple(replace(cell, value_format='') if cell.kind == 'metric' else cell for cell in row) for row in group.rows))
                    for group in snapshot.card_groups))
                self.assertEqual(self.render(legacy, width=width), plain)
                self.assertTrue(all(cell_width(line) <= width for line in actual.splitlines()))
                self.assertFalse(any(char in actual for char in BOX))

    def test_height_adaptive_frames_preserve_all_metric_rows_and_evidence(self):
        for width in (70, 73, 78, 83, 88, 93, 133):
            for height in (6, 7, 8):
                for snapshot in (example(), example(checked=6)):
                    with self.subTest(width=width, height=height):
                        text = self.render(snapshot, width=width, max_lines=height)
                        self.assert_frame(text, width, extra=height - 4)
                        for value in ('OUTCAR SCF 9/300', 'FORCE', 'ELECTRONIC',
                                      '3.1087e+00 eV @SCF8', '2.9332e+00 eV @SCF8', '1.89e+00 @SCF8'):
                            self.assertIn(value, text)
                        self.assertIn('data 12s ago', text)
                        if snapshot.verification[0].status == 'PARTIAL':
                            self.assertIn('outcar checked 6', text)
                            self.assertIn('8 source-local', text)
        fallback = self.render(example(), max_lines=5)
        self.assertFalse(any(char in fallback for char in BOX))
        self.assertIn('3.1087e+00 eV @SCF8', fallback)

    def test_compact_conflict_and_long_notice_keep_frame_and_qualifications(self):
        from cmw.progress.models import SettingObservation
        snapshot = replace(example(), read_health='ERROR',
            verification=(replace(example().verification[0], status='CONFLICT'),),
            events=(ProgressEvent('warn', 'native-fatal', None, 1, message='Synthetic'),),
            settings=(SettingObservation('Long synthetic setting name', 1, 'fixture', discrepancy=True),))
        text = self.render(snapshot, width=70, max_lines=6)
        self.assert_frame(text, 70, extra=2)
        for value in ('native warnings 1', 'settings differ', 'SOURCE CONFLICT',
                      'stdout/outcar', 'ERROR', 'data 12s ago'):
            self.assertIn(value, text)

    def test_compact_partial_error_retains_complete_frontier_qualifications(self):
        for observed, checked in ((8, 6), (10, 6), (300, 299)):
            snapshot = replace(example(active=observed + 1, complete=observed, checked=checked), read_health='ERROR')
            for now in (112, 3700):
                with self.subTest(observed=observed, checked=checked, now=now):
                    text = progress_text(snapshot, framed=True, width=70, max_lines=6, now=now)
                    self.assertEqual(len(text.splitlines()), 6)
                    self.assertEqual({cell_width(line) for line in text.splitlines()}, {70})
                    footer = text.splitlines()[-1]
                    for value in ('ERROR', f'stdout {observed} source-local', f'outcar checked {checked}',
                                  'data 12s' if now == 112 else 'data 60.0m'):
                        self.assertIn(value, footer)
                    self.assertNotIn('…', footer)

    def test_columns_stay_fixed_when_metric_values_change(self):
        snapshot = example()
        for width in (70, 73, 135):
            initial = self.render(snapshot, width=width, max_lines=6).splitlines()
            for value, shown in ((0., '0e+00'), (1.23456e-12, '1.2346e-12'), (123456.789, '1.2346e+05')):
                with self.subTest(value=value, width=width):
                    updated = replace(snapshot, metrics=tuple(
                        replace(series, latest=replace(series.latest, value=value)) for series in snapshot.metrics))
                    lines = self.render(updated, width=width, max_lines=6).splitlines()
                    self.assertEqual([separators(line) for line in lines[1:5]],
                                     [separators(line) for line in initial[1:5]])
                    self.assertEqual(lines[-1], initial[-1])
                    self.assertIn(f'{shown} eV @SCF8', lines[3])

    def test_default_details_and_structured_copy_remain_undecorated(self):
        snapshot = example()
        snapshot = replace(snapshot, context=replace(snapshot.context, job_id=1, attempt_id='synthetic-attempt'))
        job = dict(id=1, attempt_id='synthetic-attempt', display_id='J1.1', name='Synthetic', status='Run', engine='VASP')
        runtime = dict(version='synthetic', python='synthetic', executable='/synthetic/python', module='/synthetic/cmw')
        before = snapshot.to_dict()
        captured = capture_snapshot(job, progress=snapshot, captured_at=112, runtime=runtime)
        expected = {mode: render_snapshot(captured, mode=mode) for mode in ('summary', 'diagnostics', 'json')}
        self.assert_frame(self.render(snapshot), 135)
        after = capture_snapshot(job, progress=snapshot, captured_at=112, runtime=runtime)
        for mode, text in expected.items():
            self.assertEqual(render_snapshot(after, mode=mode), text)
            self.assertFalse(any(char in text for char in BOX))
        plain = progress_text(snapshot, width=135, now=112)
        details = progress_text(snapshot, detailed=True, framed=True, width=135, now=112)
        self.assertFalse(any(char in plain + details for char in BOX))
        self.assertEqual(snapshot.to_dict(), before)


if __name__ == '__main__':
    unittest.main()
