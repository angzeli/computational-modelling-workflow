from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cmw.jobs.cli import elapsed, safe_text, status_text, tail
from cmw.jobs.ownership import exclusive, group_members, identity, owner_alive
from cmw.jobs.runtime import launch_blocker, reap_detached, reconcile, start, stop
from cmw.jobs.store import JobsError, Store

ROOT = Path(__file__).resolve().parents[2]


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError("Timed out waiting for synthetic lifecycle")


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cmw jobs 空格 ")
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.started = False

    def tearDown(self):
        if self.store.path.exists():
            for j in self.jobs():
                if j['status'] in {'Run', 'Starting'}:
                    self.store.change(j['id'], 'cancel', confirm=True)
            try:
                wait_for(lambda: not any(j['status'] in {'Run','Starting','Cancelling'} for j in self.jobs()), timeout=9)
            finally:
                stop(self.store)
                wait_for(lambda: not self.store.snapshot()['controller']['online'])
        reap_detached()
        self.temp.cleanup()

    def jobs(self):
        return self.store.snapshot()['jobs']

    def add(self, code="print('ok')", **kwargs):
        return self.store.add(argv=[sys.executable, '-c', code], cwd=self.root,
                              name=kwargs.pop('name', 'synthetic'), **kwargs)

    def run_queue(self):
        start(self.store)
        self.started = True

    def completed(self, count):
        return wait_for(lambda: len(self.jobs()) == count and all(j['status'] in {'Done','Fail','Cancelled'} for j in self.jobs()))

    def test_sequential_order_logs_unicode_and_fixed_elapsed(self):
        for name in 'ABC':
            self.add("import sys,time; print('你好 stdout'); print('stderr',file=sys.stderr); time.sleep(.15)", name=name)
        self.assertFalse(self.store.snapshot()['controller']['online'])
        self.assertTrue(all(j['started_at'] is None for j in self.jobs()))
        self.run_queue()
        self.completed(3)
        jobs = self.jobs()
        self.assertEqual([j['status'] for j in jobs], ['Done']*3)
        for first, second in zip(jobs, jobs[1:]):
            self.assertLessEqual(first['finished_at'], second['started_at'])
        self.assertIn('你好 stdout', tail(jobs[0]['logs']['stdout']))
        self.assertIn('stderr', tail(jobs[0]['logs']['stderr']))
        self.assertEqual(self.store.snapshot(now=time.time()+100)['jobs'][0]['elapsed'], jobs[0]['elapsed'])
        self.assertTrue(all(j['check'] is None for j in jobs))

    def test_hold_order_pause_and_restart(self):
        release = self.root/'release-paused-job'
        a = self.add(f"import time; from pathlib import Path; gate=Path({str(release)!r})\n"
                     "while not gate.exists(): time.sleep(.05)", name='A', hold=True)
        b = self.add(name='B')
        c = self.add(name='C')
        self.run_queue()
        time.sleep(.3)
        self.assertTrue(all(j['started_at'] is None for j in self.jobs()))
        self.store.change(c['id'], 'move', position=2)
        self.store.change(a['id'], 'release')
        wait_for(lambda: self.jobs()[0]['status']=='Run')
        with self.assertRaises(JobsError):
            self.store.change(a['id'], 'move', position=1)
        self.store.dispatch(False)
        self.assertEqual(self.jobs()[0]['status'], 'Run')
        release.touch()
        wait_for(lambda: self.jobs()[0]['status']=='Done')
        self.assertEqual([j['order'] for j in self.jobs()[1:]], [2,1])
        stop(self.store)
        wait_for(lambda: not self.store.snapshot()['controller']['online'])
        self.run_queue()
        self.completed(3)
        self.assertLess(self.jobs()[2]['started_at'], self.jobs()[1]['started_at'])

    def test_failure_pause_and_continue_and_shell_failure(self):
        self.add('raise SystemExit(7)')
        second = self.add('raise SystemExit(9)', on_failure='continue')
        self.add()
        self.run_queue()
        wait_for(lambda: self.jobs()[0]['status']=='Fail')
        self.assertEqual(self.jobs()[0]['exit_code'], 7)
        self.assertIsNone(self.jobs()[1]['started_at'])
        self.store.dispatch(True)
        self.completed(3)
        self.assertEqual(self.jobs()[1]['exit_code'], 9)
        script = self.root / 'failure 空格.sh'
        script.write_text('false\necho misleading success\n')
        self.store.add(argv=['/bin/bash',str(script)],cwd=self.root,name='script')
        self.completed(4)
        self.assertEqual(self.jobs()[3]['status'], 'Fail')
        self.assertNotIn('misleading', tail(self.jobs()[3]['logs']['stdout']))

    def test_child_outlives_wrapper_and_cancellation_tree(self):
        marker = self.root/'child-finished'
        child = f"import time,pathlib; time.sleep(.8); pathlib.Path({str(marker)!r}).write_text('done')"
        self.add(f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])")
        self.add(f"from pathlib import Path; assert Path({str(marker)!r}).exists()")
        self.run_queue()
        self.completed(2)
        self.assertEqual([j['status'] for j in self.jobs()], ['Done','Done'])
        running = self.add("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); time.sleep(60)")
        pending = self.add()
        wait_for(lambda: self.jobs()[2]['status']=='Run')
        group = self.jobs()[2]['group']
        unrelated = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
        try:
            time.sleep(.2)
            with self.assertRaises(JobsError):
                self.store.change(running['id'],'cancel')
            self.store.change(pending['id'],'cancel')
            self.assertEqual(self.jobs()[2]['status'],'Run')
            self.store.change(running['id'],'cancel',confirm=True)
            self.assertEqual(self.jobs()[2]['status'],'Cancelling')
            self.completed(4)
            self.assertEqual(self.jobs()[2]['status'],'Cancelled')
            self.assertFalse(group_members(group['pid']))
            self.assertIsNone(unrelated.poll())
            self.assertFalse(self.store.snapshot()['controller']['dispatch'])
        finally:
            unrelated.terminate()
            unrelated.wait()

    def test_controller_restart_reconciles_live_worker_without_duplicate(self):
        marker = self.root/'count'
        release = self.root/'release-restarted-job'
        self.add(f"from pathlib import Path; import time; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); gate=Path({str(release)!r})\n"
                 "while not gate.exists(): time.sleep(.05)")
        self.add()
        self.run_queue()
        wait_for(lambda: marker.exists())
        owner = self.store.snapshot()['controller']['owner']
        self.assertTrue(owner_alive(owner))
        os.kill(owner['pid'],signal.SIGKILL)
        wait_for(lambda: not self.store.snapshot()['controller']['online'])
        self.run_queue()
        self.assertEqual(self.jobs()[0]['status'], 'Run')
        release.touch()
        self.completed(2)
        self.assertEqual(marker.read_text(),'x')
        self.assertEqual([j['status'] for j in self.jobs()],['Done','Done'])

    def test_concurrent_clients_singleton_and_order(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda n: self.add(name=str(n), hold=True), range(12)))
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda n: self.store.change(n,'move',position=1), range(1,13)))
        self.assertEqual(sorted(j['order'] for j in self.jobs()),list(range(1,13)))
        def launch(_):
            try:
                start(self.store)
                return 'started'
            except JobsError:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(launch,range(2)))
        self.assertEqual(sorted(results),['rejected','started'])

    def test_spawn_gap_stale_pid_unknown_never_continues(self):
        first = self.add(on_failure='continue')
        self.add()
        with self.store.transaction() as con:
            job = self.store.get(con,first['id'])
            stale = identity()
            stale['birth'] -= 100
            self.assertFalse(owner_alive(stale))
            job.update(status='Starting', order=None, claimed_at=time.time()-20, worker=stale, claim='crash')
            self.store.save(con,job)
            reconcile(self.store,con,job)
        self.assertEqual(self.jobs()[0]['status'],'Unknown')
        with self.assertRaises(JobsError):
            self.store.dispatch(True)
        self.run_queue()
        time.sleep(.3)
        self.assertIsNone(self.jobs()[1]['started_at'])
        # Simulate a crash immediately after intent commit, without a worker.
        with self.store.transaction() as con:
            job = self.store.get(con,first['id'])
            job.update(status='Starting', worker=None, claimed_at=time.time()-20)
            self.store.save(con,job)
            reconcile(self.store,con,job)
        self.assertEqual(self.jobs()[0]['status'],'Unknown')

    def test_launch_blocker_then_execution_failure(self):
        missing = self.root/'not executable'
        self.store.add(argv=[str(missing)],cwd=self.root,name='blocked')
        self.add()
        self.run_queue()
        wait_for(lambda: 'Executable' in self.jobs()[0]['reason'])
        self.assertIsNone(self.jobs()[1]['started_at'])
        missing.write_text('#!/no/such/interpreter\n')
        missing.chmod(0o700)
        wait_for(lambda: self.jobs()[0]['status']=='Fail')
        self.assertEqual(self.jobs()[0]['exit_code'],126 if sys.platform == 'darwin' else 127)
        self.assertIsNone(self.jobs()[1]['started_at'])

    def test_kernel_gate_catches_child_missed_by_enumeration(self):
        marker = self.root/'grandchild-ended'
        release = self.root/'release-missed-child'
        child = (f"import time; from pathlib import Path; gate=Path({str(release)!r})\n"
                 "while not gate.exists(): time.sleep(.05)\n"
                 f"Path({str(marker)!r}).touch()")
        first = self.add(f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])")
        self.add()
        with self.store.transaction() as con:
            job = self.store.get(con, first['id'])
            job.update(status='Starting', order=None, claim='enumeration-race', claimed_at=time.time())
            self.store.save(con,job)
        program = """
import sys
from cmw.jobs import runtime
from cmw.jobs.store import Store
runtime.group_members = lambda pid: [pid]
runtime.worker(Store(sys.argv[1]),1,'enumeration-race')
"""
        try:
            subprocess.run([sys.executable,'-c',program,str(self.store.root)],check=True,timeout=10,
                env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
            self.assertEqual(self.jobs()[0]['status'],'Unknown')
            self.assertIn('still contains live members',self.jobs()[0]['reason'])
            self.assertIsNone(self.jobs()[1]['started_at'])
        finally:
            release.touch()
            wait_for(marker.exists)
        from cmw.jobs.ownership import group_exists
        wait_for(lambda: not group_exists(self.jobs()[0]['group']['pid']))

    def test_real_worker_crash_boundaries_fail_closed(self):
        from cmw.core.provenance import read_json
        for boundary in ('before_spawn', 'after_spawn', 'before_go', 'before_finish'):
            with self.subTest(boundary=boundary):
                store = Store(self.root/boundary)
                marker = self.root/(boundary+'-ran')
                audit = self.root/(boundary+'-owner.json')
                first = store.add(argv=[sys.executable, '-c',
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('once')"],
                    cwd=self.root, name='crash boundary', on_failure='continue')
                store.add(argv=['/bin/echo','must not start'],cwd=self.root,name='next')
                with store.transaction() as con:
                    job = store.get(con,first['id'])
                    job.update(status='Starting', order=None, claim='test-claim', claimed_at=time.time())
                    store.save(con,job)
                program = r"""
import os,sys
from pathlib import Path
from cmw.jobs import runtime
from cmw.jobs.store import Store
from cmw.jobs.ownership import identity
from cmw.core.provenance import atomic_write_json
boundary, state, audit = sys.argv[1:]
original = runtime.subprocess.Popen
def spawn(*args, **kwargs):
    if boundary == 'before_spawn': os._exit(91)
    process = original(*args, **kwargs)
    atomic_write_json(Path(audit), identity(process.pid))
    if boundary == 'after_spawn': os._exit(92)
    if boundary == 'before_go':
        pipe = process.stdin
        class InterruptPipe:
            def write(self, value): os._exit(93)
            def close(self): pipe.close()
        process.stdin = InterruptPipe()
    return process
runtime.subprocess.Popen = spawn
if boundary == 'before_finish':
    runtime.finish = lambda *args, **kwargs: os._exit(94)
runtime.worker(Store(state),1,'test-claim')
"""
                process = subprocess.Popen([sys.executable,'-c',program,boundary,str(store.root),str(audit)],
                    env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'})
                try:
                    self.assertIn(process.wait(timeout=10), (91,92,93,94))
                    start(store)
                    wait_for(lambda: store.snapshot()['jobs'][0]['status']=='Unknown')
                    self.assertIsNone(store.snapshot()['jobs'][1]['started_at'])
                    self.assertEqual(marker.exists(),boundary=='before_finish')
                    self.assertFalse(store.snapshot()['controller']['dispatch'])
                finally:
                    # This audit belongs solely to our deliberately interrupted
                    # test worker. Never scan for or signal external engine names.
                    if audit.exists():
                        owned = read_json(audit)
                        if owner_alive(owned):
                            os.killpg(owned['pid'],signal.SIGKILL)
                            wait_for(lambda: not group_members(owned['pid']))
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    stop(store)
                    wait_for(lambda: not store.snapshot()['controller']['online'])
                    reap_detached()

    def test_path_lookup_matches_job_cwd_and_internal_tools_ignore_payload_path(self):
        binary = self.root/'relative bin'
        binary.mkdir()
        for directory in (binary, self.root):
            executable = directory/'cmw-synthetic'
            executable.write_text('#!/bin/sh\nprintf "path-ok\\n"\n')
            executable.chmod(0o700)
        for search_path in ('relative bin', '', str(binary)):
            job = self.store.add(argv=['cmw-synthetic'],cwd=self.root,
                                 name='PATH lookup',env={'PATH':search_path})
            self.assertEqual(launch_blocker(job), '')
        self.store.add(argv=['/bin/echo','absolute-ok'],cwd=self.root,
                       name='empty PATH absolute command',env={'PATH':''})
        self.run_queue()
        self.completed(4)
        self.assertEqual([j['status'] for j in self.jobs()], ['Done']*4)
        for job in self.jobs()[:3]:
            self.assertIn('path-ok',tail(job['logs']['stdout']))
        self.assertIn('absolute-ok',tail(self.jobs()[3]['logs']['stdout']))

    def test_environment_override_does_not_break_internal_receipt(self):
        self.add("import os; assert os.environ['PAYLOAD_FLAG'] == '值'", env={
            'PAYLOAD_FLAG': '值', 'PYTHONPATH': str(self.root/'empty path')})
        self.run_queue()
        self.completed(1)
        self.assertEqual(self.jobs()[0]['status'], 'Done')

    def test_existing_workflow_runner_preserves_result_contract(self):
        import shutil
        from tests.workflows.test_opt_freq_sp import _config, RUNNER, STRUCTURE
        config = self.root / 'workflow.json'
        structure = self.root / 'starting.xyz'
        output = self.root / 'results'
        _config(config)
        shutil.copyfile(STRUCTURE, structure)
        self.store.add(argv=['/bin/bash', str(RUNNER), '--mode', 'opt+sp',
            '--structure', str(structure), '--config', str(config), '--output', str(output)],
            cwd=self.root, name='existing fake ORCA workflow', engine='ORCA',
            env={'PYTHON_BIN': sys.executable, 'FAKE_ORCA_LOG': str(self.root/'fake.log')})
        self.run_queue()
        self.completed(1)
        self.assertEqual(self.jobs()[0]['status'], 'Done', self.jobs()[0])
        results = list((output/'workflows').glob('*/workflow_result.json'))
        self.assertEqual(len(results), 1)
        result = json.loads(results[0].read_text())
        self.assertTrue(result)
        self.assertIsNone(self.jobs()[0]['check'])

    def test_observation_safety_json_errors_and_tail(self):
        self.assertFalse(self.store.path.exists())
        state = self.store.snapshot()
        self.assertFalse(self.store.path.exists())
        self.assertIn('No jobs',status_text(state))
        self.assertEqual(elapsed(90061),'25:01:01')
        self.assertEqual(elapsed(None),'—')
        self.assertEqual(safe_text('\x1b[31mRED\x1b[0m\x1b]52;c;bad\x07[bold]'),'RED[bold]')
        log = self.root/'large.log'
        with log.open('wb') as handle:
            handle.seek(10*1024*1024)
            handle.write(b'end')
        self.assertEqual(len(tail(log,64)),3)  # NUL controls are removed.
        self.assertTrue(tail(log).endswith('end'))
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'}
        for args,code in [(['status','--json'],0),(['show','J99','--json'],2)]:
            result = subprocess.run([sys.executable,'-m','cmw.cli','jobs','--state',str(self.store.root),*args],capture_output=True,text=True,env=env)
            self.assertEqual(result.returncode,code,result.stderr)
            json.loads(result.stdout)
            self.assertNotIn('\x1b',result.stdout)
            self.assertEqual(result.stderr,'')


if __name__ == '__main__':
    unittest.main()
