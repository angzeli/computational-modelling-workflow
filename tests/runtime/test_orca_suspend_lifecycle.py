"""Harmless Python modules through the actual ORCA shell, never an engine."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

from cmw.jobs import runtime, suspension
from cmw.jobs.ownership import identity, owner_alive
from cmw.jobs.store import Store
from tests.jobs.isolated_runtime import install
from tests.jobs.lifecycle_evidence import bounded_tail, retain_lifecycle_evidence

ROOT = Path(__file__).resolve().parents[2]


def wait_for(predicate, timeout=12):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Timed out waiting for owned ORCA runner fixture')


class OrcaSuspendLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.isolation = install()
        self.addCleanup(self.isolation.close)
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw controlled orca ')
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root / 'state')
        self.module = self.root / 'module.py'
        self.module.write_text('''import json,os,pathlib,sys,time
stage=int(sys.argv[1]); root=pathlib.Path(sys.argv[2]); sequence=0
sentinel='same-memory-'+str(os.getpid())
while not (root/f'release-{stage}').exists():
    sequence+=1
    target=root/f'module-{stage}.json'; temporary=target.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid':os.getpid(),'sequence':sequence,'sentinel':sentinel}))
    temporary.replace(target)
    time.sleep(.025)
''')
        self.fake = self.root / 'benign-orca-driver'
        self.fake.write_text(f'''#!{sys.executable}
import json,os,pathlib,subprocess,sys
root=pathlib.Path({str(self.root)!r})
(root/'driver.json').write_text(json.dumps({{'pid':os.getpid()}}))
for stage in range(2):
    child=subprocess.Popen([sys.executable,{str(self.module)!r},str(stage),str(root)])
    code=child.wait()
    if code: raise SystemExit(code)
print('Program Version 1.0.0')
print('SCF CONVERGED AFTER 3 CYCLES')
print('FINAL SINGLE POINT ENERGY -4.000000000')
print('ORCA TERMINATED NORMALLY')
''')
        self.fake.chmod(0o700)
        structure = self.root / 'geometry.xyz'
        shutil.copyfile(ROOT / 'tests/fixtures/xyz/synthetic.xyz', structure)
        self.input, self.target = self.root / 'stage.inp', self.root / 'target.json'
        subprocess.run([sys.executable, '-m', 'cmw.molecular.orca.cli', 'prepare', '--stage', 'SP',
            '--structure', str(structure), '--charge', '0', '--multiplicity', '1', '--keywords', 'HF STO-3G',
            '--nprocs', '1', '--maxcore', '64', '--input', str(self.input), '--target', str(self.target)],
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'},
            check=True, capture_output=True)
        self.metadata = self.root / 'attempt.json'
        self.job = self.store.add(argv=[str(ROOT / 'scripts/orca/run_orca.sh'), '--input', str(self.input),
            '--target', str(self.target), '--metadata', str(self.metadata), '--output', str(self.root / 'stage.out'),
            '--stderr', str(self.root / 'stage.err'), '--controlled-suspension'], cwd=self.root,
            name='fictional two-module driver', engine='ORCA', cpus=1, memory_gib=.1,
            suspension_backend='orca-local-v1', env={'ORCA_EXE': str(self.fake), 'PYTHON_BIN': sys.executable,
            'NPROCS': '1', 'MAXCORE_MB': '64', 'CMW_CAFFEINATE': '0'})
        self.recorded = []

    def tearDown(self):
        if self.store.path.exists():
            owner = self.store.snapshot()['controller']['owner']
            job = self.current()
            if job['status'] in {'Starting', 'Run', 'Unknown'}:
                self.store.change(job['id'], 'cancel', confirm=True)
            wait_for(lambda: self.current()['status'] in {'Done', 'Fail', 'Cancelled'}, timeout=12)
            runtime.stop(self.store)
            wait_for(lambda: not self.store.snapshot()['controller']['online'])
            for process in runtime._CHILDREN:
                if owner and process.pid == owner['pid']:
                    process.wait(timeout=5)
            runtime.reap_detached()
            self.assertFalse(any(owner_alive(item) for item in self.recorded))
        self.temporary.cleanup()

    def current(self):
        return next(job for job in self.store.snapshot()['jobs'] if job['id'] == self.job['id'])

    @retain_lifecycle_evidence
    def test_two_module_transitions_preserve_process_and_finalize_only_after_continue(self):
        runtime.start(self.store)
        wait_for(lambda: suspension.readiness(self.store, self.job['id'])['ready'])
        attempt = self.current()['attempt_id']
        wait_for((self.root / 'driver.json').exists)
        driver_pid = json.loads((self.root / 'driver.json').read_text())['pid']
        driver = identity(driver_pid)
        self.recorded.append(driver)
        for stage in range(2):
            path = self.root / f'module-{stage}.json'
            wait_for(path.exists)
            before = json.loads(path.read_text())
            member = identity(before['pid'])
            self.recorded.append(member)
            wait_for(lambda: suspension.readiness(self.store, self.job['id'])['ready'])
            suspension.submit(self.store, self.job['id'], 'suspend', reason='synthetic meeting')
            wait_for(lambda: self.current()['suspension']['phase'] == 'Suspended')
            stopped = json.loads(path.read_text())
            self.assertEqual(psutil.Process(driver_pid).status(), psutil.STATUS_STOPPED)
            self.assertEqual(psutil.Process(member['pid']).status(), psutil.STATUS_STOPPED)
            self.assertFalse(self.metadata.exists())
            self.assertFalse((self.root / f'module-{stage+1}.json').exists())
            time.sleep(.2)
            self.assertEqual(json.loads(path.read_text()), stopped)
            self.assertEqual(self.current()['attempt_id'], attempt)
            self.assertEqual(self.current()['resources']['cpus'], 1)
            suspension.submit(self.store, self.job['id'], 'continue')
            wait_for(lambda: self.current()['suspension']['phase'] == 'Run')
            wait_for(lambda: json.loads(path.read_text())['sequence'] > stopped['sequence'])
            self.assertEqual(identity(driver_pid), driver)
            self.assertEqual(identity(member['pid']), member)
            self.assertEqual(json.loads(path.read_text())['sentinel'], before['sentinel'])
            (self.root / f'release-{stage}').touch()
        def terminal():
            job = self.current()
            return job if job['status'] in {'Done', 'Fail', 'Cancelled', 'Unknown'} else None
        try:
            finished = wait_for(terminal)
            self.assertEqual(finished['status'], 'Done')
        except AssertionError as exc:
            logs = {}
            for path in (self.root/'stage.err',
                         self.store.root/'attempts'/attempt/'stderr.log'):
                if path.is_file():
                    logs[path.name] = bounded_tail(path)
            raise AssertionError(json.dumps({'error': str(exc), 'job': self.current(),
                                             'logs': logs}, indent=2)) from exc
        self.assertEqual(self.current()['attempt_id'], attempt)
        self.assertEqual(len(self.current()['suspension']['intervals']), 2)
        record = json.loads(self.metadata.read_text())
        self.assertTrue(record)
        self.assertEqual(self.current()['exit_code'], 0)

    @retain_lifecycle_evidence
    def test_cancel_suspended_driver_does_not_advance_or_finalize(self):
        self._cancel_without_finalization()

    def _cancel_without_finalization(self):
        runtime.start(self.store)
        wait_for(lambda: suspension.readiness(self.store, self.job['id'])['ready'])
        wait_for((self.root / 'module-0.json').exists)
        first = json.loads((self.root / 'module-0.json').read_text())
        driver = identity(json.loads((self.root / 'driver.json').read_text())['pid'])
        self.recorded.extend([driver, identity(first['pid'])])
        suspension.submit(self.store, self.job['id'], 'suspend')
        wait_for(lambda: self.current()['suspension']['phase'] == 'Suspended')
        self.store.change(self.job['id'], 'cancel', confirm=True)
        wait_for(lambda: self.current()['status'] == 'Cancelled')
        self.assertFalse(self.metadata.exists())
        self.assertFalse((self.root / 'module-1.json').exists())
        self.assertEqual(self.current()['attempt_id'], self.job['attempt_id'])
        self.assertTrue((self.store.root / 'attempts' / self.job['attempt_id'] / 'payload-exit.json').is_file())


    def _failed_driver_keeps_metadata(self, code):
        self.fake.write_text(f"""#!{sys.executable}
