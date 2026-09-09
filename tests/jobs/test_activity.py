"""Deterministic process-table fixtures; never scan the research workstation."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs import activity


def owner(pid, birth=1):
    return {'pid': pid, 'birth': birth, 'host': 'fixture', 'boot': 1}


def process(pid, exe='/engine/vasp_std', *, name=None, parent=999, group=None,
            session=None, uid=None, status=psutil.STATUS_SLEEPING):
    p = Mock(pid=pid)
    p.name.return_value = name or Path(exe).name
    p.exe_value = exe
    p.uids.return_value = SimpleNamespace(real=os.getuid() if uid is None else uid)
    p.status.return_value = status
    p.ppid.return_value = parent
    p.cwd.return_value = '/same/work'
    p.group_value = pid if group is None else group
    p.session_value = pid if session is None else session
    return p


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-observer-unit-')
        self.addCleanup(self.temp.cleanup)
        self.store = SimpleNamespace(root=Path(self.temp.name))

    def collect(self, processes, *, state=None, ids=None, identities=None, executables=None):
        def factory(pid):
            if pid not in processes:
                raise psutil.NoSuchProcess(pid)
            return processes[pid]
        with ExitStack() as stack:
            stack.enter_context(patch.object(activity, 'identity', side_effect=identities or owner))
            stack.enter_context(patch.object(activity, 'native_executable', side_effect=executables or (lambda p:p.exe_value)))
            stack.enter_context(patch.object(activity.os, 'getpgid', side_effect=lambda pid:factory(pid).group_value))
            stack.enter_context(patch.object(activity.os, 'getsid', side_effect=lambda pid:factory(pid).session_value))
            return activity.collect(self.store, state or {'jobs': []}, time.monotonic()+1,
                                    pids_provider=lambda:list(processes) if ids is None else ids,
                                    process_factory=factory)

    def test_client_reuses_snapshot_without_caching_or_persisting_pid_inventory(self):
        state = {'jobs': [], 'controller': {'online': False, 'stale': False, 'dispatch': False}}
        def collect(store, requested, deadline):
            self.assertTrue(requested.get('_telemetry_membership'))
            return self.collect({1: process(1)}, state=requested)
        observer = activity.Observer(collector=collect)
        sampler = Mock()
        sampler.sample.return_value = {'machine_usage': {}, 'jobs': {}, 'external': {}}
        result = activity.project(self.store, state=state, observer=observer, sampler=sampler)
        self.assertEqual(sampler.sample.call_args.args[3], [1])
        self.assertNotIn('_pids', result['external_activity'])
        self.assertNotIn('_pids', observer.last)
        self.assertNotIn('_telemetry_membership', state)
        self.assertNotIn('_telemetry_membership', result)
        controller_result = self.collect({1: process(1)})
        self.assertNotIn('_pids', controller_result)

    def test_default_names_and_exact_paths(self):
        for name, engine in activity.NAMES.items():
            with self.subTest(name=name):
                result = self.collect({1:process(1, '/engine/'+name)})
                self.assertEqual(result['state'], 'BUSY')
                self.assertEqual(result['observations'][0]['engine'], engine)
                self.assertIsNone(result['observations'][0]['cpus'])
                self.assertIsNone(result['observations'][0]['memory_gib'])
        path = str((self.store.root/'benign-fixture').resolve())
        (self.store.root/'external-activity.json').write_text(json.dumps({'executables':{path:'ORCA'}}))
        self.assertEqual(self.collect({1:process(1,path)})['observations'][0]['engine'],'ORCA')
        self.assertEqual(self.collect({1:process(1,path+'-different')})['state'],'NO_MATCH')

    def test_false_positive_names_paths_scripts_and_launcher(self):
        for exe in ['/bin/grep','/bin/vim','/tmp/vasp_std.sh','/tmp/orca-helper',
                    '/orca/vasp_std_backup','/bin/mpirun','/bin/python']:
            with self.subTest(exe=exe):
                p = process(1,exe,name='vasp_std')
                p.cmdline.return_value = ['vim','/real/vasp_std','orca']
                self.assertEqual(self.collect({1:p})['state'],'NO_MATCH')
                p.cmdline.assert_not_called()

    def test_optional_cwd_denial_keeps_candidate(self):
        p = process(1)
        p.cwd.side_effect = psutil.AccessDenied(1)
        result = self.collect({1:p})
        self.assertEqual(result['state'],'BUSY')
        self.assertTrue(result['coverage']['complete'])
        self.assertIsNone(result['observations'][0]['cwd'])
        self.assertTrue(result['observations'][0]['missing'])

    def test_missing_critical_identity_and_scope_fail_closed(self):
        result = self.collect({1:process(1)}, identities=Mock(side_effect=psutil.AccessDenied(1)))
        self.assertEqual(result['state'],'UNCERTAIN')
        self.assertFalse(activity.permits(result))
        p = process(1,'/bin/python')
        p.uids.side_effect = psutil.AccessDenied(1)
        self.assertEqual(self.collect({1:p})['state'],'UNCERTAIN')
        p = process(1)
        for name in ['vasp_std']:
            p.name.return_value = name
            result = self.collect({1:p},executables=Mock(side_effect=psutil.AccessDenied(1)))
            self.assertEqual(result['state'],'UNCERTAIN')

    def test_verified_other_user_excluded_before_metadata_denials(self):
        p = process(1,uid=os.getuid()+1)
        p.name.side_effect = psutil.AccessDenied(1)
        result = self.collect({1:p})
        self.assertEqual(result['state'],'NO_MATCH')
        self.assertEqual(result['coverage']['out_of_scope'],1)
        p.name.assert_not_called()

    def test_disappearance_is_reconciled_without_lasting_uncertainty(self):
        self.assertEqual(self.collect({},ids=[1])['state'],'NO_MATCH')
        calls = 0
        def identity(pid):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise psutil.NoSuchProcess(pid)
            return owner(pid)
        result = self.collect({1:process(1)},identities=identity)
        self.assertEqual(result['state'],'NO_MATCH')
        self.assertEqual(result['coverage']['vanished'],1)

    def test_pid_reuse_and_exec_transitions_are_not_clear(self):
        counts = {}
        def reused(pid):
            counts[pid] = counts.get(pid,0)+1
            return owner(pid,1 if counts[pid]==1 else 2)
        self.assertEqual(self.collect({1:process(1)},identities=reused)['state'],'UNCERTAIN')
        for values in [['/engine/vasp_std','/bin/python'],
                       ['/engine/vasp_std','/engine/vasp_std','/bin/python']]:
            with self.subTest(values=values):
                self.assertEqual(self.collect({1:process(1)},executables=Mock(side_effect=values))['state'],'UNCERTAIN')

    def test_sleeping_stopped_and_zombie_with_surviving_worker(self):
        for status in [psutil.STATUS_SLEEPING,psutil.STATUS_STOPPED]:
            self.assertEqual(self.collect({1:process(1,status=status)})['state'],'BUSY')
        result = self.collect({1:process(1,status=psutil.STATUS_ZOMBIE),2:process(2,parent=1)})
        self.assertEqual(result['state'],'BUSY')
        self.assertEqual(result['coverage']['zombies'],1)
        self.assertEqual(result['observations'][0]['pid'],2)

    def test_narrow_launcher_grouping_and_unrelated_shared_cwd_session(self):
        parent = process(10,'/bin/mpirun',session=10)
        workers = {11:process(11,parent=10,session=10),12:process(12,parent=10,session=10)}
        result = self.collect({10:parent,**workers})
        self.assertEqual(len(result['observations']),1)
        self.assertEqual(result['observations'][0]['nproc'],2)
        stable = self.collect({10:parent,**workers})
        self.assertEqual(result['observations'][0]['id'],stable['observations'][0]['id'])
        for exe in ['/bin/zsh','/bin/python']:
            parent.exe_value = exe
            self.assertEqual(len(self.collect({10:parent,**workers})['observations']),2)
        # A generic launcher sharing a terminal session is insufficient.
        parent.exe_value = '/bin/mpirun'
        parent.session_value = 99
        self.assertEqual(len(self.collect({10:parent,**workers})['observations']),2)
        self.assertEqual(self.collect(workers)['state'],'BUSY')
        self.assertEqual(self.collect({12:workers[12]})['observations'][0]['nproc'],1)

    def test_group_representative_disappears_without_clearing_surviving_engine(self):
        processes = {10:process(10,'/bin/mpirun',session=10),
                     11:process(11,parent=10,session=10),
                     12:process(12,parent=10,session=10)}
        counts = {}
        def identities(pid):
            counts[pid] = counts.get(pid,0)+1
            if pid==11 and counts[pid]>=3:
                raise psutil.NoSuchProcess(pid)
            return owner(pid)
        result = self.collect(processes,identities=identities)
        self.assertEqual(result['state'],'BUSY')
        self.assertEqual(result['observations'][0]['pid'],12)
        self.assertEqual(result['observations'][0]['nproc'],1)
        self.assertFalse(activity.permits(result))

    def test_invalid_exact_path_configuration_is_structured_unavailable(self):
        for config in [{'executables':{'relative':'VASP'}},
                       {'executables':{'/fixture':'OTHER'}}, {'extra':{}}]:
            with self.subTest(config=config):
                (self.store.root/'external-activity.json').write_text(json.dumps(config))
                observer = activity.Observer(lambda store,state,deadline:self.collect({}))
                result = observer.scan(self.store,{'jobs':[]})
                self.assertEqual(result['state'],'UNAVAILABLE')
                self.assertFalse(activity.permits(result))
                self.assertIn('ValueError',result['reason'])

    def test_verified_managed_exclusion_does_not_exempt_independent_instance(self):
        processes = {11:process(11,group=10,session=10),21:process(21)}
        state = {'jobs':[{'status':'Run','group':owner(10),'worker':owner(9),'attempt_id':'fixture'}]}
        with patch.object(activity,'owner_alive',return_value=True), patch.object(activity,'lock_held',return_value=True):
            result = self.collect(processes,state=state)
        self.assertEqual(result['coverage']['managed'],1)
        self.assertEqual([o['pid'] for o in result['observations']],[21])
        # Another queue has no selected-state ownership exemption.
        self.assertEqual(len(self.collect(processes)['observations']),2)
        with patch.object(activity,'owner_alive',return_value=False):
            result = self.collect({11:processes[11]},state=state)
        self.assertEqual(result['state'],'BUSY')
        self.assertFalse(result['coverage']['complete'])
        self.assertFalse(activity.permits(result))

    def test_inaccessible_worker_lock_does_not_exempt_candidate(self):
        state = {'jobs':[{'status':'Run','group':owner(10),'worker':owner(9),'attempt_id':'fixture'}]}
        with patch.object(activity,'owner_alive',return_value=True), \
             patch.object(activity,'lock_held',side_effect=PermissionError('fixture denial')) as lock:
            result = self.collect({11:process(11,group=10,session=10)},state=state)
        self.assertEqual(result['state'],'BUSY')
        self.assertEqual(result['coverage']['managed'],0)
        self.assertFalse(result['coverage']['complete'])
        self.assertFalse(activity.permits(result))
        lock.assert_called_once_with(self.store.root/'attempts'/'fixture'/'worker.lock',strict=True)

    def test_launcher_disappears_during_final_scan_keeps_workers_separate(self):
        processes = {10:process(10,'/bin/mpirun',session=10),
                     11:process(11,parent=10,session=10),
                     12:process(12,parent=10,session=10)}
        calls = 0
        def identities(pid):
            nonlocal calls
            if pid==10:
                calls += 1
                # Both workers first verify the parent twice for grouping.
                if calls>4:
                    raise psutil.NoSuchProcess(pid)
            return owner(pid)
        result = self.collect(processes,identities=identities)
        self.assertEqual(result['state'],'BUSY')
        self.assertEqual(len(result['observations']),2)
        for observation in result['observations']:
            self.assertIsNone(observation['launcher'])
            self.assertIsNone(observation['launcher_exe'])
            self.assertEqual(observation['nproc'],1)
            self.assertFalse(any('grouping inference' in e for e in observation['evidence']))
        self.assertFalse(activity.permits(result))

    def test_stale_clear_is_not_admission_and_timeout_keeps_last_known(self):
        clear = self.collect({})
        self.assertTrue(activity.permits(clear))
        clear['observed_at'] -= activity.MAX_AGE+1
        self.assertFalse(activity.permits(clear))
        busy = self.collect({1:process(1)})
        release = threading.Event()
        entered = threading.Event()
        calls = []
        def collector(*args):
            calls.append(1)
            if len(calls)==1:
                return busy
            entered.set()
            release.wait(2)
            return self.collect({})
        observer = activity.Observer(collector)
        try:
            with patch.object(activity,'SCAN_BUDGET',.02):
                self.assertEqual(observer.scan(self.store,{})['state'],'BUSY')
                result = observer.scan(self.store,{})
                self.assertTrue(entered.is_set())
                self.assertEqual(result['state'],'UNAVAILABLE')
                self.assertTrue(result['stale'])
                self.assertEqual(result['observations'],busy['observations'])
                self.assertFalse(activity.permits(result))
                self.assertEqual(observer.scan(self.store,{})['state'],'UNAVAILABLE')
                self.assertEqual(len(calls),2)
        finally:
            release.set()
            observer.thread.join(2)
        # A late clear result is discarded; the next call collects afresh.
        observer.collector = lambda *args:busy
        self.assertEqual(observer.scan(self.store,{})['state'],'BUSY')
        observer.collector = Mock(side_effect=PermissionError('fixture denial'))
        result = observer.scan(self.store,{})
        self.assertEqual(result['state'],'UNAVAILABLE')
        self.assertEqual(result['observations'],busy['observations'])
        self.assertIn('PermissionError',result['reason'])

    def test_unrecognized_missing_executable_is_a_nonblocking_scope_limit(self):
        for name in ('ChatGPTHelper', 'browser_crashpad_handler', 'node', 'codex', 'benign-sleep-fixture'):
            with self.subTest(name=name):
                p = process(1, '', name=name)
                result = self.collect({1:p})
                self.assertEqual(result['state'], 'NO_MATCH')
                self.assertTrue(activity.permits(result))
                self.assertEqual(result['coverage']['uncertainties'], 0)
                self.assertIn('non-blocking', result['coverage']['limitations'][0])
        # A configured differently named computational candidate is still
        # material when its actual executable cannot be checked.
        (self.store.root/'external-activity.json').write_text(
            json.dumps({'executables': {'/engine/custom-engine': 'VASP'}}))
        result = self.collect({1:process(1, '', name='custom-engine')})
        self.assertEqual(result['state'], 'UNCERTAIN')
        self.assertFalse(activity.permits(result))
