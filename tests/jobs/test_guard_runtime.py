"""Real lifecycle acceptance with discovery restricted to owned benign fixtures."""
import json
import subprocess
import sys

import psutil
import time
import unittest
from unittest.mock import patch

from cmw.jobs import runtime
from cmw.jobs.activity import native_executable
from cmw.jobs import activity
from cmw.jobs.ownership import exclusive, group_members, identity
from tests.jobs import test_jobs as lifecycle
from tests.jobs.isolated_runtime import detached


def external_fixture(test):
    ready = test.root / 'external-ready'
    release = test.root / 'external-release'
    code = (f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); "
            f"release=Path({str(release)!r})\nwhile not release.exists(): time.sleep(.02)")
    process = subprocess.Popen([sys.executable, '-c', code])
    # Cleanup is registered before observing the child, including setup failures.
    def close():
        if process.poll() is None:
            release.touch()
            process.wait(timeout=5)
    test.fixture_closers.append(close)
    lifecycle.wait_for(ready.exists)
    owned = identity(process.pid)
    executable = native_executable(psutil.Process(process.pid))
    test.store.root.mkdir(parents=True, exist_ok=True)
    (test.store.root / 'external-activity.json').write_text(
        json.dumps({'executables': {executable: 'VASP'}}))
    (test.store.root / 'fixture-processes.json').write_text(json.dumps([owned]))
    return process, release


