"""Real task-owned processes through the production worker and runner contract."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

from cmw.jobs import runtime, sharing, suspension
from cmw.jobs.ownership import identity, owner_alive, group_members
from cmw.jobs.store import Store, JobsError
from tests.jobs.isolated_runtime import install
from tests.jobs.test_jobs import wait_for


class SuspensionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.isolation = install()
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-suspension-')
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.owners = []
        self.sentinel = None

    def tearDown(self):
        try:
            if self.store.path.exists():
                for job in self.store.snapshot()['jobs']:
                    if job['status'] in {'Run', 'Starting'}:
                        self.store.change(job['id'], 'cancel', confirm=True)
                wait_for(lambda: all(j['status'] not in {'Run','Starting','Cancelling'} for j in self.store.snapshot()['jobs']))
                runtime.stop(self.store)
                wait_for(lambda: not self.store.snapshot()['controller']['online'])
            if self.sentinel:
                self.sentinel.terminate(); self.sentinel.wait(timeout=5)
            wait_for(lambda: not any(owner_alive(owner) for owner in self.owners), timeout=5)
            runtime.reap_detached()
        finally:
            self.isolation.close()
            self.temp.cleanup()

    def job(self):
        return self.store.snapshot()['jobs'][0]

    def phase(self, target):
        def observed():
            job = self.job()
            if job['suspension']['phase'] == 'Unverified' and target != 'Unverified':
                raise AssertionError(json.dumps(job['suspension'], indent=2))
            return job['suspension']['phase'] == target
        wait_for(observed)
        return self.job()

    def launch(self, *, threads=False, hard=None, budget=20, tree=False):
        code = '''import json,os,time,threading,subprocess,sys
from pathlib import Path
root=Path(sys.argv[1]); seq=0; sentinel=17391
'''
        if threads:
            code += "threading.Thread(target=lambda: [(sum(range(1000)),time.sleep(.01)) for _ in range(2500)],daemon=True).start()\n"
        if tree:
            code += "child=subprocess.Popen([sys.executable,'-c',\"import time; [time.sleep(.03) for _ in range(1500)]\"],preexec_fn=os.setpgrp)\n"
        code += '''while not (root/'finish').exists():
 seq+=1
 temporary=root/'counter.tmp'; temporary.write_text(json.dumps({'pid':os.getpid(),'seq':seq,'sentinel':sentinel})); temporary.replace(root/'counter.json')
 sum(range(2000)); time.sleep(.03)
'''
        if tree:
            code += 'child.terminate();child.wait()\n'
        args = [sys.executable,'-m','cmw.jobs.suspension_protocol','--execution-budget',str(budget)]
        if hard is not None:
            args += ['--hard-timeout', str(hard)]
        args += ['--',sys.executable,'-c',code,str(self.root)]
        job = self.store.add(argv=args,cwd=self.root,name='benign same-memory computation',
                             cpus=2,memory_gib=.2,suspension_backend='local-command-v1')
        runtime.start(self.store)
        wait_for(lambda: self.job()['status'] in {'Run','Fail','Unknown'})
        wait_for((self.root/'counter.json').exists)
        def ready():
            current = self.job()
            if current['suspension']['phase'] == 'Unverified':
                raise AssertionError(json.dumps(current['suspension'], indent=2))
            return suspension.readiness(self.store,job['id'])['ready']
        wait_for(ready)
        current = self.job()
        self.owners += [current['group'],current['worker'],current['suspension']['capability']['runner']]
        self.owners += current['suspension']['evidence']['computational']
        return job

    def counter(self):
        return json.loads((self.root/'counter.json').read_text())

    def test_same_memory_repeat_retained_resources_dispatch_and_controller_detach(self):
        self.launch(threads=True,tree=True)
        self.sentinel = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
        untouched = identity(self.sentinel.pid)
        original = self.job()
        for cycle in range(2):
            ready = suspension.readiness(self.store,1)
            result = suspension.submit(self.store,1,'suspend',reason='meeting u/c/p/x',expected_revision=ready['revision'])
            self.assertEqual(result['result'],'ACCEPTED')
            stopped = self.phase('Suspended')
            self.assertEqual(stopped['status'],'Run')
            before = self.counter(); time.sleep(.3); self.assertEqual(before,self.counter())
            self.assertEqual(identity(before['pid']), original['suspension']['evidence']['computational'][0])
            self.assertTrue(all(psutil.Process(m['pid']).status()==psutil.STATUS_STOPPED for m in stopped['suspension']['evidence']['computational']))
            self.assertTrue(all(psutil.Process(m['pid']).status()!=psutil.STATUS_STOPPED for m in stopped['suspension']['evidence']['protected']))
            self.assertEqual(identity(self.sentinel.pid),untouched)
            self.assertEqual(suspension.submit(self.store,1,'suspend')['result'],'COMPLETED')
            state = self.store.snapshot(); decision=sharing.evaluate(state,{'observed_at':time.time(),'state':'NO_MATCH','observations':[],'reason':''})
            self.assertFalse(decision['permitted']); self.assertEqual(decision['cpu_committed'],2)
            self.assertEqual(decision['memory_committed_gib'],.2)
            self.store.annotate(1,'concurrent note u/c/p/x')
            if cycle == 0:
                runtime.stop(self.store); wait_for(lambda: not self.store.snapshot()['controller']['online'])
            suspension.submit(self.store,1,'continue')
            resumed=self.phase('Run'); wait_for(lambda: self.counter()['seq']>before['seq'])
            self.assertEqual(resumed['attempt_id'],original['attempt_id'])
            self.assertEqual(self.counter()['sentinel'],17391)
            self.assertEqual(resumed['note'],'concurrent note u/c/p/x')
            self.assertFalse(self.store.snapshot()['controller']['dispatch'])
        (self.root/'finish').touch()
        wait_for(lambda: self.job()['status']=='Done')
        self.assertEqual(len(self.job()['suspension']['intervals']),2)
        final = self.job()['suspension']
        self.assertGreater(final['unsuspended_elapsed'],0)
        self.assertEqual(self.store.snapshot(now=time.time()+100)['jobs'][0]['suspension']['unsuspended_elapsed'],final['unsuspended_elapsed'])

    def test_cancel_stopped_tree_drains_before_commitments_released(self):
        self.launch(tree=True)
        suspension.submit(self.store,1,'suspend'); stopped=self.phase('Suspended')
        sid=stopped['group']['pid']
        self.store.change(1,'cancel',confirm=True)
        with self.assertRaises(JobsError): suspension.submit(self.store,1,'continue')
        wait_for(lambda: self.job()['status']=='Cancelled')
        self.assertFalse(group_members(sid,session=True))

    def test_external_continue_invalidates_without_corrective_stop(self):
        self.launch()
        suspension.submit(self.store,1,'suspend'); stopped=self.phase('Suspended')
        member=stopped['suspension']['evidence']['computational'][0]
        self.assertTrue(owner_alive(member)); os.kill(member['pid'],signal.SIGCONT)
        unverified=self.phase('Unverified')
        self.assertTrue(unverified['suspension']['inhibited'])
        with self.assertRaises(JobsError): suspension.submit(self.store,1,'continue')

    def test_pause_aware_budget_excludes_only_confirmed_interval(self):
        self.launch(budget=2)
        suspension.submit(self.store,1,'suspend'); self.phase('Suspended')
        time.sleep(2.2)
        self.assertEqual(self.job()['status'],'Run')
        self.assertGreater(self.job()['suspension']['suspended_seconds'],2)
        suspension.submit(self.store,1,'continue'); self.phase('Run')
        (self.root/'finish').touch()
        wait_for(lambda:self.job()['status']=='Done')

    def test_hard_deadline_while_suspended_is_failure_not_continue(self):
        self.launch(hard=3)
        suspension.submit(self.store,1,'suspend'); self.phase('Suspended')
        wait_for(lambda: self.job()['status']=='Fail',timeout=9)
        self.assertEqual(self.job()['exit_code'],124)
        self.assertIsNotNone(self.job().get('termination_requested'))


if __name__ == '__main__': unittest.main()
