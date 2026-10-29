"""Actual installed old/new stdio peers carrying native scientific evidence.

Disposable held VASP jobs use public native-shaped text. Multiwfn reuses an
original pre-enrolled benign workflow receipt and its actual observed records.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
from uuid import uuid4

from tests.campaign.closure_workflows import SUPPORT, installed_origin, monitored, save, validate_child_reports


def helper_command(python, action, *arguments):
    return [str(python), '-I', '-B', '-c',
        'import sys;sys.path.insert(0,sys.argv.pop(1));'
        'from tests.campaign.closure_peer import helper_main;helper_main()',
        str(SUPPORT), action, *map(str, arguments)]


def setup_fixture(root, analysis_case=None):
    from cmw.jobs.store import Store, JobsError
    from cmw.nodes.identity import enroll
    from cmw.progress.models import SourceDescriptor
    from tests.periodic.vasp.result_fixtures import native_texts, structure_text
    root.mkdir(mode=0o700)
    science = root / 'native'; science.mkdir(mode=0o700)
    texts = native_texts(settings={'NCORE': 1, 'KPAR': 1})
    endpoint = texts.pop('endpoint')
    texts.update(POSCAR=structure_text(), CONTCAR=structure_text(endpoint))
    for name, text in texts.items():
        (science / name).write_text(text)
    store = Store(root / 'state')
    vasp = store.add(argv=['/usr/bin/true'], cwd=science, name='Native VASP peer fixture', engine='VASP', hold=True,
        progress_sources=tuple(SourceDescriptor(role.lower(), str(science / role))
                               for role in ('OUTCAR', 'OSZICAR', 'POSCAR')))
    folder = root / 'multiwfn'; folder.mkdir(mode=0o700)
    events = folder / 'events.jsonl'; events.touch()
    native = folder / 'native.log'
    native.write_text('Version 2026.7.15\n( Number of parallel threads: 2 Current date: synthetic )\n'
                      'Loaded /synthetic/source successfully!\n'
                      'Loading configuration coefficients of excited state 1...\n')
    info = events.stat()
    association = {'schema': 2, 'stream_id': 'closure-stream', 'request_id': 'closure-request',
        'stream_generation': 1, 'events_identity': [info.st_dev, info.st_ino],
        'native_path': str(native), 'profile': 'IFCT', 'attempt_directory': str(folder),
        'scientific_attempt_id': 'attempt_001', 'source_target_id': 'source-target',
        'source_attempt_id': 'donor-001', 'source_result': str(folder / 'source.json'),
        'version': '2026.7.15', 'threads': 2, 'state_index': 1, 'spin_manifold': 'singlet',
        'fragment_labels': ['D', 'A'], 'fragment_counts': [2, 3], 'expected_roles': 'session_log'}
    metadata = folder / 'association.json'; save(metadata, association)
    sources = [SourceDescriptor('multiwfn_session', str(native), stage='IFCT'),
        SourceDescriptor('multiwfn_events', str(events), optional=True, stage='IFCT'),
        SourceDescriptor('multiwfn_validation', str(folder / 'validation.jsonl'), optional=True, stage='IFCT'),
        SourceDescriptor('multiwfn_metadata', str(metadata), optional=True, stage='IFCT',
            expected_sha256=hashlib.sha256(metadata.read_bytes()).hexdigest(), expected_size=metadata.stat().st_size)]
    for sequence, kind in enumerate(('prepared', 'launch_requested', 'child_launched', 'process_exited'), 1):
        event = {**association, 'kind': kind, 'sequence': sequence,
                 'event_source_identity': association['events_identity'],
                 'value': '42' if kind == 'child_launched' else '0' if kind == 'process_exited' else None}
        with events.open('a') as stream:
            stream.write(json.dumps(event) + '\n')
    unsupported, multiwfn = None, None
    if analysis_case is None:
        try:
            store.add(argv=['/usr/bin/true'], cwd=folder, name='Unsupported Multiwfn source probe',
                      engine='Multiwfn', hold=True, progress_sources=tuple(sources))
        except JobsError as exc:
            assert str(exc) == 'No progress adapter is registered for engine Multiwfn'
            unsupported = {'type': type(exc).__name__, 'message': str(exc)}
        else:
            raise AssertionError('Supported new peer requires an original workflow analysis case')
    config = enroll(root / 'node', {'queues': {'research': str(store.root)},
        'storages': {'scratch': str(science)},
        'profiles': {'benign': {'engine': 'Command', 'runner': '/usr/bin/true', 'max_cpus': 1}}})
    entry = {'transport': 'local', 'config_root': str(root / 'node'), 'node_id': config['node_id'],
        'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
        'queues': {'research': config['queues']['research']['queue_id']}, 'default_queue': 'research', 'read_only': True}
    def target(job):
        return {'node_id': entry['node_id'], 'queue_id': entry['queues']['research'],
                'job_id': job['id'], 'attempt_id': job['attempt_id']}
    return {'root': str(root), 'entry': entry, 'vasp': target(vasp), 'multiwfn': analysis_case['target'] if analysis_case else None, 'multiwfn_refusal': unsupported,
            'analysis_case': analysis_case,
            'association': association, 'origin': installed_origin()}


def helper_main():
    origin = installed_origin()
    action, supplied = sys.argv[1], Path(sys.argv[2]).resolve()
    evidence = supplied.parent / ('old-' + action + '-helper.ownership.json')
    if action == 'setup':
        evidence = supplied.parent / (supplied.name + '-setup-helper.ownership.json')
    save(evidence.with_suffix('.origin.json'), origin)
    with monitored('installed-peer-helper:' + action, evidence):
        if action == 'setup':
            analysis_case = None
            if len(sys.argv) > 3:
                workflow_path = Path(sys.argv[3]).resolve()
                workflow = json.loads(workflow_path.read_text())
                assert workflow['components']['multiwfn-IFCT']['status'] == 'PASS'
                analysis_case = workflow['pty_cases']['analysis']
                original, = [item for item in workflow['multiwfn'] if item['profile'] == 'IFCT']
                receipt = Path(original['receipt'])
                assert receipt.parent.name == analysis_case['attempt_id']
                assert json.loads(receipt.read_text())['exit_code'] == 0
                analysis_case = {**analysis_case, 'workflow_report': str(workflow_path), 'receipt': str(receipt)}
            result = setup_fixture(supplied, analysis_case)
        elif action == 'decode':
            from cmw.nodes.codec import decode_typed
            decoded = decode_typed(json.loads(supplied.read_text()))
            result = {'status': 'DECODED', 'type': type(decoded).__name__, 'oracle': assert_vasp(decoded)}
        elif action == 'observe':
            record = json.loads(supplied.read_text())
            client = connect(record, sys.argv[3])
            try:
                envelope, snapshot = progress(client, record['vasp'])
                result = {'status': 'LIVE_ROUNDTRIP', 'origin': origin,
                          'oracle': assert_vasp(snapshot), 'envelope': envelope}
            finally:
                client.close()
        else:
            raise ValueError('Unsupported helper action')
    print(json.dumps(result))


def connect(record, python):
    from cmw.nodes.connections import ConnectedBackend
    from cmw.nodes.transport import ProcessTransport
    command = [str(python), '-I', '-B', '-m', 'cmw.cli', 'nodes', 'endpoint',
               '--config', record['entry']['config_root'], '--queue', 'research']
    # No status call is made: native progress/capabilities do not enumerate host
    # activity. Each endpoint is pinned to its own disposable enrolled runtime.
    return ConnectedBackend('native-peer', record['entry'], transport=ProcessTransport(command, timeout=20))


def progress(client, target):
    from cmw.nodes.codec import decode_typed
    from cmw.progress.models import ProgressSnapshot
    value = client.call('progress', target=target)
    return value, decode_typed(value['result']['snapshot'], ProgressSnapshot)


def assert_vasp(snapshot):
    metrics = {series.latest.semantic_id: series.latest for series in snapshot.metrics}
    oracle = {'vasp.electronic.free_energy': -2.10000005, 'vasp.electronic.dE': 5e-8,
              'vasp.electronic.d_eps': 4e-8, 'vasp.electronic.rms': 1e-4,
              'vasp.electronic.rms_c': None, 'vasp.force.maximum_atomic_norm.all': .5}
    for name, expected in oracle.items():
        assert name in metrics and metrics[name].value == expected, (name, snapshot.to_dict())
    assert metrics['vasp.electronic.dE'].signed_value == -5e-8
    assert metrics['vasp.electronic.d_eps'].signed_value == -4e-8
    settings = {setting.name: setting for setting in snapshot.settings}
    assert settings['EDIFF'].value == 1e-6 and settings['NCORE'].value == 1
    assert settings['NCORE'].qualification == 'effective'
    assert any(item.observed_frontier == item.native_frontier == item.checked_frontier == 3
               for item in snapshot.verification), snapshot.verification
    assert snapshot.iteration[-1].index == 3 and snapshot.scientific_validation == 'not-assessed'
    return {'values': oracle, 'settings': {name: settings[name].value for name in ('EDIFF', 'NCORE')},
            'frontiers': [asdict(item) for item in snapshot.verification]}


def run_peer(root, old_python, workflow_report, report):
    from cmw.nodes.codec import CodecError, decode_typed
    from cmw.nodes.protocol import NodeError
    clients, records = {}, {}
    try:
        for label, python in (('old', old_python), ('new', sys.executable)):
            arguments = (workflow_report,) if label == 'new' else ()
            setup = subprocess.run(helper_command(python, 'setup', root / label, *arguments),
                capture_output=True, text=True, timeout=30,
                env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
            assert setup.returncode == 0, (label, setup.stdout, setup.stderr)
            record = json.loads(setup.stdout)
            records[label] = record; clients[label] = connect(record, python)
        clients['analysis'] = connect(records['new']['analysis_case'], sys.executable)
        report['runtimes'] = {label: record['origin'] for label, record in records.items()}
        report['vasp'], report['multiwfn'] = {}, {}
        encoded_new = None
        for label in ('old', 'new'):
            client = clients[label]
            envelope, snapshot = progress(client, records[label]['vasp'])
            report['vasp'][label] = {'oracle': assert_vasp(snapshot), 'envelope': envelope}
            if label == 'new':
                encoded_new = envelope['result']['snapshot']
            if records[label]['multiwfn'] is None:
                assert label == 'old' and records[label]['multiwfn_refusal']
                report['multiwfn'][label] = {'status': 'EXPLICIT_UNSUPPORTED_REGISTRATION',
                    'refusal': records[label]['multiwfn_refusal']}
                continue
            envelope, native = progress(clients['analysis'], records[label]['multiwfn'])
            if native.status in {'ADAPTER_UNAVAILABLE', 'UNSUPPORTED'}:
                assert label == 'old', native.to_dict()
                report['multiwfn'][label] = {'status': 'EXPLICIT_UNSUPPORTED', 'native': envelope}
                continue
            assert native.scientific_validation == 'validator-recorded-passed', native.to_dict()
            assert all(any(event.kind == kind for event in native.events) for kind in ('native', 'runner', 'validator'))
            stages = {setting.name: setting.value for setting in native.settings}
            assert stages['native_stage'] == 'Native IFCT matrix marker observed', stages
            assert stages['runner_stage'] == 'Output validation returned successfully', stages
            assert stages['threads'] == 2 and stages['state_index'] == 1
            report['multiwfn'][label] = {'status': 'OBSERVED_DISTINCT_STAGES', 'evidence': envelope,
                'original_case': records['new']['analysis_case'],
                'qualification': 'public synthetic native output; original benign Jobs execution receipt retained'}
        # Old endpoint support is established from the actual negotiated peer.
        capability = clients['old'].call('capabilities')['result'].get('convergence_study')
        if capability is None:
            try:
                clients['old'].call('study.plan', {'manifest_path': '/not-read/unsupported.json'})
            except NodeError as exc:
                assert exc.code == 'UNSUPPORTED_STUDY', exc
                report['unsupported_study'] = exc.code
            else:
                raise AssertionError('Unsupported old peer accepted study request')
        else:
            report['unsupported_study'] = 'NOT_APPLICABLE: supplied old peer advertises studies'
        assert encoded_new is not None
        for label, mutate in (('extra', lambda value: value['value'].update(unexpected_native_field=1)),
                              ('missing', lambda value: value['value'].pop('settings'))):
            invalid = deepcopy(encoded_new); mutate(invalid)
            try:
                decode_typed(invalid)
            except CodecError:
                pass
            else:
                raise AssertionError('Current strict codec accepted ' + label + ' fields')
        wire = root / 'new-native-wire.json'; save(wire, encoded_new)
        old_decode = subprocess.run(helper_command(old_python, 'decode', wire),
            capture_output=True, text=True, timeout=20)
        assert old_decode.returncode == 0, old_decode.stderr
        report['old_decoder_new_native'] = json.loads(old_decode.stdout)
        # The selected old runtime has the same native progress model schema.
        # Arbitrary decoder errors must not count as compatible behavior.
        assert report['old_decoder_new_native']['status'] == 'DECODED'
        new_record = root / 'new-peer.json'; save(new_record, records['new'])
        reverse = subprocess.run(helper_command(old_python, 'observe', new_record, sys.executable),
            capture_output=True, text=True, timeout=25)
        assert reverse.returncode == 0, reverse.stderr
        report['old_client_new_endpoint'] = json.loads(reverse.stdout)
        assert report['old_client_new_endpoint']['status'] == 'LIVE_ROUNDTRIP'
        before, _ = progress(clients['new'], records['new']['vasp'])
        clients['old'].close()
        after, snapshot = progress(clients['new'], records['new']['vasp'])
        assert after['sequence'] > before['sequence'], (before, after)
        assert_vasp(snapshot)
        report['observer_isolation'] = {'before_sequence': before['sequence'], 'after_sequence': after['sequence'],
            'fresh_native_evidence': after}
    finally:
        for client in clients.values():
            client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('old_python', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--workflow-report', type=Path, required=True,
        help='Original workflow or molecular report with a completed pre-enrolled IFCT case')
    parser.add_argument('--development-source', type=Path, help='Explicit development only; forbidden for final installed qualification')
    args = parser.parse_args()
    if args.development_source:
        os.environ['CMW_CLOSURE_DEVELOPMENT_SOURCE_ROOT'] = str(args.development_source.resolve())
    else:
        assert not os.environ.get('CMW_CLOSURE_DEVELOPMENT_SOURCE_ROOT'), 'Development override requires an explicit flag'
    output = args.output.resolve(); assert not output.exists()
    root = output.with_suffix(''); root.mkdir(mode=0o700)
    report = {'origin': installed_origin(), 'status': 'RUNNING', 'qualification': 'development only' if args.development_source else 'installed native stdio peers',
              'real_engines': False, 'production_touched': False}
    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('Declared 120-second peer deadline exceeded')))
    started = time.monotonic(); signal.alarm(120)
    try:
        with monitored('closure-native-peer', root / 'ownership.json') as ledger:
            ledger.phase = 'native-peer-roundtrips'
            run_peer(root, args.old_python.absolute(), args.workflow_report.resolve(), report)
        report['child_ownership'] = validate_child_reports(root, (sys.executable, str(args.old_python.absolute())))
        report['status'] = 'PASS'
    except BaseException as exc:
        report.update(status='FAIL', error={'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, previous)
        report.update(seconds=time.monotonic() - started, budget_seconds=120)
        from tests.campaign.closure_workflows import observation_rollup
        report.update(observation_rollup(root, report['status'] == 'PASS'))
        save(output, report)
    raise SystemExit(report['status'] != 'PASS')


if __name__ == '__main__':
    main()
