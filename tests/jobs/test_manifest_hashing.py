"""Asynchronous restart-output digests (#41 Phase 2c): never on the completion path."""
import hashlib
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from cmw.jobs import manifest_hashing, manifest_state
from cmw.jobs.manifests import ManifestError
from tests.jobs.test_catalogue_cli import run_jobs
from tests.jobs.test_manifests import Draft202012Validator, ManifestCase, schema_errors

CONTRACTS = Path(__file__).resolve().parents[1] / 'fixtures' / 'manifests' / 'parser-contracts'
WAVECAR, CHGCAR = b'synthetic output wavefunction', b'synthetic output charge density'


class OutputHashingTests(ManifestCase):
    def setUp(self):
        super().setUp()
        self.init()

    def finished_vasp_job(self, *, outputs=True):
        contract = json.loads((CONTRACTS / 'converged-at-nelm.json').read_text())
        directory, argv = self.vasp_job()
        job = self.add(directory, argv)
        run = directory / 'run'
        run.mkdir()
        (run / 'OUTCAR').write_text('\n'.join(contract['OUTCAR_lines']) + '\n')
        if outputs:
            (run / 'WAVECAR').write_bytes(WAVECAR)
            (run / 'CHGCAR').write_bytes(CHGCAR)
        return directory, job

    def finish(self, job):
        with self.store.transaction() as con:
            record = self.store.get(con, job['id'])
            record.update(status='Done', exit_code=0, started_at=time.time() - 5, finished_at=time.time())
            self.store.save(con, record)

    def state(self, directory):
        return json.loads((directory / '.cmw-manifest' / 'state.json').read_text())

    def test_terminal_state_is_published_pending_then_hashed_by_the_single_writer(self):
        directory, job = self.finished_vasp_job()
        with patch.object(manifest_hashing, 'launch') as launch:
            self.finish(job)
            pending = self.state(directory)
            self.assertEqual((pending['restart_outputs'], pending['hashes_pending']),
                             ({'WAVECAR': None, 'CHGCAR': None}, True))
            launch.assert_called_once_with(self.store, job['id'])
            hashed = manifest_hashing.hash_outputs(self.store, job['id'])
            self.assertEqual(hashed['restart_outputs'], {'WAVECAR': hashlib.sha256(WAVECAR).hexdigest(),
                                                         'CHGCAR': hashlib.sha256(CHGCAR).hexdigest()})
            self.assertEqual((hashed['hashes_pending'], hashed['revision']), (False, pending['revision'] + 1))
            if Draft202012Validator is not None:
                self.assertEqual(schema_errors('state.v1.json', hashed), [])
            self.store.annotate(job['id'], 'later save')  # Digests survive and nothing relaunches.
            self.assertEqual(self.state(directory)['restart_outputs'], hashed['restart_outputs'])
            launch.assert_called_once()

    def test_changed_or_missing_outputs_stay_null_with_evidence(self):
        directory, job = self.finished_vasp_job()
        with patch.object(manifest_hashing, 'launch'):
            self.finish(job)
        (directory / 'run' / 'CHGCAR').unlink()
        real = manifest_hashing.stable_digest

        def changing(path):  # stable_digest's own change detection is tested directly below.
            return (None, 'changed while it was hashed') if path.name == 'WAVECAR' else real(path)
        with patch.object(manifest_hashing, 'stable_digest', side_effect=changing):
            state = manifest_hashing.hash_outputs(self.store, job['id'])
        self.assertEqual((state['restart_outputs'], state['hashes_pending']),
                         ({'WAVECAR': None, 'CHGCAR': None}, False))
        markers = ' '.join(item['marker'] for item in state['evidence'])
        self.assertIn('WAVECAR changed while it was hashed', markers)
        self.assertIn('CHGCAR is unreadable', markers)

    def test_stable_digest_detects_a_change_during_the_read(self):
        path = self.base / 'checkpoint'
        path.write_bytes(WAVECAR)
        self.assertEqual(manifest_hashing.stable_digest(path), (hashlib.sha256(WAVECAR).hexdigest(), None))
        before = path.lstat()
        after = os.stat_result((*before[:6], before.st_size + 1, *before[7:]))
        with patch.object(Path, 'lstat', side_effect=[before, after]):
            self.assertEqual(manifest_hashing.stable_digest(path), (None, 'changed while it was hashed'))

    def test_detached_hasher_updates_state_without_blocking_the_transition(self):
        directory, job = self.finished_vasp_job()
        started = time.monotonic()
        self.finish(job)
        self.assertLess(time.monotonic() - started, 10)
        deadline = time.monotonic() + 60
        while self.state(directory)['hashes_pending'] and time.monotonic() < deadline:
            time.sleep(0.2)
        state = self.state(directory)
        self.assertFalse(state['hashes_pending'], (self.base / 'state' / 'manifest-hashes.log').read_text()
                         if (self.base / 'state' / 'manifest-hashes.log').exists() else 'no hasher log')
        self.assertEqual(state['restart_outputs']['WAVECAR'], hashlib.sha256(WAVECAR).hexdigest())

    def test_no_outputs_or_non_terminal_state_never_hash(self):
        directory, job = self.finished_vasp_job(outputs=False)
        with patch.object(manifest_hashing, 'launch') as launch:
            self.finish(job)
        launch.assert_not_called()
        self.assertEqual((self.state(directory)['restart_outputs'], self.state(directory)['hashes_pending']),
                         ({}, False))
        directory, job = self.finished_vasp_job()
        manifest_state.publish(self.store, job['id'])
        with self.assertRaisesRegex(ManifestError, 'current terminal state'):
            manifest_state.record_output_hashes(self.store, job['id'], {'WAVECAR': '0' * 64})

    def test_cli_retry_hashes_pending_outputs(self):
        directory, job = self.finished_vasp_job()
        with patch.object(manifest_hashing, 'launch'):
            self.finish(job)
        code, output = run_jobs(['manifest', 'hash-outputs', job['display_id'], '--json'], self.base / 'state')
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['restart_outputs']['CHGCAR'], hashlib.sha256(CHGCAR).hexdigest())


if __name__ == '__main__':
    unittest.main()
