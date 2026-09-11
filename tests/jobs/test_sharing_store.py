"""Schema-1 compatibility and explicit scheduling metadata mutations."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cmw.jobs.store import JobsError, Store, scheduler_defaults


class SharingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw-sharing-store-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root/'state')
        for replacement in (patch('psutil.cpu_count', return_value=8),
                            patch('psutil.virtual_memory', return_value=SimpleNamespace(total=16*1024**3))):
            replacement.start()
            self.addCleanup(replacement.stop)

    def add(self, **kwargs):
        return self.store.add(argv=['/bin/echo', 'synthetic'], cwd=self.root,
                              name='synthetic', **kwargs)

    def raw(self):
        with closing(sqlite3.connect(self.store.path)) as con, con:
            return (con.execute('SELECT * FROM meta ORDER BY key').fetchall(),
                    con.execute('SELECT * FROM jobs ORDER BY id').fetchall(),
                    con.execute('SELECT * FROM events ORDER BY id').fetchall())

    def legacy_fixture(self):
        jobs = [self.add(), self.add(hold=True)]
        # Recreate the actual prior JSON schema: same tables and all legacy
        # lifecycle fields, with no scheduling metadata or sharing anchors.
        with closing(sqlite3.connect(self.store.path)) as con, con:
            control = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
            control['schema'] = 1
            del control['scheduler']
            con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(control),))
            for job in jobs:
                job.pop('scheduling')
                job.pop('sharing_anchor')
                con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), job['id']))
        return jobs

    def test_readonly_legacy_defaults_and_transactional_migration(self):
        legacy = self.legacy_fixture()
        before = self.raw()
        observed = self.store.snapshot()
        self.assertEqual(self.raw(), before)
        self.assertEqual(observed['schema'], 2)
        self.assertEqual(observed['mode'], 'Sequential')
        self.assertEqual(observed['scheduler'], scheduler_defaults())
        for old, new in zip(legacy, observed['jobs']):
            self.assertEqual(new['scheduling']['role'], 'primary')
            self.assertFalse(new['scheduling']['allow_auxiliary'])
            self.assertIsNone(new['sharing_anchor'])
            for key in ('id', 'attempt_id', 'order', 'logs', 'status', 'enqueued_at'):
                self.assertEqual(new[key], old[key])
        with self.store.transaction():
            pass
        after = self.raw()
        self.assertEqual(after[2], before[2])
        self.assertEqual(json.loads(after[0][0][1])['schema'], 2)
        for old, (_, data) in zip(legacy, after[1]):
            migrated = json.loads(data)
            self.assertEqual({k: migrated[k] for k in old}, old)
            self.assertIn('scheduling', migrated)

    def test_interrupted_migration_rolls_back_and_future_schema_rejected(self):
        self.legacy_fixture()
        before = self.raw()
        with patch.object(Store, 'save', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                with self.store.transaction():
                    pass
        self.assertEqual(self.raw(), before)
        with closing(sqlite3.connect(self.store.path)) as con, con:
            con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps({'schema': 3}),))
        future = self.raw()
        with self.assertRaisesRegex(JobsError, 'Unsupported'):
            self.store.snapshot()
        with self.assertRaisesRegex(JobsError, 'Unsupported'):
            with self.store.transaction():
                pass
        self.assertEqual(self.raw(), future)

    def test_legacy_runtime_migration_waits_for_attempts_and_controller(self):
        jobs = self.legacy_fixture()
        for status in ('Starting', 'Run', 'Cancelling', 'Unknown'):
            with self.subTest(status=status):
                with closing(sqlite3.connect(self.store.path)) as con, con:
                    job = {**jobs[0], 'status': status}
                    con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), job['id']))
                before = self.raw()
                with patch('cmw.jobs.ownership.owner_alive', return_value=False), \
                     patch('cmw.jobs.ownership.lock_held', return_value=False):
                    self.store.snapshot()
                    self.assertEqual(self.raw(), before)
                    with self.assertRaisesRegex(JobsError, 'Legacy Jobs runtime'):
                        with self.store.transaction():
                            self.fail('Migration must not expose a mutating transaction')
                self.assertEqual(self.raw(), before)
        with closing(sqlite3.connect(self.store.path)) as con, con:
            con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(jobs[0]), jobs[0]['id']))
        for alive, locked in ((True, False), (False, True)):
            before = self.raw()
            with self.subTest(alive=alive, locked=locked), \
                 patch('cmw.jobs.ownership.owner_alive', return_value=alive), \
                 patch('cmw.jobs.ownership.lock_held', return_value=locked):
                with self.assertRaisesRegex(JobsError, 'original installation'):
                    with self.store.transaction():
                        self.fail('Live legacy controller must keep schema 1')
            self.assertEqual(self.raw(), before)
        with patch('cmw.jobs.ownership.owner_alive', return_value=False), \
             patch('cmw.jobs.ownership.lock_held', return_value=False):
            with self.store.transaction():
                pass
        self.assertEqual(json.loads(self.raw()[0][0][1])['schema'], 2)

    def test_explicit_policy_validation_and_sequential_does_not_mutate_active_job(self):
        job = self.add()
        for config in ({}, {'cpu_budget': 4}, {'cpu_budget': 9, 'memory_gib': 8, 'cpu_reserve': 1, 'min_available_gib': 1},
                       {'cpu_budget': 4, 'memory_gib': 17, 'cpu_reserve': 1, 'min_available_gib': 1},
                       {'cpu_budget': True, 'memory_gib': 8, 'cpu_reserve': 1, 'min_available_gib': 1},
                       {'cpu_budget': 4, 'memory_gib': 8, 'cpu_reserve': float('nan'), 'min_available_gib': 1}):
            before = self.raw()
            with self.subTest(config=config), self.assertRaises(JobsError):
                self.store.configure_sharing('bounded-sharing', **config)
            self.assertEqual(self.raw(), before)
        config = self.store.configure_sharing('bounded-sharing', cpu_budget=4, memory_gib=8,
                                             cpu_reserve=1, min_available_gib=1)
        self.assertEqual(config['mode'], 'bounded-sharing')
        with self.store.transaction() as con:
            job.update(status='Run', order=None, started_at=1, claim='owned')
            self.store.save(con, job)
        before_job = self.raw()[1]
        self.store.configure_sharing('sequential')
        self.assertEqual(self.raw()[1], before_job)
        self.assertEqual(self.store.snapshot()['mode'], 'Sequential')
        self.assertEqual(self.store.configure_sharing('bounded-sharing')['cpu_budget'], 4)

    def test_auxiliary_requires_explicit_contract_and_pending_metadata_rules(self):
        for invalid in ({'role': 'auxiliary'}, {'role': 'auxiliary', 'independent': True},
                        {'role': 'auxiliary', 'independent': True, 'resource_contract': 'trusted-declared'},
                        {'role': 'primary', 'allow_auxiliary': 'yes'}):
            with self.subTest(invalid=invalid), self.assertRaises(JobsError):
                self.add(**invalid)
        job = self.add(cpus=2, memory_gib=1, role='auxiliary', independent=True,
                       resource_contract='trusted-declared')
        self.assertEqual(job['scheduling']['write_scope'], str(self.root))
        self.assertEqual(job['scheduling']['role'], 'auxiliary')
        job = self.store.set_sharing(job['id'], role='primary', allow_auxiliary=True)
        with self.store.transaction() as con:
            job.update(status='Run', order=None, started_at=1)
            self.store.save(con, job)
        before = self.raw()[1][0][1]
        changed = self.store.set_sharing(job['id'], allow_auxiliary=False)
        baseline = json.loads(before)
        baseline['scheduling']['allow_auxiliary'] = False
        self.assertEqual(changed, baseline)
        for updates in ({'role': 'auxiliary'}, {'independent': False}, {'write_scope': str(self.root)}):
            with self.assertRaises(JobsError):
                self.store.set_sharing(job['id'], **updates)
        with self.store.transaction() as con:
            changed['status'] = 'Done'
            self.store.save(con, changed)
        with self.assertRaises(JobsError):
            self.store.set_sharing(job['id'], allow_auxiliary=True)

    def test_reservation_shape_numeric_validation_and_revoke(self):
        reservation = dict(observation_id='E012abc', fingerprint='a'*64, cpu_budget=2,
                           memory_gib=1, write_scope=str(self.root), created_at=1,
                           source='user supplied', allow_auxiliary=True)
        persisted = self.store.set_external_reservation(reservation)
        self.assertEqual(persisted['external_reservation'], reservation)
        for updates in ({'fingerprint': 'bad'}, {'cpu_budget': None}, {'cpu_budget': 1.5},
                        {'memory_gib': float('inf')}, {'created_at': True}, {'source': 'inferred'},
                        {'write_scope': 'relative'}, {'extra': 'unexpected'}):
            before = self.raw()
            with self.subTest(updates=updates), self.assertRaises(JobsError):
                self.store.set_external_reservation({**reservation, **updates})
            self.assertEqual(self.raw(), before)
        self.assertIsNone(self.store.set_external_reservation(None)['external_reservation'])

    def test_multiple_active_snapshot_describes_every_active_attempt(self):
        first, second, pending = self.add(), self.add(), self.add()
        with self.store.transaction() as con:
            for job in (first, second):
                job.update(status='Run', order=None, started_at=1)
                self.store.save(con, job)
        reason = self.store.snapshot()['jobs'][2]['reason']
        self.assertIn(first['display_id'], reason)
        self.assertIn(second['display_id'], reason)


if __name__ == '__main__':
    unittest.main()
