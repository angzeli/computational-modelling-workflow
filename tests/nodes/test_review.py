"""Owner-node review observations: strict identity, bounded work and compatibility."""
from copy import deepcopy
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.review import ReviewService
from cmw.jobs.review_models import ReviewRecord
from cmw.jobs.store import JobsError
from cmw.nodes.codec import CodecError, decode_typed, encode_typed
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.protocol import NodeError, encode, is_mutation, validate_envelope
from tests.nodes import test_backend


class DirectTransport:
    def __init__(self, backend, transform=None):
        self.backend, self.transform = backend, transform
        self.calls = []

    def exchange(self, operation, params=None, target=None, *, request_id=None):
        self.calls.append(operation)
        value = self.backend.call(operation, params, target, request_id=request_id)
        return self.transform(operation, value) if self.transform else value

    def close(self):
        self.backend.close()


class OwnerNodeTestCase(unittest.TestCase):
    """One owner-node backend fixture and a local client for owner-service node tests."""
    def setUp(self):
        self.fixture = test_backend.BackendObservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend, self.store = self.fixture.backend, self.fixture.store
        self.job = self.fixture.job()
        self.target = self.backend.target(self.job)

    def client(self, *, readonly=True, transform=None):
        entry = {'transport':'local', 'config_root':str(self.fixture.root/'node-config'),
            'node_id':self.target['node_id'], 'host_id':self.fixture.config['host_id'],
            'runtime_id':self.fixture.config['runtime']['fingerprint'],
            'queues':{'research':self.target['queue_id']}, 'default_queue':'research',
            'read_only':readonly}
        client = ConnectedBackend('fixture', entry, transport=DirectTransport(self.backend, transform))
        self.addCleanup(client.close)
        return client


