"""Reusable benign ORCA fixtures for source and installed Jobs acceptance."""
from __future__ import annotations

from pathlib import Path
import json
import os
from uuid import uuid4

from cmw.core.provenance import file_hash
from tests.python_fixture import python_source
from tests import waiting


NATIVE = r'''#!/usr/bin/env python3
from pathlib import Path
import shutil
import sys
import time
input_path=Path(sys.argv[1])
stage=input_path.read_text().splitlines()[0].split()[-1].upper()
print('Program Version 6.1.1',flush=True)
print('INPUT FILE\n| 1> ! HF STO-3G '+stage+'\n| 2> ****END OF INPUT****',flush=True)
print('SCF Procedure:\n Maximum # iterations MaxIter .... 125',flush=True)
if stage=='OPT': print('GEOMETRY OPTIMIZATION CYCLE 1',flush=True)
print('Iteration Energy (Eh) Delta-E RMSDP MaxDP DIISErr Damp Time(sec)',flush=True)
for row in (' 1 -1.0 0.0 0.02 0.04 0.1 0.7 0.1',' 2 -1.1 -0.1 0.01 0.02 0.04 0.0 0.2'):
    print(row,flush=True); time.sleep(.15)
failed=Path(__file__).with_name('fail-stage').read_text().strip() if Path(__file__).with_name('fail-stage').exists() else ''
print('SCF DID NOT CONVERGE' if stage==failed else 'SCF CONVERGED AFTER 2 CYCLES',flush=True)
print('FINAL SINGLE POINT ENERGY -1.1',flush=True)
if stage=='OPT' and stage!=failed:
    print('THE OPTIMIZATION HAS CONVERGED',flush=True)
    shutil.copyfile(input_path.parent/'input.xyz',input_path.with_suffix('.xyz'))
if stage=='FREQ' and stage!=failed:
    print('VIBRATIONAL FREQUENCIES\n  0: 0.00 cm**-1\n  1: 100.00 cm**-1',flush=True)
print('TOTAL RUN TIME: 0 days 0 hours 0 minutes 0.3 seconds',flush=True)
print('ORCA TERMINATED NORMALLY',flush=True)
'''


def make_spec(root, *, profile='sp', workflow=False, fail_stage=None):
    root=Path(root).resolve(); root.mkdir(parents=True,exist_ok=True)
    native=root/'orca-substitute';native.write_text(python_source(NATIVE));native.chmod(0o755)
    if fail_stage:
        (root/'fail-stage').write_text(fail_stage)
    spec={'schema':1,'request_id':str(uuid4()),'engine':'ORCA','name':'synthetic ORCA '+profile,
          'intent':'held','resources':{'cpus':1,'memory_gib':1,'mpi_ranks':1,'threads_per_rank':1},
          'workdir':str(root/'attempt Ω'),
          'runtime':{'executable':str(native),'sha256':file_hash(native),'version':'6.1.1','qualification':'synthetic'}}
    if workflow:
        geometry=root/'source.xyz';geometry.write_text('2\nsynthetic H2\nH 0 0 0\nH 0 0 0.74\n')
        config=root/'workflow.json';config.write_text(json.dumps({'schema_version':1,'charge':0,'multiplicity':1,
            'resources':{'nprocs':1,'maxcore_mb_per_process':100},
            'validation':{'require_minimum':False,'imaginary_tolerance_cm1':0},
            'stages':{stage:{'keywords':'HF STO-3G','blocks':[]} for stage in ('opt','freq','sp')}}))
        spec['scientific']={'profile':profile,'workflow':True,'structure':str(geometry),'config':str(config)}
    else:
        source=root/'input.inp';source.write_text('! HF STO-3G '+profile.upper()+'\n%pal nprocs 1 end\n%maxcore 100\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n')
        spec['scientific']={'profile':profile,'input':str(source)}
        if profile=='freq':
            spec['scientific']['validation']={'require_minimum':False,'imaginary_tolerance_cm1':0}
    return spec


def wait_for(predicate, *, timeout=15):
    return waiting.wait_for(predicate, timeout=timeout, interval=.04, what='Timed out waiting for isolated ORCA lifecycle')


def owned_jobs_surface(root):
    """Common submit/Store/worker/readers/adapter/finalizer, with fixture-only guard."""
    from cmw.jobs import runtime
    from cmw.jobs.molecular import submit
    from cmw.jobs.progress import resolve_context
    from cmw.jobs.store import Store
    from cmw.progress.service import ObservationService
    from tests.jobs.isolated_runtime import install
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    report={}
    with install():
        for stage in ('sp','opt'):
            spec=make_spec(root/stage,profile=stage)
            store=Store(root/stage/'state');observer=ObservationService()
            job=None
            try:
                result=submit(store,spec)
                assert result['status']=='REGISTERED',result
                job=result['job'];assert job['status']=='Hold',job
                context=resolve_context(store,job['display_id'])
                assert context.binding_status=='BOUND',context
                pending=observer.poll(context)
                assert not pending.metrics,pending
                replay=submit(store,spec)
                assert replay['job']['attempt_id']==job['attempt_id'],replay
                store.change(job['id'],'release')
                runtime.start(store)
                seen=[]
                def done():
                    current=store.snapshot()['jobs'][0]
                    snapshot=observer.poll(resolve_context(store,job['display_id']))
                    if snapshot.metrics:seen.append(snapshot)
                    return current if current['status'] in {'Done','Fail','Cancelled','Unknown'} else None
                completed=wait_for(done)
                assert completed['status']=='Done',completed
                assert seen,'Real incremental native output never reached the adapter'
                record=json.loads((Path(spec['workdir'])/'job.json').read_text())
                assert record['scientific']['status']=='VALID',record['scientific']
                history=observer.history_page(resolve_context(store,job['display_id']))
                assert history.entries,history
                report[stage]={'status':completed['status'],'scientific':record['scientific']['status'],
                    'metrics':len(seen[-1].metrics),'history':len(history.entries),
                    'source':context.sources[0].path,'attempt_id':job['attempt_id']}
            finally:
                observer.close()
                if store.path.exists():
                    for current in store.snapshot()['jobs']:
                        if current['status'] in {'Run','Starting'}:store.change(current['id'],'cancel',confirm=True)
                    wait_for(lambda:not any(item['status'] in {'Run','Starting','Cancelling'} for item in store.snapshot()['jobs']))
                    owner=store.snapshot()['controller']['owner']
                    runtime.stop(store)
                    wait_for(lambda:not store.snapshot()['controller']['online'])
                    for child in list(runtime._CHILDREN):
                        if owner and child.pid==owner['pid']:child.wait(timeout=5)
                runtime.reap_detached()
    return report
