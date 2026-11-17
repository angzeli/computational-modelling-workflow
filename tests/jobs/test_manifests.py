"""Native job manifests, Phase 1a (#41): owner-only allocation and job.json."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs import manifests
from cmw.jobs.manifests import ManifestError
from cmw.jobs.store import JobsError, Store
from tests.jobs.test_catalogue_cli import run_jobs

ROOT = Path(__file__).resolve().parents[2]
try:
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
except ImportError:  # Schema conformance runs where the schema-tests extra is installed.
    Draft202012Validator = None


def schema_errors(name, value):
    schemas = {path.name: json.loads(path.read_text()) for path in (ROOT / 'schemas').glob('*.v1.json')}
    registry = Registry().with_resources((schema['$id'], Resource.from_contents(schema)) for schema in schemas.values())
    validator = Draft202012Validator(schemas[name], registry=registry, format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(value)]


def synthetic_potcar(*elements):
    # Invented metadata only; never a licensed dataset.
    return ''.join(f'TITEL = FAKE {element}\nVRHFIN = {element}: fake\nsynthetic\nEnd of Dataset\n'
                   for element in elements)


def _reserve_many(arguments):
    state, root, count = arguments
    return [manifests.reserve(Store(state), root)['id'] for _ in range(count)]


class ManifestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-manifests-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.store = Store(self.base / 'state')
        self.node = uuid4().hex
        self.queue = self.store.enroll_queue(self.node, 'linux:synthetic-only')
        self.project = self.base / 'project'
        self.project.mkdir()

    def init(self, inventory=None):
        return manifests.init_project(self.store, self.project, 'demo', inventory=inventory,
                                      no_legacy=inventory is None)

    def vasp_job(self, *, restart='none'):
        directory = Path(manifests.reserve(self.store, self.project)['job_directory'])
        inputs = directory / 'inputs'
        inputs.mkdir()
        (inputs / 'INCAR').write_text('ENCUT = 400\n')
        (inputs / 'POSCAR').write_text('Si\n1.0\n5 0 0\n0 5 0\n0 0 5\nSi\n2\nDirect\n0 0 0\n0.25 0.25 0.25\n')
        (inputs / 'KPOINTS').write_text('Gamma\n0\nGamma\n1 1 1\n')
        (inputs / 'POTCAR').write_text(synthetic_potcar('Si'))
        if restart != 'none':
            (inputs / 'WAVECAR').write_bytes(b'synthetic checkpoint bytes')
        argv = ['/unexecuted/run-vasp.sh', '--input', str(inputs), '--output', str(directory / 'run'),
                '--binary', 'std', '--ranks', '2', '--ncore', '1', '--kpar', '1', '--mpi-mode', 'synthetic',
                '--restart', restart, '--timeout', '10', '--stop-before', '0', '--managed-foreground']
        return directory, argv

    def add(self, directory, argv, **options):
        return manifests.add_native(self.store, self.project, cwd=directory, argv=argv, engine='VASP',
                                    name='synthetic', mpi_ranks=2, env={'OMP_NUM_THREADS': '1', 'SECRET': 'x'},
                                    **options)


class ProjectAndLedgerTests(ManifestCase):
    def test_init_publishes_schema_valid_project_once(self):
        result = self.init()
        project = json.loads((self.project / '.cmw-project.json').read_text())
        self.assertEqual((project['owner_node_id'], result['first_id']), (self.node, 'J1'))
        if Draft202012Validator is not None:
            self.assertEqual(schema_errors('project.v1.json', project), [])
        with self.assertRaisesRegex(ManifestError, 'already initialised'):
            self.init()

    def test_unenrolled_queue_cannot_own_a_project(self):
        with self.assertRaisesRegex(ManifestError, 'enrolled to a node UUID'):
            manifests.init_project(Store(self.base / 'other'), self.project, 'demo', no_legacy=True)
        self.assertFalse((self.project / '.cmw').exists())

    def test_inventory_counter_starts_above_every_legacy_id_and_label(self):
        inventory = {'schema_version': 1, 'project_key': 'demo', 'complete': True,
                     'names': ['J4.1', 'J27.1', 'J30', 'relaxed slab', 'M11.1'], 'reviewed_other_names': ['M11.1']}
        self.assertEqual(self.init(inventory)['first_id'], 'J31')
        self.assertEqual(manifests.reserve(self.store, self.project)['id'], 'J31')

    def test_inventory_must_be_complete_and_fully_accounted(self):
        base = {'schema_version': 1, 'project_key': 'demo', 'complete': True, 'names': ['J2.1'],
                'reviewed_other_names': []}
        for change, message in (({'complete': False}, 'not marked complete'),
                                ({'names': ['J2.1', 'M11.1']}, 'reviewed_other_names'),
                                ({'project_key': 'other'}, 'project key')):
            with self.subTest(change=change), self.assertRaisesRegex(ManifestError, message):
                self.init({**base, **change})
        self.assertFalse((self.project / '.cmw-project.json').exists())

    def test_null_inventory_never_counts_as_no_legacy_ids(self):
        for options in ({}, {'inventory': None}, {'inventory': {}, 'no_legacy': True}):
            with self.subTest(options=options), self.assertRaisesRegex(ManifestError, 'exactly one'):
                manifests.init_project(self.store, self.project, 'demo', **options)
        inventory = self.base / 'null.json'
        inventory.write_text('null\n')
        code, output = run_jobs(['manifest', 'init', '--project-root', str(self.project), '--project-key', 'demo',
                                 '--legacy-inventory', str(inventory)], self.base / 'state')
        self.assertEqual(code, 2, output)
        self.assertFalse((self.project / '.cmw-project.json').exists())

    def test_reservations_are_idempotent_and_never_reused(self):
        self.init()
        request = uuid4().hex
        first = manifests.reserve(self.store, self.project, request=request)
        again = manifests.reserve(self.store, self.project, request=request)
        self.assertEqual((first['id'], again['id'], again['new_reservation']), ('J1', 'J1', False))
        Path(first['job_directory']).rmdir()  # Deleting the directory never frees the ID.
        self.assertEqual(manifests.reserve(self.store, self.project)['id'], 'J2')

    def test_concurrent_reservations_receive_distinct_ids(self):
        self.init()
        context = multiprocessing.get_context('spawn')
        with context.Pool(4) as pool:
            batches = pool.map(_reserve_many, [(self.base / 'state', self.project, 5)] * 4)
        ids = [item for batch in batches for item in batch]
        self.assertEqual(sorted(ids, key=lambda value: int(value[1:])), [f'J{n}' for n in range(1, 21)])

    def test_non_owner_node_refuses_reservation_and_creation(self):
        self.init()
        other = Store(self.base / 'other-state')
        other.enroll_queue(uuid4().hex, 'linux:synthetic-only')
        with self.assertRaisesRegex(ManifestError, 'does not own'):
            manifests.reserve(other, self.project)
        directory, argv = self.vasp_job()
        with self.assertRaisesRegex(ManifestError, 'does not own'):
            manifests.add_native(other, self.project, cwd=directory, argv=argv, engine='VASP', name='x')


class JobManifestTests(ManifestCase):
    def setUp(self):
        super().setUp()
        self.init()

    def journal(self):
        return manifests.read_journal(self.project)

    def test_native_vasp_job_is_frozen_anchored_and_registered_on_hold(self):
        directory, argv = self.vasp_job()
        job = self.add(directory, argv)
        path = directory / '.cmw-manifest' / 'job.json'
        raw = path.read_bytes()
        manifest = json.loads(raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
        self.assertEqual((manifest['id'], manifest['authoring_node'], manifest['target_node']), ('J1', self.node, self.node))
        self.assertEqual(manifest['inputs']['INCAR'], hashlib.sha256(b'ENCUT = 400\n').hexdigest())
        self.assertEqual([(item['element'], item['block_index']) for item in manifest['potcar']], [('Si', 0)])
        self.assertEqual(manifest['launch']['environment'], {'OMP_NUM_THREADS': '1'})
        self.assertEqual((manifest['parent'], manifest['restart_inputs']), (None, {}))
        if Draft202012Validator is not None:
            self.assertEqual(schema_errors('job.v1.json', manifest), [])
        digest = hashlib.sha256(raw).hexdigest()
        events = self.journal()
        self.assertEqual([event['event'] for event in events], ['manifest', 'registration'])
        self.assertEqual({event['payload']['job_sha256'] for event in events}, {digest})
        self.assertEqual(events[1]['payload']['binding'],
                         {'node_id': self.node, 'queue_id': self.queue['queue_id'],
                          'job_id': job['id'], 'attempt_id': job['attempt_id']})
        if Draft202012Validator is not None:
            for event in events:
                self.assertEqual(schema_errors('integrity.v1.json', event), [])
        self.assertEqual(job['status'], 'Hold')
        self.assertEqual(job['manifest'], {'project': 'demo', 'id': 'J1', 'root': str(self.project), 'job_sha256': digest})
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ['.cmw-manifest', 'inputs'])

    def test_restart_checkpoints_are_hashed_synchronously(self):
        directory, argv = self.vasp_job(restart='wavecar')
        self.add(directory, argv)
        manifest = json.loads((directory / '.cmw-manifest' / 'job.json').read_text())
        self.assertEqual(manifest['restart_inputs'], {'WAVECAR': hashlib.sha256(b'synthetic checkpoint bytes').hexdigest()})
        self.assertIsNone(manifest['parent'])

    def test_failed_registration_burns_the_published_id(self):
        directory, argv = self.vasp_job()
        with patch.object(Store, 'add', side_effect=JobsError('synthetic registration failure')):
            with self.assertRaisesRegex(JobsError, 'synthetic registration failure'):
                self.add(directory, argv)
        with self.assertRaisesRegex(ManifestError, 'burned'):
            self.add(directory, argv)
        self.assertEqual([event['event'] for event in self.journal()], ['manifest'])
        self.assertEqual(self.store.snapshot()['jobs'], [])
        self.assertEqual(manifests.reserve(self.store, self.project)['id'], 'J2')

    def test_registered_id_cannot_be_registered_twice(self):
        directory, argv = self.vasp_job()
        self.add(directory, argv)
        with self.assertRaisesRegex(ManifestError, 'already registered'):
            self.add(directory, argv)

    def test_refuses_unreserved_directories_and_foreign_input_paths(self):
        unreserved = self.project / 'jobs' / 'J9'
        unreserved.mkdir(parents=True)
        with self.assertRaisesRegex(ManifestError, 'not reserved'):
            self.add(unreserved, ['/bin/true'])
        directory, argv = self.vasp_job()
        argv[argv.index('--output') + 1] = str(self.base / 'elsewhere')
        with self.assertRaisesRegex(ManifestError, '--output'):
            self.add(directory, argv)
        self.assertFalse((directory / '.cmw-manifest' / 'job.json').exists())

    def test_symlinked_sidecar_directory_is_refused(self):
        directory, argv = self.vasp_job()
        outside = self.base / 'outside'
        outside.mkdir()
        (directory / '.cmw-manifest').symlink_to(outside)
        with self.assertRaisesRegex(ManifestError, 'real directory'):
            self.add(directory, argv)
        self.assertEqual((list(outside.iterdir()), manifests.read_journal(self.project)), ([], []))

    def test_incomplete_vasp_inputs_refuse_before_publication(self):
        directory, argv = self.vasp_job()
        (directory / 'inputs' / 'POTCAR').write_text(synthetic_potcar('O'))
        with self.assertRaisesRegex(ManifestError, 'not complete and valid'):
            self.add(directory, argv)
        self.assertFalse((directory / '.cmw-manifest' / 'job.json').exists())

    def test_plain_add_inside_project_jobs_tree_is_refused(self):
        directory = Path(manifests.reserve(self.store, self.project)['job_directory'])
        with self.assertRaisesRegex(JobsError, 'manifest-enabled'):
            self.store.add(argv=['/bin/true'], cwd=directory, name='bypass')

    def test_corrupt_journal_tail_blocks_further_writes(self):
        directory, argv = self.vasp_job()
        with (self.project / '.cmw' / 'integrity.jsonl').open('ab') as stream:
            stream.write(b'{"partial":')
        with self.assertRaisesRegex(ManifestError, 'incomplete tail'):
            self.add(directory, argv)
        self.assertEqual(self.store.snapshot()['jobs'], [])

    def test_journal_retries_are_idempotent_and_conflicts_refuse(self):
        request = uuid4().hex
        payload = {'id': 'J1', 'job_sha256': '0' * 64, 'binding': None}
        first = manifests.append_journal(self.project, 'demo', 'manifest', request, payload)
        self.assertEqual(manifests.append_journal(self.project, 'demo', 'manifest', request, payload), first)
        with self.assertRaisesRegex(ManifestError, 'different manifest'):
            manifests.append_journal(self.project, 'demo', 'manifest', request, {**payload, 'job_sha256': '1' * 64})
        self.assertEqual(len(self.journal()), 1)


class ManifestCliTests(ManifestCase):
    def test_init_reserve_and_add_through_the_cli(self):
        state = self.base / 'state'
        code, output = run_jobs(['manifest', 'init', '--project-root', str(self.project), '--project-key', 'demo',
                                 '--no-legacy-ids', '--json'], state)
        self.assertEqual(code, 0, output)
        code, output = run_jobs(['manifest', 'reserve', '--project-root', str(self.project), '--json'], state)
        self.assertEqual((code, json.loads(output)['id']), (0, 'J1'), output)
        directory = self.project / 'jobs' / 'J1'
        code, output = run_jobs(['add', '--name', 'cmd', '--manifest-project', str(self.project),
                                 '--cwd', str(directory), '--json', '--', '/bin/true'], state)
        self.assertEqual(code, 0, output)
        job = json.loads(output)
        self.assertEqual((job['status'], job['manifest']['id']), ('Hold', 'J1'))
        manifest = json.loads((directory / '.cmw-manifest' / 'job.json').read_text())
        self.assertEqual((manifest['engine'], manifest['potcar'], manifest['inputs']['INCAR']), ('Command', None, None))

    def test_remote_manifest_operations_are_refused_without_contacting_a_node(self):
        code, output = run_jobs(['--node', 'mini', 'manifest', 'reserve', '--project-root', str(self.project)])
        self.assertEqual(code, 2)
        self.assertIn('owner-local only', output)
        code, output = run_jobs(['add', '--name', 'x', '--cwd', str(self.project), '--manifest-request',
                                 uuid4().hex, '--', '/bin/true'], self.base / 'state')
        self.assertEqual(code, 2)
        self.assertIn('requires --manifest-project', output)


if __name__ == '__main__':
    unittest.main()
