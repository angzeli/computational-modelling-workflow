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


def main(output):
    import cmw
    from cmw.jobs.store import Store
    from cmw.nodes.connections import ConnectedBackend
    from cmw.nodes.identity import enroll
    from cmw.nodes.submission import submission_digest
    from cmw.nodes.transfer import prepare_local_bundle, upload_local_bundle
    from cmw.nodes.transport import ProcessTransport
    output.mkdir(mode=0o700)
    package = Path(cmw.__file__).resolve()
    assert sys.flags.isolated and sys.dont_write_bytecode
    assert package.is_relative_to(Path(sys.prefix).resolve()) and 'site-packages' in package.parts
    report = {'python': sys.executable, 'package': str(package), 'operation': 'setup',
              'owned_helpers': [], 'payloads_started': 0,
              'interpreter_before_probe': path_snapshot(Path(sys.executable))}
    transport = None
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
        argv = [sys.executable, '-I', '-B', '-m', 'cmw.cli', 'nodes', 'endpoint',
                '--config', str(config_path), '--queue', 'research']
        transport = ProcessTransport(argv, timeout=30)
        original_close = transport.close
        def close():
            process = transport.process
            original_close()
            if process is not None:
                report['owned_helpers'].append({'role': 'native node endpoint', 'argv': argv,
                    'pid': process.pid, 'returncode': process.returncode, 'reaped': process.poll() is not None})
                assert process.returncode is not None
        transport.close = close
        report['operation'] = 'endpoint startup'
        backend = ConnectedBackend('disposable', entry, 'research', transport=transport)
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
        registered = backend.call('submit', {'spec': spec}, request_id=request_id)['result']
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
        if transport is not None:
            transport.close()
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
        main(Path(sys.argv[1]).resolve())
    finally:
        signal.alarm(0)
