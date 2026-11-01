"""Actual owner service over an unconverged synthetic ORCA-route terminal job."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from cmw.jobs import runtime
from cmw.jobs.molecular import launch_blocker
from cmw.jobs.successor import SuccessorService, launch_blocker as successor_launch_blocker
from tests.jobs.successor_orca_fixtures import make_orca_fixture, orca_successor_case


class SuccessorOrcaServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-orca-successor-service-')
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture = make_orca_fixture(Path(temporary.name)/'owner')

    def setUp(self):
        self.store = self.fixture['store']
        self.parent = self.fixture['job']
        self.service = SuccessorService(self.store)
        self.spec,self.path = orca_successor_case(self.fixture,uuid4().hex)

    def plan(self):
        return self.service.plan(self.parent['id'],str(self.path),self.parent['attempt_id'])

    def test_real_native_receipt_supports_unconverged_complete_geometry_read_only(self):
        database = self.store.path.read_bytes()
        original = {p:p.read_bytes() for p in self.fixture['run'].iterdir() if p.is_file()}
        first,second = self.plan(),self.plan()
        self.assertEqual(first.status,'READY',first.payload_json)
        self.assertEqual(first.plan_digest,second.plan_digest)
        payload = json.loads(first.payload_json)
        self.assertTrue(payload['closure']['closed'])
        self.assertEqual(payload['adapter']['source_qualification'],'COMPLETE_OBSERVED_GEOMETRY_NOT_OPTIMIZED')
        self.assertEqual(self.parent['status'],'Fail')
        self.assertEqual(self.fixture['scientific_record']['scientific']['status'],'NOT_CONVERGED')
        self.assertTrue(self.fixture['receipt'].is_file())
        self.assertEqual(database,self.store.path.read_bytes())
        self.assertEqual(original,{p:p.read_bytes() for p in original})
        self.assertFalse(Path(self.spec['adapter']['output']).exists())
        self.assertEqual(self.fixture['cleanup']['remaining_owned_processes'],[])

    def test_actual_materialize_register_recovery_is_one_held_new_attempt(self):
        before = self.store.snapshot()
        parent_bytes = {p:p.read_bytes() for p in self.fixture['run'].iterdir() if p.is_file()}
        plan = self.plan()
        self.assertEqual(plan.status,'READY',plan.payload_json)
        result = self.service.materialize(self.parent['id'],str(self.path),plan.plan_digest,self.parent['attempt_id'])
        self.assertEqual(result.status,'MATERIALIZED',result.payload_json)
        record = json.loads(result.payload_json)['record_path']
        self.assertEqual(before['jobs'],self.store.snapshot()['jobs'])
        request = uuid4().hex
        first = self.service.register(self.parent['id'],record,plan.plan_digest,request,self.parent['attempt_id'])
        replay = SuccessorService(self.store).register(self.parent['id'],record,plan.plan_digest,request,self.parent['attempt_id'])
        a,b = json.loads(first.payload_json),json.loads(replay.payload_json)
        self.assertEqual(a['child'],b['child'])
        self.assertFalse(a['replayed']);self.assertTrue(b['replayed'])
        jobs = self.store.snapshot()['jobs']
        self.assertEqual(len(jobs),len(before['jobs'])+1)
        child = next(j for j in jobs if j['id']==a['child']['job_id'])
        self.assertEqual(child['status'],'Hold')
        self.assertNotEqual(child['attempt_id'],self.parent['attempt_id'])
        self.assertIsNone(launch_blocker(child,final=True))
        self.assertIsNone(successor_launch_blocker(child))
        self.assertFalse(runtime.launch_blocker(child,final=True))
        self.assertFalse(Path(child['molecular_submission']['plan']['result_path']).exists())
        self.assertFalse(Path(child['cwd'],'stage.out').exists())
        self.assertFalse(list(Path(self.spec['adapter']['output']).rglob('*.gbw')))
        self.assertFalse(self.store.snapshot()['controller']['online'])
        self.assertEqual(parent_bytes,{p:p.read_bytes() for p in parent_bytes})


if __name__ == '__main__':
    unittest.main()
