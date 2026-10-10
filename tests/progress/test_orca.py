"""Synthetic incremental ORCA grammar and scientific-boundary regressions."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from cmw.molecular.orca.status import parse_orca_output
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.formatting import history_cell
from cmw.progress.reader import RegularFileReader
from cmw.progress.orca import OrcaAdapter, MAX_PENDING_HISTORY
from tests.progress.orca_fixtures import header, scf, geometry, cycle, output


class OrcaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cmw-orca-progress-')
        self.root = Path(self.tmp.name)
        self.path = self.root / 'declared.out'
        self.readers = []
        self.context = RunContext('fictional-orca', 'ORCA',
            (SourceDescriptor('orca_output', str(self.path), stage='OPT'),), stage='OPT', attempt_id='one')
        self.reset()

    def tearDown(self):
        for r in self.readers:
            r.close()
        self.tmp.cleanup()

    def reset(self, budget=65536):
        for r in self.readers:
            r.close()
        self.readers = [RegularFileReader(s, byte_budget=budget) for s in self.context.sources]
        self.adapter = OrcaAdapter(self.context)
        self.entries = {}
        self.updates = {}

    def write(self, text):
        self.path.write_text(text)

    def append(self, text):
        with self.path.open('a') as f:
            f.write(text)

    def poll(self, mono=1.0, historical=True, clock='test'):
        self.adapter.set_observation(mono, historical=historical, clock_id=clock)
        updates = []
        roles = self.adapter.read_roles()
        for reader in self.readers:
            role = reader.source.role
            if role in roles:
                update = reader.poll(100 + mono)
                self.updates[role] = update
            else:
                update = replace(self.updates[role], lines=(), reset=False, reason=None)
            updates.append(update)
        result = self.adapter.update(tuple(updates), 100 + mono)
        self.entries.update((x.entry_id, x) for x in self.adapter.drain_history())
        return result

    def catchup(self):
        for _ in range(20000):
            snap = self.poll()
            if not self.adapter.has_pending_work and not any(u.state.has_more for u in self.updates.values()):
                return snap
        self.fail('did not catch up')

    def metric(self, snapshot, name):
        return next(m.latest for m in snapshot.metrics if m.latest.semantic_id == name)

    def test_native_diis_soscf_geometry_and_batch_parity(self):
        text = output()
        self.write(text)
        snap = self.catchup()
        batch = parse_orca_output(text)
        self.assertTrue(batch.scf_converged and batch.optimization_converged and batch.normal_termination)
        self.assertEqual(self.metric(snap, 'orca.final_energy').value, batch.final_energy_hartree)
        self.assertEqual(self.metric(snap, 'orca.geometry.max_gradient_component').value, .0002)
        self.assertEqual(self.metric(snap, 'orca.geometry.max_gradient_component').unit, 'Eh/bohr')
        self.assertEqual(len([e for e in self.entries.values() if e.kind == 'scf']), 6)
        self.assertEqual(len([e for e in self.entries.values() if e.kind == 'geometry']), 2)
        self.assertEqual(snap.scientific_validation, 'not-assessed')
        self.assertTrue(any(e.kind == 'scf-converged' for e in snap.events))
        runtime = next(e for e in self.entries.values() if e.kind == 'run-timing')
        self.assertEqual(runtime.timings[0].value, 9.125)
        self.assertEqual(runtime.timings[0].quantity, 'cumulative')

    def test_history_context_descriptor_renders_native_labels(self):
        self.write(header() + cycle())
        self.catchup()
        column = self.adapter._columns()[0]
        geometry_entry = next(e for e in self.entries.values() if e.kind == 'geometry')
        scf_entry = next(e for e in self.entries.values() if e.kind == 'scf')
        self.assertEqual(history_cell(geometry_entry, column), 'OPT cycle 1 / 40 allowed')
        self.assertIn('SCF 1', history_cell(scf_entry, column))
        self.assertNotIn('IterationLevel(', history_cell(scf_entry, column))

    def test_all_criteria_distinct_and_no_global_and(self):
        self.write(header() + cycle())
        snap = self.catchup()
        maximum = self.metric(snap, 'orca.geometry.max_gradient_component')
        rms = self.metric(snap, 'orca.geometry.rms_gradient')
        self.assertEqual(maximum.relation, 'BELOW')
        self.assertEqual(rms.relation, 'AT_OR_ABOVE')
        self.assertIn('native-yes', maximum.qualification)
        self.assertIn('native-no', rms.qualification)
        self.assertFalse(any(e.kind == 'optimization-converged' for e in snap.events))
        self.assertEqual(len([m for m in snap.metrics if m.latest.semantic_id.startswith('orca.geometry.')]), 5)
        self.assertTrue(any('no synthetic global AND' in m for m in snap.messages))

    def test_override_and_rounding_native_disagreement(self):
        self.write(header() + cycle(table=False) + geometry(max_gradient='0.0003000000', max_target='0.0003000000', max_native='YES'))
        snap = self.catchup()
        metric = self.metric(snap, 'orca.geometry.max_gradient_component')
        self.assertEqual(metric.relation, 'UNCERTAIN')
        self.assertEqual(metric.criterion.value, .0003)
        self.assertIn('native-yes', metric.qualification)
        self.assertIn('numeric-uncertain', metric.qualification)
        self.append(cycle(2, table=False) + geometry(max_target='0.0000100000', max_native='NO'))
        snap = self.catchup()
        metric = self.metric(snap, 'orca.geometry.max_gradient_component')
        self.assertEqual(metric.criterion.value, .00001)
        series = next(s for s in snap.metrics if s.latest == metric)
        self.assertIsNone(series.previous)

    def test_missing_threshold_unknown_mode_and_preset_not_defaulted(self):
        text = (header(mode='unknown-mode').replace('HF STO-3G OPT', 'HF STO-3G TightOpt') +
                cycle(table=False) + geometry().replace('0.0002000000 0.0003000000 YES', '0.0002000000 YES'))
        self.write(text)
        snap = self.catchup()
        self.assertIsNone(self.metric(snap, 'orca.geometry.max_gradient_component').criterion)
        self.assertTrue(any('Unrecognized convergence-check' in m for m in snap.messages))

    def test_partial_geometry_table_no_zero_fill(self):
        self.write(header() + cycle(table=False) + geometry(complete=False))
        snap = self.catchup()
        self.assertFalse(any(s.latest.semantic_id.startswith('orca.geometry.') for s in snap.metrics))
        self.assertTrue(any('unavailable/incomplete' in m for m in snap.messages))
        self.append('MAX step 0.0030000000 0.0040000000 YES\n----------------\n')
        snap = self.catchup()
        self.assertEqual(self.metric(snap, 'orca.geometry.max_step').value, .003)

    def test_arbitrary_byte_chunks_identical_history(self):
        self.write(output(cycles=4))
        snap = self.catchup()
        expected = [(e.entry_id, e.context, e.metrics, e.state) for e in sorted(self.entries.values(), key=lambda e: e.sequence)]
        self.reset(budget=7)
        other = self.catchup()
        actual = [(e.entry_id, e.context, e.metrics, e.state) for e in sorted(self.entries.values(), key=lambda e: e.sequence)]
        self.assertEqual(actual, expected)
        self.assertEqual(other.metrics, snap.metrics)

    def test_iteration_reset_and_algorithm_transition_not_compared(self):
        self.write(header() + '* GEOMETRY OPTIMIZATION CYCLE 1 *\n' + scf(converged=False))
        snap = self.catchup()
        self.assertFalse(any(s.latest.semantic_id == 'orca.scf.diis_error' for s in snap.metrics))
        self.assertIsNone(next(s for s in snap.metrics if s.latest.semantic_id == 'orca.scf.max_orbital_gradient').previous)
        self.append(' 1 -10.0110000000 -1.00e-03 1.00e-06 2.00e-06 2.00e-06 0.1\n')
        snap = self.catchup()
        self.assertIsNone(next(s for s in snap.metrics if s.latest.semantic_id == 'orca.scf.max_orbital_gradient').previous)
        self.assertTrue(any(e.kind == 'scf-reset' for e in snap.events))

    def test_summary_not_a_live_iteration_or_diis_replacement(self):
        self.write(header() + '* SCF CONVERGED AFTER 99 CYCLES *\nLast DIIS Error ... 4.0e-4 Tolerance : 1.0e-7\n')
        snap = self.catchup()
        self.assertFalse(any(e.kind == 'scf' for e in self.entries.values()))
        self.assertFalse(any(s.latest.semantic_id == 'orca.scf.diis_error' for s in snap.metrics))
        self.assertEqual(self.metric(snap, 'orca.scf_summary.diis_error').value, .0004)

    def test_current_cycle_without_gradient_preserves_old_label(self):
        self.write(header() + cycle() + '* GEOMETRY OPTIMIZATION CYCLE 2 *\n' + scf(converged=False))
        snap = self.catchup()
        gradient = self.metric(snap, 'orca.geometry.max_gradient_component')
        self.assertEqual(gradient.context[0].index, 1)
        self.assertEqual(snap.iteration[0].index, 2)
        self.assertTrue(any('carried values' in m for m in snap.messages))

    def test_stage_transition_clears_metrics_and_criteria_but_retains_history(self):
        self.write(output(footer=False) + 'THE OPTIMIZATION HAS CONVERGED\nVIBRATIONAL FREQUENCIES\n')
        snap = self.catchup()
        self.assertEqual(snap.status, 'UNSUPPORTED')
        self.assertEqual(snap.calculation, 'FREQ')
        self.assertEqual(snap.metrics, ())
        self.assertFalse(any(e.kind == 'optimization-converged' and e.segment == snap.segment for e in snap.events))
        self.assertTrue(any(e.stage == 'OPT' and e.metrics for e in self.entries.values()))

    def test_multi_file_stages_exact_sources_and_missing_future(self):
        future = self.root / 'future.out'
        self.context = replace(self.context, stage=None, sources=(
            SourceDescriptor('orca_output:OPT', str(self.path), stage='OPT'),
            SourceDescriptor('orca_output:SP', str(future), stage='SP')))
        self.reset(budget=99)
        self.write(output())
        first = self.catchup()
        self.assertEqual(first.calculation, 'OPT')
        future.write_text(header('SP') + scf(converged=False))
        second = self.catchup()
        self.assertEqual(second.calculation, 'SP')
        self.assertFalse(any(s.latest.semantic_id.startswith('orca.geometry.') for s in second.metrics))
        self.assertTrue(any(e.stage == 'OPT' for e in self.entries.values()))
        self.assertTrue(any(e.stage == 'SP' for e in self.entries.values()))

    def test_failed_scf_not_certified_by_normal_process_completion(self):
        self.write(header() + cycle(converged=False, table=False) + 'SCF NOT CONVERGED\n****ORCA TERMINATED NORMALLY****\n')
        snap = self.catchup()
        self.assertEqual(next(e for e in self.entries.values() if e.kind == 'geometry').state, 'NATIVE_FAILURE')
        self.assertFalse(any(e.kind == 'scf-converged' for e in snap.events))
        self.assertEqual(snap.scientific_validation, 'not-assessed')

    def test_unknown_dialect_specialized_and_trah_are_diagnostic(self):
        for text in (header(version='7.0.0') + cycle(), header('OptTS') + cycle(), header('FREQ') + scf()):
            self.reset()
            self.write(text)
            snap = self.catchup()
            self.assertIn(snap.status, {'UNSUPPORTED', 'SOURCE_UNRESOLVED'})
            self.assertEqual(snap.metrics, ())
        self.reset()
        self.write(header() + cycle(table=False, converged=False) + 'TRAH-SCF\n1 -1.0 2.0 3.0\n')
        snap = self.catchup()
        self.assertFalse(any(s.latest.semantic_id.startswith('orca.scf.') for s in snap.metrics))
        self.assertTrue(any('TRAH' in m for m in snap.limitations))

    def test_same_file_new_run_cannot_inherit_convergence(self):
        self.write(output() + header('SP') + scf(converged=False))
        snap = self.catchup()
        self.assertEqual(snap.calculation, 'SP')
        self.assertFalse(any(s.latest.semantic_id.startswith('orca.geometry.') for s in snap.metrics))
        self.assertFalse(any(e.kind == 'scf-converged' and e.segment == snap.segment for e in snap.events))
        self.assertFalse(any('Native optimization convergence marker observed' in m for m in snap.messages))

    def test_buffered_native_time_unknown_and_total_cumulative(self):
        self.write(output())
        snap = self.catchup()
        self.assertEqual(snap.completed_durations, ())
        evaluations = [e for e in self.entries.values() if e.kind in {'geometry', 'scf'}]
        self.assertTrue(all(t.value is None for e in evaluations for t in e.timings))
        self.assertTrue(all(t.source == 'unavailable' for e in evaluations for t in e.timings))

    def test_observer_interval_is_qualified_and_reopen_unavailable(self):
        self.write(header())
        self.poll(1)
        self.append('* GEOMETRY OPTIMIZATION CYCLE 1 *\n')
        self.poll(5, historical=False)
        self.append(scf() + geometry())
        snap = self.poll(9, historical=False)
        entry = next(e for e in self.entries.values() if e.kind == 'geometry')
        self.assertEqual(entry.timings[0].value, 4)
        self.assertEqual(entry.timings[0].quantity, 'observation-interval')
        self.assertIn('not execution duration', entry.timings[0].reason)
        self.reset()
        self.catchup()
        entry = next(e for e in self.entries.values() if e.kind == 'geometry')
        self.assertIsNone(entry.timings[0].value)

    def test_observer_clock_reset_does_not_zero_fill(self):
        self.write(header())
        self.poll()
        self.append('* GEOMETRY OPTIMIZATION CYCLE 1 *\n')
        self.poll(5, historical=False)
        self.append(scf() + geometry())
        self.poll(2, historical=False)
        entry = next(e for e in self.entries.values() if e.kind == 'geometry')
        self.assertIsNone(entry.timings[0].value)

    def test_replacement_clears_current_metrics(self):
        self.write(output())
        self.catchup()
        replacement = self.root / 'replacement.out'
        replacement.write_text(header('SP') + scf(converged=False))
        replacement.replace(self.path)
        snap = self.catchup()
        self.assertFalse(any(s.latest.semantic_id.startswith('orca.geometry.') for s in snap.metrics))
        self.assertTrue(any('generation changed' in m for m in snap.limitations))

    def test_history_memory_and_large_chunk_bounds(self):
        self.write(output(cycles=100))
        snap = self.catchup()
        self.assertEqual(len([e for e in self.entries.values() if e.kind == 'geometry']), 100)
        self.assertLessEqual(len(self.adapter.events), 64)
        self.assertLessEqual(len(self.adapter.issues), 32)
        self.assertLessEqual(len(self.adapter.series), 20)
        self.assertLessEqual(len(self.adapter.history), MAX_PENDING_HISTORY)
        self.assertEqual(self.adapter.current['children'], 3)
        self.assertFalse(self.adapter.has_pending_work)

    def test_split_partial_scf_row_waits_for_newline(self):
        text = header() + '* GEOMETRY OPTIMIZATION CYCLE 1 *\n' + scf(converged=False, soscf=False)
        self.write(text.rsplit('\n', 2)[0] + '\n 2 -10.01 -1.00e-2 1.0e-3')
        self.catchup()
        count = len([e for e in self.entries.values() if e.kind == 'scf'])
        self.append(' 2.0e-3 4.0e-3 0.0 0.2\n')
        self.catchup()
        self.assertEqual(len([e for e in self.entries.values() if e.kind == 'scf']), count + 1)

    def test_late_energy_and_table_cannot_erase_native_failure(self):
        self.write(header() + cycle(converged=False, table=False) +
                   'SCF NOT CONVERGED\nFINAL SINGLE POINT ENERGY -10.0\n' + geometry())
        self.catchup()
        self.assertEqual(next(e for e in self.entries.values() if e.kind == 'geometry').state, 'NATIVE_FAILURE')

    def test_malformed_numbers_and_rows_remain_diagnostic(self):
        self.write(header() + cycle(table=False) + geometry(max_gradient='1e999'))
        snap = self.catchup()
        self.assertFalse(any(s.latest.semantic_id == 'orca.geometry.max_gradient_component' for s in snap.metrics))
        self.assertTrue(any('nonfinite' in x for x in snap.limitations))

    def test_specialized_input_blocks_do_not_leak_ordinary_metrics(self):
        for block in ('$new_job', '%geom Scan', '%geom\n| 3> Scan', '%compound', '%tddft', '! HF NEB-CI'):
            self.reset()
            text = header().replace('| 2> ****END OF INPUT****', f'| 2> {block}\n| 4> ****END OF INPUT****')
            self.write(text + cycle())
            snap = self.catchup()
            self.assertEqual(snap.status, 'UNSUPPORTED', block)
            self.assertEqual(snap.metrics, (), block)

    def test_repeated_cycle_is_new_segment_without_old_convergence(self):
        self.write(header() + cycle() + 'THE OPTIMIZATION HAS CONVERGED\n' + cycle(1, converged=False, table=False))
        snap = self.catchup()
        self.assertFalse(any('Native optimization convergence marker observed' in m for m in snap.messages))
        self.assertFalse(any(s.latest.semantic_id.startswith('orca.geometry.') for s in snap.metrics))
        roots = [e for e in self.entries.values() if e.kind == 'geometry']
        self.assertEqual(len(roots), 2)
        self.assertNotEqual(roots[0].segment, roots[1].segment)

    def test_bounded_echo_and_annotations(self):
        self.write('Program Version 6.1.1\nINPUT FILE\n' + '| 1> %compound\n' * 400)
        self.catchup()
        self.assertLessEqual(len(self.adapter.echo_tokens), 128)
        self.reset()
        self.write(header() + cycle(table=False) + '* SCF CONVERGED AFTER 3 CYCLES *\n' * 400)
        self.catchup()
        self.assertLessEqual(len(self.adapter.current['annotations']), 32)

    def test_earlier_multifile_source_replacement_is_unresolved(self):
        future = self.root / 'future.out'
        self.context = replace(self.context, stage=None, sources=(
            SourceDescriptor('orca_output:OPT', str(self.path), stage='OPT'),
            SourceDescriptor('orca_output:SP', str(future), stage='SP')))
        self.reset()
        self.write(output())
        future.write_text(header('SP') + scf())
        self.catchup()
        replacement = self.root / 'replacement.out'
        replacement.write_text(header() + cycle())
        replacement.replace(self.path)
        snap = self.catchup()
        self.assertEqual(snap.status, 'SOURCE_UNRESOLVED')
        self.assertEqual(snap.metrics, ())

    def test_explicit_initial_stage_conflict_is_unresolved(self):
        self.context = replace(self.context, stage='FREQ', sources=(
            replace(self.context.sources[0], stage='FREQ'),))
        self.reset()
        self.write(header('OPT') + cycle())
        snap = self.catchup()
        self.assertEqual(snap.status, 'SOURCE_UNRESOLVED')
        self.assertEqual(snap.metrics, ())

    def test_total_runtime_covers_whole_run_across_native_stages(self):
        self.write(header() + cycle() + 'VIBRATIONAL FREQUENCIES\n'
                   'TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 125 msec\n')
        snap = self.catchup()
        total = next(e for e in self.entries.values() if e.kind == 'run-timing')
        self.assertIsNone(total.stage)
        self.assertIsNone(total.timings[0].stage)
        self.assertEqual(total.timings[0].value, 62.125)
        self.assertEqual(total.context[0].kind, 'run')
        self.assertLess(total.segment, snap.segment)

    def test_negative_total_runtime_components_not_cancelled(self):
        self.write(header() + scf() +
            'TOTAL RUN TIME: 0 days 0 hours 1 minutes -1 seconds\n'
            'TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds -5 msec\n')
        snap = self.catchup()
        self.assertFalse(any(e.kind == 'run-timing' for e in self.entries.values()))
        self.assertTrue(any('Negative native total runtime' in m for m in snap.limitations))

    def test_native_flag_disagrees_with_numeric_comparison(self):
        self.write(header() + cycle(table=False) + geometry(max_native='NO'))
        snap = self.catchup()
        metric = self.metric(snap, 'orca.geometry.max_gradient_component')
        self.assertEqual(metric.relation, 'BELOW')
        self.assertIn('native-no', metric.qualification)
        self.assertIn('numeric-below', metric.qualification)

    def test_unprinted_scf_units_are_not_inferred(self):
        self.write(header() + cycle(table=False))
        snap = self.catchup()
        self.assertEqual(self.metric(snap, 'orca.scf.max_orbital_gradient').unit, 'unspecified')
        self.assertEqual(self.metric(snap, 'orca.scf.max_density_change').unit, 'unspecified')
        self.assertEqual(self.metric(snap, 'orca.scf_summary.diis_error').unit, 'unspecified')
        self.assertEqual(self.metric(snap, 'orca.scf.energy').unit, 'Eh')

    def test_total_runtime_requires_established_native_run_boundary(self):
        for prefix in ('', 'Program Version 6.2.0\n'):
            self.reset()
            self.write(prefix + 'TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds\n')
            snap = self.catchup()
            self.assertFalse(any(e.kind == 'run-timing' for e in self.entries.values()))
            self.assertTrue(any('timing unavailable' in x for x in snap.limitations))
