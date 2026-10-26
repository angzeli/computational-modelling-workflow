"""Runner evidence is append-only, identity-bound and separate from validation."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from cmw.molecular.multiwfn.jobs import check, event, prepare
from tests.molecular.multiwfn.jobs_fixtures import make_spec


class RunnerEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-event-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def prepared(self, name='one', profile='FMO'):
        spec = make_spec(self.root / name, profile)
        checked = check(spec)
        self.assertEqual(checked['status'], 'READY', checked)
        Path(spec['workdir']).mkdir()
        plan = prepare(spec, checked)
        return spec, plan, Path(plan['attempt_directory'])

    def test_same_names_keep_separate_request_stream_and_source_identity(self):
        first, one, attempt_one = self.prepared('one')
        second, two, attempt_two = self.prepared('two')
        self.assertEqual(first['name'], second['name'])
        a = json.loads((attempt_one / 'jobs-association.json').read_text())
        b = json.loads((attempt_two / 'jobs-association.json').read_text())
        self.assertNotEqual(a['stream_id'], b['stream_id'])
        self.assertNotEqual(a['request_id'], b['request_id'])
        self.assertNotEqual(a['source_result'], b['source_result'])
        event(attempt_one, 'launch_requested')
        records = [json.loads(line) for line in (attempt_one / 'jobs-events.jsonl').read_text().splitlines()]
        self.assertEqual([r['sequence'] for r in records], [1, 2])
        self.assertTrue(all(r['events_identity'] == r['event_source_identity'] == a['events_identity'] for r in records))
        with self.assertRaisesRegex(ValueError, 'already recorded'):
            event(attempt_one, 'launch_requested')

    def test_replaced_or_truncated_stream_is_never_adopted(self):
        for damage in ('replace', 'truncate', 'empty', 'symlink', 'partial'):
            with self.subTest(damage=damage):
                _, _, attempt = self.prepared(damage)
                path = attempt / 'jobs-events.jsonl'
                initial = path.read_bytes()
                event(attempt, 'launch_requested')
                if damage == 'replace':
                    path.rename(attempt / 'retained-events.jsonl')
                    path.write_bytes(initial)
                elif damage == 'symlink':
                    path.rename(attempt / 'retained-events.jsonl')
                    path.symlink_to(attempt / 'retained-events.jsonl')
                else:
                    path.write_bytes(initial if damage == 'truncate' else b'' if damage == 'empty' else initial + b'{')
                with self.assertRaises((ValueError, OSError)):
                    event(attempt, 'child_launched', '123')

    def test_success_event_requires_existing_validator_outcome(self):
        _, plan, attempt = self.prepared()
        before = (attempt / 'jobs-events.jsonl').read_bytes()
        with self.assertRaises(FileNotFoundError):
            event(attempt, 'validation_passed')
        self.assertEqual((attempt / 'jobs-events.jsonl').read_bytes(), before)
        self.assertFalse((attempt / 'jobs-validation.jsonl').exists())
        Path(plan['result_path']).write_text(json.dumps({'scientific': {'status': 'INVALID'}}))
        with self.assertRaisesRegex(ValueError, 'did not record'):
            event(attempt, 'validation_passed')
        self.assertEqual((attempt / 'jobs-events.jsonl').read_bytes(), before)

    def test_stale_valid_result_and_out_of_order_boundary_are_rejected(self):
        _, plan, attempt = self.prepared()
        with self.assertRaisesRegex(ValueError, 'out of order'):
            event(attempt, 'child_launched', '123')
        event(attempt, 'launch_requested')
        event(attempt, 'process_exited', '0')
        event(attempt, 'validation_started')
        Path(plan['result_path']).write_text(json.dumps({'scientific': {'status': 'VALID'},
            'execution': {'process_exit_code': 0}, 'attempt': {'attempt_id': 'other'},
            'target': {'target_id': plan['scientific_plan']['target_id']}}))
        with self.assertRaisesRegex(ValueError, 'different scientific attempt'):
            event(attempt, 'validation_passed')
        self.assertFalse((attempt / 'jobs-validation.jsonl').exists())

    def test_invalid_preflight_for_every_profile_never_materializes(self):
        for profile in ('FMO', 'ESP', 'IGMH', 'IFCT'):
            spec = make_spec(self.root / profile, profile)
            bad = copy.deepcopy(spec)
            bad['resources']['cpus'] = 1
            self.assertNotEqual(check(bad)['status'], 'READY')
            bad = copy.deepcopy(spec)
            bad['runtime']['version'] = '2026.7.16'
            self.assertNotEqual(check(bad)['status'], 'READY')
            if profile == 'IGMH':
                fragments = Path(spec['scientific']['fragments_file'])
                content = json.loads(fragments.read_text())
                content['fragments']['B'] = [1, 3, 5]
                fragments.write_text(json.dumps(content))
                self.assertNotEqual(check(spec)['status'], 'READY')
            self.assertFalse(Path(spec['workdir']).exists())

    def test_launch_event_failure_still_drains_owned_child(self):
        _, plan, attempt = self.prepared()
        runtime_helper = Path(__file__).resolve().parents[3] / 'scripts/multiwfn/multiwfn_runtime.sh'
        command = 'source "$1"; multiwfn_runtime_load "$2"; multiwfn_jobs_event() { return 1; }; cd "$3"; multiwfn_runtime_launch "$4" < menu.in > multiwfn.log'
        result = subprocess.run(['/bin/bash', '-e', '-c', command, 'runner-test', str(runtime_helper),
            str(attempt / 'runtime.json'), str(attempt), plan['source_wavefunction']],
            env={**os.environ, **plan['env'], 'CMW_MULTIWFN_OPERATION': 'FMO',
                 'CMW_SOURCE_GEOMETRY': plan['source_geometry'], 'FAKE_MULTIWFN_CHILD_SECONDS': '0.2'},
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('child supervision continues', result.stderr)
        self.assertTrue((attempt / 'orb000005.cub').is_file())
        # Runtime alias cleanup only runs after its owned child has drained.
        alias = dict(line.split('=', 1) for line in (attempt / 'multiwfn-runtime-alias.txt').read_text().splitlines())
        self.assertFalse(Path(alias['settings']).parent.exists())


if __name__ == '__main__':
    unittest.main()
