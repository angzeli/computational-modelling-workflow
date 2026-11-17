"""Owner-written annotations.json (#41 Phase 3a): append-only human events, never the queue note."""
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs import manifest_annotations, manifests
from cmw.jobs.manifests import ManifestError
from cmw.jobs.store import Store
from tests.jobs.test_catalogue_cli import run_jobs
from tests.jobs.test_manifests import Draft202012Validator, ManifestCase, schema_errors


class ManifestAnnotationTests(ManifestCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.directory = Path(manifests.reserve(self.store, self.project)['job_directory'])
        self.job = manifests.add_native(self.store, self.project, cwd=self.directory, argv=['/bin/true'],
                                        engine='Command', name='synthetic')
        self.path = self.directory / '.cmw-manifest' / 'annotations.json'

    def annotate(self, **changes):
        options = {key: changes.pop(key) for key in ('expected_revision', 'request') if key in changes}
        return manifest_annotations.annotate(self.store, self.project, 'J1', changes, **options)

    def document(self):
        return json.loads(self.path.read_text())

    def test_events_append_in_sequence_and_fold_into_the_current_view(self):
        first = self.annotate(label='slab relax', purpose='pre-convergence', outcome='checkpoint-only')
        second = self.annotate(outcome='reported', note='Line one\r\nline two', label='')
        document = self.document()
        self.assertEqual((document['revision'], [event['sequence'] for event in document['events']]), (2, [1, 2]))
        self.assertEqual(document['events'][0], first['event'])
        self.assertEqual((first['event']['author_node'], first['event']['system'], first['replayed']),
                         (self.node, None, False))
        self.assertTrue(first['event']['timestamp'].endswith('Z'))
        self.assertEqual(second['current'], {'label': None, 'purpose': 'pre-convergence', 'system': None,
                                             'outcome': 'reported', 'note': 'Line one\nline two'})
        self.assertEqual(document['events'][1]['label'], '')  # The clearing event itself is kept.
        if Draft202012Validator is not None:
            self.assertEqual(schema_errors('annotations.v1.json', document), [])
        # The replaceable queue note is a separate interface and stays untouched.
        self.assertEqual(self.store.snapshot()['jobs'][0]['note'], '')

    def test_invalid_noop_and_unsupported_changes_append_nothing(self):
        self.annotate(outcome='reported')
        for changes, message in (({}, 'at least one'), ({'outcome': 'reported'}, 'changes nothing'),
                                 ({'label': ''}, 'changes nothing'), ({'outcome': ''}, 'clearing an outcome'),
                                 ({'outcome': 'converged'}, 'Outcome must be one of'),
                                 ({'label': 'a\nb'}, 'control characters'), ({'parent': 'J0'}, 'may only name')):
            with self.subTest(changes=changes), self.assertRaisesRegex(ManifestError, message):
                self.annotate(**changes)
        self.assertEqual(self.document()['revision'], 1)

    def test_expected_revision_rejects_a_concurrent_append(self):
        self.annotate(purpose='first', expected_revision=0)
        with self.assertRaisesRegex(ManifestError, 'at revision 1, not 0'):
            self.annotate(purpose='second', expected_revision=0)
        self.assertEqual(self.annotate(purpose='second', expected_revision=1)['revision'], 2)

    def test_request_uuid_retries_return_the_original_event(self):
        request = uuid4().hex
        first = self.annotate(note='once', request=request)
        retry = self.annotate(note='once', request=request)
        self.assertEqual((retry['event'], retry['replayed'], retry['revision']), (first['event'], True, 1))
        with self.assertRaisesRegex(ManifestError, 'different annotation'):
            self.annotate(note='other', request=request)
        self.assertEqual(self.document()['revision'], 1)
        edited = self.document()
        edited['events'][0]['note'] = 'rewritten by hand'
        self.path.write_text(json.dumps(edited))
        with self.assertRaisesRegex(ManifestError, 'differs from its ledger receipts'):
            self.annotate(note='once', request=request)
        with self.assertRaisesRegex(ManifestError, 'differs from its ledger receipts'):
            self.annotate(note='a fresh request')  # A new append never republishes an altered history.
        self.assertEqual(self.document()['events'][0]['note'], 'rewritten by hand')

    def test_committed_receipt_without_a_published_file_is_reconciled(self):
        request = uuid4().hex
        with patch.object(manifest_annotations, '_write', side_effect=OSError('synthetic lost publication')):
            with self.assertRaises(OSError):
                self.annotate(purpose='lost reply', request=request)
        self.assertFalse(self.path.exists())
        retried = self.annotate(purpose='lost reply', request=request)
        self.assertEqual((retried['replayed'], retried['revision'], retried['event']['sequence']), (True, 1, 1))
        with patch.object(manifest_annotations, '_write', side_effect=OSError('synthetic lost publication')):
            with self.assertRaises(OSError):
                self.annotate(outcome='discarded')
        later = self.annotate(note='after the gap')  # A different request publishes the pending one first.
        self.assertEqual([event['sequence'] for event in self.document()['events']], [1, 2, 3])
        self.assertEqual((later['current']['outcome'], later['revision']), ('discarded', 3))

    def test_concurrent_appends_get_contiguous_sequences(self):
        errors = []

        def append(index):
            try:
                self.annotate(note=f'writer {index}')
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        threads = [threading.Thread(target=append, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        document = self.document()
        self.assertEqual(([event['sequence'] for event in document['events']], document['revision']),
                         (list(range(1, 7)), 6))

    def test_non_owner_unknown_and_tampered_targets_are_refused(self):
        other = Store(self.base / 'other-state')
        other.enroll_queue(uuid4().hex, 'linux:synthetic-only')
        with self.assertRaisesRegex(ManifestError, 'does not own'):
            manifest_annotations.annotate(other, self.project, 'J1', {'note': 'x'})
        for job_id, message in (('J1.1', 'native project ID'), ('J9', 'no manifest on this project owner')):
            with self.subTest(job_id=job_id), self.assertRaisesRegex(ManifestError, message):
                manifest_annotations.annotate(self.store, self.project, job_id, {'note': 'x'})
        self.annotate(note='kept')
        self.path.write_text(json.dumps({**self.document(), 'revision': 5}))
        with self.assertRaisesRegex(ManifestError, 'not a consistent append-only history'):
            self.annotate(note='refused')
        job_json = self.directory / '.cmw-manifest' / 'job.json'
        job_json.unlink()
        job_json.write_text('{}\n')
        with self.assertRaisesRegex(ManifestError, 'differs from its integrity receipt'):
            self.annotate(note='refused')

    def test_cli_appends_and_keeps_the_queue_note_form(self):
        state = self.base / 'state'
        code, output = run_jobs(['annotate', 'J1', '--manifest-project', str(self.project), '--outcome', 'reported',
                                 '--note', 'from the CLI', '--json'], state)
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['current']['outcome'], 'reported')
        code, output = run_jobs(['annotate', 'J1', '--manifest-project', str(self.project), '--clear-note',
                                 '--expected-revision', '1', '--json'], state)
        self.assertEqual((code, json.loads(output)['current']['note']), (0, None), output)
        code, output = run_jobs(['annotate', self.job['display_id'], '--outcome', 'reported'], state)
        self.assertEqual(code, 2)
        self.assertIn('require --manifest-project', output)
        code, _ = run_jobs(['annotate', self.job['display_id'], '--note', 'queue note', '--json'], state)
        self.assertEqual((code, self.store.snapshot()['jobs'][0]['note'], self.document()['revision']),
                         (0, 'queue note', 2))


if __name__ == '__main__':
    unittest.main()
