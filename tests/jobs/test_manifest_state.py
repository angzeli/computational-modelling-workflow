"""state.json publisher (#41 Phase 2b): a revisioned projection of the queue record."""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from cmw.jobs import manifest_state, manifests
from cmw.jobs.manifests import ManifestError
from tests.jobs.test_manifests import Draft202012Validator, ManifestCase, schema_errors

CONTRACTS = Path(__file__).resolve().parents[1] / 'fixtures' / 'manifests' / 'parser-contracts'


class StatePublisherTests(ManifestCase):
    def setUp(self):
        super().setUp()
        self.init()

    def command_job(self):
        directory = Path(manifests.reserve(self.store, self.project)['job_directory'])
        job = manifests.add_native(self.store, self.project, cwd=directory, argv=['/bin/true'],
                                   engine='Command', name='synthetic')
        return directory, job

    def state(self, directory):
        return json.loads((directory / '.cmw-manifest' / 'state.json').read_text())

    def transition(self, job, **changes):
        with self.store.transaction() as con:
            record = self.store.get(con, job['id'])
            record.update(changes)
            self.store.save(con, record)

    def assert_valid(self, state):
        if Draft202012Validator is not None:
            self.assertEqual(schema_errors('state.v1.json', state), [])

    def test_registration_and_each_transition_publish_a_new_revision(self):
        directory, job = self.command_job()
        queued = self.state(directory)
        self.assertEqual((queued['revision'], queued['status'], queued['native_status']), (1, 'queued', 'Hold'))
        self.assertEqual(queued['binding'], {'node_id': self.node, 'queue_id': self.queue['queue_id'],
                                             'job_id': job['id'], 'attempt_id': job['attempt_id']})
        self.assertEqual((queued['writer_node'], queued['scf_result']), (self.node, 'unknown'))
        self.assert_valid(queued)
        self.store.change(job['id'], 'release')
        self.assertEqual((self.state(directory)['revision'], self.state(directory)['native_status']), (2, 'Queue'))
        started = time.time()
        self.transition(job, status='Run', started_at=started)
        self.transition(job, status='Done', finished_at=started + 5, exit_code=0)
        done = self.state(directory)
        self.assertEqual((done['revision'], done['status'], done['exit_code']), (4, 'done', 0))
        self.assertTrue(done['started_at'].endswith('Z') and done['ended_at'].endswith('Z'))
        self.assert_valid(done)
        self.transition(job)  # An unchanged projection is not rewritten.
        self.assertEqual(self.state(directory)['revision'], 4)

    def test_unknown_projects_to_null_status(self):
        directory, job = self.command_job()
        self.transition(job, status='Unknown')
        state = self.state(directory)
        self.assertEqual((state['status'], state['native_status']), (None, 'Unknown'))
        self.assert_valid(state)

    def test_terminal_vasp_attempt_is_classified_once(self):
        contract = json.loads((CONTRACTS / 'converged-at-nelm.json').read_text())
        directory, argv = self.vasp_job()
        job = self.add(directory, argv)
        run = directory / 'run'
        run.mkdir()
        (run / 'OUTCAR').write_text('\n'.join(contract['OUTCAR_lines']) + '\n')
        (run / 'stdout.log').write_text('\n'.join(contract['stdout_lines']) + '\n')
        (run / 'WAVECAR').write_bytes(b'synthetic output checkpoint')
        self.transition(job, status='Done', exit_code=0, finished_at=time.time())
        state = self.state(directory)
        self.assertEqual((state['scf_result'], state['last_step'], state['energy_ev'], state['total_magnetisation_mu_b']),
                         ('converged', 2, -10.00000001, 2.0))
        self.assertEqual((state['restart_outputs'], state['hashes_pending']), ({'WAVECAR': None}, False))
        self.assert_valid(state)
        with patch('cmw.periodic.vasp.scf_classifier.classify_run', side_effect=AssertionError('reclassified')):
            self.store.annotate(job['id'], 'still Done')
        self.assertEqual(self.state(directory)['scf_result'], 'converged')

    def test_running_vasp_attempt_is_never_classified(self):
        directory, argv = self.vasp_job()
        job = self.add(directory, argv)
        with patch('cmw.periodic.vasp.scf_classifier.classify_run', side_effect=AssertionError('classified')):
            self.transition(job, status='Run', started_at=time.time())
        self.assertEqual((self.state(directory)['status'], self.state(directory)['scf_result']), ('running', 'unknown'))

    def test_foreign_state_is_fenced_and_failures_never_change_the_queue(self):
        directory, job = self.command_job()
        path = directory / '.cmw-manifest' / 'state.json'
        foreign = {**self.state(directory), 'binding': {**self.state(directory)['binding'], 'attempt_id': 'f' * 32}}
        path.write_text(json.dumps(foreign))
        with self.assertRaisesRegex(ManifestError, 'another attempt'):
            manifest_state.publish(self.store, job['id'])
        self.store.change(job['id'], 'release')  # The queue transition still commits.
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['jobs'][0]['status'], 'Queue')
        self.assertEqual(json.loads(path.read_text())['binding']['attempt_id'], 'f' * 32)
        with self.store.read_transaction() as con:
            messages = [row[0] for row in con.execute('SELECT message FROM events')]
        self.assertTrue(any('state.json not updated' in message for message in messages))

    def test_changed_job_json_blocks_publication(self):
        directory, job = self.command_job()
        sidecar = directory / '.cmw-manifest'
        (sidecar / 'job.json').unlink()
        (sidecar / 'job.json').write_text('{}\n')
        with self.assertRaisesRegex(ManifestError, 'differs from the registered manifest'):
            manifest_state.publish(self.store, job['id'])

    def test_jobs_without_manifests_get_no_state(self):
        plain = self.base / 'plain'
        plain.mkdir()
        job = self.store.add(argv=['/bin/true'], cwd=plain, name='plain')
        self.store.change(job['id'], 'release')
        self.assertEqual(list(plain.iterdir()), [])
        self.assertIsNone(manifest_state.publish(self.store, job['id']))


if __name__ == '__main__':
    unittest.main()
