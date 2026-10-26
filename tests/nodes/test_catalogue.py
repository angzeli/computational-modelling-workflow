"""Strict catalogue endpoint identities, requests and independent federation."""
from copy import deepcopy
import json
from dataclasses import replace
import threading
import unittest
from uuid import uuid4

from cmw.jobs.catalogue import CatalogueStore
from cmw.jobs.catalogue_models import CatalogueQuery, CataloguePage, CatalogueRecord
from cmw.jobs.catalogue_relations import RelationPage, RelationRecord, RECORD_SCOPE
from cmw.jobs.store import JobsError
from cmw.nodes.catalogue import federated_query
from cmw.nodes.codec import CodecError, encode_typed, decode_typed
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.protocol import NodeError, encode, validate_envelope
from tests.nodes import test_backend
from tests.nodes.test_review import DirectTransport


class CatalogueNodeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_backend.BackendObservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend, self.store = self.fixture.backend, self.fixture.store
        self.job = self.fixture.job()
        self.target = self.backend.target(self.job)

    def client(self, *, readonly=True, transform=None, fixture=None, alias='fixture'):
        fixture = fixture or self.fixture
        entry = {'transport': 'local', 'config_root': str(fixture.root/'node-config'),
            'node_id': fixture.node_id, 'host_id': fixture.config['host_id'],
            'runtime_id': fixture.config['runtime']['fingerprint'],
            'queues': {'research': fixture.config['queues']['research']['queue_id']},
            'default_queue': 'research', 'read_only': readonly}
        client = ConnectedBackend(alias, entry, transport=DirectTransport(fixture.backend, transform))
        self.addCleanup(client.close)
        return client

    def test_query_is_same_core_contract_read_only_bounded_and_strict(self):
        self.store.annotate(self.job['id'], '繁體 [literal].* $(never) ' + '字'*4000)
        self.fixture.job()
        client = self.client()
        before = self.store.path.read_bytes()
        query = CatalogueQuery(text='[literal].*', limit=1)
        response = client.call('catalogue.query', {'query': encode_typed(query)})
        page = decode_typed(response['result']['page'], CataloguePage)
        local = CatalogueStore(self.store).query(query)
        self.assertEqual(replace(page, observed_at=local.observed_at), local)
        self.assertEqual((page.total_count, page.matching_count), (2, 1))
        self.assertLessEqual(len(page.records[0].note), 512)
        self.assertEqual(page.records[0].target, self.target)
        self.assertLess(len(encode(response)), 2*1024*1024)
        self.assertEqual(before, self.store.path.read_bytes())
        for params in ({}, {'query': {}}, {'query': encode_typed(query), 'regex': True}):
            with self.subTest(params=params), self.assertRaises((NodeError, ValueError)):
                client.call('catalogue.query', params)

    def test_edits_require_owner_guard_cas_and_idempotent_request(self):
        params = {'patch': {'project': 'ZIS', 'tags': ['comparison']}, 'expected_revision': 0}
        with self.assertRaises(NodeError) as caught:
            self.backend.call('catalogue.edit', params, self.target, request_id=uuid4().hex)
        self.assertEqual(caught.exception.code, 'ENDPOINT_CHANGED')
        readonly = self.client()
        with self.assertRaises(NodeError) as caught:
            readonly.call('catalogue.edit', params, self.target)
        self.assertEqual(caught.exception.code, 'READ_ONLY')
        client = self.client(readonly=False)
        request_id = uuid4().hex
        first = client.call('catalogue.edit', params, self.target, request_id=request_id)
        again = client.call('catalogue.edit', params, self.target, request_id=request_id)
        self.assertEqual(first['result'], again['result'])
        row = decode_typed(first['result']['record'], CatalogueRecord)
        self.assertEqual((row.project, row.revision), ('ZIS', 1))
        with self.assertRaises(JobsError):
            client.call('catalogue.edit', {'patch': {'project': 'changed'}, 'expected_revision': 0},
                        self.target, request_id=request_id)
        with self.assertRaises(JobsError):
            client.call('catalogue.edit', params, self.target, request_id=uuid4().hex)
        current = self.store.snapshot()['jobs'][0]
        self.assertEqual((current['name'], current['status'], current['order']), ('Fixture', 'Hold', 1))

    def test_old_endpoint_declines_before_catalogue_operation(self):
        for capability in (None, {}, {'schema': True, 'query': 'CatalogueQuery', 'page': 'CataloguePage', 'record': 'CatalogueRecord'}):
            def transform(operation, value):
                if operation == 'capabilities':
                    value['result']['research_catalogue'] = capability
                return value
            client = self.client(transform=transform)
            with self.subTest(capability=capability), self.assertRaises(NodeError) as caught:
                client.call('catalogue.show', target=self.target)
            self.assertEqual(caught.exception.code, 'UNSUPPORTED_CATALOGUE')
            self.assertEqual(client.transport.calls, ['capabilities'])

    def test_strict_models_and_response_attempt_binding(self):
        client = self.client()
        envelope = client.call('catalogue.show', target=self.target)
        record = envelope['result']['record']
        for change in ('unknown', 'missing', 'boolean_revision'):
            bad = deepcopy(record)
            if change == 'unknown': bad['value']['scientific_pass'] = True
            elif change == 'missing': del bad['value']['project']
            else: bad['value']['revision'] = True
            with self.assertRaises(CodecError):
                decode_typed(bad, CatalogueRecord)
        foreign = deepcopy(envelope)
        foreign['result']['record']['value']['node_id'] = uuid4().hex
        with self.assertRaises(NodeError): validate_envelope(foreign, 'catalogue.show')
        for key in ('node_id', 'queue_id', 'attempt_id'):
            with self.subTest(key=key), self.assertRaises((NodeError, JobsError)):
                client.call('catalogue.show', target={**self.target, key: uuid4().hex})
        def retarget(operation, value):
            if operation == 'catalogue.show':
                value['result']['record']['value']['attempt_id'] = uuid4().hex
            return value
        bad_client = self.client(transform=retarget)
        with self.assertRaisesRegex(NodeError, 'immutable attempt'):
            bad_client.call('catalogue.show', target=self.target)

    def test_global_internal_target_does_not_resolve_numeric_display_alias(self):
        other = self.fixture.job()
        with self.store.transaction() as con:
            value = self.store.get(con, other['id'])
            value['display_id'] = str(self.job['id'])
            self.store.save(con, value)
        row = decode_typed(self.client().call('catalogue.show', target=self.target)['result']['record'], CatalogueRecord)
        self.assertEqual(row.attempt_id, self.job['attempt_id'])

    def test_relation_owner_mutation_replay_and_remote_parent_unresolved(self):
        client = self.client(readonly=False)
        parent = {'node_id': uuid4().hex, 'queue_id': uuid4().hex, 'job_id': 1, 'attempt_id': uuid4().hex}
        params = {'parent': parent, 'kind': 'continuation', 'expected_revision': 0}
        request_id = uuid4().hex
        response = client.call('catalogue.relation.add', params, self.target, request_id=request_id)
        page = decode_typed(response['result']['relations'], RelationPage)
        self.assertEqual(page.relations[0].resolution, 'UNRESOLVED_REMOTE')
        self.assertEqual(page.relations[0].basis, 'intent')
        self.assertEqual(client.call('catalogue.relation.add', params, self.target, request_id=request_id)['result'], response['result'])
        listed = decode_typed(client.call('catalogue.relations', target=self.target)['result']['relations'], RelationPage)
        self.assertEqual(listed.relations, page.relations)
        withdrawn = client.call('catalogue.relation.retract', {'relation_id': page.relations[0].relation_id,
            'expected_revision': page.revision}, self.target, request_id=uuid4().hex)
        self.assertEqual(decode_typed(withdrawn['result']['relations'], RelationPage).relations[0].state, 'RETRACTED')

    def test_federation_has_per_owner_cursors_duplicate_ids_and_partial_coverage(self):
        other = test_backend.BackendObservationTests(); other.setUp(); self.addCleanup(other.doCleanups)
        other.job(); other.job(); self.fixture.job()
        one, two = self.client(), self.client(fixture=other, alias='other')
        backends = {'one': one, 'two': two}
        first = federated_query(backends, CatalogueQuery(limit=1))
        self.assertFalse(first['partial'])
        self.assertIn('not an atomic', first['consistency'])
        self.assertEqual([row['page']['records'][0]['job_id'] for row in first['owners']], [1, 1])
        self.assertNotEqual(first['owners'][0]['node_id'], first['owners'][1]['node_id'])
        cursors = {row['alias']: row['page']['next_cursor'] for row in first['owners']}
        second = federated_query(backends, CatalogueQuery(limit=1), cursors=cursors)
        self.assertEqual([row['page']['records'][0]['job_id'] for row in second['owners']], [2, 2])
        def disconnected(*args, **kwargs): raise NodeError('DISCONNECTED', 'Synthetic endpoint unavailable')
        two.call = disconnected
        partial = federated_query(backends, CatalogueQuery())
        self.assertTrue(partial['partial'])
        self.assertEqual(partial['owners'][1]['coverage'], 'UNAVAILABLE')
        self.assertIsNone(partial['owners'][1]['page'])
        cancelled = threading.Event(); cancelled.set()
        self.assertTrue(all(row['coverage'] == 'CANCELLED' for row in federated_query(backends, CatalogueQuery(), cancel=cancelled)['owners']))
        with self.assertRaises(NodeError): federated_query(backends, CatalogueQuery(), cursors={'unknown': 'bad'})

    def test_relationship_wire_page_is_bounded_before_export(self):
        parent = {**self.target, 'attempt_id': uuid4().hex, 'job_id': 2}
        evidence = {'parent_record': '/' + '\\'*2400, 'child_record': '/' + '\\'*2400,
            'parent_artifact_id': 'a'*64, 'child_artifact_id': 'b'*64,
            'parent_record_sha256': 'c'*64, 'child_record_sha256': 'd'*64,
            'verified_at': 10., 'scope': 'recorded-artifact-parent-edge'}
        record = RelationRecord(uuid4().hex, 'continuation', json.dumps(parent), 'artifact-record',
            'LOCAL_PRESENT', 'ACTIVE', 10., None, json.dumps(evidence), RECORD_SCOPE)
        rows = tuple(replace(record, relation_id=uuid4().hex) for _ in range(100))
        with self.assertRaisesRegex(ValueError, 'bound'):
            RelationPage(1, self.target['node_id'], self.target['queue_id'], self.job['id'],
                         self.job['attempt_id'], 100, rows, 'COMPLETE', 10.)
        page = RelationPage(1, self.target['node_id'], self.target['queue_id'], self.job['id'],
                            self.job['attempt_id'], 20, rows[:20], 'COMPLETE', 10.)
        self.assertEqual(decode_typed(encode_typed(page), RelationPage), page)
        self.assertLess(len(encode({'relations': encode_typed(page)})), 2*1024*1024)

    def test_status_summary_keeps_blockers_outside_page(self):
        other = self.fixture.job()
        with self.store.transaction() as con:
            value = self.store.get(con, other['id']); value['status'] = 'Unknown'; self.store.save(con, value)
        envelope = self.backend.call('status', {'limit': 1})
        value = envelope['result']
        self.assertEqual(value['job_count'], 2)
        self.assertEqual(value['status_counts'], {'Hold': 1, 'Unknown': 1})
        self.assertEqual(len(value['state']['jobs']), 1)
        validate_envelope(envelope, 'status')
        value['status_counts']['Unknown'] = -1
        with self.assertRaises(NodeError):
            validate_envelope(envelope, 'status')
