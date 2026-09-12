"""Real leaderless subgroups in isolated, explicitly owned Jobs sessions."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from cmw.jobs import runtime, sharing
from cmw.jobs.ownership import group_members, identity, owner_alive
from cmw.jobs.store import JobsError, Store, TERMINAL
from tests.jobs.isolated_runtime import install
from tests.jobs.lifecycle_evidence import retain_lifecycle_evidence
from tests.jobs.test_jobs import wait_for


class SessionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(install().close)
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-session-')
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.sentinel = None
        self.owners = []

    def tearDown(self):
        (self.root/'release').touch()
        (self.root/'sentinel-release').touch()
        try:
            if self.store.path.exists():
                for job in self.store.snapshot()['jobs']:
                    if job['status'] in {'Run', 'Starting'}:
                        try:
                            self.store.change(job['id'], 'cancel', confirm=True)
                        except JobsError:
                            current = next(j for j in self.store.snapshot()['jobs'] if j['id'] == job['id'])
                            if current['status'] not in TERMINAL:
                                raise
                wait_for(lambda: all(j['status'] not in {'Starting', 'Run', 'Cancelling'}
                                     for j in self.store.snapshot()['jobs']))
        finally:
            try:
                if self.store.path.exists():
                    controller = self.store.snapshot()['controller'].get('owner')
                    runtime.stop(self.store)
                    wait_for(lambda: not self.store.snapshot()['controller']['online'])
                    for child in runtime._CHILDREN:
                        if controller and child.pid == controller['pid']:
                            child.wait(timeout=5)
            finally:
                if self.sentinel:
                    self.sentinel.wait(timeout=5)
                wait_for(lambda: not any(owner_alive(owner) for owner in self.owners), timeout=5)
                runtime.reap_detached()
                self.temp.cleanup()

    def exercise(self, mode):
        sentinel_code = ("import signal,time; from pathlib import Path; "
            f"signal.signal(signal.SIGTERM,lambda *args: Path({str(self.root/'sentinel-term')!r}).touch()); "
            f"Path({str(self.root/'sentinel-ready')!r}).touch(); end=time.monotonic()+30\n"
            f"while not Path({str(self.root/'sentinel-release')!r}).exists() and time.monotonic()<end: time.sleep(.02)")
        self.sentinel = subprocess.Popen([sys.executable, '-c', sentinel_code], start_new_session=True)
        sentinel_owner = identity(self.sentinel.pid)
        wait_for((self.root/'sentinel-ready').exists)
        job = self.store.add(argv=[sys.executable, '-m', 'tests.jobs.session_payload',
                                  'coordinator', str(self.root), mode],
                             cwd=self.root, name='leaderless synthetic session',
                             env={'CMW_JOBS_OWN_SESSION': '1'}, cpus=1)
        runtime.start(self.store)
        wait_for((self.root/'topology.json').exists)
        topology = json.loads((self.root/'topology.json').read_text())
        current = self.store.snapshot()['jobs'][0]
        sid = current['group']['pid']
        survivor = topology['survivor']['identity']
        self.owners = [topology['coordinator'], topology['leader'], survivor, current['group'], current['worker']]
        self.assertEqual(topology['leader_returncode'], 0)
        with self.assertRaises(ProcessLookupError):
            os.getpgid(topology['leader']['pid'])
        self.assertEqual(identity(survivor['pid']), survivor)
        self.assertEqual(os.getsid(survivor['pid']), sid)
        self.assertEqual(os.getpgid(survivor['pid']), topology['leader']['pid'])
        self.assertNotEqual(os.getsid(self.sentinel.pid), sid)
        self.store.change(job['id'], 'cancel', confirm=True)
        wait_for((self.root/'term-seen').exists)
        if mode == 'resistant':
            self.assertTrue(owner_alive(survivor))
            cancelling = self.store.snapshot()
            self.assertEqual(cancelling['jobs'][0]['status'], 'Cancelling')
            self.assertEqual(sharing.evaluate(cancelling, {})['cpu_committed'], 1)
        wait_for(lambda: self.store.snapshot()['jobs'][0]['status'] == 'Cancelled')
        finished = self.store.snapshot()['jobs'][0]
        self.assertFalse(owner_alive(survivor))
        self.assertFalse(group_members(sid, session=True))
        self.assertEqual(sharing.evaluate(self.store.snapshot(), {})['cpu_committed'], 0)
        if mode == 'resistant':
            self.assertEqual((finished['exit_code'], finished['signal']), (137, signal.SIGKILL))
        self.assertEqual(identity(self.sentinel.pid), sentinel_owner)
        self.assertIsNone(self.sentinel.poll())
        self.assertFalse((self.root/'sentinel-term').exists())

    @retain_lifecycle_evidence
    def test_cooperative_leaderless_subgroup_receives_term(self):
        self.exercise('cooperative')

    @retain_lifecycle_evidence
    def test_resistant_leaderless_subgroup_receives_kill_before_release(self):
        self.exercise('resistant')
