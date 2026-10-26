"""Human lineage intent and bounded artifact-record links, never science status."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.core.artifacts import ArtifactValidation, EnergyArtifact, ValidationStatus
from cmw.core.provenance import stable_hash
from cmw.jobs.catalogue import CatalogueStore
from cmw.jobs.catalogue_models import CatalogueQuery
from cmw.jobs.catalogue_relations import RelationStore, RelationPage, RelationRecord, verify_record_link
from cmw.jobs.store import JobsError, Store


class CatalogueRelationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cmw-catalogue-relations-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root / 'state')
        self.identity = self.store.enroll_queue(uuid4().hex, 'synthetic-host')
        self.parent = self.store.add(argv=['/unexecuted/synthetic'], cwd=self.root, hold=True, name='same name')
        self.child = self.store.add(argv=['/unexecuted/synthetic'], cwd=self.root, hold=True, name='same name')
        self.relations = RelationStore(self.store)

    def target(self, job):
        return dict(node_id=self.identity['node_id'], queue_id=self.identity['queue_id'],
                    job_id=job['id'], attempt_id=job['attempt_id'])

    def add(self, **kwargs):
        args = dict(parent=self.target(self.parent), kind='continuation',
                    expected_revision=0, expected_attempt_id=self.child['attempt_id'])
        args.update(kwargs)
        return self.relations.add(self.child['id'], **args)

    def records(self):
        """Synthetic *record metadata* built using the existing Artifact contract."""
        parent_art = EnergyArtifact('synthetic-parent', protocol={'operational_attempt_id':self.parent['attempt_id']},
                                    validation=ArtifactValidation(ValidationStatus.PASSED))
        child_art = EnergyArtifact('synthetic-child', protocol={'operational_attempt_id':self.child['attempt_id']},
                                   parent_artifacts=(parent_art.artifact_id,), validation=ArtifactValidation(ValidationStatus.PASSED))
        def record(job, artifact, name):
            path = self.root / name
            value = {'schema_version':1, 'kind':'vasp-artifact-finalization',
                     'producing_calculation':artifact.producing_calculation,
                     'artifact_bundle':{'status':'FINALIZED','artifact_count':1,'artifacts':[artifact.to_dict()]},
                     'evidence':{'binding':{'execution':{'job':{'id':job['id'],'attempt_id':job['attempt_id']},
                                                       'binding':{'queue_identity':self.identity}}}}}
            value['record_id'] = stable_hash(value)
            value['publication'] = {'state':'complete','record_path':str(path)}
            path.write_text(json.dumps(value))
            return str(path)
        return dict(parent_record=record(self.parent,parent_art,'parent.json'),
                    child_record=record(self.child,child_art,'child.json'),
                    parent_artifact_id=parent_art.artifact_id,child_artifact_id=child_art.artifact_id)

    def test_read_does_not_backfill_and_unenrolled_intent_is_refused(self):
        before = self.store.path.read_bytes()
        page = self.relations.list(self.child['id'])
        self.assertEqual(page.relations, ())
        self.assertEqual(self.store.path.read_bytes(), before)
        other = Store(self.root/'legacy')
        job = other.add(argv=['/not/run'],cwd=self.root,hold=True,name='legacy')
        before = other.path.read_bytes()
        self.assertEqual(RelationStore(other).list(job['id']).coverage,'LOCAL_UNENROLLED')
        with self.assertRaisesRegex(JobsError,'enrollment'):
            RelationStore(other).add(job['id'],parent=self.target(self.parent),kind='repeat',
                                    expected_attempt_id=job['attempt_id'],expected_revision=0)
        self.assertEqual(other.path.read_bytes(), before)

    def test_intent_retraction_retains_history_and_request_replay(self):
        original_jobs = self.store.snapshot()['jobs']
        request = uuid4().hex
        page = self.add(request_id=request)
        self.assertEqual(page.revision,1)
        self.assertEqual(self.add(request_id=request),page)
        relation = page.relations[0]
        self.assertEqual(relation.basis,'intent')
        self.assertIn('does not satisfy',relation.qualification)
        with self.assertRaises(JobsError):
            self.add(request_id=request,kind='repeat')
        result = self.relations.retract(self.child['id'],relation.relation_id,
            expected_attempt_id=self.child['attempt_id'],expected_revision=1)
        self.assertEqual(result.revision,2)
        self.assertEqual(result.relations[0].state,'RETRACTED')
        self.assertEqual(result.relations[0].created_at,relation.created_at)
        self.assertEqual(self.store.snapshot()['jobs'],original_jobs)

    def test_metadata_cas_and_query_cursor_share_relationship_edits(self):
        catalogue = CatalogueStore(self.store)
        cursor = catalogue.query(CatalogueQuery(limit=1)).next_cursor
        self.add()
        with self.assertRaisesRegex(JobsError,'changed'):
            catalogue.edit(self.child['id'], {'project':'later'},expected_attempt_id=self.child['attempt_id'],expected_revision=0)
        with self.assertRaisesRegex(JobsError,'cursor invalidated'):
            catalogue.query(CatalogueQuery(limit=1,cursor=cursor))

    def test_self_wrong_attempt_missing_parent_duplicate_and_cycle_refuse(self):
        for parent in (self.target(self.child), {**self.target(self.parent),'attempt_id':uuid4().hex},
                       {**self.target(self.parent),'job_id':999}):
            with self.assertRaises(JobsError): self.add(parent=parent)
        self.add()
        with self.assertRaisesRegex(JobsError,'already exists'): self.add(expected_revision=1)
        with self.assertRaisesRegex(JobsError,'cycle'):
            self.relations.add(self.parent['id'],parent=self.target(self.child),kind='next-stage',
                               expected_attempt_id=self.parent['attempt_id'],expected_revision=0)

    def test_remote_parent_is_unresolved_without_opening_any_remote_path(self):
        remote = {**self.target(self.parent),'node_id':uuid4().hex,'queue_id':uuid4().hex}
        with patch('cmw.jobs.catalogue_relations._read_record',side_effect=AssertionError('No remote file access')):
            page = self.add(parent=remote)
            self.assertEqual(page.relations[0].resolution,'UNRESOLVED_REMOTE')
            with self.assertRaisesRegex(JobsError,'Cross-owner'):
                self.add(parent=remote,kind='repeat',expected_revision=1,evidence={
                    'parent_record':'/remote/no','child_record':'/remote/no',
                    'parent_artifact_id':'a'*64,'child_artifact_id':'b'*64})

    def test_artifact_record_link_is_explicit_bounded_and_never_science_pass(self):
        evidence = self.records()
        jobs = self.store.snapshot()['jobs']
        page = self.add(evidence=evidence)
        item = page.relations[0]
        self.assertEqual(item.basis,'artifact-record')
        self.assertEqual(item.evidence['scope'],'recorded-artifact-parent-edge')
        self.assertIn('NOT assessed',item.qualification)
        self.assertEqual(self.store.snapshot()['jobs'],jobs)
        with patch('cmw.jobs.catalogue_relations._read_record',side_effect=AssertionError('No rehash on list')):
            self.assertEqual(self.relations.list(self.child['id']).relations,page.relations)

    def test_mismatched_artifacts_and_identical_content_distinct_attempts_do_not_join(self):
        evidence = self.records()
        with self.assertRaisesRegex(JobsError,'absent or ambiguous'):
            verify_record_link({**evidence,'parent_artifact_id':'0'*64},self.target(self.parent),self.target(self.child))
        foreign = {**self.target(self.parent),'attempt_id':uuid4().hex}
        with self.assertRaisesRegex(JobsError,'exact global attempt'):
            verify_record_link(evidence,foreign,self.target(self.child))
        child_path = Path(evidence['child_record'])
        data = json.loads(child_path.read_text())
        artifact = EnergyArtifact('synthetic-child',protocol={'operational_attempt_id':self.child['attempt_id']},
                                  validation=ArtifactValidation(ValidationStatus.PASSED))
        data['artifact_bundle']['artifacts']=[artifact.to_dict()]
        data['record_id']=stable_hash({k:v for k,v in data.items() if k not in {'record_id','publication'}})
        child_path.write_text(json.dumps(data))
        with self.assertRaisesRegex(JobsError,'does not declare'):
            verify_record_link({**evidence,'child_artifact_id':artifact.artifact_id},self.target(self.parent),self.target(self.child))

    def test_changed_copied_oversized_or_symlink_record_cannot_establish_link(self):
        evidence = self.records()
        parent = Path(evidence['parent_record'])
        raw = parent.read_bytes()
        copy = self.root/'copy.json'; copy.write_bytes(raw)
        with self.assertRaises(JobsError): self.add(evidence={**evidence,'parent_record':str(copy)})
        alias = self.root/'alias.json'; alias.symlink_to(parent)
        with self.assertRaises(JobsError): self.add(evidence={**evidence,'parent_record':str(alias)})
        parent.write_bytes(b' '*(2*1024*1024+1))
        with self.assertRaisesRegex(JobsError,'2 MiB'): self.add(evidence=evidence)
        parent.write_bytes(raw.replace(b'synthetic-parent',b'changed-parent'))
        with self.assertRaises(JobsError): self.add(evidence=evidence)
        self.assertEqual(self.relations.list(self.child['id']).relations,())

    def test_strict_models_reject_unqualified_claims(self):
        page=self.add()
        record=asdict(page.relations[0])
        for changed in ({'qualification':'PASS'}, {'basis':'scientifically-verified'}, {'parent_json':'{}'}):
            with self.assertRaises(ValueError): RelationRecord(**{**record,**changed})
        with self.assertRaises(ValueError):
            RelationPage(**{**asdict(page),'relations':page.relations,'schema':2})
