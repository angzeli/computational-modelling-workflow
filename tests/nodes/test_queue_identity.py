"""Explicit target-local queue enrollment; no live jobs or host discovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.store import ACTIVE, JobsError, Store


class QueueIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-queue-identity-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'state')

    def add(self, **kwargs):
        return self.store.add(argv=['/unexecuted/fixture'], cwd=self.root,
                              name='Retained local fixture', hold=True, **kwargs)

    def enroll(self):
        return self.store.enroll_queue('node-fixture', 'darwin:hardware-fixture')

    def test_readonly_missing_and_unenrolled_do_not_initialize_or_migrate(self):
        self.assertIsNone(self.store.queue_identity())
        self.assertFalse(self.store.root.exists())
        self.store.root.mkdir(mode=0o700)
        self.assertIsNone(self.store.queue_identity())
        self.assertEqual(list(self.store.root.iterdir()), [])
        self.add()
        with self.store.transaction() as con:
            control = self.store.control(con)
            control['schema'] = 1
            self.store.set_control(con, control)
        before = self.store.path.read_bytes()
        self.assertIsNone(self.store.queue_identity())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_explicit_enrollment_is_stable_and_preserves_pending_work(self):
        job = self.add(note='User work retained')
        result = self.enroll()
        self.assertEqual(result['schema'], 1)
        self.assertRegex(result['queue_id'], r'^[a-f0-9]{32}$')
        self.assertEqual(result['state_root'], str(self.store.root))
        before = self.store.path.read_bytes()
        self.assertEqual(self.store.queue_identity(), result)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.enroll(), result)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['schema'], 4)
        current = snapshot['jobs'][0]
        for field in job:
            if field != 'reason':
                self.assertEqual(current[field], job[field], field)
        self.assertEqual(len(snapshot['events']), 2)

    def test_new_queue_can_be_explicitly_enrolled_without_jobs(self):
        result = self.enroll()
        self.assertEqual(self.store.queue_identity(), result)
        self.assertEqual(self.store.snapshot()['jobs'], [])
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_enrollment_retains_one_queue_identity(self):
        self.add()
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.enroll(), range(2)))
        self.assertEqual(values[0], values[1])
        self.assertEqual(len(self.store.snapshot()['events']), 2)

    def test_conflicting_node_or_host_is_refused_without_changes(self):
        self.enroll()
        before = self.store.path.read_bytes()
        for node, host in [('another-node', 'darwin:hardware-fixture'),
                           ('node-fixture', 'darwin:another-hardware')]:
            with self.subTest(node=node, host=host), self.assertRaisesRegex(JobsError, 'another node or host'):
                self.store.enroll_queue(node, host)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_cloned_queue_or_replaced_database_cannot_inherit_identity(self):
        self.enroll()
        clone = self.root / 'clone'
        shutil.copytree(self.store.root, clone)
        with self.assertRaisesRegex(JobsError, 'moved, replaced or copied'):
            Store(clone).queue_identity()
        replacement = self.store.root / 'replacement.sqlite3'
        shutil.copy2(self.store.path, replacement)
        replacement.replace(self.store.path)
        with self.assertRaisesRegex(JobsError, 'moved, replaced or copied'):
            self.store.queue_identity()
        with self.assertRaisesRegex(JobsError, 'moved, replaced or copied'):
            self.store.dispatch(True)

    def test_replaced_root_is_refused_even_with_original_database_inode(self):
        self.enroll()
        old_root = self.root / 'old-state'
        self.store.root.rename(old_root)
        self.store.root.mkdir(mode=0o700)
        (old_root / 'queue.sqlite3').rename(self.store.path)
        with self.assertRaisesRegex(JobsError, 'moved, replaced or copied'):
            self.store.queue_identity()

    def test_database_symlink_and_wal_are_not_read_as_enrollment(self):
        self.enroll()
        saved = self.store.root / 'saved.sqlite3'
        self.store.path.rename(saved)
        self.store.path.symlink_to(saved)
        with self.assertRaises(JobsError):
            self.store.queue_identity()
        self.store.path.unlink()
        saved.rename(self.store.path)
        with closing(sqlite3.connect(self.store.path)) as con:
            con.execute('PRAGMA journal_mode=WAL')
        before = sorted(path.name for path in self.store.root.iterdir())
        with self.assertRaisesRegex(JobsError, 'journal format'):
            self.store.queue_identity()
        self.assertEqual(sorted(path.name for path in self.store.root.iterdir()), before)

    def test_active_or_unresolved_rows_prevent_legacy_schema_promotion(self):
        row = self.add()
        for status in ACTIVE:
            with self.subTest(status=status):
                with self.store.transaction() as con:
                    job = self.store.get(con, row['id'])
                    job['status'] = status
                    self.store.save(con, job)
                with self.assertRaisesRegex(JobsError, 'no active or unresolved'):
                    self.enroll()
                self.assertIsNone(self.store.queue_identity())
                self.assertEqual(self.store.snapshot()['schema'], 2)

    def test_controller_lock_or_owner_prevents_schema_promotion(self):
        self.add()
        for symbol in ('lock_held', 'owner_alive'):
            with self.subTest(symbol=symbol), patch('cmw.jobs.ownership.' + symbol, return_value=True):
                with self.assertRaisesRegex(JobsError, 'offline controller'):
                    self.enroll()
        self.assertIsNone(self.store.queue_identity())

    def test_enrollment_record_and_schema_share_one_commit(self):
        self.add()
        with patch.object(self.store, 'event', side_effect=RuntimeError('before commit')):
            with self.assertRaisesRegex(RuntimeError, 'before commit'):
                self.enroll()
        self.assertIsNone(self.store.queue_identity())
        self.assertEqual(self.store.snapshot()['schema'], 2)
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)

    def test_schema4_requires_its_identity_record_and_strict_types(self):
        self.enroll()
        with closing(sqlite3.connect(self.store.path)) as con, con:
            raw = json.loads(con.execute("SELECT value FROM meta WHERE key='queue_identity'").fetchone()[0])
            raw['schema'] = True
            con.execute("UPDATE meta SET value=? WHERE key='queue_identity'", (json.dumps(raw),))
        with self.assertRaisesRegex(JobsError, 'malformed'):
            self.store.queue_identity()
        with closing(sqlite3.connect(self.store.path)) as con, con:
            con.execute("DELETE FROM meta WHERE key='queue_identity'")
        with self.assertRaisesRegex(JobsError, 'identity is missing'):
            self.store.queue_identity()

    def test_invalid_enrollment_does_not_create_state(self):
        for node, host in [(None, 'host'), ('node', ''), ('node', 'a' * 257), ('bad\nnode', 'host')]:
            with self.subTest(node=node, host=host), self.assertRaises(JobsError):
                self.store.enroll_queue(node, host)
        self.assertFalse(self.store.root.exists())

    def test_schema4_observation_and_suspension_keep_existing_protocol(self):
        from cmw.jobs.lifecycle import read_job, observe_execution
        from cmw.jobs.locations import discover
        from cmw.jobs.reconciliation import preview_reconciliation
        self.enroll()
        job = self.add(suspension_backend='local-command-v1')
        current, control = read_job(self.store, job['id'])
        self.assertEqual(control['schema'], 4)
        self.assertEqual(current['suspension']['protocol'], 1)
        self.assertEqual(discover(self.store, job['id']).attempt_id, job['attempt_id'])
        self.assertEqual(observe_execution(self.store, current).observed_execution, 'NOT_APPLICABLE')
        preview = preview_reconciliation(self.store, current)
        self.assertEqual(preview.proposed_action, 'none')
        self.assertTrue(any('Schema 4' in line for line in preview.rationale))
        self.assertEqual(self.store.queue_identity()['queue_id'], self.enroll()['queue_id'])

    def test_unenrolled_schema3_and_schema2_behavior_is_unchanged(self):
        self.add()
        self.assertEqual(self.store.snapshot()['schema'], 2)
        self.add(suspension_backend='local-command-v1')
        self.assertEqual(self.store.snapshot()['schema'], 3)
        self.assertIsNone(self.store.queue_identity())
