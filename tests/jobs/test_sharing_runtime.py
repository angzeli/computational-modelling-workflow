"""Real bounded-sharing lifecycles with isolated process and host evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import subprocess
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psutil

from cmw.jobs import activity, runtime, sharing
from cmw.jobs.ownership import exclusive, identity, owner_alive
from tests.jobs import test_jobs as lifecycle
from tests.jobs.isolated_runtime import FixtureAdmissionSampler, detached


class SharingRuntimeTests(unittest.TestCase):
    def setUp(self):
        lifecycle.QueueTests.setUp(self)
        self.gates = {}
        self.external = []
        with patch('psutil.cpu_count', return_value=64), patch('psutil.virtual_memory',
                return_value=SimpleNamespace(total=64*2**30)):
            self.store.configure_sharing('bounded-sharing', cpu_budget=4, memory_gib=4,
                                         cpu_reserve=0, min_available_gib=0)

    def tearDown(self):
        for gate in self.gates.values():
            gate['release'].touch()
        for process, gate in self.external:
            gate.touch()
            process.wait(timeout=5)
        lifecycle.QueueTests.tearDown(self)

    jobs = lifecycle.QueueTests.jobs
    run_queue = lifecycle.QueueTests.run_queue
    completed = lifecycle.QueueTests.completed

    def add_gate(self, name, *, auxiliary=False, hold=False, exit_code=0, on_failure='pause'):
        cwd = self.root/name
        cwd.mkdir()
        gate = {key: cwd/key for key in ('ready', 'release', 'count')}
        code = ("from pathlib import Path; import time; "
                f"count=Path({str(gate['count'])!r}); count.write_text(count.read_text()+'x' if count.exists() else 'x'); "
                f"Path({str(gate['ready'])!r}).touch(); gate=Path({str(gate['release'])!r})\n"
                "while not gate.exists(): time.sleep(.02)\n"
                f"raise SystemExit({exit_code})")
        declaration = ({'role':'auxiliary', 'independent':True, 'resource_contract':'trusted-declared'}
                       if auxiliary else {'allow_auxiliary':True})
        job = self.store.add(argv=[sys.executable, '-c', code], cwd=cwd, name=name,
                             cpus=1 if auxiliary else 2, memory_gib=1, hold=hold,
                             on_failure=on_failure, **declaration)
        self.gates[job['id']] = gate
        return job

    def job(self, job):
        return next(row for row in self.jobs() if row['id'] == job['id'])

    def stored_job(self, job):
        with closing(sqlite3.connect(self.store.path.as_uri()+'?mode=ro', uri=True)) as con:
            return json.loads(con.execute('SELECT data FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])

    def running(self, job):
        lifecycle.wait_for(lambda: self.job(job)['status'] == 'Run' and self.gates[job['id']]['ready'].exists())

    def release(self, job):
        self.gates[job['id']]['release'].touch()

    def unchanged_queue(self, jobs):
        # Several controller cycles establish that an occupied/vetoed slot does
        # not accidentally launch a second process after the first assertion.
        deadline = time.monotonic()+.4
        while time.monotonic() < deadline:
            for job in jobs:
                row = self.job(job)
                self.assertEqual(row['status'], 'Queue')
                self.assertIsNone(row['started_at'])
                self.assertFalse(self.gates[job['id']]['ready'].exists())
            time.sleep(.05)

    def external_fixture(self, name):
        cwd = self.root/name
        cwd.mkdir()
        ready, release = cwd/'ready', cwd/'release'
        code = (f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); p=Path({str(release)!r})\n"
                "while not p.exists(): time.sleep(.02)")
        process = subprocess.Popen([sys.executable, '-c', code], cwd=cwd)
        self.external.append((process, release))
        lifecycle.wait_for(ready.exists)
        executable = activity.native_executable(psutil.Process(process.pid))
        (self.store.root/'external-activity.json').write_text(json.dumps({'executables':{executable:'VASP'}}))
        self.refresh_external_pids()
        return process, release, cwd

    def refresh_external_pids(self):
        records = [identity(process.pid) for process, _ in self.external if process.poll() is None]
        (self.store.root/'fixture-processes.json').write_text(json.dumps(records))

    def reserve(self, process):
        guard = activity.DEFAULT_OBSERVER.scan(self.store, source='test')
        observed = next(row for row in guard['observations'] if row['pid'] == process.pid)
        self.store.set_external_reservation(sharing.make_reservation(observed, guard, 2, 1))
        return guard

    def test_primary_auxiliary_overlap_and_lingering_auxiliary_blocks_next_window(self):
        primary = self.add_gate('primary')
        next_primary = self.add_gate('next-primary')
        auxiliary = self.add_gate('auxiliary', auxiliary=True)
        next_auxiliary = self.add_gate('next-auxiliary', auxiliary=True)
        self.run_queue()
        self.running(primary)
        self.running(auxiliary)
        self.unchanged_queue([next_primary, next_auxiliary])
        self.assertEqual(self.job(auxiliary)['sharing_anchor']['attempt_id'], primary['attempt_id'])
        self.release(primary)
        lifecycle.wait_for(lambda: self.job(primary)['status'] == 'Done')
        self.unchanged_queue([next_primary, next_auxiliary])
        self.release(auxiliary)
        self.running(next_primary)
        self.running(next_auxiliary)
        self.assertEqual(self.job(next_auxiliary)['sharing_anchor']['attempt_id'], next_primary['attempt_id'])
        self.release(next_primary)
        self.release(next_auxiliary)
        self.completed(4)
        self.assertTrue(all(gate['count'].read_text() == 'x' for gate in self.gates.values()))

    def test_auxiliary_hold_order_and_explicit_pause(self):
        primary = self.add_gate('primary')
        first = self.add_gate('first-aux', auxiliary=True, hold=True)
        second = self.add_gate('second-aux', auxiliary=True)
        self.run_queue()
        self.running(primary)
        self.unchanged_queue([second])
        self.store.dispatch(False)
        self.store.change(first['id'], 'release')
        self.unchanged_queue([first, second])
        self.store.dispatch(True)
        self.running(first)
        self.unchanged_queue([second])
        self.release(first)
        self.running(second)
        self.release(second)
        self.release(primary)
        self.completed(3)
        self.assertLessEqual(self.job(first)['finished_at'], self.job(second)['started_at'])

    def test_stop_restart_reconciles_both_active_attempts_without_duplicate(self):
        primary = self.add_gate('primary')
        auxiliary = self.add_gate('auxiliary', auxiliary=True)
        self.run_queue()
        self.running(primary)
        self.running(auxiliary)
        before = {job['id']: (job['worker'], job['group']) for job in self.jobs()}
        runtime.stop(self.store)
        lifecycle.wait_for(lambda: not self.store.snapshot()['controller']['online'])
        self.run_queue()
        self.assertEqual({job['id']: (job['worker'], job['group']) for job in self.jobs()}, before)
        self.release(primary)
        self.release(auxiliary)
        self.completed(2)
        self.assertTrue(all(gate['count'].read_text() == 'x' for gate in self.gates.values()))

    def test_auxiliary_failure_and_cancellation_do_not_touch_primary(self):
        primary = self.add_gate('primary')
        failed = self.add_gate('failed-aux', auxiliary=True, exit_code=7)
        cancelled = self.add_gate('cancelled-aux', auxiliary=True)
        self.run_queue()
        self.running(primary)
        self.running(failed)
        primary_owner = self.job(primary)['group']
        self.release(failed)
        lifecycle.wait_for(lambda: self.job(failed)['status'] == 'Fail')
        self.assertEqual(self.job(failed)['exit_code'], 7)
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])
        self.assertEqual(self.job(primary)['status'], 'Run')
        self.unchanged_queue([cancelled])
        self.store.dispatch(True)
        self.running(cancelled)
        self.store.change(cancelled['id'], 'cancel', confirm=True)
        lifecycle.wait_for(lambda: self.job(cancelled)['status'] == 'Cancelled')
        self.assertTrue(owner_alive(primary_owner))
        self.assertEqual(self.job(primary)['status'], 'Run')
        self.release(primary)
        self.completed(3)

    def test_primary_failure_preserves_auxiliary_and_pauses_next_primary(self):
        primary = self.add_gate('failed-primary', exit_code=7)
        auxiliary = self.add_gate('auxiliary', auxiliary=True)
        next_primary = self.add_gate('next-primary')
        self.run_queue()
        self.running(primary)
        self.running(auxiliary)
        auxiliary_owner = self.job(auxiliary)['group']
        self.release(primary)
        lifecycle.wait_for(lambda: self.job(primary)['status'] == 'Fail')
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])
        self.assertEqual(self.job(auxiliary)['status'], 'Run')
        self.assertTrue(owner_alive(auxiliary_owner))
        self.store.dispatch(True)
        self.unchanged_queue([next_primary])
        self.release(auxiliary)
        self.running(next_primary)
        self.release(next_primary)
        self.completed(3)

    def test_external_reservation_stays_busy_and_expires_without_transfer(self):
        original, release, cwd = self.external_fixture('external')
        first = self.add_gate('first-aux', auxiliary=True)
        second = self.add_gate('second-aux', auxiliary=True)
        self.run_queue()
        self.unchanged_queue([first, second])
        self.assertEqual(self.reserve(original)['state'], 'BUSY')
        self.running(first)
        self.assertIsNone(original.poll())
        self.assertEqual(self.store.snapshot()['controller']['external_guard']['state'], 'BUSY')
        runtime.stop(self.store)
        lifecycle.wait_for(lambda: not self.store.snapshot()['controller']['online'])
        self.run_queue()
        self.assertEqual(self.job(first)['status'], 'Run')
        extra, extra_release, _ = self.external_fixture('unrelated-external')
        self.release(first)
        lifecycle.wait_for(lambda: self.job(first)['status'] == 'Done')
        self.unchanged_queue([second])
        release.touch()
        original.wait(timeout=5)
        self.refresh_external_pids()
        self.unchanged_queue([second])
        self.assertIsNone(extra.poll())
        self.reserve(extra)
        self.running(second)
        self.release(second)
        self.completed(2)
        self.assertIsNone(extra.poll())
        extra_release.touch()
        extra.wait(timeout=5)

    def test_primary_cancellation_leaves_auxiliary_alive_until_its_own_release(self):
        primary = self.add_gate('primary')
        auxiliary = self.add_gate('auxiliary', auxiliary=True)
        next_primary = self.add_gate('next-primary')
        self.run_queue()
        self.running(primary)
        self.running(auxiliary)
        primary_owner = self.job(primary)['group']
        auxiliary_owner = self.job(auxiliary)['group']
        self.store.change(primary['id'], 'cancel', confirm=True)
        lifecycle.wait_for(lambda: self.job(primary)['status'] == 'Cancelled')
        self.assertFalse(owner_alive(primary_owner))
        self.assertTrue(owner_alive(auxiliary_owner))
        self.assertEqual(self.job(auxiliary)['status'], 'Run')
        guard, evidence = FixtureAdmissionSampler().collect(self.store)
        decision = sharing.evaluate(self.store.snapshot(), guard, evidence)
        self.assertEqual(decision['cpu_committed'], 1)
        self.assertEqual(decision['memory_committed_gib'], 1)
        self.store.dispatch(True)
        self.unchanged_queue([next_primary])
        self.release(auxiliary)
        self.running(next_primary)
        self.assertLessEqual(self.job(auxiliary)['finished_at'], self.job(next_primary)['started_at'])
        self.release(next_primary)
        self.completed(3)

    def test_external_exit_expires_reservation_but_lingering_auxiliary_blocks_primary(self):
        original, external_release, _ = self.external_fixture('original-external')
        auxiliary = self.add_gate('auxiliary', auxiliary=True)
        next_primary = self.add_gate('next-primary')
        self.reserve(original)
        self.run_queue()
        self.running(auxiliary)
        auxiliary_owner = self.job(auxiliary)['group']
        external_release.touch()
        original.wait(timeout=5)
        self.refresh_external_pids()
        guard, evidence = FixtureAdmissionSampler().collect(self.store)
        decision = sharing.evaluate(self.store.snapshot(), guard, evidence)
        self.assertEqual(guard['state'], 'NO_MATCH')
        self.assertEqual(decision['external_reservation_state'], 'expired')
        self.assertFalse(decision['permitted'])
        self.assertEqual(decision['cpu_committed'], 1)
        self.assertTrue(owner_alive(auxiliary_owner))
        self.assertEqual(self.job(auxiliary)['status'], 'Run')
        self.unchanged_queue([next_primary])
        self.release(auxiliary)
        self.running(next_primary)
        self.assertLessEqual(self.job(auxiliary)['finished_at'], self.job(next_primary)['started_at'])
        self.release(next_primary)
        self.completed(2)

    def test_final_go_rechecks_independent_cpu_and_memory_evidence(self):
        primary = self.add_gate('primary')
        self.run_queue()
        self.running(primary)
        for index, override in enumerate(({'cpu_percent':100}, {'available_gib':0})):
            path = self.store.root/'sharing-admission.json'
            path.write_text(json.dumps({'supervisor-admission':override}))
            auxiliary = self.add_gate('aux-'+str(index), auxiliary=True)
            lifecycle.wait_for(lambda: self.stored_job(auxiliary)['reason'].startswith(('Host CPU', 'Available RAM')))
            self.store.dispatch(False)
            lifecycle.wait_for(lambda: self.job(auxiliary)['status'] == 'Queue')
            self.assertEqual(self.job(auxiliary)['status'], 'Queue')
            self.assertIsNone(self.job(auxiliary)['claim'])
            self.assertFalse(self.gates[auxiliary['id']]['ready'].exists())
            path.unlink()
            self.store.dispatch(True)
            self.running(auxiliary)
            self.release(auxiliary)
            lifecycle.wait_for(lambda: self.job(auxiliary)['status'] == 'Done')
        self.release(primary)
        self.completed(3)

    def test_two_simultaneous_claim_transactions_admit_only_one_auxiliary(self):
        primary = self.add_gate('primary')
        self.run_queue()
        self.running(primary)
        runtime.stop(self.store)
        lifecycle.wait_for(lambda: not self.store.snapshot()['controller']['online'])
        first = self.add_gate('first-aux', auxiliary=True)
        second = self.add_gate('second-aux', auxiliary=True)
        with exclusive(self.store.root/'controller.lock'):
            with self.store.transaction() as con:
                control = self.store.control(con)
                control.update(owner=identity(), dispatch=True, stop=False, heartbeat=time.time())
                self.store.set_control(con, control)
            guard, evidence = FixtureAdmissionSampler().collect(self.store)
            barrier = threading.Barrier(2)

            class SimultaneousEvidence:
                def collect(self, store, *, source='controller', wait=False):
                    barrier.wait(timeout=5)
                    return guard, evidence

            with patch.dict(runtime._ADMISSION_SAMPLERS, {str(self.store.root): SimultaneousEvidence()}):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: runtime.tick(self.store), range(2)))
            self.assertEqual(results, [True, True])
            self.running(first)
            self.unchanged_queue([second])
            self.assertEqual(sum(job['status'] in {'Starting', 'Run'} for job in self.jobs()), 2)
            self.assertEqual(self.gates[first['id']]['count'].read_text(), 'x')
            self.release(primary)
            self.release(first)
            lifecycle.wait_for(lambda: self.job(primary)['status'] == self.job(first)['status'] == 'Done')

    def test_final_go_rechecks_new_unreserved_external_identity(self):
        original, _, _ = self.external_fixture('original-external')
        waiting_primary = self.add_gate('waiting-primary')
        auxiliary = self.add_gate('auxiliary', auxiliary=True)
        self.reserve(original)

        def insert_external(arguments, log):
            self.external_fixture('new-external')
            return detached(arguments, log)

        with exclusive(self.store.root/'controller.lock'):
            with self.store.transaction() as con:
                control = self.store.control(con)
                control.update(owner=identity(), dispatch=True, stop=False, heartbeat=time.time())
                self.store.set_control(con, control)
            with patch.object(runtime, 'detached', insert_external):
                runtime.tick(self.store)
            lifecycle.wait_for(lambda: self.stored_job(auxiliary)['reason'].startswith('Additional external'))
            row = self.job(auxiliary)
            self.assertEqual(row['status'], 'Queue')
            self.assertIsNone(row['claim'])
            self.assertIsNone(row['worker'])
            self.assertIsNone(row['started_at'])
            self.assertEqual(self.job(waiting_primary)['order'], 1)
            self.assertEqual(row['order'], 2)
            self.assertFalse(self.gates[auxiliary['id']]['ready'].exists())
            self.assertTrue(all(process.poll() is None for process, _ in self.external))


if __name__ == '__main__':
    unittest.main()
