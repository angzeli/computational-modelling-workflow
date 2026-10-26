"""Incremental actual-file observation of native, runner and validator evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cmw.progress.card import card_text
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.service import ObservationService


class MultiwfnProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-multiwfn-progress-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = {'schema': 1, 'profile': 'IFCT', 'attempt_directory': str(self.root),
            'scientific_attempt_id': 'attempt_001', 'source_target_id': 'source-target',
            'source_attempt_id': 'donor-001', 'source_result': str(self.root / 'source.json'),
            'version': '2026.7.15', 'threads': 2, 'state_index': 1}
        metadata = self.root / 'jobs-association.json'
        metadata.write_text(json.dumps(self.record) + '\n')
        self.sources = [SourceDescriptor('multiwfn_session', str(self.root / 'native.log'), stage='IFCT'),
            SourceDescriptor('multiwfn_events', str(self.root / 'events.jsonl'), optional=True, stage='IFCT'),
            SourceDescriptor('multiwfn_validation', str(self.root / 'validation.jsonl'), optional=True, stage='IFCT'),
            SourceDescriptor('multiwfn_metadata', str(metadata), optional=True, stage='IFCT',
                expected_sha256=hashlib.sha256(metadata.read_bytes()).hexdigest(), expected_size=metadata.stat().st_size)]
        self.context = RunContext('test-binding', 'Multiwfn', tuple(self.sources), stage='IFCT', attempt_id='jobs-attempt')
        self.service = ObservationService(); self.addCleanup(self.service.close)

    def write_event(self, **fields):
        with Path(self.sources[1].path).open('a') as stream:
            stream.write(json.dumps({**self.record, 'kind': 'prepared', **fields}) + '\n')

    def validation(self, **fields):
        native_path = Path(self.sources[0].path)
        if not native_path.exists():
            native_path.write_text('Version 2026.7.15\n')
        native = native_path.stat()
        fields = {'native_path': str(native_path), 'native_identity': [native.st_dev, native.st_ino],
            'native_file_version': [native.st_size, native.st_mtime_ns, native.st_ctime_ns], **fields}
        Path(self.sources[2].path).write_text(json.dumps({**self.record, 'kind': 'validator_record',
            'status': 'PASSED', 'result_path': str(self.root / 'result.json'), 'result_sha256': '1' * 64, **fields}) + '\n')

    def test_partial_native_lines_and_no_invented_metrics(self):
        native = Path(self.sources[0].path)
        native.write_text('Version 2026.')
        first = self.service.poll(self.context)
        self.assertFalse(any(item.kind == 'native' for item in first.events))
        with native.open('a') as stream:
            stream.write('7.15\n( Number of parallel threads: 2 Current date: test )\nLoaded /alias/source successfully!\nLoading configuration coefficients of excited state 1...\n')
        second = self.service.poll(self.context)
        self.assertIn('Native IFCT state 1', second.phase)
        self.assertEqual(second.metrics, ())
        self.assertEqual(second.scientific_validation, 'not-assessed')
        self.assertEqual([group.label for group in second.card_groups], ['ANALYSIS', 'STAGE', 'OUTPUT'])
        for width in (77, 80, 100, 140):
            lines = card_text(second, width=width, now=0, max_lines=8, framed=True).splitlines()
            self.assertLessEqual(len(lines), 8)
            self.assertTrue(all(len(line) <= width for line in lines))

    def test_runner_exit_is_not_validation_and_exact_record_is_separate(self):
        self.write_event(kind='process_exited', value='0')
        observed = self.service.poll(self.context)
        self.assertEqual(observed.scientific_validation, 'not-assessed')
        self.validation()
        observed = self.service.poll(self.context)
        self.assertEqual(observed.scientific_validation, 'validator-recorded-passed')
        self.assertTrue(any(item.kind == 'validator' for item in observed.events))
        self.assertTrue(self.service.history(self.context).entries)

    def test_wrong_attempt_and_unpinned_events_are_not_accepted(self):
        self.validation(scientific_attempt_id='previous-attempt')
        observed = self.service.poll(self.context)
        self.assertEqual(observed.status, 'SOURCE_UNRESOLVED')
        self.assertEqual(observed.scientific_validation, 'not-assessed')
        unpinned = RunContext('other', 'Multiwfn', tuple(self.sources[:3]), stage='IFCT', attempt_id='other')
        self.assertEqual(self.service.poll(unpinned).scientific_validation, 'not-assessed')

    def test_native_thread_mismatch_and_unknown_version_fail_honestly(self):
        native = Path(self.sources[0].path)
        native.write_text('Version 2026.7.15\nNumber of parallel threads: 8\n')
        self.assertEqual(self.service.poll(self.context).status, 'SOURCE_UNRESOLVED')
        unknown = RunContext('unknown', 'Multiwfn', tuple(self.sources), stage='IFCT', attempt_id='unknown')
        native.write_text('Version 9.9\n')
        snapshot = self.service.poll(unknown)
        self.assertIn(snapshot.status, {'UNSUPPORTED', 'SOURCE_UNRESOLVED'})
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')

    def test_replaced_unknown_native_grammar_can_recover(self):
        native = Path(self.sources[0].path)
        native.write_text('Version 9.9\n')
        self.assertIn(self.service.poll(self.context).status, {'UNSUPPORTED', 'SOURCE_UNRESOLVED'})
        native.unlink(); native.write_text('Version 2026.7.15\nLoaded /alias/source successfully!\n')
        snapshot = self.service.poll(self.context)
        self.assertEqual(snapshot.status, 'OK')
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')

    def test_replacement_and_malformed_record_invalidate_claims(self):
        self.write_event()
        self.validation()
        native = Path(self.sources[0].path)
        self.assertEqual(self.service.poll(self.context).scientific_validation, 'validator-recorded-passed')
        native.unlink(); native.write_text('Version 2026.7.15\nnew session\n')
        self.assertEqual(self.service.poll(self.context).scientific_validation, 'not-assessed')
        with Path(self.sources[1].path).open('a') as stream:
            stream.write('{broken complete record}\n')
        self.assertEqual(self.service.poll(self.context).status, 'SOURCE_UNRESOLVED')

    def test_metadata_only_change_preserves_recorded_validation(self):
        self.validation()
        self.assertEqual(self.service.poll(self.context).scientific_validation, 'validator-recorded-passed')
        native = Path(self.sources[0].path)
        before = native.stat()
        native.chmod(0o400)
        after = native.stat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
        self.assertEqual(self.service.poll(self.context).scientific_validation, 'validator-recorded-passed')
        fresh = ObservationService(); self.addCleanup(fresh.close)
        self.assertEqual(fresh.poll(self.context).scientific_validation, 'validator-recorded-passed')

    def test_append_after_validation_makes_record_historical(self):
        self.validation()
        self.assertEqual(self.service.poll(self.context).scientific_validation, 'validator-recorded-passed')
        with Path(self.sources[0].path).open('a') as stream:
            stream.write('later unrelated native output\n')
        snapshot = self.service.poll(self.context)
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')
        self.assertTrue(any('historical' in message for message in snapshot.messages))

    def test_pinned_association_mutation_revokes_validator_evidence(self):
        self.validation()
        self.assertEqual(self.service.poll(self.context).scientific_validation, 'validator-recorded-passed')
        Path(self.sources[3].path).write_text('{}\n')
        snapshot = self.service.poll(self.context)
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')
        self.assertTrue(any(source.health == 'INTEGRITY_ERROR' for source in snapshot.sources))


if __name__ == '__main__':
    unittest.main()
