"""Invented native timing/history fixtures; no engines or scientific records."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.results import inspect_result
from cmw.progress.models import RunContext, SourceDescriptor, SourceState, SourceUpdate
from cmw.progress.reader import RegularFileReader
from cmw.progress.vasp import VaspAdapter
from tests.periodic.vasp.result_fixtures import native_texts, structure_text


def timing_texts(*, evaluations=2, loops=True, outer=True, footer=False, **kwargs):
    texts = native_texts(relaxation=True, evaluations=evaluations, footer=footer, **kwargs)
    output = []
    for line in texts['OUTCAR'].splitlines():
        if 'reached required accuracy' in line:
            continue
        output.append(line)
        if loops and 'EDDAV:' in line:
            output.append('      LOOP:  cpu time 1.2500: real time 2.5000')
        if outer and 'energy  without entropy=' in line:
            output.append('     LOOP+:  cpu time 5.0000: real time 9.0000')
    texts['OUTCAR'] = '\n'.join(output) + '\n'
    return texts


class VaspHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-vasp-history-')
        self.root = Path(self.temp.name)
        self.context = RunContext('invented-history', 'VASP', tuple(
            SourceDescriptor(role.lower(), str(self.root / role), optional=role == 'POSCAR')
            for role in ('OUTCAR', 'OSZICAR', 'POSCAR')), stage='relax', attempt_id='attempt-one')
        self.readers = []
        self.reset()

    def tearDown(self):
        for reader in self.readers:
            reader.close()
        self.temp.cleanup()

    def reset(self, budget=65536):
        for reader in self.readers:
            reader.close()
        self.readers = [RegularFileReader(source, byte_budget=budget) for source in self.context.sources]
        self.adapter = VaspAdapter(self.context)
        self.updates = {}
        self.entries = {}

    def write(self, texts=None, **kwargs):
        texts = timing_texts(**kwargs) if texts is None else texts
        for role in ('OUTCAR', 'OSZICAR'):
            (self.root / role).write_text(texts[role])
        (self.root / 'POSCAR').write_text(structure_text(flags=((True,) * 3, (False,) * 3)))
        return texts

    def append(self, role, text):
        with (self.root / role).open('a') as handle:
            handle.write(text)

    def poll(self, *, mono=1.0, historical=True):
        self.adapter.set_observation(mono, historical=historical, clock_id='test-clock')
        roles = self.adapter.read_roles()
        updates = []
        for reader in self.readers:
            role = reader.source.role
            if role in roles:
                value = reader.poll(100.0 + mono)
                self.updates[role] = value
            else:
                old = self.updates[role]
                value = replace(old, state=replace(old.state, bytes_read=0), lines=(), reset=False, reason=None)
            updates.append(value)
        snapshot = self.adapter.update(tuple(updates), 100.0 + mono)
        self.entries.update((entry.entry_id, entry) for entry in self.adapter.drain_history())
        return snapshot

    def catchup(self):
        for _ in range(20000):
            snapshot = self.poll()
            if not self.adapter.has_pending_work and not any(item.state.has_more for item in self.updates.values()):
                return snapshot
        self.fail('bounded reader did not catch up')

    def evaluations(self):
        return sorted((item for item in self.entries.values() if item.kind in {'evaluation', 'trial-evaluation'}), key=lambda item: item.sequence)

    def electronic(self, parent=None):
        return sorted((item for item in self.entries.values() if item.kind == 'electronic'
                       and (parent is None or item.parent_id == parent)), key=lambda item: item.sequence)

    @staticmethod
    def wall(entry):
        return next(item for item in entry.timings if item.clock == 'wall')

    def test_native_nested_timers_are_distinct_inclusive_intervals(self):
        self.write()
        snapshot = self.catchup()
        evaluations = self.evaluations()
        self.assertEqual(len(evaluations), 2)
        self.assertEqual(len(self.electronic()), 6)
        self.assertEqual([value.value for value in snapshot.completed_durations], [9.0, 9.0])
        for entry in evaluations:
            self.assertEqual(entry.child_count, 3)
            self.assertEqual(entry.state, 'COMPLETE')
            self.assertEqual(entry.coverage, 'COMPLETE')
            self.assertEqual({value.clock: value.value for value in entry.timings}, {'cpu': 5.0, 'wall': 9.0})
            self.assertIn('do not add nested', self.wall(entry).reason)
            self.assertEqual(sum(self.wall(row).value for row in self.electronic(entry.entry_id)), 7.5)
        self.assertTrue(all(self.wall(row).value == 2.5 for row in self.electronic()))

    def test_timing_before_electronic_energy_has_structural_header_scope(self):
        self.write(evaluations=1)
        self.catchup()
        row = self.electronic()[0]
        self.assertLess(self.wall(row).start_evidence[0].byte_start, self.wall(row).end_evidence[0].byte_start)
        self.assertEqual(self.wall(row).source, 'native')
        self.assertEqual(row.context[-1].index, 1)

    def test_late_outer_timing_upserts_same_evaluation(self):
        self.write(evaluations=1, outer=False)
        snapshot = self.catchup()
        before = self.evaluations()[0]
        self.assertEqual(self.wall(before).source, 'unavailable')
        self.append('OUTCAR', ' LOOP+: cpu time 5.0: real time 8.0\n')
        after_snapshot = self.poll()
        after = self.evaluations()[0]
        self.assertEqual((after.entry_id, after.sequence), (before.entry_id, before.sequence))
        self.assertEqual(len(self.evaluations()), 1)
        self.assertEqual(self.wall(after).value, 8.0)
        self.assertEqual(after_snapshot.completed_durations[0].value, 8.0)

    def test_partial_native_timing_waits_for_newline(self):
        self.write(evaluations=1, outer=False)
        self.catchup()
        self.append('OUTCAR', ' LOOP+: cpu time 5.0: real time 8.')
        self.poll()
        self.assertEqual(self.wall(self.evaluations()[0]).source, 'unavailable')
        self.append('OUTCAR', '0\n')
        self.poll()
        self.assertEqual(self.wall(self.evaluations()[0]).value, 8.0)

    def test_footer_total_never_manufactures_step_durations(self):
        self.write(evaluations=1, loops=False, outer=False, footer=True)
        snapshot = self.catchup()
        self.assertIsNone(snapshot.completed_durations[0].value)
        total = next(item for item in self.entries.values() if item.kind == 'segment-total')
        self.assertEqual({time.clock: time.value for time in total.timings}, {'cpu': 2.0, 'wall': 2.2})
        self.assertTrue(all(time.quantity == 'cumulative' for time in total.timings))
        self.assertTrue(all(self.wall(row).source == 'unavailable' for row in self.electronic()))

    def test_zero_scientific_notation_and_whitespace(self):
        texts = timing_texts(evaluations=1)
        texts['OUTCAR'] = texts['OUTCAR'].replace('cpu time 5.0000: real time 9.0000', 'CPU   time 0D+0 : REAL time 9.00e-1   ')
        self.write(texts)
        self.catchup()
        self.assertEqual({time.clock: time.value for time in self.evaluations()[0].timings}, {'cpu': 0.0, 'wall': 0.9})

    def test_malformed_negative_cpu_wall_and_old_alias_are_unavailable(self):
        for row in ('LOOP+: cpu time -1: real time 3', 'LOOP+: cpu time 1: real time -3',
                    'LOOP+: cpu time nan: real time 3', 'LOOP+: cpu time 1: real time nope',
                    'LOOP+: VPU time 1: CPU time 3'):
            with self.subTest(row=row):
                self.reset()
                texts = timing_texts(evaluations=1, outer=False)
                texts['OUTCAR'] += row + '\n'
                self.write(texts)
                self.catchup()
                self.assertIsNone(self.wall(self.evaluations()[0]).value)
                self.assertTrue(self.evaluations()[0].metrics)

    def test_duplicate_timer_is_ambiguous_instead_of_summed(self):
        self.write(evaluations=1)
        self.catchup()
        self.append('OUTCAR', ' LOOP+: cpu time 5.0: real time 8.0\n')
        self.poll()
        self.assertIsNone(self.wall(self.evaluations()[0]).value)
        self.assertIn('Multiple timing', self.wall(self.evaluations()[0]).reason)

    def test_loop_outside_electronic_interval_is_unavailable(self):
        self.write(evaluations=1, loops=False)
        self.catchup()
        self.append('OUTCAR', ' LOOP: cpu time 1: real time 3\n')
        self.poll()
        self.assertIsNone(self.wall(self.electronic()[-1]).value)
        self.assertIn('outside', self.wall(self.electronic()[-1]).reason)

    def test_repeated_labels_and_trial_text_keep_distinct_chronology(self):
        texts = timing_texts(evaluations=2)
        texts['OUTCAR'] = texts['OUTCAR'].replace('Iteration      2(', 'Iteration      1(')
        texts['OUTCAR'] = texts['OUTCAR'].replace(' POSITION', ' trial: synthetic line-search evidence\n POSITION', 1)
        texts['OSZICAR'] = texts['OSZICAR'].replace('   2 F=', '   1 F=')
        self.write(texts)
        snapshot = self.catchup()
        evaluations = self.evaluations()
        self.assertEqual([item.context[0].index for item in evaluations], [1, 2])
        self.assertEqual([item.native_label for item in evaluations], ['1 / 1', '2 / 1'])
        self.assertEqual(evaluations[0].kind, 'trial-evaluation')
        self.assertEqual(len({item.entry_id for item in evaluations}), 2)
        self.assertTrue(all('Accepted optimization step unestablished' in item.annotations for item in evaluations))
        self.assertEqual(snapshot.status, 'OBSERVED')

    def test_new_segment_visible_and_duration_comparison_does_not_cross(self):
        texts = self.write(evaluations=1)
        self.catchup()
        self.append('OUTCAR', texts['OUTCAR'])
        self.append('OSZICAR', texts['OSZICAR'])
        snapshot = self.catchup()
        self.assertEqual([item.segment for item in self.evaluations()], [1, 2])
        self.assertEqual(len(snapshot.completed_durations), 1)
        self.assertEqual(snapshot.completed_durations[0].segment, 2)

    def test_force_stays_on_completed_evaluation_during_next_scf(self):
        self.write(evaluations=1)
        self.catchup()
        self.append('OUTCAR', ' Iteration 2(1)\n EDDAV: synthetic\n')
        self.append('OSZICAR', 'DAV: 1 -2.000000000000 -2.0 -0.02 12 0.001\n')
        self.poll()
        previous, current = self.evaluations()
        self.assertTrue(any('force.' in item.semantic_id for item in previous.metrics))
        self.assertFalse(any('force.' in item.semantic_id for item in current.metrics))
        self.assertEqual(current.state, 'PARTIAL')

    def test_buffered_completed_batch_has_no_zero_observer_durations(self):
        self.write(evaluations=3, loops=False, outer=False)
        self.poll(mono=5, historical=False)
        self.assertTrue(all(self.wall(item).source == 'unavailable' for item in self.evaluations()))
        self.assertTrue(all(self.wall(item).source == 'unavailable' for item in self.electronic()))

    def test_mid_evaluation_observer_is_only_within_session_interval(self):
        texts = timing_texts(evaluations=1, loops=False, outer=False)
        cut = texts['OUTCAR'].index(' EDDAV:')
        partial = dict(texts, OUTCAR=texts['OUTCAR'][:cut], OSZICAR='')
        self.write(partial)
        self.poll(mono=1)
        self.poll(mono=5, historical=False)
        self.poll(mono=8, historical=False)
        observed = self.wall(self.evaluations()[0])
        self.assertEqual((observed.value, observed.source, observed.quantity), (3, 'observer', 'observation-interval'))
        self.assertIn('unknown start', observed.reason)
        self.append('OUTCAR', texts['OUTCAR'][cut:])
        self.append('OSZICAR', texts['OSZICAR'])
        self.poll(mono=11, historical=False)
        self.assertEqual(self.wall(self.evaluations()[0]).value, 6)
        self.append('OUTCAR', ' Iteration 2(1)\n')
        self.poll(mono=20, historical=False)
        self.assertEqual(self.wall(self.evaluations()[0]).value, 6)
        self.reset()
        self.catchup()
        self.assertEqual(self.wall(self.evaluations()[0]).source, 'unavailable')

    def test_monotonic_reset_is_unavailable_not_zero(self):
        texts = timing_texts(evaluations=1, loops=False, outer=False)
        cut = texts['OUTCAR'].index(' EDDAV:')
        self.write(dict(texts, OUTCAR=texts['OUTCAR'][:cut], OSZICAR=''))
        self.poll(mono=10, historical=False)
        self.poll(mono=2, historical=False)
        self.assertIsNone(self.wall(self.evaluations()[0]).value)

    def test_native_reopening_reconstructs_same_entry_keys_and_values(self):
        self.write(evaluations=2)
        self.catchup()
        before = [(row.entry_id, self.wall(row).value) for row in self.evaluations() + self.electronic()]
        self.reset()
        self.catchup()
        self.assertEqual(before, [(row.entry_id, self.wall(row).value) for row in self.evaluations() + self.electronic()])

    def test_metric_history_is_independent_of_reader_chunking(self):
        self.write(evaluations=20)
        expected = None
        for budget in (65536, 127, 1024):
            with self.subTest(budget=budget):
                self.reset(budget)
                self.catchup()
                actual = [(row.context, row.coverage, tuple((metric.semantic_id, metric.value) for metric in row.metrics),
                           tuple((timing.clock, timing.value) for timing in row.timings))
                          for row in self.evaluations() + self.electronic()]
                self.assertEqual(len(self.evaluations()), 20)
                self.assertEqual(len(self.electronic()), 60)
                if expected is None:
                    expected = actual
                else:
                    self.assertEqual(expected, actual)

    def test_completed_history_force_agrees_with_strict_batch(self):
        self.write(evaluations=2, footer=True)
        self.catchup()
        batch = inspect_result(self.root)
        observed = next(metric for metric in self.evaluations()[-1].metrics
                        if metric.semantic_id == 'vasp.force.maximum_atomic_norm.free')
        self.assertEqual(observed.value, batch['endpoint']['force_summary']['free']['maximum_norm'])

    def test_energy_metrics_reference_actual_numeric_source_lines(self):
        self.write(evaluations=1)
        self.catchup()
        lines = (self.root / 'OUTCAR').read_text().splitlines()
        for entry in self.evaluations() + self.electronic():
            for metric in entry.metrics:
                if metric.semantic_id in {'vasp.electronic.free_energy', 'vasp.evaluation.free_energy'}:
                    self.assertIn('TOTEN', lines[metric.evidence[0].line_start - 1])
                if metric.semantic_id in {'vasp.evaluation.without_entropy', 'vasp.evaluation.sigma_to_zero'}:
                    self.assertIn('without entropy=', lines[metric.evidence[0].line_start - 1])

    def test_timing_source_reset_changes_stable_entry_identity(self):
        self.write(evaluations=1)
        self.catchup()
        first_ids = {item.entry_id for item in self.evaluations()}
        replacement = self.root / 'replacement'
        replacement.write_text(timing_texts(evaluations=1)['OUTCAR'])
        replacement.replace(self.root / 'OUTCAR')
        self.entries.clear()
        self.poll()
        current_ids = {item.entry_id for item in self.evaluations()}
        self.assertTrue(current_ids)
        self.assertFalse(first_ids & current_ids)
        self.assertTrue(all(item.coverage == 'UNRESOLVED' for item in self.evaluations()))

    def test_initial_poscar_catchup_precedes_historical_force_derivation(self):
        self.write(evaluations=1)
        expected = None
        for budget in (65536, 7):
            self.reset(budget)
            self.catchup()
            values = [(metric.semantic_id, metric.value) for metric in self.evaluations()[0].metrics]
            if expected is None:
                expected = values
            else:
                self.assertEqual(expected, values)

    def test_observer_interval_stops_at_incomplete_evaluation_boundary(self):
        texts = timing_texts(evaluations=1, loops=False, outer=False)
        cut = texts['OUTCAR'].index(' EDDAV:')
        self.write(dict(texts, OUTCAR=texts['OUTCAR'][:cut], OSZICAR=''))
        self.poll(mono=5, historical=False)
        self.append('OUTCAR', ' Iteration 2(1)\n EDDAV: synthetic\n')
        self.poll(mono=9, historical=False)
        previous = self.evaluations()[0]
        self.assertEqual(previous.state, 'INCOMPLETE')
        self.assertEqual(self.wall(previous).value, 4)
        self.poll(mono=15, historical=False)
        self.assertEqual(self.wall(self.evaluations()[0]).value, 4)

    def test_unsupported_mode_keeps_metrics_without_native_timer_claim(self):
        self.write(evaluations=1, settings={'IBRION': '0'})
        self.catchup()
        self.assertEqual(self.wall(self.evaluations()[0]).source, 'unavailable')
        self.assertTrue(self.evaluations()[0].metrics)


if __name__ == '__main__':
    unittest.main()
