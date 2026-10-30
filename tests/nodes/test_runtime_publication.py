"""Runtime-check boundaries around real disposable publication, without enrollment.

Runtime/volume readiness is injected; transfer files and managed Store are real.
These focused tests do not qualify native runtime collection or a stdio endpoint.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.nodes import submission
from cmw.nodes.protocol import NodeError
from cmw.nodes.storage import check_storage, identify_storage
from cmw.nodes.transfer import prepare_local_bundle, upload_local_bundle
from tests.nodes import test_backend as fixtures
from tests.nodes.test_storage import volume


class RuntimePublicationTests(unittest.TestCase):
    setUp = fixtures.BackendObservationTests.setUp
    make_backend = fixtures.BackendObservationTests.make_backend

    def call_mutation(self, backend, operation, params, request_id):
        guard = {'node_id': self.config['node_id'], 'queue_id': backend.queue['queue_id'],
                 'host_id': self.config['host_id'],
                 'runtime_id': self.config['runtime']['fingerprint'], 'clock_id': backend.clock_id}
        return backend.call(operation, {**params, '_endpoint': guard}, request_id=request_id)['result']

    def boundary(self, phase):
        storage = self.root / 'storage'
        storage.mkdir()
        self.config['storages']['scratch'] = identify_storage(storage, probe=volume)
        original_config = deepcopy(self.config)
        source = self.root / 'source.txt'
        source.write_bytes(b'explicit disposable input\n')
        request_id = uuid4().hex
        bundle = prepare_local_bundle(self.root / 'spool',
            [{'source': str(source), 'path': 'inputs/input.txt', 'role': 'input'}],
            node_id=self.node_id, queue_id=self.backend.queue['queue_id'])
        manifest = deepcopy(bundle['manifest'])
        params = {'storage': 'scratch', 'request_id': request_id,
                  'manifest_digest': bundle['manifest_digest']}
        with patch('cmw.nodes.transfer.check_storage',
                   side_effect=lambda value: check_storage(value, probe=volume)):
            receipt = self.call_mutation(self.backend, 'transfer.begin',
                {'storage': 'scratch', 'request_id': request_id, 'manifest': manifest}, request_id)
            upload_local_bundle(bundle['spool'], receipt)
            self.readiness['runtime_diagnostic'] = {
                'category': 'in_flight_change', 'phase': 'security-after-read',
                'path': '/synthetic/runtime/member.py'}
            original = submission.dispatch
            def after_publication(*args, **kwargs):
                result = original(*args, **kwargs)
                self.assertEqual(result['status'], 'PUBLISHED')
                self.readiness['runtime_ready'] = False
                return result
            if phase == 'before':
                self.readiness['runtime_ready'] = False
            with patch.object(submission, 'dispatch', side_effect=after_publication) as dispatch:
                with self.assertRaises(NodeError) as caught:
                    self.call_mutation(self.backend, 'transfer.publish', params, request_id)
                self.assertEqual(dispatch.call_count, 0 if phase == 'before' else 1)
            self.assertEqual(caught.exception.code, 'RUNTIME_MISMATCH')
            context = json.loads(str(caught.exception).split(' | ', 1)[1])
            self.assertEqual({key: context[key] for key in ('operation', 'operation_phase', 'phase', 'path')},
                {'operation': 'transfer.publish', 'operation_phase': phase,
                 'phase': 'security-after-read', 'path': '/synthetic/runtime/member.py'})
            self.assertEqual(context['request_id'], request_id)
            self.assertEqual(context['node_id'], self.node_id)
            self.assertEqual(context['queue_id'], self.backend.queue['queue_id'])
            self.assertEqual(context['dispatch_entered'], phase == 'after')
            self.assertEqual(context['outcome'], 'not_independently_confirmed' if phase == 'after' else 'not_dispatched')
            self.assertEqual(context['original_error_code'], 'RUNTIME_MISMATCH')
            self.assertIn('transfer.lookup', context['recovery'])
            self.assertLess(len(str(caught.exception)), 4096)
            self.assertEqual(self.backend.runtime_diagnostic['operation_phase'], phase)
            self.readiness['runtime_ready'] = True
            fresh = self.make_backend()
            transfer = fresh.call('transfer.lookup',
                {'storage': 'scratch', 'request_id': request_id})['result']['transfer']
            self.assertEqual(transfer['status'], 'INCOMING' if phase == 'before' else 'PUBLISHED')
            published_root = Path(receipt['published_root'])
            old_inode = published_root.stat().st_ino if published_root.exists() else None
            published = self.call_mutation(fresh, 'transfer.publish', params, request_id)
            inode = published_root.stat().st_ino
            if old_inode is not None:
                self.assertEqual(inode, old_inode)
            replayed = self.call_mutation(fresh, 'transfer.publish', params, request_id)
            self.assertEqual(published, replayed)
            self.assertEqual(published_root.stat().st_ino, inode)
            self.assertEqual([p.name for p in published_root.parent.iterdir()], [request_id])
            self.assertEqual((published_root / 'inputs/input.txt').read_bytes(), source.read_bytes())
            self.assertEqual(bundle['manifest'], manifest)
            self.assertEqual(self.config, original_config)
            self.assertEqual(self.store.snapshot()['jobs'], [])
            self.assertIsNone(self.store.request_lookup(request_id))

    def test_pre_operation_refusal_leaves_upload_unpublished_then_same_id_recovers(self):
        self.boundary('before')

    def test_post_operation_refusal_keeps_publication_and_same_id_replays(self):
        self.boundary('after')

    def test_other_mutations_do_not_invent_submission_request_lookup_authority(self):
        self.readiness['runtime_ready'] = False
        with self.assertRaises(NodeError) as caught:
            self.backend.call('fetch.prepare', {}, request_id=uuid4().hex)
        context = json.loads(str(caught.exception).split(' | ', 1)[1])
        self.assertNotIn('recovery', context)
        self.assertNotIn('request_id', context)
