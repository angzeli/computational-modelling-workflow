"""Genuine disposable ORCA-route receipts produced by a benign text emitter.

The established wrapper/finalizer executes a synthetic Python fixture, never an
ORCA scientific engine or MPI. No receipt or terminal Jobs status is fabricated.
"""
from copy import deepcopy
import json
from pathlib import Path

from cmw.core.provenance import file_hash
from cmw.jobs import molecular, runtime
from cmw.jobs.ownership import owner_alive
from cmw.jobs.runner_assets import runner_path
from cmw.jobs.store import Store
from cmw.nodes.identity import enroll
from tests.jobs.isolated_runtime import install
from tests.jobs.test_jobs import wait_for
from tests.molecular.orca.jobs_fixture import make_spec
from tests.periodic.vasp.handoff_case import wait_for_fixture_sources


def make_orca_fixture(root, *, accepted=False):
    root = Path(root).resolve()
    root.mkdir(parents=True, mode=0o700)
    storage = root / 'storage'
    storage.mkdir(mode=0o700)
    spec = make_spec(root / 'synthetic-source', profile='opt')
    input_path = Path(spec['scientific']['input'])
    input_path.write_text(input_path.read_text().replace('STO-3G OPT', 'STO-3G OPT NoAutoStart').replace('%maxcore 100', '%maxcore 819'))
    emitter = Path(spec['runtime']['executable'])
    code = emitter.read_text()
    insertion = ("print('CARTESIAN COORDINATES (ANGSTROEM)\\n---------------------------------',flush=True)\n"
                 "for row in (input_path.parent/'input.xyz').read_text().splitlines()[2:]:\n"
                 "    atom,x,y,z=row.split(); print(f'{atom} {float(x):.6f} {float(y):.6f} {float(z):.6f}',flush=True)\n"
                 "print('',flush=True)\n")
    code = code.replace("print('Iteration Energy (Eh)", insertion + "print('Iteration Energy (Eh)")
    if not accepted:
        code = code.replace("print('THE OPTIMIZATION HAS CONVERGED',flush=True)",
                            "print('The optimization did not converge',flush=True)")
    emitter.write_text(code)
    spec['runtime']['sha256'] = file_hash(emitter)
    config = enroll(root / 'node', {'queues': {'research': str(root / 'state')},
        'storages': {'scratch': str(storage)}, 'profiles': {'orca': {
            'engine': 'ORCA', 'runner': str(runner_path('orca/run_orca.sh')),
            'max_cpus': 1, 'max_ranks': 1, 'runtime': spec['runtime']}}})
    store = Store(config['queues']['research']['state'])
    records = []
    with install():
        registered = molecular.submit(store, spec)
        assert registered['status'] == 'REGISTERED', registered
        parent_id = registered['job']['id']
        try:
            store.change(parent_id, 'release')
            runtime.start(store)
            wait_for(lambda: store.snapshot()['jobs'][0]['status'] in {'Done', 'Fail', 'Unknown'}, timeout=25)
            job = store.snapshot()['jobs'][0]
            assert job['status'] == ('Done' if accepted else 'Fail'), job
        finally:
            snapshot = store.snapshot()
            controller = snapshot['controller']['owner']
            for item in (controller, snapshot['jobs'][0].get('worker'), snapshot['jobs'][0].get('group')):
                if item and item not in records:
                    records.append(item)
            for job in snapshot['jobs']:
                if job['status'] in {'Run', 'Starting'}:
                    store.change(job['id'], 'cancel', confirm=True)
            wait_for(lambda: all(j['status'] not in {'Run','Starting','Cancelling'} for j in store.snapshot()['jobs']),timeout=10)
            runtime.stop(store)
            wait_for(lambda: not store.snapshot()['controller']['online'], timeout=10)
            for child in tuple(runtime._CHILDREN):
                if controller and child.pid == controller['pid']:
                    child.wait(timeout=10)
            runtime.reap_detached()
    wait_for(lambda: not any(owner_alive(record) for record in records), timeout=10)
    cleanup = {'tracked_processes': records,
               'remaining_owned_processes': [record for record in records if owner_alive(record)]}
    (root / 'fixture-cleanup.json').write_text(json.dumps(cleanup, indent=2) + '\n')
    job = next(j for j in store.snapshot()['jobs'] if j['id'] == parent_id)
    target = {key: config['queues']['research'][key] for key in ('node_id','queue_id')}
    target.update(job_id=job['id'], attempt_id=job['attempt_id'])
    run = Path(spec['workdir'])
    record = json.loads((run/'job.json').read_text())
    assert record['scientific']['status'] == ('VALID' if accepted else 'NOT_CONVERGED'), record
    template = root/'successor-template.inp'
    template.write_text(input_path.read_text().replace('STO-3G OPT', 'STO-3G SP'))
    profile = root/'execution.json'
    profile.write_text(json.dumps({'schema_version':1,'active_profile':'explicit','profiles':{
        'explicit':{'orca':{'nprocs':1,'total_memory_gb':1},'multiwfn':{'nthreads':1,'total_memory_gb':1}}}},indent=2)+'\n')
    source = {'mode':'accepted' if accepted else 'observed','input':str(run/'stage.inp'),
              'target':str(run/'target.json'),'geometry':str(run/'stage.xyz'),'output':str(run/'stage.out')}
    if accepted:
        source['result'] = str(run/'job.json')
    successor = {'schema':1,'parent':target,'mode':'next-stage','engine':'ORCA',
        'name':'Reviewed geometry-only SP after synthetic OPT','resources':spec['resources'],'runtime':spec['runtime'],
        'adapter':{'source':source,'template_input':str(template),'execution_config':str(profile),
                   'execution_profile':'explicit','output':str(root/'new-inputs')},
        'approved_changes':['scientific_stage'],'checkpoint':'none'}
    spec_path = root/'successor.json'
    spec_path.write_text(json.dumps(successor,indent=2)+'\n')
    # Settle newly created fixture metadata before approval; production epochs stay strict.
    wait_for_fixture_sources([template, profile, spec_path], quiet_seconds=.25, timeout=3)
    return {'root':root,'store':store,'config':config,'job':job,'target':target,
            'spec':successor,'spec_path':spec_path,'run':run,'scientific_record':record,
            'receipt':store.root/'attempts'/job['attempt_id']/'payload-exit.json','cleanup':cleanup}


def orca_successor_case(fixture, name):
    spec = deepcopy(fixture['spec'])
    root = fixture['root']/name
    root.mkdir(mode=0o700)
    spec['adapter']['output'] = str(root/'new-inputs')
    path = root/'successor.json'
    path.write_text(json.dumps(spec,indent=2)+'\n')
    return spec,path
