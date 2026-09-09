"""Bounded telemetry acceptance using only synchronized, owned Python fixtures."""
import json
import subprocess
import sys
import unittest

import psutil

from cmw.jobs import activity
from cmw.jobs.ownership import identity
from tests.jobs import test_jobs as lifecycle
from tests.jobs.isolated_runtime import fixture_collect


class TelemetryRuntimeTests(unittest.TestCase):
    def setUp(self):
        lifecycle.QueueTests.setUp(self)
        from cmw.jobs.telemetry import Sampler
        self.sampler = Sampler()
        self.observer = activity.Observer(collector=fixture_collect)
        self.fixtures = []

    def tearDown(self):
        try:
            for process, gates in self.fixtures:
                gates['release'].touch()
                if process is not None:
                    process.wait(timeout=5)
        finally:
            lifecycle.QueueTests.tearDown(self)

    jobs = lifecycle.QueueTests.jobs
    add = lifecycle.QueueTests.add
    run_queue = lifecycle.QueueTests.run_queue
    completed = lifecycle.QueueTests.completed

    def fixture_code(self, name):
        gates = {key: self.root / (name+'-'+key) for key in
                 ('ready', 'allocate', 'allocated', 'idle', 'idling', 'release')}
        code = f'''
import os,time
from pathlib import Path
p = {{key: Path(value) for key,value in {dict((k,str(v)) for k,v in gates.items())!r}.items()}}
p['ready'].write_text(str(os.getpid()))
buffer = None
while not p['release'].exists():
    if p['allocate'].exists() and buffer is None:
        buffer = bytearray(16 * 1024 * 1024)
        for i in range(0, len(buffer), 4096): buffer[i] = 1
        p['allocated'].touch()
    if buffer is None or p['idle'].exists():
        if p['idle'].exists(): p['idling'].touch()
        time.sleep(.02)
    else:
        sum(i*i for i in range(30000))
'''
        return code, gates

    def external(self, name):
        code, gates = self.fixture_code(name)
        process = subprocess.Popen([sys.executable, '-c', code])
        self.fixtures.append((process, gates))
        lifecycle.wait_for(gates['ready'].exists)
        return process, gates

    def select_external(self, processes):
        self.store.root.mkdir(parents=True, exist_ok=True)
        executable = activity.native_executable(psutil.Process(processes[0].pid))
        (self.store.root / 'external-activity.json').write_text(
            json.dumps({'executables': {executable: 'VASP'}}))
        self.select_pids([p.pid for p in processes])

    def select_pids(self, pids):
        self.store.root.mkdir(parents=True, exist_ok=True)
        (self.store.root / 'fixture-processes.json').write_text(
            json.dumps([identity(pid) for pid in pids]))

    def project(self):
        return activity.project(self.store, observer=self.observer, sampler=self.sampler)

    def positive_usage(self, extract):
        def check():
            state = self.project()
            usage = extract(state)
            if usage and (usage.get('cpu_cores') or 0) > 0:
                return state
        return lifecycle.wait_for(check, timeout=8)

    def test_external_cpu_memory_warmup_and_idle_never_clears_busy(self):
        process, gates = self.external('external')
        self.select_external([process])
        self.assertFalse(self.store.path.exists())
        first = self.project()
        usage = first['external_activity']['observations'][0]['usage']
        self.assertIsNone(usage['cpu_cores'])
        baseline = usage['rss_bytes']
        self.assertGreater(baseline, 0)
        gates['allocate'].touch()
        lifecycle.wait_for(gates['allocated'].exists)
        extract = lambda s: s['external_activity']['observations'][0]['usage']
        busy = self.positive_usage(extract)
        usage = extract(busy)
        self.assertGreater(usage['rss_bytes'], baseline + 8 * 1024 * 1024)
        self.assertEqual(busy['external_activity']['state'], 'BUSY')
        self.assertEqual(busy['jobs'], [])
        gates['idle'].touch()
        lifecycle.wait_for(gates['idling'].exists)
        # Rebaseline after the explicit idle acknowledgement; subsequent CPU
        # deltas must fall without removing the still-live external workload.
        self.project()
        active_cpu = usage['cpu_cores']
        def cooled():
            state = self.project()
            value = extract(state)['cpu_cores']
            return state if value is not None and value < active_cpu else None
        idle = lifecycle.wait_for(cooled, timeout=8)
        self.assertEqual(idle['external_activity']['state'], 'BUSY')
        self.assertIsNone(process.poll())
        gates['release'].touch()
        process.wait(timeout=5)
        ended = self.project()
        self.assertEqual(ended['external_activity']['observations'], [])
        self.assertEqual(ended['external_activity']['state'], 'NO_MATCH')
        self.assertFalse(self.store.path.exists(), 'View-only telemetry must not create SQLite state/events')

    def test_unrelated_external_workloads_keep_separate_usage(self):
        first, first_gates = self.external('first')
        second, second_gates = self.external('second')
        self.select_external([first, second])
        first_gates['allocate'].touch()
        lifecycle.wait_for(first_gates['allocated'].exists)
        self.project()
        state = self.positive_usage(lambda s: next(
            row['usage'] for row in s['external_activity']['observations'] if row['pid'] == first.pid))
        rows = {row['pid']: row for row in state['external_activity']['observations']}
        self.assertEqual(set(rows), {first.pid, second.pid})
        self.assertTrue(all(row['nproc'] == 1 for row in rows.values()))
        self.assertGreater(rows[first.pid]['usage']['rss_bytes'],
                           rows[second.pid]['usage']['rss_bytes'] + 8 * 1024 * 1024)
        self.assertNotEqual(rows[first.pid]['id'], rows[second.pid]['id'])

    def test_managed_surviving_child_usage_and_done_preserve_requests(self):
        code, gates = self.fixture_code('managed-child')
        self.fixtures.append((None, gates))
        parent_gone = self.root / 'wrapper-returned'
        wrapper = ("import os,subprocess,sys; from pathlib import Path; "
                   f"subprocess.Popen([sys.executable,'-c',{code!r}]); "
                   f"Path({str(parent_gone)!r}).write_text(str(os.getpid()))")
        self.add(wrapper, cpus=2, memory_gib=1)
        self.run_queue()
        lifecycle.wait_for(gates['ready'].exists)
        lifecycle.wait_for(parent_gone.exists)
        wrapper_pid = int(parent_gone.read_text())
        lifecycle.wait_for(lambda: not psutil.pid_exists(wrapper_pid))
        child_pid = int(gates['ready'].read_text())
        group = self.jobs()[0]['group']
        self.select_pids([group['pid'], child_pid])
        first = self.project()
        self.assertEqual(first['jobs'][0]['status'], 'Run')
        self.assertIsNone(first['jobs'][0]['usage']['cpu_cores'])
        gates['allocate'].touch()
        lifecycle.wait_for(gates['allocated'].exists)
        state = self.positive_usage(lambda s: s['jobs'][0]['usage'])
        job = state['jobs'][0]
        self.assertEqual(job['status'], 'Run')
        self.assertGreater(job['usage']['rss_bytes'], 8 * 1024 * 1024)
        self.assertEqual(job['resources']['cpus'], 2)
        self.assertEqual(job['resources']['memory_gib'], 1)
        self.assertTrue(psutil.pid_exists(child_pid))
        self.assertEqual(state['external_activity']['observations'], [])
        gates['release'].touch()
        self.completed(1)
        ended = self.project()
        self.assertEqual(ended['jobs'][0]['status'], 'Done')
        self.assertIsNone(ended['jobs'][0]['usage'])
        self.assertEqual(ended['jobs'][0]['resources'], job['resources'])
        self.assertFalse(psutil.pid_exists(child_pid))


    def test_unavailable_usage_preserves_real_external_identity_and_busy_guard(self):
        from cmw.jobs.telemetry import Sampler
        process, gates = self.external('denied-usage')
        self.select_external([process])

        class DeniedUsage:
            def __init__(self, pid):
                self.process = psutil.Process(pid)

            def __getattr__(self, name):
                return getattr(self.process, name)

            def cpu_times(self):
                raise psutil.AccessDenied(self.process.pid)

            def memory_info(self):
                raise psutil.AccessDenied(self.process.pid)

        self.sampler = Sampler(process_factory=DeniedUsage)
        state = self.project()
        guard = state['external_activity']
        self.assertEqual(guard['state'], 'BUSY')
        self.assertTrue(guard['coverage']['complete'])
        self.assertFalse(state['admission']['permitted'])
        observation = guard['observations'][0]
        self.assertEqual(observation['identity'], identity(process.pid))
        self.assertIsNone(observation['usage']['cpu_cores'])
        self.assertIsNone(observation['usage']['rss_bytes'])
        self.assertEqual(observation['usage']['cpu_quality'], 'unavailable')
        self.assertEqual(observation['usage']['memory_quality'], 'unavailable')
        self.assertIsNone(process.poll())


if __name__ == '__main__':
    unittest.main()
