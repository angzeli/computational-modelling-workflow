"""Disposable study fixtures with invented text and genuine benign Jobs receipts."""
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from cmw.core.provenance import stable_hash
from cmw.jobs import runtime
from cmw.jobs.ownership import owner_alive
from cmw.jobs.store import Store
from cmw.nodes.identity import enroll
from tests.jobs.isolated_runtime import install
from tests.jobs.successor_fixtures import _runner
from tests.jobs.test_jobs import wait_for
from tests.periodic.vasp import fake_handoff_runner
from tests.periodic.vasp.handoff_case import prepare_case, bind_completed_case
from tests.periodic.vasp.result_case import digest, write_json
from tests.periodic.vasp.result_fixtures import native_texts


def make_study(root):
    """Create source fixtures only; no scientific inputs are submitted or started."""
    root = Path(root).resolve()
    root.mkdir(mode=0o700)
    baseline = prepare_case(root / 'baseline')
    runner = _runner(root)
    entrypoint = runner.parent / 'run_vasp.py'
    native_script = Path(fake_handoff_runner.__file__).read_text()
    begin = native_script.index('    parser = argparse.ArgumentParser()', native_script.index('def main():'))
    end = native_script.index('    source, output = ', begin)
    native_script = native_script[:begin] + '    args = parser().parse_args()\n' + native_script[end:]
    native_script = native_script.replace('    output.mkdir()',
        "    import re\n    encut = re.search(r'(?m)^ENCUT\\s*=\\s*([0-9.]+)', (source / 'INCAR').read_text())\n"
        "    payload = payload[str(int(float(encut[1])))]\n    output.mkdir()")
    entrypoint.write_text(entrypoint.read_text() + '\n' + native_script)
    prepared_spec = json.loads((baseline['root'] / 'prepare.json').read_text())
    incar = prepared_spec['profile']['incar']
    payloads = {}
    for encut in (300, 400):
        texts = native_texts(settings={**incar, 'ENCUT': encut, 'NCORE': 2, 'KPAR': 1})
        texts.pop('endpoint'); texts.pop('INCAR')
        if encut == 400:
            texts['OUTCAR'] = (texts['OUTCAR'].replace('TOTEN = -3.00000000', 'TOTEN = -2.99000000')
                .replace('without entropy= -2.99000000', 'without entropy= -2.98000000')
                .replace('energy(sigma->0) = -2.99500000', 'energy(sigma->0) = -2.98500000'))
            texts['OSZICAR'] = texts['OSZICAR'].replace('-.30000000E+01', '-.29900000E+01').replace('-.29950000E+01', '-.29850000E+01')
        payloads[str(encut)] = {'outputs': texts, 'exit_code': 0}
    write_json(runner.parent / 'payload.json', payloads)
    storage, output, scratch, runs = (root / name for name in ('storage', 'inputs', 'scratch', 'runs'))
    for directory in (storage, output, scratch, runs):
        directory.mkdir(mode=0o700)
    records = scratch / 'records'
    records.mkdir(mode=0o700)
    config = enroll(root / 'node', {'queues': {'research': str(root / 'state')},
        'storages': {'scratch': str(storage)},
        'profiles': {'vasp': {'schema': 2, 'runner_interpreter': sys.executable, 'engine': 'VASP', 'runner': str(runner), 'max_cpus': 2, 'max_ranks': 2}}})
    store = Store(config['queues']['research']['state'])
    study_id = uuid4().hex
    manifest = {'schema': 1, 'study_id': study_id, 'name': 'Invented static ENCUT study', 'engine': 'VASP',
        'baseline': {'directory': str(baseline['inputs']), 'inputs': baseline['prepared']['prepared_inputs']},
        'preparation_spec': str(baseline['root'] / 'prepare.json'),
        'matrix': {'encut': [300, 400], 'kmeshes': [{'name': 'gamma', 'mode': 'Gamma', 'mesh': [1, 1, 1], 'shift': [0, 0, 0]}],
            'structures': [{'name': 'fixed', 'path': str(baseline['inputs'] / 'POSCAR'), 'sha256': digest(baseline['inputs'] / 'POSCAR')}]},
        'cases': [{'name': 'encut-' + str(encut), 'structure': 'fixed', 'encut': encut, 'kmesh': 'gamma'} for encut in (300, 400)],
        'varied': ['ENCUT'], 'scientific_policy': {'path': str(baseline['policy_path']), 'sha256': digest(baseline['policy_path'])},
        'metric': {'name': 'sigma_to_zero', 'unit': 'eV'}, 'normalization': {'kind': 'none'},
        'comparison_rule': {'kind': 'absolute-to-reference',
            'reference_case': stable_hash({'study_id': study_id, 'name': 'encut-300'}), 'tolerance': .02},
        'execution': {'node_id': config['node_id'], 'queue_id': config['queues']['research']['queue_id'],
            'node_config': str(root / 'node'), 'profile': 'vasp',
            'resources': {'cpus': 2, 'memory_gib': 1.0, 'mpi_ranks': 2, 'threads_per_rank': 1},
            'vasp': {'ncore': 2, 'kpar': 1, 'mpi_mode': 'native', 'timeout': 30, 'stop_before': 0},
            'run_root': str(runs)},
        'output_root': str(output), 'scratch_root': str(scratch), 'record_root': str(records)}
    path = root / 'study-manifest.json'
    write_json(path, manifest)
    return {'root': root, 'store': store, 'config': config, 'manifest_path': path,
            'manifest': manifest, 'baseline': baseline, 'runner': runner}


