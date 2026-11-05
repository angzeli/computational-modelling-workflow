"""Successor owner boundaries, strict wire negotiation and mutation guards."""
from copy import deepcopy
from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.successor_models import SuccessorResult, SuccessorUnverifiable
from cmw.jobs.store import JobsError, Store
from cmw.nodes.codec import CodecError, decode_typed, encode_typed
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.protocol import NodeError, decode, encode, is_mutation, request, validate_envelope
from tests.nodes import test_backend
from tests.nodes.test_review import DirectTransport


class SuccessorNodeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_backend.BackendObservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend, self.store = self.fixture.backend, self.fixture.store
        self.job = self.fixture.job()
        self.target = self.backend.target(self.job)
        self.digest = 'a' * 64
        self.patcher = patch('cmw.jobs.successor.SuccessorService')
        self.factory = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.service = self.factory.return_value
        for action, status in (('plan', 'READY'), ('materialize', 'MATERIALIZED'), ('register', 'REGISTERED')):
            getattr(self.service, action).return_value = SuccessorResult(1, self.target['node_id'],
                self.target['queue_id'], self.job['id'], self.job['attempt_id'], action, status,
                self.digest, json.dumps({'scope': 'synthetic interface test; no scientific claim'}))

    def client(self, *, readonly=True, transform=None):
        entry = {'transport': 'local', 'config_root': str(self.fixture.root / 'node-config'),
            'node_id': self.target['node_id'], 'host_id': self.fixture.config['host_id'],
            'runtime_id': self.fixture.config['runtime']['fingerprint'],
            'queues': {'research': self.target['queue_id']}, 'default_queue': 'research', 'read_only': readonly}
        client = ConnectedBackend('fixture', entry, transport=DirectTransport(self.backend, transform))
        self.addCleanup(client.close)
        return client

    def test_plan_is_read_only_owner_local_and_exact_attempt_bound(self):
        client = self.client()
        before = self.store.path.read_bytes()
        path = '/owner only/不存在/spec.json'
        envelope = client.call('successor.plan', {'spec_path': path}, self.target)
        result = decode_typed(envelope['result']['successor'], SuccessorResult)
        self.assertEqual(result, self.service.plan.return_value)
        self.service.plan.assert_called_once_with(self.job['id'], path, expected_attempt_id=self.job['attempt_id'])
        self.assertEqual(self.factory.call_args.kwargs['origin']['node_id'], self.target['node_id'])
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertFalse(is_mutation('successor.plan'))
        self.assertEqual(client.transport.calls, ['capabilities', 'successor.plan'])

    def test_publication_and_registration_require_verified_write_endpoint(self):
        for action, params in (
            ('materialize', {'spec_path': '/owner/spec.json', 'approved_digest': self.digest}),
            ('register', {'record_path': '/owner/materialization.json', 'approved_digest': self.digest}),
        ):
            operation = 'successor.' + action
            self.assertTrue(is_mutation(operation))
            with self.assertRaises(NodeError) as caught:
                self.backend.call(operation, params, self.target, request_id=uuid4().hex)
            self.assertEqual(caught.exception.code, 'ENDPOINT_CHANGED')
            with self.assertRaises(NodeError) as caught:
                self.client().call(operation, params, self.target, request_id=uuid4().hex)
            self.assertEqual(caught.exception.code, 'READ_ONLY')
        client = self.client(readonly=False)
        client.call('successor.materialize', {'spec_path': '/owner/spec.json', 'approved_digest': self.digest}, self.target)
        self.service.materialize.assert_called_once_with(self.job['id'], '/owner/spec.json', self.digest,
            expected_attempt_id=self.job['attempt_id'])
        request = uuid4().hex
        params = {'record_path': '/owner/materialization.json', 'approved_digest': self.digest}
        first = client.call('successor.register', params, self.target, request_id=request)
        second = client.call('successor.register', params, self.target, request_id=request)
        self.assertEqual(first['result'], second['result'])
        for call in self.service.register.call_args_list:
            self.assertEqual(call.args, (self.job['id'], '/owner/materialization.json', self.digest, request))
            self.assertEqual(call.kwargs, {'expected_attempt_id': self.job['attempt_id']})
        self.assertEqual(self.store.snapshot()['jobs'][0]['status'], 'Hold')

    def test_endpoint_reports_retryable_plan_materialization_and_registration(self):
        from cmw.nodes.endpoint import serve
        reason = 'Parent queue temporarily unreadable; retry'
        self.service.plan.return_value = replace(self.service.plan.return_value, status='UNVERIFIABLE',
            payload_json=json.dumps({'closure': {'reason': reason}}))
        self.service.materialize.return_value = replace(self.service.materialize.return_value,
            status='PENDING_VERIFICATION', payload_json=json.dumps({'record_path': '/owner/record', 'reason': reason}))
        self.service.register.side_effect = SuccessorUnverifiable(reason)
        frames = io.BytesIO()
        for operation, params in (('plan', {'spec_path': '/owner/spec'}),
                                  ('materialize', {'spec_path': '/owner/spec'}),
                                  ('register', {'record_path': '/owner/record'})):
            if operation != 'plan':
                params.update(approved_digest=self.digest, _endpoint={'node_id': self.target['node_id'],
                    'queue_id': self.target['queue_id'], 'host_id': self.fixture.config['host_id'],
                    'runtime_id': self.fixture.config['runtime']['fingerprint'], 'clock_id': self.backend.clock_id})
            frames.write(encode(request('successor.' + operation, params, self.target)))
        frames.seek(0)
        replies = io.BytesIO()
        self.assertEqual(serve(None, 'research', input_stream=frames, output_stream=replies,
                               backend_factory=lambda *_: self.backend), 0)
        replies.seek(0)
        for expected_code in ('SUCCESSOR_UNVERIFIABLE', 'SUCCESSOR_PENDING_VERIFICATION', 'SUCCESSOR_UNVERIFIABLE'):
            reply = decode(replies.readline())
            self.assertIs(reply['ok'], False)
            self.assertEqual(reply['error']['code'], expected_code)
            self.assertIn(reason, reply['error']['message'])
            self.assertEqual(set(reply['error']), {'code', 'message'})

    def test_queue_lock_at_preflight_is_retryable_but_response_loss_after_registration_is_unknown(self):
        from cmw.jobs.lifecycle import JobObservationUnavailable
        with patch.object(Store, 'queue_identity', side_effect=PermissionError('read denied')):
            for op, code in (('lookup', 'QUEUE_UNAVAILABLE'), ('capabilities', 'QUEUE_UNAVAILABLE'),
                             ('successor.plan', 'SUCCESSOR_UNVERIFIABLE')):
                with self.subTest(op=op), self.assertRaises(NodeError) as refused:
                    self.backend.call(op)
                self.assertEqual(refused.exception.code, code)
        with patch.object(self.backend, '_job', side_effect=[self.job, JobObservationUnavailable('read denied')]):
            with self.assertRaises(NodeError) as refused:
                self.client(readonly=False).call('successor.register', {'record_path': '/owner/record',
                    'approved_digest': self.digest}, self.target, request_id=uuid4().hex)
            self.assertEqual(refused.exception.code, 'OUTCOME_UNKNOWN')

    def test_old_or_ambiguous_capability_refuses_before_operation(self):
        for capability in (None, {}, {'schema': True, 'model': 'SuccessorResult', 'geometry_only': True},
                           {'schema': 1, 'model': 'SuccessorResult', 'geometry_only': 1}):
            def transform(operation, value):
                if operation == 'capabilities':
                    value['result']['successor_planner'] = capability
                return value
            client = self.client(transform=transform)
            with self.subTest(capability=capability), self.assertRaises(NodeError) as caught:
                client.call('successor.plan', {'spec_path': '/owner/spec.json'}, self.target)
            self.assertEqual(caught.exception.code, 'UNSUPPORTED_SUCCESSOR')
            self.assertEqual(client.transport.calls, ['capabilities'])
        self.service.plan.assert_not_called()

    def test_invalid_paths_parameters_digest_and_parent_never_reach_service(self):
        client = self.client(readonly=False)
        for params in ({}, {'spec_path': 'relative.json'}, {'spec_path': '/x', 'copy': True},
                       {'spec_path': '/x\0unsafe'}, {'spec_path': False}):
            with self.subTest(params=params), self.assertRaises((NodeError, ValueError)):
                client.call('successor.plan', params, self.target)
        for digest in (None, True, '0'*63, 'g'*64):
            with self.assertRaises(NodeError):
                client.call('successor.materialize', {'spec_path': '/owner/spec', 'approved_digest': digest}, self.target)
        for key in ('node_id', 'queue_id', 'attempt_id'):
            with self.subTest(key=key), self.assertRaises((NodeError, JobsError)):
                client.call('successor.plan', {'spec_path': '/owner/spec'}, {**self.target, key: uuid4().hex})
        self.service.plan.assert_not_called()
        self.service.materialize.assert_not_called()

    def test_strict_result_codec_and_owner_parent_operation_checks(self):
        encoded = encode_typed(self.service.plan.return_value)
        for change in ('extra', 'missing', 'schema_boolean', 'payload_array'):
            value = deepcopy(encoded)
            if change == 'extra': value['value']['scientific_pass'] = True
            elif change == 'missing': del value['value']['operation']
            elif change == 'schema_boolean': value['value']['schema'] = True
            else: value['value']['payload_json'] = '[]'
            with self.subTest(change=change), self.assertRaises(CodecError):
                decode_typed(value, SuccessorResult)
        envelope = self.client().call('successor.plan', {'spec_path': '/owner/spec'}, self.target)
        for key, replacement in (('node_id', uuid4().hex), ('operation', 'materialize')):
            value = deepcopy(envelope)
            value['result']['successor']['value'][key] = replacement
            with self.assertRaises(NodeError):
                validate_envelope(value, 'successor.plan')
        def change_attempt(operation, value):
            if operation == 'successor.plan':
                value['result']['successor']['value']['attempt_id'] = uuid4().hex
            return value
        with self.assertRaisesRegex(NodeError, 'misbound'):
            self.client(transform=change_attempt).call('successor.plan', {'spec_path': '/owner/spec'}, self.target)

    def test_internal_target_does_not_follow_numeric_display_alias(self):
        other = self.fixture.job()
        with self.store.transaction() as connection:
            value = self.store.get(connection, other['id'])
            value['display_id'] = str(self.job['id'])
            self.store.save(connection, value)
        self.client().call('successor.plan', {'spec_path': '/owner/spec'}, self.target)
        self.service.plan.assert_called_once_with(self.job['id'], '/owner/spec', expected_attempt_id=self.job['attempt_id'])
