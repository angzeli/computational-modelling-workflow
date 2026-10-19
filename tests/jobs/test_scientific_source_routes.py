"""Declared alternatives stay explicit; synthetic sources never execute a payload."""
from pathlib import Path
import unittest
from unittest.mock import patch

from cmw.jobs.progress import reconcile_sources, resolve_context
from cmw.jobs.source_selection import inspect_selection, validate_selection
from cmw.progress.models import SourceDescriptor
from cmw.progress.registry import get_registration
from tests.jobs import test_source_selection as selection_tests


class ScientificSourceRoutesTests(unittest.TestCase):
    setUp = selection_tests.SourceSelectionTests.setUp

    def test_inspection_exposes_stdout_alternative_and_declared_input(self):
        before = self.store.path.read_bytes()
        inspection = inspect_selection(self.store, self.job['id'])
        plans = {plan.role: plan for plan in inspection.plans}
        self.assertEqual(set(plans), {'outcar', 'oszicar', 'poscar', 'incar', 'stdout'})
        self.assertIn('stdout', plans['oszicar'].alternatives)
        self.assertIsNone(plans['stdout'].filename)
        self.assertEqual(plans['incar'].location_role, 'inputs')
        self.assertEqual(before, self.store.path.read_bytes())

    def test_stdout_can_replace_oszicar_without_relabeling_or_discovery(self):
        stdout = self.root / 'caller-declared-stream'
        stdout.write_text('DAV: 1 -624.178913665 +3.1087 -2.9332 120 1.89 1.41\n')
        sources = (self.sources[0], SourceDescriptor('stdout', str(stdout)))
        with patch.object(Path, 'glob', side_effect=AssertionError('No discovery')):
            validation = validate_selection(self.store, self.job['id'], sources)
            context = resolve_context(self.store, self.job['id'], sources=sources)
        self.assertTrue(validation.valid, validation.errors)
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual({source.role for source in context.sources}, {'outcar', 'stdout'})

    def test_conflicting_alternative_is_retained_as_required_review(self):
        registration = get_registration('VASP')
        sources = (*self.sources, SourceDescriptor('stdout', str(self.root / 'first'), optional=True),
                   SourceDescriptor('stdout', str(self.root / 'second'), optional=True))
        selected, conflicts, warnings = reconcile_sources(sources, (*registration.sources, *registration.source_roles))
        self.assertTrue(any('stdout: conflicting' in item for item in conflicts))
        self.assertFalse(any('stdout: conflicting' in item for item in warnings))
        self.assertNotIn('stdout', {source.role for source in selected})

    def test_input_settings_need_declared_input_root(self):
        from cmw.jobs.locations import LocationDescriptor
        without = resolve_context(self.store, self.job['id'],
                                  descriptors=(LocationDescriptor('output', self.output),))
        self.assertNotIn('incar', {source.role for source in without.sources})
        declared = resolve_context(self.store, self.job['id'], descriptors=(
            LocationDescriptor('output', self.output), LocationDescriptor('inputs', self.root)))
        settings = next(source for source in declared.sources if source.role == 'incar')
        self.assertEqual(settings.path, str(self.root / 'INCAR'))
        self.assertTrue(settings.optional)
