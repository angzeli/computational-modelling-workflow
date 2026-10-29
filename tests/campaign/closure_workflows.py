"""Bounded installed study/review/successor/molecular closure driver.

Run from copied test support outside the checkout with -I -B. All payloads are
public synthetic text emitters; original Jobs receipts are never fabricated.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import csv
import json
from importlib.metadata import version
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback
import unittest
from uuid import uuid4


SUPPORT = Path(__file__).resolve().parents[2]


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def installed_origin():
    import cmw
    assert sys.flags.isolated and not os.environ.get('PYTHONPATH')
    assert not (SUPPORT / 'src').exists(), 'Copy tests only; source masking is forbidden'
    package = Path(cmw.__file__).resolve()
    development = os.environ.get('CMW_CLOSURE_DEVELOPMENT_SOURCE_ROOT')
    installed = package.is_relative_to(Path(sys.prefix).resolve())
    if development:
        expected = Path(development).resolve() / 'src' / 'cmw' / '__init__.py'
        assert installed or package == expected, (package, expected)
    else:
        assert installed, package
    return {'python': sys.executable, 'package': str(package), 'support': str(SUPPORT),
            'version': version('computational-modelling-workflow'), 'prefix': sys.prefix, 'python_version': sys.version,
            'development_source_root': development, 'installed_package': installed}


@contextmanager
def monitored(name, destination):
    """Use the campaign's shared classified ledger before any child launch."""
    from tests.campaign.support import BUDGETS, ProcessLedger
    from tests.campaign.qualification_v2 import outcomes
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stopped = threading.Event()
    samples, sampling_errors = [], []
    with ProcessLedger(name) as ledger:
        def sample():
            while not stopped.is_set():
                try:
                    samples.append(ledger.sample())
                except BaseException as exc:
                    sampling_errors.append({'type': type(exc).__name__, 'message': str(exc)})
                    return
                stopped.wait(.1)
        sampler = threading.Thread(target=sample, name='closure-owned-sampler', daemon=True)
        sampler.start()
        body_failed = False
        try:
            yield ledger
        except BaseException:
            body_failed = True
            raise
        finally:
            # The work deadline and the bounded cleanup budget are separate.
            if threading.current_thread() is threading.main_thread():
                signal.alarm(0)
            cleanup_deadline = time.monotonic() + 30
            stopped.set()
            sampler.join(timeout=2)
            ledger.finish_sampling()
            cleanup_error = None
            try:
                remaining = ledger.cleanup(cleanup_deadline)
            except BaseException as exc:
                remaining = []
                cleanup_error = type(exc).__name__ + ': ' + str(exc)
            evidence = ledger.report()
            evidence['cleanup_error'] = cleanup_error
            live = [process for sample in samples for process in sample['processes']]
            peaks = {'rss_bytes': max((process['rss_bytes'] for process in live if 'rss_bytes' in process), default=None),
                     'fds': max((process['fds'] for process in live if 'fds' in process), default=None),
                     'owned_subprocesses': max((sum(process['pid'] != os.getpid() for process in sample['processes'])
                                               for sample in samples), default=0)}
            resource_ok = (all(('rss_bytes' not in process or process['rss_bytes'] <= BUDGETS['process_rss_bytes'])
                           and ('fds' not in process or process['fds'] <= BUDGETS['process_fds']) for process in live)
                           and peaks['owned_subprocesses'] <= BUDGETS['owned_subprocesses'])
            evidence.update(peaks=peaks, resource_limits_passed=resource_ok, samples=samples, sampler_errors=sampling_errors,
                            sampler_stopped=not sampler.is_alive(), remaining=remaining)
            if sampling_errors:
                evidence.update(observation_accounting='FAIL', resource_observability='RESOURCE_QUALIFICATION_INCOMPLETE')
            evidence['outcomes'] = outcomes(functional_passed=not body_failed,
                safety_passed=evidence['control_identity_safety'] == 'PASS',
                cleanup_passed=cleanup_error is None and not remaining and not ledger.cleanup_errors and not sampler.is_alive(),
                observation=evidence, resource_limits_passed=resource_ok)
            save(destination, evidence)
            if not body_failed:
                summary = {'evidence': str(destination), 'coverage_passed': evidence['coverage_passed'],
                    'resource_limits_passed': resource_ok, 'remaining_records': len(remaining),
                    'sampling_errors': sampling_errors, 'sampler_stopped': not sampler.is_alive()}
                assert evidence['outcomes']['execution_passed'], summary


