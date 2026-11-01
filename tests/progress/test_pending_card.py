"""Pending observations retain descriptors without confusing source declarations."""
from dataclasses import replace
import unittest

from cmw.progress.formatting import progress_text
from cmw.progress.models import ProgressSnapshot, RunContext, SourceDescriptor
from cmw.progress.vasp_card import vasp_card_groups


class PendingCardTests(unittest.TestCase):
    def test_bound_unresolved_empty_snapshot_keeps_bordered_card(self):
        context = RunContext('invented-attempt', 'VASP', (
            SourceDescriptor('outcar', '/invented/OUTCAR'),
            SourceDescriptor('stdout', '/invented/stdout.log')))
        snapshot = ProgressSnapshot(context, 'vasp-live-6.6.1',
            status='SOURCE_UNRESOLVED', messages=('Native correspondence pending',))
        snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
        text = progress_text(snapshot, width=73, max_lines=7, framed=True)
        self.assertTrue(text.startswith('┌'), text)
        for label in ('SCF', 'FORCE', 'ELECTRONIC'):
            self.assertIn(label, text)
        self.assertIn('pending', text.lower())
        self.assertNotIn('Scientific: BOUND', text)
        self.assertNotIn('Configure Sources', text)

    def test_service_binding_failure_is_not_cross_file_waiting(self):
        from cmw.progress.service import ObservationService
        service = ObservationService()
        self.addCleanup(service.close)
        snapshot = service.poll(RunContext('empty-declaration', 'VASP'))
        text = progress_text(snapshot, width=80, framed=True)
        self.assertIn('Sources unresolved', text)
        self.assertIn('Configure Sources', text)
        self.assertNotIn('correspondence pending', text)

    def test_conflicted_primary_cannot_fall_back_to_another_force_scope(self):
        from cmw.progress.models import CardCell, CardGroup, Metric, MetricSeries
        primary = Metric('free', 'Fmax movable', 'Synthetic movable norm', 'eV', 9., qualification='conflict')
        fallback = replace(primary, semantic_id='all', label='Fmax all', value=8., qualification='observed')
        snapshot = ProgressSnapshot(RunContext('conflicted-scope', 'FICTIONAL'), 'test',
            status='SOURCE_CONFLICT', metrics=(MetricSeries(primary), MetricSeries(fallback)),
            card_groups=(CardGroup('FORCE', ((CardCell(('free', 'all'), 'Fmax movable'),),)),))
        text = progress_text(snapshot, width=80)
        self.assertIn('Fmax movable —', text)
        self.assertNotIn('8 eV', text)
        self.assertIn('8 eV', progress_text(snapshot, detailed=True))
