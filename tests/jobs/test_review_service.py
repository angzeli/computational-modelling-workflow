"""Bounded actual observer subprocesses over genuine benign worker evidence."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.core.provenance import stable_hash
from cmw.jobs.review import ReviewService, from_dict, options
from cmw.jobs.review_models import cached_review
from cmw.jobs.store import JobsError
from cmw.periodic.vasp.results import inspect_result
from tests.periodic.vasp.test_result_execution_native import run_fixture
from tests.periodic.vasp.test_result_policy import policy


class ReviewServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='cmw-review-service-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.store, cls.snapshot, cls.receipt, cls.run_directory = run_fixture(cls.root/'native')
        cls.job = cls.snapshot['jobs'][0]

    def setUp(self):
        self.service = ReviewService(self.store)
        self.addCleanup(self.service.close)
        self.original = self.store.path.read_bytes()

    def tearDown(self):
        self.assertEqual(self.original,self.store.path.read_bytes())

    def wait(self,record):
        deadline=time.monotonic()+40
        while record.status=='PENDING' and time.monotonic()<deadline:
            time.sleep(.05)
            record=self.service.poll(record.job_id,record.review_id,record.attempt_id)
        self.assertNotEqual(record.status,'PENDING')
        return record

    def start(self,settings=None):
        return self.wait(self.service.start(self.job['id'],settings or {},uuid4().hex,self.job['attempt_id']))

    def policy_spec(self):
        root=self.root/uuid4().hex;root.mkdir()
        snapshot=root/'snapshot.json';snapshot.write_text(json.dumps(self.store.snapshot()))
        evidence=inspect_result(self.run_directory);selected=policy()
        pp=root/'policy.json';pp.write_text(json.dumps(selected))
        spec={'schema_version':1,'intent':'retrospective','producing_calculation':'synthetic result review',
            'snapshot_id':evidence['snapshot_id'],'segment_id':evidence['selected_segment']['segment_id'],
            'policy_id':stable_hash(selected),'source_identities':{s['role']:s['sha256'] for s in evidence['sources']},
            'artifact_roles':selected['artifact_roles'],'execution':{'snapshot':str(snapshot),'receipt':str(self.receipt),
            'job_id':self.job['id'],'attempt_id':self.job['attempt_id'],'source_identities':{
            'jobs_snapshot':hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            'jobs_receipt':hashlib.sha256(self.receipt.read_bytes()).hexdigest()}}}
        sp=root/'spec.json';sp.write_text(json.dumps(spec))
        return {'policy_path':str(pp),'spec_path':str(sp)},spec

    def test_preview_is_metadata_only_and_default_inspection_has_no_policy_pass(self):
        with patch('subprocess.Popen',side_effect=AssertionError('preview spawned work')):
            before=self.service.preview(self.job['id'])
        self.assertEqual(before.status,'PREVIEW')
        record=self.start()
        self.assertEqual(record.status,'COMPLETE',record.message)
        domains={d.name:d.status for d in record.domains}
        self.assertEqual(domains['operational'],'PASS')
        self.assertEqual(domains['policy'],'NOT ASSESSED')
        self.assertEqual(domains['ionic_analysis'],'N/A')
        self.assertEqual(from_dict(record.to_dict()),record)
        with patch('hashlib.sha256',side_effect=AssertionError('cache rehashed')):
            cached=self.service.preview(self.job['id'])
        self.assertEqual(cached.status,'CACHED')
        self.assertNotIn('PASS',[d.status for d in cached.domains])
        self.assertEqual(cached_review(record).status,'CACHED')

    def test_explicit_policy_export_and_source_mutation_invalidate_reuse(self):
        settings,_=self.policy_spec(); destination=Path(settings['policy_path']).parent/'new-review.json'
        record=self.start(settings|{'export_path':str(destination)})
        self.assertEqual(record.status,'COMPLETE',record.message)
        self.assertEqual(next(d.status for d in record.domains if d.name=='policy'),'PASS')
        self.assertTrue(destination.exists())
        saved=destination.read_bytes()
        again=self.start(settings|{'export_path':str(destination)})
        self.assertEqual(again.status,'ERROR')
        self.assertEqual(saved,destination.read_bytes())
        source=self.run_directory/'OUTCAR';original=source.read_bytes()
        try:
            source.write_bytes(original+b'\nchanged after assessment\n')
            # The last successful review's original source metadata cannot be reused.
            from cmw.jobs.review import source_changed
            self.assertTrue(source_changed(record))
            self.assertEqual(self.service.poll(record.job_id,record.review_id,record.attempt_id).status,'STALE')
            stale=self.start(settings)
            self.assertNotEqual(next(d.status for d in stale.domains if d.name=='policy'),'PASS')
        finally:
            source.write_bytes(original)

    def test_wrong_selected_spec_and_export_inside_calculation_refused(self):
        settings,spec=self.policy_spec();spec['execution']['attempt_id']='a'*32
        Path(settings['spec_path']).write_text(json.dumps(spec))
        self.assertEqual(self.start(settings).status,'ERROR')
        output=self.run_directory/'forbidden-review.json'
        self.assertEqual(self.start({'export_path':str(output)}).status,'ERROR')
        self.assertFalse(output.exists())

    def test_request_replay_conflict_and_foreign_handle(self):
        request=uuid4().hex
        first=self.service.start(self.job['id'],{},request,self.job['attempt_id'])
        replay=self.service.start(self.job['id'],{},request,self.job['attempt_id'])
        self.assertEqual(first.review_id,replay.review_id)
        with self.assertRaises(JobsError):self.service.start(self.job['id'],{'max_bytes':1},request)
        with self.assertRaises(JobsError):self.service.poll(self.job['id'],'foreign')
        with self.assertRaises(JobsError):self.service.preview(self.job['id'],'b'*32)
        complete=self.wait(first)
        replay=self.service.start(self.job['id'],{},request,self.job['attempt_id'])
        self.assertEqual(replay.status,'CACHED')
        self.assertNotIn('PASS',[d.status for d in replay.domains])

    def test_deadline_and_cancel_kill_only_owned_observer(self):
        real=subprocess.Popen; children=[]
        def slow(command,**kwargs):
            child=real([sys.executable,'-c','import time; time.sleep(30)'],**kwargs)
            children.append(child);return child
        with patch('cmw.jobs.review.subprocess.Popen',side_effect=slow):
            record=self.start({'timeout_seconds':1})
            self.assertEqual(record.status,'LIMIT')
            pending=self.service.start(self.job['id'],{},uuid4().hex)
            self.service.cancel(pending.job_id,pending.review_id,pending.attempt_id)
            self.assertEqual(self.wait(pending).status,'CANCELLED')
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_partial_inspection_is_qualified_and_strict_options(self):
        record=self.start({'max_bytes':100})
        self.assertEqual(record.status,'COMPLETE',record.message)
        self.assertNotEqual(next(d.status for d in record.domains if d.name=='electronic'),'PASS')
        for bad in ({'unknown':1},{'max_bytes':True},{'timeout_seconds':121},{'policy_path':'relative'}):
            with self.assertRaises(JobsError):options(bad)

    def test_donor_input_directory_is_protected_and_binary_sources_reverify(self):
        from cmw.jobs.review_engines import _Sources
        from cmw.jobs.review_worker import _export
        donor=self.root/uuid4().hex;donor.mkdir()
        source=donor/'wavefunction.bin';source.write_bytes(b'\x00\xff\xfe'*20)
        observed=_Sources(1024,None).observe(source)
        observed['scientific_input']=True
        record=replace(self.service.preview(self.job['id']),status='COMPLETE',sources_json=json.dumps([observed]))
        with self.assertRaises(JobsError):_export(record,donor/'review.json',[])
        self.assertFalse((donor/'review.json').exists())
        destination=self.root/(uuid4().hex+'.json')
        self.assertEqual(_export(record,destination,[]).status,'COMPLETE')
        self.assertTrue(destination.exists())
