"""Human annotations remain separate from authoritative execution metadata."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from cmw.jobs.store import ACTIVE, PENDING, TERMINAL, JobsError, NOTE_FIELDS, Store


class NotesStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw-notes-store-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root / 'state')

    def add(self, **kwargs):
        return self.store.add(argv=['/bin/echo', 'synthetic'], cwd=self.root,
                              name='同名 [literal]', **kwargs)

    def raw(self):
        with closing(sqlite3.connect(self.store.path.as_uri() + '?mode=ro', uri=True)) as con:
            return {table: con.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                    for table in ('meta', 'jobs', 'events')}

    def stored(self, job):
        with closing(sqlite3.connect(self.store.path.as_uri() + '?mode=ro', uri=True)) as con:
            return json.loads(con.execute('SELECT data FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])

    def test_missing_fields_default_without_readonly_backfill(self):
        job = self.add()
        with closing(sqlite3.connect(self.store.path)) as con, con:
            for field in NOTE_FIELDS:
                job.pop(field)
            con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), job['id']))
        before = self.raw()
        observed = self.store.snapshot(now=100)['jobs'][0]
        self.assertEqual(tuple(observed[key] for key in NOTE_FIELDS), ('', 0, None))
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.store.annotate(job['id'], '')['note_revision'], 0)
        self.assertEqual(self.raw(), before)

    def test_enqueue_replace_clear_and_normalized_noop(self):
        job = self.add(note='繁體中文 [literal]\r\nline\r\ttab  e\u0301 😀')
        self.assertEqual(job['note'], '繁體中文 [literal]\nline\n\ttab  e\u0301 😀')
        self.assertEqual((job['note_revision'], job['note_updated_at']), (0, None))
        with patch('cmw.jobs.store.time.time', return_value=123):
            updated = self.store.annotate(job['display_id'], 'qphxolbn\nconverged; $(touch never)')
        self.assertEqual((updated['note_revision'], updated['note_updated_at']), (1, 123))
        before = self.raw()
        self.store.annotate(job['id'], 'qphxolbn\r\nconverged; $(touch never)')
        self.assertEqual(self.raw(), before)
        cleared = self.store.annotate(job['id'], '')
        self.assertEqual((cleared['note'], cleared['note_revision']), ('', 2))
        self.assertEqual([row[3] for row in self.raw()['events']],
                         ['Enqueued at order 1', 'Note updated', 'Note cleared'])
        self.assertEqual(Store(self.store.root).snapshot()['jobs'][0]['note'], '')

    def test_validation_rejects_without_mutation_and_preserves_unicode(self):
        job = self.add()
        before = self.raw()
        invalid = [None, 1, True, [], {}, 'x' * 4097, '\0', '\x1b[31m', '\x7f',
                   '\x85', '\u202e', '\u200d', '\ud800']
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(JobsError):
                    self.store.annotate(job['id'], value)
                with self.assertRaises(JobsError):
                    self.add(note=value)
                self.assertEqual(self.raw(), before)
        text = '繁' * 4096
        updated = self.store.annotate(job['id'], text)
        self.assertEqual(updated['note'], text)
        updated = self.store.annotate(job['id'], ' \t\n[bold] "quotes" e\u0301 😀 ')
        self.assertEqual(updated['note'], ' \t\n[bold] "quotes" e\u0301 😀 ')

    def test_every_status_preserves_identity_execution_controls_and_receipts(self):
        job = self.add(cpus=2, memory_gib=3, env={'EXAMPLE': 'data'})
        receipt = self.store.root / 'attempts' / job['attempt_id'] / 'payload-exit.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"exit_code": 0, "recorded_at": 100}\n')
        receipt_bytes = receipt.read_bytes()
        for status in sorted(PENDING | ACTIVE | TERMINAL):
            with self.subTest(status=status):
                with self.store.transaction() as con:
                    current = self.store.get(con, job['id'])
                    current.update(status=status, claim='claim', worker={'fixture': 'worker'},
                                   group={'fixture': 'group'}, started_at=10, finished_at=20,
                                   exit_code=0, signal=None,
                                   layout={'target_id': 'untouched', 'attempt_id': 'scientific'},
                                   scientific_evidence={'converged': False})
                    self.store.save(con, current)
                before = self.stored(job)
                controls = self.raw()['meta']
                updated = self.store.annotate(job['id'], f'Human annotation in {status}')
                self.assertEqual({k: v for k, v in updated.items() if k not in NOTE_FIELDS},
                                 {k: v for k, v in before.items() if k not in NOTE_FIELDS})
                self.assertEqual(self.raw()['meta'], controls)
                self.assertEqual(receipt.read_bytes(), receipt_bytes)
        self.assertFalse(self.store.snapshot()['controller']['online'])

    def test_missing_external_and_unsafe_state_never_created_or_repaired(self):
        missing = Store(self.root / 'not-created')
        for target in ('J9.1', 'E123abc'):
            with self.assertRaises(JobsError):
                missing.annotate(target, 'test')
        self.assertFalse(missing.root.exists())
        empty = self.root / 'existing-empty'
        empty.mkdir(mode=0o700)
        with self.assertRaises(JobsError):
            Store(empty).annotate('J1.1', 'test')
        self.assertEqual(list(empty.iterdir()), [])
        job = self.add()
        before = self.raw()
        for target in ('J9.1', 'E123abc'):
            with self.assertRaises(JobsError):
                self.store.annotate(target, 'test')
            self.assertEqual(self.raw(), before)
        self.store.root.chmod(0o755)
        try:
            with self.assertRaisesRegex(JobsError, 'Unsafe Jobs state directory'):
                self.store.annotate(job['id'], 'test')
            self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(self.raw(), before)
        finally:
            self.store.root.chmod(0o700)

    def test_editor_revision_attempt_and_display_rename_tokens(self):
        first, other = self.add(), self.add()
        updated = self.store.annotate(first['id'], 'first editor', expected_revision=0,
                                      expected_attempt_id=first['attempt_id'])
        before = self.raw()
        with self.assertRaisesRegex(JobsError, 'Note changed'):
            self.store.annotate(first['id'], 'second editor', expected_revision=0)
        with self.assertRaisesRegex(JobsError, 'Note changed'):
            self.store.annotate(first['id'], 'first editor', expected_revision=0)
        with self.assertRaisesRegex(JobsError, 'Attempt identity differs'):
            self.store.annotate(first['id'], 'changed', expected_revision=1,
                                expected_attempt_id=other['attempt_id'])
        for revision in (-1, True, 1.0, '1'):
            with self.assertRaisesRegex(JobsError, 'nonnegative integer'):
                self.store.annotate(first['id'], 'changed', expected_revision=revision)
        self.assertEqual(self.raw(), before)
        self.store.rename_id(first['id'], 'A1.1')
        self.store.annotate(first['id'], 'same editor after rename', expected_revision=1,
                            expected_attempt_id=updated['attempt_id'])
        self.assertEqual(self.stored(first)['display_id'], 'A1.1')
        self.assertEqual(self.stored(other)['note'], '')
        with self.store.transaction() as con:
            current = self.store.get(con, first['id'])
            current['status'] = 'Done'
            self.store.save(con, current)
        self.assertEqual(self.store.annotate(first['id'], 'after Done', expected_revision=2,
                                             expected_attempt_id=first['attempt_id'])['status'], 'Done')

    def test_simultaneous_editors_have_one_winner(self):
        job = self.add()
        gate = threading.Barrier(2)
        def edit(text):
            gate.wait(timeout=5)
            try:
                return self.store.annotate(job['id'], text, expected_revision=0)['note']
            except JobsError as exc:
                self.assertIn('Note changed', str(exc))
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ('first', 'second')))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertIn(self.stored(job)['note'], results)
        self.assertEqual(self.stored(job)['note_revision'], 1)

    def test_stale_lifecycle_writes_preserve_notes_and_annotations_keep_new_lifecycle(self):
        job = self.add()
        stale = dict(job)
        self.store.annotate(job['id'], 'newer note')
        with self.store.transaction() as con:
            stale.update(status='Done', finished_at=100, exit_code=0)
            self.store.save(con, stale)
            control = self.store.control(con)
            control.update(dispatch=False, reason='Lifecycle pause')
            self.store.set_control(con, control)
        self.assertEqual(self.stored(job)['note'], 'newer note')
        self.assertEqual(stale['note_revision'], 1)
        controls = self.raw()['meta']
        updated = self.store.annotate(job['id'], 'latest', expected_revision=1)
        self.assertEqual((updated['status'], updated['finished_at'], updated['exit_code']), ('Done', 100, 0))
        self.assertEqual(self.raw()['meta'], controls)
        # The pre-feature writer preserved unknown fields when it reread the
        # current full row in its transaction, but lacked the stale-write guard.
        with self.store.transaction() as con:
            current = json.loads(con.execute('SELECT data FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
            current['reason'] = 'prior runtime fresh lifecycle write'
            con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(current), job['id']))
        self.assertEqual(self.stored(job)['note'], 'latest')

    def test_legacy_active_migration_safeguard_still_blocks_annotation(self):
        job = self.add()
        with self.store.transaction() as con:
            current = self.store.get(con, job['id'])
            current['status'] = 'Unknown'
            self.store.save(con, current)
            control = self.store.control(con)
            control['schema'] = 1
            self.store.set_control(con, control)
        before = self.raw()
        with self.assertRaisesRegex(JobsError, 'Legacy Jobs runtime still active'):
            self.store.annotate(job['id'], 'do not migrate')
        self.assertEqual(self.raw(), before)
