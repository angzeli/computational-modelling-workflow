"""Nested MPI observations at both real admission gates, using a fake process table.

Only the auxiliary payload and its guardian are real owned benign processes.
The external VASP/launcher/session records never create processes or scan the host.
"""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
import unittest

import psutil

from cmw.jobs import activity, runtime, sharing
from cmw.jobs.ownership import exclusive, identity
from tests.jobs.isolated_runtime import FixtureAdmissionSampler, environment
from tests.jobs.test_activity import owner, process
from tests.jobs import test_jobs as lifecycle
from tests.jobs import test_sharing_runtime as sharing_lifecycle


SESSION = 91000001
LAUNCHER = 91000002
WRAPPER = 91000011
RANKS = tuple(range(91000003, 91000011))
CHILDREN = []


def nested_collect(store, state, deadline):
    external = store.root.parent / 'external-family'
    payload = Path(activity.__file__).with_name('payload.sh').resolve()
    processes = {
        SESSION: process(SESSION, '/bin/bash', parent=91000000, group=SESSION, session=SESSION),
        WRAPPER: process(WRAPPER, '/fixture/python', parent=SESSION, group=SESSION, session=SESSION),
        LAUNCHER: process(LAUNCHER, '/fixture/prterun', parent=WRAPPER, group=SESSION, session=SESSION),
        **{pid: process(pid, '/fixture/vasp_std', parent=LAUNCHER,
                       group=SESSION, session=SESSION) for pid in RANKS},
    }
    processes[SESSION].cmdline.return_value = [
        '/bin/bash', str(payload), sys.executable, str(payload.parents[2]),
        str(external / 'payload-exit.json'), str(external / 'stdout.log'),
        str(external / 'stderr.log'), '/fixture/prterun', '-n', '8', '/fixture/vasp_std']
    processes[LAUNCHER].cmdline.return_value = ['/fixture/prterun', '-n', '8', '/fixture/vasp_std']
    for pid, row in processes.items():
        row.cwd.return_value = str(external)
        row.terminal.return_value = None
        if pid in RANKS:
            row.cmdline.return_value = ['/fixture/vasp_std']

    def factory(pid):
        if pid not in processes:
            raise psutil.NoSuchProcess(pid)
        return processes[pid]

    # Fallbacks keep the genuine guardian/payload lifecycle untouched if another
    # thread verifies one of our real owned process identities during this scan.
    actual_identity = activity.identity
    actual_executable = activity.native_executable
    actual_group, actual_session = os.getpgid, os.getsid
    with ExitStack() as patches:
        patches.enter_context(patch.object(activity, 'identity', side_effect=lambda pid:
            owner(pid) if pid in processes else actual_identity(pid)))
        patches.enter_context(patch.object(activity, 'native_executable', side_effect=lambda p:
            p.exe_value if p.pid in processes else actual_executable(p)))
        patches.enter_context(patch.object(activity.os, 'getpgid', side_effect=lambda pid:
            processes[pid].group_value if pid in processes else actual_group(pid)))
        patches.enter_context(patch.object(activity.os, 'getsid', side_effect=lambda pid:
            processes[pid].session_value if pid in processes else actual_session(pid)))
        return activity.collect(store, state, deadline,
            pids_provider=lambda: list(processes), process_factory=factory)


class RecordingAdmissionSampler(FixtureAdmissionSampler):
    def collect(self, store, *, source='controller', wait=False):
        guard, evidence = super().collect(store, source=source, wait=wait)
        record = {'source': source, 'collector_pid': os.getpid(), 'state': guard['state'],
                  'observations': [{'id': row['id'], 'nproc': row['nproc'],
                                    'fingerprint': sharing.fingerprint(row)}
                                   for row in guard['observations']]}
        with (store.root / 'family-admission-trace.jsonl').open('a') as stream:
            stream.write(json.dumps(record)+'\n')
        return guard, evidence


