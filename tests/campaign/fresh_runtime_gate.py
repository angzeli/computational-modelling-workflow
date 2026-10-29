"""Actual installed-runtime metadata gate; never use a final or production install.

The install must be a separate wheel environment named fresh-runtime-gate-env-*
under this campaign's artifacts directory. Run its Python with -I -B. There is no
quiet-window helper, fixed settling sleep, metadata retry, or baseline rewrite.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from uuid import uuid4


def write(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + '\n')


def security_complete(observed):
    return (observed['root_epoch'][-1] and observed['executable_epoch'][-1]
            and all(epoch[-1] for epoch in observed['members'].values())
            and all(epoch[-1] for epoch in observed['directories'].values()))


def verify_install(args):
    import cmw
    campaign = args.campaign_root.resolve(strict=True)
    install = args.install_root.resolve(strict=True)
    assert install.is_relative_to(campaign / 'artifacts'), install
    assert install.name.startswith('fresh-runtime-gate-env-'), 'A separately named disposable install is mandatory'
    assert Path(sys.prefix).resolve() == install, (sys.prefix, install)
    package = Path(cmw.__file__).resolve().parent
    assert package.is_relative_to(install) and 'site-packages' in package.parts, package
    assert sys.flags.isolated and sys.dont_write_bytecode, 'Run the disposable interpreter with -I -B'
    assert args.output.resolve().is_relative_to(campaign / 'artifacts'), args.output
    assert not args.output.resolve().is_relative_to(install), 'Evidence must be outside the mutated install'
    return {'install': str(install), 'package_origin': cmw.__file__,
            'python': sys.version, 'executable': sys.executable,
            'isolated': bool(sys.flags.isolated), 'dont_write_bytecode': sys.dont_write_bytecode}


def semantic_digest(record):
    from cmw.nodes.control import request_digest
    target = record['target']
    return request_digest(target['node_id'], target['queue_id'], target, record['control_params'])


def lookup(backend, record):
    try:
        response = backend.call('request', {'request_id': record['request_id']})['result']['request']
        assert response == record['receipt_record'], response
        verifier = backend._runtime_verifier
        return {'outcome': 'ACCEPTED_ORIGINAL_REQUEST', 'receipt': response,
                'recovered': verifier.recovered, 'verification': verifier.last_evidence}
    except Exception as exc:
        return {'outcome': 'REFUSED', 'code': getattr(exc, 'code', type(exc).__name__),
                'message': str(exc), 'verification': deepcopy(getattr(backend, 'runtime_diagnostic', None))}


def fresh(args, origin):
    from cmw.jobs.store import Store
    from cmw.nodes.backend import LocalBackend
    from cmw.nodes.identity import load_node, runtime_observed
    record = json.loads((args.output / 'request-original.json').read_text())
    original = json.loads((args.output / 'enrollment-original.json').read_text())
    backend = LocalBackend(record['config'], 'research', defer_check=True)
    result = {'phase': 'fresh-isolated', 'origin': origin, 'safety_contract_passed': False,
              'positive_recovery_demonstrated': False}
    try:
        result['lookup'] = lookup(backend, record)
        current_runtime, current_observation = runtime_observed()
        result['current_runtime'] = current_runtime
        result['current_observation'] = current_observation
        result['original_security_complete'] = bool(security_complete(original['runtime_observation']))
        result['original_trust_unchanged'] = load_node(record['config']) == original
        result['enrollment_bytes_unchanged'] = (Path(record['config']) / 'node.json').read_bytes() == (args.output / 'enrollment-original.json').read_bytes()
        result['fingerprint_unchanged'] = current_runtime['fingerprint'] == original['runtime']['fingerprint']
        result['semantic_request_digest_unchanged'] = semantic_digest(record) == record['request_digest']
        result['durable_original_receipt_unchanged'] = Store(record['state']).request_lookup(record['request_id']) == record['receipt_record']
        assert all(result[key] for key in ('original_trust_unchanged', 'enrollment_bytes_unchanged',
            'fingerprint_unchanged', 'semantic_request_digest_unchanged', 'durable_original_receipt_unchanged'))
        observed = result['lookup']
        if result['original_security_complete']:
            assert observed['outcome'] == 'ACCEPTED_ORIGINAL_REQUEST', observed
            assert observed['recovered'] and observed['verification']['category'] in {'ctime_only_current_disk_verified', 'uf_tracked_current_disk_verified'}
            result['supported_status'] = backend.call('status', {})['result']
            result['supported_preview'] = backend.call('control.preview', {'action': 'hold'}, record['target'])['result']
            assert len(Store(record['state']).snapshot()['jobs']) == 1
            result.update(safety_contract_passed=True, positive_recovery_demonstrated=True,
                          qualification='BOUNDED_CURRENT_DISK_RECOVERY_AND_ORIGINAL_REQUEST_LOOKUP')
        else:
            assert observed['outcome'] == 'REFUSED' and observed['code'] == 'RUNTIME_MISMATCH', observed
            assert observed['verification']['category'] == 'security_baseline_incomplete', observed
            result.update(safety_contract_passed=True,
                          qualification='REFUSAL_ONLY_SECURITY_BASELINE_INCOMPLETE')
        result['imported_cmw_modules'] = [{
            'module': name, 'path': getattr(module, '__file__', None),
            'cached_path': getattr(module, '__cached__', None),
            'cache_exists': bool(getattr(module, '__cached__', None) and Path(module.__cached__).exists())}
            for name, module in tuple(sys.modules.items()) if name == 'cmw' or name.startswith('cmw.')]
    except BaseException as exc:
        result['error'] = {'type': type(exc).__name__, 'message': str(exc),
                           'verification': getattr(exc, 'evidence', None)}
    finally:
        backend.close()
        result['backend_closed'] = backend._closed
        write(args.output / 'fresh-isolated-result.json', result)
    return result


def gate(args, origin):
    import cmw
    from cmw.jobs.store import Store
    from cmw.nodes.backend import LocalBackend
    from cmw.nodes.identity import enroll, load_node, runtime_observed
    from cmw.nodes.runtime_observation import file_epoch

    args.output.mkdir(mode=0o700)
    from unittest.mock import patch
    from cmw.nodes import identity
    collection = []
    original_observer = identity.runtime_observed
    capture = patch.object(identity, 'runtime_observed',
                           lambda: original_observer(diagnostics=collection))
    capture.start()
    result = {'origin': origin, 'safety_contract_passed': False,
              'positive_recovery_demonstrated': False, 'payloads_started': 0,
              'quiet_window_used': False, 'retries': 0}
    backend = None
    try:
        work = args.output / 'private-work'
        work.mkdir(mode=0o700)
        store = Store(args.output / 'private-queue')
        job = store.add(argv=['/usr/bin/true'], cwd=work, name='Runtime trust fixture', hold=True)
        config_root = args.output / 'private-node'
        config = enroll(config_root, {'queues': {'research': str(store.root)},
            'storages': {'scratch': str(work)},
            'profiles': {'benign': {'engine': 'Command', 'runner': '/usr/bin/true', 'max_cpus': 1}}})
        enrollment_bytes = (config_root / 'node.json').read_bytes()
        (args.output / 'enrollment-original.json').write_bytes(enrollment_bytes)
        backend = LocalBackend(config_root, 'research', defer_check=True)
        target = {'node_id': config['node_id'], 'queue_id': config['queues']['research']['queue_id'],
                  'job_id': job['id'], 'attempt_id': job['attempt_id']}
        preview = backend.call('control.preview', {'action': 'hold'}, target)['result']
        params = {'action': 'hold', 'expected_clock_id': preview['expected_clock_id'],
                  'expected_revision': preview['expected_revision'], 'confirm': False, 'reason': ''}
        record = {'config': str(config_root), 'state': str(store.root), 'target': target,
                  'request_id': uuid4().hex, 'control_params': params}
        record['request_digest'] = semantic_digest(record)
        endpoint = {'node_id': config['node_id'], 'queue_id': target['queue_id'],
                    'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
                    'clock_id': preview['expected_clock_id']}
        receipt = backend.call('control', {**params, '_endpoint': endpoint}, target,
                               request_id=record['request_id'])['result']
        assert receipt['digest'] == record['request_digest']
        record['receipt_record'] = backend.call('request', {'request_id': record['request_id']})['result']['request']
        assert record['receipt_record'] == store.request_lookup(record['request_id'])
        write(args.output / 'request-original.json', record)
        original_trust = deepcopy(backend._runtime_verifier.original)
        member = Path(cmw.__file__)
        assert not member.is_symlink() and member.resolve().is_relative_to(Path(origin['install']))
        before = file_epoch(member)
        before_bytes = member.read_bytes()
        assert before_bytes and len(before_bytes) <= 16 * 1024**2
        # Sacrificial same-object UF_TRACKED transition, never a final install workaround.
        descriptor = os.open(member, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            opened = os.fstat(descriptor)
            assert (opened.st_dev, opened.st_ino) == before[:2]
            assert opened.st_nlink == 1, 'Disposable package member is hardlinked; require uv --link-mode copy'
            assert sys.platform == 'darwin' and hasattr(os, 'fchflags')
            os.fchflags(descriptor, opened.st_flags ^ 0x40)
        finally:
            os.close(descriptor)
        after = file_epoch(member)
        result['mutation'] = {'member': str(member), 'before': before, 'after': after,
            'bytes_sha256': hashlib.sha256(before_bytes).hexdigest(),
            'bytes_equal': member.read_bytes() == before_bytes,
            'ctime_changed': before[4] != after[4],
            'uf_tracked_xor': before[8] ^ after[8],
            'all_other_epoch_fields_equal': before[:4] == after[:4] and before[5:8] == after[5:8] and before[9:] == after[9:]}
        write(args.output / 'mutation.json', result['mutation'])
        assert result['mutation']['uf_tracked_xor'] == 0x40
        assert all(result['mutation'][key] for key in ('bytes_equal', 'ctime_changed', 'all_other_epoch_fields_equal'))
        result['long_lived_lookup'] = lookup(backend, record)
        refused = result['long_lived_lookup']
        assert refused['outcome'] == 'REFUSED' and refused['code'] == 'RUNTIME_MISMATCH', refused
        assert refused['verification']['category'] in {'security_baseline_incomplete', 'fresh_isolated_observer_required'}, refused
        assert refused['verification']['before'] and refused['verification']['after']
        assert backend._runtime_verifier.original == original_trust
        assert backend._runtime_verifier.current == original_trust
        current_runtime, observation = runtime_observed()
        result['after_mutation_runtime'] = current_runtime
        result['after_mutation_observation'] = observation
        assert current_runtime['fingerprint'] == config['runtime']['fingerprint']
        assert semantic_digest(record) == record['request_digest']
        assert store.request_lookup(record['request_id']) == record['receipt_record']
        assert (config_root / 'node.json').read_bytes() == enrollment_bytes
        backend.close()
        command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                   '--campaign-root', str(args.campaign_root), '--phase', 'fresh', '--install-root', origin['install'], '--output', str(args.output)]
        result['fresh_command'] = command
        child = subprocess.run(command, capture_output=True, text=True, timeout=30)
        (args.output / 'fresh-process.log').write_text(child.stdout + child.stderr)
        result['fresh_returncode'] = child.returncode
        result['fresh'] = json.loads((args.output / 'fresh-isolated-result.json').read_text())
        assert child.returncode == 0 and result['fresh']['safety_contract_passed'], result['fresh']
        assert load_node(config_root) == config and (config_root / 'node.json').read_bytes() == enrollment_bytes
        state = store.snapshot()
        assert all(row['status'] == 'Hold' for row in state['jobs'])
        assert not state['controller']['online'] and not state['controller']['dispatch']
        result.update(safety_contract_passed=True,
            positive_recovery_demonstrated=result['fresh']['positive_recovery_demonstrated'],
            qualification=result['fresh']['qualification'], enrollment_bytes_unchanged=True,
            fingerprint_unchanged=True, semantic_request_digest_unchanged=True,
            durable_original_receipt_unchanged=True)
    except BaseException as exc:
        result['error'] = {'type': type(exc).__name__, 'message': str(exc),
                           'verification': getattr(exc, 'evidence', None)}
    finally:
        if backend is not None:
            backend.close()
        result['backend_closed'] = backend is None or backend._closed
        capture.stop()
        result['runtime_collection'] = collection
        write(args.output / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign-root', type=Path, required=True)
    parser.add_argument('--install-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--phase', choices=('gate', 'fresh'), default='gate')
    args = parser.parse_args()
    args.output = args.output.resolve()
    origin = verify_install(args)
    result = fresh(args, origin) if args.phase == 'fresh' else gate(args, origin)
    print(json.dumps({key: result.get(key) for key in
        ('safety_contract_passed', 'positive_recovery_demonstrated', 'qualification', 'error')}))
    return 0 if result['safety_contract_passed'] and result['positive_recovery_demonstrated'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
