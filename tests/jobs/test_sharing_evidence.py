"""Admission evidence cadence and one bounded, owned Python CPU fixture."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs import activity, sharing
from cmw.jobs.ownership import identity


class SharingEvidenceTests(unittest.TestCase):
    def test_window_retains_two_sufficiently_spaced_samples(self):
        window = sharing.EvidenceWindow()
        usage = {'machine_usage':{'cpu_percent':1},'jobs':{},'external':{}}
        with patch.object(sharing.time,'time',side_effect=[10,10.1,10.6,11.2]), \
             patch.object(psutil,'cpu_count',return_value=8):
            first = window.add(usage,source='controller')
            self.assertEqual(len(first['samples']),1)
            self.assertEqual(len(window.add(usage,source='controller')['samples']),1)
            self.assertEqual(len(window.add(usage,source='controller')['samples']),2)
            final = window.add(usage,source='supervisor-admission')
        self.assertEqual([s['observed_at'] for s in final['samples']],[10.6,11.2])
        self.assertEqual(final['source'],'supervisor-admission')
        self.assertEqual(len(first['samples']),1)

    def test_unavailable_machine_metrics_never_form_permitted_evidence(self):
        sampler = sharing.AdmissionSampler()
        store = Mock()
        store.snapshot.return_value = {'jobs':[]}
        guard = {'state':'NO_MATCH','stale':False,'observations':[],'_pids':[]}
        sampler.sampler = Mock()
        sampler.sampler.sample.return_value = {'machine_usage':{'cpu_quality':'unavailable',
                                                               'memory_quality':'unavailable'},
                                               'jobs':{},'external':{}}
        with patch.object(activity.DEFAULT_OBSERVER,'scan',side_effect=lambda *a,**k:copy.deepcopy(guard)):
            _,evidence = sampler.collect(store,wait=False)
        self.assertFalse(sharing._samples(evidence,time.time())[0])
        self.assertEqual(evidence['source'],'controller')

    def test_real_owned_cpu_fixture_yields_fresh_bounded_admission_window(self):
        with tempfile.TemporaryDirectory(prefix='cmw-sharing-evidence-') as temporary:
            root = Path(temporary)
            ready,start,release = (root/name for name in ['ready','start','release'])
            code = '''
import sys,time
from pathlib import Path
ready,start,release = map(Path,sys.argv[1:])
ready.touch()
while not start.exists() and not release.exists(): time.sleep(.01)
end = time.monotonic()+.5
while time.monotonic()<end and not release.exists():
    sum(i*i for i in range(20000))
while not release.exists(): time.sleep(.01)
'''
            process = subprocess.Popen([sys.executable,'-c',code,str(ready),str(start),str(release)],
                                       stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+3
                while not ready.exists() and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                owner = identity(process.pid)
                sampler = sharing.AdmissionSampler()
                store = Mock()
                store.snapshot.return_value = {'jobs':[]}
                def scan(*args,**kwargs):
                    self.assertEqual(identity(process.pid),owner)
                    start.touch()
                    return {'state':'BUSY','stale':False,'observed_at':time.time(),
                            'coverage':{'complete':True},'_pids':[process.pid],
                            'observations':[{'id':'Efixture','members':[{'identity':owner}]}]}
                began = time.monotonic()
                with patch.object(activity.DEFAULT_OBSERVER,'scan',side_effect=scan), \
                     patch.object(psutil,'pids',side_effect=AssertionError('Fixture must not enumerate host processes')):
                    guard,evidence = sampler.collect(store,source='supervisor-admission',wait=True)
                self.assertLess(time.monotonic()-began,3)
                samples,reason = sharing._samples(evidence,time.time())
                self.assertEqual(len(samples),2,reason)
                self.assertEqual(guard['state'],'BUSY')
                self.assertEqual(evidence['source'],'supervisor-admission')
                self.assertTrue(any(s['external']['Efixture']['cpu_cores'] > 0 for s in samples))
                self.assertTrue(all(s['machine_usage']['memory_quality']=='fresh' for s in samples))
                self.assertTrue(all(s['machine_usage']['cpu_quality']=='fresh' for s in samples))
                # No assertion about current host load or admission permission.
            finally:
                release.touch()
                process.wait(timeout=5)
                if process.stderr:
                    process.stderr.close()
            self.assertEqual(process.returncode,0)
