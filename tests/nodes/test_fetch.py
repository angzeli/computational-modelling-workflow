"""Selected replica fetch with real files/Store and synthetic lifecycle fixtures."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.core.provenance import stable_hash
from cmw.jobs.locations import _read_job, discover
from cmw.jobs.store import Store
from cmw.core.preparation_publication import _rename_exclusive
from cmw.nodes.fetch import (prepare_fetch, complete_fetch, download_fetch, download_local_fetch,
                             normalize_fetch_spec, NORMAL_FILE_BYTES, LARGE_FILE_BYTES, _capture)
from cmw.nodes.protocol import NodeError
from cmw.nodes.storage import identify_storage, check_storage
from tests.nodes.test_storage import volume
from tests.periodic.vasp.handoff_case import wait_for_fixture_sources


class FetchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-fetch-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.storage = self.root / 'storage [selected] "quoted"'
        self.storage.mkdir(mode=0o700)
        self.work = self.storage / 'run'
        self.work.mkdir()
        self.source = self.work / 'summary.txt'
        self.source.write_bytes(b'Invented observed output\n')
        self.node_root = self.root / 'node'
        self.node_root.mkdir(mode=0o700)
        node_id = uuid4().hex
        self.store = Store(self.root / 'queue')
        queue = self.store.enroll_queue(node_id, 'fixture-host')
        self.job = self.store.add(argv=['/usr/bin/true'], cwd=self.work, name='Never executed fixture', hold=True)
        self.backend = SimpleNamespace(config_root=self.node_root, store=self.store,
            config={'node_id': node_id, 'queues': {'research': queue},
                    'storages': {'scratch': identify_storage(self.storage, probe=volume)}})
        self.backend.target = lambda job: {'node_id': node_id, 'queue_id': queue['queue_id'],
                                            'job_id': job['id'], 'attempt_id': job['attempt_id']}
        self.backend._job = lambda target: _read_job(self.store, target['job_id'])[0]
        found = discover(self.store, self.job['id'])
        location = next(item for item in found.locations if item.role == 'working-directory')
        self.spec = {'schema': 1, 'storage': 'scratch', 'purpose': 'summaries', 'capture': 'observation',
            'large_files': False, 'files': [{'location_id': location.key, 'relative': 'summary.txt', 'path': 'analysis/summary.txt'}]}
        for module in ('cmw.nodes.fetch', 'cmw.nodes.transfer'):
            check = patch(module + '.check_storage', side_effect=lambda value: check_storage(value, probe=volume))
            check.start()
            self.addCleanup(check.stop)
        self.destination = self.root / 'new replica'
        self.fetch_id = uuid4().hex

    def capture(self, spec=None, **kwargs):
        wait_for_fixture_sources([self.source])
        return prepare_fetch(self.backend, self.job, spec or self.spec, self.fetch_id, **kwargs)

    def terminal(self):
        # Deliberate isolated Store fixture; this is not completion evidence.
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job['status'] = 'Done'
            self.store.save(con, job)
        self.job = _read_job(self.store, self.job['id'])[0]

    def test_observational_capture_fetch_replica_and_origin_lineage(self):
        before = self.store.path.read_bytes()
        receipt = self.capture()
        self.assertEqual(receipt['manifest']['qualification'], 'INCOMPLETE_OBSERVATIONAL')
        self.assertEqual(receipt['target'], self.backend.target(self.job))
        result = download_local_fetch(receipt, self.destination)
        self.assertEqual(result['status'], 'FETCHED')
        self.assertEqual((self.destination / 'analysis/summary.txt').read_bytes(), self.source.read_bytes())
        replica = json.loads((self.destination / 'fetch-manifest.json').read_text())
        self.assertFalse(replica['original_location_authority'])
        self.assertEqual(replica['origin'], self.backend.target(self.job))
        evidence = replica['manifest']['files'][0]['source']
        self.assertEqual(evidence['node_path'], str(self.source))
        self.assertEqual(evidence['source_before'][:2], [self.source.stat().st_dev, self.source.stat().st_ino])
        self.assertNotEqual((self.destination / 'analysis/summary.txt').stat().st_ino, self.source.stat().st_ino)
        self.assertEqual(complete_fetch(self.backend, self.job, self.fetch_id, receipt['manifest_digest'])['status'], 'ACKNOWLEDGED')
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(Path(receipt['export_path']).is_dir())

    def test_same_id_replays_original_capture_and_conflicting_selection_refuses(self):
        receipt = self.capture()
        self.source.write_bytes(b'new observation is a different version')
        self.assertEqual(prepare_fetch(self.backend, self.job, self.spec, self.fetch_id), receipt)
        changed = deepcopy(self.spec)
        changed['files'][0]['path'] = 'different-name'
        with self.assertRaisesRegex(NodeError, 'different selection'):
            prepare_fetch(self.backend, self.job, changed, self.fetch_id)
        result = download_local_fetch(receipt, self.destination)
        self.assertEqual(result['qualification'], 'INCOMPLETE_OBSERVATIONAL')
        self.assertEqual((self.destination / 'analysis/summary.txt').read_bytes(), b'Invented observed output\n')

    def test_stable_archive_requires_terminal_and_keeps_replica_qualification(self):
        spec = {**self.spec, 'purpose': 'archive', 'capture': 'stable'}
        with self.assertRaisesRegex(NodeError, 'terminal'):
            self.capture(spec)
        self.terminal()
        receipt = self.capture(spec)
        result = download_local_fetch(receipt, self.destination)
        self.assertEqual(result['qualification'], 'STABLE_REPLICA')
        self.assertFalse(receipt['manifest']['original_location_authority'])
        self.assertEqual(receipt['manifest']['scientific_validation'], 'not-assessed')

    def test_stable_whole_set_source_change_and_lifecycle_change_are_refused(self):
        self.terminal()
        spec = {**self.spec, 'purpose': 'archive', 'capture': 'stable'}
        def change(point):
            if point == 'after_capture':
                self.source.write_bytes(b'changed after copying')
        with self.assertRaisesRegex(NodeError, 'complete set'):
            self.capture(spec, _fault=change)
        with self.assertRaisesRegex(NodeError, 'Interrupted'):
            prepare_fetch(self.backend, self.job, spec, self.fetch_id)
        self.assertFalse(self.destination.exists())

    def test_active_changing_source_is_explicitly_incomplete(self):
        original = _capture
        def change(path, destination, before, **options):
            result = original(path, destination, before, **options)
            self.source.write_bytes(b'new active data')
            return result
        with patch('cmw.nodes.fetch._capture', side_effect=change):
            receipt = self.capture()
        self.assertEqual(receipt['manifest']['qualification'], 'INCOMPLETE_OBSERVATIONAL')
        self.assertEqual(receipt['manifest']['capture'], 'observation')
        self.assertFalse(receipt['manifest']['original_location_authority'])

    def test_append_during_copy_is_bounded_and_only_observational(self):
        original_fdopen = os.fdopen
        source_path = self.source
        class AppendingReader:
            def __init__(self, handle):
                self.handle, self.changed = handle, False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def fileno(self):
                return self.handle.fileno()
            def read(self, count):
                data = self.handle.read(count)
                if not self.changed:
                    with source_path.open('ab') as output:
                        output.write(b'newly appended live bytes')
                    self.changed = True
                return data
        def wrapped(fd, mode):
            handle = original_fdopen(fd, mode)
            return AppendingReader(handle) if mode == 'rb' else handle
        original = self.source.read_bytes()
        info = self.source.stat()
        epoch = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        with patch('cmw.nodes.fetch.os.fdopen', side_effect=wrapped):
            observed = _capture(self.source, self.root / 'captured-prefix', epoch,
                                stable=False, maximum=NORMAL_FILE_BYTES)
        self.assertTrue(observed['changed'])
        self.assertEqual((self.root / 'captured-prefix').read_bytes(), original)
        info = self.source.stat()
        epoch = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        with patch('cmw.nodes.fetch.os.fdopen', side_effect=wrapped), self.assertRaisesRegex(NodeError, 'Stable source changed'):
            _capture(self.source, self.root / 'rejected-stable', epoch, stable=True, maximum=NORMAL_FILE_BYTES)

    def test_restart_requires_explicit_opt_in_and_active_refusal(self):
        restart = self.work / 'WAVECAR'
        restart.write_bytes(b'Invented restart-like bytes, not engine data')
        spec = deepcopy(self.spec)
        spec['files'][0].update(relative='WAVECAR', path='WAVECAR')
        with self.assertRaisesRegex(NodeError, 'Restart files require'):
            normalize_fetch_spec(spec)
        spec.update(purpose='restart', capture='stable', large_files=True)
        with self.assertRaisesRegex(NodeError, 'terminal'):
            self.capture(spec)
        self.terminal()
        wait_for_fixture_sources([restart])
        receipt = self.capture(spec)
        self.assertEqual(receipt['manifest']['purpose'], 'restart')
        self.assertEqual(receipt['manifest']['scientific_validation'], 'not-assessed')

    def test_manifest_bounds_and_path_escape_casefold_special_file_refusals(self):
        for update in ({'relative': '../secret'}, {'relative': ''}, {'path': '/absolute'},
                       {'path': 'fetch-manifest.json'}, {'path': 'analysis/../secret'},
                       {'path': '.ssh/id_ed25519'}, {'path': 'queue.sqlite3'}, {'relative': 'link'},
                       {'relative': 'fifo'}):
            value = deepcopy(self.spec)
            value['files'][0].update(update)
            if update.get('relative') == 'link':
                (self.work / 'link').symlink_to(self.source)
            if update.get('relative') == 'fifo':
                os.mkfifo(self.work / 'fifo')
            with self.subTest(update=update), self.assertRaises(NodeError):
                prepare_fetch(self.backend, self.job, value, uuid4().hex)
        values = deepcopy(self.spec)
        values['files'].append({**values['files'][0], 'relative': 'other', 'path': 'ANALYSIS/SUMMARY.txt'})
        with self.assertRaises(NodeError):
            normalize_fetch_spec(values)
        with self.assertRaises(NodeError):
            normalize_fetch_spec({**self.spec, 'schema': True})
        large = self.work / 'oversized'
        with large.open('wb') as handle:
            handle.truncate(NORMAL_FILE_BYTES + 1)
        value = deepcopy(self.spec)
        value['files'][0]['relative'] = large.name
        with self.assertRaisesRegex(NodeError, '64 MiB'):
            prepare_fetch(self.backend, self.job, value, uuid4().hex)
        self.assertGreater(LARGE_FILE_BYTES, NORMAL_FILE_BYTES)

    def test_missing_location_and_replaced_storage_refuse(self):
        value = deepcopy(self.spec)
        value['files'][0]['location_id'] = 'f' * 64
        with self.assertRaisesRegex(NodeError, 'location'):
            prepare_fetch(self.backend, self.job, value, uuid4().hex)
        with patch('cmw.nodes.transfer.check_storage', return_value={'ready': False, 'error': 'volume replaced'}):
            with self.assertRaisesRegex(NodeError, 'volume replaced'):
                self.capture()
        self.assertFalse(self.destination.exists())

    def test_partial_export_and_lost_reply_keep_distinct_recoverable_states(self):
        def crash(point):
            if point == 'after_capture':
                raise RuntimeError('interruption')
        with self.assertRaises(RuntimeError):
            self.capture(_fault=crash)
        with self.assertRaisesRegex(NodeError, 'Interrupted'):
            prepare_fetch(self.backend, self.job, self.spec, self.fetch_id)
        self.fetch_id = uuid4().hex
        def lost(point):
            if point == 'after_record':
                raise RuntimeError('lost reply')
        with self.assertRaises(RuntimeError):
            self.capture(_fault=lost)
        receipt = prepare_fetch(self.backend, self.job, self.spec, self.fetch_id)
        self.assertEqual(receipt['status'], 'CAPTURED')
        self.assertEqual(download_local_fetch(receipt, self.destination)['status'], 'FETCHED')

    def test_no_clobber_and_same_named_destination_is_not_adopted(self):
        receipt = self.capture()
        self.destination.mkdir()
        marker = self.destination / 'existing-work'
        marker.write_bytes(b'preserve')
        with self.assertRaisesRegex(NodeError, 'new canonical'):
            download_local_fetch(receipt, self.destination)
        self.assertEqual(marker.read_bytes(), b'preserve')

    def test_destination_appearing_at_publication_is_preserved(self):
        receipt = self.capture()
        def raced(source, destination):
            destination.mkdir()
            (destination / 'existing-work').write_bytes(b'preserve concurrent work')
            return _rename_exclusive(source, destination)
        with patch('cmw.nodes.fetch._rename_exclusive', side_effect=raced), self.assertRaises(FileExistsError):
            download_local_fetch(receipt, self.destination)
        self.assertEqual((self.destination / 'existing-work').read_bytes(), b'preserve concurrent work')
        self.assertFalse((self.destination / 'fetch-manifest.json').exists())

    def test_corrupt_export_and_receipt_never_publish(self):
        receipt = self.capture()
        bad = deepcopy(receipt)
        bad['target']['attempt_id'] = uuid4().hex
        with self.assertRaises(NodeError):
            download_local_fetch(bad, self.destination)
        source = Path(receipt['export_path']) / 'files/0000.bin'
        source.write_bytes(b'corrupt')
        with self.assertRaises(NodeError):
            download_local_fetch(receipt, self.destination)
        with self.assertRaises(NodeError):
            complete_fetch(self.backend, self.job, self.fetch_id, receipt['manifest_digest'])
        self.assertFalse(self.destination.exists())

    def test_replaced_export_tree_cannot_be_adopted(self):
        receipt = self.capture()
        source = Path(receipt['export_path'])
        saved = source.with_name('retained-original')
        source.rename(saved)
        shutil.copytree(saved, source)
        with self.assertRaisesRegex(NodeError, 'replaced'):
            complete_fetch(self.backend, self.job, self.fetch_id, receipt['manifest_digest'])

    def test_actual_separate_sftp_download_and_literal_source_quoting(self):
        if not Path('/usr/bin/sftp').is_file() or not Path('/usr/libexec/sftp-server').is_file():
            self.skipTest('Installed local OpenSSH SFTP mechanism unavailable')
        receipt = self.capture()
        result = download_fetch({}, receipt, self.destination,
            _argv=['/usr/bin/sftp', '-b', '-', '-D', '/usr/libexec/sftp-server'])
        self.assertEqual(result['status'], 'FETCHED')
        self.assertEqual((self.destination / 'analysis/summary.txt').read_bytes(), self.source.read_bytes())
        self.assertEqual(receipt['manifest_digest'], stable_hash(receipt['manifest']))