def detached(arguments, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    runtime.reap_detached()
    with log.open('ab', buffering=0) as output:
        child = subprocess.Popen(
            [sys.executable, '-m', 'tests.jobs.test_mpi_family_runtime', '_runtime', *arguments],
            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            start_new_session=True, close_fds=True, env=environment(), cwd='/')
    runtime._CHILDREN.append(child)
    CHILDREN.append(child)
    return child


def install():
    patches = ExitStack()
    patches.enter_context(patch.object(activity, 'DEFAULT_OBSERVER', activity.Observer(collector=nested_collect)))
    patches.enter_context(patch.object(sharing, 'AdmissionSampler', RecordingAdmissionSampler))
    patches.enter_context(patch.object(runtime, 'detached', detached))
    return patches


class MPIFamilyRuntimeTests(unittest.TestCase):
    setUp_base = sharing_lifecycle.SharingRuntimeTests.setUp
    jobs = sharing_lifecycle.SharingRuntimeTests.jobs
    add_gate = sharing_lifecycle.SharingRuntimeTests.add_gate
    job = sharing_lifecycle.SharingRuntimeTests.job
    stored_job = sharing_lifecycle.SharingRuntimeTests.stored_job
    running = sharing_lifecycle.SharingRuntimeTests.running
    release = sharing_lifecycle.SharingRuntimeTests.release

    def setUp(self):
        self.setUp_base()
        (self.root / 'external-family').mkdir()
        self.family_isolation = install()
        self.addCleanup(self.family_isolation.close)
        self.sampler = RecordingAdmissionSampler()
        self.addCleanup(runtime._ADMISSION_SAMPLERS.pop, str(self.store.root), None)
        runtime._ADMISSION_SAMPLERS[str(self.store.root)] = self.sampler

    def tearDown(self):
        sharing_lifecycle.SharingRuntimeTests.tearDown(self)
        for child in CHILDREN:
            child.wait(timeout=5)
        CHILDREN.clear()
        runtime.reap_detached()

    @contextmanager
    def controller_authority(self):
        with exclusive(self.store.root / 'controller.lock'):
            with self.store.transaction() as con:
                control = self.store.control(con)
                control.update(owner=identity(), dispatch=True, stop=False, heartbeat=time.time())
                self.store.set_control(con, control)
            yield

    def reserve_family(self):
        guard = activity.DEFAULT_OBSERVER.scan(self.store, source='test')
        self.assertEqual(guard['state'], 'BUSY')
        self.assertEqual(len(guard['observations']), 1)
        self.assertEqual(guard['observations'][0]['nproc'], 8)
        self.store.set_external_reservation(sharing.make_reservation(
            guard['observations'][0], guard, 2, 1, self.root / 'external-family'))

    def traces(self):
        path = self.store.root / 'family-admission-trace.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def assert_same_independent_family(self):
        traces = self.traces()
        controller = next(row for row in traces if row['source'] == 'controller')
        supervisor = next(row for row in traces if row['source'] == 'supervisor-admission')
        self.assertNotEqual(controller['collector_pid'], supervisor['collector_pid'])
        self.assertEqual(controller['state'], supervisor['state'])
        self.assertEqual(controller['state'], 'BUSY')
        self.assertEqual(len(controller['observations']), 1)
        self.assertEqual(controller['observations'][0]['nproc'], 8)
        self.assertEqual(controller['observations'], supervisor['observations'])

    def test_controller_missing_reservation_does_not_start_payload(self):
        auxiliary = self.add_gate('auxiliary', auxiliary=True)
        with self.controller_authority():
            runtime.tick(self.store)
            row = self.job(auxiliary)
            self.assertEqual(row['status'], 'Queue')
            self.assertIsNone(row['claim'])
            self.assertIsNone(row['started_at'])
            guard, evidence = self.sampler.collect(self.store)
            decision = sharing.evaluate(self.store.snapshot(), guard, evidence)
            self.assertEqual(decision['reason'], 'External workload has no valid coexistence reservation')
            self.assertEqual([row['source'] for row in self.traces()], ['controller', 'controller'])
            self.assertFalse(self.gates[auxiliary['id']]['count'].exists())

    def test_valid_family_reservation_passes_both_gates_and_runs_one_auxiliary(self):
        first = self.add_gate('first-auxiliary', auxiliary=True)
        second = self.add_gate('second-auxiliary', auxiliary=True)
        self.reserve_family()
        with self.controller_authority():
            runtime.tick(self.store)
            self.running(first)
            self.assert_same_independent_family()
            runtime.tick(self.store)
            self.assertEqual(self.job(second)['status'], 'Queue')
            self.assertFalse(self.gates[second['id']]['count'].exists())
            self.assertEqual(self.gates[first['id']]['count'].read_text(), 'x')
            self.release(first)
            lifecycle.wait_for(lambda: self.job(first)['status'] == 'Done')

    def test_final_gate_independently_refuses_removed_family_reservation(self):
        auxiliary = self.add_gate('auxiliary', auxiliary=True)
        self.reserve_family()

        def remove_reservation_before_worker(arguments, log):
            self.store.set_external_reservation(None)
            return detached(arguments, log)

        with self.controller_authority():
            with patch.object(runtime, 'detached', remove_reservation_before_worker):
                runtime.tick(self.store)
            lifecycle.wait_for(lambda: self.stored_job(auxiliary)['reason'] ==
                               'External workload has no valid coexistence reservation')
            self.assert_same_independent_family()
            row = self.job(auxiliary)
            self.assertEqual(row['status'], 'Queue')
            self.assertIsNone(row['claim'])
            self.assertIsNone(row['worker'])
            self.assertIsNone(row['started_at'])
            self.assertFalse(self.gates[auxiliary['id']]['count'].exists())


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '_runtime':
        del sys.argv[1]
        with install():
            runtime.main()
    else:
        unittest.main()