def child_command(arguments, evidence):
    return [sys.executable, '-I', '-B', '-c',
        'import sys;sys.path.insert(0,sys.argv.pop(1));'
        'from tests.campaign.closure_workflows import child_main;child_main()',
        str(SUPPORT), str(evidence), *arguments]


def installed_detached(arguments, log):
    from cmw.jobs import runtime
    from cmw.jobs.store import private_opener
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    runtime.reap_detached()
    with open(log, 'ab', buffering=0, opener=private_opener) as output:
        process = subprocess.Popen(child_command(arguments, log.with_suffix('.ownership.json')),
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
            close_fds=True, cwd='/', env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
    runtime._CHILDREN.append(process)
    return process


def configure_children():
    from tests.jobs import isolated_runtime
    isolated_runtime.detached = installed_detached
    # The fixture launcher limits activity discovery to task-owned entries.
    # No source path or runtime-trust hook is substituted.
    return isolated_runtime


def child_main():
    origin = installed_origin()
    evidence = Path(sys.argv.pop(1))
    save(evidence.with_suffix('.origin.json'), origin)
    launcher = configure_children()
    with monitored('installed-fixture-child:' + ':'.join(sys.argv[1:3]), evidence):
        launcher.main()


def wait_job(store, deadline):
    while time.monotonic() < deadline:
        snapshot = store.snapshot()
        if all(job['status'] in {'Done', 'Fail', 'Unknown', 'Hold'} for job in snapshot['jobs']):
            return snapshot
        time.sleep(.05)
    raise AssertionError('Bounded benign worker deadline exceeded')


def execute_registered(store, job_ids):
    from cmw.jobs import runtime
    from tests.jobs.isolated_runtime import install
    from tests.jobs.test_jobs import wait_for
    selected = set(job_ids)
    with install():
        try:
            for job in store.snapshot()['jobs']:
                if job['id'] in selected:
                    assert job['status'] == 'Hold'
                    store.change(job['id'], 'release')
            runtime.start(store)
            snapshot = wait_job(store, time.monotonic() + 40)
            jobs = [job for job in snapshot['jobs'] if job['id'] in selected]
            assert len(jobs) == len(selected)
            assert all(job['status'] == 'Done' for job in jobs), jobs
            for job in jobs:
                receipt = store.root / 'attempts' / job['attempt_id'] / 'payload-exit.json'
                assert receipt.is_file(), job
                assert json.loads(receipt.read_text())['exit_code'] == 0
                assert receipt.parent.name == job['attempt_id']
            return snapshot
        finally:
            # Only this disposable queue's exact registered attempts are controlled.
            for job in store.snapshot()['jobs']:
                if job['id'] in selected and job['status'] in {'Run', 'Starting'}:
                    store.change(job['id'], 'cancel', confirm=True)
            wait_for(lambda: all(job['status'] not in {'Run', 'Starting', 'Cancelling'}
                                 for job in store.snapshot()['jobs']), timeout=10)
            runtime.stop(store)
            wait_for(lambda: not store.snapshot()['controller']['online'], timeout=10)
            for process in tuple(runtime._CHILDREN):
                code = process.wait(timeout=10)
                assert code == 0, {'pid': process.pid, 'returncode': code}
            runtime.reap_detached()


def review(store, job, options):
    from cmw.jobs.review import ReviewService
    service = ReviewService(store)
    try:
        record = service.start(job['id'], options, uuid4().hex, job['attempt_id'])
        deadline = time.monotonic() + 18
        while record.status == 'PENDING' and time.monotonic() < deadline:
            time.sleep(.05)
            record = service.poll(job['id'], record.review_id, job['attempt_id'])
        assert record.status == 'COMPLETE', record.to_dict()
        assert record.attempt_id == job['attempt_id'] and record.job_id == job['id']
        return record
    finally:
        service.close()


def study_fixture(root):
    from tests.jobs.study_fixtures import make_study
    from tests.periodic.vasp.result_fixtures import native_texts
    fixture = make_study(root)
    manifest = fixture['manifest']
    manifest['matrix']['encut'] = [300, 400, 500, 600]
    manifest['cases'] = [dict(name='encut-' + str(value), structure='fixed', encut=value, kmesh='gamma')
                         for value in manifest['matrix']['encut']]
    spec = json.loads(Path(manifest['preparation_spec']).read_text())
    # Handwritten energy oracle deliberately includes non-monotonic changes.
    energies = {300: (-3., -2.99, -2.995), 400: (-2.99, -2.98, -2.985),
                500: (-2.996, -2.986, -2.991), 600: (-2.985, -2.975, -2.98)}
    payloads = {}
    for encut, (free, entropy, sigma) in energies.items():
        texts = native_texts(settings={**spec['profile']['incar'], 'ENCUT': encut, 'NCORE': 2, 'KPAR': 1})
        texts.pop('endpoint'); texts.pop('INCAR')
        texts['OUTCAR'] = (texts['OUTCAR'].replace('TOTEN = -3.00000000', f'TOTEN = {free:.8f}')
            .replace('without entropy= -2.99000000', f'without entropy= {entropy:.8f}')
            .replace('energy(sigma->0) = -2.99500000', f'energy(sigma->0) = {sigma:.8f}'))
        texts['OSZICAR'] = texts['OSZICAR'].replace('-.30000000E+01', f'{free:.8E}').replace('-.29950000E+01', f'{sigma:.8E}')
        payloads[str(encut)] = {'outputs': texts, 'exit_code': 0}
    save(fixture['runner'].parent / 'payload.json', payloads)
    save(fixture['manifest_path'], manifest)
    return fixture


def connection_metadata(config, config_root, job):
    queue = config['queues']['research']
    target = {'node_id': config['node_id'], 'queue_id': queue['queue_id'],
              'job_id': job['id'], 'attempt_id': job['attempt_id']}
    entry = {'transport': 'local', 'config_root': str(config_root), 'node_id': config['node_id'],
        'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
        'queues': {'research': queue['queue_id']}, 'default_queue': 'research', 'read_only': False}
    command = [sys.executable, '-I', '-B', '-m', 'cmw.cli', 'nodes', 'endpoint',
               '--config', str(config_root), '--queue', 'research']
    return {'entry': entry, 'command': command, 'target': target}


def study_workflow(root, report):
    from cmw.jobs.study import StudyService
    from cmw.jobs.successor import SuccessorService
    from tests.jobs.successor_fixtures import CATEGORIES
    from tests.periodic.vasp.handoff_case import bind_completed_case
    from cmw.progress.models import SourceDescriptor
    fixture = study_fixture(root / 'study')
    store, manifest = fixture['store'], fixture['manifest']
    service = StudyService(store)
    plan = service.plan(fixture['manifest_path'])
    assert plan.status == 'READY'
    assert service.plan(fixture['manifest_path']).plan_digest == plan.plan_digest
    assert not store.snapshot()['jobs']
    assert not any(Path(case['output']).exists() for case in json.loads(plan.payload_json)['preparation']['cases'])
    prepared = service.materialize(fixture['manifest_path'], plan.plan_digest)
    assert prepared.status == 'MATERIALIZED', prepared.payload_json
    record_path = json.loads(prepared.payload_json)['record_path']
    record = json.loads(Path(record_path).read_text())
    cases = record['plan']['preparation']['cases']
    assert len(cases) == 4
    for case in cases:
        assert {p.name for p in Path(case['output']).iterdir()} == {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'}
    registered = service.register(record_path, plan.plan_digest, manifest['study_id'])
    assert registered.status == 'REGISTERED', registered.payload_json
    replay = service.register(record_path, plan.plan_digest, manifest['study_id'])
    assert all(case['replayed'] for case in json.loads(replay.payload_json)['cases'])
    jobs = store.snapshot()['jobs']
    assert len(jobs) == 4 and all(job['status'] == 'Hold' and job['started_at'] is None for job in jobs)
    snapshot = execute_registered(store, [job['id'] for job in jobs])
    first_job = next(job for job in snapshot['jobs'] if job['name'] == 'encut-300')
    report.setdefault('pty_cases', {})['study'] = {'state': str(store.root), 'job_id': first_job['id'],
        'attempt_id': first_job['attempt_id'], 'study_id': manifest['study_id'], 'plan_digest': plan.plan_digest,
        'series': manifest['name'], 'name': first_job['name'], 'source_case': str(root / 'study'),
        **connection_metadata(fixture['config'], root / 'study/node', first_job)}
    selection = {'schema': 1, 'study_id': manifest['study_id'], 'plan_digest': plan.plan_digest, 'cases': {}}
    bindings = {}
    for case in cases:
        job, = [job for job in snapshot['jobs'] if job['node_submission']['study']['case_id'] == case['case_id']]
        folder = root / ('binding-' + case['name']); folder.mkdir(mode=0o700)
        selected = deepcopy(snapshot); selected['jobs'] = [job]
        bound = {'root': folder, 'run': Path(job['argv'][job['argv'].index('--output') + 1]),
                 'scratch': Path(manifest['scratch_root']), 'policy': fixture['baseline']['policy'],
                 'policy_path': fixture['baseline']['policy_path'],
                 'preparation_record': Path(case['record_directory']) / 'preparation.json'}
        # Preserve the established scientific fixture preparation precondition.
        # No refused inspection is retried or retroactively qualified.
        bind_completed_case(bound, selected)
        target = {key: record['plan'][key] for key in ('node_id', 'queue_id')}
        target.update(job_id=job['id'], attempt_id=job['attempt_id'])
        selection['cases'][case['case_id']] = {'target': target, 'spec_path': str(bound['spec_path'])}
        bindings[case['case_id']] = (job, bound)
    report['reached_stage'] = 'four-exact-attempt-reviews'
    report['selected_reviews'] = []
    for case in cases:
        job, bound = bindings[case['case_id']]
        reviewed = review(store, job, {'policy_path': str(bound['policy_path']), 'spec_path': str(bound['spec_path'])})
        domains = {item.name: item.status for item in reviewed.domains}
        assert domains['operational'] == domains['policy'] == 'PASS', reviewed.to_dict()
        assert domains['finalization'] == 'NOT ASSESSED'
        assert (reviewed.node_id, reviewed.queue_id) == (record['plan']['node_id'], record['plan']['queue_id'])
        report['selected_reviews'].append(reviewed.to_dict())
    selection_path = root / 'selected-results.json'; save(selection_path, selection)
    report['reached_stage'] = 'study-comparison'
    compared = service.compare(record_path, str(selection_path), str(root / 'comparison'))
    payload = json.loads(compared.payload_json)
    assert compared.status == 'COMPLETE' and payload['qualified_count'] == 4, payload
    assert [row['energy']['value'] for row in payload['rows']] == [-2.995, -2.985, -2.991, -2.98]
    assert [row['delta'] for row in payload['rows']] == [0, .01, .004, .015]
    assert payload['rule_result'] == 'PASS'
    assert all(set(row['domains'].values()) == {'PASS'} for row in payload['rows'])
    exported = payload['export']; assert set(exported) == {'json', 'csv', 'md'}
    saved = json.loads(Path(exported['json']).read_text())
    assert saved == {key: value for key, value in payload.items() if key != 'export'}
    assert saved['metric'] == {'name': 'sigma_to_zero', 'unit': 'eV'}
    assert saved['normalization'] == {'kind': 'none'} and saved['delta_unit'] == 'eV'
    assert saved['comparison_rule'] == manifest['comparison_rule']
    assert saved['study_id'] == manifest['study_id'] and saved['study_plan_digest'] == plan.plan_digest
    assert saved['comparison_rule']['reference_case'] == cases[0]['case_id']
    with Path(exported['csv']).open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 4
    markdown = Path(exported['md']).read_text()
    for text in ('Metric: sigma_to_zero (eV).', 'Normalization: none; delta units: eV.',
                 'Reference: ' + cases[0]['case_id'] + '; absolute tolerance: 0.02 eV.',
                 'Study digest: ' + saved['study_digest'] + '.'):
        assert text in markdown, text
    for case, row, exported_row, energy, delta in zip(cases, saved['rows'], rows,
            (-2.995, -2.985, -2.991, -2.98), (0, .01, .004, .015)):
        assert row['target'] == selection['cases'][case['case_id']]['target']
        assert exported_row['case_id'] == row['case_id'] == case['case_id']
        assert exported_row['name'] == row['name'] == case['name']
        assert row['status'] == exported_row['status'] == 'QUALIFIED'
        assert json.loads(exported_row['target']) == row['target']
        assert json.loads(exported_row['policy_identity']) == row['policy_identity']
        assert row['sources'] and json.loads(exported_row['source_identities']) == row['sources']
        assert all(exported_row[name] == row['domains'][name] == 'PASS'
                   for name in ('process', 'completeness', 'native', 'policy', 'binding'))
        assert exported_row['metric'] == 'sigma_to_zero' and exported_row['unit'] == 'eV'
        assert float(exported_row['value']) == energy and float(exported_row['delta']) == delta
        assert exported_row['rule_result'] == row['rule_result'] == 'PASS'
        assert exported_row['study_digest'] == saved['study_digest']
        assert exported_row['normalization'] == 'none' and exported_row['delta_unit'] == 'eV'
        assert '| ' + case['name'] + ' | QUALIFIED | ' in markdown
        assert json.dumps(row['target'], sort_keys=True) in markdown
        assert json.dumps(row['policy_identity'], sort_keys=True) in markdown
        assert all(source['resolved_path'] in markdown and source['sha256'] in markdown for source in row['sources'])
    report['export_contract_limits'] = ['CSV carries no separate reference column; reference is asserted in JSON and Markdown.',
        'Markdown carries qualified evidence and provenance, without separate per-domain columns; domains are asserted in JSON and CSV.']
    report['study'] = {'plan': plan.to_dict(), 'comparison': compared.to_dict(),
                       'selection': selection, 'receipt_paths': [str(value[1]['receipt']) for value in bindings.values()]}
    report['reached_stage'] = 'selected-and-held-reviews'
    first_id = cases[0]['case_id']; parent, bound = bindings[first_id]
    held_review = held_unknown_review(store, bound['run'])
    report['held_review'] = held_review.to_dict()
    report['reached_stage'] = 'successor'
    successor_spec = {'schema': 1, 'parent': selection['cases'][first_id]['target'], 'mode': 'geometry',
        'engine': 'VASP', 'name': 'Explicit study reference successor', 'resources': manifest['execution']['resources'],
        'runtime': {'node_config': manifest['execution']['node_config'], 'profile': 'vasp',
                    'vasp': manifest['execution']['vasp'], 'run_directory': str(root / 'successor-run')},
        'adapter': {'directory': str(bound['run']), 'source_mode': 'observed',
                    'preparation_spec': manifest['preparation_spec'], 'output': str(root / 'successor-inputs'),
                    'scratch_root': manifest['scratch_root'], 'record_directory': 'study-successor'},
        'approved_changes': CATEGORIES, 'checkpoint': 'none'}
    successor_path = root / 'successor.json'; save(successor_path, successor_spec)
    successors = SuccessorService(store)
    next_plan = successors.plan(parent['id'], str(successor_path), parent['attempt_id'])
    assert next_plan.status == 'READY', next_plan.payload_json
    materialized = successors.materialize(parent['id'], str(successor_path), next_plan.plan_digest, parent['attempt_id'])
    assert materialized.status == 'MATERIALIZED', materialized.payload_json
    next_record = json.loads(materialized.payload_json)['record_path']
    request = uuid4().hex
    child = successors.register(parent['id'], next_record, next_plan.plan_digest, request, parent['attempt_id'])
    retry = successors.register(parent['id'], next_record, next_plan.plan_digest, request, parent['attempt_id'])
    child_data = json.loads(child.payload_json)
    assert child_data['child'] == json.loads(retry.payload_json)['child']
    assert json.loads(retry.payload_json)['replayed'] and child_data['child']['status'] == 'Hold'
    assert child_data['child']['attempt_id'] != parent['attempt_id']
    report['successor'] = {'plan': next_plan.to_dict(), 'registered': child.to_dict()}
    report['reached_stage'] = 'comparison-refusal-controls'
    wrong_case = deepcopy(selection)
    wrong_case['cases'][first_id]['target'] = deepcopy(selection['cases'][cases[1]['case_id']]['target'])
    case_path = root / 'wrong-case-selection.json'; save(case_path, wrong_case)
    case_report = json.loads(service.compare(record_path, str(case_path)).payload_json)
    assert case_report['rows'][0]['status'] != 'QUALIFIED' and case_report['rows'][0]['delta'] is None
    wrong_owner = deepcopy(selection)
    wrong_owner['cases'][first_id]['target']['node_id'] = uuid4().hex
    owner_path = root / 'wrong-owner-selection.json'; save(owner_path, wrong_owner)
    owner_report = json.loads(service.compare(record_path, str(owner_path)).payload_json)
    assert owner_report['rows'][0]['status'] != 'QUALIFIED' and owner_report['rows'][0]['delta'] is None
    wrong = deepcopy(selection); wrong['cases'][first_id]['target']['attempt_id'] = uuid4().hex
    wrong_path = root / 'wrong-selection.json'; save(wrong_path, wrong)
    wrong_report = json.loads(service.compare(record_path, str(wrong_path)).payload_json)
    assert wrong_report['rows'][0]['status'] != 'QUALIFIED' and wrong_report['rows'][0]['delta'] is None
    missing = {**selection, 'cases': {}}; missing_path = root / 'missing-selection.json'; save(missing_path, missing)
    absent = json.loads(service.compare(record_path, str(missing_path)).payload_json)
    assert absent['qualified_count'] == 0 and all(row['delta'] is None for row in absent['rows'])
    with (bound['run'] / 'OUTCAR').open('a') as stream:
        stream.write('\nsynthetic source mutation after accepted inspection\n')
    changed = json.loads(service.compare(record_path, str(selection_path)).payload_json)
    assert changed['rows'][0]['status'] != 'QUALIFIED' and changed['rows'][0]['delta'] is None
    report['negative_controls'] = {'wrong_case': case_report, 'wrong_owner': owner_report, 'wrong_attempt': wrong_report, 'missing': absent, 'source_change': changed}
    assert not store.snapshot()['controller']['online']


def held_unknown_review(store, run):
    """Execute the exact post-export held branch without a completion receipt."""
    from cmw.progress.models import SourceDescriptor
    held = store.add(argv=['/usr/bin/true'], cwd=run, name='held UNKNOWN review control', engine='VASP', hold=True,
        progress_sources=tuple(SourceDescriptor(role.lower(), str(run / role))
                               for role in ('OUTCAR', 'OSZICAR', 'POSCAR')))
    held_review = review(store, held, {})
    domains = {item.name: item.status for item in held_review.domains}
    assert domains['operational'] == 'UNKNOWN', held_review.to_dict()
    assert domains['policy'] == domains['finalization'] == 'NOT ASSESSED'
    return held_review


def observation_rollup(root, functional_passed):
    """A nested monitor failure changes qualification, never recorded child exits."""
    paths = sorted({*Path(root).rglob('ownership.json'), *Path(root).rglob('*.ownership.json')})
    reports, monitor_errors = [], []
    for path in paths:
        try:
            value = json.loads(path.read_text())
            reports.append({'path': str(path), 'outcomes': value['outcomes']})
        except (OSError, ValueError, KeyError) as exc:
            monitor_errors.append({'path': str(path), 'error': repr(exc)})
    keys = ('functional_result', 'control_identity_safety', 'cleanup_result',
            'observation_accounting', 'resource_observability', 'performance_result', 'visual_result')
    result = {}
    for key in keys:
        statuses = [report['outcomes'][key] for report in reports]
        result[key] = ('FAIL' if 'FAIL' in statuses else 'RESOURCE_QUALIFICATION_INCOMPLETE'
            if 'RESOURCE_QUALIFICATION_INCOMPLETE' in statuses else 'PASS' if 'PASS' in statuses else 'NOT_ASSESSED')
    if not functional_passed:
        result['functional_result'] = 'FAIL'
    if not reports or monitor_errors:
        result['observation_accounting'] = 'FAIL'
        result['resource_observability'] = 'RESOURCE_QUALIFICATION_INCOMPLETE'
    return {'outcomes': result, 'qualified': all(value in {'PASS', 'NOT_ASSESSED'} for value in result.values()),
            'monitor_reports': reports, 'monitor_errors': monitor_errors}


def run_component(name, action, report):
    components = report.setdefault('components', {})
    started = time.monotonic()
    try:
        action()
    except TimeoutError:
        raise
    except Exception as exc:
        components[name] = {'status': 'FAIL', 'stage': report.get('reached_stage'),
            'error': {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}}
    else:
        components[name] = {'status': 'PASS'}
    components[name]['seconds'] = time.monotonic() - started


def orca_workflow(root, report):
    from tests.jobs.successor_orca_fixtures import make_orca_fixture
    report['reached_stage'] = 'orca-benign-worker'
    orca = make_orca_fixture(root, accepted=True)
    assert orca['job']['status'] == 'Done' and orca['receipt'].is_file()
    assert orca['scientific_record']['scientific']['status'] == 'VALID'
    report['orca'] = {'target': orca['target'], 'receipt': str(orca['receipt']), 'record': orca['scientific_record']}


def multiwfn_workflow(root, profile, report):
    from cmw.jobs import molecular
    from cmw.jobs.store import Store
    from cmw.jobs.progress import resolve_context
    from cmw.progress.service import ObservationService
    from tests.molecular.multiwfn.jobs_fixtures import make_spec
    report['reached_stage'] = 'multiwfn-' + profile
    spec = make_spec(root / profile, profile)
    from cmw.nodes.identity import enroll
    config_root = root / ('node-' + profile)
    config = enroll(config_root, {'queues': {'research': str(root / ('queue-' + profile))},
        'storages': {'scratch': str(root / profile)},
        'profiles': {'benign': {'engine': 'Command', 'runner': '/usr/bin/true', 'max_cpus': 2}}})
    store = Store(config['queues']['research']['state'])
    registered = molecular.submit(store, spec)
    assert registered['status'] == 'REGISTERED' and registered['job']['status'] == 'Hold'
    job = registered['job']; execute_registered(store, [job['id']])
    context = resolve_context(store, job['id'])
    observer = ObservationService()
    try:
        snapshot = observer.poll(context)
        assert snapshot.scientific_validation == 'validator-recorded-passed', snapshot.to_dict()
        assert any(event.kind == 'validator' for event in snapshot.events)
        assert [group.label for group in snapshot.card_groups] == ['ANALYSIS', 'STAGE', 'OUTPUT']
    finally:
        observer.close()
    current = next(item for item in store.snapshot()['jobs'] if item['id'] == job['id'])
    observed = molecular.inspect_result(current)
    assert observed['status'] == 'RECORDED', observed
    if profile == 'IFCT':
        report.setdefault('pty_cases', {})['analysis'] = {'state': str(store.root), 'job_id': job['id'],
            'attempt_id': job['attempt_id'], 'profile': profile, 'source_case': str(root / profile),
            **connection_metadata(config, config_root, job),
            'literal_text': ['Two-fragment IFCT', 'singlet', 'State 1', '2 threads', '2 frags',
                'IFCT matrix marker observed', 'Output validation returned successfully',
                'session_log', 'Validator PASSED (recorded)']}
    report['multiwfn'].append({'profile': profile, 'attempt_id': job['attempt_id'],
        'receipt': str(store.root / 'attempts' / job['attempt_id'] / 'payload-exit.json'),
        'snapshot': snapshot.to_dict(), 'result': observed})


def molecular_workflows(root, report):
    root.mkdir(mode=0o700)
    run_component('orca', lambda: orca_workflow(root / 'orca', report), report)
    report['multiwfn'] = []
    for profile in ('FMO', 'ESP', 'IGMH', 'IFCT'):
        run_component('multiwfn-' + profile, lambda: multiwfn_workflow(root, profile, report), report)


FAULT_TESTS = [
    'tests.jobs.test_study.StudyServiceTests.test_partial_registration_and_committed_response_loss_resume_without_duplicates',
    'tests.jobs.test_study.StudyServiceTests.test_failed_publication_receipt_never_promotes_matching_input_bytes',
    'tests.jobs.test_study.StudyServiceTests.test_changed_baseline_refuses_plan_bound_materialization',
    'tests.jobs.test_study.StudyServiceTests.test_incomplete_actual_publication_blocks_registration_and_admission',
    'tests.jobs.test_study_plan.StudyPlanTests.test_partial_publication_retains_completed_case_and_failed_split_record',
    'tests.jobs.test_study_plan.StudyPlanTests.test_changed_source_or_manifest_before_materialize_refuses_all_writes',
    'tests.jobs.test_study_comparison.StudyComparisonArithmeticTests',
    'tests.nodes.test_molecular.MolecularNodeTests.test_target_artifact_is_same_node_attempt_and_declared_location_authority',
    'tests.nodes.test_molecular.MolecularNodeTests.test_source_result_cannot_be_a_transferred_manifest',
    'tests.nodes.test_molecular.MolecularNodeTests.test_all_multiwfn_profiles_register_from_authoritative_same_node_donors',
    'tests.nodes.test_molecular.MolecularNodeTests.test_existing_conversion_requires_original_result_and_pinned_plan',
]


def validate_child_reports(root, allowed_pythons=None):
    allowed_pythons = set(allowed_pythons or (sys.executable,))
    children = []
    paths = sorted(Path(root).rglob('*.ownership.json'))
    all_reports = [json.loads(path.read_text()) for path in [Path(root) / 'ownership.json', *paths]]
    direct = [row for report in all_reports for row in report['records'] if row['capture_origin'] == 'direct-launch']
    for path in paths:
        owned = json.loads(path.read_text())
        origin_path = path.with_suffix('.origin.json')
        origin = json.loads(origin_path.read_text())
        assert origin['support'] == str(SUPPORT) and origin['version']
        assert origin['python'] in allowed_pythons, origin
        package = Path(origin['package'])
        development = os.environ.get('CMW_CLOSURE_DEVELOPMENT_SOURCE_ROOT')
        if origin['installed_package']:
            assert package.is_relative_to(Path(origin['prefix']).resolve()), origin
        else:
            assert development and package == Path(development).resolve() / 'src/cmw/__init__.py'
        frontend, = [row for row in owned['records'] if row['capture_origin'] == 'explicit-root']
        matching = [row for row in direct if all(row[key] == frontend[key] for key in ('host', 'boot', 'pid', 'birth'))]
        assert origin['python'] and any(row.get('exit', {}).get('returncode') == 0
            and row['exit']['exit_evidence'] == 'original Popen wait/reap' for row in matching), str(path)
        summary = {'path': str(path), 'origin': origin, 'coverage_passed': owned['coverage_passed'],
            'live_coverage': owned['live_coverage'], 'remaining_records': len(owned['remaining']),
            'resource_limits_passed': owned['resource_limits_passed']}
        children.append(summary)
        summary['outcomes'] = owned['outcomes']
        assert owned['outcomes']['execution_passed'], summary
    return children


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('workflows', 'molecular', 'faults'))
    parser.add_argument('output', type=Path)
    parser.add_argument('--development-source', type=Path, help='Explicit development only; forbidden for final installed qualification')
    args = parser.parse_args()
    if args.development_source:
        os.environ['CMW_CLOSURE_DEVELOPMENT_SOURCE_ROOT'] = str(args.development_source.resolve())
    else:
        assert not os.environ.get('CMW_CLOSURE_DEVELOPMENT_SOURCE_ROOT'), 'Development override requires an explicit flag'
    output = args.output.resolve(); assert not output.exists()
    root = output.with_suffix(''); root.mkdir(mode=0o700)
    report = {'mode': args.mode, 'origin': installed_origin(), 'status': 'RUNNING',
              'qualification': 'development only' if args.development_source else 'installed artifact', 'real_engines': False, 'production_touched': False}
    configure_children()
    deadline = {'workflows': 180, 'molecular': 90, 'faults': 120}[args.mode]
    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('Declared gate deadline exceeded')))
    started = time.monotonic(); signal.alarm(deadline)
    try:
        with monitored('closure-' + args.mode, root / 'ownership.json') as ledger:
            if args.mode == 'workflows':
                ledger.phase = 'study-review-successor'
                run_component('study-review-successor', lambda: study_workflow(root, report), report)
            if args.mode in {'workflows', 'molecular'}:
                ledger.phase = 'molecular'
                molecular_workflows(root / 'molecular', report)
            else:
                ledger.phase = 'installed-contract-faults'
                report['scope'] = 'Installed API fault injection and handwritten arithmetic fixtures; no fabricated execution receipts'
                suite = unittest.defaultTestLoader.loadTestsFromNames(FAULT_TESTS)
                result = unittest.TextTestRunner(verbosity=2, failfast=True).run(suite)
                report['tests'] = {'names': FAULT_TESTS, 'count': result.testsRun,
                    'failures': [(str(test), detail) for test, detail in result.failures],
                    'errors': [(str(test), detail) for test, detail in result.errors],
                    'skips': [(str(test), reason) for test, reason in result.skipped]}
                assert result.wasSuccessful() and not result.skipped
        children = validate_child_reports(root)
        report['child_ownership'] = children
        if args.mode in {'workflows', 'molecular'}:
            assert children, 'Benign workers must supply their own launch ownership evidence'
        failed = [name for name, component in report.get('components', {}).items() if component['status'] != 'PASS']
        assert not failed, {'failed_components': failed}
        report['status'] = 'PASS'
    except BaseException as exc:
        report.update(status='FAIL', error={'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, previous)
        report.update(seconds=time.monotonic() - started, budget_seconds=deadline)
        report.update(observation_rollup(root, report['status'] == 'PASS'))
        save(output, report)
    raise SystemExit(report['status'] != 'PASS')


if __name__ == '__main__':
    main()
