"""Strict study capability negotiation, bounded owner evidence and write guards."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.study_models import StudyResult
from cmw.nodes.codec import CodecError, decode_typed, encode_typed
from cmw.nodes.connections import ConnectedBackend
from cmw.nodes.protocol import NodeError, is_mutation, validate_envelope
from tests.nodes import test_backend
from tests.nodes.test_review import DirectTransport


class StudyNodeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_backend.BackendObservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend, self.store = self.fixture.backend, self.fixture.store
        self.job = self.fixture.job()
        self.target = self.backend.target(self.job)
        self.study_id, self.digest = uuid4().hex, 'a'*64
        patcher = patch('cmw.jobs.study.StudyService')
        self.factory = patcher.start(); self.addCleanup(patcher.stop)
        self.service = self.factory.return_value
        for action, status in (('plan','READY'),('materialize','MATERIALIZED'),('register','REGISTERED'),('compare','COMPLETE'),('view','COMPLETE')):
            getattr(self.service,action).return_value = StudyResult(1,self.target['node_id'],self.target['queue_id'],
                self.study_id,action,status,self.digest,json.dumps({'cases':[],'coverage':'synthetic interface only'}))
        self.service.view_for_job.return_value = replace(self.service.view.return_value,
            payload_json=json.dumps({'selected_target':self.target,'cases':[]}))

    def client(self, *, readonly=True, transform=None):
        entry = {'transport':'local','config_root':str(self.fixture.root/'node-config'),
            'node_id':self.target['node_id'],'host_id':self.fixture.config['host_id'],
            'runtime_id':self.fixture.config['runtime']['fingerprint'],
            'queues':{'research':self.target['queue_id']},'default_queue':'research','read_only':readonly}
        client = ConnectedBackend('fixture',entry,transport=DirectTransport(self.backend,transform))
        self.addCleanup(client.close)
        return client

    def test_plan_view_compare_are_owner_local_and_read_only(self):
        client = self.client(); before = self.store.path.read_bytes()
        path = '/owner only/不存在/manifest.json'
        result = decode_typed(client.call('study.plan',{'manifest_path':path})['result']['study'],StudyResult)
        self.assertEqual(result,self.service.plan.return_value)
        self.service.plan.assert_called_once_with(path)
        client.call('study.view',{'record_path':'/owner/study.json'})
        client.call('study.compare',{'record_path':'/owner/study.json','selection_path':'/owner/selection.json'})
        self.service.compare.assert_called_once_with('/owner/study.json','/owner/selection.json',export_directory=None)
        self.assertEqual(before,self.store.path.read_bytes())
        self.assertTrue(all(not is_mutation('study.'+action) for action in ('plan','view','compare')))

    def test_selected_view_uses_exact_attempt_not_mutable_series_or_paths(self):
        client = self.client()
        response = client.call('study.view',target=self.target)
        self.service.view_for_job.assert_called_once_with(self.job['id'],expected_attempt_id=self.job['attempt_id'])
        self.assertEqual(json.loads(response['result']['study']['value']['payload_json'])['selected_target'],self.target)
        for key in ('node_id','queue_id','attempt_id'):
            with self.subTest(key=key),self.assertRaises(ValueError):
                client.call('study.view',target={**self.target,key:uuid4().hex})

    def test_publication_registration_and_export_require_write_guard(self):
        actions = [('study.materialize',{'manifest_path':'/owner/m','approved_digest':self.digest}),
            ('study.register',{'record_path':'/owner/r','approved_digest':self.digest}),
            ('study.compare',{'record_path':'/owner/r','selection_path':'/owner/s','export_directory':'/owner/new'})]
        for operation,params in actions:
            self.assertTrue(is_mutation(operation,params))
            with self.assertRaises(NodeError) as caught:
                self.backend.call(operation,params,request_id=self.study_id)
            self.assertEqual(caught.exception.code,'ENDPOINT_CHANGED')
            with self.assertRaises(NodeError) as caught:
                self.client().call(operation,params,request_id=self.study_id)
            self.assertEqual(caught.exception.code,'READ_ONLY')
        client = self.client(readonly=False)
        for operation,params in actions:
            client.call(operation,params,request_id=self.study_id)
        self.service.register.assert_called_once_with('/owner/r',self.digest,self.study_id)
        self.service.compare.assert_called_once_with('/owner/r','/owner/s',export_directory='/owner/new')

    def test_old_malformed_capability_refuses_before_study_operation(self):
        from cmw.nodes.study import CAPABILITY
        for capability in (None,{}, {**CAPABILITY,'schema':True},{**CAPABILITY,'max_cases':True},
                           {**CAPABILITY,'max_cases':65},{**CAPABILITY,'extra':'future'}):
            def transform(operation,value):
                if operation=='capabilities': value['result']['convergence_study']=capability
                return value
            client = self.client(transform=transform)
            with self.subTest(capability=capability),self.assertRaises(NodeError) as caught:
                client.call('study.plan',{'manifest_path':'/owner/m'})
            self.assertEqual(caught.exception.code,'UNSUPPORTED_STUDY')
            self.assertEqual(client.transport.calls,['capabilities'])
        self.service.plan.assert_not_called()

    def test_invalid_params_and_wrong_target_never_reach_service(self):
        client = self.client(readonly=False)
        for params in ({},{'manifest_path':'relative'},{'manifest_path':False},{'manifest_path':'/owner/m','guess':True}):
            with self.assertRaises(ValueError): client.call('study.plan',params)
        with self.assertRaises(NodeError): client.call('study.plan',{'manifest_path':'/owner/m'},self.target)
        with self.assertRaises(NodeError): client.call('study.view',{'record_path':'/owner/r'},self.target)
        for digest in (False,'a'*63,'g'*64):
            with self.assertRaises(NodeError):
                client.call('study.materialize',{'manifest_path':'/owner/m','approved_digest':digest})
        self.service.plan.assert_not_called(); self.service.materialize.assert_not_called()

    def test_strict_codec_owner_operation_and_selected_attempt(self):
        encoded = encode_typed(self.service.plan.return_value)
        for key,value in (('schema',True),('study_id','name'),('payload_json','[]'),('payload_json','{"x":1,"x":2}'),('payload_json','{"x":NaN}')):
            changed=deepcopy(encoded);changed['value'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(CodecError):decode_typed(changed,StudyResult)
        changed=deepcopy(encoded);changed['value']['scientific_pass']=True
        with self.assertRaises(CodecError):decode_typed(changed,StudyResult)
        envelope=self.client().call('study.plan',{'manifest_path':'/owner/m'})
        for key,value in (('node_id',uuid4().hex),('operation','view')):
            changed=deepcopy(envelope);changed['result']['study']['value'][key]=value
            with self.assertRaises(NodeError):validate_envelope(changed,'study.plan')
        self.service.view_for_job.return_value = replace(self.service.view.return_value,
            payload_json=json.dumps({'selected_target':{**self.target,'attempt_id':uuid4().hex}}))
        with self.assertRaisesRegex(NodeError,'immutable attempt'):self.client().call('study.view',target=self.target)

    def test_publication_response_is_bound_to_approval_and_request(self):
        def transform(operation,value):
            if operation=='study.materialize':value['result']['study']['value']['plan_digest']='b'*64
            return value
        with self.assertRaisesRegex(NodeError,'misbound'):
            self.client(readonly=False,transform=transform).call('study.materialize',
                {'manifest_path':'/owner/m','approved_digest':self.digest})
        # A malformed reply deliberately closes its observation session.
        self.backend=self.fixture.make_backend()
        self.service.register.return_value=replace(self.service.register.return_value,study_id=uuid4().hex)
        with self.assertRaisesRegex(NodeError,'durable study identity'):
            self.client(readonly=False).call('study.register',
                {'record_path':'/owner/r','approved_digest':self.digest},request_id=self.study_id)

    def test_payload_limit_and_nonfinite_are_rejected(self):
        base=self.service.plan.return_value
        for payload in ('{"value":"'+('x'*(256*1024))+'"}', '{"nested":[Infinity]}',
                        '{"nested":'+('['*1100)+'0'+(']'*1100)+'}'):
            with self.assertRaises(ValueError):replace(base,payload_json=payload)
