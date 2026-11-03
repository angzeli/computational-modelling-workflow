"""Experimental binary-manifest route: static checks, pinning, binding and summary.

All files are invented fixtures. Nothing here executes a runner, an executable
or the backend aggregator.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp import backend_evidence as be
from cmw.periodic.vasp.result_runner import validate_runner_argv, _validate_backend_metadata
from cmw.periodic.vasp.runner_compatibility import check_runner
from cmw.jobs.location_resolvers import vasp_runner_locations

TOOLCHAIN = {'PORT_GCC_VERSION': '16.2.0', 'PORT_OPENMPI_VERSION': '5.0.11', 'PORT_PRRTE_VERSION': '4.1.0_1',
             'PORT_OPENBLAS_VERSION': '0.3.34', 'PORT_SCALAPACK_VERSION': '2.2.3', 'PORT_FFTW_VERSION': '3.3.11'}
SUMMARY = {'evidence': 'COMPLETE', 'execution': 'GPU_CONSUMED', 'shadow_check': 'PASSED',
           'gpu_qualified': True, 'mode': 'force', 'check': True, 'world_size': 16,
           'max_check_ratio': 0.04, 'ranks_reported': 16, 'missing_ranks': [], 'ranks_with_consumed': 3,
           'calls': 10, 'consumed': 6, 'cpu_calls': 4, 'gpu_submitted': 6, 'range_fallbacks': 4, 'problems': 0}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BackendEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-backend-evidence-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.port = self.root / 'port'
        scripts = self.port / 'scripts'
        scripts.mkdir(parents=True)
        self.runner = scripts / 'run-vasp.sh'
        self.runner.write_text('#!/usr/bin/env bash\n')
        (scripts / 'environment.sh').write_text('PORT_ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)\n')
        for name in ('run_vasp.py', 'launch.py', 'observables.py'):
            (scripts / name).write_text('"""Invented source fixture."""\n')
        (self.port / 'private/binary-manifests').mkdir(parents=True)
        self.exe = self.root / 'relink/vasp_std_vmb'
        self.exe.parent.mkdir()
        self.exe.write_bytes(b'invented executable bytes')
        self.aggregator = self.root / 'vmb_evidence.py'
        self.aggregator.write_text('# invented aggregator fixture\n')
        self.lib = self.root / 'Cellar/libdemo.dylib'
        self.lib.parent.mkdir()
        self.lib.write_bytes(b'invented library')
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.output = self.root / 'output'
        self.attempt = 'a' * 32
        self.argv = [str(self.runner), '--input', str(self.inputs), '--output', str(self.output),
                     '--binary', 'std', '--binary-manifest', 'gpu-force', '--ranks', '16', '--ncore', '4',
                     '--kpar', '1', '--mpi-mode', 'native', '--restart', 'none', '--timeout', '120',
                     '--stop-before', '0', '--managed-foreground']

    def manifest(self, **selection):
        data = {'schema': 'vasp-port.binary-manifest.v1', 'name': 'gpu-force', 'status': 'reviewed',
                'executable': {'path': str(self.exe), 'sha256': sha(self.exe), 'bytes': self.exe.stat().st_size},
                'vasp': {'version': '6.6.1', 'variant': 'std'},
                'backend': {'name': 'vasp-metal-backend', 'version': '1.2.3', 'source_commit': 'c' * 40,
                            'source_digest': 'd' * 64, 'source_dirty': True, 'library_sha256': 'e' * 64,
                            'report_schema': 'vmb.rank-report.v1',
                            'aggregator': {'path': str(self.aggregator), 'sha256': sha(self.aggregator)}},
                'build': {'objects': '/private/objects', 'object_count': 321}, 'toolchain': dict(TOOLCHAIN),
                'runtime_dependencies': [{'path': str(self.lib), 'resolved': str(self.lib), 'sha256': sha(self.lib)}],
                'selection': {'mode': 'force', 'shadow_check': True, **selection}}
        path = self.port / 'private/binary-manifests/gpu-force.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def test_argv_grammar(self):
        self.assertEqual(validate_runner_argv(self.argv), (self.inputs, self.output))
        self.assertEqual(be.manifest_name(self.argv), 'gpu-force')
        bad = [self.argv[:7] + ['../x'] + self.argv[8:],
               self.argv + ['--binary-manifest', 'other'],
               self.argv + ['--controlled-suspension', '--execution-budget', '10']]
        for argv in bad:
            with self.subTest(argv=argv[7:9]), self.assertRaises(ValueError):
                validate_runner_argv(argv)
        stock = [a for i, a in enumerate(self.argv) if i not in (7, 8)]
        self.assertIsNone(be.manifest_name(stock))
        self.assertEqual(validate_runner_argv(stock), (self.inputs, self.output))

    def test_static_selection_and_pinned_sources(self):
        self.manifest()
        selection = be.verify_selection(self.runner, 'gpu-force', dict(TOOLCHAIN))
        self.assertEqual((selection['executable'], selection['mode'], selection['shadow_check']),
                         (str(self.exe), 'force', True))
        sources = be.manifest_sources(self.runner, 'gpu-force')
        self.assertEqual(sources, [self.port / 'private/binary-manifests/gpu-force.json', self.exe,
                                   self.aggregator, self.lib])
        self.exe.write_bytes(b'changed executable')
        with self.assertRaisesRegex(ValueError, 'differs from its reviewed manifest'):
            be.verify_selection(self.runner, 'gpu-force')
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'toolchain differs'):
            be.verify_selection(self.runner, 'gpu-force', {**TOOLCHAIN, 'PORT_GCC_VERSION': '1.0'})
        self.manifest(mode='turbo')
        with self.assertRaises(ValueError):
            be.read_manifest(self.runner, 'gpu-force')
        self.manifest()
        (self.port / 'private/binary-manifests/gpu-force.json').chmod(0o664)
        with self.assertRaises(ValueError):
            be.read_manifest(self.runner, 'gpu-force')

    def test_check_runner_refuses_old_port_without_declaration(self):
        self.manifest()
        result = check_runner(self.runner, self.argv[1:])
        self.assertNotEqual(result['interface_compatibility']['status'], 'COMPATIBLE')

    def test_locations_declare_backend_outputs(self):
        roles = {d.role for d in vasp_runner_locations({'argv': self.argv, 'cwd': str(self.inputs)})}
        self.assertTrue({'backend-reports', 'backend-evidence', 'output'} <= roles)

    def metadata(self, *, status='COMPLETE', summary=SUMMARY, attempt=None, extra_terminal=''):
        self.output.mkdir()
        evidence = self.output / 'vmb-evidence.json'
        evidence.write_text(json.dumps({'schema': 'vmb.attempt-evidence.v1', 'attempt_id': attempt or self.attempt,
                                        'evidence': status, 'missing_ranks': [], 'problems': []}))
        manifest = self.manifest()
        lines = ['state: STAGING', f'binary: {self.exe}', 'binary_selection: manifest',
                 f'binary_manifest: {manifest}; sha256={sha(manifest)}; name=gpu-force',
                 'backend: vasp-metal-backend 1.2.3; source_commit=c; source_digest=d; library_sha256=e',
                 'backend_mode: force', 'backend_check: True',
                 f'backend_attempt_id: {attempt or self.attempt}; origin=cmw',
                 f'backend_report_dir: {self.output / "vmb-reports"}', 'state: RUNNING',
                 f'backend_evidence: {evidence}; sha256={sha(evidence)}',
                 f'backend_evidence_status: {status}',
                 f'backend_summary: {json.dumps(summary, separators=(",", ":"))}', extra_terminal,
                 'state: FINISHED']
        (self.output / 'RUN_METADATA.txt').write_text('\n'.join(l for l in lines if l) + '\n')
        return {'argv': self.argv, 'attempt_id': self.attempt}

    def test_summary_complete_qualified(self):
        result = be.summarize(self.metadata())
        self.assertEqual(result['problems'], [])
        self.assertEqual(result['state'], 'FINISHED')
        self.assertIn('GPU_CONSUMED: consumed 6 of 10 calls on 3 ranks; shadow PASSED', result['line'])
        self.assertIn('GPU qualified yes', result['line'])

    def test_summary_problems_remain_visible(self):
        incomplete = {**SUMMARY, 'evidence': 'INCOMPLETE', 'ranks_reported': 15, 'missing_ranks': [7],
                      'gpu_qualified': False}
        result = be.summarize(self.metadata(status='INCOMPLETE', summary=incomplete))
        self.assertIn('PROBLEM: backend evidence INCOMPLETE', result['line'])
        self.assertIn('GPU qualified NO', result['line'])

    def test_summary_rejects_contradictions(self):
        cases = [dict(summary={**SUMMARY, 'gpu_qualified': False}),
                 dict(summary={**SUMMARY, 'calls': 11}),
                 dict(summary={**SUMMARY, 'execution': 'CPU_ONLY'}),
                 dict(attempt='b' * 32)]
        for case in cases:
            with self.subTest(case=case):
                if self.output.exists():
                    for p in sorted(self.output.rglob('*'), reverse=True):
                        p.unlink()
                    self.output.rmdir()
                result = be.summarize(self.metadata(**case))
                self.assertTrue(result['problems'], result)

    def test_planned_job_shows_selection(self):
        self.manifest()
        result = be.summarize({'argv': self.argv, 'attempt_id': self.attempt})
        self.assertEqual((result['state'], result['selection']['executable']), ('NOT_STARTED', str(self.exe)))
        self.assertIn('mode=force, check=1', result['line'])
        self.assertIsNone(be.summarize({'argv': [a for a in self.argv if a not in ('--binary-manifest', 'gpu-force')]}))

    def test_result_binding_requires_attempt_and_block_order(self):
        self.metadata()
        lines = (self.output / 'RUN_METADATA.txt').read_text().splitlines()
        values = {l.partition(': ')[0]: l.partition(': ')[2] for l in lines if l.partition(': ')[0] != 'state'}
        positions = {k: [i] for i, l in enumerate(lines) for k in [l.partition(': ')[0]]}
        running, finished = 9, len(lines) - 1
        job = {'argv': self.argv, 'attempt_id': self.attempt}
        facts = _validate_backend_metadata(values, positions, running, finished, job, 'gpu-force', self.output)
        self.assertEqual((facts['evidence_status'], facts['summary']['consumed']), ('COMPLETE', 6))
        with self.assertRaisesRegex(ValueError, 'not bound to this Jobs attempt'):
            _validate_backend_metadata(values, positions, running, finished, {**job, 'attempt_id': 'b' * 32},
                                       'gpu-force', self.output)
        moved = dict(positions, backend_mode=[running + 1])
        with self.assertRaisesRegex(ValueError, 'staging or terminal block'):
            _validate_backend_metadata(values, moved, running, finished, job, 'gpu-force', self.output)

    def test_profile_pins_manifest_sources(self):
        from cmw.nodes.identity import _profile
        self.manifest()
        self.runner.chmod(0o700)
        spec = {'engine': 'VASP', 'runner': str(self.runner), 'max_cpus': 16, 'max_ranks': 16,
                'binary_manifests': ['gpu-force']}
        profile = _profile(spec, {'fingerprint': 'f' * 64})
        pinned = {item['path'] for item in profile['dependencies']}
        self.assertTrue({str(self.exe), str(self.aggregator), str(self.lib),
                         str(self.port / 'private/binary-manifests/gpu-force.json')} <= pinned)
        for bad in (['../x'], ['gpu-force', 'gpu-force'], 'gpu-force'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _profile({**spec, 'binary_manifests': bad}, {'fingerprint': 'f' * 64})

    @unittest.skipUnless(os.environ.get('CMW_TEST_VASP_PORT'), 'set CMW_TEST_VASP_PORT to a candidate port checkout')
    def test_check_runner_accepts_candidate_port(self):
        import shutil
        source = Path(os.environ['CMW_TEST_VASP_PORT'])
        shutil.rmtree(self.port / 'scripts')
        shutil.copytree(source / 'scripts', self.port / 'scripts')
        shutil.copytree(source / 'config', self.port / 'config')
        (self.port / 'private/toolchain.env').write_text(''.join(f'{k}={v}\n' for k, v in TOOLCHAIN.items()))
        self.manifest()
        result = check_runner(self.runner, self.argv[1:])
        self.assertEqual(result['interface_compatibility']['status'], 'COMPATIBLE', result['missing_requirements'])
        selection = result['observed_capabilities']['binary_selection']
        self.assertEqual((selection['executable'], selection['mode']), (str(self.exe), 'force'))
        self.exe.write_bytes(b'changed')
        result = check_runner(self.runner, self.argv[1:])
        self.assertNotEqual(result['interface_compatibility']['status'], 'COMPATIBLE')
        self.assertTrue(any('not usable' in m for m in result['missing_requirements']))


if __name__ == '__main__':
    unittest.main()
