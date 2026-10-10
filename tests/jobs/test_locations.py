from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cmw.core.execution_layout import ExecutionLayout
from cmw.jobs.locations import LocationDescriptor, discover, inspect_path, read_location_metadata
from cmw.jobs.store import JobsError, Store


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw files 繁體 ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'state')
        self.job = self.store.add(argv=['/unknown/wrapper', '--output', '/do/not/guess'],
                                  cwd=self.root, name='ignored name', engine='fictional', hold=True)

    def discover(self, **kwargs):
        return discover(self.store, self.job['id'], resolvers=(), **kwargs)

    def test_unknown_wrapper_preserves_working_directory_and_expected_logs(self):
        before = self.store.path.read_bytes()
        with patch('subprocess.run', side_effect=AssertionError('No execution')):
            result = self.discover()
        self.assertEqual(result.status, 'LOCATION_UNKNOWN')
        self.assertIsNone(result.preferred_key)
        working = next(item for item in result.locations if item.role == 'working-directory')
        self.assertEqual(working.path, str(self.root))
        self.assertEqual(working.accessibility, 'AVAILABLE')
        self.assertFalse(working.process_ownership)
        self.assertEqual(working.scientific_validation, 'not-assessed')
        self.assertTrue(all(item.accessibility == 'NOT_CREATED_YET' for item in result.locations
                            if item.role == 'operational-logs'))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot()['controller']['online'], False)

    def test_identical_display_ids_in_different_states_are_distinct(self):
        other = Store(self.root / 'other-state')
        second = other.add(argv=['opaque'], cwd=self.root, name='same')
        # Even a copied immutable attempt UUID does not erase the selected state.
        with other.transaction() as con:
            second['attempt_id'] = self.job['attempt_id']
            Store.save(con, second)
        left = self.discover().locations[0]
        right = discover(other, second['id'], resolvers=()).locations[0]
        self.assertEqual(second['display_id'], self.job['display_id'])
        self.assertNotEqual(left.key, right.key)

    def test_future_resolver_and_caller_descriptor_need_no_engine_dispatch(self):
        output = self.root / '輸出 $(touch injected); with spaces'
        output.mkdir()
        def fictional(job):
            self.assertEqual(job['engine'], 'fictional')
            return [LocationDescriptor('output', output, origin='fictional-resolver')]
        result = discover(self.store, self.job['id'], resolvers=(fictional,))
        selected = next(item for item in result.locations if item.key == result.preferred_key)
        self.assertEqual(selected.path, str(output))
        self.assertEqual(selected.accessibility, 'AVAILABLE')
        explicit = self.discover(descriptors=[LocationDescriptor('output', output)])
        self.assertEqual(explicit.status, 'AVAILABLE')
        self.assertFalse((self.root / 'injected').exists())

    def test_missing_output_remains_preferred_without_input_fallback(self):
        result = self.discover(descriptors=[LocationDescriptor('output', self.root/'missing'),
                                           LocationDescriptor('inputs', self.root)])
        selected = next(item for item in result.locations if item.key == result.preferred_key)
        self.assertEqual(selected.role, 'output')
        self.assertEqual(result.status, 'NOT_FOUND')
        expected = self.discover(descriptors=[LocationDescriptor('output', self.root/'new', expected_creation=True)])
        self.assertEqual(expected.status, 'NOT_CREATED_YET')

    def test_multistage_requires_chooser_even_same_parent(self):
        result = self.discover(descriptors=[LocationDescriptor('output', self.root, stage='opt'),
                                           LocationDescriptor('output', self.root, stage='sp')])
        self.assertEqual(result.status, 'AMBIGUOUS')
        self.assertIsNone(result.preferred_key)

    def test_conflicting_output_locations_are_ambiguous(self):
        result = self.discover(descriptors=[LocationDescriptor('output', self.root),
                                           LocationDescriptor('output', self.root/'second')])
        self.assertEqual(result.status, 'AMBIGUOUS')

    def test_terminal_job_missing_log_is_missing_not_not_created(self):
        with self.store.transaction() as con:
            job = Store.get(con, self.job['id'])
            job.update(status='Done', started_at=1, finished_at=2)
            Store.save(con, job)
        result = self.discover(descriptors=[LocationDescriptor('output', self.root)])
        self.assertEqual(result.status, 'AVAILABLE')
        self.assertTrue(all(item.accessibility == 'NOT_FOUND' for item in result.locations
                            if item.role == 'operational-logs'))

    def test_layout_identity_is_verified_and_navigation_needs_no_receipt(self):
        layout = ExecutionLayout(self.root, 'system', 'static', 'a'*64, 'attempt_001')
        layout.working_directory.mkdir(parents=True)
        layout.layout_path.write_text(json.dumps(layout.to_dict()))
        job = self.store.add(argv=['opaque'], cwd=layout.working_directory, name='layout', layout=layout.layout_path)
        result = discover(self.store, job['id'], resolvers=())
        self.assertEqual(result.status, 'AVAILABLE')
        self.assertEqual(next(item for item in result.locations if item.key == result.preferred_key).stage, 'static')
        bad = layout.to_dict()
        bad['attempt_identifier'] = 'attempt_002'
        layout.layout_path.write_text(json.dumps(bad))
        rejected = discover(self.store, job['id'], resolvers=())
        self.assertEqual(rejected.status, 'LOCATION_UNKNOWN')
        self.assertTrue(rejected.diagnostics)

    def test_declared_file_exposes_source_and_same_output_parent_is_unique(self):
        source = self.root / 'orca.out'
        source.write_text('not scientific evidence')
        result = self.discover(descriptors=[LocationDescriptor('output', source, kind='file')])
        selected = next(item for item in result.locations if item.key == result.preferred_key)
        self.assertEqual(selected.path, str(source))
        self.assertEqual(selected.kind, 'file')

    def test_attempt_mismatch_and_uninitialized_state_do_not_mutate(self):
        with self.assertRaisesRegex(JobsError, 'Attempt identity'):
            self.discover(expected_attempt_id='wrong')
        missing = Store(self.root / 'absent-state')
        with self.assertRaises(JobsError):
            discover(missing, 'J1.1', resolvers=())
        self.assertFalse(missing.root.exists())

    def test_legacy_schema_read_does_not_migrate(self):
        with sqlite3.connect(self.store.path) as con:
            raw = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
            raw['schema'] = 1
            con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(raw),))
        before = self.store.path.read_bytes()
        self.discover()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_permission_and_unavailable_volume_are_distinct(self):
        with patch('cmw.jobs.locations.os.scandir', side_effect=PermissionError('blocked')):
            self.assertEqual(inspect_path(self.root, 'directory')[1], 'PERMISSION_DENIED')
        with patch('pathlib.Path.stat', side_effect=FileNotFoundError):
            self.assertEqual(inspect_path(Path('/Volumes/isolated-missing-volume/run'), 'directory')[1],
                             'VOLUME_UNAVAILABLE')

    def test_uri_rejected_and_output_collision_not_modified(self):
        source = self.root / 'existing-output'
        source.write_text('preserve')
        before = self.store.path.read_bytes()
        result = self.discover(descriptors=[LocationDescriptor('output', source),
                                           LocationDescriptor('records', 'https://example.invalid/data')])
        self.assertTrue(all(item.accessibility == 'LOCATION_UNKNOWN' for item in result.locations
                            if item.role in {'output', 'records'}))
        self.assertEqual(source.read_text(), 'preserve')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_descriptor_metadata_is_bounded_and_unambiguous(self):
        for stage in ('line\nbreak', 'x' * 257, 42):
            with self.assertRaises(ValueError):
                LocationDescriptor('output', self.root, stage=stage)
        with self.assertRaises(ValueError):
            LocationDescriptor('output', self.root, expected_creation='yes')

    def test_unreadable_source_not_mislabeled_available(self):
        source = self.root / 'private.out'
        source.write_text('private')
        with patch('cmw.jobs.locations.os.access', return_value=False):
            self.assertEqual(inspect_path(source, 'file')[1], 'PERMISSION_DENIED')

    def test_bounded_metadata_rejects_nonobject_and_oversize(self):
        record = self.root / 'record.json'
        record.write_text('[1]')
        with self.assertRaisesRegex(ValueError, 'JSON object'):
            read_location_metadata(record)
        record.write_text('{"value": "too large"}')
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            read_location_metadata(record, max_bytes=5)
        record.write_text('{"valid": true}')
        self.assertEqual(read_location_metadata(record), {'valid': True})


if __name__ == '__main__':
    unittest.main()
