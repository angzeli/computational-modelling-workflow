"""Bounded source-state distinctions, native clocks and visible-value regressions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.progress.card import card_text
from cmw.progress.formatting import progress_text
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.reader import RegularFileReader
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts
from tests.progress.test_vasp_crosscheck import electronic_row, native_row


class SourceDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-source-diagnostics-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = 100.0
        self.service = ObservationService(clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.context = RunContext('synthetic-diagnostics', 'VASP', tuple(
            SourceDescriptor(role, str(self.root / name)) for role, name in
            (('outcar', 'OUTCAR'), ('oszicar', 'OSZICAR'), ('stdout', 'stdout.log'))))
        self.prefix = native_texts()['OUTCAR'].split('--------------------------------------- Iteration')[0]
        (self.root / 'OUTCAR').write_text(self.prefix)
        (self.root / 'OSZICAR').write_text('')
        (self.root / 'stdout.log').write_text('')

    def poll(self):
        result = self.service.poll(self.context)
        self.assertNotEqual(result.status, 'ADAPTER_ERROR', result.messages)
        return result

    def append(self, name, value):
        with (self.root / name).open('a') as handle:
            handle.write(value)

    def diagnostic(self, snapshot, role):
        return next(item for item in snapshot.source_diagnostics if item.role == role)

    def test_empty_and_undeclared_have_no_source_local_claim(self):
        self.context = RunContext('no-stdout', 'VASP', self.context.sources[:2])
        snapshot = self.poll()
        self.assertEqual(self.diagnostic(snapshot, 'oszicar').status, 'EMPTY')
        self.assertEqual(self.diagnostic(snapshot, 'stdout').status, 'UNDECLARED')
        text = card_text(snapshot, width=80, now=self.now)
        self.assertIn('oszicar empty', text)
        self.assertIn('stdout not declared', text)
        self.assertNotIn('source-only', text)
        self.assertNotIn('source-local', text)
        self.assertNotIn('data observed', text)
        self.assertIn('EDIFF (both) <1e-06 eV', text)

    def test_unparsed_partial_malformed_and_unsupported_are_distinct(self):
        cases = (('header only\n', 'UNPARSED'), ('DAV: 1 ', 'NOT_YET_PARSED'),
                 ('DAV: 1 -1 0.1\n', 'MALFORMED'), ('CG: 1 -1 0.1 0.1 12 1\n', 'UNSUPPORTED'))
        for index, (text, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                (self.root / 'stdout.log').write_text(text)
                self.context = RunContext(f'case-{index}', 'VASP', self.context.sources)
                snapshot = self.poll()
                self.assertEqual(self.diagnostic(snapshot, 'stdout').status, expected)
                self.assertEqual(self.diagnostic(snapshot, 'stdout').usable_rows, 0)
                self.assertIn(expected, progress_text(snapshot, detailed=True))

    def test_read_error_and_missing_are_unavailable_not_empty(self):
        (self.root / 'stdout.log').unlink()
        snapshot = self.poll()
        self.assertEqual(self.diagnostic(snapshot, 'stdout').status, 'UNAVAILABLE')
        self.assertIn('MISSING', self.diagnostic(snapshot, 'stdout').reason)
        self.assertEqual(next(s for s in snapshot.sources if s.role == 'stdout').last_attempt_at, self.now)

    def test_distinct_clocks_and_outcar_advance_never_refresh_electronic_age(self):
        self.append('stdout.log', electronic_row(1) + electronic_row(2))
        first = self.poll()
        self.assertEqual(self.diagnostic(first, 'stdout').status, 'SOURCE_LOCAL')
        self.assertIsNone(first.last_correspondence_at)
        self.now = 110
        self.append('OUTCAR', native_row(1))
        second = self.poll()
        self.assertEqual(self.diagnostic(second, 'stdout').status, 'PARTIALLY_MATCHED')
        self.assertEqual(second.last_structural_evidence_at, 110)
        self.assertEqual(second.last_correspondence_at, 110)
        self.assertEqual(self.diagnostic(second, 'stdout').last_complete_row_at, 100)
        self.assertIn('data 10s ago', card_text(second, width=140, now=self.now))
        self.now = 120
        self.append('OUTCAR', native_row(2))
        third = self.poll()
        self.assertEqual(self.diagnostic(third, 'stdout').status, 'MATCHED')
        self.assertEqual(third.last_correspondence_at, 120)
        self.now = 130
        unchanged = self.poll()
        self.assertEqual(unchanged.last_observation_at, 130)
        self.assertEqual(unchanged.last_correspondence_at, 120)
        self.assertEqual(unchanged.last_structural_evidence_at, 120)
        source = next(item for item in unchanged.sources if item.role == 'stdout')
        self.assertEqual(source.last_success_at, 130)
        self.assertEqual(source.last_growth_at, 100)
        self.assertIn('data 30s ago', card_text(unchanged, width=140, now=self.now))

    def test_both_electronic_sources_report_their_established_native_match(self):
        self.append('OUTCAR', native_row(1) + native_row(2))
        for name in ('OSZICAR', 'stdout.log'):
            self.append(name, electronic_row(1) + electronic_row(2))
        snapshot = self.poll()
        self.assertEqual(snapshot.verification[0].source_role, 'oszicar')
        for role in ('oszicar', 'stdout'):
            diagnostic = self.diagnostic(snapshot, role)
            self.assertEqual(diagnostic.status, 'MATCHED')
            self.assertEqual(diagnostic.usable_rows, 2)
            self.assertIn('OUTCAR checked through SCF 2 for label, energy, algorithm only', diagnostic.reason)

    def test_nonselected_source_reports_partial_native_frontier_and_later_match(self):
        self.append('OUTCAR', native_row(1))
        self.append('OSZICAR', electronic_row(1) + electronic_row(2))
        self.append('stdout.log', electronic_row(1) + electronic_row(2) + electronic_row(3))
        snapshot = self.poll()
        self.assertEqual(snapshot.verification[0].source_role, 'stdout')
        diagnostic = self.diagnostic(snapshot, 'oszicar')
        self.assertEqual(diagnostic.status, 'PARTIALLY_MATCHED')
        self.assertEqual((diagnostic.usable_rows, diagnostic.latest_iteration), (2, 2))
        self.assertIn('OUTCAR checked through SCF 1 for label, energy, algorithm only', diagnostic.reason)
        self.now = 110
        self.append('OUTCAR', native_row(2))
        promoted = self.poll()
        self.assertEqual(self.diagnostic(promoted, 'oszicar').status, 'MATCHED')
        self.assertEqual(self.diagnostic(promoted, 'stdout').status, 'PARTIALLY_MATCHED')
        self.assertEqual(self.diagnostic(promoted, 'oszicar').last_complete_row_at, 100)

    def test_outcar_alone_never_fabricates_electronic_metrics(self):
        self.append('OUTCAR', native_row(1))
        self.poll()
        self.now = 110
        self.append('OUTCAR', native_row(2))
        snapshot = self.poll()
        self.assertFalse(snapshot.metrics)
        self.assertEqual(self.diagnostic(snapshot, 'stdout').status, 'EMPTY')
        self.assertNotIn('source-local', card_text(snapshot, width=80, now=self.now))

    def test_partial_record_keeps_real_previous_complete_row(self):
        self.append('stdout.log', electronic_row(1) + 'DAV: 2 -610.0')
        snapshot = self.poll()
        diagnostic = self.diagnostic(snapshot, 'stdout')
        self.assertEqual((diagnostic.status, diagnostic.complete_rows, diagnostic.usable_rows),
                         ('NOT_YET_PARSED', 1, 1))
        self.assertEqual(snapshot.metrics[0].latest.context[-1].index, 1)
        self.assertIn('stdout catching up', card_text(snapshot, width=140, now=self.now))
        self.assertIn('1 complete rows', card_text(snapshot, width=140, now=self.now))

    def test_conflict_is_explicit_and_cannot_be_overwritten_by_empty_peer(self):
        self.append('stdout.log', electronic_row(1))
        self.append('OUTCAR', native_row(1).replace('-601.000000000', '-602.000000000'))
        snapshot = self.poll()
        self.assertEqual(self.diagnostic(snapshot, 'stdout').status, 'CONFLICT')
        self.assertIn('SOURCE CONFLICT', card_text(snapshot, width=77, now=self.now))
        self.assertEqual(snapshot.metrics[0].latest.qualification, 'conflict')

    def test_mixed_run_segments_never_share_target_or_native_context(self):
        self.append('OUTCAR', native_row(1))
        self.append('stdout.log', 'vasp.6.6.1\n' + electronic_row(1) + 'vasp.6.6.1\n' + electronic_row(1))
        snapshot = self.poll()
        for series in snapshot.metrics:
            self.assertEqual([level.kind for level in series.latest.context], ['electronic'])
            self.assertIsNone(series.latest.criterion)
        self.assertIsNone(snapshot.verification[0].checked_frontier)
        self.assertNotIn('EDIFF (both) <', card_text(snapshot, width=80, now=self.now))

    def test_failed_read_advances_only_attempt_clock(self):
        source = self.context.sources[-1]
        reader = RegularFileReader(source)
        self.addCleanup(reader.close)
        initial = reader.poll(100)
        with patch('cmw.progress.reader.os.stat', side_effect=PermissionError('denied')):
            failed = reader.poll(200)
        self.assertEqual(failed.state.last_attempt_at, 200)
        self.assertEqual(failed.state.last_success_at, initial.state.last_success_at)
        self.assertEqual(failed.state.health, 'PERMISSION_DENIED')

    def test_unsupported_banner_cannot_be_verified_by_matching_energies(self):
        self.append('OUTCAR', native_row(1))
        self.append('stdout.log', 'vasp.5.4.4\n' + electronic_row(1))
        snapshot = self.poll()
        self.assertEqual(self.diagnostic(snapshot, 'stdout').status, 'UNSUPPORTED')
        self.assertFalse(snapshot.metrics)
        self.assertFalse(any(check.checked_frontier for check in snapshot.verification))

    def test_malformed_new_evaluation_does_not_hide_retained_previous_values(self):
        self.append('stdout.log', electronic_row(1))
        self.poll()
        self.now = 110
        self.append('stdout.log', 'DAV: 1 garbage\n')
        snapshot = self.poll()
        self.assertEqual(self.diagnostic(snapshot, 'stdout').usable_rows, 0)
        self.assertTrue(snapshot.metrics)
        text = card_text(snapshot, width=140, now=self.now)
        self.assertIn('stdout malformed', text)
        self.assertIn('cached SCF1', text)
        self.assertIn('data 10s ago', text)
        self.assertNotIn('SCF values unavailable', text)

    def test_generation_reset_clears_correspondence_clock(self):
        self.append('stdout.log', electronic_row(1))
        self.append('OUTCAR', native_row(1))
        self.assertEqual(self.poll().last_correspondence_at, 100)
        (self.root / 'stdout.log').write_text('')
        self.now = 110
        self.assertIsNone(self.poll().last_correspondence_at)
