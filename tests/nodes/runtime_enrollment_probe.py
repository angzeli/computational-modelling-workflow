"""One native installed enrollment-to-held-registration probe; no payload launch."""
from pathlib import Path
import json
import os
import signal
import sys
from uuid import uuid4

# This directory supplies only the already tested recorder, never CMW source.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_publish_probe import failure_record


def path_snapshot(path):
    info = path.lstat()
    fields = ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns', 'mode', 'uid', 'gid', 'nlink', 'flags')
    return {'lexical': str(path), 'resolved': str(path.resolve(strict=True)),
        'link_target': os.readlink(path) if path.is_symlink() else None,
        'lstat': {name: getattr(info, 'st_' + name) for name in fields}}


def endpoint(config, diagnostic_path, mode):
    """Real endpoint/collectors; wrapper only records or applies the named fault."""
    import hashlib
    import cmw
    from cmw.nodes.backend import LocalBackend
    from cmw.nodes.endpoint import serve
    report = {'mode': mode, 'python': sys.executable, 'package': cmw.__file__}
    def factory(root, queue):
        backend = LocalBackend(root, queue, defer_check=True)
        original = backend._check
        injected = False
        def check(operation=None, *, phase='initial', request_id=None):
            nonlocal injected
            if mode == 'post-submit' and operation == 'submit' and phase == 'after' and not injected:
                member = Path(cmw.__file__).resolve()
                assert member.is_relative_to(Path(sys.prefix).resolve()) and member.stat().st_nlink == 1
                before = member.stat()
                digest = hashlib.sha256(member.read_bytes()).hexdigest()
                os.chflags(member, before.st_flags ^ 0x40)
                after = member.stat()
                report['controlled_fault'] = {'member': str(member), 'ctime_before': before.st_ctime_ns,
                    'ctime_after': after.st_ctime_ns, 'flags_before': before.st_flags,
                    'flags_after': after.st_flags, 'sha256_before': digest,
                    'sha256_after': hashlib.sha256(member.read_bytes()).hexdigest()}
                injected = True
            try:
                return original(operation, phase=phase, request_id=request_id)
            except BaseException as exc:
                report.update(operation=operation, operation_phase=phase, request_id=request_id,
                    error_code=getattr(exc, 'code', None), error=str(exc),
                    runtime_diagnostic=getattr(backend, 'runtime_diagnostic', None))
                diagnostic_path.write_text(json.dumps(report, indent=2)+'\n')
                raise
        backend._check = check
        return backend
    return serve(config, 'research', backend_factory=factory)


