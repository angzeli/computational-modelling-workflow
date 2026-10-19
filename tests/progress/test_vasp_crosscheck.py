"""Synthetic independently declared electronic streams; no scientific execution."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts, write_run


ENERGIES = ('-601.000000000', '-610.000000000', '-620.000000000', '-623.000000000',
            '-626.000000000', '-627.000000000', '-627.287613665', '-624.178913665')


def native_row(index):
    return (f'--------------------------------------- Iteration      1({index:4d})  ---------------------------------------\n'
            ' EDDAV: cpu time 0.0010: real time 0.0020\n'
            f' free energy    TOTEN = {ENERGIES[index-1]} eV\n')


def electronic_row(index):
    # Last row uses the request's uploaded-snapshot numbers in an invented run.
    return (f'DAV: {index} {ENERGIES[index-1]} +3.1087 -2.9332 12 1.89 1.41\n')


class VaspCrosscheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-crosscheck-synthetic-')
        self.root = Path(self.temp.name).resolve()
        self.service = ObservationService()
        self.context = RunContext('invented-bound-attempt', 'VASP', tuple(
            SourceDescriptor(role, str(self.root / name), optional=role == 'incar')
            for role, name in (('outcar', 'OUTCAR'), ('oszicar', 'OSZICAR'), ('stdout', 'stdout.log'), ('incar', 'INCAR'))),
            stage='synthetic-stage', attempt_id='synthetic-attempt')

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def append(self, name, text):
        with (self.root / name).open('a') as handle:
            handle.write(text)

    def initial(self, count=6):
        texts = native_texts(nelm=300)
        prefix = texts['OUTCAR'].split('--------------------------------------- Iteration')[0]
        (self.root / 'OUTCAR').write_text(prefix + ''.join(native_row(i) for i in range(1, count+1)))
        (self.root / 'OSZICAR').write_text('')
        (self.root / 'stdout.log').write_text(''.join(electronic_row(i) for i in range(1, 9)))
        (self.root / 'INCAR').write_text(texts['INCAR'])
        return self.poll()

    def poll(self):
        snapshot = self.service.poll(self.context)
        self.assertNotIn(snapshot.status, ('ADAPTER_ERROR', 'READ_ERROR'), snapshot.messages)
        return snapshot

    @staticmethod
    def metric(snapshot, suffix):
        return next(series.latest for series in snapshot.metrics if series.latest.semantic_id == 'vasp.electronic.' + suffix)

    def history(self):
        return self.service.history(self.context, limit=100, parent_id="*")

    def test_empty_oszicar_selects_stdout_and_checks_available_outcar_prefix(self):
        snapshot = self.initial()
        evidence = snapshot.verification[0]
        self.assertEqual((evidence.source_role, evidence.status, evidence.observed_frontier, evidence.checked_frontier),
                         ('stdout', 'PARTIAL', 8, 6))
        self.assertEqual(set(evidence.fields), {'label', 'algorithm', 'energy'})
        self.assertNotIn('rms', evidence.fields)
        self.assertEqual(self.metric(snapshot, 'dE').signed_value, 3.1087)
        self.assertEqual(self.metric(snapshot, 'd_eps').signed_value, -2.9332)
        self.assertEqual(self.metric(snapshot, 'rms').value, 1.89)
        self.assertEqual(self.metric(snapshot, 'rms_c').value, 1.41)
        self.assertEqual(self.metric(snapshot, 'dE').context[-1].index, 8)
        self.assertEqual(self.metric(snapshot, 'dE').qualification, 'source-local')
        self.assertIsNone(self.metric(snapshot, 'rms').criterion)
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')

    def test_late_outcar_and_duplicate_promote_existing_history_without_reset(self):
        first = self.initial()
        history = self.history()
        rows = {entry.native_label: entry for entry in history.entries if entry.kind == 'electronic'}
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows['8'].coverage, 'PARTIAL')
        self.append('OUTCAR', native_row(7) + native_row(8) +
                    '--------------------------------------- Iteration      1(   9)  ---------------------------------------\n')
        checked = self.poll()
        self.assertEqual(checked.verification[0].checked_frontier, 8)
        self.assertEqual(checked.iteration[-1].index, 9)
        self.assertEqual(self.metric(checked, 'dE').context[-1].index, 8)
        self.assertEqual(self.metric(checked, 'dE').qualification, 'row-correspondence')
        checked_rows = {entry.native_label: entry for entry in self.history().entries if entry.kind == 'electronic'}
        self.assertEqual(checked_rows['8'].entry_id, rows['8'].entry_id)
        self.assertEqual(checked_rows['8'].sequence, rows['8'].sequence)
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)))
        duplicated = self.poll()
        self.assertEqual(duplicated.verification[0].source_role, 'stdout')
        self.assertEqual(duplicated.verification[1].status, 'MATCHED')
        self.assertEqual(duplicated.verification[1].checked_frontier, 8)
        self.assertIn('dE', duplicated.verification[1].fields)
        final_rows = [entry for entry in self.history().entries if entry.kind == 'electronic']
        self.assertEqual(len(final_rows), 9)
        self.assertEqual(len({entry.entry_id for entry in final_rows}), 9)
        self.assertEqual(first.last_new_evidence_at, self.metric(duplicated, 'dE').observed_at)

    def test_usable_oszicar_preserved_and_newer_stdout_selected_stably(self):
        self.initial(8)
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)))
        self.service.close()
        self.service = ObservationService()
        first = self.poll()
        self.assertEqual(first.verification[0].source_role, 'oszicar')
        self.append('stdout.log', 'DAV: 9 -624.000000000 +0.1789 -0.1 12 1.2\n')
        newer = self.poll()
        self.assertEqual(newer.verification[0].source_role, 'stdout')
        self.assertEqual(newer.verification[1].checked_frontier, 8)
        self.assertEqual(newer.verification[1].status, 'PARTIAL')
        self.assertIsNone(self.metric(newer, 'rms_c').value)
        self.assertEqual(newer.metrics[0].previous.context[-1].index, 8)
        self.assertEqual(self.poll().verification[0].source_role, 'stdout')

    def test_duplicate_signed_difference_is_conflict_and_later_agreement_cannot_erase_it(self):
        self.initial()
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)).replace('+3.1087', '-3.1087', 1))
        conflict = self.poll()
        self.assertEqual(conflict.status, 'SOURCE_CONFLICT')
        self.assertEqual(conflict.verification[0].status, 'PARTIAL')
        self.assertEqual(conflict.verification[1].status, 'CONFLICT')
        self.assertTrue(any('dE' in message for message in conflict.limitations))
        self.assertIsNone(self.metric(conflict, 'dE').criterion)
        for name in ('OSZICAR', 'stdout.log'):
            self.append(name, 'DAV: 9 -624.0 +0.1 -0.1 12 1.2\n')
        self.assertEqual(self.poll().status, 'SOURCE_CONFLICT')

    def test_matching_large_energy_does_not_use_loose_relative_tolerance(self):
        self.initial()
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)).replace('-601.000000000', '-601.000000100'))
        self.assertEqual(self.poll().status, 'SOURCE_CONFLICT')

    def test_independent_generation_change_does_not_inherit_matches(self):
        self.initial(8)
        replacement = self.root / 'new-stdout'
        replacement.write_text(''.join(electronic_row(i) for i in range(1, 9)))
        replacement.replace(self.root / 'stdout.log')
        snapshot = self.poll()
        self.assertEqual(snapshot.status, 'SOURCE_UNRESOLVED')
        self.assertEqual(snapshot.verification[0].status, 'UNRESOLVED')
        self.assertIsNone(snapshot.verification[0].checked_frontier)
        self.assertEqual(len(self.metric(snapshot, 'dE').context), 1)
        self.assertIsNone(self.metric(snapshot, 'dE').criterion)

    def test_absent_outcar_still_shows_complete_source_local_values_without_outer_mapping(self):
        (self.root / 'stdout.log').write_text(''.join(electronic_row(i) for i in range(1, 9)))
        snapshot = self.poll()
        self.assertEqual(snapshot.verification[0].status, 'UNAVAILABLE')
        self.assertEqual(self.metric(snapshot, 'dE').value, 3.1087)
        self.assertEqual(len(self.metric(snapshot, 'dE').context), 1)
        self.assertIsNone(self.metric(snapshot, 'dE').criterion)
        self.assertIsNone(snapshot.calculation)

    def test_effective_settings_remain_authoritative_over_current_input(self):
        self.initial()
        (self.root / 'INCAR').write_text('EDIFF = 0.5\nEDIFFG = 0.3\nNSW = 10\n')
        snapshot = self.poll()
        setting = next(item for item in snapshot.settings if item.name == 'EDIFF')
        self.assertTrue(setting.discrepancy)
        self.assertEqual(setting.value, 1e-6)
        self.assertEqual(self.metric(snapshot, 'dE').criterion.value, 1e-6)
        self.assertEqual(snapshot.calculation, 'static')
        self.assertIn('OUTCAR effective', setting.origin)

    def test_current_incar_target_is_explicitly_provisional_when_output_target_missing(self):
        self.initial()
        text = (self.root / 'OUTCAR').read_text()
        (self.root / 'OUTCAR').write_text('\n'.join(line for line in text.splitlines() if not line.startswith('EDIFF ='))+'\n')
        self.service.close()
        self.service = ObservationService()
        snapshot = self.poll()
        setting = next(item for item in snapshot.settings if item.name == 'EDIFF')
        self.assertEqual(setting.qualification, 'provisional')
        self.assertIn('historical linkage unestablished', self.metric(snapshot, 'dE').criterion.origin)
        self.assertEqual(self.metric(snapshot, 'dE').criterion.kind, 'provisional-engine-stopping')
        self.assertTrue(self.metric(snapshot, 'dE').relation.startswith('PROVISIONAL_'))
        self.assertNotIn('effective EDIFF', self.metric(snapshot, 'dE').criterion.definition)

    def test_electronic_conflict_preserves_independent_force(self):
        write_run(self.root, relaxation=True)
        (self.root / 'stdout.log').write_text((self.root / 'OSZICAR').read_text().replace('-.10000000E+00', '+.10000000E+00'))
        self.context = replace(self.context, sources=(*self.context.sources, SourceDescriptor('poscar', str(self.root / 'POSCAR'))))
        snapshot = self.poll()
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        force = next(series.latest for series in snapshot.metrics if series.latest.semantic_id.endswith('maximum_atomic_norm.free'))
        self.assertEqual(force.value, 0.5)
        self.assertIsNotNone(force.criterion)

    def test_partial_line_not_a_complete_row_and_malformed_is_explicit(self):
        self.initial()
        self.append('stdout.log', 'DAV: 9 -624.0 +0.1')
        self.assertEqual(self.poll().verification[0].observed_frontier, 8)
        self.append('stdout.log', '\n')
        snapshot = self.poll()
        self.assertTrue(any('malformed' in message for message in snapshot.limitations))
        self.assertEqual(snapshot.verification[0].observed_frontier, 8)

    def test_no_native_summary_needed_for_prefix_match(self):
        snapshot = self.initial(8)
        self.assertEqual(snapshot.verification[0].status, 'MATCHED')
        self.assertEqual(snapshot.verification[0].checked_frontier, 8)
        self.assertEqual(snapshot.completeness, 'PARTIAL')
        self.assertFalse(any(event.kind == 'native-footer' for event in snapshot.events))

    def test_unchanged_poll_does_not_repeat_printed_comparisons(self):
        self.initial(8)
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)))
        self.poll()
        with patch('cmw.progress.vasp._same_printed', side_effect=AssertionError('unchanged prefix compared again')):
            snapshot = self.poll()
        self.assertEqual(snapshot.verification[0].checked_frontier, 8)
        self.assertEqual(sum(source.bytes_read for source in snapshot.sources), 0)

    def test_source_only_history_promotes_in_place_when_native_prefix_arrives(self):
        (self.root / 'stdout.log').write_text(''.join(electronic_row(i) for i in range(1, 9)))
        (self.root / 'OSZICAR').write_text('')
        self.poll()
        before = self.history()
        rows = {entry.native_label: entry for entry in before.entries if entry.kind == 'electronic'}
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(len(entry.context) == 1 for entry in rows.values()))
        texts = native_texts(nelm=300)
        prefix = texts['OUTCAR'].split('--------------------------------------- Iteration')[0]
        (self.root / 'OUTCAR').write_text(prefix + ''.join(native_row(i) for i in range(1, 9)))
        self.poll()
        after = self.history()
        updated = {entry.native_label: entry for entry in after.entries if entry.kind == 'electronic'}
        self.assertEqual(len(updated), 8)
        for label, row in rows.items():
            self.assertEqual(updated[label].entry_id, row.entry_id)
            self.assertEqual(len(updated[label].context), 2)

    def test_future_stream_segment_stays_source_local_instead_of_blanking(self):
        self.initial(8)
        self.append('stdout.log', ' vasp.6.6.1 new synthetic segment\n' + electronic_row(1))
        snapshot = self.poll()
        self.assertEqual(self.metric(snapshot, 'dE').context[-1].index, 1)
        self.assertEqual(self.metric(snapshot, 'dE').segment, 2)
        self.assertEqual(snapshot.segment, 2)
        self.assertIsNone(snapshot.calculation)
        self.assertEqual(self.metric(snapshot, 'dE').qualification, 'source-local')
        self.assertEqual(len(self.metric(snapshot, 'dE').context), 1)
        self.assertIsNone(self.metric(snapshot, 'dE').criterion)
        self.assertIsNone(snapshot.verification[0].checked_frontier)

    def test_malformed_peer_is_unsupported_not_complete_row_conflict(self):
        self.initial(8)
        self.append('OSZICAR', 'DAV: 1 broken complete row\n')
        snapshot = self.poll()
        self.assertNotEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertEqual(snapshot.verification[1].status, 'UNSUPPORTED')
        self.assertEqual(snapshot.verification[0].source_role, 'stdout')

    def test_attempt_binding_is_part_of_history_identity(self):
        self.initial(8)
        before = {entry.entry_id for entry in self.history().entries}
        context = replace(self.context, binding_id='other-bound-attempt', attempt_id='other-attempt')
        self.service.poll(context)
        after = self.service.history(context, parent_id='*')
        self.assertTrue(before.isdisjoint(entry.entry_id for entry in after.entries))

    def test_algorithm_and_ordered_label_conflicts_are_not_fallback(self):
        for bad in ('RMM: 1', 'DAV: 2'):
            with self.subTest(bad=bad):
                self.service.close()
                self.service = ObservationService()
                self.initial(8)
                self.append('OSZICAR', electronic_row(1).replace('DAV: 1', bad))
                snapshot = self.poll()
                self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
                self.assertEqual(snapshot.verification[0].source_role, 'stdout')


    def test_unrelated_source_reset_preserves_existing_pair_conflict(self):
        self.initial(8)
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)).replace('-601.000000000', '-601.100000000'))
        self.assertEqual(self.poll().status, 'SOURCE_CONFLICT')
        replacement = self.root / 'new-stdout'
        replacement.write_text(''.join(electronic_row(i) for i in range(1, 9)))
        replacement.replace(self.root / 'stdout.log')
        snapshot = self.poll()
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertTrue(any('oszicar/OUTCAR' in message for message in snapshot.limitations))

    def test_duplicate_conflict_qualifies_history_and_preserves_force_domain(self):
        self.initial(8)
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)).replace('+3.1087', '-3.1087', 1))
        self.poll()
        rows = [entry for entry in self.history().entries if entry.kind == 'electronic']
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(entry.coverage != 'COMPLETE' for entry in rows))
        self.assertTrue(all(entry.annotations for entry in rows))
        self.assertTrue(all(metric.criterion is None for entry in rows for metric in entry.metrics))
        self.assertTrue(all(metric.qualification == 'conflict' for entry in rows for metric in entry.metrics))

    def test_duplicate_fields_do_not_claim_missing_current_residual(self):
        self.initial(8)
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)).rstrip().removesuffix(' 1.41') + '\n')
        snapshot = self.poll()
        duplicate = snapshot.verification[1]
        self.assertEqual(duplicate.status, 'MATCHED')
        self.assertEqual(duplicate.checked_frontier, 8)
        self.assertNotIn('rms_c', duplicate.fields)
        self.assertIn('rms(c) absent', duplicate.reason)


    def test_future_segment_duplicate_conflict_qualifies_active_snapshot(self):
        self.initial(8)
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)))
        self.poll()
        banner = ' vasp.6.6.1 synthetic next segment\n'
        self.append('stdout.log', banner + electronic_row(1))
        self.append('OSZICAR', banner + electronic_row(1).replace('+3.1087', '-3.1087'))
        snapshot = self.poll()
        self.assertEqual(snapshot.segment, 2)
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertTrue(any('dE' in message for message in snapshot.limitations))
        self.assertEqual(self.metric(snapshot, 'dE').qualification, 'conflict')


    def test_duplicate_conflict_also_qualifies_source_ahead_history_rows(self):
        self.initial(6)
        self.append('OSZICAR', ''.join(electronic_row(i) for i in range(1, 9)).replace('+3.1087', '-3.1087', 1))
        self.poll()
        rows = [entry for entry in self.history().entries if entry.kind == 'electronic']
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(metric.criterion is None for entry in rows for metric in entry.metrics))
        self.assertTrue(all(metric.qualification == 'conflict' for entry in rows for metric in entry.metrics))



if __name__ == '__main__':
    unittest.main()
