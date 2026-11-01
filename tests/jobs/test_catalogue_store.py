"""Synthetic catalogue histories never inspect research outputs or start jobs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.catalogue import CatalogueStore
from cmw.jobs.catalogue_models import CatalogueQuery
from cmw.jobs.store import JobsError, Store


def populate_history(store, directory, count, *, first=None):
    """One disposable transaction; preserve an optional already-registered row."""
    first = first or store.add(argv=['/bin/echo', 'synthetic'], cwd=directory, name='Synthetic 00000', hold=True)
    with store.transaction() as con:
        for index in range(1, count):
            job = {**first, 'id': index + 1, 'display_id': f'J{index + 1}.1',
                   'attempt_id': uuid4().hex, 'name': f'Synthetic {index:05d}',
                   'note': f'研究 series {index % 10}', 'order': index + 1}
            if 'progress_sources' in job:
                job.pop('progress_sources')
                job.update(argv=['/bin/echo', 'synthetic'], cwd=str(directory), engine='Command')
            con.execute('INSERT INTO jobs(id,data) VALUES (?,?)', (job['id'], json.dumps(job)))
    return first


class CatalogueStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw-catalogue-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root / 'state')
        self.catalogue = CatalogueStore(self.store)

    def add(self, **kwargs):
        return self.store.add(argv=['/bin/echo', 'synthetic'], cwd=self.root,
                              name=kwargs.pop('name', '研究 café [literal]'), hold=True, **kwargs)

    def raw(self):
        with closing(sqlite3.connect(self.store.path)) as con:
            return {name: con.execute(f'SELECT * FROM {name} ORDER BY 1').fetchall()
                    for name, in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def edit(self, job, patch, revision=0, **kwargs):
        return self.catalogue.edit(job['id'], patch, expected_revision=revision,
                                   expected_attempt_id=job['attempt_id'], **kwargs)

    def test_read_legacy_empty_and_populated_do_not_initialize_or_backfill(self):
        self.assertEqual(self.catalogue.query().coverage, 'EMPTY')
        self.assertFalse(self.store.root.exists())
        job = self.add()
        before = self.store.path.read_bytes()
        page = self.catalogue.query()
        row = self.catalogue.read(job['id'])
        self.assertEqual((page.coverage, row.revision, row.project, row.target), ('LOCAL_UNENROLLED', 0, '', None))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertNotIn('catalogue_annotations', self.raw())
        with self.store.transaction() as con:
            control = self.store.control(con)
            control['schema'] = 1
            con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(control),))
        before = self.store.path.read_bytes()
        self.assertEqual(self.catalogue.query().records[0].job_id, job['id'])
        with self.assertRaisesRegex(JobsError, 'Legacy schema-1'):
            self.edit(job, {'project': 'synthetic'})
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_metadata_alias_and_retries_preserve_original_submission_and_lifecycle(self):
        job = self.add(note='existing note')
        original = self.raw()
        request = uuid4().hex
        edited = self.edit(job, {'alias': 'Display alias', 'project': 'ZIS', 'system': 'surface',
            'series': 'S1', 'research_stage': 'human review', 'tags': ['β', 'compare']}, request_id=request)
        self.assertEqual(edited.revision, 1)
        self.assertEqual(edited.name, job['name'])
        self.assertEqual(edited.tags, ('compare', 'β'))
        after = self.raw()
        self.assertEqual(after['jobs'], original['jobs'])
        self.assertEqual(after['meta'], original['meta'])
        self.assertEqual(after['events'], original['events'])
        same = self.edit(job, {'alias': 'Display alias', 'project': 'ZIS', 'system': 'surface',
            'series': 'S1', 'research_stage': 'human review', 'tags': ['β', 'compare']}, request_id=request)
        self.assertEqual(same, edited)
        self.assertEqual(self.raw(), after)
        with self.assertRaisesRegex(JobsError, 'different operation'):
            self.edit(job, {'project': 'other'}, request_id=request)
        with self.assertRaisesRegex(JobsError, 'changed since'):
            self.edit(job, {'project': 'other'})
        with self.assertRaisesRegex(JobsError, 'Attempt identity'):
            self.catalogue.edit(job['id'], {'project': 'other'}, expected_revision=1,
                                expected_attempt_id=uuid4().hex)
        for invalid in ({'name': 'rewrite'}, {'tags': ['a', 'a']}, {'project': 'x' * 257},
                        {'alias': '\x1b[31m'}, {'tags': ['x'] * 17}):
            with self.assertRaises(JobsError):
                self.edit(job, invalid, revision=1)
        self.assertEqual(self.raw(), after)
        self.assertEqual(self.edit(job, {'project': 'ZIS'}, revision=1).revision, 1)
        self.assertEqual(self.raw(), after)

    def test_simultaneous_edits_and_stale_old_lifecycle_writer_keep_metadata(self):
        job = self.add()
        stale = dict(job)
        gate = threading.Barrier(2)
        def write(value):
            gate.wait(timeout=5)
            try:
                return self.edit(job, {'project': value})
            except JobsError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ('first', 'second')))
        self.assertEqual(sum(result is not None for result in results), 1)
        expected = self.catalogue.read(job['id'])
        # Simulates a pre-feature lifecycle writer replacing the entire old job JSON.
        stale.update(status='Done', finished_at=100, exit_code=0)
        with self.store.transaction() as con:
            con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(stale), job['id']))
        current = self.catalogue.read(job['id'])
        self.assertEqual((current.project, current.revision, current.status), (expected.project, 1, 'Done'))
        self.store.annotate(job['id'], 'new independent note')
        self.assertEqual(self.catalogue.read(job['id']).project, expected.project)

    def test_unicode_literal_query_filters_long_notes_and_global_identity(self):
        first = self.add(note='繁' * 4080 + ' trailing needle')
        second = self.add(name='regex .* $(touch nope)', engine='VASP')
        third = self.add(name='other')
        identity = self.store.enroll_queue(uuid4().hex, uuid4().hex)
        self.edit(first, {'project': 'ZIS', 'series': 'S1', 'system': 'surface',
                          'research_stage': 'review', 'tags': ['比對', 'β']})
        self.edit(second, {'project': 'ZIS', 'series': 'S2', 'tags': ['β']})
        query = CatalogueQuery(text='CAFE\u0301 [literal]', project='ZIS', series='S1',
            system='surface', research_stage='review', tags=('比對', 'β'),
            node_id=identity['node_id'], queue_id=identity['queue_id'])
        page = self.catalogue.query(query)
        self.assertEqual([row.job_id for row in page.records], [first['id']])
        self.assertEqual((page.total_count, page.matching_count, dict(page.status_counts)), (3, 1, {'Hold': 3}))
        self.assertEqual(page.records[0].target, {key: identity[key] for key in ('node_id', 'queue_id')} |
                         {'job_id': first['id'], 'attempt_id': first['attempt_id']})
        self.assertEqual(self.catalogue.query(CatalogueQuery(text='trailing needle')).matching_count, 1)
        self.assertLessEqual(len(page.records[0].note), 512)
        self.assertEqual(len(self.catalogue.read(first['id']).note), 4096)
        self.assertEqual(self.catalogue.query(CatalogueQuery(text='.*')).records[0].job_id, second['id'])
        self.assertEqual(self.catalogue.query(CatalogueQuery(text='^研究')).matching_count, 0)
        self.assertEqual(self.catalogue.query(CatalogueQuery(engine='VASP')).matching_count, 1)
        self.assertEqual(self.catalogue.query(CatalogueQuery(node_id=uuid4().hex)).matching_count, 0)
        self.assertEqual(self.catalogue.query(CatalogueQuery(status='Done')).matching_count, 0)
        self.assertEqual(self.catalogue.query(CatalogueQuery(text='other')).records[0].job_id, third['id'])

    def test_cursor_stable_pages_and_invalidates_notes_append_reorder_metadata(self):
        jobs = [self.add(name=f'Job {index}') for index in range(5)]
        base = CatalogueQuery(limit=2)
        page = self.catalogue.query(base)
        self.assertEqual([row.job_id for row in page.records], [1, 2])
        second = self.catalogue.query(replace(base, cursor=page.next_cursor))
        self.assertEqual([row.job_id for row in second.records], [3, 4])
        with self.assertRaisesRegex(JobsError, 'cursor invalidated'):
            self.catalogue.query(replace(base, text='different', cursor=page.next_cursor))
        for operation in (
            lambda: self.store.annotate(jobs[0]['id'], 'new note'),
            lambda: self.add(name='appended'),
            lambda: self.store.change(jobs[2]['id'], 'move', position=1),
            lambda: self.edit(jobs[0], {'series': 'new'}),
        ):
            before = self.catalogue.query(base)
            operation()
            with self.assertRaisesRegex(JobsError, 'cursor invalidated'):
                self.catalogue.query(replace(base, cursor=before.next_cursor))
        order = [(job['id'], job['order']) for job in self.store.snapshot()['jobs']]
        self.catalogue.query(CatalogueQuery(sort='name'))
        self.catalogue.query(CatalogueQuery(sort='id_desc'))
        self.assertEqual([(job['id'], job['order']) for job in self.store.snapshot()['jobs']], order)

    def test_cursor_rejects_relevant_lifecycle_deletion_and_identity_changes(self):
        jobs = [self.add(name=f'Job {index}') for index in range(4)]
        query = CatalogueQuery(limit=2)
        for key, value in (('status', 'Done'), ('engine', 'ORCA'), ('name', 'changed'),
                           ('display_id', 'J7.1'), ('attempt_id', uuid4().hex)):
            page = self.catalogue.query(query)
            with self.store.transaction() as con:
                job = self.store.get(con, jobs[2]['id'])
                job[key] = value
                con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), job['id']))
            with self.assertRaisesRegex(JobsError, 'cursor invalidated'):
                self.catalogue.query(replace(query, cursor=page.next_cursor))
        page = self.catalogue.query(query)
        with self.store.transaction() as con:
            con.execute('DELETE FROM jobs WHERE id=?', (jobs[-1]['id'],))
        with self.assertRaisesRegex(JobsError, 'cursor invalidated'):
            self.catalogue.query(replace(query, cursor=page.next_cursor))
        page = self.catalogue.query(query)
        replacement = self.store.path.with_name('replacement.sqlite3')
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(replacement)) as target:
            source.backup(target)
        replacement.replace(self.store.path)
        with self.assertRaisesRegex(JobsError, 'cursor invalidated'):
            self.catalogue.query(replace(query, cursor=page.next_cursor))

    def test_database_replacement_during_projection_refuses_returned_page(self):
        self.add()
        generation = self.catalogue._generation
        replacement = self.store.path.with_name('replaced-during-read.sqlite3')
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(replacement)) as target:
            source.backup(target)
        def replace_after_projection(*args):
            result = generation(*args)
            replacement.replace(self.store.path)
            return result
        with patch.object(self.catalogue, '_generation', replace_after_projection):
            with self.assertRaisesRegex(JobsError, 'changed during catalogue read'):
                self.catalogue.query()

    def test_filtered_running_and_unknown_remain_in_unfiltered_counts(self):
        first, other, unknown = self.add(name='visible'), self.add(name='hidden'), self.add(name='uncertain')
        with self.store.transaction() as con:
            for job, status in ((other, 'Run'), (unknown, 'Unknown')):
                job['status'] = status
                self.store.save(con, job)
        page = self.catalogue.query(CatalogueQuery(text='visible'))
        self.assertEqual([row.job_id for row in page.records], [first['id']])
        self.assertEqual(dict(page.status_counts), {'Hold': 1, 'Run': 1, 'Unknown': 1})
        self.assertEqual((page.total_count, page.matching_count), (3, 1))

    def test_internal_integer_target_cannot_be_redirected_by_a_display_alias(self):
        first, other = self.add(), self.add()
        with self.store.transaction() as con:
            other['display_id'] = str(first['id'])
            self.store.save(con, other)
        self.assertEqual(self.catalogue.read(first['id']).attempt_id, first['attempt_id'])
        self.edit(first, {'project': 'right target'})
        self.assertEqual(self.catalogue.read(other['id']).project, '')

    def test_control_transaction_refuses_replaced_visible_attempt_without_writes(self):
        job = self.add()
        with self.store.transaction() as con:
            replacement = {**job, 'attempt_id': uuid4().hex}
            con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(replacement), job['id']))
        before = self.store.path.read_bytes()
        for action in ('hold', 'release', 'move', 'cancel'):
            with self.subTest(action=action), self.assertRaisesRegex(JobsError, 'Attempt changed'):
                self.store.change(job['id'], action, position=1, expected='Hold',
                                  expected_attempt_id=job['attempt_id'])
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_control_immutable_internal_target_ignores_numeric_display_alias(self):
        first, other = self.add(), self.add()
        with self.store.transaction() as con:
            other['display_id'] = str(first['id'])
            self.store.save(con, other)
        released = self.store.change(first['id'], 'release', expected='Hold',
                                     expected_attempt_id=first['attempt_id'])
        self.assertEqual((released['id'], released['status']), (first['id'], 'Queue'))
        moved = self.store.change(first['id'], 'move', position=2, expected='Queue',
                                 expected_attempt_id=first['attempt_id'])
        self.assertEqual((moved['id'], moved['order']), (first['id'], 2))
        changed = self.store.change(first['id'], 'cancel', expected='Queue',
                                   expected_attempt_id=first['attempt_id'])
        self.assertEqual((changed['id'], changed['status']), (first['id'], 'Cancelled'))
        self.assertEqual(self.catalogue.read(other['id']).status, 'Hold')

    def test_missing_target_invalid_inputs_and_cancel_do_not_write(self):
        job = self.add()
        before = self.store.path.read_bytes()
        with self.assertRaises(JobsError):
            self.edit({**job, 'id': 999}, {'project': 'bad'})
        for value in (-1, True, 101):
            with self.assertRaises(ValueError):
                CatalogueQuery(limit=value)
        for query in ({'text': '\x1b'}, {'tags': ('same', 'same')}, {'status': 'Converged'}, {'schema': True}):
            with self.assertRaises(ValueError):
                CatalogueQuery(**query)
        event = threading.Event()
        event.set()
        with self.assertRaisesRegex(JobsError, 'cancelled'):
            self.catalogue.query(cancelled=event)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_page_byte_bound_can_shorten_a_requested_page_without_losing_rows(self):
        populate_history(self.store, self.root, 100)
        metadata = {key: '繁' * 256 for key in ('alias', 'project', 'system', 'series', 'research_stage')}
        metadata['tags'] = [str(index).zfill(2) + '標' * 62 for index in range(16)]
        with self.catalogue.transaction() as con:
            for job in self.store.rows(con):
                self.catalogue.bump_metadata(con, job, metadata)
        query = CatalogueQuery(limit=100)
        page = self.catalogue.query(query)
        self.assertLess(len(page.records), 100)
        self.assertLess(len(json.dumps(asdict(page)).encode()), 600 * 1024)
        identities = [row.job_id for row in page.records]
        while page.next_cursor:
            page = self.catalogue.query(replace(query, cursor=page.next_cursor))
            identities.extend(row.job_id for row in page.records)
        self.assertEqual(identities, list(range(1, 101)))

    def test_ten_thousand_metadata_only_query_is_bounded_and_reads_no_sources(self):
        populate_history(self.store, self.root, 10000)
        opened = []
        original_open = Path.open
        def watch(path, *args, **kwargs):
            opened.append(str(path))
            return original_open(path, *args, **kwargs)
        began = time.monotonic()
        with patch.object(Path, 'open', watch):
            page = self.catalogue.query(CatalogueQuery(text='研究 series 7', limit=40))
            second = self.catalogue.query(CatalogueQuery(text='研究 series 7', limit=40, cursor=page.next_cursor))
        self.assertEqual((page.total_count, page.matching_count, len(page.records), len(second.records)), (10000, 1000, 40, 40))
        self.assertLess(time.monotonic() - began, 4.0)
        self.assertEqual(opened, [])
        self.assertNotIn('catalogue_annotations', self.raw())


if __name__ == '__main__':
    unittest.main()
