"""Synthetic association and identity checks; never launch a payload."""
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.locations import LocationDescriptor
from cmw.jobs.progress import resolve_context
from cmw.jobs.source_selection import activate_selection, inspect_selection, validate_selection
from cmw.jobs.store import JobsError, Store
from cmw.progress.models import SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import write_run


class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw source selection ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'jobs')
        self.job = self.store.add(argv=['/synthetic/opaque'], cwd=self.root, engine='VASP', name='Synthetic selection', hold=True)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.sources = tuple(SourceDescriptor(role, str(self.output / name))
                             for role, name in [('outcar', 'OUTCAR'), ('oszicar', 'OSZICAR')])
        for source in self.sources:
            Path(source.path).write_text('synthetic bounded evidence\n')

    def test_validation_is_read_only_and_requires_separate_activation(self):
        before = self.store.path.read_bytes()
        with patch('subprocess.run', side_effect=AssertionError('No payload launch')), \
                patch.object(Path, 'glob', side_effect=AssertionError('No output search')):
            validation = validate_selection(self.store, self.job['id'], self.sources)
            self.assertTrue(validation.valid)
            self.assertEqual(resolve_context(self.store, self.job['id']).binding_status, 'SOURCE_UNRESOLVED')
            selection = activate_selection(self.store, validation, 1)
            context = resolve_context(self.store, self.job['id'], session_selection=selection)
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual(context.binding_generation, 1)
        self.assertIn('session-local', context.association)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(resolve_context(self.store, self.job['id']).binding_status, 'SOURCE_UNRESOLVED')

    def test_native_looking_operational_or_nearby_stdout_is_not_implicitly_adopted(self):
        write_run(self.output)
        native = (self.output / 'OSZICAR').read_text()
        operational = Path(self.job['logs']['stdout'])
        operational.parent.mkdir(parents=True)
        operational.write_text(native)
        (self.output / 'stdout.log').write_text(native)
        (self.output / 'OSZICAR').write_text('')
        before = self.store.path.read_bytes()
        context = resolve_context(self.store, self.job['id'],
            descriptors=(LocationDescriptor('output', self.output),))
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual({source.role for source in context.sources}, {'outcar', 'oszicar'})
        service = ObservationService()
        self.addCleanup(service.close)
        snapshot = service.poll(context)
        self.assertFalse(any(series.latest.semantic_id == 'vasp.electronic.dE'
                             for series in snapshot.metrics))
        self.assertEqual({source.role for source in snapshot.sources}, {'outcar', 'oszicar'})
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_compatible_origins_merge_and_conflicts_require_explicit_review(self):
        automatic = (LocationDescriptor('output', self.output, origin='recognized-runner'),)
        job = self.store.add(argv=['/synthetic/opaque'], cwd=self.root, engine='VASP', name='Synthetic selection', hold=True,
                             progress_sources=self.sources)
        context = resolve_context(self.store, job['id'], descriptors=automatic)
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual(context.sources[0].origins, ('recognized-runner', 'user-declared-at-add'))
        other = self.root / 'other'; other.mkdir()
        declarations = (LocationDescriptor('output', other, origin='recognized-runner'),)
        context = resolve_context(self.store, job['id'], descriptors=declarations)
        self.assertEqual(context.binding_status, 'SOURCE_CONFLICT')
        self.assertFalse(context.sources)
        validation = validate_selection(self.store, job['id'], self.sources, descriptors=declarations)
        self.assertTrue(validation.valid)
        self.assertTrue(validation.conflict)
        with self.assertRaisesRegex(JobsError, 'Confirm'):
            activate_selection(self.store, validation, 1, descriptors=declarations)
        selected = activate_selection(self.store, validation, 1, descriptors=declarations, confirmed=True)
        chosen = resolve_context(self.store, job['id'], descriptors=declarations, session_selection=selected)
        self.assertEqual(chosen.binding_status, 'BOUND')
        self.assertTrue(chosen.source_conflicts)
        self.assertEqual(chosen.declarations, context.declarations)

    def test_optional_conflicts_do_not_block_required_sources(self):
        declarations = (LocationDescriptor('output', self.output),
                        LocationDescriptor('inputs', self.root / 'one'),
                        LocationDescriptor('inputs', self.root / 'two'))
        context = resolve_context(self.store, self.job['id'], descriptors=declarations)
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual({source.role for source in context.sources}, {'outcar', 'oszicar'})
        self.assertTrue(any('poscar: conflicting' in message for message in context.limitations))

    def test_automatic_required_roles_cannot_alias_one_canonical_file(self):
        Path(self.sources[1].path).unlink()
        Path(self.sources[1].path).symlink_to(self.sources[0].path)
        context = resolve_context(self.store, self.job['id'],
            descriptors=(LocationDescriptor('output', self.output),))
        self.assertEqual(context.binding_status, 'SOURCE_CONFLICT')
        self.assertFalse(context.sources)

    def test_unavailable_required_and_optional_files_have_distinct_validation(self):
        optional = SourceDescriptor('poscar', str(self.root / 'POSCAR'), optional=True)
        sources = (*self.sources, optional)
        validation = validate_selection(self.store, self.job['id'], sources)
        self.assertTrue(validation.valid)
        self.assertTrue(validation.warnings)
        from cmw.jobs.locations import inspect_path
        def denied(path, kind, **kwargs):
            if str(path) == self.sources[0].path:
                return str(path), 'PERMISSION_DENIED', None, None, 'synthetic access denial'
            return inspect_path(path, kind, **kwargs)
        with patch('cmw.jobs.source_selection.inspect_path', side_effect=denied):
            self.assertFalse(validate_selection(self.store, self.job['id'], sources).valid)
        invalid_optional = replace(optional, path=str(self.output))
        self.assertFalse(validate_selection(self.store, self.job['id'], (*self.sources, invalid_optional)).valid)

    def test_automatic_inaccessible_path_still_has_an_established_declaration(self):
        descriptors = (LocationDescriptor('output', self.output),)
        with patch.object(Path, 'resolve', side_effect=PermissionError('synthetic access denial')):
            context = resolve_context(self.store, self.job['id'], descriptors=descriptors)
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual(len(context.sources), 2)

    def test_file_growth_does_not_invalidate_validation(self):
        validation = validate_selection(self.store, self.job['id'], self.sources)
        with Path(self.sources[0].path).open('a') as stream:
            stream.write('additional evidence\n')
        selected = activate_selection(self.store, validation, 1)
        self.assertEqual(resolve_context(self.store, self.job['id'], session_selection=selected).binding_status, 'BOUND')

    def test_file_or_directory_replacement_and_symlink_redirect_require_validation(self):
        alias = self.root / 'alias'; alias.symlink_to(self.output, target_is_directory=True)
        sources = tuple(replace(source, path=str(alias / Path(source.path).name)) for source in self.sources)
        validation = validate_selection(self.store, self.job['id'], sources)
        selected = activate_selection(self.store, validation, 1)
        self.output.rename(self.root / 'old')
        self.output.mkdir()
        for source in self.sources:
            Path(source.path).write_text('replaced')
        with self.assertRaisesRegex(JobsError, 'validate again'):
            resolve_context(self.store, self.job['id'], session_selection=selected)
        with self.assertRaisesRegex(JobsError, 'validate again'):
            activate_selection(self.store, validation, 2)
        alias.unlink(); alias.symlink_to(self.root / 'old', target_is_directory=True)
        with self.assertRaisesRegex(JobsError, 'validate again'):
            resolve_context(self.store, self.job['id'], session_selection=selected)

    def test_same_path_new_database_is_a_different_session_identity(self):
        selected = activate_selection(self.store, validate_selection(self.store, self.job['id'], self.sources), 1)
        replacement = self.root / 'replacement.sqlite3'
        shutil.copyfile(self.store.path, replacement)
        replacement.replace(self.store.path)
        with self.assertRaisesRegex(JobsError, 'stale'):
            resolve_context(self.store, self.job['id'], session_selection=selected)

    def test_job_attempt_engine_and_state_isolation(self):
        selected = activate_selection(self.store, validate_selection(self.store, self.job['id'], self.sources), 1)
        other = self.store.add(argv=['/synthetic/opaque'], cwd=self.root, engine='VASP', name='Synthetic selection', hold=True)
        with self.assertRaises(JobsError):
            resolve_context(self.store, other['id'], session_selection=selected)
        with self.assertRaises(JobsError):
            resolve_context(self.store, self.job['id'], session_selection=replace(selected, identity=(*selected.identity[:4], 'other', 'VASP')))
        with self.assertRaises(JobsError):
            resolve_context(self.store, self.job['id'], engine='ORCA', session_selection=selected)
        second = Store(self.root / 'second')
        job = second.add(argv=['/synthetic/opaque'], cwd=self.root, engine='VASP', name='Synthetic selection', hold=True)
        with self.assertRaises(JobsError):
            resolve_context(second, job['id'], session_selection=selected)

    def test_missing_future_sources_remain_bound_and_start_when_they_appear(self):
        future = self.root / 'future'
        sources = tuple(replace(source, path=str(future / Path(source.path).name)) for source in self.sources)
        validation = validate_selection(self.store, self.job['id'], sources)
        self.assertTrue(validation.valid, validation.errors)
        self.assertTrue(validation.warnings)
        selected = activate_selection(self.store, validation, 1)
        service = ObservationService(); self.addCleanup(service.close)
        before = resolve_context(self.store, self.job['id'], session_selection=selected)
        service.poll(before)
        future.mkdir()
        for source in sources:
            Path(source.path).write_text('test evidence\n')
        after = resolve_context(self.store, self.job['id'], session_selection=selected)
        self.assertNotEqual(before.key, after.key)
        self.assertEqual(after.binding_status, 'BOUND')
        self.assertNotEqual(service.poll(after).status, 'SOURCE_UNRESOLVED')

    def test_later_unavailable_evidence_keeps_the_established_binding(self):
        selected = activate_selection(self.store, validate_selection(self.store, self.job['id'], self.sources), 1)
        Path(self.sources[0].path).unlink()
        context = resolve_context(self.store, self.job['id'], session_selection=selected)
        self.assertEqual(context.binding_status, 'BOUND')
        service = ObservationService(); self.addCleanup(service.close)
        snapshot = service.poll(context)
        self.assertTrue(any(source.health != 'OK' for source in snapshot.sources))

    def test_directory_and_duplicate_aliases_are_invalid_sources(self):
        validation = validate_selection(self.store, self.job['id'],
            (replace(self.sources[0], path=str(self.output)), self.sources[1]))
        self.assertFalse(validation.valid)
        alias = self.root / 'file alias'; alias.symlink_to(self.sources[0].path)
        with self.assertRaisesRegex(JobsError, 'multiple progress roles'):
            validate_selection(self.store, self.job['id'], (self.sources[0], replace(self.sources[1], path=str(alias))))

    def test_automatic_missing_is_bound_and_inspection_never_selects_cwd(self):
        descriptors = (LocationDescriptor('output', self.root / 'absent'),)
        inspection = inspect_selection(self.store, self.job['id'], descriptors=descriptors)
        self.assertEqual(inspection.context.binding_status, 'BOUND')
        self.assertEqual(len(inspection.context.sources), 2)
        self.assertEqual(inspect_selection(self.store, self.job['id']).context.binding_status, 'SOURCE_UNRESOLVED')

    def test_changed_default_declaration_invalidates_session_override(self):
        descriptors = (LocationDescriptor('output', self.output),)
        validation = validate_selection(self.store, self.job['id'], self.sources, descriptors=descriptors)
        selection = activate_selection(self.store, validation, 1, descriptors=descriptors)
        with self.assertRaisesRegex(JobsError, 'stale'):
            resolve_context(self.store, self.job['id'], descriptors=(LocationDescriptor('output', self.root/'other'),),
                            session_selection=selection)