import pathlib,time
root=pathlib.Path({str(self.root)!r})
while not (root/'exit-now').exists(): time.sleep(.025)
print('Program Version 1.0.0')
raise SystemExit({code})
""")
        runtime.start(self.store)
        wait_for(lambda: suspension.readiness(self.store, self.job['id'])['ready'])
        (self.root / 'exit-now').touch()
        wait_for(lambda: self.current()['status'] == 'Fail')
        record = json.loads(self.metadata.read_text())
        self.assertEqual(record['execution']['process_exit_code'], code)
        self.assertFalse(record['reusable'])

    @retain_lifecycle_evidence
    def test_ordinary_exit_137_keeps_failed_attempt_metadata(self):
        self._failed_driver_keeps_metadata(137)

    @retain_lifecycle_evidence
    def test_ordinary_exit_143_keeps_failed_attempt_metadata(self):
        self._failed_driver_keeps_metadata(143)

    @retain_lifecycle_evidence
    def test_cancel_with_external_scratch_does_not_copy_back_or_finalize(self):
        from cmw.core.execution_layout import ExecutionLayout
        from cmw.core.provenance import atomic_write_json
        from cmw.molecular.orca.job import load_target
        target, _ = load_target(self.target)
        layout = ExecutionLayout(project_root=self.root, system_identifier='synthetic',
            workflow_node_identifier='sp', target_identifier=target.target_id,
            attempt_identifier='attempt_001')
        layout.create_working_directory()
        atomic_write_json(layout.layout_path, layout.to_dict())
        self.input = layout.working_directory / 'stage.inp'
        self.target = layout.target_directory / 'target.json'
        prepared = subprocess.run([sys.executable, '-m', 'cmw.molecular.orca.cli', 'prepare',
            '--stage', 'SP', '--structure', str(self.root/'geometry.xyz'), '--charge', '0',
            '--multiplicity', '1', '--keywords', 'HF STO-3G', '--nprocs', '1', '--maxcore', '64',
            '--input', str(self.input), '--target', str(self.target)],
            env={**os.environ, 'PYTHONPATH': str(ROOT/'src'), 'PYTHONDONTWRITEBYTECODE': '1'},
            check=True, capture_output=True, text=True)
        self.metadata = layout.working_directory / 'attempt.json'
        self.store.change(self.job['id'], 'cancel', confirm=True)
        self.job = self.store.add(argv=[str(ROOT/'scripts/orca/run_orca.sh'), '--input', str(self.input),
            '--target', str(self.target), '--metadata', str(self.metadata),
            '--output', str(layout.working_directory/'stage.out'), '--stderr', str(layout.working_directory/'stage.err'),
            '--controlled-suspension', '--layout', str(layout.layout_path), '--geometry-contract',
            json.loads(prepared.stdout)['geometry_contract'], '--external-scratch-root', str(self.root/'scratch'),
            '--external-scratch-mount', '/', '--external-scratch-minimum-free-gib', '0.000001'],
            cwd=self.root, name='fictional cancelled external scratch driver', engine='ORCA', cpus=1, memory_gib=.1,
            suspension_backend='orca-local-v1', env={'ORCA_EXE': str(self.fake), 'PYTHON_BIN': sys.executable,
            'NPROCS': '1', 'MAXCORE_MB': '64', 'CMW_CAFFEINATE': '0'})
        self._cancel_without_finalization()
        scratch = json.loads((layout.working_directory/'external-scratch.json').read_text())
        self.assertFalse(scratch.get('copyback'))
        self.assertEqual(scratch['status'], 'FAILED')
        self.assertTrue(Path(scratch['execution_directory']).is_dir())
        self.assertFalse((layout.working_directory/'stage.out').exists())
        self.assertFalse((layout.working_directory/'stage.err').exists())

    @retain_lifecycle_evidence
    def test_surviving_module_delays_copyback_and_finalization_after_driver_exit(self):
        self.fake.write_text(f"""#!{sys.executable}
