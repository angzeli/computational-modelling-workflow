from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from cmw.progress.models import (IterationLevel, Metric, MetricSeries, ProgressSnapshot,
                                 RunContext, SourceDescriptor, SourceState, SourceUpdate)
from cmw.progress.reader import RegularFileReader
from cmw.progress.registry import Registration
from cmw.progress.service import ObservationService


class FictionalAdapter:
    def __init__(self, context):
        self.context = context
        self.latest = None
        self.previous = None
        self.evidence_at = None

    def update(self, updates, now):
        for update in updates:
            for line in update.lines:
                if not line.text.startswith('DIPOLE '):
                    continue
                geometry, outer, inner, value = line.text.split()[1:]
                context = (IterationLevel('geometry', int(geometry), f'evaluation {geometry}'),
                           IterationLevel('outer', int(outer), f'outer {outer}'),
                           IterationLevel('inner', int(inner), f'inner {inner}'))
                metric = Metric('fictional.dipole', 'Dipole', 'Magnitude of fictional dipole',
                                'Debye', float(value), context=context,
                                comparison_context=context[:-1], evidence=(line.evidence,),
                                observed_at=line.observed_at)
                self.previous, self.latest = self.latest, metric
                self.evidence_at = line.observed_at
        return ProgressSnapshot(self.context, 'FICTIONAL/1', status='OBSERVING', phase='Inner evaluation',
                                iteration=self.latest.context if self.latest else (),
                                metrics=(MetricSeries(self.latest, self.previous),) if self.latest else (),
                                last_new_evidence_at=self.evidence_at)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw progress service ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'fictional.out'
        self.path.write_text('DIPOLE 1 2 3 4.5\n')
        self.context = RunContext('binding-1', 'FICTIONAL', (SourceDescriptor('native', str(self.path)),),
                                  stage='response', attempt_id='immutable-attempt')
        self.registration = Registration(FictionalAdapter)

    def service(self, **kwargs):
        service = ObservationService(registration_getter=lambda engine: self.registration if engine == 'FICTIONAL' else None,
                                     **kwargs)
        self.addCleanup(service.close)
        return service

    def test_fictional_three_nested_levels_and_different_metric_native_unit(self):
        service = self.service(clock=lambda: 10)
        first = service.poll(self.context)
        self.assertEqual([level.kind for level in first.iteration], ['geometry', 'outer', 'inner'])
        self.assertEqual(first.metrics[0].latest.unit, 'Debye')
        self.assertEqual(first.metrics[0].latest.value, 4.5)
        self.assertEqual(first.context.attempt_id, 'immutable-attempt')
        self.assertFalse(first.context.process_ownership)
        with self.path.open('a') as handle:
            handle.write('DIPOLE 1 2 4 3.2\n')
        next_snapshot = service.poll(self.context)
        self.assertEqual(next_snapshot.metrics[0].previous.value, 4.5)
        self.assertEqual(next_snapshot.metrics[0].latest.value, 3.2)
        self.assertEqual(next_snapshot.to_dict()['metrics'][0]['latest']['semantic_id'], 'fictional.dipole')

    def test_success_growth_evidence_and_metric_ages_are_distinct(self):
        clock = iter((10,20,30,40))
        service = self.service(clock=lambda: next(clock))
        first = service.poll(self.context)
        unchanged = service.poll(self.context)
        self.assertEqual(unchanged.last_success_at, 20)
        self.assertEqual(unchanged.last_file_growth_at, 10)
        self.assertEqual(unchanged.last_source_change_at, 10)
        self.assertEqual(unchanged.last_new_evidence_at, 10)
        self.assertEqual(unchanged.metrics[0].latest.observed_at, 10)
        with self.path.open('a') as handle:
            handle.write('unrelated diagnostic\n')
        diagnostic = service.poll(self.context)
        self.assertEqual(diagnostic.last_file_growth_at, 30)
        self.assertEqual(diagnostic.last_new_evidence_at, first.last_new_evidence_at)
        self.assertEqual(service.poll(self.context).sources[0].bytes_read, 0)

    def test_unknown_engine_unresolved_binding_and_source_limit(self):
        service = self.service()
        unknown = service.poll(RunContext('other', 'NO_ADAPTER'))
        self.assertEqual(unknown.status, 'ADAPTER_UNAVAILABLE')
        self.assertEqual(unknown.phase, 'Progress adapter unavailable')
        unresolved = service.poll(replace(self.context, binding_status='SOURCE_UNRESOLVED', limitations=('ambiguous stages',)))
        self.assertEqual(unresolved.status, 'SOURCE_UNRESOLVED')
        self.assertIn('ambiguous', unresolved.phase)
        self.assertEqual(service.poll(replace(self.context, sources=self.context.sources*9)).status, 'SOURCE_UNRESOLVED')

    def test_same_key_different_context_is_refused(self):
        service = self.service()
        service.poll(self.context)
        changed = replace(self.context, attempt_id='different-attempt')
        self.assertEqual(service.poll(changed).status, 'SOURCE_UNRESOLVED')
        self.assertEqual(service.poll(self.context).context.attempt_id, 'immutable-attempt')

    def test_one_inflight_per_context_and_global_limit_without_worker_replacement(self):
        entered = threading.Event()
        release = threading.Event()
        made = []
        class BlockingReader:
            def __init__(self, source):
                self.source, self.closed = source, False
                made.append(self)
            def poll(self, now):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test release missing')
                return SourceUpdate(self.source, SourceState(self.source.role, self.source.path, health='OK'))
            def close(self):
                self.closed = True
        service = self.service(max_inflight=1, reader_factory=BlockingReader)
        results = []
        thread = threading.Thread(target=lambda: results.append(service.poll(self.context)))
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        self.assertEqual(service.poll(self.context).read_health, 'BUSY')
        second = replace(self.context, binding_id='second')
        for _ in range(20):
            self.assertEqual(service.poll(second).read_health, 'BUSY')
        self.assertEqual(len(made), 1)
        service.retain((second,))
        self.assertFalse(made[0].closed)
        self.assertEqual(service.poll(self.context).status, 'RETIRED')
        self.assertEqual(service.poll(second).read_health, 'BUSY')
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status, 'RETIRED')
        self.assertTrue(made[0].closed)
        self.assertNotIn(self.context.key, service._contexts)
        self.assertEqual(service.poll(second).status, 'OBSERVING')
        self.assertEqual(len(made), 2)

    def test_detaching_does_not_wait_for_or_accept_delayed_read(self):
        entered = threading.Event()
        release = threading.Event()
        class BlockingReader(RegularFileReader):
            def poll(self, now):
                entered.set()
                release.wait(5)
                return super().poll(now)
        service = self.service(reader_factory=BlockingReader)
        results = []
        thread = threading.Thread(target=lambda: results.append(service.poll(self.context)))
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        service.close()
        self.assertEqual(service.poll(self.context).status, 'CLOSED')
        self.assertEqual(service._inflight, 1)
        release.set()
        thread.join(3)
        self.assertEqual(results[0].status, 'RETIRED')
        self.assertEqual(service._inflight, 0)
        self.assertFalse(service._contexts)

    def test_cache_is_bounded_and_views_share_reader(self):
        made = []
        def factory(source):
            reader = RegularFileReader(source)
            made.append(reader)
            return reader
        service = self.service(reader_factory=factory, max_contexts=2)
        service.poll(self.context)
        service.poll(self.context)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]._total_bytes, len(self.path.read_bytes()))
        service.poll(replace(self.context, binding_id='second'))
        service.poll(replace(self.context, binding_id='third'))
        self.assertEqual(len(service._contexts), 2)
        self.assertTrue(made[0]._closed)
        service.retain(('third',))
        self.assertEqual(tuple(service._contexts), ('third',))
        self.assertTrue(made[1]._closed)

    def test_reader_failure_keeps_previous_metrics(self):
        class FailingReader(RegularFileReader):
            def poll(self, now):
                if self._last_success is not None:
                    raise OSError('transient failure')
                return super().poll(now)
        service = self.service(reader_factory=FailingReader)
        first = service.poll(self.context)
        second = service.poll(self.context)
        self.assertEqual(second.read_health, 'READ_ERROR')
        self.assertEqual(second.metrics, first.metrics)
        self.assertIn('transient failure', second.messages[-1])

    def test_missing_and_optional_sources_preserve_supported_metrics(self):
        missing = SourceDescriptor('optional', str(self.root/'absent'), optional=True)
        service = self.service()
        result = service.poll(replace(self.context, sources=(*self.context.sources, missing)))
        self.assertEqual(result.read_health, 'OK')
        self.assertEqual(result.metrics[0].latest.value, 4.5)
        required = replace(missing, optional=False)
        result = service.poll(replace(self.context, binding_id='required', sources=(*self.context.sources, required)))
        self.assertEqual(result.read_health, 'PARTIAL')
        self.assertEqual(result.metrics[0].latest.value, 4.5)

    def test_catchup_is_explicit_and_bounded(self):
        self.path.write_text('DIPOLE 1 2 3 4.5\n'+'unparsed\n'*100)
        service = self.service(reader_factory=lambda source: RegularFileReader(source, byte_budget=32))
        result = service.poll(self.context)
        self.assertEqual(result.completeness, 'CATCHING_UP')
        self.assertTrue(result.sources[0].has_more)
        self.assertEqual(result.sources[0].bytes_read, 32)
        self.assertEqual(result.metrics[0].latest.value, 4.5)
        self.assertTrue(any('historical' in message for message in result.messages))

    def test_root_binding_change_hides_previous_metrics_until_resolved(self):
        info = self.root.stat()
        source = replace(self.context.sources[0], root_path=str(self.root), root_identity=(info.st_dev, info.st_ino+1))
        service = self.service()
        result = service.poll(replace(self.context, sources=(source,)))
        self.assertEqual(result.status, 'SOURCE_UNRESOLVED')
        self.assertFalse(result.metrics)

    def test_adapter_failure_and_invalid_return_are_bounded_errors(self):
        class BadAdapter:
            def update(self, updates, now):
                return None
        self.registration = Registration(lambda context: BadAdapter())
        result = self.service().poll(self.context)
        self.assertEqual(result.read_health, 'READ_ERROR')
        self.assertIn('invalid', result.phase)
        def broken(context):
            raise ValueError('adapter setup')
        self.registration = Registration(broken)
        result = self.service().poll(self.context)
        self.assertEqual(result.status, 'ADAPTER_ERROR')


if __name__ == '__main__':
    unittest.main()
