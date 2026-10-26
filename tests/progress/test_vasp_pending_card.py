"""Real bounded readers retain the scientific frame across incomplete evidence."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.snapshot import capture_snapshot, render_snapshot
from cmw.progress.formatting import cell_width, progress_text
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts, structure_text
from tests.progress.test_vasp_crosscheck import electronic_row, native_row


class VaspPendingCardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-pending-card-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 100.
        self.service = ObservationService(clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.context = RunContext('invented-card-binding', 'VASP', tuple(
            SourceDescriptor(role, str(self.root / name)) for role, name in
            (('outcar', 'OUTCAR'), ('stdout', 'stdout.log'))),
            attempt_id='invented-attempt', job_id=25)
        self.native = native_texts(nelm=300, ediff='1e-6', ediffg='.02')
        self.prefix = self.native['OUTCAR'].split('--------------------------------------- Iteration')[0]
        self.write('OUTCAR', self.prefix)
        self.write('stdout.log', '')

    def write(self, name, text):
        (self.root / name).write_text(text)

    def append(self, name, text):
        with (self.root / name).open('a') as handle:
            handle.write(text)

    def poll(self):
        snapshot = self.service.poll(self.context)
        self.assertNotEqual(snapshot.status, 'ADAPTER_ERROR', snapshot.messages)
        return snapshot

    def frame(self, snapshot, *, width=100):
        text = progress_text(snapshot, width=width, max_lines=8, framed=True, now=self.now)
        self.assertTrue(text.startswith('┌'), text)
        self.assertIn('└', text)
        for label in ('SCF', 'FORCE', 'ELECTRONIC'):
            self.assertIn(label, text)
        self.assertLessEqual(len(text.splitlines()), 8)
        self.assertTrue(all(cell_width(line) <= width for line in text.splitlines()))
        self.assertNotIn('Scientific: BOUND', text)
        return text

    def test_bound_static_waiting_for_first_complete_row_keeps_known_targets(self):
        self.append('OUTCAR', native_row(1))
        self.append('stdout.log', 'DAV: 1 -601.000000000 +3.1087')
        snapshot = self.poll()
        self.assertEqual(snapshot.context.binding_status, 'BOUND')
        self.assertFalse(snapshot.metrics)
        self.assertEqual(snapshot.calculation, 'static')
        self.assertTrue(snapshot.card_groups)
        for width in (77, 80, 100, 140):
            with self.subTest(width=width):
                text = self.frame(snapshot, width=width)
                self.assertIn('EDIFF 1E-6 eV', text)
                self.assertIn('Target N/A · static', text)
                self.assertIn('|dE| —', text)
                self.assertIn('rms —', text)
                self.assertNotIn('Configure Sources', text)
                self.assertTrue(any(word in text.lower() for word in ('waiting', 'catching up', 'pending')))

    def test_effective_ediff_before_native_evaluation_does_not_invent_counters(self):
        snapshot = self.poll()
        self.assertFalse(snapshot.iteration)
        self.assertFalse(snapshot.metrics)
        text = self.frame(snapshot)
        self.assertIn('EDIFF 1E-6 eV', text)
        self.assertIn('Eval unassigned', text)
        self.assertIn('OUTCAR SCF unavailable', text)
        self.assertNotIn('Eval 1', text)

    def test_stdout_ahead_retains_native_and_checked_frontiers(self):
        self.append('OUTCAR', native_row(1))
        self.append('stdout.log', electronic_row(1) + electronic_row(2))
        snapshot = self.poll()
        evidence = snapshot.verification[0]
        self.assertEqual((evidence.observed_frontier, evidence.checked_frontier, evidence.native_frontier), (2, 1, 1))
        self.assertEqual(snapshot.metrics[0].latest.qualification, 'source-local')
        self.assertEqual(snapshot.iteration[-1].index, 1)
        text = self.frame(snapshot, width=140)
        self.assertIn('OUTCAR SCF 1/300', text)
        self.assertIn('@SCF2', text)
        self.assertIn('outcar checked 1', text.lower())
        self.assertIn('source-local', text)
        self.assertNotIn('Configure Sources', text)

    def test_true_missing_declaration_uses_read_free_skeleton(self):
        self.context = replace(self.context, sources=(), binding_status='SOURCE_UNRESOLVED',
                               limitations=('No supported source declaration',))
        with patch.object(self.service, 'reader_factory', side_effect=AssertionError('must not read')) as readers:
            snapshot = self.poll()
        readers.assert_not_called()
        self.assertFalse(snapshot.sources)
        self.assertFalse(snapshot.settings)
        self.assertIsNone(snapshot.calculation)
        text = self.frame(snapshot)
        self.assertIn('Configure Sources', text)
        self.assertNotIn('Target N/A · static', text)
        self.assertIn('Target unavailable', text)

    def test_required_declaration_conflict_keeps_frame_and_review_action(self):
        self.context = replace(self.context, sources=(), binding_status='SOURCE_CONFLICT',
            source_conflicts=('outcar differs between explicit and runner declarations',),
            limitations=('Required outcar declarations disagree',))
        snapshot = self.poll()
        self.assertFalse(snapshot.sources)
        self.assertFalse(snapshot.metrics)
        text = self.frame(snapshot)
        self.assertIn('conflict', text.lower())
        self.assertIn('Review Sources', text)
        self.assertNotIn('Configure Sources', text)

    def test_unsupported_native_dialect_keeps_frame_without_source_configuration(self):
        self.append('OUTCAR', native_row(1))
        self.append('stdout.log', 'vasp.5.4.4\n' + electronic_row(1))
        snapshot = self.poll()
        self.assertFalse(snapshot.metrics)
        self.assertTrue(any(item.status == 'UNSUPPORTED' for item in snapshot.source_diagnostics))
        text = self.frame(snapshot)
        self.assertIn('unsupported', text.lower())
        self.assertNotIn('Configure Sources', text)
        self.assertFalse(any(check.checked_frontier for check in snapshot.verification))

    def test_declared_missing_files_wait_without_replacement_guidance(self):
        (self.root / 'OUTCAR').unlink()
        (self.root / 'stdout.log').unlink()
        snapshot = self.poll()
        self.assertEqual(snapshot.context.binding_status, 'BOUND')
        self.assertTrue(all(item.health == 'MISSING' for item in snapshot.sources))
        text = self.frame(snapshot)
        self.assertTrue(any(word in text.lower() for word in ('waiting', 'unavailable', 'missing')))
        self.assertNotIn('Configure Sources', text)
        self.assertNotIn('Target N/A · static', text)

    def test_permission_failure_preserves_declared_identity_without_inventing_metrics(self):
        with patch('cmw.progress.reader.os.stat', side_effect=PermissionError('synthetic denied')):
            snapshot = self.poll()
        self.assertTrue(all(item.health == 'PERMISSION_DENIED' for item in snapshot.sources))
        self.assertFalse(snapshot.metrics)
        text = self.frame(snapshot)
        self.assertIn('unavailable', text.lower())
        self.assertNotIn('Configure Sources', text)
        self.assertIn('PERMISSION_DENIED', progress_text(snapshot, detailed=True))

    def test_cached_read_error_and_recovery_keep_same_generation_and_data_age(self):
        self.append('OUTCAR', native_row(1))
        self.append('stdout.log', electronic_row(1))
        initial = self.poll()
        self.now = 110.
        with patch('cmw.progress.reader.os.stat', side_effect=PermissionError('synthetic denied')):
            failed = self.poll()
        self.assertEqual(failed.context, initial.context)
        self.assertEqual([source.generation for source in failed.sources], [source.generation for source in initial.sources])
        self.assertEqual(failed.metrics[0].latest.observed_at, 100.)
        text = self.frame(failed, width=140)
        self.assertIn('cached', text.lower())
        self.assertIn('unavailable', text.lower())
        self.assertNotIn('Configure Sources', text)
        self.now = 120.
        recovered = self.poll()
        self.assertEqual(recovered.read_health, 'OK')
        self.assertEqual(recovered.metrics[0].latest.observed_at, 100.)
        self.assertNotIn('cached', self.frame(recovered, width=140).lower())

    def test_service_read_error_labels_retained_observations_cached(self):
        self.append('OUTCAR', native_row(1))
        self.append('stdout.log', electronic_row(1))
        initial = self.poll()
        self.append('stdout.log', 'synthetic later line\n')
        self.now = 110.
        with patch('cmw.progress.reader.os.pread', side_effect=RuntimeError('synthetic read failure')):
            failed = self.poll()
        self.assertEqual(failed.read_health, 'READ_ERROR')
        self.assertEqual(failed.metrics, initial.metrics)
        self.assertEqual(failed.context.attempt_id, 'invented-attempt')
        text = self.frame(failed, width=140)
        self.assertIn('cached', text.lower())
        self.assertTrue(any(word in text.lower() for word in ('read_error', 'unavailable')))
        self.assertNotIn('Configure Sources', text)

    def test_cross_file_conflict_does_not_present_invalidated_metrics_as_current(self):
        self.append('OUTCAR', native_row(1).replace('-601.000000000', '-602.000000000'))
        self.append('stdout.log', electronic_row(1))
        snapshot = self.poll()
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertEqual(snapshot.metrics[0].latest.qualification, 'conflict')
        text = self.frame(snapshot)
        self.assertIn('conflict', text.lower())
        self.assertRegex(text, r'\|dE\|\s+—')
        self.assertRegex(text, r'\|d eps\|\s+—')
        self.assertIn('EDIFF 1E-6 eV', text)
        self.assertIn('conflict', progress_text(snapshot, detailed=True))
        self.assertTrue(snapshot.to_dict()['metrics'])

    def test_replaced_source_drops_old_metrics_and_correspondence(self):
        self.append('OUTCAR', native_row(1))
        self.append('stdout.log', electronic_row(1))
        initial = self.poll()
        replacement = self.root / 'replacement'
        replacement.write_text('')
        replacement.replace(self.root / 'stdout.log')
        changed = self.poll()
        self.assertEqual(changed.status, 'SOURCE_UNRESOLVED')
        self.assertEqual(changed.context.binding_status, 'BOUND')
        self.assertFalse(changed.metrics)
        self.assertIsNone(changed.last_correspondence_at)
        self.assertGreater(changed.sources[-1].generation, initial.sources[-1].generation)
        text = self.frame(changed)
        self.assertIn('|dE| —', text)
        self.assertNotIn('Configure Sources', text)

    def test_complete_static_force_has_no_ionic_target_even_with_positive_ediffg(self):
        self.write('OUTCAR', self.native['OUTCAR'])
        self.write('stdout.log', self.native['OSZICAR'])
        snapshot = self.poll()
        force = next(series.latest for series in snapshot.metrics if series.latest.semantic_id == 'vasp.force.maximum_atomic_norm.all')
        self.assertEqual(force.value, .5)
        self.assertIsNone(force.criterion)
        self.assertEqual(snapshot.calculation, 'static')
        text = self.frame(snapshot)
        self.assertIn('0.5 eV/Å', text)
        self.assertIn('@E1', text)
        self.assertIn('Target N/A · static', text)
        self.assertNotIn('Positive EDIFFG is energy-based', text)
        self.assertIn('Positive EDIFFG is energy-based', progress_text(snapshot, detailed=True))
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')

    def test_ordinary_constrained_optimization_retains_force_target_and_context(self):
        native = native_texts(relaxation=True, ediff='1e-6', footer=False)
        native['OUTCAR'] = native['OUTCAR'].replace(
            ' reached required accuracy - stopping structural energy minimisation\n', '')
        self.write('OUTCAR', native['OUTCAR'])
        self.write('stdout.log', native['OSZICAR'])
        self.write('POSCAR', structure_text(flags=((True,) * 3, (False,) * 3)))
        self.context = replace(self.context, sources=(*self.context.sources,
            SourceDescriptor('poscar', str(self.root / 'POSCAR'), optional=True)))
        self.append('OUTCAR', ' Iteration 2( 1)\n EDDAV: synthetic\n free energy    TOTEN = -2.000000000000 eV\n')
        self.append('stdout.log', 'DAV: 1 -2.000000000000 -2.00000000E+00 -0.02000000E+00 12 0.00100000\n')
        snapshot = self.poll()
        self.assertEqual(snapshot.calculation, 'fixed-cell-relaxation')
        force = next(series.latest for series in snapshot.metrics if series.latest.semantic_id == 'vasp.force.maximum_atomic_norm.free')
        self.assertIsNotNone(force.criterion)
        self.assertEqual(force.context[0].index, 1)
        text = self.frame(snapshot, width=140)
        self.assertIn('Eval 2/10', text)
        self.assertIn('Fmax movable', text)
        self.assertIn('@E1', text)
        self.assertIn('Target 0.020 eV/Å', text)
        self.assertNotIn('Target N/A', text)
        self.assertEqual(text.count('Fmax'), 1)

    def test_details_and_frozen_copy_retain_all_diagnostics(self):
        self.context = replace(self.context, limitations=(
            'Output location is not declared by a supported route',
            'Location declarations do not establish process ownership or scientific validity.'))
        self.append('OUTCAR', native_row(1))
        snapshot = self.poll()
        before = snapshot.to_dict()
        text = self.frame(snapshot)
        self.assertNotIn('Output location is not declared', text)
        self.assertNotIn('ownership', text)
        self.assertEqual(snapshot.to_dict(), before)
        details = progress_text(snapshot, detailed=True)
        frozen = capture_snapshot({'id': 25, 'attempt_id': 'invented-attempt', 'status': 'RUNNING'},
                                  progress=snapshot, captured_at=self.now, runtime={})
        diagnostics = render_snapshot(frozen, mode='diagnostics')
        copied = frozen.to_dict()['progress']
        for message in (*snapshot.messages, *snapshot.limitations):
            with self.subTest(message=message):
                self.assertIn(message, details)
                self.assertIn(message, diagnostics)
        self.assertEqual(copied['messages'], list(snapshot.messages))
        self.assertEqual(copied['limitations'], list(snapshot.limitations))


if __name__ == '__main__':
    unittest.main()