class ReviewNodeTests(OwnerNodeTestCase):

    def call(self, operation, params=None, *, target=None, request_id=None):
        value = self.backend.call(operation, params, self.target if target is None else target,
                                  request_id=request_id)
        validate_envelope(value, operation)
        encode(value)
        return decode_typed(value['result']['review'], ReviewRecord)

    def paused_worker(self):
        entered, release = threading.Event(), threading.Event()
        def run(service, task):
            entered.set()
            while not release.wait(.01):
                if task['cancel'].is_set():
                    break
        patcher = patch.object(ReviewService, '_run', run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(release.set)
        return entered, release

    def test_preview_is_typed_owner_metadata_and_never_inspects_science(self):
        before = self.store.path.read_bytes()
        with patch.object(ReviewService, 'start', side_effect=AssertionError('preview started work')):
            record = self.call('review.preview')
            repeated = self.call('review.preview')
        self.assertEqual(record.status, 'PREVIEW')
        self.assertEqual(record.coverage, 'metadata only')
        self.assertEqual(record.domains, repeated.domains)
        self.assertTrue(all(domain.status == 'NOT ASSESSED' for domain in record.domains))
        self.assertEqual(record.sources_json, '[]')
        self.assertEqual((record.node_id,record.queue_id,record.job_id,record.attempt_id),
                         tuple(self.target[key] for key in ('node_id','queue_id','job_id','attempt_id')))
        self.assertEqual(decode_typed(encode_typed(record), ReviewRecord), record)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_start_is_nonblocking_and_retry_and_handle_are_target_bound(self):
        entered, release = self.paused_worker()
        other = self.fixture.job()
        before = self.store.path.read_bytes()
        request_id = uuid4().hex
        first = self.call('review.start', {'options':{}}, request_id=request_id)
        self.assertTrue(entered.wait(1))
        self.assertEqual(first.status, 'PENDING')
        self.assertFalse(release.is_set())
        # The endpoint lock is free while the synthetic review observer waits.
        status = self.backend.call('status')['result']
        self.assertEqual(len(status['state']['jobs']), 2)
        replay = self.call('review.start', {'options':{}}, request_id=request_id)
        self.assertEqual(replay.review_id, first.review_id)
        self.assertEqual(self.call('review.poll', {'review_id':first.review_id}).status, 'PENDING')
        with self.assertRaises(JobsError):
            self.call('review.start', {'options':{'max_bytes':42}}, request_id=request_id)
        with self.assertRaises(JobsError):
            self.call('review.poll', {'review_id':first.review_id}, target=self.backend.target(other))
        self.call('review.cancel', {'review_id':first.review_id})
        self.assertTrue(self.backend._review_service._tasks[first.review_id]['cancel'].is_set())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_wrong_owner_attempt_and_invalid_options_are_refused(self):
        for key in ('node_id','queue_id','attempt_id'):
            with self.subTest(key=key), self.assertRaises((JobsError,NodeError)):
                self.call('review.preview', target={**self.target,key:uuid4().hex})
        for operation, params in (
            ('review.preview', {'max_bytes':1}),
            ('review.start', {}), ('review.start', {'options':[]}),
            ('review.start', {'options':{'directory':'/invented'}}),
            ('review.start', {'options':{'timeout_seconds':121}}),
            ('review.poll', {'review_id':'unknown'}),
            ('review.cancel', {'review_id':uuid4().hex,'kill_job':True})):
            with self.subTest(operation=operation,params=params), self.assertRaises((JobsError,NodeError)):
                self.call(operation, params, request_id=uuid4().hex)

    def test_old_or_incompatible_endpoint_is_refused_before_review_request(self):
        for capability in (None, {}, {'schema':2,'async':True,'model':'ReviewRecord'},
                           {'schema':True,'async':True,'model':'ReviewRecord'},
                           {'schema':1,'async':False,'model':'ReviewRecord'}):
            def transform(operation, value):
                if operation == 'capabilities':
                    value['result']['result_review'] = capability
                return value
            client = self.client(transform=transform)
            with self.subTest(capability=capability), self.assertRaises(NodeError) as caught:
                client.call('review.preview', target=self.target)
            self.assertEqual(caught.exception.code, 'UNSUPPORTED_REVIEW')
            self.assertEqual(client.transport.calls, ['capabilities'])

    def test_strict_codec_unknown_shapes_and_conflicting_owner_fail_closed(self):
        record = self.call('review.preview')
        payload = encode_typed(record)
        variants = []
        altered = deepcopy(payload); altered['schema'] = 2; variants.append(altered)
        altered = deepcopy(payload); del altered['value']['coverage']; variants.append(altered)
        altered = deepcopy(payload); altered['value']['overall_pass'] = True; variants.append(altered)
        altered = deepcopy(payload); altered['value']['domains'][0]['invented'] = True; variants.append(altered)
        for value in variants:
            with self.assertRaises(CodecError):
                decode_typed(value, ReviewRecord)
        for key in ('node_id','queue_id','attempt_id'):
            def transform(operation, envelope):
                if operation == 'review.preview':
                    envelope['result']['review']['value'][key] = uuid4().hex
                return envelope
            # Each refused connection closes its observer, so use a fresh backend.
            self.backend = self.fixture.make_backend()
            client = self.client(transform=transform)
            with self.subTest(key=key), self.assertRaises(NodeError):
                client.call('review.preview', target=self.target)

    def test_export_start_obeys_endpoint_guard_and_readonly_connection(self):
        supplied = {'options':{'export_path':str(self.fixture.root/'explicit-review.json')}}
        self.assertTrue(is_mutation('review.start', supplied))
        self.assertFalse(is_mutation('review.start', {'options':{}}))
        with self.assertRaises(NodeError) as caught:
            self.call('review.start', supplied, request_id=uuid4().hex)
        self.assertEqual(caught.exception.code, 'ENDPOINT_CHANGED')
        readonly = self.client()
        with self.assertRaises(NodeError) as caught:
            readonly.call('review.start', supplied, self.target, request_id=uuid4().hex)
        self.assertEqual(caught.exception.code, 'READ_ONLY')
        self.assertEqual(readonly.transport.calls, [])
        entered, _ = self.paused_worker()
        client = self.client(readonly=False)
        value = client.call('review.start', supplied, self.target, request_id=uuid4().hex)
        self.assertTrue(entered.wait(1))
        self.assertEqual(value['result']['review']['value']['status'], 'PENDING')
        self.assertEqual(client.transport.calls, ['capabilities','review.start'])
        self.assertFalse((self.fixture.root/'explicit-review.json').exists())

    def test_disconnect_retains_last_review_observation_and_never_changes_job(self):
        client = self.client()
        record = client.call('review.preview', target=self.target)['result']['review']
        cached = deepcopy(record)
        before = self.store.path.read_bytes()
        with patch.object(client.transport,'exchange',side_effect=NodeError('DISCONNECTED','Observer disconnected')):
            with self.assertRaises(NodeError) as caught:
                client.call('review.poll', {'review_id':uuid4().hex}, self.target)
        self.assertEqual(caught.exception.code, 'DISCONNECTED')
        self.assertEqual(record, cached)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot()['jobs'][0]['status'], 'Hold')

    def test_endpoint_close_cancels_observer_only(self):
        entered, _ = self.paused_worker()
        first = self.call('review.start', {'options':{}}, request_id=uuid4().hex)
        self.assertTrue(entered.wait(1))
        task = self.backend._review_service._tasks[first.review_id]
        before = self.store.path.read_bytes()
        self.backend.close()
        self.assertTrue(task['cancel'].is_set())
        self.assertFalse(task['thread'].is_alive())
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
