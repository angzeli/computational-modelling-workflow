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
_STATUS_NAMES = ('ok', 'disabled', 'trivial', 'badarg', 'nonfinite', 'too_small', 'too_large',
                 'no_device', 'gpu_error', 'gpu_nonfinite', 'check_failed', 'range', 'auto_cpu')
# Consistent synthetic vmb.attempt-evidence.v1 matching SUMMARY (attempt filled per test).
EVIDENCE = {'schema': 'vmb.attempt-evidence.v1', 'attempt_id': None, 'world_size': 16,
            'report_dir': '/synthetic/vmb-reports', 'evidence': 'COMPLETE', 'problems': [],
            'ranks_reported': list(range(16)), 'missing_ranks': [], 'claimed_without_report': [],
            'totals': {'calls': 10, 'gpu_submitted': 6, 'gpu_completed': 6, 'consumed': 6, 'cpu_calls': 4,
                       'checked': 6},
            'statuses': {**dict.fromkeys(_STATUS_NAMES, 0), 'ok': 6, 'range': 4},
            'max_check_ratio': 0.04, 'ranks_with_consumed': [0, 5, 9], 'execution': 'GPU_CONSUMED',
            'shadow_check': 'PASSED', 'gpu_qualified': True, 'backend': None, 'mode': 'force', 'check': True,
            'executable': '/synthetic/vasp_std_vmb', 'hostname': 'synthetic', 'limitations': []}


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

    def evidence(self, **changes):
        doc = json.loads(json.dumps(EVIDENCE))
        doc['attempt_id'] = self.attempt
        for dotted, value in changes.items():
            target = doc
            *parents, leaf = dotted.split('__')
            for key in parents:
                target = target[key]
            target[leaf] = value
        return doc

    def metadata(self, *, status='COMPLETE', evidence=None, summary=None, attempt=None, digest=None,
                 mode='force', check='True', write_evidence=True, launcher_status='0'):
        """Write a runner record whose terminal keys describe OUTPUT/vmb-evidence.json."""
        if not self.output.exists():
            self.output.mkdir()
        path = self.output / 'vmb-evidence.json'
        doc = self.evidence() if evidence is None else evidence
        if write_evidence:
            path.write_text(json.dumps(doc, indent=1))
        recorded = digest or (sha(path) if path.exists() else '0' * 64)
        manifest = self.manifest()
        lines = ['state: STAGING', f'binary: {self.exe}', 'binary_selection: manifest',
                 f'binary_manifest: {manifest}; sha256={sha(manifest)}; name=gpu-force',
                 'backend: vasp-metal-backend 1.2.3; source_commit=c; source_digest=d; library_sha256=e',
                 f'backend_mode: {mode}', f'backend_check: {check}',
                 f'backend_attempt_id: {attempt or self.attempt}; origin=cmw',
                 f'backend_report_dir: {self.output / "vmb-reports"}', 'state: RUNNING',
                 f'launcher_status: {launcher_status}',
                 f'backend_evidence: {path}; sha256={recorded}', f'backend_evidence_status: {status}',
                 f'backend_summary: {json.dumps(SUMMARY if summary is None else summary, separators=(",", ":"))}',
                 'state: FINISHED']
        (self.output / 'RUN_METADATA.txt').write_text('\n'.join(lines) + '\n')
        return {'argv': self.argv, 'attempt_id': self.attempt}

    def reset_output(self):
        if self.output.exists():
            for item in sorted(self.output.rglob('*'), reverse=True):
                item.unlink()
            self.output.rmdir()

    def bind(self, job):
        """Strict result-binding path: the runner-record backend validation."""
        lines = (self.output / 'RUN_METADATA.txt').read_text().splitlines()
        keys = [line.partition(': ')[0] for line in lines]
        values = {k: line.partition(': ')[2] for k, line in zip(keys, lines) if k != 'state'}
        positions = {k: [i] for i, k in enumerate(keys) if k != 'state'}
        running, finished = keys.index('state', 1), len(lines) - 1
        return _validate_backend_metadata(values, positions, running, finished, job, 'gpu-force', self.output)

    def assert_refused(self, job, fragment):
        """Integrity failures refuse binding and leave display without a verdict."""
        with self.assertRaisesRegex(ValueError, fragment):
            self.bind(job)
        shown = be.summarize(job)
        self.assertFalse(shown['verified'])
        self.assertIsNone(shown['summary'])
        self.assertIn('evidence UNVERIFIED', shown['line'])
        self.assertNotIn('CPU_ONLY', shown['line'])
        self.assertNotIn('GPU qualified yes', shown['line'])

    def test_complete_evidence_binds_and_displays(self):
        job = self.metadata()
        facts = self.bind(job)
        self.assertTrue(facts['verified'] and facts['gpu_qualified'])
        self.assertEqual((facts['evidence_status'], facts['execution'], facts['shadow_check']),
                         ('COMPLETE', 'GPU_CONSUMED', 'PASSED'))
        self.assertEqual(facts['summary'], SUMMARY)
        shown = be.summarize(job)
        self.assertEqual(shown['problems'], [])
        self.assertIn('GPU_CONSUMED: consumed 6 of 10 calls on 3 ranks; shadow PASSED', shown['line'])
        self.assertIn('GPU qualified yes', shown['line'])

    def test_missing_or_altered_evidence_file(self):
        job = self.metadata()
        (self.output / 'vmb-evidence.json').unlink()
        self.assert_refused(job, 'unreadable')
        self.reset_output()
        job = self.metadata()
        path = self.output / 'vmb-evidence.json'
        path.write_text(path.read_text().replace('"calls": 10', '"calls": 11'))  # digest not updated
        self.assert_refused(job, 'differ from the recorded digest')
        self.reset_output()
        job = self.metadata()
        text = (self.output / 'RUN_METADATA.txt').read_text()
        (self.output / 'RUN_METADATA.txt').write_text(
            text.replace(f'{self.output}/vmb-evidence.json', f'{self.root}/vmb-evidence.json'))
        self.assert_refused(job, 'OUTPUT/vmb-evidence.json')

    def test_summary_must_match_evidence(self):
        for change in ({'consumed': 7}, {'gpu_qualified': False}, {'missing_ranks': [3]},
                       {'ranks_with_consumed': 2}, {'range_fallbacks': 0}, {'max_check_ratio': 0.05},
                       {'problems': 1}, {'world_size': 8}):
            with self.subTest(change=change):
                self.reset_output()
                self.assert_refused(self.metadata(summary={**SUMMARY, **change}), 'summary differs|world size')

    def test_tampered_counters_with_recomputed_digest(self):
        cases = [{'totals__calls': 11}, {'totals__consumed': 5}, {'statuses__ok': 5},
                 {'totals__gpu_completed': 5}, {'totals__checked': 0}, {'statuses__range': 3},
                 {'totals__consumed': 5, 'totals__cpu_calls': 5, 'statuses__ok': 5, 'statuses__range': 5,
                  'totals__gpu_submitted': 5, 'totals__gpu_completed': 5, 'totals__checked': 5}]
        for change in cases:
            with self.subTest(change=change):
                self.reset_output()
                self.assert_refused(self.metadata(evidence=self.evidence(**change)),
                                    'inconsistent|summary differs|contradict|checked')

    def test_identity_mismatches(self):
        cases = [(dict(evidence=self.evidence(attempt_id='b' * 32)), 'another attempt'),
                 (dict(mode='auto'), 'mode/check'), (dict(check='False'), 'mode/check'),
                 (dict(evidence=self.evidence(world_size=8)), 'world size'),
                 (dict(evidence=self.evidence(schema='vmb.attempt-evidence.v0')), 'not vmb.attempt-evidence.v1')]
        for kwargs, fragment in cases:
            with self.subTest(kwargs=kwargs):
                self.reset_output()
                self.assert_refused(self.metadata(**kwargs), fragment)

    def test_rank_sets_must_be_consistent(self):
        cases = [{'missing_ranks': [3]}, {'ranks_reported': list(range(15))},
                 {'ranks_with_consumed': [0, 5, 99]}, {'ranks_with_consumed': [0, 0, 5]},
                 {'ranks_with_consumed': [1, 2, 20]}, {'claimed_without_report': [4]},
                 {'ranks_with_consumed': []}]
        for change in cases:
            with self.subTest(change=change):
                self.reset_output()
                self.assert_refused(self.metadata(evidence=self.evidence(**change)),
                                    'rank|COMPLETE|Consumed')

    def test_malformed_and_nonfinite_values(self):
        cases = [{'max_check_ratio': -1.0}, {'max_check_ratio': 'inf'}, {'totals__calls': 10.0},
                 {'totals__calls': True}, {'statuses__ok': '6'}, {'gpu_qualified': 1}, {'problems': 'none'},
                 {'evidence': 'MOSTLY'}, {'totals': {'calls': 10}}]
        for change in cases:
            with self.subTest(change=change):
                self.reset_output()
                self.assert_refused(self.metadata(evidence=self.evidence(**change)), '.')
        self.reset_output()
        job = self.metadata()
        path = self.output / 'vmb-evidence.json'
        path.write_text(path.read_text().replace('0.04', 'NaN'))
        text = (self.output / 'RUN_METADATA.txt').read_text()
        (self.output / 'RUN_METADATA.txt').write_text(
            '\n'.join(l.replace(l.partition('sha256=')[2], sha(path)) if l.startswith('backend_evidence:') else l
                      for l in text.splitlines()) + '\n')
        self.assert_refused(job, 'non-finite')

    def test_honest_bad_evidence_binds_but_never_qualifies(self):
        incomplete = self.evidence(evidence='INCOMPLETE', ranks_reported=[r for r in range(16) if r != 7],
                                   missing_ranks=[7], claimed_without_report=[7], gpu_qualified=False)
        invalid = self.evidence(evidence='INVALID', problems=['rank 3: report belongs to another attempt'],
                                ranks_reported=[r for r in range(16) if r != 3], gpu_qualified=False)
        for status, doc, missing in (('INCOMPLETE', incomplete, [7]), ('INVALID', invalid, [])):
            with self.subTest(status=status):
                self.reset_output()
                summary = {**SUMMARY, 'evidence': status, 'gpu_qualified': False, 'ranks_reported': 15,
                           'missing_ranks': missing, 'problems': len(doc['problems'])}
                job = self.metadata(status=status, evidence=doc, summary=summary)
                facts = self.bind(job)  # process exited 0; the association stays, never qualified
                self.assertEqual((facts['evidence_status'], facts['gpu_qualified']), (status, False))
                self.assertTrue(facts['problems'])
                shown = be.summarize(job)
                self.assertIn(f'PROBLEM: backend evidence {status}', shown['line'])
                self.assertIn('GPU qualified NO', shown['line'])
        self.reset_output()
        job = self.metadata(status='AGGREGATION_FAILED', write_evidence=False, digest='NONE',
                            summary={'error': 'aggregator exited 1'})
        facts = self.bind(job)
        self.assertEqual((facts['verified'], facts['gpu_qualified'], facts['execution']), (False, False, None))
        shown = be.summarize(job)
        self.assertIn('evidence AGGREGATION_FAILED; no verified backend verdict', shown['line'])
        self.assertIn('PROBLEM: backend aggregation failed', shown['line'])
        self.assertNotIn('CPU_ONLY', shown['line'])
        self.reset_output()
        with self.assertRaisesRegex(ValueError, 'aggregation-failure'):
            self.bind(self.metadata(status='AGGREGATION_FAILED', summary=SUMMARY))

    def accounting(self, ranks, consumed):
        """Aggregate and summary whose counters are consistent with `consumed` GPU results."""
        cpu = 10 - consumed
        doc = self.evidence(ranks_with_consumed=ranks,
                            totals={'calls': 10, 'gpu_submitted': consumed, 'gpu_completed': consumed,
                                    'consumed': consumed, 'cpu_calls': cpu, 'checked': consumed},
                            statuses={**dict.fromkeys(_STATUS_NAMES, 0), 'ok': consumed, 'range': cpu},
                            max_check_ratio=0.04 if consumed else None,
                            execution='GPU_CONSUMED' if consumed else 'CPU_ONLY',
                            shadow_check='PASSED' if consumed else 'NO_CHECKS', gpu_qualified=bool(consumed))
        summary = {**SUMMARY, 'consumed': consumed, 'cpu_calls': cpu, 'gpu_submitted': consumed,
                   'range_fallbacks': cpu, 'ranks_with_consumed': len(ranks),
                   'max_check_ratio': doc['max_check_ratio'], 'execution': doc['execution'],
                   'shadow_check': doc['shadow_check'], 'gpu_qualified': doc['gpu_qualified']}
        return doc, summary

    def test_consumed_ranks_cannot_exceed_consumed_calls(self):
        doc, summary = self.accounting([0, 1, 2], 1)  # three ranks claim GPU work; only one result exists
        with self.assertRaisesRegex(ValueError, 'more consumed ranks than consumed'):
            be.verify_aggregate(doc, attempt_id=self.attempt, world_size=16, mode='force', check=True)
        self.assert_refused(self.metadata(evidence=doc, summary=summary), 'more consumed ranks than consumed')

    def test_consumed_rank_accounting_positive_controls(self):
        for ranks, consumed, execution in (([0, 5], 2, 'GPU_CONSUMED'), ([], 0, 'CPU_ONLY')):
            with self.subTest(consumed=consumed):
                self.reset_output()
                doc, summary = self.accounting(ranks, consumed)
                derived = be.verify_aggregate(doc, attempt_id=self.attempt, world_size=16, mode='force', check=True)
                self.assertEqual(derived, summary)
                facts = self.bind(self.metadata(evidence=doc, summary=summary))
                self.assertEqual((facts['verified'], facts['execution'], facts['gpu_qualified']),
                                 (True, execution, bool(consumed)))

    def test_shadow_failure_is_not_qualified(self):
        failed = self.evidence(max_check_ratio=2.5, shadow_check='FAILED', gpu_qualified=False)
        job = self.metadata(evidence=failed, summary={**SUMMARY, 'max_check_ratio': 2.5,
                                                       'shadow_check': 'FAILED', 'gpu_qualified': False})
        facts = self.bind(job)
        self.assertEqual((facts['shadow_check'], facts['gpu_qualified']), ('FAILED', False))
        self.assertIn('shadow check failed', be.summarize(job)['line'])

    def show_surfaces(self, job):
        """`cmw jobs show --json` locally and the node endpoint's `show` for one stored job."""
        from contextlib import redirect_stdout
        from copy import deepcopy
        import io
        from uuid import uuid4
        from cmw.cli import main
        from cmw.nodes.backend import LocalBackend
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(['jobs', '--state', str(self.store.root), 'show', job['display_id'], '--json']), 0)
        local = json.loads(out.getvalue())
        if not hasattr(self, 'node_record'):
            node_id = uuid4().hex
            self.node_record = node_id, self.store.enroll_queue(node_id, 'linux:synthetic-only')
        node_id, record = self.node_record
        config = {'schema': 1, 'node_id': node_id, 'host_id': 'linux:synthetic-only',
                  'host': {'platform': 'linux', 'hardware_id': 'synthetic-only'},
                  'runtime': {'fingerprint': 'synthetic-runtime', 'version': '0.1.0'},
                  'queues': {'research': {**record, 'state': str(self.store.root)}},
                  'storages': {}, 'profiles': {}}
        readiness = {'ready': True, 'identity_ready': True, 'runtime_ready': True, 'storage_ready': True,
                     'profiles_ready': True, 'identity_errors': [], 'runtime_errors': [], 'errors': [],
                     'host': config['host'], 'runtime': config['runtime'], 'queues': {'research': record},
                     'storages': {}}
        backend = LocalBackend(self.root / 'node-config', 'research', clock=lambda: 100.,
                               identity_loader=lambda _: deepcopy(config),
                               identity_checker=lambda _: deepcopy(readiness),
                               activity_projector=lambda store: store.snapshot(now=100.),
                               boot_provider=lambda: 10.)
        self.addCleanup(backend.close)
        node = backend.call('show', target=backend.target(job))['result']
        return local.get('backend_evidence'), node.get('backend_evidence')

    def test_jobs_show_local_and_node_surfaces(self):
        from cmw.jobs.store import Store
        self.store = Store(self.root / 'queue')
        job = self.store.add(argv=self.argv, cwd=self.inputs, name='Synthetic manifest job', hold=True,
                             write_scope=str(self.root))
        self.attempt = job['attempt_id']
        self.metadata()
        for surface in self.show_surfaces(job):
            self.assertTrue(surface['verified'])
            self.assertIn('GPU qualified yes', surface['line'])
        path = self.output / 'vmb-evidence.json'
        path.write_text(path.read_text().replace('"consumed": 6', '"consumed": 7'))
        for surface in self.show_surfaces(job):
            self.assertFalse(surface['verified'])
            self.assertIn('evidence UNVERIFIED', surface['line'])
            self.assertNotIn('GPU qualified', surface['line'])
        stock = [a for a in self.argv if a not in ('--binary-manifest', 'gpu-force')]
        stock[stock.index('--output') + 1] = str(self.root / 'stock-output')
        ordinary = self.store.add(argv=stock, cwd=self.inputs, name='Ordinary VASP', hold=True,
                                  write_scope=str(self.root))
        self.assertEqual(self.show_surfaces(ordinary), (None, None))

    def test_planned_job_shows_selection(self):
        self.manifest()
        result = be.summarize({'argv': self.argv, 'attempt_id': self.attempt})
        self.assertEqual((result['state'], result['selection']['executable']), ('NOT_STARTED', str(self.exe)))
        self.assertIn('mode=force, check=1', result['line'])
        self.assertIsNone(be.summarize({'argv': [a for a in self.argv if a not in ('--binary-manifest', 'gpu-force')]}))

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
