"""Native manifests for remote targets (#41 Phase 1b) over real local endpoints.

The owner frontend reserves the ID and freezes job.json; the target verifies it
against its own plan, publishes it under <storage>/<project>/jobs/J<n>/ and
registers on Hold. Same-host protocol tests; nothing is executed.
"""
import hashlib
import json
from pathlib import Path
import stat
import unittest
from uuid import uuid4

from cmw.jobs import manifests
from cmw.jobs.store import JobsError, Store
from cmw.nodes.client import submit as client_submit
from cmw.nodes.protocol import NodeError
from cmw.nodes.submission import normalize_spec
from tests.jobs.test_manifests import Draft202012Validator, schema_errors
from tests.nodes import test_submission as base


class NativeSubmissionTests(unittest.TestCase):
    # Reuse the harness helpers without collecting the base class's tests here.
    _base = base.SubmissionEndpointTests
    setUp, node, connect, store, prepare, publish = (_base.setUp, _base.node, _base.connect, _base.store,
                                                     _base.prepare, _base.publish)

    def owner(self):
        store = Store(self.root / 'owner-state')
        identity = store.enroll_queue(uuid4().hex, 'linux:synthetic-only')
        project = self.root / 'project'
        project.mkdir()
        manifests.init_project(store, project, 'demo', no_legacy=True)
        return store, identity, project

    def frontend(self, node):
        spec, prepared, selections = self.prepare(node)
        frontend = {key: value for key, value in spec.items() if key != 'manifest'}
        frontend['files'] = selections
        return frontend, spec, prepared

    def test_owner_reserves_freezes_and_target_registers_the_same_bytes(self):
        node = self.node()
        backend = node['backends']['research']
        owner_store, owner_identity, project = self.owner()
        frontend, _, _ = self.frontend(node)
        client_root = self.root / 'frontend'
        result = client_submit(backend, frontend, root=client_root, manifest_project=project, local_store=owner_store)
        job = result['result']['job']
        work = node['storage'] / 'demo' / 'jobs' / 'J1'
        self.assertEqual((job['status'], job['cwd'], job['manifest']['id']), ('Hold', str(work), 'J1'))
        target_raw = (work / '.cmw-manifest' / 'job.json').read_bytes()
        owner_raw = (project / 'jobs' / 'J1' / '.cmw-manifest' / 'job.json').read_bytes()
        self.assertEqual(target_raw, owner_raw)
        self.assertEqual(stat.S_IMODE((work / '.cmw-manifest' / 'job.json').stat().st_mode), 0o444)
        manifest = json.loads(target_raw)
        self.assertEqual((manifest['authoring_node'], manifest['target_node'], manifest['id_owner_node']),
                         (owner_identity['node_id'], node['config']['node_id'], owner_identity['node_id']))
        self.assertEqual(manifest['launch']['argv'], job['argv'])
        if Draft202012Validator is not None:
            self.assertEqual(schema_errors('job.v1.json', manifest), [])
        self.assertEqual((node['storage'] / 'demo' / '.cmw-project.json').read_bytes(),
                         (project / '.cmw-project.json').read_bytes())
        self.assertFalse((node['storage'] / 'demo' / '.cmw').exists())  # A replica has no ledger.
        events = manifests.read_journal(project)
        self.assertEqual([event['event'] for event in events], ['manifest', 'registration'])
        self.assertEqual(events[1]['payload'], {
            'id': 'J1', 'job_sha256': hashlib.sha256(target_raw).hexdigest(),
            'binding': {'node_id': node['config']['node_id'], 'queue_id': frontend['queue_id'],
                        'job_id': job['id'], 'attempt_id': job['attempt_id']}})
        # A lost reply is retried with the same request: same ID, same attempt, no new receipts.
        retried = client_submit(backend, frontend, root=client_root, manifest_project=project, local_store=owner_store)
        self.assertTrue(retried['result']['replayed'])
        self.assertEqual(retried['result']['job']['attempt_id'], job['attempt_id'])
        self.assertEqual(len(manifests.read_journal(project)), 2)
        self.assertEqual(manifests.reserve(owner_store, project)['id'], 'J2')
        with self.assertRaisesRegex(JobsError, 'manifest-enabled'):
            self.store(node).add(argv=['/bin/true'], cwd=work, name='bypass')

    def test_non_owner_and_incapable_endpoints_refuse_before_reserving(self):
        node = self.node()
        backend = node['backends']['research']
        owner_store, _, project = self.owner()
        frontend, _, _ = self.frontend(node)
        other = Store(self.root / 'other-state')
        other.enroll_queue(uuid4().hex, 'linux:synthetic-only')
        with self.assertRaisesRegex(JobsError, 'does not own'):
            client_submit(backend, frontend, root=self.root / 'f1', manifest_project=project, local_store=other)

        class OldEndpoint:
            def __getattr__(self, name):
                return getattr(backend, name)

            def call(self, operation, *args, **kwargs):
                envelope = backend.call(operation, *args, **kwargs)
                if operation == 'capabilities':
                    envelope['result'].pop('native_manifests')
                return envelope
        with self.assertRaisesRegex(NodeError, 'cannot verify native job manifests'):
            client_submit(OldEndpoint(), frontend, root=self.root / 'f2', manifest_project=project,
                          local_store=owner_store)
        with self.assertRaisesRegex(NodeError, 'no preview'):
            client_submit(backend, frontend, root=self.root / 'f3', preview=True, manifest_project=project,
                          local_store=owner_store)
        self.assertEqual(manifests.reserve(owner_store, project)['id'], 'J1')
        self.assertEqual(self.store(node).snapshot()['jobs'], [])

    def test_target_refuses_drifted_or_missing_job_manifest_without_registering(self):
        node = self.node()
        backend = node['backends']['research']
        owner_store, _, project = self.owner()
        request = uuid4().hex
        owner = manifests.begin_remote(owner_store, project, request)
        spec, prepared, _ = self.prepare(node, request_id=request)
        spec = normalize_spec({**spec, 'native': owner['native']})
        plan = backend.call('submit.preview', {'spec': spec})['result']
        self.assertEqual(plan['cwd'], str(node['storage'] / 'demo' / 'jobs' / owner['id']))
        self.publish(node, spec, prepared)
        job = {**manifests.remote_fields(native=owner['native'], spec=spec, plan=plan, potcar=None,
                                         input_digests={item['path']: item['sha256'] for item in spec['manifest']['files']}),
               'created_at': '2026-11-17T12:00:00Z', 'authoring_node': owner['node_id']}
        drifted = {**job, 'launch': {**job['launch'], 'argv': ['/elsewhere/runner']}}
        for params, message in (({'spec': spec}, 'requires the job.json'),
                                ({'spec': spec, 'job_manifest': drifted}, 'refuses drift'),
                                ({'spec': {k: v for k, v in spec.items() if k != 'native'}, 'job_manifest': job},
                                 'requires a native submission')):
            with self.subTest(message=message), self.assertRaisesRegex(NodeError, message):
                backend.call('submit', params, request_id=spec['request_id'])
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        self.assertFalse((node['storage'] / 'demo' / 'jobs' / owner['id'] / '.cmw-manifest').exists())
        unrelated = node['storage'] / 'demo' / 'notes.txt'
        unrelated.parent.mkdir(mode=0o700)
        unrelated.write_text('an existing folder, not a project replica\n')
        with self.assertRaisesRegex(NodeError, 'cannot become a project replica'):
            backend.call('submit', {'spec': spec, 'job_manifest': job}, request_id=spec['request_id'])
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        unrelated.unlink()
        registered = backend.call('submit', {'spec': spec, 'job_manifest': job}, request_id=spec['request_id'])
        self.assertEqual(registered['result']['job']['manifest']['id'], owner['id'])


if __name__ == '__main__':
    unittest.main()
