"""Real disposable Jobs receipts over invented VASP-shaped text; no engines."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tests.python_fixture import vasp_environment
from types import SimpleNamespace

from cmw.jobs import runtime
from cmw.jobs.ownership import owner_alive
from cmw.jobs.store import Store
from cmw.nodes.identity import enroll
from tests.jobs.isolated_runtime import install
from tests.jobs.test_jobs import wait_for
from tests.periodic.vasp.result_fixtures import native_texts, structure_text
from tests.periodic.vasp.test_inputs import potential
from tests.periodic.vasp.test_runner_compatibility import RunnerCompatibilityTests


CATEGORIES = ['geometry/cell', 'atom/species order', 'constraints', 'k sampling', 'potentials',
              'Hamiltonian/spin/charge', 'solver/convergence controls', 'resources',
              'checkpoint semantics', 'title_comment_format', 'missing_evidence', 'scientific_stage']


def _runner(root):
    scripts = root / 'fixture-port' / 'scripts'
    scripts.mkdir(parents=True)
    runner = scripts / 'run-vasp.sh'
    runner.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'
                      '. "$(dirname -- "$0")/environment.sh"\n'
                      'exec "${CMW_MANAGED_PYTHON:-python3}" -B "$PORT_ROOT/scripts/run_vasp.py" "$@"\n')
    runner.chmod(0o700)
    (scripts / 'environment.sh').write_text(
        vasp_environment())
    for name in ('launch.py', 'observables.py'):
        (scripts / name).write_text('"""Inert source fixture: never executed."""\n')
    RunnerCompatibilityTests.write_parser(SimpleNamespace(entrypoint=scripts / 'run_vasp.py'))
    return runner


def make_vasp_fixture(root, *, ionic_stop=False):
    """Return an enrolled owner, completed genuine benign attempt and new plan.

    The fixture controller/worker is stopped and reaped before returning. Source
    text was produced by its owned Python payload, never by a scientific engine.
    """
    root = Path(root).resolve()
    root.mkdir(parents=True, mode=0o700)
    run, scratch, storage = root / 'parent-run', root / 'scratch', root / 'storage'
    for directory in (run, scratch, storage):
        directory.mkdir(mode=0o700)
    runner = _runner(root)
    config = enroll(root / 'node', {'queues': {'research': str(root / 'state')},
        'storages': {'scratch': str(storage)},
        'profiles': {'vasp': {'engine': 'VASP', 'runner': str(runner), 'max_cpus': 1, 'max_ranks': 1}}})
    store = Store(config['queues']['research']['state'])
    flags = [[True] * 3, [False] * 3]
    texts = native_texts(relaxation=True, evaluations=2)
    texts['CONTCAR'] = structure_text(texts.pop('endpoint'), flags=flags)
    texts['POSCAR'] = structure_text(flags=flags)
    texts['KPOINTS'] = 'invented mesh\n0\nGamma\n1 1 1\n0 0 0\n'
    texts['POTCAR'] = potential(('H', 'He')).decode()
    # End-of-budget geometry remains usable without asserting ionic acceptance.
    if not ionic_stop:
        texts['OUTCAR'] = texts['OUTCAR'].replace(
            ' reached required accuracy - stopping structural energy minimisation\n', '')
    payload = root / 'invented-writer-input.json'
    payload.write_text(json.dumps(texts))
    code = ('import json; from pathlib import Path; data=json.loads(Path(' + repr(str(payload)) +
            ').read_text()); [Path(name).write_text(text) for name,text in data.items()]; '
            'print("Invented VASP-shaped text; no engine or MPI")')
    resources = {'cpus': 1, 'memory_gib': 1.0, 'mpi_ranks': 1, 'threads_per_rank': 1}
    records = []
    with install():
        added = store.add(argv=[sys.executable, '-c', code], cwd=run, name='benign successor source writer',
                          engine='VASP', **resources)
        try:
            runtime.start(store)
            wait_for(lambda: store.snapshot()['jobs'][0]['status'] in {'Done', 'Fail', 'Unknown'}, timeout=25)
            snapshot = store.snapshot()
            job = snapshot['jobs'][0]
            assert job['status'] == 'Done', job
        finally:
            snapshot = store.snapshot()
            controller = snapshot['controller']['owner']
            for item in (controller, snapshot['jobs'][0].get('worker'), snapshot['jobs'][0].get('group')):
                if item and item not in records:
                    records.append(item)
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
    job = next(item for item in store.snapshot()['jobs'] if item['id'] == added['id'])
    target = {key: config['queues']['research'][key] for key in ('node_id', 'queue_id')}
    target.update(job_id=job['id'], attempt_id=job['attempt_id'])
    for element in ('H', 'He'):
        directory = root / 'library' / element
        directory.mkdir(parents=True)
        (directory / 'POTCAR').write_bytes(potential((element,)))
    preparation = {'schema_version': 1, 'calculation': 'static', 'structure': str(run / 'CONTCAR'),
        'profile': {'name': 'explicit invented successor', 'incar': {
            'GGA': 'PE', 'ENCUT': 300, 'PREC': 'Accurate', 'ISPIN': 1, 'ISMEAR': 0, 'SIGMA': .05,
            'EDIFF': 1e-6, 'NELM': 20, 'ALGO': 'Normal', 'ISTART': 0, 'ICHARG': 2, 'NELECT': 3},
            'kpoints': {'mode': 'Gamma', 'mesh': [1, 1, 1], 'shift': [0, 0, 0]},
            'potentials': {'root': str(root / 'library'), 'variants': {'H': 'H', 'He': 'He'},
                           'requirements': {'family': 'FAKE'}}},
        'runtime': {'cpus': 1, 'mpi_ranks': 1, 'threads_per_rank': 1, 'incar': {'NCORE': 1, 'KPAR': 1}}}
    preparation_path = root / 'new-preparation.json'
    preparation_path.write_text(json.dumps(preparation, indent=2) + '\n')
    spec = {'schema': 1, 'parent': target, 'mode': 'geometry', 'engine': 'VASP',
        'name': 'Explicit new initial guess', 'resources': resources,
        'runtime': {'node_config': str(root / 'node'), 'profile': 'vasp',
                    'vasp': {'ncore': 1, 'kpar': 1, 'mpi_mode': 'synthetic', 'timeout': 10, 'stop_before': 0},
                    'run_directory': str(root / 'new-run')},
        'adapter': {'directory': str(run), 'source_mode': 'observed',
                    'preparation_spec': str(preparation_path), 'output': str(root / 'new-inputs'),
                    'scratch_root': str(scratch), 'record_directory': 'new-record'},
        'approved_changes': CATEGORIES, 'checkpoint': 'none'}
    spec_path = root / 'successor.json'
    spec_path.write_text(json.dumps(spec, indent=2) + '\n')
    return {'root': root, 'store': store, 'config': config, 'job': job, 'target': target,
            'spec': spec, 'spec_path': spec_path, 'run': run,
            'receipt': store.root / 'attempts' / job['attempt_id'] / 'payload-exit.json',
            'cleanup': cleanup}


def successor_case(fixture, name):
    """Use a fresh publication location while retaining the exact parent evidence."""
    spec = deepcopy(fixture['spec'])
    root = fixture['root'] / name
    root.mkdir(mode=0o700)
    spec['adapter']['output'] = str(root / 'inputs')
    spec['adapter']['record_directory'] = name
    spec['runtime']['run_directory'] = str(root / 'run')
    path = root / 'successor.json'
    path.write_text(json.dumps(spec, indent=2) + '\n')
    return spec, path