import json,os,pathlib,subprocess,sys,time
root=pathlib.Path({str(self.root)!r})
(root/'driver.json').write_text(json.dumps({{'pid':os.getpid()}}))
subprocess.Popen([sys.executable,{str(self.module)!r},'0',str(root)])
while not (root/'driver-may-exit').exists(): time.sleep(.025)
print('Program Version 1.0.0')
print('SCF CONVERGED AFTER 3 CYCLES')
print('FINAL SINGLE POINT ENERGY -4.000000000')
print('ORCA TERMINATED NORMALLY')
""")
        runtime.start(self.store)
        wait_for(lambda: suspension.readiness(self.store, self.job['id'])['ready'])
        wait_for((self.root / 'module-0.json').exists)
        member = identity(json.loads((self.root / 'module-0.json').read_text())['pid'])
        driver = identity(json.loads((self.root / 'driver.json').read_text())['pid'])
        self.recorded.extend([driver, member])
        (self.root / 'driver-may-exit').touch()
        wait_for(lambda: not owner_alive(driver))
        time.sleep(.2)
        self.assertTrue(owner_alive(member))
        self.assertFalse(self.metadata.exists())
        self.assertEqual(self.current()['status'], 'Run')
        (self.root / 'release-0').touch()
        wait_for(lambda: self.current()['status'] == 'Done')
        self.assertTrue(self.metadata.exists())
