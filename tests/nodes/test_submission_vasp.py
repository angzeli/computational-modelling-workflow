"""Held synthetic VASP handoff through real endpoints; no runner is executed."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import unittest
from uuid import uuid4

from cmw.nodes.identity import enroll
from cmw.nodes.protocol import NodeError
from cmw.nodes.transfer import prepare_local_bundle
from tests.nodes import test_submission as endpoint_fixture
from tests.periodic.vasp import test_runner_compatibility as runner_fixture
from tests.periodic.vasp.handoff_case import wait_for_fixture_sources
from tests.periodic.vasp.test_inputs import bundle


class VaspSubmissionTests(unittest.TestCase):
    # Reuse the real endpoint harness without inheriting and rerunning its tests.
    setUp = endpoint_fixture.SubmissionEndpointTests.setUp
    connect = endpoint_fixture.SubmissionEndpointTests.connect
    store = endpoint_fixture.SubmissionEndpointTests.store
    publish = endpoint_fixture.SubmissionEndpointTests.publish
    register = endpoint_fixture.SubmissionEndpointTests.register

    def node(self, *, max_ranks=8, expanded=False):
        fixture = runner_fixture.RunnerCompatibilityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if expanded:
            fixture.expanded_rank_contract()
            if os.environ.get('CMW_VASP_PORT'):
                fixture.runner = Path(os.environ['CMW_VASP_PORT']) / 'scripts/run-vasp.sh'
        root = self.root / 'mini'
        root.mkdir(mode=0o700)
        storage = root / 'calculation'
        storage.mkdir(mode=0o700)
        spec = {'queues': {'research': str(root / 'queue-research')},
                'storages': {'scratch': str(storage)},
                'profiles': {'vasp': {'engine': 'VASP', 'runner': str(fixture.runner),
                                     'max_cpus': max_ranks, 'max_ranks': max_ranks}}}
        config = enroll(root / 'node', spec)
        node = {'root': root, 'storage': storage, 'runner': fixture.runner,
                'config': config, 'backends': {}}
        node['backends']['research'] = self.connect(node)
        return node

    def prepare(self, node, *, invalid=False):
        request_id = uuid4().hex
        source = node['root'] / 'selected-inputs'
        source.mkdir()
        files = bundle()
        if invalid:
            files['POSCAR'] = b'Invalid synthetic structure' + bytes([10])
        selections = []
        for name, content in files.items():
            path = source / name
            path.write_bytes(content)
            selections.append({'source': str(path), 'path': 'inputs/' + name,
                               'role': 'potcar' if name == 'POTCAR' else 'input'})
        wait_for_fixture_sources(source.iterdir(), quiet_seconds=12, timeout=40)
        prepared = prepare_local_bundle(node['root'] / 'spool', selections,
            node_id=node['config']['node_id'], queue_id=node['config']['queues']['research']['queue_id'],
            potcar_authorized=True)
        spec = {'schema': 1, 'request_id': request_id, 'node_id': node['config']['node_id'],
            'queue_id': node['config']['queues']['research']['queue_id'], 'storage': 'scratch',
            'profile': 'vasp', 'engine': 'VASP', 'name': 'Invented VASP held fixture',
            'resources': {'cpus': 2, 'memory_gib': 1, 'mpi_ranks': 2, 'threads_per_rank': 1},
            'manifest': prepared['manifest'], 'intent': 'held', 'dependencies_complete': True,
            'vasp': {'ncore': 1, 'kpar': 1, 'mpi_mode': 'native', 'timeout': 10, 'stop_before': 0}}
        return spec, prepared, source

    def test_preview_before_transfer_and_held_registration_pin_destination_poscar(self):
        node = self.node()
        spec, prepared, source = self.prepare(node)
        backend = node['backends']['research']
        preview = backend.call('submit.preview', {'spec': spec})['result']
        self.assertEqual(preview['status'], 'PREVIEW')
        self.assertFalse(Path(preview['input_bundle']).exists())
        self.assertFalse(Path(preview['native_output']).exists())
        receipt = self.publish(node, spec, prepared)
        job = self.register(backend, spec)['job']
        self.assertEqual(job['status'], 'Hold')
        self.assertNotEqual(job['attempt_id'], spec['request_id'])
        self.assertEqual(job['node_submission']['node_id'], node['config']['node_id'])
        target = Path(receipt['published_root']) / 'inputs' / 'POSCAR'
        binding = job['progress_sources']['vasp_input']
        self.assertEqual(binding['attempt_id'], job['attempt_id'])
        self.assertEqual(binding['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(target.read_bytes(), (source / 'POSCAR').read_bytes())
        self.assertNotEqual(target.stat().st_ino, (source / 'POSCAR').stat().st_ino)
        self.assertIn(str(target.parent), job['argv'])
        self.assertNotIn(str(source), job['argv'])
        self.assertFalse(Path(preview['native_output']).exists())
        self.assertEqual({path.name for path in target.parent.iterdir()}, {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'})
        state = self.store(node).snapshot()
        self.assertFalse(state['controller']['online'])
        self.assertFalse(state['controller']['dispatch'])
        self.assertIsNone(state['jobs'][0]['worker'])
        self.assertFalse(any(node['root'].rglob('OUTCAR')))
        self.assertFalse(any(node['root'].rglob('RUN_METADATA.txt')))

    def test_explicit_sixteen_rank_preview_and_held_registration(self):
        node = self.node(max_ranks=16, expanded=True)
        spec, prepared, _ = self.prepare(node)
        spec['resources'].update(cpus=16, mpi_ranks=16)
        spec['vasp'].update(ncore=4, kpar=1)
        backend = node['backends']['research']
        preview = backend.call('submit.preview', {'spec': spec})['result']
        self.assertEqual(preview['resources'], spec['resources'])
        self.assertEqual(preview['argv'][preview['argv'].index('--ranks') + 1], '16')
        self.publish(node, spec, prepared)
        job = self.register(backend, spec)['job']
        self.assertEqual(job['status'], 'Hold')
        self.assertEqual(job['argv'], preview['argv'])
        self.assertEqual(job['node_submission']['node_id'], node['config']['node_id'])
        self.assertNotEqual(job['attempt_id'], spec['request_id'])
        self.assertIsNone(job['worker'])
        self.assertFalse(Path(preview['native_output']).exists())
        self.assertFalse(self.store(node).snapshot()['controller']['dispatch'])

    def test_sixteen_rank_profile_does_not_upgrade_old_runner(self):
        node = self.node(max_ranks=16)
        spec, _, _ = self.prepare(node)
        spec['resources'].update(cpus=16, mpi_ranks=16)
        with self.assertRaisesRegex(NodeError, 'expanded MPI rank contract'):
            node['backends']['research'].call('submit.preview', {'spec': spec})
        self.assertEqual(self.store(node).snapshot()['jobs'], [])

    def test_invalid_four_file_structure_never_registers(self):
        node = self.node()
        spec, prepared, _ = self.prepare(node, invalid=True)
        self.publish(node, spec, prepared)
        with self.assertRaisesRegex(NodeError, 'four-file checker'):
            self.register(node['backends']['research'], spec)
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        self.assertFalse(any(node['root'].rglob('OUTCAR')))

    def test_rank_limit_and_existing_native_output_fail_closed(self):
        node = self.node()
        spec, prepared, _ = self.prepare(node)
        backend = node['backends']['research']
        oversized = deepcopy(spec)
        oversized['resources'].update(cpus=16, mpi_ranks=16)
        with self.assertRaisesRegex(NodeError, 'pinned profile limits'):
            backend.call('submit.preview', {'spec': oversized})
        preview = backend.call('submit.preview', {'spec': spec})['result']
        self.publish(node, spec, prepared)
        native = Path(preview['native_output'])
        native.mkdir(parents=True)
        with self.assertRaises(NodeError):
            self.register(backend, spec)
        self.assertEqual(self.store(node).snapshot()['jobs'], [])
        self.assertTrue(native.is_dir())
