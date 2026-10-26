"""Molecular node plans use authoritative target paths and shared engine checks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.core.provenance import file_hash
from cmw.jobs.locations import discover
from cmw.jobs.store import Store
from cmw.jobs.runner_assets import runner_path
from cmw.nodes.backend import LocalBackend
from cmw.nodes.client import check as client_check, submit as client_submit
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.identity import enroll, check_node, load_node, _file_identity
from cmw.nodes.molecular import normalize_scientific, resolve_artifact
from cmw.nodes.protocol import NodeError
from cmw.nodes.submission import launch_blocker
from cmw.nodes.transfer import inspect_local_bundle, prepare_local_bundle, upload_local_bundle, MAX_FILE_BYTES
from tests.nodes.test_identity import HOST, RUNTIME
from tests.nodes.test_storage import volume
from cmw.nodes.storage import identify_storage, check_storage


class MolecularNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-molecular-node-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root / 'storage'; self.storage.mkdir(mode=0o700)
        self.native = self.root / 'benign engine'
        self.native.write_text('#!/bin/sh\nexit 91\n'); self.native.chmod(0o700)
        self.input = self.root / 'source Ω.inp'
        self.input.write_text('! HF STO-3G SP\n%pal nprocs 1 end\n%maxcore 100\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n')
        self.runtime = {'executable':str(self.native),'sha256':file_hash(self.native),'version':'6.1.1','qualification':'synthetic'}
        self.config_root = self.root / 'node'
        self.config = enroll(self.config_root, {
            'queues':{'research':str(self.root/'queue')},'storages':{'scratch':str(self.storage)},
            'profiles':{'orca':{'engine':'ORCA','runner':str(runner_path('orca/run_orca.sh')),
                                'max_cpus':1,'runtime':self.runtime}}},
            host_provider=lambda:HOST, runtime_provider=lambda:RUNTIME,
            storage_provider=lambda path:identify_storage(path,probe=volume))
        for name in ('cmw.nodes.submission.check_node','cmw.nodes.backend.check_node'):
            # LocalBackend uses the explicitly supplied checker below; submission
            # performs its own independent node-local readiness check.
            if name.endswith('backend.check_node'):
                continue
            patcher=patch(name,side_effect=self.readiness);patcher.start();self.addCleanup(patcher.stop)
        for name in ('cmw.nodes.submission.check_storage','cmw.nodes.transfer.check_storage','cmw.nodes.molecular.check_storage'):
            patcher=patch(name,side_effect=lambda record:check_storage(record,probe=volume));patcher.start();self.addCleanup(patcher.stop)
        local=LocalBackend(self.config_root,'research',identity_checker=self.readiness)
        entry={'transport':'local','config_root':str(self.config_root),'node_id':self.config['node_id'],
            'host_id':self.config['host_id'],'runtime_id':RUNTIME['fingerprint'],
            'queues':{'research':self.config['queues']['research']['queue_id']},'default_queue':'research','read_only':False}
        self.backend=ConnectedBackend('mini',entry,transport=DirectTransport(local))
        self.addCleanup(self.backend.close)
        self.store=Store(self.config['queues']['research']['state'])
        self.spec={'schema':1,'request_id':uuid4().hex,'node_id':self.config['node_id'],
            'queue_id':self.config['queues']['research']['queue_id'],'storage':'scratch','profile':'orca',
            'engine':'ORCA','name':'H2 Ω reviewed','resources':{'cpus':1,'memory_gib':1,'mpi_ranks':1,'threads_per_rank':1},
            'files':[{'source':str(self.input),'path':'inputs/source Ω.inp','role':'input'}],
            'scientific':{'profile':'sp','input':{'bundle':'inputs/source Ω.inp'}},'intent':'held','dependencies_complete':True}

    def readiness(self,config):
        return check_node(config,host_provider=lambda:HOST,runtime_provider=lambda:RUNTIME,
                          storage_provider=lambda record:check_storage(record,probe=volume))

    def test_pure_check_and_preview_are_honest_and_do_not_publish(self):
        result=client_check(self.backend,self.spec)['result']
        self.assertEqual(result['status'],'UNASSESSED')
        self.assertTrue(any(item['domain']=='input' and item['status']=='UNASSESSED' for item in result['findings']))
        self.assertEqual(list(self.storage.iterdir()),[])
        self.assertEqual(self.store.snapshot()['jobs'],[])
        self.assertFalse((self.root/'requests').exists())
        preview=client_submit(self.backend,self.spec,root=self.root/'frontend',preview=True)['result']
        self.assertEqual(preview['preflight']['status'],'UNASSESSED')
        self.assertEqual(list(self.storage.iterdir()),[])
        self.assertTrue((self.root/'frontend/requests'/self.spec['request_id']/'request.json').exists())

    def test_orca_shared_plan_registers_once_and_final_revalidation_rejects_mutation(self):
        result=client_submit(self.backend,self.spec,root=self.root/'frontend')['result']
        job=result['job']
        self.assertEqual((job['engine'],job['status']),('ORCA','Hold'))
        self.assertEqual(job['progress_sources']['sources'][0]['path'],str(Path(job['cwd'])/'stage.out'))
        self.assertNotEqual(job['attempt_id'],self.spec['request_id'])
        self.assertIsNone(launch_blocker(job,final=True))
        shown=self.backend.call('show',target={'node_id':self.config['node_id'],
            'queue_id':self.config['queues']['research']['queue_id'],'job_id':job['id'],'attempt_id':job['attempt_id']})['result']
        self.assertEqual(shown['scientific_result']['status'],'PENDING')
        replay=client_submit(self.backend,self.spec,root=self.root/'frontend')['result']
        self.assertTrue(replay['replayed']);self.assertEqual(len(self.store.snapshot()['jobs']),1)
        changed=deepcopy(self.spec);changed['resources']['memory_gib']=2
        with self.assertRaises(NodeError):client_submit(self.backend,changed,root=self.root/'frontend')
        # Native/runtime replacement cannot be hidden by a successful earlier preview.
        self.native.write_text('#!/bin/sh\nexit 0\n')
        self.assertIn('readiness',launch_blocker(job,final=True).lower())

    def test_destination_input_validation_occurs_before_registration(self):
        self.input.write_text('! HF STO-3G SP\n%pal nprocs 2 end\n%maxcore 100\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n')
        with self.assertRaisesRegex(NodeError,'preflight'):
            client_submit(self.backend,self.spec,root=self.root/'frontend')
        self.assertEqual(self.store.snapshot()['jobs'],[])

    def donor(self):
        folder=self.storage/'donor';folder.mkdir()
        file=folder/'wavefunction.mwfn';file.write_text('same-node synthetic bytes\n')
        job=self.store.add(argv=[str(self.native)],cwd=folder,name='donor',engine='Command',hold=True)
        with self.store.transaction() as con:
            current=self.store.get(con,job['id']);current['status']='Done';self.store.save(con,current)
        location=next(item for item in discover(self.store,job['id']).locations if item.role=='working-directory')
        return file, {'target':{'node_id':self.config['node_id'],'queue_id':self.spec['queue_id'],
                'job_id':job['id'],'attempt_id':job['attempt_id']},'location_id':location.key,'relative':file.name,
                'role':location.role,'node_path':str(file),'sha256':file_hash(file)}

    def test_target_artifact_is_same_node_attempt_and_declared_location_authority(self):
        file,ref=self.donor()
        self.assertEqual(resolve_artifact(self.config,ref),str(file))
        for field,value in [('node_path',str(self.input)),('relative','../escape'),('role','output'),('sha256','0'*64)]:
            wrong=deepcopy(ref);wrong[field]=value
            with self.subTest(field=field),self.assertRaises(NodeError):resolve_artifact(self.config,wrong)
        wrong=deepcopy(ref);wrong['target']['node_id']=uuid4().hex
        with self.assertRaisesRegex(NodeError,'different node'):resolve_artifact(self.config,wrong)
        wrong=deepcopy(ref);wrong['target']['attempt_id']=uuid4().hex
        with self.assertRaisesRegex(NodeError,'attempt'):resolve_artifact(self.config,wrong)
        file.write_text('mutated donor\n')
        with self.assertRaisesRegex(NodeError,'digest'):resolve_artifact(self.config,ref)

    def test_source_result_cannot_be_a_transferred_manifest(self):
        with self.assertRaisesRegex(NodeError,'copied manifests'):
            normalize_scientific('Multiwfn',{'profile':'FMO','source_result':{'bundle':'inputs/result.json'}})
        with self.assertRaisesRegex(NodeError,'reference'):
            normalize_scientific('ORCA',{'profile':'sp','input':str(self.input)})

    def test_empty_target_local_bundle_and_transfer_size_bound(self):
        empty=inspect_local_bundle([],node_id=self.spec['node_id'],queue_id=self.spec['queue_id'])
        self.assertEqual(empty['files'],[])
        large=self.root/'large native binary'
        with large.open('wb') as handle:handle.truncate(MAX_FILE_BYTES+1)
        identity=_file_identity(str(large))
        self.assertEqual(identity['bytes'],MAX_FILE_BYTES+1)
        with self.assertRaisesRegex(NodeError,'64 MiB'):
            prepare_local_bundle(self.root/'oversize',[{'source':str(large),'path':'inputs/large','role':'input'}],
                                 node_id=self.spec['node_id'],queue_id=self.spec['queue_id'])
        self.assertFalse((self.root/'oversize').exists())

    def test_all_multiwfn_profiles_register_from_authoritative_same_node_donors(self):
        from tests.molecular.multiwfn.jobs_fixtures import make_spec
        runners={'FMO':'workflows/generate_homo_lumo_cubes.sh','ESP':'workflows/generate_esp_cubes.sh',
                 'IGMH':'workflows/generate_igmh_cubes.sh','IFCT':'multiwfn/run_ifct.sh'}
        for profile in runners:
            with self.subTest(profile=profile):
                local_spec=make_spec(self.storage/profile,profile)
                config_root=self.root/('node-'+profile)
                config=enroll(config_root,{'queues':{'research':str(self.root/('queue-'+profile))},
                    'storages':{'scratch':str(self.storage)},'profiles':{'analysis':{'engine':'Multiwfn',
                    'runner':str(runner_path(runners[profile])),'max_cpus':2,'runtime':local_spec['runtime']}}},
                    host_provider=lambda:HOST,runtime_provider=lambda:RUNTIME,
                    storage_provider=lambda path:identify_storage(path,probe=volume))
                store=Store(config['queues']['research']['state'])
                source=Path(local_spec['scientific']['source_result'])
                record=json.loads(source.read_text())
                target_path=source.parent/'target.json';target_path.write_text(json.dumps({'schema_version':1,'target':record['target']}))
                donor=store.add(argv=[str(runner_path('orca/run_orca.sh')),'--metadata',str(source),'--target',str(target_path),'--input',str(source.parent/'input.inp'),
                                    '--output',str(source.parent/'source.out'),'--stderr',str(source.parent/'source.err')],
                                cwd=source.parent,name='synthetic declared ORCA result',engine='ORCA',hold=True)
                with store.transaction() as con:
                    job=store.get(con,donor['id']);job['status']='Done';store.save(con,job)
                location=next(item for item in discover(store,donor['id']).locations if item.role=='working-directory')
                def reference(path,scientific=False):
                    path=Path(path)
                    value={'target':{'node_id':config['node_id'],'queue_id':config['queues']['research']['queue_id'],
                            'job_id':donor['id'],'attempt_id':donor['attempt_id']},'location_id':location.key,
                           'relative':str(path.relative_to(source.parent)),'role':location.role,'node_path':str(path),'sha256':file_hash(path)}
                    if scientific:value.update(scientific_target_id=record['target']['target_id'],scientific_attempt_id=record['attempt']['attempt_id'])
                    return {'artifact':value}
                original_ref=reference(source,True)['artifact']
                with store.transaction() as con:
                    original=store.get(con,donor['id']);original['engine']='Command';store.save(con,original)
                with self.assertRaisesRegex(NodeError,'generic Command'):
                    resolve_artifact(config,original_ref,scientific_result=True)
                with store.transaction() as con:
                    original=store.get(con,donor['id']);original['engine']='ORCA';store.save(con,original)
                copied=source.parent/'copied-result.json';copied.write_bytes(source.read_bytes())
                copied_ref=reference(copied,True)['artifact']
                with self.assertRaisesRegex(NodeError,'neither the declared original'):
                    resolve_artifact(config,copied_ref,scientific_result=True)
                scientific=deepcopy(local_spec['scientific']);scientific['source_result']=reference(source,True)
                files=[]
                for key in ('fragments_file','configuration_file'):
                    if key in scientific:
                        path=Path(scientific[key]);relative='inputs/'+path.name
                        files.append({'source':str(path),'path':relative,'role':'input'});scientific[key]={'bundle':relative}
                if 'orca_output' in scientific:scientific['orca_output']=reference(scientific['orca_output'])
                spec={**self.spec,'request_id':uuid4().hex,'node_id':config['node_id'],
                      'queue_id':config['queues']['research']['queue_id'],'profile':'analysis','engine':'Multiwfn',
                      'resources':local_spec['resources'],'scientific':scientific,'files':files}
                local=LocalBackend(config_root,'research',identity_checker=self.readiness)
                entry={'transport':'local','config_root':str(config_root),'node_id':config['node_id'],
                       'host_id':config['host_id'],'runtime_id':RUNTIME['fingerprint'],
                       'queues':{'research':spec['queue_id']},'default_queue':'research','read_only':False}
                backend=ConnectedBackend('mini-'+profile,entry,transport=DirectTransport(local));self.addCleanup(backend.close)
                checked=client_check(backend,spec)['result']
                self.assertEqual(checked['status'],'UNASSESSED' if profile=='IGMH' else 'READY',checked)
                job=client_submit(backend,spec,root=self.root/('client-'+profile))['result']['job']
                self.assertEqual((job['engine'],job['status']),('Multiwfn','Hold'))
                self.assertTrue(job['progress_sources']['sources'])
                self.assertIsNone(launch_blocker(job,final=True))
                wave=source.parent/'source.molden.input';wave.write_text('changed source after review')
                self.assertIsNone(launch_blocker(job))
                self.assertIn('integrity',launch_blocker(job,final=True))

    def test_existing_conversion_requires_original_result_and_pinned_plan(self):
        from tests.molecular.orca.test_wavefunction_conversion import OrcaWavefunctionConversionTests
        from cmw.molecular.orca.conversion import finalize_molden_conversion
        from cmw.nodes.molecular import _scientific_authority
        fixture=OrcaWavefunctionConversionTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        target=fixture.root/'target.json';target.write_text(json.dumps({'target':fixture.target.to_dict()}))
        declared={'engine':'ORCA','argv':[str(runner_path('orca/run_orca.sh')),
            '--input',str(fixture.geometry),'--output',str(fixture.orca_output),'--stderr',str(fixture.root/'stage.err'),
            '--metadata',str(fixture.source_result),'--target',str(target)]}
        prepared=fixture.prepare_output()
        path=Path(prepared['result_path'])
        record=finalize_molden_conversion(plan_path=Path(prepared['plan_path']),process_exit_code=0)
        _scientific_authority(declared,path,record)
        plan=Path(prepared['plan_path']);plan.write_text(plan.read_text()+' ')
        with self.assertRaisesRegex(NodeError,'Conversion plan identity changed'):
            _scientific_authority(declared,path,record)

    def test_malformed_client_and_peer_shapes_fail_without_spool(self):
        class NoContact:
            def call(self,*args,**kwargs):raise AssertionError('Malformed document contacted a peer')
        for value in (None,[],True,'not a spec'):
            with self.subTest(value=value),self.assertRaises(NodeError) as caught:
                client_submit(NoContact(),value,root=self.root/'malformed-spool')
            self.assertEqual(caught.exception.code,'INVALID_SUBMISSION')
        class MalformedPeer:
            def call(self,*args,**kwargs):
                return {'result':{'molecular_submission':{'schema':1,'engines':None,'target_local_artifacts':1}}}
        with self.assertRaises(NodeError) as caught:
            client_submit(MalformedPeer(),self.spec,root=self.root/'malformed-spool')
        self.assertEqual(caught.exception.code,'INCOMPATIBLE_ENGINE_SUBMISSION')
        self.assertFalse((self.root/'malformed-spool').exists())

    def test_old_peer_refused_before_spool(self):
        class OldPeer:
            def call(self,*args,**kwargs):return {'result':{'mutations_supported':True}}
        with self.assertRaisesRegex(NodeError,'schema'):
            client_submit(OldPeer(),self.spec,root=self.root/'old-peer-spool')
        self.assertFalse((self.root/'old-peer-spool').exists())


class DirectTransport:
    def __init__(self,backend):self.backend=backend
    def exchange(self,operation,params,target,request_id=None):
        return self.backend.call(operation,params,target,request_id=request_id)
    def close(self):self.backend.close()
