"""Durable fetch recovery against isolated local capture APIs, without endpoints."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.core.preparation_publication import _rename_exclusive
from cmw.core.provenance import stable_hash
from cmw.nodes.client import fetch
from cmw.nodes.fetch import prepare_fetch, complete_fetch
from cmw.nodes.protocol import NodeError
from tests.nodes import test_fetch as fixtures
from tests.periodic.vasp.handoff_case import wait_for_fixture_sources


class FetchClientTests(unittest.TestCase):
    def setUp(self):
        fixtures.FetchTests.setUp(self)
        wait_for_fixture_sources([self.source])
        self.target = self.backend.target(self.job)
        self.frontend = self.root / 'frontend'
        owner = self
        class LocalClient:
            node_id, queue_id = owner.target['node_id'], owner.target['queue_id']
            connection = {'transport': 'local'}
            last_envelope = None
            calls = []
            lost_prepares = 0
            lost_acks = 0
            ack_io_error = False
            rewrite = None
            def call(self, operation, params=None, target=None, *, request_id=None):
                self.calls.append(operation)
                if operation == 'fetch.prepare':
                    intent = owner.frontend / 'requests' / request_id / 'request.json'
                    owner.assertTrue(intent.is_file(), 'Durable client intent must precede mutation')
                    result = prepare_fetch(owner.backend, owner.job, params['spec'], request_id)
                    if self.lost_prepares:
                        self.lost_prepares -= 1
                        raise NodeError('OUTCOME_UNKNOWN', 'synthetic lost capture reply')
                    if self.rewrite:
                        result = self.rewrite(deepcopy(result))
                elif operation == 'fetch.complete':
                    owner.assertTrue(owner.destination.is_dir())
                    state = json.loads((owner.frontend / 'requests' / params['fetch_id'] / 'fetch.json').read_text())
                    owner.assertIsNotNone(state['publication'])
                    owner.assertEqual(state['result']['status'], 'FETCHED')
                    result = complete_fetch(owner.backend, owner.job, params['fetch_id'], params['manifest_digest'])
                    if self.ack_io_error:
                        raise OSError('synthetic local acknowledgment I/O failure')
                    if self.lost_acks:
                        self.lost_acks -= 1
                        raise NodeError('OUTCOME_UNKNOWN', 'synthetic lost acknowledgment')
                else:
                    owner.assertEqual(operation, 'capabilities')
                    result = {}
                self.last_envelope = {'schema': 1, 'node_id': self.node_id, 'queue_id': self.queue_id, 'result': result}
                return self.last_envelope
        self.client = LocalClient()

    def run_fetch(self, **options):
        return fetch(self.client, self.target, self.spec, self.destination,
                     root=self.frontend, request_id=self.fetch_id, **options)

    def test_existing_destination_and_invalid_spec_refuse_before_remote_capture(self):
        self.destination.mkdir()
        (self.destination / 'user-work').write_bytes(b'preserve')
        with self.assertRaisesRegex(NodeError, self.fetch_id):
            self.run_fetch()
        self.assertEqual(self.client.calls, [])
        self.assertFalse((self.node_root / 'fetches').exists())
        self.assertEqual((self.destination / 'user-work').read_bytes(), b'preserve')
        with self.assertRaises(NodeError):
            fetch(self.client, self.target, {**self.spec, 'schema': True}, self.root / 'other',
                  root=self.frontend, request_id=uuid4().hex)
        self.assertEqual(self.client.calls, [])

    def test_relative_destination_is_normalized_before_record_and_copy(self):
        before = Path.cwd()
        try:
            os.chdir(self.root)
            result = fetch(self.client, self.target, self.spec, Path(self.destination.name),
                           root=self.frontend, request_id=self.fetch_id)['result']
        finally:
            os.chdir(before)
        self.assertEqual(result['destination'], str(self.destination))
        self.assertFalse(result['acknowledgment_pending'])
        intent = json.loads((self.frontend / 'requests' / self.fetch_id / 'request.json').read_text())
        self.assertEqual(intent['params']['destination'], str(self.destination))

    def test_wrong_target_and_self_consistent_wrong_selection_refuse(self):
        wrong = {**self.target, 'attempt_id': uuid4().hex}
        self.client.rewrite = lambda value: {**value, 'target': wrong}
        with self.assertRaises(NodeError):
            self.run_fetch()
        self.assertFalse(self.destination.exists())
        def changed(value):
            value['manifest']['purpose'] = 'analysis'
            value['manifest_digest'] = stable_hash(value['manifest'])
            return value
        self.client.rewrite = changed
        with self.assertRaisesRegex(NodeError, 'reviewed normalized selection'):
            self.run_fetch()
        self.assertFalse(self.destination.exists())
        self.assertNotIn('fetch.complete', self.client.calls)

    def test_capture_reply_loss_retries_same_id_and_retains_fetch_specific_guidance(self):
        self.client.lost_prepares = 1
        with self.assertRaises(NodeError) as raised:
            self.run_fetch()
        self.assertIn(self.fetch_id, str(raised.exception))
        self.assertIn('separate from Jobs request lookup', str(raised.exception))
        result = self.run_fetch()['result']
        self.assertEqual(result['status'], 'FETCHED')
        self.assertFalse(result['acknowledgment_pending'])
        self.assertEqual(self.client.calls.count('fetch.prepare'), 2)
        self.assertEqual(len(list((self.node_root / 'fetches').glob(self.fetch_id + '.json'))), 1)

    def test_lost_ack_keeps_verified_replica_and_retry_only_acknowledges(self):
        self.client.lost_acks = 1
        result = self.run_fetch()['result']
        self.assertEqual(result['status'], 'FETCHED')
        self.assertTrue(result['acknowledgment_pending'])
        self.assertIn(self.fetch_id, result['acknowledgment_error']['message'])
        identity = self.destination.stat().st_ino
        self.client.calls.clear()
        with patch('cmw.nodes.fetch.download_local_fetch', side_effect=AssertionError('Must not copy again')):
            recovered = self.run_fetch()['result']
        self.assertEqual(self.client.calls, ['fetch.complete'])
        self.assertFalse(recovered['acknowledgment_pending'])
        self.assertEqual(self.destination.stat().st_ino, identity)
        self.client.calls.clear()
        self.assertFalse(self.run_fetch()['result']['acknowledgment_pending'])
        self.assertEqual(self.client.calls, [])

    def test_acknowledgment_io_failure_still_reports_published_replica(self):
        self.client.ack_io_error = True
        result = self.run_fetch()['result']
        self.assertEqual(result['status'], 'FETCHED')
        self.assertTrue(result['acknowledgment_pending'])
        self.assertEqual(result['acknowledgment_error']['code'], 'FETCH_ACK_IO_ERROR')
        self.assertTrue((self.destination / 'fetch-manifest.json').is_file())

    def test_crash_after_rename_before_result_record_recovers_owned_directory(self):
        def lost(source, destination):
            _rename_exclusive(source, destination)
            raise NodeError('OUTCOME_UNKNOWN', 'synthetic crash after local rename')
        with patch('cmw.nodes.fetch._rename_exclusive', side_effect=lost), self.assertRaises(NodeError):
            self.run_fetch()
        self.assertTrue(self.destination.is_dir())
        state = json.loads((self.frontend / 'requests' / self.fetch_id / 'fetch.json').read_text())
        self.assertIsNotNone(state['publication'])
        self.assertIsNone(state['result'])
        self.client.calls.clear()
        with patch('cmw.nodes.fetch.download_local_fetch', side_effect=AssertionError('Must recover without copy')):
            self.assertFalse(self.run_fetch()['result']['acknowledgment_pending'])
        self.assertEqual(self.client.calls, ['fetch.complete'])

    def test_identical_copied_destination_is_not_adopted_on_retry(self):
        self.client.lost_acks = 1
        self.run_fetch()
        retained = self.root / 'retained-original'
        self.destination.rename(retained)
        shutil.copytree(retained, self.destination)
        self.client.calls.clear()
        with self.assertRaisesRegex(NodeError, 'recorded owned replica'):
            self.run_fetch()
        self.assertEqual(self.client.calls, [])
        self.assertEqual((retained / 'analysis/summary.txt').read_bytes(), self.source.read_bytes())

    def test_changed_or_extra_replica_bytes_refuse_without_acknowledgment(self):
        self.client.lost_acks = 1
        self.run_fetch()
        (self.destination / 'analysis/summary.txt').write_bytes(b'mutated')
        self.client.calls.clear()
        with self.assertRaises(NodeError):
            self.run_fetch()
        self.assertEqual(self.client.calls, [])
        (self.destination / 'analysis/summary.txt').write_bytes(self.source.read_bytes())
        (self.destination / 'unselected').write_bytes(b'preserve')
        with self.assertRaises(NodeError):
            self.run_fetch()
        self.assertEqual((self.destination / 'unselected').read_bytes(), b'preserve')

    def test_interrupted_download_reuses_captured_receipt_without_recapture(self):
        with patch('cmw.nodes.fetch.download_local_fetch', side_effect=NodeError('TRANSFER_INTERRUPTED', 'fixture interruption')):
            with self.assertRaises(NodeError):
                self.run_fetch()
        state_path = self.frontend / 'requests' / self.fetch_id / 'fetch.json'
        self.assertTrue(state_path.is_file())
        self.client.calls.clear()
        self.assertFalse(self.run_fetch()['result']['acknowledgment_pending'])
        self.assertEqual(self.client.calls, ['capabilities', 'fetch.complete'])
