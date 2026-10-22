"""Portable held registration over real endpoints and disposable node-local Stores.

These are isolated same-host protocol tests, not physical dual-node acceptance.
No controller, worker, scientific engine or user queue is started.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from cmw.jobs.store import Store
from cmw.nodes.client import submit as client_submit
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.identity import enroll
from cmw.nodes.protocol import NodeError, encode, request
from cmw.nodes.submission import submission_digest
from cmw.nodes.transfer import prepare_local_bundle, upload_local_bundle
from cmw.nodes.transport import ProcessTransport


class SubmissionEndpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-submission-endpoint-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = Path(__file__).resolve().parents[2]
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(self.source / 'src')}

    def node(self, name='mini', queues=('research',)):
        root = self.root / name
        root.mkdir(mode=0o700)
        storage = root / 'calculation'
        storage.mkdir(mode=0o700)
        runner = root / 'trusted-benign-runner'
        runner.write_text('#!/bin/sh\nexit 0\n')
        runner.chmod(0o700)
        spec = {'queues': {alias: str(root / ('queue-' + alias)) for alias in queues},
                'storages': {'scratch': str(storage)},
                'profiles': {'benign': {'engine': 'Command', 'runner': str(runner), 'max_cpus': 1}}}
        config = enroll(root / 'node', spec)
        node = {'root': root, 'storage': storage, 'runner': runner, 'config': config, 'backends': {}}
        for queue in queues:
            node['backends'][queue] = self.connect(node, queue)
        return node

    def connect(self, node, queue='research', *, read_only=False, runtime_id=None):
        config = node['config']
        argv = [sys.executable, '-B', '-m', 'cmw.cli', 'nodes', 'endpoint',
                '--config', str(node['root'] / 'node'), '--queue', queue]
        transport = ProcessTransport(argv, timeout=30, env=self.env)
        entry = {'transport': 'local', 'config_root': str(node['root'] / 'node'),
                 'node_id': config['node_id'], 'host_id': config['host_id'],
                 'runtime_id': runtime_id or config['runtime']['fingerprint'],
                 'queues': {alias: record['queue_id'] for alias, record in config['queues'].items()},
                 'default_queue': queue, 'read_only': read_only}
        backend = ConnectedBackend(node['root'].name, entry, queue, transport=transport)
        self.addCleanup(backend.close)
        return backend

    def store(self, node, queue='research'):
        return Store(node['config']['queues'][queue]['state'])

    def prepare(self, node, queue='research', *, request_id=None):
        request_id = request_id or uuid4().hex
        source = node['root'] / ('selected-' + request_id + '.txt')
        source.write_text('Explicit selected benign input\n')
        selections = [{'source': str(source), 'path': 'inputs/input.txt', 'role': 'input'}]
        prepared = prepare_local_bundle(node['root'] / ('spool-' + request_id), selections,
            node_id=node['config']['node_id'], queue_id=node['config']['queues'][queue]['queue_id'])
        specification = {'schema': 1, 'request_id': request_id, 'node_id': node['config']['node_id'],
            'queue_id': node['config']['queues'][queue]['queue_id'], 'storage': 'scratch', 'profile': 'benign',
            'engine': 'Command', 'name': 'Benign held request', 'resources': {'cpus': 1, 'memory_gib': 1},
            'manifest': prepared['manifest'], 'arguments': [{'bundle': 'inputs/input.txt'}],
            'intent': 'held', 'dependencies_complete': True}
        return specification, prepared, selections

    def publish(self, node, spec, prepared, queue='research'):
        backend = node['backends'][queue]
        receipt = backend.call('transfer.begin', {'storage': 'scratch', 'request_id': spec['request_id'],
                                                  'manifest': spec['manifest']})['result']
        upload_local_bundle(prepared['spool'], receipt)
        return backend.call('transfer.publish', {'storage': 'scratch', 'request_id': spec['request_id'],
            'manifest_digest': prepared['manifest_digest']})['result']

    def register(self, backend, spec):
        return backend.call('submit', {'spec': spec}, request_id=spec['request_id'])['result']

    def test_preview_publish_register_held_and_lookup_through_real_endpoint(self):
        node = self.node()
        backend = node['backends']['research']
        spec, prepared, _ = self.prepare(node)
        before = self.store(node).path.read_bytes()
        preview = backend.call('submit.preview', {'spec': spec})['result']
        self.assertEqual(preview['status'], 'PREVIEW')
        self.assertEqual(preview['digest'], submission_digest(spec))
        self.assertEqual(preview['argv'][0], str(node['runner']))
        self.assertEqual(self.store(node).path.read_bytes(), before)
        self.assertFalse((node['storage'] / ('cmw-' + node['config']['node_id'])).exists())
        receipt = self.publish(node, spec, prepared)
        self.assertEqual(receipt['status'], 'PUBLISHED')
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        result = self.register(backend, spec)
        self.assertEqual(result['status'], 'REGISTERED')
        self.assertFalse(result['replayed'])
        job = result['job']
        self.assertEqual(job['status'], 'Hold')
        self.assertNotEqual(job['attempt_id'], spec['request_id'])
        self.assertEqual(job['node_submission']['published_root'], receipt['published_root'])
        self.assertEqual(job['node_submission']['queue_id'], spec['queue_id'])
        self.assertTrue(Path(job['argv'][1]).is_file())
        self.assertFalse(Path(preview['native_output']).exists())
        state = self.store(node).snapshot()
        self.assertFalse(state['controller']['dispatch'])
        self.assertFalse(state['controller']['online'])
        self.assertIsNone(state['jobs'][0]['worker'])
        lookup = backend.call('request', {'request_id': spec['request_id']})['result']['request']
        self.assertEqual(lookup['result'], job)
        target = backend.call('lookup', {'id': job['display_id']})['result']['target']
        self.assertEqual((target['node_id'], target['queue_id'], target['attempt_id']),
                         (spec['node_id'], spec['queue_id'], job['attempt_id']))

    def test_same_id_retry_returns_original_attempt_and_changed_semantics_conflict(self):
        node = self.node()
        backend = node['backends']['research']
        spec, prepared, _ = self.prepare(node)
        self.publish(node, spec, prepared)
        first = self.register(backend, spec)
        second = self.register(backend, spec)
        self.assertTrue(second['replayed'])
        self.assertEqual(second['job'], first['job'])
        changed = {**spec, 'name': 'Different reviewed intent'}
        with self.assertRaisesRegex(NodeError, 'different semantic'):
            self.register(backend, changed)
        self.assertEqual(len(self.store(node).snapshot()['jobs']), 1)

    def test_independent_nodes_same_display_ids_keep_distinct_attempts_and_requests(self):
        left, right = self.node('mbp'), self.node('mini')
        request_id = uuid4().hex
        jobs = []
        for node in (left, right):
            spec, prepared, _ = self.prepare(node, request_id=request_id)
            self.publish(node, spec, prepared)
            jobs.append(self.register(node['backends']['research'], spec)['job'])
        self.assertEqual(jobs[0]['display_id'], jobs[1]['display_id'])
        self.assertNotEqual(jobs[0]['attempt_id'], jobs[1]['attempt_id'])
        self.assertNotEqual(jobs[0]['node_submission']['node_id'], jobs[1]['node_submission']['node_id'])
        self.assertNotEqual(jobs[0]['node_submission']['queue_id'], jobs[1]['node_submission']['queue_id'])

    def test_wrong_node_queue_and_endpoint_guard_fail_before_staging(self):
        node = self.node(queues=('research', 'other'))
        backend = node['backends']['research']
        spec, prepared, _ = self.prepare(node)
        for key in ('node_id', 'queue_id'):
            changed = deepcopy(spec)
            changed[key] = uuid4().hex
            changed['manifest'][key] = changed[key]
            with self.subTest(key=key), self.assertRaises(NodeError):
                self.register(backend, changed)
        with self.assertRaises(NodeError):
            node['backends']['other'].call('transfer.begin', {'storage': 'scratch',
                'request_id': spec['request_id'], 'manifest': spec['manifest']})
        capability = backend.call('capabilities')
        guard = {'node_id': spec['node_id'], 'queue_id': spec['queue_id'],
                 'host_id': node['config']['host_id'], 'runtime_id': node['config']['runtime']['fingerprint'],
                 'clock_id': '0' * 64}
        with self.assertRaisesRegex(NodeError, 'endpoint/boot guard'):
            backend.transport.exchange('transfer.begin', {'storage': 'scratch', 'request_id': spec['request_id'],
                'manifest': spec['manifest'], '_endpoint': guard})
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        self.assertFalse((node['storage'] / ('cmw-' + node['config']['node_id'])).exists())

    def test_transfer_publication_and_lookup_cannot_cross_enrolled_queue(self):
        node = self.node(queues=('research', 'other'))
        first, other = node['backends']['research'], node['backends']['other']
        spec, prepared, _ = self.prepare(node)
        receipt = first.call('transfer.begin', {'storage': 'scratch', 'request_id': spec['request_id'],
                                              'manifest': spec['manifest']})['result']
        upload_local_bundle(prepared['spool'], receipt)
        with self.assertRaises(NodeError):
            other.call('transfer.lookup', {'storage': 'scratch', 'request_id': spec['request_id']})
        with self.assertRaises(NodeError):
            other.call('transfer.publish', {'storage': 'scratch', 'request_id': spec['request_id'],
                                           'manifest_digest': prepared['manifest_digest']})
        self.assertFalse(Path(receipt['published_root']).exists())
        self.assertEqual(self.store(node, 'other').snapshot()['jobs'], [])

    def test_incomplete_and_changed_published_bundle_never_register(self):
        node = self.node()
        backend = node['backends']['research']
        spec, prepared, _ = self.prepare(node)
        with self.assertRaisesRegex(NodeError, 'published bundle'):
            self.register(backend, spec)
        receipt = backend.call('transfer.begin', {'storage': 'scratch', 'request_id': spec['request_id'],
                                                  'manifest': spec['manifest']})['result']
        with self.assertRaises(NodeError):
            backend.call('transfer.publish', {'storage': 'scratch', 'request_id': spec['request_id'],
                                              'manifest_digest': prepared['manifest_digest']})
        self.assertFalse(Path(receipt['published_root']).exists())
        receipt = self.publish(node, spec, prepared)
        (Path(receipt['published_root']) / 'inputs/input.txt').write_text('different bytes\n')
        with self.assertRaises(NodeError):
            self.register(backend, spec)
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        self.assertIsNone(self.store(node).request_lookup(spec['request_id']))

    def test_missing_and_same_named_replacement_storage_refused_without_fallback(self):
        node = self.node()
        backend = node['backends']['research']
        spec, prepared, _ = self.prepare(node)
        old = node['storage'].with_name('retained-original-calculation')
        node['storage'].rename(old)
        for replacement in (False, True):
            if replacement:
                node['storage'].mkdir(mode=0o700)
            with self.subTest(replacement=replacement), self.assertRaises(NodeError):
                backend.call('submit.preview', {'spec': spec})
            with self.assertRaises(NodeError):
                backend.call('transfer.begin', {'storage': 'scratch', 'request_id': spec['request_id'],
                                               'manifest': spec['manifest']})
            if not replacement:
                self.assertFalse(node['storage'].exists())
            else:
                self.assertEqual(list(node['storage'].iterdir()), [])
        self.assertEqual(self.store(node).snapshot()['jobs'], [])

    def test_changed_runner_and_pinned_runtime_refused_before_staging(self):
        node = self.node()
        backend = node['backends']['research']
        spec, prepared, _ = self.prepare(node)
        wrong = self.connect(node, runtime_id='wrong-runtime')
        with self.assertRaisesRegex(NodeError, 'runtime identity changed'):
            wrong.call('transfer.begin', {'storage': 'scratch', 'request_id': spec['request_id'],
                                          'manifest': spec['manifest']})
        node['runner'].write_text('#!/bin/sh\nexit 7\n')
        with self.assertRaisesRegex(NodeError, 'preflight failed'):
            backend.call('submit.preview', {'spec': spec})
        with self.assertRaises(NodeError):
            self.register(backend, spec)
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        self.assertFalse((node['storage'] / ('cmw-' + node['config']['node_id'])).exists())

    def test_resources_intent_and_dependencies_fail_before_publication(self):
        node = self.node()
        backend = node['backends']['research']
        spec, _, _ = self.prepare(node)
        changes = [{'resources': {'cpus': 2, 'memory_gib': 1}}, {'intent': 'run'},
            {'dependencies_complete': False}, {'arguments': [{'bundle': 'support/missing.txt'}]},
            {'arguments': [{'dependency': 0}]}, {'arguments': ['/undeclared/path']}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(NodeError):
                backend.call('submit.preview', {'spec': {**spec, **change}})
        self.assertFalse((node['storage'] / ('cmw-' + node['config']['node_id'])).exists())

    def test_read_only_connection_and_direct_transport_cannot_mutate_without_handshake(self):
        node = self.node()
        spec, _, _ = self.prepare(node)
        readonly = self.connect(node, read_only=True)
        with self.assertRaisesRegex(NodeError, 'observation only'):
            self.register(readonly, spec)
        direct = self.connect(node)
        with self.assertRaisesRegex(NodeError, 'capabilities'):
            direct.transport.exchange('submit', {'spec': spec}, request_id=spec['request_id'])
        self.assertEqual(self.store(node).snapshot()['jobs'], [])

    def test_broken_reply_pipe_after_registration_recovers_original_attempt(self):
        node = self.node()
        backend = node['backends']['research']
        spec, prepared, _ = self.prepare(node)
        self.publish(node, spec, prepared)
        capability = backend.call('capabilities')
        guard = {'node_id': spec['node_id'], 'queue_id': spec['queue_id'], 'host_id': node['config']['host_id'],
                 'runtime_id': node['config']['runtime']['fingerprint'], 'clock_id': capability['clock_id']}
        argv = [sys.executable, '-B', '-m', 'cmw.cli', 'nodes', 'endpoint',
                '--config', str(node['root'] / 'node'), '--queue', 'research']
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=self.env)
        try:
            packet = encode(request('submit', {'spec': spec, '_endpoint': guard}, request_id=spec['request_id']))
            process.stdin.write(packet)
            process.stdin.flush()
            process.stdout.close()  # Actual reply pipe disappears; no reply is read or synthesized.
            process.stdin.close()
            code = process.wait(timeout=30)
            diagnostic = process.stderr.read().decode(errors='replace')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            process.stderr.close()
        self.assertNotEqual(code, 0)
        self.assertIn('BrokenPipeError', diagnostic)
        lookup = backend.call('request', {'request_id': spec['request_id']})['result']['request']
        self.assertIsNotNone(lookup)
        retry = self.register(backend, spec)
        self.assertTrue(retry['replayed'])
        self.assertEqual(retry['job'], lookup['result'])
        self.assertEqual(len(self.store(node).snapshot()['jobs']), 1)
        self.assertEqual(retry['job']['status'], 'Hold')

    def test_client_preparation_is_durable_before_requests_and_retry_reuses_outcome(self):
        node = self.node()
        backend = node['backends']['research']
        spec, prepared, selections = self.prepare(node)
        frontend = {key: value for key, value in spec.items() if key != 'manifest'}
        frontend['files'] = selections
        client_root = self.root / 'frontend'
        record = client_root / 'requests' / spec['request_id'] / 'request.json'
        seen = []
        class RecordingBackend:
            def __getattr__(self, name):
                return getattr(backend, name)
            def call(self, operation, *args, **kwargs):
                persisted = json.loads(record.read_text())
                if persisted['spec']['request_id'] != spec['request_id'] or persisted['digest'] != submission_digest(persisted['spec']):
                    raise AssertionError('Portable identity must be durable before the first request')
                seen.append(operation)
                return backend.call(operation, *args, **kwargs)
        recorder = RecordingBackend()
        preview = client_submit(recorder, frontend, root=client_root, preview=True)
        self.assertEqual(preview['result']['status'], 'PREVIEW')
        self.assertFalse(preview['result']['dispatch_intent'])
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        self.assertNotIn('transfer.begin', seen)
        result = client_submit(recorder, frontend, root=client_root)
        self.assertEqual(result['result']['job']['status'], 'Hold')
        source = Path(selections[0]['source'])
        source.unlink()  # Successful recovery does not need the original MacBook source again.
        retried = client_submit(recorder, frontend, root=client_root)
        self.assertTrue(retried['result']['replayed'])
        self.assertEqual(retried['result']['job']['attempt_id'], result['result']['job']['attempt_id'])
        with self.assertRaisesRegex(NodeError, 'different reviewed'):
            client_submit(recorder, {**frontend, 'name': 'Changed'}, root=client_root)
        self.assertEqual(len(self.store(node).snapshot()['jobs']), 1)

    def test_malformed_portable_types_return_structured_refusal_without_side_effects(self):
        node = self.node()
        backend = node['backends']['research']
        spec, _, _ = self.prepare(node)
        before = self.store(node).path.read_bytes()
        cases = [
            ('submit.preview', {'spec': {**spec, 'engine': []}}, 'INVALID_SUBMISSION'),
            ('submit.preview', {'spec': {**spec, 'engine': {}}}, 'INVALID_SUBMISSION'),
            ('transfer.begin', {'storage': [], 'request_id': spec['request_id'],
                                'manifest': spec['manifest']}, 'INVALID_SUBMISSION'),
            ('transfer.begin', {'storage': 'scratch', 'request_id': spec['request_id'],
                                'manifest': []}, 'INVALID_MANIFEST'),
        ]
        for operation, params, code in cases:
            with self.subTest(operation=operation, params=params):
                with self.assertRaises(NodeError) as caught:
                    backend.call(operation, params)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.store(node).path.read_bytes(), before)
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        self.assertFalse((node['storage'] / ('cmw-' + node['config']['node_id'])).exists())
        self.assertEqual(backend.call('capabilities')['node_id'], spec['node_id'])

    def test_cli_spec_reader_rejects_duplicate_nonfinite_oversized_and_fifo(self):
        from cmw.nodes.cli import _read_spec
        source = self.root / 'request.json'
        for raw in ('{"schema":1,"schema":2}', '{"value":NaN}', ' ' * (128 * 1024 + 1)):
            with self.subTest(raw=raw[:40]):
                source.write_text(raw)
                with self.assertRaises(NodeError):
                    _read_spec(source)
        source.unlink()
        os.mkfifo(source)
        script = ('from cmw.nodes.cli import _read_spec; from cmw.nodes.protocol import NodeError; '
                  'import sys\ntry: _read_spec(sys.argv[1])\n'
                  'except NodeError as exc: print(exc.code)\nelse: raise AssertionError("FIFO accepted")\n')
        result = subprocess.run([sys.executable, '-B', '-c', script, str(source)],
                                env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'INVALID_SPEC')
