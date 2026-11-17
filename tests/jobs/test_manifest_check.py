"""Read-only manifest integrity check (#41 Phase 3b): files against their permanent anchors."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs import manifest_annotations, manifest_check, manifest_hashing, manifests
from tests.jobs.test_catalogue_cli import run_jobs
from tests.jobs.test_manifests import ManifestCase

CHECKPOINT = b'synthetic parent WAVECAR'


class ManifestCheckTests(ManifestCase):
    def setUp(self):
        super().setUp()
        self.init()

    def command_job(self):
        directory = Path(manifests.reserve(self.store, self.project)['job_directory'])
        manifests.add_native(self.store, self.project, cwd=directory, argv=['/bin/true'], engine='Command',
                             name='synthetic')
        return directory

    def check(self, *job_ids, **options):
        return manifest_check.check(self.store, self.project, job_ids, **options)

    def job(self, result, job_id):
        return next(job for job in result['jobs'] if job['id'] == job_id)

    def codes(self, result, job_id):
        return {item['code'] for item in self.job(result, job_id)['findings']}

    def lineage(self):
        """J1 parent with hashed WAVECAR output, J2 restarted from it."""
        parent, argv = self.vasp_job()
        job = self.add(parent, argv)
        (parent / 'run').mkdir()
        (parent / 'run' / 'WAVECAR').write_bytes(CHECKPOINT)
        with patch.object(manifest_hashing, 'launch'), self.store.transaction() as con:
            record = self.store.get(con, job['id'])
            record.update(status='Done', exit_code=0, started_at=time.time() - 5, finished_at=time.time())
            self.store.save(con, record)
        manifest_hashing.hash_outputs(self.store, job['id'])
        child, argv = self.vasp_job()
        argv[argv.index('--restart') + 1] = 'wavecar'
        self.add(child, argv, restart_from='J1')
        return parent, child

    def test_clean_project_verifies_every_known_job_and_writes_nothing(self):
        parent, child = self.lineage()
        self.command_job()
        manifest_annotations.annotate(self.store, self.project, 'J3', {'outcome': 'reported'})
        manifests.reserve(self.store, self.project)  # J4: reserved, never manifested.
        # SQLite's own transient -wal/-shm files (and their directory) are not project data.
        def files():
            return {path: path.stat().st_mtime_ns for path in self.project.rglob('*')
                    if path != self.project / '.cmw' and not path.name.endswith(('-wal', '-shm'))}
        before = files()
        result = self.check(rehash=True)
        self.assertEqual((result['status'], result['errors']), ('VERIFIED', 0), result)
        self.assertEqual([(job['id'], job['status']) for job in result['jobs']],
                         [('J1', 'verified'), ('J2', 'verified'), ('J3', 'verified'), ('J4', 'reserved')])
        self.assertEqual(self.job(result, 'J2')['restart_inputs'], {'WAVECAR': 'unchanged'})
        self.assertEqual(self.job(result, 'J2')['lineage'], {'parent': 'J1', 'roles': {'WAVECAR': 'match'}})
        self.assertEqual((self.job(result, 'J1')['state'], self.job(result, 'J3')['annotation_revision']), ('done', 1))
        self.assertEqual(files(), before)
        self.assertEqual(self.check('J2')['jobs'][0]['restart_inputs'], {'WAVECAR': 'present'})  # No rehash.

    def test_hand_edited_or_writable_job_json_is_detected(self):
        directory = self.command_job()
        path = directory / '.cmw-manifest' / 'job.json'
        os.chmod(path, 0o644)
        self.assertEqual(self.codes(self.check(), 'J1'), {'job_json_writable'})
        path.write_text(path.read_text().replace('"Command"', '"VASP"'))
        result = self.check()
        self.assertEqual((result['status'], self.job(result, 'J1')['status']), ('FAILED', 'failed'))
        self.assertIn('job_json_changed', self.codes(result, 'J1'))
        code, output = run_jobs(['check', '--manifest-project', str(self.project), 'J1'], self.base / 'state')
        self.assertEqual(code, 2)
        self.assertIn('job_json_changed', output)

    def test_changed_inputs_restart_copies_and_parent_outputs_are_reported(self):
        parent, child = self.lineage()
        (child / 'inputs' / 'INCAR').write_text('ENCUT = 520\n')
        (child / 'inputs' / 'WAVECAR').unlink()
        (child / 'inputs' / 'WAVECAR').write_bytes(b'replaced checkpoint')
        state_path = parent / '.cmw-manifest' / 'state.json'
        state = json.loads(state_path.read_text())
        state['restart_outputs']['WAVECAR'] = '0' * 64
        state_path.write_text(json.dumps(state))
        codes = self.codes(self.check('J2', rehash=True), 'J2')
        self.assertTrue({'input_changed', 'restart_input_changed', 'parent_output_differs'} <= codes, codes)

    def test_state_annotations_and_journal_damage_are_reported(self):
        directory = self.command_job()
        sidecar = directory / '.cmw-manifest'
        state = json.loads((sidecar / 'state.json').read_text())
        (sidecar / 'state.json').write_text(json.dumps({**state, 'binding': {**state['binding'], 'attempt_id': 'f' * 32}}))
        manifest_annotations.annotate(self.store, self.project, 'J1', {'note': 'original'})
        edited = json.loads((sidecar / 'annotations.json').read_text())
        edited['events'][0]['note'] = 'rewritten'
        (sidecar / 'annotations.json').write_text(json.dumps(edited))
        self.assertTrue({'state_binding', 'annotations_changed'} <= self.codes(self.check(), 'J1'))
        with open(self.project / '.cmw' / 'integrity.jsonl', 'ab') as stream:
            stream.write(b'{"partial":')
        result = self.check()
        self.assertEqual([item['code'] for item in result['findings']], ['journal_damaged'])
        self.assertIn('2 complete record(s)', result['findings'][0]['message'])
        self.assertEqual(self.job(result, 'J1')['binding']['node_id'], self.node)  # The valid prefix still checks.

    def test_unpublished_annotation_receipt_is_a_notice(self):
        self.command_job()
        with patch.object(manifest_annotations, '_write', side_effect=OSError('synthetic lost publication')):
            with self.assertRaises(OSError):
                manifest_annotations.annotate(self.store, self.project, 'J1', {'note': 'pending'})
        result = self.check()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertIn('annotations_unpublished', self.codes(result, 'J1'))

    def test_burned_remote_and_unreserved_jobs(self):
        directory = Path(manifests.reserve(self.store, self.project)['job_directory'])
        with patch.object(self.store, 'add', side_effect=RuntimeError('synthetic registration failure')):
            with self.assertRaises(RuntimeError):
                manifests.add_native(self.store, self.project, cwd=directory, argv=['/bin/true'],
                                     engine='Command', name='burned')
        self.assertEqual(self.codes(self.check(), 'J1'), {'unregistered'})
        remote = Path(manifests.reserve(self.store, self.project)['job_directory'])
        manifests.add_native(self.store, self.project, cwd=remote, argv=['/bin/true'], engine='Command', name='r')
        digest = hashlib.sha256((remote / '.cmw-manifest' / 'job.json').read_bytes()).hexdigest()
        records = manifests.read_journal(self.project)
        # Rewrite J2's registration as another node's, as a remote target would have registered it.
        foreign = {'node_id': uuid4().hex, 'queue_id': uuid4().hex, 'job_id': 'J1.1', 'attempt_id': 'a' * 32}
        lines = [json.dumps({**record, 'payload': {'id': 'J2', 'job_sha256': digest, 'binding': foreign}}, sort_keys=True)
                 if record['event'] == 'registration' and record['payload']['id'] == 'J2'
                 else json.dumps(record, sort_keys=True) for record in records]
        (self.project / '.cmw' / 'integrity.jsonl').write_text('\n'.join(lines) + '\n')
        self.assertEqual(self.codes(self.check('J2'), 'J2'), {'remote'})
        (self.project / 'jobs' / 'J99').mkdir()
        result = self.check()
        self.assertEqual(self.codes(result, 'J99'), {'unreserved'})
        self.assertEqual(result['status'], 'FAILED')

    def test_check_never_creates_ledger_tables_and_cli_forms_are_explicit(self):
        self.command_job()
        code, output = run_jobs(['check', '--manifest-project', str(self.project), '--json'], self.base / 'state')
        self.assertEqual((code, json.loads(output)['status']), (0, 'VERIFIED'), output)
        with sqlite3.connect(self.project / '.cmw' / 'ids.sqlite3') as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('annotation_requests', tables)
        for arguments in (['check'], ['check', '--spec', 'x.json', '--manifest-project', str(self.project)],
                          ['check', '--spec', 'x.json', 'J1'],
                          ['check', '--manifest-project', str(self.project), 'J1.1']):
            with self.subTest(arguments=arguments):
                self.assertEqual(run_jobs(arguments, self.base / 'state')[0], 2)


if __name__ == '__main__':
    unittest.main()