make_study_fixture = make_study


def complete_study(fixture, record_path):
    """Explicitly run only this fixture's registered benign cases, then bind them."""
    root, store = fixture['root'], fixture['store']
    record = json.loads(Path(record_path).read_text())
    plan = record['plan']
    jobs = store.snapshot()['jobs']
    assert len(jobs) == len(plan['preparation']['cases']) == 2
    expected = {case['case_id'] for case in plan['preparation']['cases']}
    for job in jobs:
        assert job['status'] == 'Hold'
        assert job['argv'][0] == str(fixture['runner'])
        assert Path(job['argv'][0]).is_relative_to(root / 'fixture-port')
        assert job['node_submission']['study']['case_id'] in expected
        store.change(job['id'], 'release')
    tracked = []
    with install():
        try:
            runtime.start(store)
            wait_for(lambda: all(job['status'] in {'Done', 'Fail', 'Unknown'} for job in store.snapshot()['jobs']), timeout=30)
            snapshot = store.snapshot()
            assert all(job['status'] == 'Done' for job in snapshot['jobs']), snapshot['jobs']
        finally:
            snapshot = store.snapshot()
            controller = snapshot['controller']['owner']
            for process in (controller, *(job.get(key) for job in snapshot['jobs'] for key in ('worker', 'group'))):
                if process and process not in tracked:
                    tracked.append(process)
            runtime.stop(store)
            wait_for(lambda: not store.snapshot()['controller']['online'], timeout=10)
            for child in tuple(runtime._CHILDREN):
                if controller and child.pid == controller['pid']:
                    child.wait(timeout=10)
            runtime.reap_detached()
    wait_for(lambda: not any(owner_alive(process) for process in tracked), timeout=10)
    fixture['cleanup'] = {'tracked_processes': tracked,
        'remaining_owned_processes': [process for process in tracked if owner_alive(process)]}
    write_json(root / 'fixture-cleanup.json', fixture['cleanup'])
    snapshot = store.snapshot()
    selection = {'schema': 1, 'study_id': plan['study_id'], 'plan_digest': record['plan_digest'], 'cases': {}}
    for case in plan['preparation']['cases']:
        job, = [job for job in snapshot['jobs'] if job['node_submission']['study']['case_id'] == case['case_id']]
        private = root / ('binding-' + case['name'])
        private.mkdir(mode=0o700)
        native = Path(job['argv'][job['argv'].index('--output') + 1])
        selected = deepcopy(snapshot)
        selected['jobs'] = [job]
        bound = {'root': private, 'run': native, 'scratch': Path(fixture['manifest']['scratch_root']),
            'policy': fixture['baseline']['policy'], 'policy_path': fixture['baseline']['policy_path'],
            'preparation_record': Path(case['record_directory']) / 'preparation.json'}
        bind_completed_case(bound, selected)
        target = {key: plan[key] for key in ('node_id', 'queue_id')}
        target.update(job_id=job['id'], attempt_id=job['attempt_id'])
        selection['cases'][case['case_id']] = {'target': target, 'spec_path': str(bound['spec_path'])}
    path = root / 'selected-results.json'
    write_json(path, selection)
    return str(path)
