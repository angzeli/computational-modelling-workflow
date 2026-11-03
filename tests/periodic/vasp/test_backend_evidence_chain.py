"""Cross-repository contract: backend reports -> aggregate -> port metadata -> CMW.

Opt-in: set CMW_TEST_VASP_PORT to a candidate VASP port checkout and
CMW_TEST_VMB_BACKEND to a built vasp-metal-backend checkout (build/rank_probe).
The port runner is driven in-process with MPI supervision replaced by a few
harmless rank_probe processes that receive Open MPI-style rank variables; no
MPI job, VASP binary or relink is involved. The backend's own aggregator runs
as pinned by the synthetic manifest; CMW then binds and displays the result
without executing anything.
"""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cmw.jobs.store import Store
from cmw.periodic.vasp import backend_evidence as be
from cmw.periodic.vasp.result_runner import DIALECT, inspect_runner

PORT = os.environ.get('CMW_TEST_VASP_PORT')
BACKEND = os.environ.get('CMW_TEST_VMB_BACKEND')
POSCAR = 'Si synthetic fixture\n5.43\n0 .5 .5\n.5 0 .5\n.5 .5 0\nSi\n2\nDirect\n0 0 0\n.25 .25 .25\n'
WORLD = 4


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@unittest.skipUnless(PORT and BACKEND, 'set CMW_TEST_VASP_PORT and CMW_TEST_VMB_BACKEND')
class BackendEvidenceChainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-backend-chain-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.port = self.root / 'port'
        for name in ('scripts', 'config', 'provenance'):
            shutil.copytree(Path(PORT) / name, self.port / name)
        (self.port / 'private/binary-manifests').mkdir(parents=True)
        self.exe = self.root / 'relink/vasp_std_vmb'
        self.exe.parent.mkdir()
        shutil.copy(Path(BACKEND) / 'build/rank_probe', self.exe)  # harmless Mach-O stand-in
        self.aggregator = self.root / 'backend/vmb_evidence.py'
        self.aggregator.parent.mkdir()
        shutil.copy(Path(BACKEND) / 'scripts/vmb_evidence.py', self.aggregator)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        for name, text in {'INCAR': 'ENCUT=300\n', 'POSCAR': POSCAR, 'POTCAR': 'synthetic only; ZVAL = 4.0\n',
                           'KPOINTS': 'mesh\n0\nGamma\n2 2 2\n0 0 0\n'}.items():
            (self.inputs / name).write_text(text)
        self.store = Store(self.root / 'queue')
        sys.path.insert(0, str(self.port / 'scripts'))
        self.addCleanup(sys.path.remove, str(self.port / 'scripts'))
        for module in ('run_vasp', 'launch', 'toolchain', 'observables'):
            sys.modules.pop(module, None)
        self.runner = importlib.import_module('run_vasp')
        self.launch = importlib.import_module('launch')

    def manifest(self, name, mode, check):
        data = {'schema': 'vasp-port.binary-manifest.v1', 'name': name, 'status': 'reviewed',
                'executable': {'path': str(self.exe), 'sha256': sha(self.exe), 'bytes': self.exe.stat().st_size},
                'vasp': {'version': '6.6.1', 'variant': 'std'},
                'backend': {'name': 'vasp-metal-backend', 'version': '1.2.3', 'source_commit': 'c' * 40,
                            'source_digest': 'd' * 64, 'source_dirty': True, 'library_sha256': 'e' * 64,
                            'report_schema': 'vmb.rank-report.v1',
                            'aggregator': {'path': str(self.aggregator), 'sha256': sha(self.aggregator)}},
                'build': {'objects': '/synthetic/objects', 'object_count': 1},
                'toolchain': dict(self.launch.TOOLCHAIN), 'runtime_dependencies': [],
                'selection': {'mode': mode, 'shadow_check': check}}
        path = self.port / 'private/binary-manifests' / (name + '.json')
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def run_chain(self, name, mode, check, calls, *, skip_report=()):
        """Register a held job, then run the port runner with simulated ranks."""
        self.manifest(name, mode, check)
        output = self.root / f'output-{name}'
        argv = [str(self.port / 'scripts/run-vasp.sh'), '--input', str(self.inputs), '--output', str(output),
                '--binary', 'std', '--binary-manifest', name, '--ranks', str(WORLD), '--ncore', '1',
                '--kpar', '1', '--mpi-mode', 'native', '--restart', 'none', '--timeout', '60',
                '--stop-before', '0', '--managed-foreground']
        job = self.store.add(argv=argv, cwd=self.inputs, name=f'chain {name}', hold=True,
                             write_scope=str(self.root))

        def simulated_ranks(command, timeout, **kwargs):
            started = datetime.now(timezone.utc).isoformat()
            procs = []
            for rank in range(WORLD):
                env = {**kwargs['env'], 'OMPI_COMM_WORLD_RANK': str(rank), 'OMPI_COMM_WORLD_SIZE': str(WORLD)}
                how = ['noreport'] if rank in skip_report else []
                procs.append(subprocess.Popen([command[-1], calls, *how], env=env, cwd=kwargs['cwd'],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            codes = [p.wait(timeout=60) for p in procs]
            (Path(kwargs['cwd']) / 'OUTCAR').write_text(' vasp.6.6.1 synthetic\n General timing and accounting\n')
            return dict(status=max(codes), child_status=max(codes), reason='completed', advance_stop='disabled',
                        start_utc=started, end_utc=datetime.now(timezone.utc).isoformat())

        started_at = time.time()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'CMW_JOBS_ATTEMPT_ID': job['attempt_id'],
                                                        'CMW_JOBS_OWN_SESSION': '1'}))
            for attr, value in (('getpgrp', 1), ('getpid', 2), ('getsid', 1)):
                stack.enter_context(patch.object(self.runner.os, attr, return_value=value))
            plan = self.runner.plan(self.runner.parser().parse_args(argv[1:]))
            stack.enter_context(patch.object(self.launch, 'supervise', side_effect=simulated_ranks))
            stack.enter_context(patch.object(self.runner, 'git_identity', return_value='synthetic'))
            stack.enter_context(redirect_stdout(io.StringIO()))
            status = self.runner.execute(plan)
        finished = time.time()
        bound_job = {**job, 'started_at': started_at, 'finished_at': finished + 1}
        return status, output, job, bound_job, {'recorded_at': finished + .5}

    def bind(self, output, job, receipt):
        meta = output / 'RUN_METADATA.txt'
        declaration = {'dialect': DIALECT, 'path': str(meta), 'sha256': sha(meta)}
        return inspect_runner(declaration, job, receipt, output)

    def shows(self, job):
        from cmw.cli import main
        out = io.StringIO()
        with redirect_stdout(out):
            main(['jobs', '--state', str(self.store.root), 'show', job['display_id'], '--json'])
        return json.loads(out.getvalue())['backend_evidence']

    def test_force_check_chain_agrees_end_to_end(self):
        status, output, job, bound, receipt = self.run_chain('chain-force', 'force', True, '0,2')
        self.assertEqual(status, 0)
        meta = (output / 'RUN_METADATA.txt').read_text()
        self.assertIn(f"backend_attempt_id: {job['attempt_id']}; origin=cmw", meta)
        self.assertIn('backend_evidence_status: COMPLETE', meta)
        evidence = json.loads((output / 'vmb-evidence.json').read_text())
        self.assertEqual((evidence['totals']['consumed'], evidence['ranks_with_consumed']), (2, [0, 2]))
        result = self.bind(output, bound, receipt)
        self.assertTrue(result['valid'], result['error'])
        backend = result['facts']['backend']
        self.assertTrue(backend['verified'] and backend['gpu_qualified'])
        self.assertEqual((backend['summary']['consumed'], backend['summary']['ranks_reported']), (2, WORLD))
        shown = self.shows(job)
        self.assertTrue(shown['verified'])
        self.assertIn('evidence COMPLETE 4/4 ranks; GPU_CONSUMED: consumed 2 of 2 calls on 2 ranks', shown['line'])

    def test_off_chain_is_cpu_only_and_not_qualified(self):
        status, output, job, bound, receipt = self.run_chain('chain-off', 'off', False, 'all')
        self.assertEqual(status, 0)
        backend = self.bind(output, bound, receipt)['facts']['backend']
        self.assertEqual((backend['execution'], backend['shadow_check'], backend['gpu_qualified']),
                         ('CPU_ONLY', 'NOT_ENABLED', False))
        self.assertIn('CPU_ONLY: consumed 0 of 4 calls', self.shows(job)['line'])

    def test_missing_rank_and_tampering_fail_visibly(self):
        status, output, job, bound, receipt = self.run_chain('chain-missing', 'force', True, '0', skip_report=(3,))
        self.assertEqual(status, 0)  # the process exited successfully
        result = self.bind(output, bound, receipt)
        self.assertTrue(result['valid'], result['error'])
        self.assertEqual((result['facts']['backend']['evidence_status'], result['facts']['backend']['gpu_qualified']),
                         ('INCOMPLETE', False))
        self.assertIn('PROBLEM: backend evidence INCOMPLETE: missing ranks [3]', self.shows(job)['line'])
        path = output / 'vmb-evidence.json'
        path.write_text(path.read_text().replace('"INCOMPLETE"', '"COMPLETE"'))
        result = self.bind(output, bound, receipt)
        self.assertFalse(result['valid'])
        self.assertIn('digest', result['error'])
        self.assertIn('evidence UNVERIFIED', self.shows(job)['line'])


if __name__ == '__main__':
    unittest.main()
