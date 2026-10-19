from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.location_resolvers import (
    builtin_resolvers, orca_runner_locations, orca_workflow_locations,
    orca_workflow_state_locations, vasp_runner_locations,
)
from cmw.jobs.locations import discover
from cmw.jobs.store import Store


class RunnerLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='locations 繁體 ; $literal ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inputs = self.root / 'prepared inputs'
        self.inputs.mkdir()
        self.output = self.root / 'run output $(do not execute)'
        self.output.mkdir()
        self.store = Store(self.root / 'state')

    def job(self, argv, **kwargs):
        return self.store.add(argv=argv, cwd=self.inputs, name='label is not a path',
                              engine=kwargs.pop('engine', 'Synthetic'), hold=True, **kwargs)

    def vasp_argv(self, *extra):
        return ['/synthetic/run-vasp.sh', '--input', str(self.inputs),
                '--output', str(self.output), *extra]

    def orca_argv(self, *extra):
        target = self.inputs / 'target.json'
        target.write_text(json.dumps({'schema_version': 1, 'target': {'stage_type': 'SP'}}))
        return ['/synthetic/run_orca.sh', '--input', str(self.inputs / 'stage.inp'),
                '--target', str(target), '--metadata', str(self.output / 'job.json'),
                '--output', str(self.output / 'stage.out'),
                '--stderr', str(self.output / 'stage.err'), *extra]

    def test_vasp_output_is_separate_from_cwd_without_receipt(self):
        job = self.job(self.vasp_argv('--managed-foreground'))
        before = self.store.path.read_bytes()
        with patch('subprocess.run', side_effect=AssertionError('discovery must not execute')):
            result = discover(self.store, job['id'])
        self.assertEqual(self.store.path.read_bytes(), before)
        output = next(item for item in result.locations if item.role == 'output')
        self.assertEqual(output.path, str(self.output))
        self.assertEqual(output.origin, 'recognized-runner-argv')
        self.assertFalse(output.process_ownership)
        self.assertEqual(output.scientific_validation, 'not-assessed')
        self.assertEqual(result.preferred_key, output.key)
        self.assertEqual(next(item.path for item in result.locations if item.role == 'working-directory'), str(self.inputs))

    def test_navigation_does_not_apply_finalization_mode_restrictions(self):
        job = self.job(self.vasp_argv('--binary', 'gam', '--restart', 'wavecar',
                                      '--restart-from', str(self.inputs), '--ranks', '16'))
        self.assertEqual(vasp_runner_locations(job)[1].path, self.output)

    def test_shell_injected_options_are_recognized_and_no_shell_evaluation(self):
        job = self.job(['/bin/bash', *self.vasp_argv()])
        self.assertEqual(job['argv'][1:4], ['-e', '-o', 'pipefail'])
        self.assertEqual(vasp_runner_locations(job)[1].path, self.output)
        opaque = self.job(['/bin/bash', '-c', 'run-vasp.sh --input /x --output /y'])
        self.assertEqual(vasp_runner_locations(opaque), ())
        result = discover(self.store, opaque['id'])
        self.assertIsNone(result.preferred_key)
        self.assertTrue(any(item.role == 'output' and item.accessibility == 'LOCATION_UNKNOWN'
                            for item in result.locations))

    def test_unknown_named_wrapper_and_notes_do_not_supply_paths(self):
        job = self.job(['/synthetic/private-wrapper.sh', '--output', str(self.output)],
                       engine='VASP', note=str(self.output))
        result = discover(self.store, job['id'])
        self.assertIsNone(result.preferred_key)
        self.assertFalse(any(item.role == 'output' and item.path for item in result.locations))
        self.assertTrue(any(item.role == 'operational-logs' for item in result.locations))

    def test_ambiguous_or_unsupported_vasp_argv_stays_unknown(self):
        for suffix in (['--output', '/different'], ['--opaque', 'yes'], ['--input']):
            with self.subTest(suffix=suffix):
                locations = vasp_runner_locations({'cwd': str(self.inputs), 'argv': self.vasp_argv(*suffix)})
                self.assertEqual(len(locations), 1)
                self.assertIsNone(locations[0].path)

    def test_current_controlled_runner_grammar_retains_exact_output(self):
        for budget in (['--execution-budget', '3600'], ['--execution-budget=3600']):
            with self.subTest(budget=budget):
                job = self.job(self.vasp_argv('--managed-foreground',
                    '--controlled-suspension', *budget, '--timeout', '7200',
                    '--ranks', '8', '--mpi-mode', 'native'))
                result = discover(self.store, job['id'])
                output = next(item for item in result.locations if item.role == 'output')
                self.assertEqual(output.path, str(self.output))
                self.assertEqual(result.preferred_key, output.key)
                self.assertEqual(output.qualification, 'declared')
                self.assertFalse(output.process_ownership)

    def test_new_options_do_not_relax_strict_argument_shape(self):
        valid = ['--managed-foreground', '--controlled-suspension',
                 '--execution-budget', '3600']
        malformed = (
            ['--controlled-suspension=true'],
            ['--execution-budget'], ['--execution-budget='],
            ['--execution-budget', '--timeout', '7200'],
            [*valid, '--controlled-suspension'],
            [*valid, '--execution-budget=7200'],
            [*valid, '--managed-foreground'],
            [*valid, '--unknown-runner-option'],
        )
        for suffix in malformed:
            with self.subTest(suffix=suffix):
                result = vasp_runner_locations({'cwd': str(self.inputs),
                                                'argv': self.vasp_argv(*suffix)})
                self.assertEqual(len(result), 1)
                self.assertIsNone(result[0].path)
                self.assertIn('Unresolved run-vasp.sh declaration', result[0].detail)
        missing_output = {'cwd': str(self.inputs), 'argv': [
            '/synthetic/run-vasp.sh', '--input', str(self.inputs), *valid]}
        self.assertIsNone(vasp_runner_locations(missing_output)[0].path)

    def test_nested_shell_stays_opaque_even_beside_native_output(self):
        (self.inputs / 'OUTCAR').write_text('synthetic adjacent output\n')
        (self.output / 'OUTCAR').write_text('synthetic other attempt output\n')
        for argv in (
            ['/bin/bash', '-c', 'exec /synthetic/run-vasp.sh --output .'],
            ['/bin/bash', '/bin/sh', '-c', 'exec /synthetic/run-vasp.sh --output .'],
        ):
            with self.subTest(argv=argv):
                job = self.job(argv, engine='VASP')
                from cmw.jobs.progress import resolve_context
                context = resolve_context(self.store, job['id'])
                self.assertEqual(context.binding_status, 'SOURCE_UNRESOLVED')
                self.assertFalse(context.sources)
                self.assertIsNone(discover(self.store, job['id']).preferred_key)

    def test_similar_named_vasp_attempts_keep_their_declared_sources(self):
        from cmw.jobs.progress import resolve_context
        contexts = []
        for index in range(3):
            output = self.root / f'similar-run-{index}'
            output.mkdir()
            (output / 'OUTCAR').write_text('synthetic per-attempt output\n')
            job = self.store.add(argv=['/synthetic/run-vasp.sh',
                '--input', str(self.inputs), '--output', str(output)],
                cwd=self.inputs, name='same human label', engine='VASP', hold=True)
            context = resolve_context(self.store, job['id'], expected_attempt_id=job['attempt_id'])
            self.assertEqual({source.role: source.path for source in context.sources}, {
                'outcar': str(output / 'OUTCAR'), 'oszicar': str(output / 'OSZICAR'),
                'poscar': str(self.inputs / 'POSCAR'), 'incar': str(self.inputs / 'INCAR')})
            self.assertEqual(context.attempt_id, job['attempt_id'])
            contexts.append(context)
        self.assertEqual(len({context.key for context in contexts}), 3)

    def test_equal_syntax_relative_literal_paths_and_repeated_extra_inputs(self):
        job = self.job(['/synthetic/run-vasp.sh', '--input=.', '--output=../繁體 結果;echo literal',
                        '--extra-input', 'alpha', '--extra-input', 'beta'])
        self.assertEqual(vasp_runner_locations(job)[1].path,
                         self.inputs / '../繁體 結果;echo literal')

    def test_vasp_symlink_target_is_shown_by_generic_contract(self):
        link = self.root / 'output link'
        link.symlink_to(self.output, target_is_directory=True)
        argv = self.vasp_argv()
        argv[-1] = str(link)
        result = discover(self.store, self.job(argv)['id'])
        output = next(item for item in result.locations if item.role == 'output')
        self.assertEqual(output.path, str(self.output))
        self.assertEqual(output.declared_path, str(link))

    def test_orca_explicit_source_paths_have_stage_and_record_roles(self):
        job = self.job(['/bin/bash', *self.orca_argv('--artifact', str('wavefunction=') + str(self.output / 'stage.gbw'))])
        locations = orca_runner_locations(job)
        output = next(item for item in locations if item.role == 'output')
        self.assertEqual(output.kind, 'file')
        self.assertEqual(output.path, self.output / 'stage.out')
        self.assertEqual({item.stage for item in locations}, {'SP'})
        self.assertIn(self.output / 'job.json', [item.path for item in locations if item.role == 'records'])
        self.assertEqual(len([item for item in locations if item.role == 'output']), 2)

    def test_orca_bad_target_metadata_does_not_hide_declared_output(self):
        argv = self.orca_argv()
        for content in ('{broken', *(json.dumps({'schema_version': 1, 'target': {'stage_type': value}})
                                    for value in ('', '   ', 'x' * 257, 'SP\x00'))):
            with self.subTest(content=content):
                (self.inputs / 'target.json').write_text(content)
                locations = orca_runner_locations(self.job(argv))
                self.assertEqual(next(item for item in locations if item.role == 'output').path, self.output / 'stage.out')
                self.assertEqual({item.stage for item in locations}, {None})

    def test_orca_missing_required_or_duplicate_paths_stays_unknown(self):
        for argv in (self.orca_argv()[:-2], self.orca_argv('--output', '/another')):
            with self.subTest(argv=argv):
                output = orca_runner_locations({'argv': argv, 'cwd': str(self.inputs)})[0]
                self.assertIsNone(output.path)

    def test_workflow_root_is_not_mislabelled_as_actual_stage_output(self):
        argv = ['/synthetic/run_opt_freq_sp.sh', '--mode', 'opt+freq+sp',
                '--structure', str(self.inputs / 'molecule.xyz'), '--config', str(self.inputs / 'config.json'),
                '--output', str(self.output)]
        job = self.job(argv)
        locations = orca_workflow_locations(job)
        self.assertEqual({item.stage for item in locations if item.role == 'output'}, {'OPT', 'FREQ', 'SP'})
        self.assertTrue(all(item.path is None for item in locations if item.role == 'output'))
        self.assertEqual(next(item.path for item in locations if item.role == 'workflow-root'), self.output)
        self.assertIsNone(discover(self.store, job['id']).preferred_key)

    def workflow_state(self):
        state = {'schema_version': 1, 'workflow': 'opt_freq_sp', 'workflow_target_id': 'scientific-target',
                 'output_root': str(self.output), 'result_path': str(self.output / 'workflow_result.json'),
                 'stage_order': ['OPT', 'SP'], 'stages': {}}
        for stage in state['stage_order']:
            folder = self.output / stage
            folder.mkdir()
            state['stages'][stage] = {'selected': True, 'target_id': 'target-' + stage,
                'attempt_id': 'attempt_001', 'runner': {'stage': stage, 'target_id': 'target-' + stage,
                    'attempt_id': 'attempt_001', 'input': str(self.inputs / (stage + '.inp')),
                    'output': str(folder / 'stage.out'), 'metadata': str(folder / 'job.json')}}
            (folder / 'stage.out').write_text('synthetic source; no scientific parsing')
        path = self.root / 'explicit workflow_state.json'
        path.write_text(json.dumps(state))
        argv = ['/synthetic/python3', '-B', '-m', 'cmw.molecular.workflows.cli', 'next', '--state', str(path)]
        return state, path, self.job(argv)

    def test_explicit_workflow_state_retains_multiple_stage_locations(self):
        _state, _path, job = self.workflow_state()
        result = discover(self.store, job['id'])
        outputs = [item for item in result.locations if item.role == 'output']
        self.assertEqual({item.stage for item in outputs}, {'OPT', 'SP'})
        self.assertEqual({item.origin for item in outputs}, {'workflow-metadata'})
        self.assertIsNone(result.preferred_key)
        self.assertEqual(result.status, 'AMBIGUOUS')

    def test_workflow_state_rejects_mismatched_runner_identity(self):
        state, path, job = self.workflow_state()
        state['stages']['OPT']['runner']['attempt_id'] = 'attempt_002'
        path.write_text(json.dumps(state))
        outputs = [item for item in orca_workflow_state_locations(job) if item.role == 'output']
        self.assertIsNone(next(item.path for item in outputs if item.stage == 'OPT'))
        self.assertEqual(next(item.path for item in outputs if item.stage == 'SP'), self.output / 'SP' / 'stage.out')

    def test_recorded_completed_stage_artifact_needs_no_receipt_or_validation(self):
        state, path, job = self.workflow_state()
        opt = state['stages']['OPT']
        runner = opt.pop('runner')
        opt.update(artifacts={'output': {'path': runner['output']}}, metadata=runner['metadata'])
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        outputs = [item for item in orca_workflow_state_locations(job) if item.role == 'output']
        self.assertEqual(next(item.path for item in outputs if item.stage == 'OPT'), self.output / 'OPT' / 'stage.out')
        self.assertEqual(path.read_bytes(), before)

    def test_metadata_reader_is_bounded_and_does_not_scan_output(self):
        _state, path, job = self.workflow_state()
        path.write_text(' ' * (1024 * 1024 + 1))
        with patch.object(Path, 'glob', side_effect=AssertionError('no output scans')), \
             patch.object(Path, 'rglob', side_effect=AssertionError('no research scans')):
            locations = orca_workflow_state_locations(job)
        self.assertTrue(any(item.path is None and 'exceeds' in item.detail for item in locations))

    def test_uri_runner_path_is_rejected_before_path_normalization(self):
        argv = self.vasp_argv()
        argv[-1] = 'file:///Applications/Example.app'
        result = vasp_runner_locations({'argv': argv, 'cwd': str(self.inputs)})
        self.assertIsNone(result[0].path)
        self.assertIn('URI', result[0].detail)

    def test_relative_workflow_metadata_is_not_bound_to_jobs_cwd(self):
        state, path, job = self.workflow_state()
        state['stages']['OPT']['runner']['output'] = 'unbound/stage.out'
        path.write_text(json.dumps(state))
        locations = orca_workflow_state_locations(job)
        self.assertFalse(any(item.role == 'output' and item.path for item in locations))
        self.assertTrue(any('absolute' in item.detail for item in locations))

    def test_resolvers_do_not_mutate_mapping_and_are_explicitly_registered(self):
        job = self.job(self.vasp_argv())
        original = copy.deepcopy(job)
        for resolver in builtin_resolvers():
            tuple(resolver(job))
        self.assertEqual(job, original)
        self.assertEqual(builtin_resolvers(), (vasp_runner_locations, orca_runner_locations,
                         orca_workflow_locations, orca_workflow_state_locations))


if __name__ == '__main__':
    unittest.main()
