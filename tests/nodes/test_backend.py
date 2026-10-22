"""Real disposable Stores/readers behind the narrow node-local observation seam."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.store import JobsError, Store
from cmw.jobs.progress import resolve_context
from cmw.nodes.backend import BackendError, LocalBackend, job_revision
from cmw.nodes.codec import decode_typed, encode_typed
from cmw.nodes.protocol import encode, NodeError
from cmw.progress.models import HistoryPage, ProgressSnapshot, SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts


class BackendObservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cmw-node-backend-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root / 'queue')
        self.node_id = uuid4().hex
        record = self.store.enroll_queue(self.node_id, 'linux:synthetic-only')
        self.config = {'schema': 1, 'node_id': self.node_id, 'host_id': 'linux:synthetic-only',
            'host': {'platform': 'linux', 'hardware_id': 'synthetic-only'},
            'runtime': {'fingerprint': 'synthetic-runtime', 'version': '0.1.0'},
            'queues': {'research': {**record, 'state': str(self.store.root)}},
            'storages': {'scratch': {'root': str(self.root), 'storage_id': uuid4().hex}}, 'profiles': {}}
        self.readiness = {'ready': True, 'identity_ready': True, 'runtime_ready': True,
            'storage_ready': True, 'profiles_ready': True, 'identity_errors': [], 'runtime_errors': [],
            'host': self.config['host'], 'runtime': self.config['runtime'], 'queues': {'research': record},
            'storages': {'scratch': {'ready': True}}, 'errors': []}
        self.observer = ObservationService(clock=lambda: 100., monotonic_clock=lambda: 50.)
        self.observer.clock_id = 'fixed-test-observer-clock'
        self.backend = self.make_backend(observer=self.observer)

    def make_backend(self, **kwargs):
        backend = LocalBackend(self.root / 'node-config', 'research', clock=lambda: 100.,
            identity_loader=lambda _: deepcopy(self.config), identity_checker=lambda _: deepcopy(self.readiness),
            activity_projector=lambda store: store.snapshot(now=100.), boot_provider=lambda: 10., **kwargs)
        self.addCleanup(backend.close)
        return backend

    def job(self, *, scientific=False):
        if not scientific:
            return self.store.add(argv=['/bin/true'], cwd=self.root, name='Fixture', hold=True)
        output = self.root / 'scientific-output'
        output.mkdir(exist_ok=True)
        texts = native_texts()
        for name in ('OUTCAR', 'OSZICAR'):
            (output / name).write_text(texts[name])
        return self.store.add(argv=['/bin/true'], cwd=self.root, name='Synthetic observation', engine='VASP',
            hold=True, progress_sources=(SourceDescriptor('outcar', str(output / 'OUTCAR')),
                                        SourceDescriptor('oszicar', str(output / 'OSZICAR'))))

    def call(self, operation, job=None, params=None):
        return self.backend.call(operation, params, self.backend.target(job) if job else None)['result']

    def test_status_lookup_show_global_identity_and_read_only_database(self):
        job = self.job()
        before = self.store.path.read_bytes()
        status = self.call('status')
        row = status['state']['jobs'][0]
        self.assertEqual(row['target'], self.backend.target(job))
        self.assertEqual(row['revision'], job_revision(job))
        target = self.call('lookup', params={'id': job['display_id']})['target']
        self.assertEqual(self.backend.call('show', target=target)['result']['job']['attempt_id'], job['attempt_id'])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse((self.root / 'node-config').exists())
        encode(self.backend.call('status'))  # actual protocol accepts JSON-only envelope.

    def test_same_display_id_on_other_node_queue_cannot_cross_target(self):
        job = self.job()
        target = self.backend.target(job)
        for key in ('node_id', 'queue_id', 'attempt_id'):
            altered = {**target, key: uuid4().hex}
            with self.subTest(key=key), self.assertRaises((NodeError, JobsError)):
                self.backend.call('show', target=altered)
        for altered in ({'job_id': job['id']}, {**target, 'job_id': True}, {**target, 'extra': 'field'}):
            with self.assertRaises(NodeError):
                self.backend.call('show', target=altered)

    def test_capabilities_storage_failure_keeps_independent_observation(self):
        self.readiness.update(ready=False, storage_ready=False, errors=['Scratch unavailable'])
        self.job()
        capabilities = self.call('capabilities')
        self.assertFalse(capabilities['readiness']['storage_ready'])
        self.assertEqual(len(self.call('status')['state']['jobs']), 1)

    def test_host_runtime_and_reboot_changes_refused(self):
        for field in ('identity_ready', 'runtime_ready'):
            self.readiness[field] = False
            with self.assertRaises(NodeError):
                self.call('status')
            self.readiness[field] = True
        self.backend._boot_provider = lambda: 11.
        with self.assertRaisesRegex(NodeError, 'identity changed'):
            self.call('capabilities')

    def test_queue_replacement_fails_read_only(self):
        original = self.store.path.read_bytes()
        replacement = self.store.path.with_suffix('.replacement')
        replacement.write_bytes(original)
        replacement.replace(self.store.path)
        with self.assertRaises((NodeError, JobsError)):
            self.call('status')
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_missing_queue_is_not_initialized(self):
        self.store.path.unlink()
        with self.assertRaises(NodeError):
            self.call('status')
        self.assertFalse(self.store.path.exists())

    def test_pagination_and_parameters_bounded(self):
        for _ in range(3):
            self.job()
        page = self.call('status', params={'limit': 2})
        self.assertEqual((len(page['state']['jobs']), page['job_count'], page['has_more']), (2, 3, True))
        second = self.call('status', params={'offset': 2, 'limit': 2})
        self.assertEqual((len(second['state']['jobs']), second['has_more']), (1, False))
        for params in ({'limit': 1001}, {'offset': -1}, {'limit': True}, {'unknown': 1}):
            with self.assertRaises(NodeError):
                self.call('status', params=params)

    def test_progress_wire_equals_local_adapter_and_reuses_readers(self):
        job = self.job(scientific=True)
        before = self.store.path.read_bytes()
        first = decode_typed(self.call('progress', job)['snapshot'], ProgressSnapshot)
        native = ObservationService(clock=lambda: 100., monotonic_clock=lambda: 50.)
        native.clock_id = 'fixed-test-observer-clock'
        try:
            expected = native.poll(resolve_context(self.store, job['id']))
        finally:
            native.close()
        self.assertEqual(first, expected)
        encode(self.backend.call('progress', target=self.backend.target(job)))
        second = decode_typed(self.call('progress', job)['snapshot'], ProgressSnapshot)
        self.assertEqual([state.total_bytes_read for state in first.sources],
                         [state.total_bytes_read for state in second.sources])
        self.assertTrue(all(state.bytes_read == 0 for state in second.sources))
        self.assertEqual(len(self.observer._contexts), 1)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_history_and_release_use_same_bounded_observer(self):
        job = self.job(scientific=True)
        self.call('progress', job)
        result = self.call('history', job, {'limit': 10})
        page = decode_typed(result['page'], HistoryPage)
        self.assertEqual(page.context.attempt_id, job['attempt_id'])
        self.assertEqual(len(self.observer._contexts), 1)
        self.assertEqual(self.call('history.release', job), {'released': True})
        with self.assertRaises(NodeError):
            self.call('history', job, {'limit': 201})

    def test_source_validation_activation_remain_node_local_and_target_bound(self):
        job = self.job(scientific=True)
        before = self.store.path.read_bytes()
        inspection = self.call('sources.inspect', job)
        self.assertFalse(inspection['session_active'])
        paths = self.root / 'scientific-output'
        sources = [encode_typed(SourceDescriptor(role, str(paths / filename)))
                   for role, filename in (('outcar', 'OUTCAR'), ('oszicar', 'OSZICAR'))]
        validation = self.call('sources.validate', job, {'sources': sources})
        other = self.job()
        before_selection = self.store.path.read_bytes()
        with self.assertRaisesRegex(NodeError, 'another target'):
            self.call('sources.activate', other, {'validation_id': validation['validation_id'], 'confirmed': True})
        self.call('sources.activate', job, {'validation_id': validation['validation_id'], 'confirmed': True})
        self.assertTrue(self.call('sources.inspect', job)['session_active'])
        snapshot = decode_typed(self.call('progress', job)['snapshot'], ProgressSnapshot)
        self.assertEqual(snapshot.context.association, 'user-declared:session-local')
        self.call('sources.clear', job)
        self.assertFalse(self.call('sources.inspect', job)['session_active'])
        # Only our explicit second fixture registration changed the database; sources never persist metadata.
        from cmw.jobs.locations import _read_job
        current, _ = _read_job(self.store, job['id'])
        self.assertEqual(current['progress_sources'], job['progress_sources'])
        self.assertEqual(self.store.path.read_bytes(), before_selection)

    def test_reconnect_does_not_reuse_old_source_validation(self):
        job = self.job(scientific=True)
        sources = [encode_typed(SourceDescriptor(role, str(self.root / 'scientific-output' / filename)))
                   for role, filename in (('outcar', 'OUTCAR'), ('oszicar', 'OSZICAR'))]
        value = self.call('sources.validate', job, {'sources': sources})
        other = self.make_backend()
        self.assertNotEqual(other.session_id, self.backend.session_id)
        self.assertEqual(other.clock_id, self.backend.clock_id)
        with self.assertRaises(NodeError):
            other.call('sources.activate', {'validation_id': value['validation_id'], 'confirmed': True}, self.backend.target(job))

    def test_locations_are_typed_node_paths_without_local_open_authority(self):
        job = self.job(scientific=True)
        locations = self.call('locations', job)['locations']
        directory = next(item for item in locations if item['role'] == 'working-directory')
        self.assertEqual(directory['node_id'], self.node_id)
        self.assertEqual(directory['registered_root'], str(self.root))
        self.assertEqual(directory['relative_path'], '.')
        self.assertFalse(directory['local_open_permitted'])
        info = self.root.stat()
        identity = [info.st_dev, info.st_ino]
        self.assertEqual(directory['target_identity'], identity)
        self.assertEqual(directory['directory_identity'], identity)
        self.assertEqual(directory['storage_alias'], 'scratch')
        self.readiness['storages']['scratch']['ready'] = False
        unavailable = self.call('locations', job)['locations']
        self.assertTrue(all(item['accessibility'] == 'STORAGE_UNAVAILABLE'
                            for item in unavailable if item['storage_id']))

    def test_logs_are_bounded_and_never_read_fifo(self):
        job = self.job()
        path = Path(job['logs']['stdout'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'a' * 70000)
        result = self.call('logs', job, {'bytes': 128})
        self.assertEqual((result['bytes'], len(result['text'])), (128, 128))
        with self.assertRaises(NodeError):
            self.call('logs', job, {'bytes': 65537})
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(NodeError, 'regular file'):
            self.call('logs', job)

    def test_observe_and_close_never_signal_or_start_worker(self):
        job = self.job()
        before = self.store.path.read_bytes()
        with patch('os.kill', side_effect=AssertionError('No signals permitted')):
            result = self.call('observe', job)
            self.backend.close()
        self.assertEqual(result['execution']['attempt_id'], job['attempt_id'])
        self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaisesRegex(NodeError, 'closed'):
            self.call('status')

    def test_busy_and_unsupported_mutations_refused(self):
        self.backend._lock.acquire()
        try:
            with self.assertRaisesRegex(NodeError, 'in-flight'):
                self.call('status')
        finally:
            self.backend._lock.release()
        for operation in ('start', 'cancel', 'submit', 'open', 'eval'):
            with self.assertRaises(NodeError):
                self.call(operation)

    def test_sequence_and_owning_node_clock_metadata(self):
        first = self.backend.call('capabilities')
        second = self.backend.call('capabilities', request_id=uuid4().hex)
        self.assertEqual(second['sequence'], first['sequence'] + 1)
        self.assertEqual(first['observed_at'], 100.)
        self.assertEqual(first['runtime'], self.config['runtime'])
        self.assertEqual(first['session_id'], second['session_id'])
