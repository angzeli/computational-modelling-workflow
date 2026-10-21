"""Explicit add-time sources are immutable metadata, never execution authority."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs.source_declarations import (
    SOURCE_ORIGIN, SOURCE_QUALIFICATION, parse_sources, recorded_sources, validate_sources,
)
from cmw.jobs.store import ACTIVE, PENDING, TERMINAL, JobsError, Store
from cmw.progress.models import SourceDescriptor
from cmw.progress.registry import Registration, SourcePlan, _REGISTRY


class SourceDeclarationTests(unittest.TestCase):
    def test_literal_paths_and_registered_optional_roles_do_not_touch_filesystem(self):
        declarations = ['outcar=/not-mounted/繁體 $(touch never)/OUTCAR',
                        'oszicar=/not-mounted/another=literal/OSZICAR',
                        'poscar=/not-mounted/input/POSCAR']
        with patch.object(Path, 'stat', side_effect=AssertionError('No stat at add time')), \
                patch.object(Path, 'resolve', side_effect=AssertionError('No resolve at add time')):
            sources = parse_sources(declarations, 'VASP')
            result = validate_sources('VASP', sources, require_complete=True)
        self.assertEqual(result[0].path, declarations[0].split('=', 1)[1])
        self.assertFalse(result[0].optional)
        self.assertTrue(result[2].optional)

    def test_invalid_syntax_and_roles_fail_closed(self):
        for declaration in ('outcar=relative', 'outcar=~/OUTCAR', 'outcar=$HOME/OUTCAR',
                            'outcar=/bad\0file', 'outcar=/bad\nfile', 'OUTCAR=/absolute',
                            'unknown=/absolute', '=/', 'bad', 'outcar@=/absolute',
                            'outcar@@OPT=/absolute'):
            with self.subTest(declaration=declaration), self.assertRaises(JobsError):
                parse_sources([declaration], 'VASP')
        with self.assertRaises(JobsError):
            validate_sources('UNREGISTERED', [SourceDescriptor('outcar', '/absolute')], require_complete=True)
        self.assertEqual(parse_sources(['outcar=/absolute'], 'UNREGISTERED')[0].role, 'outcar')
        with self.assertRaises(JobsError):
            validate_sources('VASP', [SourceDescriptor('outcar', '/absolute', optional=1)])

    def test_duplicate_roles_paths_and_inconsistent_stages_are_rejected(self):
        for declarations in (
                ['outcar=/a', 'outcar=/b'], ['outcar=/a', 'oszicar=/a'],
                ['outcar=/a/../b', 'oszicar=/b'],
                ['orca_output@OPT=/a', 'orca_output=/b'],
                ['orca_output@OPT=/a', 'orca_output@FREQ=/a']):
            with self.subTest(declarations=declarations), self.assertRaises(JobsError):
                parse_sources(declarations, 'ORCA' if declarations[0].startswith('orca') else 'VASP')
        with self.assertRaises(JobsError):
            validate_sources('ORCA', [SourceDescriptor('orca_output:SP', '/a', stage='OPT')])

    def test_stage_order_and_legacy_factory_registration_remain_supported(self):
        sources = parse_sources(['orca_output@OPT=/a', 'orca_output@FREQ=/b',
                                 'orca_output@SP=/c'], 'ORCA')
        self.assertEqual([(s.role, s.stage) for s in sources],
                         [('orca_output:OPT', 'OPT'), ('orca_output:FREQ', 'FREQ'), ('orca_output:SP', 'SP')])
        with patch.dict(_REGISTRY, {'FICTIONAL': Registration(lambda context: None)}):
            self.assertEqual(parse_sources(['flux@inner=/a'], 'FICTIONAL')[0].role, 'flux:inner')
        self.assertEqual(validate_sources('Unknown', ()), ())

    def test_single_stage_vasp_keeps_native_roles_and_multiple_stages_are_refused(self):
        sources = parse_sources(['outcar@relax=/a', 'oszicar@relax=/b'], 'VASP')
        self.assertEqual([(s.role, s.stage) for s in sources], [('outcar', 'relax'), ('oszicar', 'relax')])
        for declarations in (['outcar@first=/a', 'outcar@second=/b'],
                             ['outcar@first=/a', 'oszicar@second=/b']):
            with self.subTest(declarations=declarations), self.assertRaisesRegex(JobsError, 'does not support multiple stages'):
                parse_sources(declarations, 'VASP')

    def test_required_roles_are_not_downgraded_and_explicit_stdout_is_compatible(self):
        supplied = (SourceDescriptor('outcar', '/a', optional=True), SourceDescriptor('oszicar', '/b'))
        self.assertFalse(validate_sources('VASP', supplied, require_complete=True)[0].optional)
        with self.assertRaisesRegex(JobsError, 'Missing required'):
            validate_sources('VASP', supplied[:1], require_complete=True)
        # Partial invocation-local sources remain supported; complete add may use
        # the registered stdout alternative to OSZICAR.
        self.assertEqual(parse_sources(['stdout=/log'], 'VASP')[0].role, 'stdout')
        stdout = parse_sources(['outcar=/a', 'stdout=/log'], 'VASP')
        self.assertEqual(len(validate_sources('VASP', stdout, require_complete=True)), 2)


class SourcePersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-source-declaration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'state')
        self.sources = (SourceDescriptor('outcar', str(self.root / 'future/OUTCAR')),
                        SourceDescriptor('oszicar', str(self.root / 'future/OSZICAR')))

    def add(self, **kwargs):
        return self.store.add(argv=['/unexecuted/synthetic', '--literal'], cwd=self.root,
            name='Same name', engine='VASP', **kwargs)

    def invoke(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['jobs', '--state', str(self.store.root), *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_add_reload_and_all_lifecycle_statuses_retain_exact_declaration(self):
        job = self.add(progress_sources=self.sources)
        record = deepcopy(job['progress_sources'])
        self.assertEqual(record['attempt_id'], job['attempt_id'])
        self.assertEqual((record['schema'], record['engine'], record['origin'], record['qualification']),
                         (1, 'VASP', SOURCE_ORIGIN, SOURCE_QUALIFICATION))
        self.assertFalse((self.root / 'future').exists())
        for status in sorted(ACTIVE | PENDING | TERMINAL):
            with self.subTest(status=status), self.store.transaction() as con:
                current = self.store.get(con, job['id'])
                current.pop('progress_sources')  # A lifecycle caller predating this field.
                current['status'] = status
                self.store.save(con, current)
            restored = Store(self.store.root).snapshot()['jobs'][0]
            self.assertEqual(restored['progress_sources'], record)
            self.assertEqual([s.path for s in recorded_sources(restored)], [s.path for s in self.sources])
        self.assertFalse(self.store.snapshot()['controller']['online'])
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])

    def test_existing_attempt_cannot_attach_replace_or_change_binding_identity(self):
        legacy = self.add()
        declared = self.add(progress_sources=self.sources)
        for target, edit in ((legacy, {'progress_sources': declared['progress_sources']}),
                             (legacy, {'progress_sources': None}),
                             (declared, {'progress_sources': None}),
                             (declared, {'engine': 'ORCA'}),
                             (declared, {'attempt_id': 'different'})):
            before = self.store.path.read_bytes()
            with self.subTest(edit=edit), self.assertRaises(JobsError):
                with self.store.transaction() as con:
                    current = self.store.get(con, target['id'])
                    current.update(edit)
                    self.store.save(con, current)
            self.assertEqual(self.store.path.read_bytes(), before)
        self.assertNotIn('progress_sources', Store(self.store.root).snapshot()['jobs'][0])

    def test_annotation_display_and_queue_edits_preserve_sources(self):
        job = self.add(progress_sources=self.sources)
        record = deepcopy(job['progress_sources'])
        self.store.annotate(job['id'], 'Human note')
        self.store.rename_id(job['id'], 'M1.1')
        self.store.change(job['id'], 'hold')
        self.store.change(job['id'], 'release')
        self.assertEqual(self.store.snapshot()['jobs'][0]['progress_sources'], record)

    def test_invalid_add_creates_no_state_and_does_not_change_existing_queue(self):
        invalid = [(SourceDescriptor('outcar', 'relative'),),
                   (SourceDescriptor('unexpected', '/a'),),
                   (SourceDescriptor('outcar', '/a'),),
                   (SourceDescriptor('outcar', '/a'), SourceDescriptor('oszicar', '/a'))]
        for sources in invalid:
            with self.subTest(sources=sources), self.assertRaises(JobsError):
                self.add(progress_sources=sources)
            self.assertFalse(self.store.root.exists())
        self.add()
        before = self.store.path.read_bytes()
        for sources in invalid:
            with self.assertRaises(JobsError):
                self.add(progress_sources=sources)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_schema_three_and_active_legacy_guard_are_unchanged(self):
        self.add(progress_sources=self.sources, suspension_backend='local-command-v1')
        self.assertEqual(self.store.snapshot()['schema'], 3)
        legacy = Store(self.root / 'legacy')
        job = legacy.add(argv=['/unexecuted'], cwd=self.root, name='legacy')
        with legacy.transaction() as con:
            current = legacy.get(con, job['id'])
            current['status'] = 'Run'
            legacy.save(con, current)
            control = legacy.control(con)
            control['schema'] = 1
            legacy.set_control(con, control)
        before = legacy.path.read_bytes()
        with self.assertRaisesRegex(JobsError, 'Legacy Jobs runtime'):
            legacy.add(argv=['/unexecuted'], cwd=self.root, name='future', engine='VASP',
                       progress_sources=self.sources)
        self.assertEqual(legacy.path.read_bytes(), before)
        self.assertNotIn('progress_sources', legacy.snapshot()['jobs'][0])

    def test_record_identity_and_schema_validation_are_independent_of_adapter_availability(self):
        job = self.add(progress_sources=self.sources)
        for changes in ({'schema': 99}, {'schema': True}, {'attempt_id': 'other'},
                        {'engine': 'ORCA'}, {'origin': 'verified'}, {'sources': []},
                        {'qualification': 'scientifically verified'}):
            altered = deepcopy(job)
            altered['progress_sources'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(JobsError):
                recorded_sources(altered)
        with patch.dict(_REGISTRY, clear=True):
            self.assertEqual(len(recorded_sources(job)), 2)
        self.assertEqual(recorded_sources({'engine': 'VASP'}), ())

    def test_cli_add_preserves_post_separator_argv_and_persists_literal_paths(self):
        path = self.root / '繁體 $(touch never)' / 'OUTCAR'
        argv = ['/unexecuted/native', '--progress-source', 'literal=argument', '--source', 'outcar=relative']
        code, out, err = self.invoke('add', '--name', 'Synthetic', '--engine', 'VASP', '--cwd', str(self.root),
            '--progress-source', f'outcar={path}', '--progress-source', f'oszicar={self.sources[1].path}',
            '--hold', '--json', '--', *argv)
        self.assertEqual((code, err), (0, ''))
        job = json.loads(out)
        self.assertEqual(job['argv'], argv)
        self.assertEqual(job['status'], 'Hold')
        self.assertEqual(recorded_sources(job)[0].path, str(path))
        code, out, err = self.invoke('show', job['display_id'], '--json')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['progress_sources'], job['progress_sources'])
        self.assertFalse(path.parent.exists())

    def test_single_stage_vasp_declaration_is_observed_after_reload(self):
        from cmw.jobs.progress import resolve_context
        from cmw.progress.service import ObservationService
        from tests.periodic.vasp.result_fixtures import write_run
        write_run(self.root / 'future', relaxation=True, evaluations=2,
                  flags=((True,) * 3, (False,) * 3))
        poscar = self.root / 'future' / 'POSCAR'
        sources = parse_sources([f'outcar@relax={self.sources[0].path}',
                                 f'oszicar@relax={self.sources[1].path}',
                                 f'poscar@relax={poscar}'], 'VASP')
        job = self.add(progress_sources=sources)
        self.assertEqual([(s.role, s.stage) for s in recorded_sources(job)],
                         [('outcar', 'relax'), ('oszicar', 'relax'), ('poscar', 'relax')])
        before = self.store.path.read_bytes()
        context = resolve_context(Store(self.store.root), job['id'])
        self.assertEqual(context.attempt_id, job['attempt_id'])
        constraint_source = next(source for source in context.sources if source.role == 'poscar')
        self.assertEqual(constraint_source.path, str(poscar))
        self.assertEqual(constraint_source.origins, (SOURCE_ORIGIN,))
        self.assertEqual(constraint_source.qualification, SOURCE_ORIGIN + ': ' + SOURCE_QUALIFICATION)
        observer = ObservationService()
        try:
            snapshot = observer.poll(context)
        finally:
            observer.close()
        self.assertEqual(snapshot.status, 'OBSERVED')
        free_force = next(series.latest for series in snapshot.metrics
                          if series.latest.semantic_id == 'vasp.force.maximum_atomic_norm.free')
        self.assertAlmostEqual(free_force.value, (0.01 ** 2 + 0.002 ** 2 + 0.003 ** 2) ** 0.5)
        self.assertEqual(free_force.qualification, 'provisional')
        self.assertIsNotNone(free_force.criterion)
        self.assertIn('historical binding unestablished', free_force.criterion.definition)
        self.assertEqual(context.stage, 'relax')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_unknown_invocation_adapter_is_reported_without_changing_state(self):
        job = self.add()
        before = self.store.path.read_bytes()
        code, out, err = self.invoke('progress', job['display_id'], '--engine', 'UNREGISTERED',
                                    '--source', f'outcar={self.sources[0].path}', '--json')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['status'], 'ADAPTER_UNAVAILABLE')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_cli_invalid_add_has_no_mutation(self):
        for declaration in ('outcar=relative', 'unknown=/a', 'outcar=/a'):
            with self.subTest(declaration=declaration):
                code, out, _ = self.invoke('add', '--name', 'Synthetic', '--engine', 'VASP',
                    '--cwd', str(self.root), '--progress-source', declaration, '--json', '--', '/unexecuted')
                self.assertEqual((code, json.loads(out)['code']), (2, 'JOBS_ERROR'))
                self.assertFalse(self.store.root.exists())
