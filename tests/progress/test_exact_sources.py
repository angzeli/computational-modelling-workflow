"""Exact declared source plans, without engine execution or output discovery."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.locations import LocationDescriptor, discover
from cmw.jobs.progress import resolve_context
from cmw.jobs.store import Store
from cmw.progress.models import SourceDescriptor
from cmw.progress.registry import Registration, SourcePlan


class ExactSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='cmw exact sources ')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.inputs = self.root / 'inputs'
        self.output = self.root / 'declared output'
        self.inputs.mkdir(); self.output.mkdir()
        self.store = Store(self.root / 'jobs')
        # Use the generic source plan independently of the ORCA parser registry.
        registration = Registration(lambda context: None,
                                    (SourcePlan('orca_output', None, 'orca-output', stages=True),))
        mock = patch('cmw.jobs.progress.get_registration', return_value=registration)
        mock.start(); self.addCleanup(mock.stop)

    def job(self, argv):
        return self.store.add(argv=argv, cwd=self.inputs, engine='ORCA', name='Synthetic', hold=True)

    def runner(self, stage='OPT', extra=()):
        target = self.inputs / 'target.json'
        target.write_text(json.dumps({'schema_version': 1, 'target': {'stage_type': stage}}))
        return self.job(['/synthetic/run_orca.sh', '--input', str(self.inputs/'stage.inp'),
                         '--target', str(target), '--metadata', str(self.output/'job.json'),
                         '--output', str(self.output/'declared.out'),
                         '--stderr', str(self.output/'stage.err'), *extra])

    def workflow(self):
        state = {'schema_version': 1, 'workflow': 'opt_freq_sp', 'workflow_target_id': 'workflow-target',
                 'output_root': str(self.output), 'stage_order': ['OPT', 'FREQ', 'SP'], 'stages': {}}
        for stage in state['stage_order']:
            folder = self.output / stage
            folder.mkdir()
            state['stages'][stage] = {'selected': True, 'target_id': 'target-'+stage,
                'attempt_id': 'attempt_001', 'runner': {'stage': stage, 'target_id': 'target-'+stage,
                'attempt_id': 'attempt_001', 'output': str(folder/'native.out')}}
        path = self.root/'workflow_state.json'
        path.write_text(json.dumps(state))
        job = self.job(['/synthetic/python3', '-m', 'cmw.molecular.workflows.cli', 'next', '--state', str(path)])
        return job, state, path

    def test_runner_exact_output_is_not_artifact_stdout_cwd_or_newest(self):
        job = self.runner(extra=('--artifact', 'wavefunction='+str(self.output/'stage.gbw')))
        (self.output/'newer.out').write_text('irrelevant')
        before = self.store.path.read_bytes()
        with patch.object(Path, 'glob', side_effect=AssertionError('no scans')), \
             patch.object(Path, 'rglob', side_effect=AssertionError('no scans')), \
             patch('subprocess.run', side_effect=AssertionError('no execution')):
            context = resolve_context(self.store, job['id'])
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual([(item.role, item.path, item.stage) for item in context.sources],
                         [('orca_output', str(self.output/'declared.out'), 'OPT')])
        self.assertEqual(context.stage, 'OPT')
        self.assertEqual(context.sources[0].root_path, str(self.output))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(context.process_ownership)

    def test_unknown_target_stage_does_not_invent_a_stage(self):
        job = self.runner()
        (self.inputs/'target.json').write_text('{incomplete')
        context = resolve_context(self.store, job['id'])
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertIsNone(context.stage)
        self.assertIsNone(context.sources[0].stage)

    def test_workflow_exact_sources_keep_declared_stage_order_despite_ambiguous_folders(self):
        job, _state, _path = self.workflow()
        self.assertEqual(discover(self.store, job['id']).status, 'AMBIGUOUS')
        context = resolve_context(self.store, job['id'])
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertIsNone(context.stage)
        self.assertEqual([(s.role, s.stage) for s in context.sources],
                         [('orca_output:OPT', 'OPT'), ('orca_output:FREQ', 'FREQ'), ('orca_output:SP', 'SP')])
        self.assertEqual([s.path for s in context.sources],
                         [str(self.output/stage/'native.out') for stage in ('OPT', 'FREQ', 'SP')])

    def test_unprepared_future_stages_do_not_get_invented_output_paths(self):
        job, state, path = self.workflow()
        for stage in ('FREQ', 'SP'):
            state['stages'][stage].pop('runner')
        path.write_text(json.dumps(state))
        context = resolve_context(self.store, job['id'])
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual([source.stage for source in context.sources], ['OPT'])

    def test_recorded_artifact_output_is_exact_source_without_finalization(self):
        job, state, path = self.workflow()
        record = state['stages']['OPT']
        runner = record.pop('runner')
        record['artifacts'] = {'output': {'path': runner['output']}}
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        context = resolve_context(self.store, job['id'])
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual(context.sources[0].path, runner['output'])
        self.assertEqual(path.read_bytes(), before)

    def test_mismatched_runner_identity_blocks_even_if_other_stages_are_valid(self):
        job, state, path = self.workflow()
        state['stages']['OPT']['runner']['attempt_id'] = 'other-attempt'
        path.write_text(json.dumps(state))
        context = resolve_context(self.store, job['id'])
        self.assertEqual(context.binding_status, 'SOURCE_UNRESOLVED')
        self.assertFalse(context.sources)
        self.assertIn('identity is inconsistent', ' '.join(context.limitations))

    def test_missing_recorded_runner_output_and_relative_paths_fail_closed(self):
        job, state, path = self.workflow()
        for value in (None, 'relative.out'):
            with self.subTest(value=value):
                state['stages']['OPT']['runner']['output'] = value
                path.write_text(json.dumps(state))
                context = resolve_context(self.store, job['id'])
                self.assertEqual(context.binding_status, 'SOURCE_UNRESOLVED')
                self.assertFalse(context.sources)

    def test_two_stages_cannot_share_one_source_file(self):
        job, state, path = self.workflow()
        state['stages']['FREQ']['runner']['output'] = state['stages']['OPT']['runner']['output']
        path.write_text(json.dumps(state))
        self.assertEqual(resolve_context(self.store, job['id']).binding_status, 'SOURCE_CONFLICT')

    def test_two_files_for_one_stage_are_ambiguous(self):
        job = self.runner()
        extra = (LocationDescriptor('orca-output', self.output/'other.out', kind='file', stage='OPT'),)
        self.assertEqual(resolve_context(self.store, job['id'], descriptors=extra).binding_status, 'SOURCE_CONFLICT')

    def test_explicit_source_selects_only_its_matching_stage_and_conflict_rejected(self):
        job, _state, _path = self.workflow()
        source = SourceDescriptor('orca_output', str(self.output/'FREQ'/'native.out'))
        context = resolve_context(self.store, job['id'], sources=(source,))
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual(context.stage, 'FREQ')
        self.assertEqual(context.sources[0].stage, 'FREQ')
        conflict = resolve_context(self.store, job['id'], sources=(replace(source, stage='OPT'),))
        self.assertEqual(conflict.binding_status, 'SOURCE_UNRESOLVED')

    def test_explicit_unknown_sources_keep_caller_stage_order(self):
        job = self.job(['/synthetic/opaque'])
        sources = tuple(SourceDescriptor('orca_output:'+stage, str(self.output/(stage+'.out')), stage=stage)
                        for stage in ('OPT', 'FREQ', 'SP'))
        context = resolve_context(self.store, job['id'], sources=sources)
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual([(s.role, s.path, s.stage) for s in context.sources],
                         [(s.role, s.path, s.stage) for s in sources])
        self.assertTrue(all(s.origins == ('invocation-local',) for s in context.sources))
        self.assertIsNone(context.stage)

    def test_external_scratch_copyback_is_not_misrepresented_as_live_output(self):
        job = self.runner(extra=('--external-scratch-root', str(self.root/'scratch')))
        context = resolve_context(self.store, job['id'])
        self.assertEqual(context.binding_status, 'SOURCE_UNRESOLVED')
        self.assertIn('copyback destination', ' '.join(context.limitations))
        self.assertTrue(any(item.role == 'output' and item.path == str(self.output/'declared.out')
                            for item in discover(self.store, job['id']).locations))
        explicit = SourceDescriptor('orca_output', str(self.root/'exact-execution.out'), stage='OPT')
        self.assertEqual(resolve_context(self.store, job['id'], sources=(explicit,)).binding_status, 'BOUND')

    def test_workflow_root_without_exact_record_does_not_trigger_search(self):
        job = self.job(['/synthetic/run_opt_freq_sp.sh', '--mode', 'opt+freq+sp',
            '--structure', str(self.inputs/'molecule.xyz'), '--config', str(self.inputs/'config.json'),
            '--output', str(self.output)])
        with patch.object(Path, 'glob', side_effect=AssertionError('no scans')), \
             patch.object(Path, 'rglob', side_effect=AssertionError('no scans')):
            context = resolve_context(self.store, job['id'])
        self.assertEqual(context.binding_status, 'SOURCE_UNRESOLVED')
        self.assertFalse(context.sources)

    def test_fictional_adapter_reuses_exact_source_stages_without_engine_branch(self):
        registration = Registration(lambda context: None,
            (SourcePlan('native', None, 'fictional-text', stages=True),))
        job = self.job(['/synthetic/opaque'])
        descriptors = tuple(LocationDescriptor('fictional-text', self.output/(stage+'.txt'),
                                              kind='file', stage=stage) for stage in ('heat', 'cool'))
        with patch('cmw.jobs.progress.get_registration', return_value=registration):
            context = resolve_context(self.store, job['id'], descriptors=descriptors, engine='FICTIONAL')
        self.assertEqual(context.binding_status, 'BOUND')
        self.assertEqual([(s.role, s.stage) for s in context.sources], [('native:heat', 'heat'), ('native:cool', 'cool')])
        self.assertEqual(context.engine, 'FICTIONAL')

    def test_exact_sources_are_bounded_and_directory_is_not_an_exact_file(self):
        job = self.job(['/synthetic/opaque'])
        for descriptors in (
            (LocationDescriptor('orca-output', self.output, stage='OPT'),),
            tuple(LocationDescriptor('orca-output', self.output/f'{index}.out', kind='file', stage=str(index))
                  for index in range(9)),
        ):
            with self.subTest(descriptors=descriptors):
                self.assertEqual(resolve_context(self.store, job['id'], descriptors=descriptors).binding_status,
                                 'SOURCE_UNRESOLVED')
        sources = tuple(SourceDescriptor(str(index), str(self.output/f'{index}.out')) for index in range(9))
        self.assertEqual(resolve_context(self.store, job['id'], sources=sources).binding_status, 'SOURCE_UNRESOLVED')

    def test_exact_file_replacement_changes_immutable_binding(self):
        job = self.runner()
        output = self.output/'declared.out'
        output.write_text('old')
        before = resolve_context(self.store, job['id'])
        output.rename(self.output/'old.out')
        output.write_text('replacement')
        self.assertNotEqual(before.key, resolve_context(self.store, job['id']).key)


if __name__ == '__main__':
    unittest.main()
