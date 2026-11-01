"""One installed-wheel, held-only endpoint probe with deterministic native faults.

The supplied workspace and installation must be disposable. No worker is started.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def failure_record(exc):
    from cmw.nodes.runtime_observation import RuntimeRefusal
    import traceback
    result = {'type': type(exc).__name__, 'message': str(exc),
              'traceback': traceback.format_exc()}
    if isinstance(exc, RuntimeRefusal):
        result['evidence'] = exc.evidence
    return result


def verify_lost_reply(error, endpoint_report, recovered, *, request_id, digest):
    """Require the deliberate committed-response loss and its exact receipt."""
    assert error['code'] == 'OUTCOME_UNKNOWN', 'Expected the injected endpoint response loss'
    assert endpoint_report.get('mode') == 'lost-submit-reply'
    committed = endpoint_report.get('committed_registration_before_lost_reply')
    assert isinstance(committed, dict), 'Endpoint did not record the lost-reply injection'
    assert committed.get('status') == 'REGISTERED' and committed.get('replayed') is False
    assert committed.get('request_id') == request_id
    assert isinstance(recovered, dict) and recovered.get('request_id') == request_id
    assert recovered.get('operation') == 'submit' and recovered.get('digest') == digest
    job, original = committed['job'], recovered['result']
    assert job['id'] == original['id'] and job['attempt_id'] == original['attempt_id']
    assert job['node_submission'] == original['node_submission']
    assert job['node_submission']['request_id'] == request_id
    assert job['node_submission']['digest'] == digest
    return job['attempt_id']


def endpoint(config, mode, report_path):
    import cmw
    from cmw.nodes import identity, native_security
    from cmw.nodes.backend import LocalBackend
    from cmw.nodes.endpoint import serve
    member = Path(cmw.__file__).resolve()
    report = {'mode': mode, 'module': identity.__file__, 'python': sys.executable,
              'isolated': bool(sys.flags.isolated), 'events': []}
    original_acl = native_security.acl_evidence
    original_observe = identity.runtime_observed
    armed, count, injected = False, 0, False
    def observe(**options):
        rows = []
        try:
            return original_observe(diagnostics=rows)
        finally:
            for row in rows:
                report['events'].append({key: row[key] for key in ('attempts', 'first_refusal', 'outcome')})
    def acl(fd, **options):
        nonlocal count, injected
        result = original_acl(fd, **options)
        info = os.fstat(fd)
        if armed and (info.st_dev, info.st_ino) == (member.stat().st_dev, member.stat().st_ino):
            count += 1
            if count == 2 and not injected:
                assert info.st_nlink == 1, 'Only a non-hardlinked disposable installation may be changed'
                injected = True
                before = native_security.object_epoch(info)
                digest = hashlib.sha256(member.read_bytes()).hexdigest()
                os.chflags(member, info.st_flags | 0x40)
                after = native_security.object_epoch(member.stat())
                assert after != before and before[:8] == after[:8]
                report['mutation'] = {'member': str(member), 'before': before, 'after': after,
                    'acl': result, 'sha256_before': digest,
                    'sha256_after': hashlib.sha256(member.read_bytes()).hexdigest()}
        return result
    def factory(root, queue):
        backend = LocalBackend(root, queue, defer_check=True)
        original_check, original_call = backend._check, backend.call
        def check(operation=None, *, phase='initial', request_id=None):
            nonlocal armed, count
            armed = operation == 'transfer.publish' and phase == mode and not injected
            if armed:
                count = 0
            try:
                return original_check(operation, phase=phase, request_id=request_id)
            except Exception as exc:
                diagnostic = getattr(backend, 'runtime_diagnostic', None) or {}
                report['events'].append({'operation': operation, 'operation_phase': phase,
                    'error': str(exc), 'code': getattr(exc, 'code', None),
                    'verifier': {key: diagnostic.get(key) for key in
                                 ('category', 'phase', 'path', 'api', 'security_state')},
                    'observer_started_ns': backend._runtime_verifier.process_started_ns})
                raise
            finally:
                armed = False
                save(report_path, report)
        def call(operation, *args, **kwargs):
            result = original_call(operation, *args, **kwargs)
            if mode == 'lost-submit-reply' and operation == 'submit':
                report['committed_registration_before_lost_reply'] = result['result']
                save(report_path, report)
                os._exit(0)  # Owned test endpoint: commit retained, no reply sent.
            return result
        backend._check, backend.call = check, call
        return backend
    with patch.object(native_security, 'acl_evidence', side_effect=acl), \
            patch.object(identity, 'runtime_observed', side_effect=observe):
        return serve(config, 'research', backend_factory=factory)


def exercise(output):
    import cmw
    from cmw.jobs.store import Store
    from cmw.nodes.connections import ConnectedBackend
    from cmw.nodes.identity import enroll
    from cmw.nodes.protocol import NodeError
    from cmw.nodes.submission import submission_digest
    from cmw.nodes.transfer import prepare_local_bundle, upload_local_bundle
    from cmw.nodes.transport import ProcessTransport
    output.mkdir(mode=0o700)
    report = {'python': sys.executable, 'package': cmw.__file__, 'isolated': bool(sys.flags.isolated),
              'cases': [], 'owned_endpoints': [], 'payloads_started': 0}
    assert sys.flags.isolated and sys.dont_write_bytecode
    assert 'site-packages' in Path(cmw.__file__).parts
    assert Path(cmw.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    backends = []
    def close(backend):
        backend.close()
        if backend in backends:
            backends.remove(backend)
    try:
        for boundary in ('before', 'after'):
            root = output / boundary; root.mkdir()
            storage = root / 'storage'; storage.mkdir()
            source = root / 'fixture.sh'; source.write_text('#!/bin/sh\nexit 0\n')
            config_path = root / 'node'
            config = enroll(config_path, {'queues': {'research': str(root / 'queue')},
                'storages': {'scratch': str(storage)},
                'profiles': {'benign': {'engine': 'Command', 'runner': '/bin/sh', 'max_cpus': 1}}})
            original_config = (config_path / 'node.json').read_bytes()
            save(root / 'original-enrollment.json', config)
            entry = {'transport': 'local', 'config_root': str(config_path), 'node_id': config['node_id'],
                'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
                'queues': {'research': config['queues']['research']['queue_id']},
                'default_queue': 'research', 'read_only': False}
            serial = 0
            def connect(mode):
                nonlocal serial
                serial += 1
                argv = [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                        '--endpoint', str(config_path), '--mode', mode,
                        '--output', str(root / f'endpoint-{serial}-{mode}.json')]
                transport = ProcessTransport(argv, timeout=30)
                original_close = transport.close
                def tracked_close():
                    process = transport.process
                    original_close()
                    if process is not None:
                        report['owned_endpoints'].append({'pid': process.pid,
                            'argv': argv, 'returncode': process.returncode,
                            'reaped': process.poll() is not None})
                        assert process.poll() is not None
                transport.close = tracked_close
                backend = ConnectedBackend('disposable', entry, transport=transport)
                backends.append(backend)
                return backend
            request_id = uuid4().hex
            bundle = prepare_local_bundle(root / 'spool',
                [{'source': str(source), 'path': 'support/fixture.sh', 'role': 'support'}],
                node_id=config['node_id'], queue_id=entry['queues']['research'])
            spec = {'schema': 1, 'request_id': request_id, 'node_id': config['node_id'],
                'queue_id': entry['queues']['research'], 'storage': 'scratch', 'profile': 'benign',
                'engine': 'Command', 'name': 'Held runtime boundary fixture',
                'resources': {'cpus': 1, 'memory_gib': .1}, 'intent': 'held',
                'dependencies_complete': True, 'manifest': bundle['manifest'],
                'arguments': [{'bundle': 'support/fixture.sh'}]}
            digest = submission_digest(spec)
            save(root / 'original-request.json', {'spec': spec, 'digest': digest,
                                                 'manifest_digest': bundle['manifest_digest']})
            backend = connect(boundary)
            receipt = backend.call('transfer.begin', {'storage': 'scratch', 'request_id': request_id,
                                                      'manifest': bundle['manifest']})['result']
            upload_local_bundle(bundle['spool'], receipt)
            publish = {'storage': 'scratch', 'request_id': request_id,
                       'manifest_digest': bundle['manifest_digest']}
            case = {'boundary': boundary, 'request_id': request_id, 'digest': digest}
            report['cases'].append(case)
            try:
                backend.call('transfer.publish', publish)
            except NodeError as exc:
                case['injected_refusal'] = {'code': exc.code, 'message': str(exc)}
                assert exc.code == 'RUNTIME_MISMATCH'
                assert 'fresh_isolated_observer_required' in str(exc), str(exc)
                assert f'"operation_phase": "{boundary}"' in str(exc)
            else:
                raise AssertionError('Old observer accepted the injected metadata transition')
            close(backend)
            backend = connect('none')
            actual = backend.call('transfer.lookup', {'storage': 'scratch', 'request_id': request_id})['result']['transfer']
            case['publication_after_refusal'] = actual['status']
            assert actual['status'] == ('INCOMING' if boundary == 'before' else 'PUBLISHED')
            assert Store(root / 'queue').snapshot()['jobs'] == []
            published = backend.call('transfer.publish', publish)['result']
            replayed = backend.call('transfer.publish', publish)['result']
            assert published == replayed and published['status'] == 'PUBLISHED'
            assert (Path(published['published_root']) / 'support/fixture.sh').read_bytes() == source.read_bytes()
            case['publication_replay_equal'] = True
            case['published_root'] = published['published_root']
            close(backend)
            if boundary == 'after':
                backend = connect('lost-submit-reply')
                lost_report_path = root / f'endpoint-{serial}-lost-submit-reply.json'
                try:
                    backend.call('submit', {'spec': spec}, request_id=request_id)
                except NodeError as exc:
                    case['lost_reply'] = {'code': exc.code, 'message': str(exc)}
                else:
                    raise AssertionError('Injected endpoint exit unexpectedly returned a reply')
                close(backend)
                lost_report = json.loads(lost_report_path.read_text())
            backend = connect('none')
            if boundary == 'after':
                recovered = backend.call('request', {'request_id': request_id})['result']['request']
                case['lost_reply_committed_attempt'] = verify_lost_reply(
                    case['lost_reply'], lost_report, recovered, request_id=request_id, digest=digest)
                case['lost_reply_original_request_recovered'] = True
            registered = backend.call('submit', {'spec': spec}, request_id=request_id)['result']
            repeated = backend.call('submit', {'spec': spec}, request_id=request_id)['result']
            store = Store(root / 'queue'); state = store.snapshot()
            durable = store.request_lookup(request_id)
            assert len(state['jobs']) == 1 and state['jobs'][0]['status'] == 'Hold'
            assert not state['controller']['dispatch'] and not state['controller']['online']
            assert repeated['replayed'] and repeated['job']['attempt_id'] == registered['job']['attempt_id']
            assert durable['digest'] == digest and durable['result']['attempt_id'] == registered['job']['attempt_id']
            if boundary == 'after':
                assert registered['replayed'], 'Fresh client must recover the committed lost reply'
            assert (config_path / 'node.json').read_bytes() == original_config
            assert submission_digest(spec) == digest
            case.update(status='PASS', attempt_id=registered['job']['attempt_id'], jobs=1,
                        original_enrollment_unchanged=True, original_digest_unchanged=True,
                        durable_receipt_matches=True, no_dispatch=True)
            close(backend)
        report['status'] = 'PASS'
    except BaseException as exc:
        report.update(status='FAIL', error=failure_record(exc))
        raise
    finally:
        for backend in list(backends):
            close(backend)
        report['cleanup_complete'] = all(row['reaped'] for row in report['owned_endpoints'])
        save(output / 'result.json', report)
    print(json.dumps({'status': report['status'], 'cases': len(report['cases']),
                      'report': str(output / 'result.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', type=Path)
    parser.add_argument('--mode', default='none')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.endpoint:
        raise SystemExit(endpoint(args.endpoint, args.mode, args.output))
    import signal
    def deadline(*_):
        raise TimeoutError('180-second installed boundary probe deadline exceeded')
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(180)
    try:
        exercise(args.output)
    finally:
        signal.alarm(0)
