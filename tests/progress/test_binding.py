"""Synthetic association boundaries; no controller or engine is launched."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.locations import LocationDescriptor
from cmw.jobs.progress import resolve_context
from cmw.jobs.store import JobsError, Store
from cmw.progress.models import (Criterion, Evidence, IterationLevel, Metric, MetricSeries,
                                  SourceDescriptor, comparable)
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts


class BindingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='cmw-progress-binding-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.inputs, self.output = self.root/'inputs', self.root/'results 繁體'
        self.inputs.mkdir(); self.output.mkdir()
        self.store = Store(self.root/'state')

    def job(self, known=True, store=None, engine='VASP'):
        argv = (['/not-executed/run-vasp.sh', '--input', str(self.inputs), '--output', str(self.output)]
                if known else ['/not-executed/wrapper'])
        return (store or self.store).add(argv=argv, cwd=self.inputs, name='not a source', engine=engine, hold=True)

    def test_uses_declared_output_and_preserves_offline_database(self):
        job = self.job()
        before = self.store.path.read_bytes()
        with patch.object(self.store, 'snapshot', side_effect=AssertionError('no activity snapshot')):
            context = resolve_context(self.store, job['display_id'])
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual({s.role: s.path for s in context.sources},
                         {'outcar': str(self.output/'OUTCAR'), 'oszicar': str(self.output/'OSZICAR'),
                          'poscar': str(self.inputs/'POSCAR'), 'incar': str(self.inputs/'INCAR')})
        self.assertFalse(context.process_ownership)
        self.assertEqual(self.store.path.read_bytes(), before)

    def managed_job(self, *, progress_sources=(), flags=()):
        if progress_sources:
            progress_sources = (SourceDescriptor('outcar', str(self.output / 'OUTCAR')), *progress_sources)
        return self.store.add(argv=['/not-executed/run-vasp.sh', '--input', str(self.inputs),
            '--output', str(self.output), '--managed-foreground', *flags], cwd=self.inputs,
            engine='VASP', name='synthetic managed attempt', hold=True, progress_sources=progress_sources)

    def test_managed_stdout_exact_path_and_attempt_identity(self):
        job = self.managed_job()
        context = resolve_context(self.store, job['id'], expected_attempt_id=job['attempt_id'])
        stdout = next(source for source in context.sources if source.role == 'stdout')
        self.assertEqual(stdout.path, str(self.output / 'stdout.log'))
        self.assertEqual(stdout.root_path, str(self.output))
        self.assertTrue(stdout.optional)
        self.assertEqual(stdout.origins, ('recognized-runner-argv',))
        with self.assertRaises(JobsError):
            resolve_context(self.store, job['id'], expected_attempt_id='another-attempt')
        dry = resolve_context(self.store, self.managed_job(flags=('--dry-run',))['id'])
        self.assertNotIn('stdout', {source.role for source in dry.sources})

    def test_managed_stdout_reconciles_explicit_same_or_wrong_attempt_path(self):
        same = self.managed_job(progress_sources=(SourceDescriptor('stdout', str(self.output / 'stdout.log')),))
        bound = resolve_context(self.store, same['id'])
        stdout = next(source for source in bound.sources if source.role == 'stdout')
        self.assertEqual(set(stdout.origins), {'recognized-runner-argv', 'user-declared-at-add'})
        other = self.managed_job(progress_sources=(SourceDescriptor('stdout', str(self.root / 'other-attempt/stdout.log')),))
        conflict = resolve_context(self.store, other['id'])
        self.assertEqual(conflict.binding_status, 'SOURCE_CONFLICT')
        self.assertFalse(conflict.sources)
        self.assertTrue(any('stdout: conflicting declarations' in item for item in conflict.limitations))

    def test_optional_stdout_missing_empty_or_redirected_preserves_native_evidence(self):
        texts = native_texts()
        for name in ('OUTCAR', 'OSZICAR'):
            (self.output / name).write_text(texts[name])
        (self.inputs / 'INCAR').write_text(texts['INCAR'])
        job = self.managed_job()
        service = ObservationService()
        self.addCleanup(service.close)
        stdout = self.output / 'stdout.log'
        foreign = self.root / 'wrong-attempt.log'
        foreign.write_text('DAV: 999 -9.0 -.1 -.1 12 .1 .1\n')
        for state in ('missing', 'empty', 'escape'):
            with self.subTest(state=state):
                if state == 'empty':
                    stdout.write_text('')
                elif state == 'escape':
                    stdout.unlink()
                    stdout.symlink_to(foreign)
                context = resolve_context(self.store, job['id'])
                self.assertEqual(context.binding_status, 'BOUND')
                snapshot = service.poll(context)
                self.assertTrue(snapshot.metrics)
                self.assertNotIn(snapshot.status, ('READ_ERROR', 'ADAPTER_ERROR'))
                if state == 'escape':
                    self.assertNotIn('stdout', {source.role for source in context.sources})
                    self.assertTrue(any('escapes the declared output' in item for item in context.limitations))
                    self.assertFalse(any(level.index == 999 for metric in snapshot.metrics for level in metric.latest.context))

    def test_unknown_wrapper_never_guesses_and_explicit_sources_are_session_local(self):
        job = self.job(False)
        before = self.store.path.read_bytes()
        unknown = resolve_context(self.store, job['id'])
        self.assertEqual(unknown.binding_status, 'SOURCE_UNRESOLVED')
        self.assertEqual(unknown.sources, ())
        context = resolve_context(self.store, job['id'], sources=(
            SourceDescriptor('outcar', str(self.output/'OUTCAR')),
            SourceDescriptor('stdout', str(self.root/'known stdout; literal')),))
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual(context.association, 'explicit-caller-declaration')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_unresolved_source_plans_deduplicate_root_cause_and_keep_declaration_detail(self):
        context = resolve_context(self.store, self.job(False)['id'])
        self.assertEqual(context.limitations.count('No unique declared output/stage: LOCATION_UNKNOWN'), 1)
        self.assertIn('Output location is not declared by a supported route', context.limitations)
        self.assertEqual(len(context.limitations), len(set(context.limitations)))
        self.assertIn('Location declarations do not establish process ownership or scientific validity.',
                      context.limitations)

    def test_distinct_resolver_and_source_role_errors_are_preserved_in_order(self):
        def denied(_job):
            raise PermissionError('synthetic declaration permission denied')
        def broken(_job):
            raise ValueError('synthetic layout identity conflict')
        context = resolve_context(self.store, self.job(False)['id'], resolvers=(denied, broken),
                                  descriptors=(LocationDescriptor('inputs', None,
                                               detail='synthetic input declaration unavailable'),))
        expected = ('No unique declared output/stage: LOCATION_UNKNOWN',
                    'Location resolver unavailable: synthetic declaration permission denied',
                    'Location resolver unavailable: synthetic layout identity conflict',
                    'synthetic input declaration unavailable')
        self.assertEqual(context.limitations[:4], expected)
        self.assertEqual(context.binding_status, 'SOURCE_UNRESOLVED')
        self.assertFalse(context.sources)

    def test_state_and_attempt_identity_and_no_external_adoption(self):
        job = self.job()
        other = Store(self.root/'other'); other_job = self.job(store=other)
        self.assertEqual(job['display_id'], other_job['display_id'])
        self.assertNotEqual(resolve_context(self.store, job['id']).key,
                            resolve_context(other, other_job['id']).key)
        with self.assertRaises(JobsError):
            resolve_context(self.store, job['id'], expected_attempt_id='stale')
        with self.assertRaises(JobsError):
            resolve_context(self.store, 'E1234')

    def test_ambiguous_stages_require_explicit_context(self):
        job = self.job(False)
        declarations = tuple(LocationDescriptor('output', str(self.output), stage=stage)
                             for stage in ('one', 'two'))
        result = resolve_context(self.store, job['id'], descriptors=declarations)
        self.assertEqual(result.binding_status, 'SOURCE_CONFLICT')
        self.assertFalse(result.sources)

    def test_unsupported_engine_keeps_no_default_source_plan(self):
        result = resolve_context(self.store, self.job(engine='CP2K')['id'])
        self.assertEqual(result.engine, 'CP2K')
        self.assertFalse(result.sources)

    def test_source_override_rejects_uri_relative_and_duplicate_roles_or_paths(self):
        job = self.job(False)
        for sources in ((SourceDescriptor('stdout', 'relative'),),
                        (SourceDescriptor('stdout', 'file:///tmp/x'),),
                        (SourceDescriptor('stdout', '/x'), SourceDescriptor('stdout', '/y')),
                        (SourceDescriptor('outcar', '/x'), SourceDescriptor('stdout', '/x'))):
            with self.subTest(sources=sources), self.assertRaises(JobsError):
                resolve_context(self.store, job['id'], sources=sources)

    def test_replaced_declared_directory_changes_binding(self):
        job = self.job()
        before = resolve_context(self.store, job['id'])
        self.output.rename(self.root/'old-output')
        self.output.mkdir()
        self.assertNotEqual(before.key, resolve_context(self.store, job['id']).key)


class ComparisonTests(unittest.TestCase):
    def test_relevant_context_definition_criterion_and_constraint_scope_must_agree(self):
        evaluation = IterationLevel('evaluation', 2, 'evaluation')
        previous = Metric('fictional.residual', 'Residual', 'native residual', 'kPa', 4.,
                          context=(evaluation, IterationLevel('inner', 1, 'inner')),
                          comparison_context=(evaluation,))
        latest = replace(previous, value=3., context=(evaluation, IterationLevel('inner', 2, 'inner')))
        self.assertTrue(comparable(latest, previous))
        self.assertEqual(MetricSeries(latest, previous).previous, previous)
        for altered in (replace(latest, comparison_context=(replace(evaluation, index=3),)),
                        replace(latest, unit='eV'), replace(latest, segment=2),
                        replace(latest, stage='new'), replace(latest, definition='different norm'),
                        replace(latest, comparison_scope='changed flags'),
                        replace(latest, criterion=Criterion(1., 'kPa', '<', 'caller', 'diagnostic'))):
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                MetricSeries(altered, previous)

    def test_source_replacement_cannot_inherit_previous_pair(self):
        evidence = Evidence('native', '/explicit/source', 1, 1, 1, 0, 10, (1, 2))
        previous = Metric('toy.value', 'Value', 'Native value', 'Debye', 1., evidence=(evidence,))
        latest = replace(previous, value=2., evidence=(replace(evidence, line_start=2, byte_start=10, byte_end=20),))
        self.assertTrue(comparable(latest, previous))
        for source in (replace(evidence, generation=2), replace(evidence, path='/different'),
                       replace(evidence, file_identity=(1, 3))):
            with self.subTest(source=source), self.assertRaises(ValueError):
                MetricSeries(replace(latest, evidence=(source,)), previous)
