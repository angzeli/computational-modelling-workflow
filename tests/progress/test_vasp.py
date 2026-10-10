"""Synthetic incremental VASP evidence; no engine or research fixtures."""
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from cmw.periodic.vasp.results import inspect_result
from cmw.progress.models import Criterion, DiagnosticTarget, RunContext, SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts, structure_text, write_run


class VaspProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-live-vasp-')
        # The strict batch reader rejects symlink ancestors, including macOS temp aliases.
        self.root = Path(self.temp.name).resolve()
        self.service = ObservationService()
        self.context = RunContext('synthetic-vasp', 'VASP', tuple(
            SourceDescriptor(role.lower(), str(self.root / role), optional=role == 'POSCAR')
            for role in ('OUTCAR', 'OSZICAR', 'POSCAR')), stage='synthetic-stage', attempt_id='synthetic-attempt')

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, **kwargs):
        write_run(self.root, **kwargs)
        return self.poll()

    def poll(self):
        result = self.service.poll(self.context)
        self.assertNotIn(result.status, {'READ_ERROR', 'ADAPTER_ERROR'}, result.messages)
        return result

    @staticmethod
    def metric(snapshot, semantic_id):
        return next(item for item in snapshot.metrics if item.latest.semantic_id == semantic_id)

    @staticmethod
    def force(snapshot, scope='free'):
        return VaspProgressTests.metric(snapshot, 'vasp.force.maximum_atomic_norm.' + scope)

    def append(self, name, value):
        with (self.root / name).open('a') as handle:
            handle.write(value)

    def test_completed_supported_values_match_batch_parser(self):
        for algorithm in ('DAV', 'RMM'):
            with self.subTest(algorithm=algorithm):
                self.service.close()
                self.service = ObservationService()
                snapshot = self.write(relaxation=True, evaluations=2, algorithm=algorithm,
                                      flags=((True,) * 3, (False,) * 3))
                batch = inspect_result(self.root)
                self.assertEqual(snapshot.status, 'OBSERVED')
                self.assertEqual(snapshot.completeness, 'COMPLETE_EVALUATION')
                self.assertEqual(snapshot.calculation, 'fixed-cell-relaxation')
                final = batch['selected_segment']['evaluations'][-1]['final_electronic_row']
                for key in ('dE', 'd_eps', 'rms'):
                    live = self.metric(snapshot, 'vasp.electronic.' + key).latest
                    self.assertEqual(live.value, abs(final[key]['value']))
                    self.assertEqual(live.precision, final[key]['half_last_place'])
                for scope in ('all', 'free', 'fixed'):
                    live = self.force(snapshot, scope)
                    self.assertEqual(live.latest.value, batch['endpoint']['force_summary'][scope]['maximum_norm'])
                    self.assertEqual(live.latest.context[0].index, 2)
                    self.assertEqual(live.previous.context[0].index, 1)
                self.assertEqual(self.force(snapshot).latest.qualification, 'provisional')
                self.assertEqual(snapshot.scientific_validation, 'not-assessed')
                self.assertFalse(snapshot.context.process_ownership)

    def test_signed_delta_precision_and_missing_rms_c_preserved(self):
        snapshot = self.write()
        metric = self.metric(snapshot, 'vasp.electronic.dE')
        self.assertEqual(metric.latest.value, 5e-8)
        self.assertEqual(metric.latest.signed_value, -5e-8)
        self.assertEqual(metric.latest.relation, 'BELOW')
        self.assertEqual(metric.previous.context[-1].index, 2)
        rms = self.metric(snapshot, 'vasp.electronic.rms_c')
        self.assertIsNone(rms.latest.value)
        self.assertIsNotNone(rms.previous.value)
        self.assertIsNone(rms.latest.criterion)
        self.assertEqual(snapshot.iteration[-1].budget, 3)
        self.assertIn('Native EDIFF reached', snapshot.messages)

    def test_printed_boundary_is_uncertain_for_both_energy_changes(self):
        snapshot = self.write(ediff='0.100E-05', terminal_de='-0.100E-05', terminal_deps='0.100E-05')
        for key in ('dE', 'd_eps'):
            self.assertEqual(self.metric(snapshot, 'vasp.electronic.' + key).latest.relation, 'UNCERTAIN')
        self.assertNotIn('PASS', str(snapshot.to_dict()))

    def test_zero_missing_and_positive_ediff_semantics(self):
        for ediff in ('0', 'omitted'):
            with self.subTest(ediff=ediff):
                self.service.close()
                self.service = ObservationService()
                texts = native_texts(ediff=ediff)
                for name in ('OUTCAR', 'OSZICAR'):
                    text = texts[name]
                    if ediff == 'omitted':
                        text = '\n'.join(line for line in text.splitlines() if not line.startswith('EDIFF =')) + '\n'
                    (self.root / name).write_text(text)
                snapshot = self.poll()
                self.assertIsNone(self.metric(snapshot, 'vasp.electronic.dE').latest.criterion)
                self.assertIsNone(self.metric(snapshot, 'vasp.electronic.d_eps').latest.criterion)

    def test_ediffg_signs_and_current_constraints_are_explicit(self):
        for ediffg in ('-0.02', '0', '0.02'):
            with self.subTest(ediffg=ediffg):
                self.service.close()
                self.service = ObservationService()
                snapshot = self.write(relaxation=True, ediffg=ediffg)
                metric = self.force(snapshot).latest
                self.assertEqual(metric.criterion is not None, ediffg.startswith('-'))
                if metric.criterion:
                    self.assertEqual(metric.criterion.signed_value, -0.02)
                    self.assertIn('historical binding unestablished', metric.criterion.definition)
                    self.assertTrue(metric.relation.startswith('PROVISIONAL_'))

    def test_partial_direction_constraints_are_never_cartesian_masks(self):
        snapshot = self.write(relaxation=True, flags=((True, False, True), (True,) * 3))
        self.assertEqual(self.force(snapshot).latest.value, 0.5)
        self.assertIsNone(self.force(snapshot).latest.criterion)
        self.assertTrue(any('not Cartesian masks' in item for item in snapshot.limitations))

    def test_missing_poscar_never_invents_free_atom_summary(self):
        texts = native_texts(relaxation=True)
        for role in ('OUTCAR', 'OSZICAR'):
            (self.root / role).write_text(texts[role])
        snapshot = self.poll()
        self.assertEqual(self.force(snapshot, 'all').latest.value, 0.5)
        self.assertFalse(any('maximum_atomic_norm.free' in item.latest.semantic_id for item in snapshot.metrics))
        self.assertIsNone(self.force(snapshot, 'all').latest.criterion)

    def test_force_is_pending_until_rows_and_closing_separator_complete(self):
        texts = native_texts()
        text = texts['OUTCAR']
        force_start = text.index(' POSITION')
        end = text.index(' total drift', force_start)
        separator = text.rfind('\n ', force_start, end - 1) + 1
        (self.root / 'OUTCAR').write_text(text[:separator])
        (self.root / 'OSZICAR').write_text(texts['OSZICAR'])
        snapshot = self.poll()
        self.assertFalse(any('force.' in item.latest.semantic_id for item in snapshot.metrics))
        self.assertTrue(any('Force table pending' in item for item in snapshot.messages))
        self.append('OUTCAR', text[separator:])
        snapshot = self.poll()
        self.assertEqual(self.force(snapshot, 'all').latest.value, 0.5)

    def test_delayed_outcar_counterpart_is_pending_then_matched(self):
        texts = native_texts()
        cut = texts['OUTCAR'].index(' free energy    TOTEN')
        (self.root / 'OUTCAR').write_text(texts['OUTCAR'][:cut])
        (self.root / 'OSZICAR').write_text(texts['OSZICAR'])
        pending = self.poll()
        self.assertNotEqual(pending.status, 'SOURCE_CONFLICT')
        self.assertFalse(pending.metrics)
        self.append('OUTCAR', texts['OUTCAR'][cut:])
        completed = self.poll()
        self.assertEqual(completed.completeness, 'COMPLETE_EVALUATION')
        self.assertEqual(self.metric(completed, 'vasp.electronic.dE').latest.value, 5e-8)

    def test_delayed_oszicar_summary_is_pending_not_conflict(self):
        texts = native_texts()
        (self.root / 'OUTCAR').write_text(texts['OUTCAR'])
        oszicar = texts['OSZICAR'].splitlines(keepends=True)
        (self.root / 'OSZICAR').write_text(''.join(oszicar[:-1]))
        pending = self.poll()
        self.assertNotEqual(pending.status, 'SOURCE_CONFLICT')
        self.assertEqual(pending.completeness, 'PARTIAL')
        self.append('OSZICAR', oszicar[-1])
        completed = self.poll()
        self.assertEqual(completed.completeness, 'COMPLETE_EVALUATION')

    def test_complete_cross_file_conflict_is_never_downgraded_to_pending(self):
        texts = native_texts()
        (self.root / 'OUTCAR').write_text(texts['OUTCAR'])
        (self.root / 'OSZICAR').write_text(texts['OSZICAR'].replace('-2.100000050000', '-9.100000050000'))
        snapshot = self.poll()
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertFalse(any('electronic.' in item.latest.semantic_id for item in snapshot.metrics))

    def test_new_evaluation_has_no_previous_residual_or_current_force(self):
        snapshot = self.write(relaxation=True, footer=False)
        native = (self.root / 'OUTCAR').read_text().replace(' reached required accuracy - stopping structural energy minimisation\n', '')
        # Start from a clean service to establish the edited synthetic initial source.
        self.service.close()
        self.service = ObservationService()
        (self.root / 'OUTCAR').write_text(native)
        self.poll()
        self.append('OUTCAR', ' Iteration 2( 1)\n EDDAV: synthetic\n free energy    TOTEN = -2.000000000000 eV\n')
        self.append('OSZICAR', 'DAV: 1 -2.000000000000 -2.00000000E+00 -0.02000000E+00 12 0.00100000\n')
        snapshot = self.poll()
        self.assertEqual(snapshot.iteration[0].index, 2)
        self.assertIsNone(self.metric(snapshot, 'vasp.electronic.dE').previous)
        self.assertEqual(self.force(snapshot).latest.context[0].index, 1)
        self.assertIn('Evaluation 2 force not yet available', snapshot.messages)

    def test_appended_banner_starts_new_segment_without_old_comparisons(self):
        self.write()
        texts = native_texts()
        self.append('OUTCAR', texts['OUTCAR'])
        pending = self.poll()
        self.assertEqual(pending.segment, 2)
        self.assertFalse(any('electronic.' in item.latest.semantic_id for item in pending.metrics))
        self.append('OSZICAR', texts['OSZICAR'])
        final = self.poll()
        self.assertEqual(final.segment, 2)
        self.assertEqual(final.completeness, 'COMPLETE_EVALUATION')
        self.assertIsNone(self.force(final).previous)

    def test_truncation_and_replacement_refuse_stale_sibling_correspondence(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                self.service.close()
                self.service = ObservationService()
                self.write()
                path = self.root / 'OUTCAR'
                text = native_texts(footer=False)['OUTCAR']
                if replacement:
                    other = self.root / 'replacement'
                    other.write_text(text)
                    other.replace(path)
                else:
                    path.write_text(text)
                snapshot = self.poll()
                self.assertEqual(snapshot.status, 'SOURCE_UNRESOLVED')
                self.assertFalse(any('electronic.' in item.latest.semantic_id for item in snapshot.metrics))

    def test_unsupported_modes_remain_diagnostic(self):
        for settings in ({'NSW': '20', 'IBRION': '0'}, {'ISIF': '3'}, {'LHFCALC': 'T'},
                         {'LNONCOLLINEAR': 'T'}, {'IMAGES': '2'}, {'IALGO': '90'}, {'METAGGA': 'R2SCAN'}):
            with self.subTest(settings=settings):
                self.service.close()
                self.service = ObservationService()
                snapshot = self.write(relaxation=True, settings=settings)
                self.assertEqual(snapshot.status, 'UNSUPPORTED')
                self.assertTrue(all(item.latest.criterion is None for item in snapshot.metrics))

    def test_explicit_stdout_route_and_duplicate_stream_suppression(self):
        write_run(self.root)
        (self.root / 'stdout').write_text((self.root / 'OSZICAR').read_text())
        self.context = replace(self.context, sources=(self.context.sources[0], SourceDescriptor('stdout', str(self.root / 'stdout')), self.context.sources[2]))
        snapshot = self.poll()
        self.assertEqual(self.metric(snapshot, 'vasp.electronic.dE').latest.evidence[0].source_role, 'stdout')
        self.service.close()
        self.service = ObservationService()
        self.context = replace(self.context, sources=(*self.context.sources, SourceDescriptor('oszicar', str(self.root / 'OSZICAR'))))
        snapshot = self.poll()
        self.assertEqual(self.metric(snapshot, 'vasp.electronic.dE').latest.evidence[0].source_role, 'oszicar')
        self.assertTrue(any('exclusively selected' in value for value in snapshot.limitations))

    def test_arbitrary_byte_chunking_matches_complete_snapshot(self):
        texts = native_texts()
        (self.root / 'POSCAR').write_text(structure_text())
        for name in ('OUTCAR', 'OSZICAR'):
            (self.root / name).write_bytes(b'')
        payloads = {name: text.encode() for name, text in texts.items() if name in {'OUTCAR', 'OSZICAR'}}
        for index in range(0, max(map(len, payloads.values())), 37):
            for name, payload in payloads.items():
                with (self.root / name).open('ab') as handle:
                    handle.write(payload[index:index + 37])
            snapshot = self.poll()
            self.assertNotEqual(snapshot.status, 'SOURCE_CONFLICT', snapshot.messages)
        self.assertEqual(snapshot.completeness, 'COMPLETE_EVALUATION')
        self.assertEqual(self.force(snapshot).latest.value, 0.5)
        self.assertEqual(self.metric(snapshot, 'vasp.electronic.dE').latest.value, 5e-8)

    def test_new_poll_does_not_refresh_metric_or_new_evidence_age(self):
        first = self.write()
        second = self.poll()
        self.assertEqual(first.last_new_evidence_at, second.last_new_evidence_at)
        self.assertEqual(self.force(first).latest.observed_at, self.force(second).latest.observed_at)
        self.assertEqual(sum(source.bytes_read for source in second.sources), 0)

    def test_caller_diagnostic_target_stays_separate(self):
        self.context = replace(self.context, diagnostic_targets=(DiagnosticTarget('vasp.electronic.dE',
            Criterion(1e-9, 'eV', '<', 'caller synthetic diagnostic', kind='diagnostic')),))
        snapshot = self.write()
        metric = self.metric(snapshot, 'vasp.electronic.dE').latest
        self.assertEqual(metric.criterion.value, 1e-6)
        self.assertEqual(metric.diagnostic_targets[0].value, 1e-9)
        self.assertEqual(metric.diagnostic_targets[0].kind, 'diagnostic')

    def test_late_source_creation_is_not_a_replacement(self):
        missing = self.poll()
        self.assertFalse(missing.metrics)
        snapshot = self.write()
        self.assertEqual(snapshot.status, 'OBSERVED')
        self.assertEqual(snapshot.completeness, 'COMPLETE_EVALUATION')

    def test_oszicar_ahead_does_not_present_old_force_as_current(self):
        self.write(relaxation=True, footer=False)
        self.append('OSZICAR', 'DAV: 1 -2.000000000000 -2.00000000E+00 -0.02000000E+00 12 0.00100000\n')
        snapshot = self.poll()
        self.assertEqual(snapshot.iteration[0].index, 2)
        self.assertEqual(snapshot.completeness, 'PARTIAL')
        self.assertEqual(self.force(snapshot).latest.context[0].index, 1)
        self.assertFalse(any('electronic.' in item.latest.semantic_id for item in snapshot.metrics))
        self.assertTrue(any('Evaluation 2 force not yet available' in message for message in snapshot.messages))

    def test_changed_constraints_remove_previous_force_comparison(self):
        texts = native_texts(relaxation=True, evaluations=2, footer=False)
        text = texts['OUTCAR'].replace(' reached required accuracy - stopping structural energy minimisation\n', '')
        cut = text.index('--------------------------------------- Iteration      2')
        (self.root / 'OUTCAR').write_text(text[:cut])
        (self.root / 'OSZICAR').write_text(texts['OSZICAR'])
        (self.root / 'POSCAR').write_text(structure_text())
        first = self.poll()
        self.assertIsNone(self.force(first).previous)
        (self.root / 'POSCAR').write_text(structure_text(flags=((True,) * 3, (False,) * 3)))
        self.append('OUTCAR', text[cut:])
        second = self.poll()
        self.assertEqual(self.force(second).latest.context[0].index, 2)
        self.assertIsNone(self.force(second).previous)
        self.assertIsNone(self.force(second, 'all').previous)

    def test_force_printed_uncertainty_uses_atomic_norm_bound(self):
        snapshot = self.write(relaxation=True, ediffg='-0.02',
                              forces=((0.020000, 0, 0), (0, 0, 0)))
        metric = self.force(snapshot).latest
        self.assertEqual(metric.relation, 'PROVISIONAL_UNCERTAIN')
        self.assertGreater(metric.precision, 0.5e-6)

    def test_setting_change_is_a_conflict_without_retroactive_target(self):
        self.write()
        self.append('OUTCAR', ' EDIFF = 0.01\n')
        snapshot = self.poll()
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertTrue(all(item.latest.criterion is None for item in snapshot.metrics))

    def test_large_unmatched_history_is_bounded_and_explicit(self):
        texts = native_texts(relaxation=True, evaluations=20, footer=False)
        (self.root / 'OUTCAR').write_text(texts['OUTCAR'])
        (self.root / 'OSZICAR').write_text('')
        snapshot = self.poll()
        while any(source.has_more for source in snapshot.sources):
            snapshot = self.poll()
        self.assertTrue(any('evicted unmatched evidence' in value for value in snapshot.limitations))
        self.assertFalse(any('electronic.' in item.latest.semantic_id for item in snapshot.metrics))
        self.append('OSZICAR', texts['OSZICAR'])
        snapshot = self.poll()
        self.assertEqual(self.metric(snapshot, 'vasp.electronic.dE').latest.context[0].index, 20)
        self.assertEqual(snapshot.completeness, 'COMPLETE_EVALUATION')

    def test_unavailable_current_constraints_do_not_bind_new_force(self):
        texts = native_texts(relaxation=True, evaluations=2, footer=False)
        text = texts['OUTCAR'].replace(' reached required accuracy - stopping structural energy minimisation\n', '')
        cut = text.index('--------------------------------------- Iteration      2')
        (self.root / 'OUTCAR').write_text(text[:cut])
        (self.root / 'OSZICAR').write_text(texts['OSZICAR'])
        (self.root / 'POSCAR').write_text(structure_text())
        self.assertIsNotNone(self.force(self.poll()).latest.criterion)
        (self.root / 'POSCAR').unlink()
        self.append('OUTCAR', text[cut:])
        snapshot = self.poll()
        self.assertFalse(any(item.latest.semantic_id.endswith('.free') for item in snapshot.metrics))
        force = self.force(snapshot, 'all')
        self.assertEqual(force.latest.context[0].index, 2)
        self.assertIsNone(force.latest.criterion)
        self.assertIsNone(force.previous)

    def test_unchanged_poscar_is_not_reparsed(self):
        self.write()
        with patch('cmw.progress.vasp.parse_poscar', side_effect=AssertionError('Unchanged source was reparsed')):
            snapshot = self.poll()
        self.assertEqual(snapshot.status, 'OBSERVED')

    def test_nonfinite_derived_force_never_enters_json_metrics(self):
        texts = native_texts(forces=((1.7e308, 1.7e308, 1.7e308), (0, 0, 0)))
        for name in ('OUTCAR', 'OSZICAR'):
            (self.root / name).write_text(texts[name])
        snapshot = self.poll()
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertFalse(any('force.' in item.latest.semantic_id for item in snapshot.metrics))
        json.dumps(snapshot.to_dict(), allow_nan=False)

    def test_partial_evaluated_energy_does_not_reuse_scf_energy(self):
        texts = native_texts()
        (self.root / 'OUTCAR').write_text(texts['OUTCAR'].replace(' free  energy   TOTEN = -3.00000000 eV\n', ''))
        (self.root / 'OSZICAR').write_text(texts['OSZICAR'])
        snapshot = self.poll()
        self.assertEqual(snapshot.completeness, 'PARTIAL')
        self.assertNotEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertEqual(self.force(snapshot, 'all').latest.value, 0.5)

    def test_static_ediffg_is_not_an_engine_force_stopping_criterion(self):
        snapshot = self.write(ediffg='-0.02')
        self.assertEqual(snapshot.calculation, 'static')
        self.assertIsNone(self.force(snapshot).latest.criterion)
        self.assertIsNone(self.force(snapshot).latest.relation)

    def test_earlier_complete_conflict_persists_when_later_evaluation_matches(self):
        texts = native_texts(relaxation=True, evaluations=2, footer=False)
        (self.root / 'OUTCAR').write_text(texts['OUTCAR'])
        (self.root / 'OSZICAR').write_text(texts['OSZICAR'].replace('-2.100000050000', '-9.100000050000', 1))
        snapshot = self.poll()
        self.assertEqual(snapshot.iteration[0].index, 2)
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertTrue(any('Evaluation 1: Complete electronic energies conflict' in value for value in snapshot.limitations))
        self.assertEqual(self.poll().status, 'SOURCE_CONFLICT')

    def test_delayed_counterpart_can_prove_an_earlier_complete_conflict(self):
        texts = native_texts(relaxation=True, evaluations=2, footer=False)
        (self.root / 'OUTCAR').write_text(texts['OUTCAR'])
        (self.root / 'OSZICAR').write_text('')
        self.assertNotEqual(self.poll().status, 'SOURCE_CONFLICT')
        self.append('OSZICAR', texts['OSZICAR'].replace('E0= -.29950000E+01', 'E0= -.89950000E+01', 1))
        snapshot = self.poll()
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertTrue(any('Evaluation 1: Complete evaluated ionic energies conflict' in value for value in snapshot.limitations))

    def test_native_fatal_and_stop_are_only_observation_events(self):
        snapshot = self.write()
        self.append('OUTCAR', ' VERY BAD NEWS synthetic\n STOPCAR found synthetic stop\n')
        snapshot = self.poll()
        kinds = {event.kind for event in snapshot.events}
        self.assertIn('native-fatal', kinds)
        self.assertIn('native-stop-request', kinds)
        self.assertEqual(snapshot.phase, 'Native interruption evidence')
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')


if __name__ == '__main__':
    unittest.main()
