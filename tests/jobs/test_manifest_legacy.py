"""Legacy queue bindings and queue-qualified legacy IDs (#41 Phase 4a, #76)."""
import json
import os
from pathlib import Path
import platform
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs import manifest_check, manifest_legacy, manifests
from cmw.jobs.manifests import ManifestError
from cmw.jobs.store import Store
from tests.jobs.test_catalogue_cli import run_jobs
from tests.jobs.test_manifests import Draft202012Validator, ManifestCase, schema_errors

VOLUME = '11111111-2222-3333-4444-555555555555'


def volume(path):
    return VOLUME


class LegacyQueueTests(ManifestCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.legacy = Store(self.base / 'legacy')
        for number in (1, 2):
            self.legacy.add(argv=['/bin/true', f'job-{number}'], cwd=self.base, name=f'legacy-{number}',
                            env={'SECRET': 'x'})

    def bind(self, node='node-a', queue='research', state_root=None, **options):
        return manifest_legacy.bind_queue(self.store, self.project, node=node, queue=queue,
                                          state_root=state_root or self.legacy.root,
                                          volume_uuid_of=options.pop('volume_uuid_of', volume), **options)

    def resolve(self, reference, **options):
        return manifest_legacy.resolve(self.project, reference, volume_uuid_of=options.pop('volume_uuid_of', volume))

    def test_binding_is_recorded_once_and_resolves_queue_qualified_ids(self):
        def queue_files():
            return {path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in self.legacy.root.iterdir()}
        before = queue_files()
        request = uuid4().hex
        result = self.bind(request=request)
        database = (self.legacy.root / 'queue.sqlite3').stat()
        self.assertEqual(result['binding'], {
            'queue_id': None, 'state_root': str(self.legacy.root),
            'root_identity': [self.legacy.root.stat().st_dev, self.legacy.root.stat().st_ino],
            'database_identity': [database.st_dev, database.st_ino], 'volume_uuid': VOLUME})
        self.assertEqual(self.bind(request=request)['sequence'], result['sequence'])  # Idempotent retry.
        records = manifests.read_journal(self.project)
        self.assertEqual([record['event'] for record in records], ['queue_binding'])
        if Draft202012Validator is not None:
            self.assertEqual(schema_errors('integrity.v1.json', records[0]), [])
        resolved = self.resolve('node-a/research:J2.1')
        self.assertEqual((resolved['job']['id'], resolved['job']['display_id'], resolved['binding_sequence']),
                         (2, 'J2.1', result['sequence']))
        shown = manifest_legacy.summary(resolved)['job']
        self.assertEqual(shown['name'], 'legacy-2')
        self.assertNotIn('env', shown)
        self.assertNotIn('argv', shown)
        self.assertEqual(queue_files(), before)  # Binding and resolving never write the legacy queue.
        # The owner's own enrolled queue binds with its queue UUID.
        own = self.bind(queue='main', state_root=self.store.root)
        self.assertEqual(own['binding']['queue_id'], self.queue['queue_id'])
        checked = manifest_check.check(self.store, self.project)
        self.assertEqual((checked['status'], checked['jobs']), ('VERIFIED', []), checked)

    def test_rebinding_aliasing_and_unverifiable_bindings_are_refused(self):
        self.bind()
        other = Store(self.base / 'other')
        other.add(argv=['/bin/true'], cwd=self.base, name='other')
        cases = [
            ({}, 'already bound'),
            ({'queue': 'alias'}, 'already bound as node-a/research'),
            ({'node': 'node-b', 'queue': 'other', 'state_root': other.root}, "node token 'node-a'"),
            ({'queue': 'other', 'state_root': other.root, 'volume_uuid_of': lambda path: None},
             'volume UUID .* is unknown'),
            ({'queue': 'bad/token', 'state_root': other.root}, 'Queue token'),
            ({'queue': 'missing', 'state_root': self.base / 'missing'}, 'unavailable'),
        ]
        for options, message in cases:
            with self.subTest(options=options), self.assertRaisesRegex(ManifestError, message):
                self.bind(**options)
        stranger = Store(self.base / 'stranger')
        stranger.enroll_queue(uuid4().hex, 'linux:synthetic-only')
        with self.assertRaisesRegex(ManifestError, 'does not own'):
            manifest_legacy.bind_queue(stranger, self.project, node='node-a', queue='other',
                                       state_root=other.root, volume_uuid_of=volume)
        # The owner cannot claim a queue enrolled to another node as its own.
        with self.assertRaisesRegex(ManifestError, 'enrolled to node .* not the project owner'):
            self.bind(queue='stranger', state_root=stranger.root)
        self.assertEqual(len(manifests.read_journal(self.project)), 1)

    def test_enrolled_queue_resolves_after_a_remount_changes_device_numbers(self):
        self.store.add(argv=['/bin/true'], cwd=self.base, name='own')
        self.bind(queue='main', state_root=self.store.root)
        real, own = Path.lstat, self.store.root

        def remounted(path, *args, **kwargs):  # Same volume UUID and inodes, a new device number.
            info = real(path, *args, **kwargs)
            if path == own or own in path.parents:
                return os.stat_result((info.st_mode, info.st_ino, info.st_dev + 1, *info[3:10]))
            return info
        with patch.object(Path, 'lstat', remounted):
            resolved = self.resolve('node-a/main:J1.1')
        self.assertEqual(resolved['job']['name'], 'own')

    def test_resolution_needs_a_qualified_reference_to_one_unchanged_record(self):
        self.bind()
        for reference, message in (('J2.1', 'queue-qualified'), ('node-a:J2.1', 'queue-qualified'),
                                   ('node-a/research:J2', 'queue-qualified'), ('node-a/other:J2.1', 'no binding'),
                                   ('node-a/research:J3.1', 'no job J3.1'), ('node-a/research:J2.2', 'displayed as')):
            with self.subTest(reference=reference), self.assertRaisesRegex(ManifestError, message):
                self.resolve(reference)
        with self.assertRaisesRegex(ManifestError, 'no longer matches'):
            self.resolve('node-a/research:J2.1', volume_uuid_of=lambda path: VOLUME.replace('1', '9'))
        self.legacy.rename_id('J1.1', 'M1.1')
        with self.assertRaisesRegex(ManifestError, "displayed as 'M1.1'"):
            self.resolve('node-a/research:J1.1')
        database = self.legacy.root / 'queue.sqlite3'
        shutil.copy2(database, self.base / 'copy.sqlite3')
        os.replace(self.base / 'copy.sqlite3', database)  # Same path and bytes, a different inode.
        with self.assertRaisesRegex(ManifestError, 'no longer matches'):
            self.resolve('node-a/research:J2.1')
        os.rename(self.legacy.root, self.base / 'moved')
        with self.assertRaisesRegex(ManifestError, 'no longer matches'):
            self.resolve('node-a/research:J2.1')

    def test_check_reports_conflicting_and_malformed_bindings(self):
        record = self.bind()
        key = manifests.load_project(self.project)[1]['project_key']
        payload = {'node': 'node-a', 'queue': 'research', 'binding': record['binding']}
        manifests.append_journal(self.project, key, 'queue_binding', uuid4().hex, payload)  # Bypasses bind_queue.
        result = manifest_check.check(self.store, self.project)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual([item['code'] for item in result['findings'] if item['level'] == 'error'],
                         ['queue_binding_conflict', 'queue_binding_conflict'])
        broken = {**payload, 'queue': 'upper', 'binding': {**record['binding'], 'volume_uuid': VOLUME[:-1] + 'A'}}
        manifests.append_journal(self.project, key, 'queue_binding', uuid4().hex, broken)
        damage = [item['message'] for item in manifest_check.check(self.store, self.project)['findings']
                  if item['code'] == 'journal_damaged']
        self.assertEqual(len(damage), 1)
        self.assertIn('record 3 is a malformed queue_binding receipt', damage[0])

    def test_cli_binds_and_resolves(self):
        state = str(self.store.root)
        with patch.object(manifest_legacy, 'observe_volume_uuid', volume):
            code, output = run_jobs(['manifest', 'bind-queue', '--project-root', str(self.project), '--node', 'node-a',
                                     '--queue', 'research', '--state-root', str(self.legacy.root), '--json'], state)
            self.assertEqual((code, json.loads(output)['queue']), (0, 'node-a/research'), output)
            code, output = run_jobs(['manifest', 'resolve', 'node-a/research:J1.1', '--project-root',
                                     str(self.project), '--json'], state)
            self.assertEqual((code, json.loads(output)['job']['display_id']), (0, 'J1.1'), output)
            self.assertNotIn('SECRET', output)
            code, output = run_jobs(['manifest', 'resolve', 'J1.1', '--project-root', str(self.project)], state)
            self.assertEqual(code, 2, output)

    def test_volume_helper_reports_a_normalized_uuid_or_nothing(self):
        observed = manifest_legacy.observe_volume_uuid(self.base)
        if platform.system() == 'Darwin':
            self.assertIsNotNone(observed)
        if observed is not None:
            self.assertRegex(observed, manifest_legacy.VOLUME_UUID)
        self.assertIsNone(manifest_legacy.observe_volume_uuid(self.base / 'missing'))


if __name__ == '__main__':
    unittest.main()
