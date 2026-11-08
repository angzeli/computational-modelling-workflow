"""Synthetic engine-aware endpoint/worker/observer acceptance, usable installed."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from cmw.jobs import runtime
from cmw.jobs.locations import discover
from cmw.jobs.ownership import identity, owner_alive
from cmw.jobs.store import Store
from cmw.jobs.runner_assets import runner_path
from cmw.core.provenance import file_hash
from cmw.nodes.client import check, submit, apply_control, fetch
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.identity import enroll
from cmw.nodes.protocol import NodeError
from cmw.nodes.transport import ProcessTransport
from cmw.nodes.codec import decode_typed
from cmw.progress.formatting import progress_text
from tests.jobs.isolated_runtime import environment, install
from tests.molecular.multiwfn.jobs_fixtures import make_spec
from tests.nodes.acceptance import wait_for


def _reference(config, store, donor, path, *, scientific=False):
    path=Path(path)
    location=next(item for item in discover(store,donor['id']).locations if item.role=='working-directory')
    value={'target':{'node_id':config['node_id'],'queue_id':config['queues']['research']['queue_id'],
        'job_id':donor['id'],'attempt_id':donor['attempt_id']},'location_id':location.key,
        'relative':str(path.relative_to(donor['cwd'])),'role':location.role,'node_path':str(path),'sha256':file_hash(path)}
    if scientific:
        record=json.loads(path.read_text())
        value.update(scientific_target_id=record['target']['target_id'],scientific_attempt_id=record['attempt']['attempt_id'])
    return {'artifact':value}


def _fixture(root, profile):
    storage=root/'calculation';storage.mkdir(mode=0o700)
    if profile=='ORCA':
        native=root/'benign-orca'
        # Native-shaped bytes are emitted incrementally by the owned harmless child.
        from tests.progress.orca_fixtures import header, scf
        before=header(stage='SP')+scf(soscf=False,converged=False)
        after='SCF CONVERGED AFTER 3 CYCLES\nFINAL SINGLE POINT ENERGY -1.1\nORCA TERMINATED NORMALLY\n'
        native.write_text('#!'+sys.executable+'\nimport time\nfrom pathlib import Path\n'+
            f'print({before!r},flush=True)\nPath({str(root/"started")!r}).touch()\n'+
            f'deadline=time.monotonic()+90\nwhile not Path({str(root/"release")!r}).exists():\n if time.monotonic()>deadline: raise SystemExit(73)\n time.sleep(.05)\n'+f'print({after!r},flush=True)\n')
        native.chmod(0o700)
        inp=root/'input Ω.inp';inp.write_text('! HF STO-3G SP\n%pal nprocs 1 end\n%maxcore 100\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n')
        return storage,{'engine':'ORCA','resources':{'cpus':1,'memory_gib':1,'mpi_ranks':1,'threads_per_rank':1},
            'runtime':{'executable':str(native),'sha256':file_hash(native),'version':'6.1.1','qualification':'synthetic'},
            'scientific':{'profile':'sp','input':str(inp)}},'orca/run_orca.sh'
    spec=make_spec(storage/'fixture',profile)
    native=Path(spec['runtime']['executable'])
    original=native.read_text()
    gate=(f'\nfrom pathlib import Path as _Path\nprint("Native fixture live",flush=True)\n_Path({str(root/"started")!r}).touch()\n'
          f'_deadline=time.monotonic()+90\nwhile not _Path({str(root/"release")!r}).exists():\n if time.monotonic()>_deadline: raise SystemExit(73)\n time.sleep(.05)\n')
    if profile=='IFCT':
        marker="time.sleep(float(os.environ.get('CMW_TEST_PAUSE','0.5')))"
        assert marker in original
        original=original.replace(marker,gate)
    else:
        marker='commands = sys.stdin.read()'
        assert marker in original
        native_header="\nprint('Version 3.8',flush=True)\nprint('Loaded '+sys.argv[1]+' successfully!',flush=True)\n"
        original=original.replace(marker,marker+native_header+gate)
    origin_probe=('import cmw as _cmw,json as _json,sys as _sys\nfrom pathlib import Path as _ProbePath\n'
                  +f'_ProbePath({str(root / "engine-package-origin.json")!r}).write_text(_json.dumps({{"cmw":_cmw.__file__,"python":_sys.executable}}))\n')
    if 'from __future__ import annotations\n' in original:
        original=original.replace('from __future__ import annotations\n','from __future__ import annotations\n'+origin_probe,1)
    else:
        original=original.replace("' '''\n", "' '''\n"+origin_probe, 1)
    native.write_text(original);spec['runtime']['sha256']=file_hash(native)
    return storage,spec,{'FMO':'workflows/generate_homo_lumo_cubes.sh','ESP':'workflows/generate_esp_cubes.sh',
                         'IGMH':'workflows/generate_igmh_cubes.sh','IFCT':'multiwfn/run_ifct.sh'}[profile]


def molecular_surface(root, *, backend_builder=None, cli_runner=None,
                      profiles=('ORCA', 'FMO', 'IFCT'), multiwfn_progress=False):
    """Actual endpoint -> held engine plan -> owned execution -> progress -> fetch."""
    root=Path(root).resolve();root.mkdir(mode=0o700)
    checks,results,all_owners={},[],[]
    for profile in profiles:
        case=root/profile;case.mkdir(mode=0o700)
        storage,local_spec,runner=_fixture(case,profile)
        config_root=case/'node'
        config=enroll(config_root,{'queues':{'research':str(case/'queue')},'storages':{'scratch':str(storage)},
            'profiles':{'engine':{'engine':local_spec['engine'],'runner':str(runner_path(runner)),
                                  'max_cpus':local_spec['resources']['cpus'],'runtime':local_spec['runtime']}}})
        store=Store(case/'queue')
        # A fixed terminal synthetic donor enables all profiles to share display J2.1;
        # immutable node/queue/attempt identities must still keep them separate.
        donor_path=Path(local_spec['scientific']['source_result']).parent if profile!='ORCA' else storage
        if profile!='ORCA':
            result_path=Path(local_spec['scientific']['source_result'])
            record=json.loads(result_path.read_text());target_path=donor_path/'target.json'
            target_path.write_text(json.dumps({'schema_version':1,'target':record['target']}))
            donor_argv=[str(runner_path('orca/run_orca.sh')),'--metadata',str(result_path),'--target',str(target_path),
                        '--input',str(donor_path/'input.inp'),'--output',str(donor_path/'source.out'),'--stderr',str(donor_path/'source.err')]
        else:
            donor_argv=['/bin/echo','synthetic sentinel']
        donor=store.add(argv=donor_argv,cwd=donor_path,name='synthetic source fixture',engine='ORCA' if profile!='ORCA' else 'Command',hold=True)
        with store.transaction() as con:
            previous=store.get(con,donor['id']);previous['status']='Done';store.save(con,previous)
        science=deepcopy(local_spec['scientific']);files=[]
        if profile=='ORCA':
            files=[{'source':science['input'],'path':'inputs/input Ω.inp','role':'input'}]
            science['input']={'bundle':'inputs/input Ω.inp'}
        else:
            science['source_result']=_reference(config,store,donor,science['source_result'],scientific=True)
            if profile=='IFCT':science['orca_output']=_reference(config,store,donor,science['orca_output'])
            for field in ('fragments_file','configuration_file'):
                if field in science:
                    path=Path(science[field]);relative='inputs/'+path.name
                    files.append({'source':str(path),'path':relative,'role':'input'})
                    science[field]={'bundle':relative}
        entry={'transport':'local','config_root':str(config_root),'node_id':config['node_id'],'host_id':config['host_id'],
            'runtime_id':config['runtime']['fingerprint'],'queues':{'research':config['queues']['research']['queue_id']},
            'default_queue':'research','read_only':False}
        spec={'schema':1,'request_id':uuid4().hex,'node_id':entry['node_id'],'queue_id':entry['queues']['research'],
            'storage':'scratch','profile':'engine','engine':local_spec['engine'],'name':profile+' synthetic remote',
            'resources':local_spec['resources'],'files':files,'scientific':science,'intent':'held','dependencies_complete':True}
        (case/'submission-spec.json').write_text(json.dumps(spec,indent=2)+'\n')
        frontend=case/'frontend';backend=None;owners=[];job=None
        def connect():
            if backend_builder is not None:return backend_builder(entry)
            argv=[sys.executable,'-B','-m','tests.jobs.isolated_runtime','cli','nodes','endpoint','--config',str(config_root),'--queue','research']
            return ConnectedBackend('molecular-mini',entry,transport=ProcessTransport(argv,timeout=30,env=environment()))
        def close():
            nonlocal backend
            if backend is None:return
            process=getattr(getattr(backend,'transport',None),'process',None)
            if process is not None and process.poll() is None:owners.append(identity(process.pid))
            backend.close();backend=None
        def control(action,target=None):
            preview=backend.call('control.preview',{'action':action},target)['result']
            return apply_control(backend,preview,root=frontend)['result']
        cli_selected=cli_runner is not None and (profile=='ORCA' or multiwfn_progress and profile=='FMO')
        try:
            backend=connect()
            if cli_selected:
                cli_runner(entry,case,['check','--spec',str(case/'submission-spec.json'),'--json'],expected=2 if files else 0)
                cli_runner(entry,case,['submit','--spec',str(case/'submission-spec.json'),'--preview','--json'])
            checked=check(backend,spec)['result'];assert checked['status']==('UNASSESSED' if files else 'READY'),checked
            assert not list(storage.glob('cmw-*'))
            checks[profile+'_pure_pretransfer_check']=True
            preview=submit(backend,spec,root=frontend,preview=True)['result']
            assert preview['preflight']['status']==('UNASSESSED' if files else 'READY') and not (case/'started').exists()
            if multiwfn_progress:
                # Lose only the client receipt after the endpoint commits. A retry
                # must recover its immutable outcome without a second registration.
                class LoseRegistrationReply:
                    def __getattr__(self,name):return getattr(backend,name)
                    def call(self,operation,*args,**kwargs):
                        result=backend.call(operation,*args,**kwargs)
                        if operation=='submit':
                            raise NodeError('OUTCOME_UNKNOWN','Synthetic lost registration response')
                        return result
                try:submit(LoseRegistrationReply(),spec,root=frontend)
                except NodeError as error:assert error.code=='OUTCOME_UNKNOWN',error
                else:raise AssertionError('Fixture did not lose its registration response')
                assert len(store.snapshot()['jobs'])==2
                close();backend=connect()
                recovered=submit(backend,spec,root=frontend)['result']
                assert recovered['replayed'] and len(store.snapshot()['jobs'])==2
                job=recovered['job']
                checks[profile+'_lost_registration_reply_recovers_exact_attempt']=True
            else:
                job=submit(backend,spec,root=frontend)['result']['job']
            assert job['engine']==local_spec['engine'] and job['status']=='Hold' and job['display_id']=='J2.1'
            target={'node_id':entry['node_id'],'queue_id':entry['queues']['research'],'job_id':job['id'],'attempt_id':job['attempt_id']}
            replay=submit(backend,spec,root=frontend)['result'];assert replay['replayed'] and replay['job']['attempt_id']==job['attempt_id']
            changed=deepcopy(spec);changed['resources']['memory_gib']=2
            try:submit(backend,changed,root=frontend)
            except NodeError as exc:assert exc.code=='REQUEST_CONFLICT'
            else:raise AssertionError('Changed reviewed semantics reused a request ID')
            checks[profile+'_held_idempotent_engine_registration']=True
            pending=decode_typed(backend.call('progress',target=target)['result']['snapshot'])
            assert not any(source.path.endswith('/stdout.log') for source in pending.context.sources)
            checks[profile+'_declared_future_native_sources']=True
            if multiwfn_progress:
                assert pending.context.engine.upper()=='MULTIWFN'
                assert {item.role for item in pending.context.sources}=={
                    'multiwfn_session','multiwfn_metadata','multiwfn_events','multiwfn_validation'}
                assert all(str(case) in item.path for item in pending.context.sources)
                assert [group.label for group in pending.card_groups]==['ANALYSIS','STAGE','OUTPUT']
                pending_text=progress_text(pending,width=70,max_lines=8,framed=True)
                assert 'ANALYSIS' in pending_text and '┌' in pending_text,pending_text
                (case/'pending-progress.txt').write_text(pending_text)
                (case/'pending-snapshot.json').write_text(json.dumps(pending.to_dict(),indent=2)+'\n')
                checks[profile+'_held_pending_analysis_card']=True
            if cli_selected:
                cli_runner(entry,case,['release','J2.1','--preview','--json'])
                cli_runner(entry,case,['release','J2.1','--json'])
                cli_runner(entry,case,['start','--preview','--json'])
                cli_runner(entry,case,['start','--json'])
            else:
                control('controller.start');control('release',target)
            wait_for(lambda:(case/'started').exists(),timeout=35)
            state=store.snapshot();active=next(item for item in state['jobs'] if item['id']==job['id'])
            assert active['status']=='Run',active
            owners.extend(item for item in (state['controller']['owner'],active['worker'],active['group']) if item)
            snapshots=[]
            def observed():
                value=decode_typed(backend.call('progress',target=target)['result']['snapshot'])
                snapshots.append(value)
                return bool(value.metrics or value.events) if profile=='ORCA' else any(event.kind=='native' for event in value.events)
            wait_for(observed,timeout=12)
            text=progress_text(snapshots[-1]);(case/'live-progress.txt').write_text(text)
            assert snapshots[-1].context.attempt_id==job['attempt_id']
            checks[profile+'_incremental_automatic_progress']=True
            if multiwfn_progress:
                from cmw.nodes.codec import encode_typed
                snapshot=snapshots[-1]
                assert not snapshot.metrics
                for width in (70,73,93,133):
                    card=progress_text(snapshot,width=width,max_lines=8,framed=True)
                    assert all(label in card for label in ('ANALYSIS','STAGE','OUTPUT')),card
                    assert '┌' in card and len(card.splitlines())<=8
                    assert not any(label in card for label in ('SCF','Fmax','ETA'))
                    (case/('live-card-'+str(width)+'.txt')).write_text(card)
                (case/'live-wire.json').write_text(json.dumps(encode_typed(snapshot),indent=2)+'\n')
                (case/'live-status.json').write_text(json.dumps(backend.call('status'),indent=2)+'\n')
                (case/'live-progress-envelope.json').write_text(json.dumps(backend.call('progress',target=target),indent=2)+'\n')
                checks[profile+'_actual_endpoint_codec_analysis_card']=True
            if cli_selected:
                for arguments in (['show','J2.1','--json'],['progress','J2.1','--json'],
                                  ['progress','J2.1','--history','--json'],['files','J2.1','--json']):
                    cli_runner(entry,case,arguments)
            close()
            assert owner_alive(active['worker']) and owner_alive(active['group'])
            assert next(item for item in store.snapshot()['jobs'] if item['id']==job['id'])['status']=='Run'
            checks[profile+'_disconnect_preserves_execution']=True
            (case/'release').touch()
            wait_for(lambda:next(item for item in store.snapshot()['jobs'] if item['id']==job['id'])['status'] in {'Done','Fail'},timeout=40)
            finished=next(item for item in store.snapshot()['jobs'] if item['id']==job['id'])
            assert finished['status']=='Done',{key:finished.get(key) for key in ('status','exit_code','reason','logs')}
            plan=job['molecular_submission']['plan']
            result_path=Path(job['cwd'])/'job.json' if profile=='ORCA' else Path(plan['result_path'])
            record=json.loads(result_path.read_text())
            valid=record.get('scientific',{}).get('status')=='VALID' if profile!='IFCT' else record.get('validation_status')=='PASSED'
            assert valid,record
            checks[profile+'_real_existing_terminal_validator']=True
            backend=connect()
            if multiwfn_progress:
                terminal=decode_typed(backend.call('progress',target=target)['result']['snapshot'])
                assert terminal.context.attempt_id==job['attempt_id']
                assert terminal.scientific_validation=='validator-recorded-passed',terminal.to_dict()
                (case/'terminal-progress.txt').write_text(progress_text(terminal,width=93,max_lines=8,framed=True))
                (case/'terminal-wire.json').write_text(json.dumps(encode_typed(terminal),indent=2)+'\n')
                checks[profile+'_reconnect_uses_exact_attempt_and_recorded_validation']=True
            shown=backend.call('show',target=target)['result']['scientific_result']
            assert shown['status']=='RECORDED' and shown['record']==record,shown
            checks[profile+'_existing_finalizer_result_inspection']=True
            if results:
                try:backend.call('progress',target=results[0]['target'])
                except NodeError:pass
                else:raise AssertionError('Another node acquired target authority')
            locations=backend.call('locations',target=target)['result']['locations']
            location=next(item for item in locations if item['kind']=='directory' and item['node_path']
                          and result_path.is_relative_to(Path(item['node_path'])))
            copied=fetch(backend,target,{'schema':1,'storage':'scratch','purpose':'summaries','capture':'stable','large_files':False,
                'files':[{'location_id':location['location_id'],'relative':str(result_path.relative_to(location['node_path'])),'path':'result.json'}]},
                case/'replica',root=frontend)['result']
            assert copied['status']=='FETCHED' and file_hash(case/'replica/result.json')==file_hash(result_path)
            if cli_selected:
                fetch_spec={'schema':1,'storage':'scratch','purpose':'summaries','capture':'stable','large_files':False,
                    'files':[{'location_id':location['location_id'],'relative':str(result_path.relative_to(location['node_path'])),'path':'result.json'}]}
                path=case/'fetch-spec.json';path.write_text(json.dumps(fetch_spec,indent=2))
                cli_runner(entry,case,['fetch','J2.1','--spec',str(path),'--destination',str(case/'cli-replica'),'--json'])
                assert file_hash(case/'cli-replica/result.json')==file_hash(result_path)
                checks[profile+'_actual_installed_cli_over_ssh']=True
            history=backend.call('history',{'limit':10},target)['result'];assert history['page']
            checks[profile+'_history_files_selected_fetch']=True
            control('controller.stop');wait_for(lambda:not store.snapshot()['controller']['online'])
            results.append({'profile':profile,'target':target,'entry':entry,'job_id':job['display_id'],
                'runtime':config['runtime'],'native_result':str(result_path),'live_text':text,'fetch':copied})
        finally:
            (case/'release').touch(exist_ok=True)
            if store.path.exists():
                state=store.snapshot()
                owners.extend(item for item in [state['controller'].get('owner'),*[j.get(key) for j in state['jobs'] for key in ('worker','group')]] if item)
                with install():runtime.stop(store)
                deadline=time.monotonic()+10
                while any(owner_alive(item) for item in owners) and time.monotonic()<deadline:time.sleep(.05)
            close()
            remaining=[owner for owner in owners if owner_alive(owner)]
            (case/'cleanup.json').write_text(json.dumps({'owners':owners,'remaining_verified_owners':remaining},indent=2))
            all_owners.extend(owners)
            assert not remaining,remaining
    checks['same_display_ids_distinct_node_attempts']=len({(r['target']['node_id'],r['target']['attempt_id']) for r in results})==len(profiles)
    return {'checks':checks,'profiles':results,'package_origin':__import__('cmw').__file__,
            'owners':all_owners,'scientific_engine_executed':False,'qualification':'Synthetic L2/L3 only; no physical node or real engine qualification'}
