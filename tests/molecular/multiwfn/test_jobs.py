import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import psutil
from unittest.mock import patch

from cmw.molecular.multiwfn.jobs import check, prepare, verify_prepared
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.service import ObservationService
from tests.molecular.multiwfn.jobs_fixtures import make_spec


class JobsProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw-firstclass-multiwfn-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def prepared(self, profile):
        spec = make_spec(self.root / profile, profile)
        with patch('subprocess.run', side_effect=AssertionError('Pure preflight must not launch')):
            checked = check(spec)
        self.assertEqual(checked['status'], 'READY', checked)
        Path(spec['workdir']).mkdir()
        plan = prepare(spec, checked)
        return spec, plan

    def test_all_profiles_preflight_and_exact_preparation(self):
        for profile in ('FMO', 'ESP', 'IGMH', 'IFCT'):
            with self.subTest(profile=profile):
                spec, plan = self.prepared(profile)
                self.assertEqual(plan['engine'], 'Multiwfn')
                self.assertEqual(len(plan['progress_sources']), 4)
                association = json.loads((Path(plan['attempt_directory']) / 'jobs-association.json').read_text())
                self.assertEqual(association['schema'], 2)
                self.assertEqual(association['request_id'], spec['request_id'])
                self.assertEqual(association['threads'], spec['resources']['cpus'])
                if profile in {'IGMH', 'IFCT'}:
                    self.assertEqual(association['fragment_labels'], ['A', 'B'])
                    self.assertEqual(association['fragment_counts'], [3, 2])
                if profile == 'IFCT':
                    self.assertEqual(association['spin_manifold'], 'singlet')
                    self.assertEqual(association['state_index'], 1)
                self.assertFalse(Path(plan['progress_sources'][0]['path']).exists())
                self.assertEqual(Path(spec['runtime']['settings_source']).read_text(), 'nthreads= 8\n')
                self.assertIn('nthreads= 2', Path(plan['runtime']['settings_path']).read_text())
                self.assertFalse(plan['suspension']['eligible'])
                verify_prepared(Path(plan['prepared_path']))

    def test_all_profiles_pending_and_unsupported_native_card(self):
        for profile in ('FMO', 'ESP', 'IGMH', 'IFCT'):
            with self.subTest(profile=profile):
                _, plan = self.prepared(profile)
                context = RunContext(f'pending-{profile}', 'Multiwfn',
                    tuple(SourceDescriptor(**source) for source in plan['progress_sources']),
                    stage=profile, attempt_id=f'jobs-{profile}')
                service = ObservationService()
                self.addCleanup(service.close)
                pending = service.poll(context)
                self.assertEqual(pending.scientific_validation, 'not-assessed')
                self.assertEqual([group.label for group in pending.card_groups],
                    ['ANALYSIS', 'STAGE', 'OUTPUT'])
                Path(plan['native_log']).write_text('Version 9.9\n')
                unsupported = service.poll(context)
                # A known prepared version makes the unknown dialect a source conflict.
                self.assertEqual(unsupported.status, 'SOURCE_CONFLICT')
                self.assertTrue(any('Unsupported native Multiwfn version' in message
                    for message in unsupported.messages))
                self.assertEqual(unsupported.scientific_validation, 'not-assessed')
                self.assertFalse(unsupported.metrics)
                self.assertEqual([group.label for group in unsupported.card_groups],
                    ['ANALYSIS', 'STAGE', 'OUTPUT'])
                explicit = RunContext(f'explicit-{profile}', 'Multiwfn',
                    (SourceDescriptor('multiwfn_session', plan['native_log'], stage=profile),),
                    stage=profile, attempt_id=f'explicit-jobs-{profile}')
                unsupported_only = service.poll(explicit)
                self.assertEqual(unsupported_only.status, 'UNSUPPORTED')
                self.assertEqual(unsupported_only.scientific_validation, 'not-assessed')
                self.assertEqual([group.label for group in unsupported_only.card_groups],
                    ['ANALYSIS', 'STAGE', 'OUTPUT'])

    def test_real_wrappers_and_validators_all_four_profiles(self):
        for profile in ('FMO', 'ESP', 'IGMH', 'IFCT'):
            with self.subTest(profile=profile):
                spec, plan = self.prepared(profile)
                context = RunContext(f'binding-{profile}', 'Multiwfn',
                    tuple(SourceDescriptor(**s) for s in plan['progress_sources']), stage=profile,
                    attempt_id=f'jobs-{profile}')
                service = ObservationService(); self.addCleanup(service.close)
                before = service.poll(context)
                self.assertEqual(before.scientific_validation, 'not-assessed')
                self.assertEqual([group.label for group in before.card_groups], ['ANALYSIS', 'STAGE', 'OUTPUT'])
                result = subprocess.run(plan['argv'], cwd=plan['cwd'], env={**os.environ, **plan['env'], 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                after = service.poll(context)
                self.assertEqual(after.scientific_validation, 'validator-recorded-passed', after)
                self.assertTrue(Path(plan['result_path']).is_file())
                records = [json.loads(line) for line in (Path(plan['attempt_directory']) / 'jobs-events.jsonl').read_text().splitlines()]
                self.assertEqual([record['sequence'] for record in records], list(range(1, 7)))
                self.assertEqual([record['kind'] for record in records], ['prepared', 'launch_requested', 'child_launched', 'process_exited', 'validation_started', 'validation_passed'])
                child = next(record for record in records if record['kind'] == 'child_launched')
                self.assertFalse(psutil.pid_exists(int(child['value'])), 'Synthetic engine child must drain before wrapper exit')
                with self.assertRaisesRegex(ValueError, 'already executed'):
                    verify_prepared(Path(plan['prepared_path']))
                history = service.history(context)
                self.assertTrue(history.entries)
                self.assertTrue(any(event.kind == 'validator' for event in after.events))

    def test_jobs_owned_registration_execution_and_automatic_sources(self):
        from cmw.jobs.molecular import submit
        from cmw.jobs.store import Store
        from cmw.jobs.runtime import start, stop, reap_detached
        from cmw.jobs.progress import resolve_context
        from tests.jobs.isolated_runtime import install
        from tests.jobs.test_jobs import wait_for
        with install():
            for profile in ('FMO', 'ESP', 'IGMH', 'IFCT'):
                with self.subTest(profile=profile):
                    spec = make_spec(self.root / ('managed-' + profile), profile)
                    store = Store(self.root / ('queue-' + profile))
                    registration = submit(store, spec)
                    self.assertEqual(registration['status'], 'REGISTERED', registration)
                    job = registration['job']
                    self.assertEqual(job['status'], 'Hold')
                    self.assertEqual(job['engine'], 'Multiwfn')
                    context = resolve_context(store, job['id'])
                    self.assertEqual(context.binding_status, 'BOUND', context)
                    self.assertTrue(any(s.role == 'multiwfn_session' for s in context.sources))
                    self.assertEqual(submit(store, spec)['job']['attempt_id'], job['attempt_id'])
                    service = ObservationService()
                    try:
                        self.assertEqual(service.poll(context).scientific_validation, 'not-assessed')
                        store.change(job['id'], 'release')
                        start(store)
                        native = Path(next(s.path for s in context.sources if s.role == 'multiwfn_session'))
                        wait_for(lambda: native.exists() and 'Version ' in native.read_text(), timeout=20)
                        live = service.poll(context)
                        self.assertTrue(any(event.kind == 'native' for event in live.events))
                        self.assertEqual(live.scientific_validation, 'not-assessed')
                        final = wait_for(lambda: next((j for j in store.snapshot()['jobs'] if j['status'] in {'Done', 'Fail'}), None), timeout=30)
                        self.assertEqual(final['status'], 'Done', Path(final['logs']['stderr']).read_text())
                        snapshot = service.poll(context)
                        self.assertEqual(snapshot.scientific_validation, 'validator-recorded-passed', snapshot)
                    finally:
                        service.close()
                        stop(store)
                        wait_for(lambda: not store.snapshot()['controller']['online'])
                        reap_detached()

    def test_negative_scope_resources_provenance(self):
        spec = make_spec(self.root / 'bad')
        cases = []
        for field, value in [('profile', 'NTO'), ('grid_spacing_bohr', 0)]:
            bad = copy.deepcopy(spec); bad['scientific'][field] = value; cases.append(bad)
        bad = copy.deepcopy(spec); bad['runtime']['version'] = '9.9'; cases.append(bad)
        bad = copy.deepcopy(spec); bad['resources']['cpus'] = 1; cases.append(bad)
        bad = copy.deepcopy(spec); bad['runtime']['threads'] = None; cases.append(bad)
        bad = copy.deepcopy(spec); bad['runtime']['qualification'] = None; cases.append(bad)
        bad = copy.deepcopy(spec); bad['resources']['memory_gib'] = 0; cases.append(bad)
        for candidate in cases:
            self.assertNotEqual(check(candidate)['status'], 'READY')
        source_path = Path(spec['scientific']['source_result'])
        source = json.loads(source_path.read_text()); source.pop('attempt'); source_path.write_text(json.dumps(source))
        self.assertIn('attempt', check(spec)['findings'][-1]['message'])

    def test_ifct_negative_state_fragments_geometry_and_donor(self):
        spec = make_spec(self.root / 'ifct-negative', 'IFCT')
        for key, value in [('atom_count', 4), ('state_index', 999), ('fragments', [{'label': 'A', 'indices_zero_based': [0, 1, 3]}, {'label': 'B', 'indices_zero_based': [0, 2, 4]}])]:
            bad = copy.deepcopy(spec); bad['scientific'][key] = value
            self.assertNotEqual(check(bad)['status'], 'READY')
        for value in (0.9, True, '0'):
            bad = copy.deepcopy(spec)
            bad['scientific']['fragments'][0]['indices_zero_based'][0] = value
            self.assertIn('exact nonnegative integers', check(bad)['findings'][-1]['message'])
        for value in (True, 1.0, '1'):
            bad = copy.deepcopy(spec); bad['scientific']['state_index'] = value
            self.assertIn('exact positive integer', check(bad)['findings'][-1]['message'])
        copied = self.root / 'other.out'; copied.write_bytes(Path(spec['scientific']['orca_output']).read_bytes())
        spec['scientific']['orca_output'] = str(copied)
        self.assertIn('authoritative', check(spec)['findings'][-1]['message'])

    def test_source_mutation_after_preparation_rejected(self):
        spec, plan = self.prepared('ESP')
        Path(plan['source_wavefunction']).write_text('changed source')
        with self.assertRaisesRegex(ValueError, 'dependency changed'):
            verify_prepared(Path(plan['prepared_path']))

    def test_cube_exit_zero_incomplete_output_not_validated(self):
        spec, plan = self.prepared('ESP')
        result = subprocess.run(plan['argv'], cwd=plan['cwd'], env={**os.environ, **plan['env'], 'FAKE_MULTIWFN_MALFORMED': 'density'}, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(Path(plan['result_path']).exists())
        self.assertFalse((Path(plan['attempt_directory']) / 'jobs-validation.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
