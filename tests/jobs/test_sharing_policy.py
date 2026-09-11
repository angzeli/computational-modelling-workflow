"""Bounded-sharing policy fixtures: no host reads or real workload launches."""
import copy
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from cmw.jobs import sharing
from cmw.jobs.store import JobsError

GIB = 1024**3
NOW = 1000.0


def identity(pid,birth=1):
    return {'pid':pid,'birth':birth,'boot':1,'host':'fixture'}


class SharingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-sharing-policy-')
        self.addCleanup(self.temp.cleanup)
        timer = patch.object(sharing.time,'time',return_value=NOW)
        timer.start()
        self.addCleanup(timer.stop)
        self.root = Path(self.temp.name).resolve()
        self.primary_dir = self.root/'primary'
        self.auxiliary_dir = self.root/'auxiliary'
        self.primary_dir.mkdir()
        self.auxiliary_dir.mkdir()
        self.primary = self.job('P1','primary','Run',None,self.primary_dir,cpus=6,memory=8)
        self.primary['scheduling']['allow_auxiliary'] = True
        self.auxiliary = self.job('A1','auxiliary','Queue',2,self.auxiliary_dir)
        self.state = {'jobs':[self.primary,self.auxiliary],
                      'controller':{'dispatch':True,'online':True,'stale':False},
                      'scheduler':{'mode':'bounded-sharing','cpu_budget':8,'memory_gib':12,
                                   'cpu_reserve':1,'min_available_gib':2,'external_reservation':None}}
        self.guard = {'state':'NO_MATCH','reason':'No recognized external computation observed',
                      'observed_at':NOW,'stale':False,'coverage':{'complete':True},'observations':[]}
        self.evidence = {'samples':[self.sample(NOW-1),self.sample(NOW)],'source':'controller'}

    def job(self,jid,role,status,order,cwd,cpus=2,memory=4):
        return {'id':jid,'display_id':jid+'.1','attempt_id':jid+'-attempt',
                'status':status,'order':order,'cwd':str(cwd),'argv':[sys.executable],'env':{},
                'resources':{'cpus':cpus,'memory_gib':memory},
                'scheduling':{'role':role,'allow_auxiliary':False,'independent':role=='auxiliary',
                              'resource_contract':'trusted-declared' if role=='auxiliary' else None,
                              'write_scope':str(cwd)}}

    def usage(self,when,cpu=1,rss=GIB):
        return {'cpu_cores':cpu,'rss_bytes':rss,'cpu_quality':'fresh','memory_quality':'fresh',
                'observed_at':when,'age_seconds':0,'stale':False,'cpu_members':1,'memory_members':1,'total_members':1}

    def sample(self,when,percent=50,available=16*GIB):
        return {'observed_at':when,'logical_cpus':16,
                'machine_usage':{'cpu_percent':percent,'ram_total_bytes':32*GIB,
                                 'ram_used_bytes':32*GIB-available,'cpu_quality':'fresh','memory_quality':'fresh',
                                 'observed_at':when,'age_seconds':0,'stale':False},
                'jobs':{'P1-attempt':self.usage(when)},'external':{}}

    def evaluate(self,**kwargs):
        return sharing.evaluate(self.state,self.guard,self.evidence,now=NOW,**kwargs)

    def assertBlocked(self,**kwargs):
        result = self.evaluate(**kwargs)
        self.assertFalse(result['permitted'],result)
        self.assertTrue(result['reason'])
        return result

    def external(self):
        observation = {'id':'E1','pid':91,'identity':identity(91),'launcher':None,
                       'engine':'VASP','exe':'/fixture/vasp_std','cwd':str(self.primary_dir),
                       'members':[{'pid':91,'identity':identity(91),'launcher':None,
                                   'exe':'/fixture/vasp_std','cwd':str(self.primary_dir)}]}
        self.guard.update(state='BUSY',reason='External VASP observed',observations=[observation])
        self.state['jobs'] = [self.auxiliary]
        for sample in self.evidence['samples']:
            sample['jobs'] = {}
            sample['external'] = {'E1':self.usage(sample['observed_at'])}
        return observation

    def reserve(self):
        observation = self.external()
        self.state['scheduler']['external_reservation'] = sharing.make_reservation(
            observation,self.guard,6,8,write_scope=str(self.primary_dir))
        return observation

    def test_exact_commitment_limits_and_no_low_usage_discount(self):
        result = self.evaluate()
        self.assertTrue(result['permitted'],result)
        self.assertEqual(result['candidate_id'],'A1')
        for field,value in [('cpus',3),('memory_gib',5)]:
            original = self.auxiliary['resources'][field]
            self.auxiliary['resources'][field] = value
            self.assertBlocked()
            self.auxiliary['resources'][field] = original
        for sample in self.evidence['samples']:
            sample['jobs']['P1-attempt']['cpu_cores'] = 0
            sample['jobs']['P1-attempt']['rss_bytes'] = 0
        self.state['scheduler']['cpu_budget'] = 7
        self.assertBlocked()
        self.state['scheduler']['cpu_budget'] = 8
        self.state['scheduler']['memory_gib'] = 11
        self.assertBlocked()

    def test_required_explicit_auxiliary_contract_and_primary_consent(self):
        for field in ['cpus','memory_gib']:
            old = self.auxiliary['resources'][field]
            self.auxiliary['resources'][field] = None
            self.assertBlocked()
            self.auxiliary['resources'][field] = old
        for field,value in [('independent',False),('resource_contract',None)]:
            old = self.auxiliary['scheduling'][field]
            self.auxiliary['scheduling'][field] = value
            self.assertBlocked()
            self.auxiliary['scheduling'][field] = old
        self.primary['scheduling']['allow_auxiliary'] = False
        self.assertBlocked()

    def test_unknown_commitment_is_not_reported_as_zero(self):
        self.primary['resources']['cpus'] = None
        self.primary['resources']['memory_gib'] = None
        decision = self.assertBlocked()
        self.assertIsNone(decision['cpu_committed'])
        self.assertIsNone(decision['memory_committed_gib'])

    def test_known_mpi_thread_declarations_cannot_understate_commitment(self):
        for job in (self.primary, self.auxiliary):
            job['resources']['mpi_ranks'] = 8
            job['resources']['threads_per_rank'] = 2
            self.assertBlocked()
            job['resources'].pop('mpi_ranks')
            job['resources'].pop('threads_per_rank')
        self.assertTrue(self.evaluate()['permitted'])

    def test_sequential_default_remains_exclusive_and_global_ordered(self):
        self.state.pop('scheduler')
        self.assertBlocked()
        self.state['jobs'] = [self.auxiliary]
        self.assertTrue(self.evaluate()['permitted'])
        self.external()
        self.assertBlocked()

    def test_auxiliary_lane_order_hold_and_no_second_primary(self):
        pending = self.job('P2','primary','Queue',1,self.root)
        self.state['jobs'].insert(1,pending)
        self.assertEqual(self.evaluate()['candidate_id'],'A1')
        self.assertBlocked(candidate_id='P2')
        later = self.job('A2','auxiliary','Queue',3,self.root)
        self.state['jobs'].append(later)
        self.assertBlocked(candidate_id='A2')
        self.auxiliary['status'] = 'Hold'
        self.assertBlocked()
        self.assertBlocked(candidate_id='A2')

    def test_second_auxiliary_and_lingering_auxiliary_block_next_primary(self):
        active = self.job('A0','auxiliary','Run',None,self.auxiliary_dir)
        self.state['jobs'].append(active)
        self.assertBlocked()
        self.state['jobs'] = [active,self.job('P2','primary','Queue',1,self.primary_dir)]
        self.assertBlocked()

    def test_pause_offline_stale_and_unresolved_execution_are_vetoes(self):
        for field,value in [('dispatch',False),('online',False),('stale',True)]:
            old = self.state['controller'][field]
            self.state['controller'][field] = value
            self.assertBlocked()
            self.state['controller'][field] = old
        for status in ['Unknown','Cancelling']:
            self.primary['status'] = status
            result = self.assertBlocked()
            self.assertEqual((result['cpu_committed'],result['memory_committed_gib']),(6,8))
        self.primary['status'] = 'Starting'
        result = self.assertBlocked()
        self.assertEqual((result['cpu_committed'],result['memory_committed_gib']),(6,8))

    def test_host_cpu_uses_worst_fresh_sample_and_ram_preserves_margin(self):
        # Candidate 2 + reserve 1 exactly fits 3 free logical cores.
        for sample in self.evidence['samples']:
            sample['machine_usage']['cpu_percent'] = 81.25
            sample['machine_usage']['ram_used_bytes'] = 26*GIB
        self.assertTrue(self.evaluate()['permitted'])
        self.evidence['samples'][0]['machine_usage']['cpu_percent'] = 82
        self.assertBlocked()
        self.evidence['samples'][0]['machine_usage']['cpu_percent'] = 81.25
        self.evidence['samples'][-1]['machine_usage']['ram_used_bytes'] = 26*GIB+1
        self.assertBlocked()

    def test_short_single_stale_unavailable_or_incomplete_host_window_blocks(self):
        original = copy.deepcopy(self.evidence)
        self.evidence['samples'] = self.evidence['samples'][-1:]
        self.assertBlocked()
        self.evidence = copy.deepcopy(original)
        self.evidence['samples'][0]['observed_at'] = NOW-.1
        self.evidence['samples'][0]['machine_usage']['observed_at'] = NOW-.1
        self.assertBlocked()
        for metric in ['cpu_quality','memory_quality']:
            self.evidence = copy.deepcopy(original)
            self.evidence['samples'][-1]['machine_usage'][metric] = 'unavailable'
            self.assertBlocked()
        self.evidence = copy.deepcopy(original)
        for sample in self.evidence['samples']:
            sample['observed_at'] -= 6
            sample['machine_usage']['observed_at'] -= 6
        self.assertBlocked()

    def test_cpu_sustained_overrun_and_rss_partial_overrun_block_without_mutation(self):
        self.evidence['samples'][0]['jobs']['P1-attempt']['cpu_cores'] = 7
        self.assertTrue(self.evaluate()['permitted'])
        self.evidence['samples'][1]['jobs']['P1-attempt']['cpu_cores'] = 7
        before = copy.deepcopy(self.state)
        result = self.assertBlocked()
        self.assertTrue(result['warnings'])
        self.assertEqual(self.state,before)
        for sample in self.evidence['samples']:
            sample['jobs']['P1-attempt']['cpu_cores'] = 1
        usage = self.evidence['samples'][-1]['jobs']['P1-attempt']
        usage.update(rss_bytes=8*GIB+1,memory_quality='partial')
        self.assertTrue(self.assertBlocked()['warnings'])

    def test_write_scopes_must_resolve_and_be_disjoint(self):
        self.assertTrue(self.evaluate()['permitted'])
        for scope in [self.primary_dir,self.root,self.primary_dir/'child',self.root/'missing']:
            if scope.name=='child':
                scope.mkdir()
            self.auxiliary['scheduling']['write_scope'] = str(scope)
            self.assertBlocked()
        alias = self.root/'alias'
        alias.symlink_to(self.primary_dir,target_is_directory=True)
        self.auxiliary['scheduling']['write_scope'] = str(alias)
        self.assertBlocked()

    def test_external_reservation_is_required_identity_bound_and_keeps_busy(self):
        self.external()
        self.assertBlocked()
        observation = self.reserve()
        self.assertTrue(self.evaluate()['permitted'])
        self.assertEqual(self.guard['state'],'BUSY')
        original = copy.deepcopy(observation)
        observation['members'][0]['identity']['birth'] = 2
        self.assertBlocked()
        self.guard['observations'] = [original,copy.deepcopy(original)]
        self.guard['observations'][1]['id'] = 'E2'
        self.guard['observations'][1]['members'][0]['identity'] = identity(92)
        self.assertBlocked()

    def test_external_uncertainty_staleness_and_disappearance_do_not_authorize_overlap(self):
        self.reserve()
        for state in ['UNCERTAIN','UNAVAILABLE']:
            self.guard['state'] = state
            self.assertBlocked()
        self.guard['state'] = 'BUSY'
        self.guard['coverage']['complete'] = False
        self.assertBlocked()
        self.guard['coverage']['complete'] = True
        self.guard['stale'] = True
        self.assertBlocked()
        self.guard.update(state='NO_MATCH',stale=False,observations=[])
        self.auxiliary.update(status='Run',order=None)
        self.state['jobs'].append(self.job('P2','primary','Queue',1,self.primary_dir))
        self.assertBlocked()

    def test_final_admission_rechecks_anchor_and_queue_identity(self):
        early = self.evaluate()
        self.assertTrue(early['permitted'])
        self.auxiliary.update(status='Starting',order=None,admission_order=2,
                              sharing_anchor=early['anchor_primary'])
        self.assertTrue(self.evaluate(candidate_id='A1',final=True)['permitted'])
        self.primary['attempt_id'] = 'replacement-attempt'
        self.assertBlocked(candidate_id='A1',final=True)

    def test_external_scope_requires_complete_cwd_or_explicit_declaration(self):
        observation = self.external()
        observation['members'][0]['cwd'] = None
        with self.assertRaises(JobsError):
            sharing.make_reservation(observation,self.guard,6,8)
        reservation = sharing.make_reservation(observation,self.guard,6,8,str(self.primary_dir))
        self.state['scheduler']['external_reservation'] = reservation
        self.assertTrue(self.evaluate()['permitted'])
        self.assertEqual((self.evaluate()['cpu_committed'],self.evaluate()['memory_committed_gib']),(6,8))
        for cpus,memory in [(None,8),(6,None),(0,8),(6,0)]:
            with self.subTest(cpus=cpus,memory=memory), self.assertRaises(JobsError):
                sharing.make_reservation(observation,self.guard,cpus,memory,str(self.primary_dir))

    def test_external_binding_changes_with_exec_launcher_and_membership(self):
        observation = self.reserve()
        original = copy.deepcopy(observation)
        for field,value in [('exe','/different/vasp_std'),('launcher',identity(90)),
                            ('pgid',123),('sid',123)]:
            self.guard['observations'] = [copy.deepcopy(original)]
            self.guard['observations'][0]['members'][0][field] = value
            self.assertEqual(self.assertBlocked()['external_reservation_state'],'expired')
        self.guard['observations'] = [copy.deepcopy(original)]
        self.guard['observations'][0]['members'].append({**copy.deepcopy(original['members'][0]),'identity':identity(92)})
        self.assertBlocked()

    def test_final_go_rechecks_resources_guard_and_pause_without_mutation(self):
        early = self.evaluate()
        self.auxiliary.update(status='Starting',order=None,admission_order=2,
                              sharing_anchor=early['anchor_primary'])
        baseline = copy.deepcopy(self.state)
        self.state['scheduler']['cpu_budget'] = 7
        self.assertBlocked(candidate_id='A1',final=True)
        self.state = copy.deepcopy(baseline)
        self.state['scheduler']['memory_gib'] = 11
        self.assertBlocked(candidate_id='A1',final=True)
        self.state = copy.deepcopy(baseline)
        self.state['controller']['dispatch'] = False
        self.assertBlocked(candidate_id='A1',final=True)
        self.state = copy.deepcopy(baseline)
        self.guard['state'] = 'UNCERTAIN'
        self.assertBlocked(candidate_id='A1',final=True)
        self.assertEqual(self.state,baseline)

    def test_invalid_host_numbers_cannot_authorize_sharing(self):
        original = copy.deepcopy(self.evidence)
        cases = [('cpu_percent',float('nan')),('cpu_percent',float('inf')),
                 ('cpu_percent',-1),('cpu_percent',101),
                 ('ram_total_bytes',float('nan')),('ram_total_bytes',float('inf')),
                 ('ram_total_bytes',0),('ram_used_bytes',float('nan')),
                 ('ram_used_bytes',float('inf')),('ram_used_bytes',-1),
                 ('ram_used_bytes',33*GIB)]
        for field,value in cases:
            with self.subTest(field=field,value=value):
                self.evidence = copy.deepcopy(original)
                self.evidence['samples'][-1]['machine_usage'][field] = value
                self.assertBlocked()
        for value in [0,-1,1.5,float('nan'),float('inf'),True,None]:
            with self.subTest(logical_cpus=value):
                self.evidence = copy.deepcopy(original)
                self.evidence['samples'][-1]['logical_cpus'] = value
                self.assertBlocked()

    def test_sustained_cpu_overrun_requires_two_fresh_available_metrics(self):
        for sample in self.evidence['samples']:
            sample['jobs']['P1-attempt']['cpu_cores'] = 7
        for quality in ['unavailable','warming-up']:
            self.evidence['samples'][0]['jobs']['P1-attempt']['cpu_quality'] = quality
            result = self.evaluate()
            self.assertTrue(result['permitted'],result)
            self.assertEqual(result['warnings'],[])
        for quality in ['fresh', 'partial']:
            self.evidence['samples'][0]['jobs']['P1-attempt']['cpu_quality'] = quality
            self.assertTrue(self.assertBlocked()['warnings'])

    def test_recent_swap_out_veto_does_not_treat_historical_swap_as_pressure(self):
        for sample in self.evidence['samples']:
            sample['machine_usage']['swap_out_bytes'] = 123456
        self.assertTrue(self.evaluate()['permitted'])
        self.evidence['samples'][-1]['machine_usage']['swap_out_bytes'] += 1
        self.assertIn('swap-out',self.assertBlocked()['reason'])
