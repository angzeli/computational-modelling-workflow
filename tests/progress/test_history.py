from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import tempfile
import threading
import unittest

from cmw.progress.history import MAX_CACHED_PAGES, MAX_RECENT_ENTRIES
from cmw.progress.models import (HistoryColumn, HistoryEntry, IterationLevel, Metric,
                                 ProgressSnapshot, RunContext, SourceDescriptor, Timing)
from cmw.progress.reader import RegularFileReader
from cmw.progress.registry import Registration
from cmw.progress.service import ObservationService


class FictionalHistoryAdapter:
    """Three-level fixture dialect; indices are explicit chronological identities."""
    observations = []

    def __init__(self, context):
        self.context = context
        self.delta = []
        self.historical = True
        self.now = 0
        self.clock_id = None

    def set_observation(self, monotonic_now, *, historical, clock_id):
        self.now, self.historical, self.clock_id = monotonic_now, historical, clock_id
        self.observations.append((monotonic_now, historical, clock_id))

    def update(self, updates, now):
        for update in updates:
            for line in update.lines:
                fields = line.text.split()
                if len(fields) != 6 or fields[0] not in {'E', 'U'}:
                    continue
                _, index, parent, depth, segment, value = fields
                index, depth, segment = int(index), int(depth), int(segment)
                levels = tuple(IterationLevel(name, index, name + ' ' + str(index))
                               for name in ('geometry', 'outer solver', 'inner solver')[:depth])
                entry_id = f'{self.context.key}:{line.evidence.generation}:{index}'
                parent_id = f'{self.context.key}:{line.evidence.generation}:{parent}' if parent != '-' else None
                metric = Metric('fiction.flux', 'Flux', 'Fixture flux', 'C/s', float(value),
                                context=levels, stage=self.context.stage, segment=segment,
                                evidence=(line.evidence,))
                timing = Timing('native-loop', 'Loop', float(value), source='native',
                                context=levels, completeness='COMPLETE', evidence=(line.evidence,))
                self.delta.append(HistoryEntry(entry_id, index, levels, parent_id,
                                               native_label=f'printed {index % 3}', state='COMPLETE',
                                               coverage='COMPLETE', metrics=(metric,), timings=(timing,),
                                               evidence=(line.evidence,), binding_id=self.context.key,
                                               attempt_id=self.context.attempt_id,
                                               stage=self.context.stage, segment=segment))
        return ProgressSnapshot(self.context, 'fictional/1', status='OBSERVING',
                                completeness='OBSERVED', read_health='OK',
                                history_columns=(HistoryColumn('flux', 'Flux', 'fiction.flux', 'metric'),))

    def drain_history(self):
        result, self.delta = tuple(self.delta), []
        return result


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw history ')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'native.log'
        self.path.write_text('E 1 - 1 1 4.5\nE 2 1 2 1 3.5\nE 3 2 3 1 2.5\n')
        self.context = RunContext('binding', 'FICTIONAL', (SourceDescriptor('native', str(self.path)),),
                                  attempt_id='attempt-one', stage='response')
        FictionalHistoryAdapter.observations = []

    def service(self, **kwargs):
        service = ObservationService(registration_getter=lambda engine: Registration(FictionalHistoryAdapter), **kwargs)
        self.addCleanup(service.close)
        return service

    def settle(self, service, **kwargs):
        for _ in range(4096):
            page = service.history(self.context, **kwargs)
            if page.status != 'LOADING':
                self.assertEqual(page.status, 'READY', page.messages)
                return page
        self.fail('bounded history reconstruction did not finish')

    def rows(self, count):
        self.path.write_text(''.join(f'E {i} - 1 {1 + (i > count//2)} {i}.5\n' for i in range(1, count+1)))

    def test_generic_three_level_history_and_native_unit(self):
        service = self.service()
        roots = self.settle(service)
        self.assertEqual(len(roots.entries), 1)
        outer = self.settle(service, parent_id=roots.entries[0].entry_id)
        inner = self.settle(service, parent_id=outer.entries[0].entry_id)
        self.assertEqual([level.kind for level in inner.entries[0].context],
                         ['geometry', 'outer solver', 'inner solver'])
        self.assertEqual(inner.entries[0].metrics[0].unit, 'C/s')
        self.assertEqual(inner.columns[0].metric_id, 'fiction.flux')
        self.assertEqual(inner.entries[0].attempt_id, 'attempt-one')
        self.assertEqual(inner.coverage, 'COMPLETE')
        self.assertEqual(inner.total, 1)

    def test_pagination_coverage_filters_and_stable_page_refresh(self):
        self.rows(12)
        service = self.service()
        first = self.settle(service, limit=4)
        self.assertEqual([entry.sequence for entry in first.entries], [1, 2, 3, 4])
        self.assertTrue(first.has_more)
        self.assertEqual(first.total, 12)
        second = self.settle(service, cursor=first.next_cursor, limit=4)
        self.assertEqual([entry.sequence for entry in second.entries], [5, 6, 7, 8])
        previous = self.settle(service, cursor=second.previous_cursor, limit=4)
        self.assertEqual(previous.entries, first.entries)
        with self.path.open('a') as handle:
            handle.write('E 13 - 1 2 13.5\n')
        refreshed = self.settle(service, cursor=first.cursor, limit=4)
        self.assertEqual([entry.entry_id for entry in refreshed.entries], [entry.entry_id for entry in first.entries])
        self.assertEqual(refreshed.total, 13)
        self.assertEqual(self.settle(service, segment=2).total, 7)
        self.assertEqual(self.settle(service, stage='different').total, 0)

    def test_late_upsert_updates_same_entry_without_duplicate_count(self):
        service = self.service()
        first = self.settle(service)
        with self.path.open('a') as handle:
            handle.write('U 1 - 1 1 9.5\n')
        second = self.settle(service, cursor=first.cursor)
        self.assertEqual(second.total, 1)
        self.assertEqual(second.entries[0].entry_id, first.entries[0].entry_id)
        self.assertEqual(second.entries[0].timings[0].value, 9.5)

    def test_uncached_pages_reconstruct_with_bounded_cache_and_reads(self):
        self.rows(700)
        service = self.service(reader_factory=lambda source: RegularFileReader(source, byte_budget=4096))
        first_loading = service.history(self.context, limit=9)
        self.assertEqual(first_loading.status, 'LOADING')
        self.assertEqual(first_loading.coverage, 'PARTIAL')
        page = self.settle(service, limit=9)
        self.assertEqual(page.total, 700)
        cache = service._contexts[self.context.key].history
        self.assertLessEqual(len(cache.recent), MAX_RECENT_ENTRIES)
        first_cursor = page.cursor
        for _ in range(6):
            page = self.settle(service, cursor=page.next_cursor, limit=9)
        self.assertLessEqual(len(cache.pages), MAX_CACHED_PAGES)
        rebuilt = self.settle(service, cursor=first_cursor, limit=9)
        self.assertEqual([entry.sequence for entry in rebuilt.entries], list(range(1, 10)))
        self.assertEqual(rebuilt.total, 700)
        unchanged = self.settle(service, cursor=rebuilt.cursor, limit=9)
        self.assertEqual(service.snapshot(self.context).sources[0].bytes_read, 0)
        self.assertEqual(unchanged.entries, rebuilt.entries)
        self.assertLessEqual(cache.retained(), MAX_RECENT_ENTRIES + MAX_CACHED_PAGES*9)

    def test_native_reconstruction_after_reopen_and_observer_clock_is_new(self):
        service = self.service(monotonic_clock=lambda: 100)
        first = self.settle(service, parent_id='*')
        old_clock = service.clock_id
        service.close()
        reopened = self.service(monotonic_clock=lambda: 2)
        second = self.settle(reopened, parent_id='*', cursor=first.cursor)
        self.assertEqual([(e.entry_id, e.metrics, e.timings) for e in first.entries],
                         [(e.entry_id, e.metrics, e.timings) for e in second.entries])
        self.assertNotEqual(old_clock, reopened.clock_id)
        self.assertTrue(FictionalHistoryAdapter.observations[-1][1])

    def test_injected_monotonic_clock_separate_from_wall_and_initial_catchup(self):
        wall = iter((1000, 1))
        monotonic = iter((10, 20))
        service = self.service(clock=lambda: next(wall), monotonic_clock=lambda: next(monotonic))
        service.poll(self.context)
        service.poll(self.context)
        records = FictionalHistoryAdapter.observations
        self.assertEqual([(r[0], r[1]) for r in records], [(10, True), (20, False)])
        self.assertEqual(records[0][2], records[1][2])

    def test_replacement_truncation_and_rebinding_invalidate_cursors(self):
        service = self.service()
        first = self.settle(service)
        replacement = self.path.with_suffix('.new')
        replacement.write_text('E 1 - 1 1 1.5\n')
        replacement.replace(self.path)
        changed = service.history(self.context, cursor=first.cursor)
        self.assertEqual(changed.status, 'CURSOR_INVALID')
        current = self.settle(service)
        self.assertNotEqual(current.source_revision, first.source_revision)
        self.assertNotEqual(current.entries[0].entry_id, first.entries[0].entry_id)
        self.path.write_text('')
        self.assertEqual(service.history(self.context, cursor=current.cursor).status, 'CURSOR_INVALID')
        other = replace(self.context, binding_id='new-binding')
        self.assertEqual(service.history(other, cursor=first.cursor).status, 'CURSOR_INVALID')

    def test_close_pending_replay_retains_slot_and_discards_late_result(self):
        self.rows(700)
        entered, release = threading.Event(), threading.Event()
        made = []
        class GatedReader(RegularFileReader):
            def __init__(self, source):
                super().__init__(source)
                made.append(self)
            def poll(self, now=None):
                if len(made) > 1 and self is made[1]:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('writer gate missing')
                return super().poll(now)
        service = self.service(reader_factory=GatedReader, max_inflight=1)
        results = []
        thread = threading.Thread(target=lambda: results.append(service.history(self.context)))
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        service.release_history(self.context)
        for _ in range(20):
            self.assertEqual(service.history(replace(self.context, binding_id='other')).status, 'BUSY')
        self.assertEqual(len(made), 2)
        self.assertFalse(made[1]._closed)
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status, 'RETIRED')
        self.assertTrue(made[1]._closed)
        self.assertFalse(made[0]._closed)
        self.assertEqual(service._inflight, 0)
        self.assertIsNone(service._contexts[self.context.key].replay)

    def test_append_catchup_does_not_claim_cached_complete_history(self):
        service = self.service(reader_factory=lambda source: RegularFileReader(source, byte_budget=64))
        first = self.settle(service)
        with self.path.open('a') as handle:
            handle.write(''.join(f'E {i} - 1 1 {i}.5\n' for i in range(4, 40)))
        pending = service.history(self.context, cursor=first.cursor)
        self.assertEqual(pending.status, 'LOADING')
        self.assertEqual(pending.coverage, 'PARTIAL')
        self.assertIsNone(pending.total)

    def test_root_replacement_and_missing_sources_do_not_claim_complete(self):
        root = self.path.parent
        info = root.stat()
        source = replace(self.context.sources[0], root_path=str(root), root_identity=(info.st_dev, info.st_ino))
        self.context = replace(self.context, sources=(source,))
        service = self.service()
        first = self.settle(service)
        self.path.unlink()
        missing = service.history(self.context, cursor=first.cursor)
        self.assertEqual(missing.status, 'UNAVAILABLE')
        self.assertEqual(missing.coverage, 'PARTIAL')
        self.assertIsNone(missing.total)
        # The same declared root identity can be invalidated without reading it.
        reader = service._contexts[self.context.key].readers[0]
        reader.source = replace(source, root_identity=(info.st_dev, info.st_ino+1))
        changed = service.history(self.context, cursor=first.cursor)
        self.assertEqual(changed.status, 'CURSOR_INVALID')
        self.assertFalse(changed.entries)

    def test_adapter_value_error_is_not_mislabelled_cursor_error(self):
        class BrokenAdapter(FictionalHistoryAdapter):
            def update(self, updates, now):
                raise ValueError('invalid native observation')
        service = self.service()
        service.registration_getter = lambda engine: Registration(BrokenAdapter)
        page = service.history(self.context)
        self.assertEqual(page.status, 'READ_ERROR')
        self.assertIn('invalid native observation', page.messages[0])

    def test_native_vasp_history_survives_eviction_and_chunk_schedules(self):
        from tests.periodic.vasp.result_fixtures import write_run
        root = self.path.parent
        write_run(root, relaxation=True, evaluations=70)
        self.context = RunContext('native-history', 'VASP', tuple(
            SourceDescriptor(name.lower(), str(root/name), optional=name == 'POSCAR')
            for name in ('OUTCAR', 'OSZICAR', 'POSCAR')), attempt_id='vasp-attempt')
        schedules = []
        for budget in (512, 65536):
            service = ObservationService(reader_factory=lambda source: RegularFileReader(source, byte_budget=budget))
            self.addCleanup(service.close)
            first = self.settle(service, parent_id='*', limit=200)
            second = self.settle(service, cursor=first.next_cursor, limit=200)
            self.assertEqual(first.total, 281)
            self.assertEqual(second.total, 281)
            self.assertFalse(second.has_more)
            entries = (*first.entries, *second.entries)
            self.assertEqual(len({entry.entry_id for entry in entries}), 281)
            schedules.append([(entry.entry_id, entry.sequence, entry.coverage,
                               [(metric.semantic_id, metric.value, metric.context) for metric in entry.metrics])
                              for entry in entries])
            self.assertLessEqual(len(service._contexts[self.context.key].history.recent), MAX_RECENT_ENTRIES)
            service.close()
        self.assertEqual(schedules[0], schedules[1])

    def test_releasing_history_during_live_poll_releases_idle_replay(self):
        self.rows(700)
        entered, release = threading.Event(), threading.Event()
        made = []
        class GatedLiveReader(RegularFileReader):
            def __init__(self, source):
                super().__init__(source, byte_budget=64)
                self.calls = 0
                made.append(self)
            def poll(self, now=None):
                self.calls += 1
                if self is made[0] and self.calls == 2:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('reader gate missing')
                return super().poll(now)
        service = self.service(reader_factory=GatedLiveReader)
        self.assertEqual(service.history(self.context).status, 'LOADING')
        self.assertEqual(len(made), 2)
        thread = threading.Thread(target=lambda: service.poll(self.context))
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        service.release_history(self.context)
        self.assertFalse(made[1]._closed)
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(made[1]._closed)
        self.assertFalse(made[0]._closed)
        self.assertIsNone(service._contexts[self.context.key].replay)

    def test_cross_session_same_inode_rewrite_and_truncation_invalidate_cursor(self):
        original = self.path.read_text()
        for replacement in (original.replace('4.5', '8.5'), ''):
            with self.subTest(replacement=replacement):
                self.path.write_text(original)
                service = self.service()
                first = self.settle(service)
                before = self.path.stat()
                service.close()
                self.path.write_text(replacement)
                # Restoring mtime cannot disguise the size/ctime version guard.
                os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertEqual(self.path.stat().st_ino, before.st_ino)
                reopened = self.service()
                page = reopened.history(self.context, cursor=first.cursor)
                self.assertEqual(page.status, 'CURSOR_INVALID')
                self.assertFalse(page.entries)
                self.assertIn('Source version changed', page.messages[0])
                reopened.close()

    def test_live_append_keeps_cursor_but_cross_session_append_is_conservative(self):
        service = self.service()
        first = self.settle(service)
        with self.path.open('a') as handle:
            handle.write('E 4 - 1 1 4.5\n')
        live = self.settle(service, cursor=first.cursor)
        self.assertEqual(live.total, 2)
        service.close()
        reopened = self.service()
        self.assertEqual(reopened.history(self.context, cursor=first.cursor).status, 'CURSOR_INVALID')
        unchanged = self.settle(reopened, cursor=live.cursor)
        self.assertEqual(unchanged.total, 2)

    def test_invalid_cursor_and_limits_are_explicit(self):
        service = self.service()
        for options in ({'cursor':'not-a-cursor'}, {'cursor':'a'}, {'cursor':'W10'}, {'limit':0}, {'limit':201}):
            self.assertEqual(service.history(self.context, **options).status, 'CURSOR_INVALID')
        initial = self.settle(service, segment=1)
        self.assertEqual(service.history(self.context, cursor=initial.cursor, segment=2).status, 'CURSOR_INVALID')


if __name__ == '__main__':
    unittest.main()
