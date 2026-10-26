"""Incremental actual-file observation of native, runner and validator evidence."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cmw.progress.card import card_text
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.service import ObservationService
from cmw.progress.reader import RegularFileReader


class MultiwfnProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-multiwfn-progress-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        events = self.root / "events.jsonl"
        events.touch()
        identity = events.stat()
        self.sequence = 0
        self.record = {'schema': 2, 'stream_id': 'stream-001', 'request_id': 'request-001',
            'stream_generation': 1, 'events_identity': [identity.st_dev, identity.st_ino],
            'native_path': str(self.root / 'native.log'), 'profile': 'IFCT', 'attempt_directory': str(self.root),
            'scientific_attempt_id': 'attempt_001', 'source_target_id': 'source-target',
            'source_attempt_id': 'donor-001', 'source_result': str(self.root / 'source.json'),
            'version': '2026.7.15', 'threads': 2, 'state_index': 1, 'spin_manifold': 'singlet',
            'fragment_labels': ['D', 'A'], 'fragment_counts': [2, 3],
            'expected_roles': 'session_log'}
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
        kind = fields.get('kind', 'prepared')
        chain = ('prepared', 'launch_requested', 'child_launched', 'process_exited',
                 'validation_started', 'validation_passed')
        if self.sequence == 0:
            self.kinds = []
        if kind in chain:
            for prior in chain[:chain.index(kind)]:
                if prior not in self.kinds:
                    self.write_event(kind=prior)
        self.sequence += 1
        value = '42' if kind == 'child_launched' else '0' if kind == 'process_exited' else None
        record = {**self.record, 'kind': kind, 'sequence': self.sequence, 'value': value,
                  'event_source_identity': self.record['events_identity'], **fields}
        with Path(self.sources[1].path).open('a') as stream:
            stream.write(json.dumps(record) + '\n')
        self.kinds.append(kind)
        return record

    def validation(self, **fields):
        native_path = Path(self.sources[0].path)
        if not native_path.exists():
            native_path.write_text('Version 2026.7.15\n')
        native = native_path.stat()
        fields = {'native_path': str(native_path), 'native_identity': [native.st_dev, native.st_ino],
            'native_file_version': [native.st_size, native.st_mtime_ns, native.st_ctime_ns], **fields}
        self.write_event(kind='validation_passed')
        Path(self.sources[2].path).write_text(json.dumps({**self.record, 'sequence': self.sequence, 'event_source_identity': self.record['events_identity'], 'kind': 'validator_record',
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
        self.assertEqual(observed.status, 'SOURCE_CONFLICT')
        self.assertEqual(observed.scientific_validation, 'not-assessed')
        unpinned = RunContext('other', 'Multiwfn', tuple(self.sources[:3]), stage='IFCT', attempt_id='other')
        self.assertEqual(self.service.poll(unpinned).scientific_validation, 'not-assessed')

    def test_native_thread_mismatch_and_unknown_version_fail_honestly(self):
        native = Path(self.sources[0].path)
        native.write_text('Version 2026.7.15\nNumber of parallel threads: 8\n')
        self.assertEqual(self.service.poll(self.context).status, 'SOURCE_CONFLICT')
        unknown = RunContext('unknown', 'Multiwfn', tuple(self.sources), stage='IFCT', attempt_id='unknown')
        native.write_text('Version 9.9\n')
        snapshot = self.service.poll(unknown)
        self.assertIn(snapshot.status, {'UNSUPPORTED', 'SOURCE_CONFLICT'})
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')

    def test_replaced_unknown_native_grammar_can_recover(self):
        native = Path(self.sources[0].path)
        native.write_text('Version 9.9\n')
        self.assertIn(self.service.poll(self.context).status, {'UNSUPPORTED', 'SOURCE_CONFLICT'})
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
        self.assertEqual(self.service.poll(self.context).status, 'SOURCE_CONFLICT')

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


    def test_exact_duplicate_is_nonadvancing_but_changed_duplicate_conflicts(self):
        record = self.write_event()
        first = self.service.poll(self.context)
        with Path(self.sources[1].path).open('a') as stream:
            stream.write(json.dumps(record) + '\n')
        duplicate = self.service.poll(self.context)
        self.assertEqual(duplicate.events, first.events)
        self.assertEqual(duplicate.status, 'OK')
        self.assertTrue(any('Duplicate identical' in message for message in duplicate.messages))
        with Path(self.sources[1].path).open('a') as stream:
            stream.write(json.dumps({**record, 'kind': 'child_launched'}) + '\n')
        self.assertEqual(self.service.poll(self.context).status, 'SOURCE_CONFLICT')

    def test_gap_out_of_order_and_wrong_file_identity_are_conflicts(self):
        for fields in ({'sequence': 2}, {'event_source_identity': [0, 0]},
                       {'stream_generation': 2}, {'request_id': 'other-attempt'}):
            with self.subTest(fields=fields):
                Path(self.sources[1].path).write_text('')
                self.sequence = 0
                self.write_event(**fields)
                context = replace(self.context, binding_id=repr(fields))
                self.assertEqual(self.service.poll(context).status, 'SOURCE_CONFLICT')

    def test_repeated_lifecycle_boundary_with_new_sequence_conflicts(self):
        self.write_event(kind='child_launched')
        self.write_event(kind='child_launched')
        snapshot = self.service.poll(self.context)
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertTrue(any('already recorded' in message for message in snapshot.messages))

    def test_event_stream_replacement_and_truncation_revoke_current_claims(self):
        self.validation()
        self.assertEqual(self.service.poll(self.context).scientific_validation, 'validator-recorded-passed')
        events = Path(self.sources[1].path)
        copy = events.with_suffix('.copy')
        copy.write_bytes(events.read_bytes())
        copy.replace(events)
        observed = self.service.poll(self.context)
        self.assertEqual(observed.status, 'SOURCE_CONFLICT')
        self.assertEqual(observed.scientific_validation, 'not-assessed')
        self.assertNotIn('Runner: Output validation returned', card_text(observed, width=140, now=0))
        fresh = ObservationService(); self.addCleanup(fresh.close)
        self.assertEqual(fresh.poll(self.context).status, 'SOURCE_CONFLICT')

    def test_same_inode_truncation_invalidates_previously_observed_sequence(self):
        self.write_event(kind='prepared')
        self.write_event(kind='child_launched')
        self.service.poll(self.context)
        events = Path(self.sources[1].path)
        first = events.read_text().splitlines()[0]
        events.write_text(first + '\n')
        observed = self.service.poll(self.context)
        self.assertEqual(observed.status, 'SOURCE_CONFLICT')
        self.assertFalse(any(setting.name == 'runner_stage' for setting in observed.settings))

    def test_native_generation_reset_removes_old_scope_and_validation(self):
        native = Path(self.sources[0].path)
        native.write_text('Version 2026.7.15\nNumber of parallel threads: 2\nLoaded /alias/source successfully!\n')
        self.validation()
        before = self.service.poll(self.context)
        self.assertTrue(any(setting.name == 'native_threads' for setting in before.settings))
        replacement = native.with_suffix('.next')
        replacement.write_text('Version 2026.7.15\n')
        replacement.replace(native)
        after = self.service.poll(self.context)
        self.assertFalse(any(setting.name in {'native_threads', 'native_stage'} for setting in after.settings))
        self.assertEqual(after.scientific_validation, 'not-assessed')
        self.assertNotIn('source load reported', card_text(after, width=140, now=0))

    def test_legacy_validator_remains_explicitly_unsequenced(self):
        self.record['schema'] = 1
        metadata = Path(self.sources[3].path)
        metadata.write_text(json.dumps(self.record) + '\n')
        source = replace(self.sources[3], expected_sha256=hashlib.sha256(metadata.read_bytes()).hexdigest(),
                         expected_size=metadata.stat().st_size)
        context = replace(self.context, sources=(*self.sources[:3], source))
        self.validation()
        snapshot = self.service.poll(context)
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')
        self.assertTrue(any('Legacy validator' in message for message in snapshot.messages))
        self.assertTrue(any(event.kind == 'legacy-validator' for event in snapshot.events))

    def test_all_profiles_use_real_reader_scope_and_indeterminate_stages(self):
        for profile in ('FMO', 'ESP', 'IGMH', 'IFCT'):
            with self.subTest(profile=profile):
                self.record['profile'] = profile
                self.record['version'] = '2026.7.15' if profile == 'IFCT' else '3.8'
                metadata = Path(self.sources[3].path)
                metadata.write_text(json.dumps(self.record) + '\n')
                sources = tuple(replace(source, stage=profile) for source in self.sources)
                sources = (*sources[:3], replace(sources[3], expected_sha256=hashlib.sha256(metadata.read_bytes()).hexdigest(),
                                                 expected_size=metadata.stat().st_size))
                Path(self.sources[1].path).write_text('')
                self.sequence = 0
                self.write_event(kind='prepared')
                context = replace(self.context, binding_id=profile, stage=profile, sources=sources)
                native = Path(self.sources[0].path)
                if native.exists():
                    native.unlink()
                pending = self.service.poll(context)
                self.assertIn('Runner: Preparation', card_text(pending, width=140, now=0))
                native.write_text(f'Version {self.record["version"]}\r\nLoaded /alias/source successfully!\r\n')
                snapshot = self.service.poll(context)
                text = card_text(snapshot, width=140, now=0)
                self.assertIn('Native: source load reported', text)
                self.assertIn('2 threads', text)
                self.assertIn('Unvalidated', text)
                self.assertNotIn('%', text)
                self.assertEqual(snapshot.metrics, ())
                self.assertTrue(self.service.history(context).entries)

    def test_unbound_missing_and_unsupported_cards_retain_three_groups(self):
        for status in ('SOURCE_UNRESOLVED', 'SOURCE_CONFLICT'):
            context = replace(self.context, binding_id=status, binding_status=status)
            snapshot = self.service.poll(context)
            self.assertEqual([group.label for group in snapshot.card_groups], ['ANALYSIS', 'STAGE', 'OUTPUT'])
            self.assertFalse(snapshot.sources)
        native = Path(self.sources[0].path)
        native.write_text('Version 9.9\n')
        unsupported = self.service.poll(self.context)
        for width in (77, 80, 100, 140):
            lines = card_text(unsupported, width=width, now=0, max_lines=8, framed=True).splitlines()
            self.assertLessEqual(len(lines), 8)
            self.assertTrue(all(len(line) <= width for line in lines))
            self.assertTrue(any('ANALYSIS' in line for line in lines))
            self.assertTrue(any('OUTPUT' in line for line in lines))

    def test_projection_without_matching_validation_event_is_not_accepted(self):
        self.validation()
        records = Path(self.sources[1].path)
        lines = records.read_text().splitlines()
        record = json.loads(lines[-1])
        lines[-1] = json.dumps({**record, 'kind': 'validation_failed'})
        records.write_text('\n'.join(lines) + '\n')
        snapshot = self.service.poll(self.context)
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')


    def test_narrow_card_keeps_scope_and_output_qualification_visible(self):
        self.write_event(kind='prepared')
        pending = self.service.poll(self.context)
        text = card_text(pending, width=70, now=0, max_lines=8, framed=True)
        for value in ('Two-fragment IFCT', 'singlet', 'State 1', '2 threads', '2 frags', 'Pending evidence'):
            self.assertIn(value, text)
        native = Path(self.sources[0].path)
        native.write_text('Version 2026.7.15\nLoaded /alias/source successfully!\n')
        unvalidated = self.service.poll(self.context)
        self.assertIn('Unvalidated', card_text(unvalidated, width=70, now=0, max_lines=8, framed=True))
        self.validation()
        validated = self.service.poll(self.context)
        self.assertIn('Validator PASSED (recorded)', card_text(validated, width=70, now=0, max_lines=8, framed=True))

    def test_validator_waits_for_native_reader_frontier_before_conflict(self):
        native = Path(self.sources[0].path)
        native.write_text('Version 2026.7.15\n' + ('x' * 1000 + '\n') * 70
                          + 'Number of parallel threads: 8\n')
        self.validation()
        first = self.service.poll(self.context)
        self.assertEqual(first.scientific_validation, 'not-assessed')
        self.assertEqual(first.completeness, 'CATCHING_UP')
        second = self.service.poll(self.context)
        self.assertEqual(second.status, 'SOURCE_CONFLICT')
        self.assertEqual(second.scientific_validation, 'not-assessed')

    def test_observation_poll_does_not_refresh_last_evidence_age(self):
        self.write_event(kind='prepared')
        first = self.service.poll(self.context)
        second = self.service.poll(self.context)
        self.assertEqual(first.last_new_evidence_at, second.last_new_evidence_at)


    def test_missing_previously_observed_event_stream_revokes_current_claims(self):
        self.validation()
        self.assertEqual(self.service.poll(self.context).scientific_validation, 'validator-recorded-passed')
        Path(self.sources[1].path).unlink()
        snapshot = self.service.poll(self.context)
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')
        self.assertEqual(snapshot.read_health, 'PARTIAL')
        self.assertFalse(any(setting.name == 'runner_stage' for setting in snapshot.settings))
        self.assertTrue(any('multiwfn_events is unavailable' in message for message in snapshot.messages))

    def test_transient_native_or_validator_read_failure_recovers_exact_projection(self):
        blocked = {'multiwfn_session'}
        class TransientReader(RegularFileReader):
            def poll(self, now=None):
                if self.source.role in blocked:
                    return self._update('READ_ERROR', reason='synthetic transient failure')
                return super().poll(now)
        service = ObservationService(reader_factory=TransientReader)
        self.addCleanup(service.close)
        self.validation()
        unavailable = service.poll(self.context)
        self.assertEqual(unavailable.scientific_validation, 'not-assessed')
        blocked.clear()
        self.assertEqual(service.poll(self.context).scientific_validation, 'validator-recorded-passed')
        blocked.add('multiwfn_validation')
        missing = service.poll(self.context)
        self.assertEqual(missing.scientific_validation, 'not-assessed')
        self.assertEqual(missing.read_health, 'PARTIAL')
        blocked.clear()
        recovered = service.poll(self.context)
        self.assertEqual(recovered.scientific_validation, 'validator-recorded-passed')
        self.assertEqual(recovered.read_health, 'OK')


if __name__ == '__main__':
    unittest.main()