def main(output, *, controlled=False):
    import cmw
    from cmw.jobs.store import Store
    from cmw.nodes.connections import ConnectedBackend
    from cmw.nodes.identity import enroll
    from cmw.nodes.submission import submission_digest
    from cmw.nodes.transfer import prepare_local_bundle, upload_local_bundle
    from cmw.nodes.transport import ProcessTransport
    from cmw.nodes.protocol import NodeError
    output.mkdir(mode=0o700)
    package = Path(cmw.__file__).resolve()
    assert sys.flags.isolated and sys.dont_write_bytecode
    assert package.is_relative_to(Path(sys.prefix).resolve()) and 'site-packages' in package.parts
    report = {'python': sys.executable, 'package': str(package), 'operation': 'setup',
              'owned_helpers': [], 'payloads_started': 0,
              'interpreter_before_probe': path_snapshot(Path(sys.executable))}
    transports = []
    serial = 0
    try:
        storage = output / 'storage'; storage.mkdir()
        source = output / 'fixture.sh'; source.write_text('#!/bin/sh\nexit 0\n')
        config_path = output / 'node'
        report['operation'] = 'enroll'
        config = enroll(config_path, {'queues': {'research': str(output / 'queue')},
            'storages': {'scratch': str(storage)},
            'profiles': {'benign': {'engine': 'Command', 'runner': '/bin/sh', 'max_cpus': 1}}})
        report['enrollment'] = {'status': 'PASS', 'node_id': config['node_id'],
                                'runtime': config['runtime']}
        original = (config_path / 'node.json').read_bytes()
        entry = {'transport': 'local', 'config_root': str(config_path),
            'node_id': config['node_id'], 'host_id': config['host_id'],
            'runtime_id': config['runtime']['fingerprint'],
            'queues': {'research': config['queues']['research']['queue_id']},
            'default_queue': 'research', 'read_only': False}
        def connect(mode):
            nonlocal serial
            serial += 1
            diagnostic = output / f'endpoint-{serial}.json'
            argv = [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--endpoint',
                    str(config_path), str(diagnostic), mode]
            transport = ProcessTransport(argv, timeout=30)
            transports.append(transport)
            original_close = transport.close
            def close():
                process = transport.process
                original_close()
                if process is not None:
                    report['owned_helpers'].append({'role': 'instrumented native endpoint', 'argv': argv,
                        'pid': process.pid, 'returncode': process.returncode, 'reaped': process.poll() is not None,
                        'diagnostic': str(diagnostic)})
                    assert process.returncode is not None
            transport.close = close
            return ConnectedBackend('disposable', entry, 'research', transport=transport)
        report['operation'] = 'endpoint startup'
        backend = connect('post-submit' if controlled else 'normal')
        request_id = uuid4().hex
        bundle = prepare_local_bundle(output / 'spool',
            [{'source': str(source), 'path': 'support/fixture.sh', 'role': 'support'}],
            node_id=config['node_id'], queue_id=entry['queues']['research'])
        spec = {'schema': 1, 'request_id': request_id, 'node_id': config['node_id'],
            'queue_id': entry['queues']['research'], 'storage': 'scratch', 'profile': 'benign',
            'engine': 'Command', 'name': 'Native held enrollment fixture',
            'resources': {'cpus': 1, 'memory_gib': .1}, 'intent': 'held',
            'dependencies_complete': True, 'manifest': bundle['manifest'],
            'arguments': [{'bundle': 'support/fixture.sh'}]}
        digest = submission_digest(spec)
        (output / 'original-request.json').write_text(json.dumps(spec, indent=2)+'\n')
        report.update(request_id=request_id, submission_digest=digest,
                      manifest_digest=bundle['manifest_digest'])
        report['operation'] = 'transfer.begin'
        receipt = backend.call('transfer.begin', {'storage': 'scratch', 'request_id': request_id,
                                                  'manifest': bundle['manifest']})['result']
        report['operation'] = 'upload'
        upload_local_bundle(bundle['spool'], receipt)
        publish = {'storage': 'scratch', 'request_id': request_id,
                   'manifest_digest': bundle['manifest_digest']}
        report['operation'] = 'transfer.publish'
        published = backend.call('transfer.publish', publish)['result']
        assert published['status'] == 'PUBLISHED' and published['manifest_digest'] == bundle['manifest_digest']
        assert (Path(published['published_root'])/'support/fixture.sh').read_bytes() == source.read_bytes()
        report['operation'] = 'submit held'
        if controlled:
            try:
                backend.call('submit', {'spec': spec}, request_id=request_id)
            except NodeError as exc:
                report['original_refusal'] = {'code': exc.code, 'message': str(exc)}
                assert exc.code == 'RUNTIME_MISMATCH'
                context = json.loads(str(exc).split(' | ', 1)[1])
                assert context['request_id'] == request_id and context['dispatch_entered']
                assert context['outcome'] == 'not_independently_confirmed'
            else:
                raise AssertionError('Controlled post-submit fault returned an accepted response')
            diagnostic = json.loads((output/'endpoint-1.json').read_text())
            assert diagnostic['controlled_fault']['sha256_before'] == diagnostic['controlled_fault']['sha256_after']
            report['durable_before_recovery'] = Store(output/'queue').request_lookup(request_id)
            assert report['durable_before_recovery']['digest'] == digest
            backend.close()
            backend = connect('normal')
            recovered = backend.call('request', {'request_id': request_id})['result']['request']
            assert recovered == report['durable_before_recovery']
            registered = backend.call('submit', {'spec': spec}, request_id=request_id)['result']
            assert registered['replayed'] and registered['job'] == recovered['result']
            report['fresh_verified_recovery'] = True
        else:
            registered = backend.call('submit', {'spec': spec}, request_id=request_id)['result']
            assert registered['status'] == 'REGISTERED' and not registered['replayed']
            report['normal_direct_response'] = True
        report['operation'] = 'original request lookup'
        found = backend.call('request', {'request_id': request_id})['result']['request']
        durable = Store(output/'queue').request_lookup(request_id)
        assert found == durable and found['digest'] == digest
        assert found['result']['attempt_id'] == registered['job']['attempt_id']
        # Durable state is inspected before either mutation is retried.
        transfer = backend.call('transfer.lookup', {'storage': 'scratch', 'request_id': request_id})['result']['transfer']
        assert transfer == published
        report['operation'] = 'same-request replay'
        assert backend.call('transfer.publish', publish)['result'] == published
        repeated = backend.call('submit', {'spec': spec}, request_id=request_id)['result']
        assert repeated['replayed'] and repeated['job'] == registered['job']
        state = Store(output/'queue').snapshot()
        assert len(state['jobs']) == 1 and state['jobs'][0]['status'] == 'Hold'
        assert not state['controller']['dispatch'] and not state['controller']['online']
        assert original == (config_path/'node.json').read_bytes() and submission_digest(spec) == digest
        report.update(status='PASS', operation='complete', publication=published,
            held_attempt=registered['job']['attempt_id'], jobs=1, original_trust_unchanged=True,
            original_request_preserved=True, durable_outcome=durable)
    except BaseException as exc:
        report.update(status='FAIL', error=failure_record(exc))
        raise
    finally:
        for transport in transports:
            transport.close()
        report['scenario'] = 'controlled-post-submit' if controlled else 'normal'
        report['cleanup_complete'] = all(row['reaped'] for row in report['owned_helpers'])
        report['interpreter_after_probe_later_inspection'] = path_snapshot(Path(sys.executable))
        report['module_origins'] = {name: module.__file__ for name, module in sys.modules.items()
            if (name == 'cmw' or name.startswith('cmw.')) and getattr(module, '__file__', None)}
        assert all(Path(path).resolve().is_relative_to(Path(sys.prefix).resolve())
                   for path in report['module_origins'].values())
        (output/'result.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__ == '__main__':
    def deadline(*_):
        raise TimeoutError('90-second native enrollment probe deadline exceeded')
    signal.signal(signal.SIGALRM, deadline); signal.alarm(90)
    try:
        if sys.argv[1] == '--endpoint':
            raise SystemExit(endpoint(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]))
        main(Path(sys.argv[1]).resolve(), controlled='--controlled' in sys.argv)
    finally:
        signal.alarm(0)