class GuardRuntimeTests(unittest.TestCase):
    def setUp(self):
        lifecycle.QueueTests.setUp(self)
        self.fixture_closers = []

    def tearDown(self):
        try:
            for close in self.fixture_closers:
                close()
        finally:
            lifecycle.QueueTests.tearDown(self)

    jobs = lifecycle.QueueTests.jobs
    add = lifecycle.QueueTests.add
    run_queue = lifecycle.QueueTests.run_queue
    completed = lifecycle.QueueTests.completed

    def test_busy_blocks_without_claim_and_clear_executes_once(self):
        marker = self.root / 'executions'
        self.add(f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')")
        process, release = external_fixture(self)
        try:
            self.run_queue()
            lifecycle.wait_for(lambda: self.store.snapshot()['controller'].get('external_guard', {}).get('state') == 'BUSY')
            queued = self.jobs()[0]
            self.assertEqual(queued['status'], 'Queue')
            self.assertIsNone(queued['claim'])
            self.assertIsNone(queued['started_at'])
            self.assertFalse(marker.exists())
            self.assertIsNone(process.poll())
            release.touch()
            process.wait(timeout=5)
            self.completed(1)
            self.assertEqual(self.jobs()[0]['status'], 'Done')
            self.assertEqual(marker.read_text(), 'x')
        finally:
            if process.poll() is None:
                release.touch()
                process.wait(timeout=5)

    def test_external_clear_preserves_explicit_pause(self):
        self.add()
        process, release = external_fixture(self)
        try:
            self.run_queue()
            lifecycle.wait_for(lambda: self.store.snapshot()['controller'].get('external_guard', {}).get('state') == 'BUSY')
            self.assertEqual(self.jobs()[0]['status'], 'Queue')
            self.store.dispatch(False)
            release.touch()
            process.wait(timeout=5)
            lifecycle.wait_for(lambda: self.store.snapshot()['controller'].get('external_guard', {}).get('state') == 'NO_MATCH')
            self.assertFalse(self.store.snapshot()['controller']['dispatch'])
            self.assertEqual(self.jobs()[0]['status'], 'Queue')
            self.assertIsNone(self.jobs()[0]['claim'])
            self.store.dispatch(True)
            self.completed(1)
        finally:
            if process.poll() is None:
                release.touch()
                process.wait(timeout=5)


    def test_final_worker_scan_catches_blocker_appearing_after_claim(self):
        marker = self.root / 'must-not-run'
        self.add(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        processes = []

        def insert_blocker(arguments, log):
            processes.append(external_fixture(self))
            return detached(arguments, log)

        try:
            with exclusive(self.store.root / 'controller.lock'):
                with self.store.transaction() as con:
                    control = self.store.control(con)
                    control.update(owner=identity(), dispatch=True, stop=False)
                    self.store.set_control(con, control)
                with patch.object(runtime, 'detached', insert_blocker):
                    runtime.tick(self.store)
                self.assertEqual(len(processes), 1)
                lifecycle.wait_for(lambda: self.jobs()[0]['status'] == 'Queue')
                job = self.jobs()[0]
                self.assertIsNone(job['claim'])
                self.assertIsNone(job['worker'])
                self.assertIsNone(job['started_at'])
                self.assertFalse(marker.exists())
                self.assertIsNone(processes[0][0].poll())
        finally:
            for process, release in processes:
                if process.poll() is None:
                    release.touch()
                    process.wait(timeout=5)

    def test_incomplete_and_stale_observations_never_claim(self):
        self.add()
        with exclusive(self.store.root / 'controller.lock'):
            with self.store.transaction() as con:
                control = self.store.control(con)
                control.update(owner=identity(), dispatch=True, stop=False)
                self.store.set_control(con, control)
            for label in ('UNCERTAIN', 'UNAVAILABLE', 'STALE_CLEAR'):
                with self.subTest(label=label):
                    guard = {'state': label if label != 'STALE_CLEAR' else 'NO_MATCH',
                             'reason': label, 'observed_at': time.time()-30,
                             'stale': label == 'STALE_CLEAR', 'coverage': {'complete': label == 'STALE_CLEAR'}}
                    with patch.object(activity.DEFAULT_OBSERVER, 'scan', return_value=guard):
                        runtime.tick(self.store)
                    self.assertEqual(self.jobs()[0]['status'], 'Queue')
                    self.assertIsNone(self.jobs()[0]['claim'])
                    self.assertIsNone(self.jobs()[0]['started_at'])

    def test_active_conflict_is_advisory_and_restart_reobserves(self):
        gate = self.root / 'managed-release'
        self.add(f"from pathlib import Path; import time; gate=Path({str(gate)!r})\nwhile not gate.exists(): time.sleep(.02)")
        self.add()
        self.run_queue()
        lifecycle.wait_for(lambda: self.jobs()[0]['status'] == 'Run')
        process, release = external_fixture(self)
        try:
            lifecycle.wait_for(lambda: self.store.snapshot()['controller'].get('external_guard', {}).get('state') == 'BUSY')
            self.assertEqual(self.jobs()[0]['status'], 'Run')
            self.assertIsNone(process.poll())
            gate.touch()
            lifecycle.wait_for(lambda: self.jobs()[0]['status'] == 'Done')
            self.assertEqual(self.jobs()[1]['status'], 'Queue')
            runtime.stop(self.store)
            lifecycle.wait_for(lambda: not self.store.snapshot()['controller']['online'])
            self.run_queue()
            lifecycle.wait_for(lambda: self.store.snapshot()['controller'].get('external_guard', {}).get('state') == 'BUSY')
            self.assertEqual(self.jobs()[1]['status'], 'Queue')
            release.touch()
            process.wait(timeout=5)
            self.completed(2)
        finally:
            gate.touch()
            release.touch()
            process.wait(timeout=5)


    def test_verified_managed_payload_excluded_but_same_executable_external_blocks(self):
        gate = self.root / 'managed-release'
        ready = self.root / 'managed-ready'
        self.add(f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); gate=Path({str(gate)!r})\nwhile not gate.exists(): time.sleep(.02)")
        self.run_queue()
        try:
            lifecycle.wait_for(ready.exists)
            process, release = external_fixture(self)
            executable = native_executable(psutil.Process(process.pid))
            group = self.jobs()[0]['group']
            managed_pids = [pid for pid in group_members(group['pid'])
                            if native_executable(psutil.Process(pid)) == executable]
            self.assertEqual(len(managed_pids), 1)
            records = [identity(managed_pids[0]), identity(process.pid)]
            (self.store.root / 'fixture-processes.json').write_text(json.dumps(records))
            lifecycle.wait_for(lambda: self.store.snapshot()['controller'].get('external_guard', {}).get('coverage', {}).get('managed', 0) == 1)
            guard = self.store.snapshot()['controller']['external_guard']
            self.assertEqual(guard['state'], 'BUSY')
            self.assertTrue(guard['coverage']['complete'])
            self.assertEqual([row['pid'] for row in guard['observations']], [process.pid])
            self.assertEqual(self.jobs()[0]['status'], 'Run')
            self.assertIsNone(process.poll())
            release.touch()
            process.wait(timeout=5)
            lifecycle.wait_for(lambda: self.store.snapshot()['controller'].get('external_guard', {}).get('state') == 'NO_MATCH')
            self.assertEqual(self.jobs()[0]['status'], 'Run')
            gate.touch()
            self.completed(1)
            self.assertEqual(self.jobs()[0]['status'], 'Done')
        finally:
            gate.touch()


if __name__ == '__main__':
    unittest.main()
