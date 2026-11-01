"""Real filesystem transfers with synthetic data; no scientific engines run."""
from __future__ import annotations

import copy
import hashlib
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.core.storage import StorageCapacityError
from cmw.nodes.protocol import NodeError
from cmw.nodes.storage import identify_storage, check_storage
from cmw.nodes.transfer import (MAX_FILE_BYTES, begin_transfer, cleanup_transfer, lookup_transfer,
    manifest_digest, normalize_manifest, prepare_local_bundle, publish_transfer,
    run_sftp, sftp_command, sftp_quote, upload_local_bundle, upload_sftp)
from tests.nodes.test_storage import volume
from tests.periodic.vasp.handoff_case import wait_for_fixture_sources


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-transfer-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root / 'storage [selected] "quoted"'
        self.storage.mkdir()
        self.source = self.root / 'scientific source.txt'
        self.source.write_bytes(b'explicit synthetic input\n')
        self.node_id, self.queue_id, self.request_id = uuid4().hex, uuid4().hex, uuid4().hex
        self.config = {'node_id': self.node_id, 'queues': {'research': {'queue_id': self.queue_id}},
                       'storages': {'scratch': identify_storage(self.storage, probe=volume)}}
        check = patch('cmw.nodes.transfer.check_storage', side_effect=lambda value: check_storage(value, probe=volume))
        check.start()
        self.addCleanup(check.stop)
        self.selections = [{'source': str(self.source), 'path': 'inputs/input.txt', 'role': 'input'}]
        self.spool = self.root / 'private spool'

    def prepared(self, selections=None, **kwargs):
        selections = selections or self.selections
        wait_for_fixture_sources(Path(item['source']) for item in selections)
        return prepare_local_bundle(self.spool, selections,
                                    node_id=self.node_id, queue_id=self.queue_id, **kwargs)

    def started(self):
        bundle = self.prepared()
        receipt = begin_transfer(self.config, 'scratch', self.request_id, bundle['manifest'])
        return bundle, receipt

    def uploaded(self):
        bundle, receipt = self.started()
        upload_local_bundle(self.spool, receipt)
        return bundle, receipt

    def test_stable_selected_bundle_published_without_jobs_or_metadata_in_inputs(self):
        bundle, receipt = self.uploaded()
        self.assertEqual(receipt['status'], 'INCOMING')
        result = publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        published = Path(result['published_root'])
        self.assertEqual((published / 'inputs/input.txt').read_bytes(), self.source.read_bytes())
        self.assertEqual({p.name for p in (published / 'inputs').iterdir()}, {'input.txt'})
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(lookup_transfer(self.config, 'scratch', self.request_id), result)
        self.assertEqual(begin_transfer(self.config, 'scratch', self.request_id, bundle['manifest']), result)
        self.assertFalse(any(self.storage.rglob('*.sqlite3')))
        self.assertFalse(any(self.storage.rglob('RUN_METADATA.txt')))

    def test_request_retry_conflict_and_wrong_target_refused(self):
        bundle, receipt = self.started()
        self.assertEqual(begin_transfer(self.config, 'scratch', self.request_id, bundle['manifest']), receipt)
        changed = copy.deepcopy(bundle['manifest'])
        changed['files'][0]['origin'] = 'different semantic source'
        with self.assertRaisesRegex(NodeError, 'different manifest'):
            begin_transfer(self.config, 'scratch', self.request_id, changed)
        for field in ('node_id', 'queue_id'):
            with self.subTest(field=field), self.assertRaises(NodeError):
                begin_transfer(self.config, 'scratch', uuid4().hex, {**bundle['manifest'], field: uuid4().hex})

    def test_traversal_casefold_prefix_unicode_and_special_files_refused(self):
        bundle = self.prepared()
        base = bundle['manifest']
        for path in ('../escape', '/absolute', 'inputs/../escape', 'inputs/./input',
                     'inputs//input', 'inputs/a\\b', 'inputs/control\nname', 'inputs/e\u0301'):
            value = copy.deepcopy(base)
            value['files'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(NodeError):
                normalize_manifest(value)
        for path in ('inputs/INPUT.txt', 'inputs/input.txt/child'):
            value = copy.deepcopy(base)
            value['files'].append({**value['files'][0], 'path': path})
            with self.subTest(path=path), self.assertRaises(NodeError):
                normalize_manifest(value)
        link = self.root / 'symlink'
        link.symlink_to(self.source)
        fifo = self.root / 'pipe'
        os.mkfifo(fifo)
        for source in (link, fifo):
            with self.subTest(source=source), self.assertRaises(NodeError):
                prepare_local_bundle(self.root / uuid4().hex,
                    [{'source': str(source), 'path': 'inputs/input', 'role': 'input'}],
                    node_id=self.node_id, queue_id=self.queue_id)

    def test_bounds_modes_licenses_and_restart_refused_before_spool(self):
        bundle = self.prepared()
        for update in ({'bytes': True}, {'bytes': MAX_FILE_BYTES + 1}, {'mode': 0o777},
                       {'role': 'engine'}, {'role': {}}, {'sha256': 'not-a-digest'}, {'path': 'inputs/WAVECAR'},
                       {'path': 'inputs/CHGCAR.old'}, {'path': 'support/id_rsa', 'role': 'support'},
                       {'path': 'inputs/POTCAR', 'role': 'potcar'}):
            value = copy.deepcopy(bundle['manifest'])
            value['files'][0].update(update)
            with self.subTest(update=update), self.assertRaises(NodeError):
                normalize_manifest(value)
        values = copy.deepcopy(bundle['manifest'])
        values['files'] = [{**values['files'][0], 'path': f'inputs/{i}', 'bytes': MAX_FILE_BYTES} for i in range(5)]
        with self.assertRaises(NodeError):
            normalize_manifest(values)

    def test_explicit_potcar_authorization_and_four_file_contract(self):
        selections = []
        for name in ('INCAR', 'KPOINTS', 'POSCAR', 'POTCAR'):
            source = self.root / name
            source.write_text('synthetic test fixture; not licensed potential data\n')
            selections.append({'source': str(source), 'path': 'inputs/' + name,
                               'role': 'potcar' if name == 'POTCAR' else 'input'})
        with self.assertRaises(NodeError):
            self.prepared(selections)
        self.assertFalse(self.spool.exists())
        bundle = self.prepared(selections, potcar_authorized=True)
        receipt = begin_transfer(self.config, 'scratch', self.request_id, bundle['manifest'])
        upload_local_bundle(self.spool, receipt)
        result = publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        self.assertEqual({p.name for p in (Path(result['published_root']) / 'inputs').iterdir()},
                         {'INCAR', 'KPOINTS', 'POSCAR', 'POTCAR'})

    def test_original_source_change_after_manifest_refuses_upload(self):
        bundle, receipt = self.started()
        self.source.write_text('changed source version\n')
        with self.assertRaisesRegex(NodeError, 'Original selected source changed'):
            upload_local_bundle(self.spool, receipt)
        self.assertEqual(list(Path(receipt['incoming_path']).iterdir()), [])
        self.assertFalse(Path(receipt['published_root']).exists())

    def test_partial_or_corrupt_upload_never_publishes(self):
        bundle, receipt = self.started()
        incoming = Path(receipt['incoming_path'])
        with self.assertRaisesRegex(NodeError, 'incomplete'):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        (incoming / '0000.bin').write_bytes(b'partial')
        (incoming / '0000.bin').chmod(0o600)
        with self.assertRaisesRegex(NodeError, 'manifest version'):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        self.assertFalse(Path(receipt['published_root']).exists())
        upload_local_bundle(self.spool, begin_transfer(self.config, 'scratch', self.request_id, bundle['manifest']))
        self.assertEqual(publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])['status'], 'PUBLISHED')

    def test_symlink_upload_and_unexpected_payload_refused(self):
        bundle, receipt = self.started()
        upload = Path(receipt['incoming_path'])
        (upload / '0000.bin').symlink_to(self.source)
        with self.assertRaises(NodeError):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        (upload / '0000.bin').unlink()
        upload_local_bundle(self.spool, begin_transfer(self.config, 'scratch', self.request_id, bundle['manifest']))
        (upload / 'unexpected').write_bytes(b'not selected')
        with self.assertRaises(NodeError):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        self.assertFalse(Path(receipt['published_root']).exists())

    def test_volume_or_publication_parent_replacement_refused(self):
        bundle, receipt = self.uploaded()
        with patch('cmw.nodes.transfer.check_storage', return_value={'ready': False, 'error': 'replaced volume'}):
            with self.assertRaisesRegex(NodeError, 'replaced volume'):
                publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        prepared = Path(receipt['published_root']).parent
        prepared.rename(prepared.with_name('old-prepared'))
        prepared.mkdir(mode=0o700)
        with self.assertRaisesRegex(NodeError, 'parent was replaced'):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])

    def test_capacity_accounts_for_two_copies_and_preserves_unrelated_files(self):
        bundle = self.prepared()
        unrelated = self.storage / 'existing-user-work'
        unrelated.write_bytes(b'preserve')
        with patch('cmw.nodes.transfer.check_storage_capacity', side_effect=StorageCapacityError('fixture disk full')) as check:
            with self.assertRaises(StorageCapacityError):
                begin_transfer(self.config, 'scratch', self.request_id, bundle['manifest'])
            minimum = check.call_args.kwargs['minimum_free_gb'] * 1024**3
            self.assertEqual(minimum, 2 * self.source.stat().st_size + 16 * 1024**2)
        self.assertEqual(unrelated.read_bytes(), b'preserve')
        self.assertEqual(lookup_transfer(self.config, 'scratch', self.request_id)['status'], 'NOT_FOUND')

    def test_publication_crashes_before_and_after_rename_recover_same_request(self):
        bundle, receipt = self.uploaded()
        def before(point):
            if point == 'before_publish':
                raise RuntimeError('fixture crash before rename')
        with self.assertRaisesRegex(RuntimeError, 'before rename'):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'], _fault=before)
        self.assertFalse(Path(receipt['published_root']).exists())
        self.assertEqual(lookup_transfer(self.config, 'scratch', self.request_id)['status'], 'PUBLISHING')
        def after(point):
            if point == 'after_publish':
                raise RuntimeError('fixture crash after rename')
        with self.assertRaisesRegex(RuntimeError, 'after rename'):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'], _fault=after)
        target = Path(receipt['published_root'])
        identity = target.stat().st_ino
        self.assertEqual(lookup_transfer(self.config, 'scratch', self.request_id)['status'], 'PUBLISHING')
        result = publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(target.stat().st_ino, identity)

    def test_lost_publication_reply_retries_original_completed_outcome(self):
        bundle, receipt = self.uploaded()
        def fault(point):
            if point == 'after_record':
                raise RuntimeError('lost reply')
        with self.assertRaisesRegex(RuntimeError, 'lost reply'):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'], _fault=fault)
        result = lookup_transfer(self.config, 'scratch', self.request_id)
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest']), result)

    def test_no_clobber_even_when_destination_appears_at_publication(self):
        bundle, receipt = self.uploaded()
        destination = Path(receipt['published_root'])
        def race(point):
            if point == 'before_publish':
                destination.mkdir()
                (destination / 'existing-work').write_bytes(b'preserve')
        with self.assertRaises(FileExistsError):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'], _fault=race)
        self.assertEqual((destination / 'existing-work').read_bytes(), b'preserve')
        with self.assertRaises(NodeError):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])

    def test_identical_copy_cannot_impersonate_interrupted_publication(self):
        bundle, receipt = self.uploaded()
        def fault(point):
            if point == 'before_publish':
                raise RuntimeError('before promotion')
        with self.assertRaises(RuntimeError):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'], _fault=fault)
        candidate = next(Path(receipt['incoming_path']).parent.glob('candidate-*'))
        destination = Path(receipt['published_root'])
        shutil.copytree(candidate, destination)
        with self.assertRaisesRegex(NodeError, 'recorded promotion candidate'):
            publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        self.assertEqual((destination / 'inputs/input.txt').read_bytes(), self.source.read_bytes())

    def test_identical_replacement_cannot_impersonate_published_directory(self):
        bundle, receipt = self.uploaded()
        publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        destination = Path(receipt['published_root'])
        original = destination.with_name('retained-original')
        destination.rename(original)
        shutil.copytree(original, destination)
        with self.assertRaisesRegex(NodeError, 'recorded promotion candidate'):
            lookup_transfer(self.config, 'scratch', self.request_id)
        self.assertEqual((original / 'inputs/input.txt').read_bytes(), self.source.read_bytes())

    def test_published_mutation_rejects_lookup_before_registration(self):
        bundle, receipt = self.uploaded()
        result = publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        (Path(result['published_root']) / 'inputs/input.txt').write_bytes(b'modified after publication')
        with self.assertRaises(NodeError):
            lookup_transfer(self.config, 'scratch', self.request_id)

    def test_readonly_lookup_does_not_create_namespace(self):
        self.assertEqual(lookup_transfer(self.config, 'scratch', self.request_id)['status'], 'NOT_FOUND')
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_actual_sftp_mechanism_with_generated_names_and_quoted_storage_path(self):
        if not Path('/usr/bin/sftp').is_file() or not Path('/usr/libexec/sftp-server').is_file():
            self.skipTest('Installed OpenSSH sftp and local sftp-server unavailable')
        bundle, receipt = self.started()
        result = upload_sftp({}, self.spool, receipt,
            _argv=['/usr/bin/sftp', '-b', '-', '-D', '/usr/libexec/sftp-server'])
        self.assertEqual(result['status'], 'UPLOADED')
        self.assertEqual(publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])['status'], 'PUBLISHED')

    def test_sftp_pinned_key_before_batch_and_private_channel_options(self):
        connection = {'ssh_alias': 'explicit-mini', 'ssh_host_key': 'SHA256:' + 'A' * 43}
        command = sftp_command(connection)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('ForwardAgent=no', command)
        self.assertIn('ControlPath=none', command)
        self.assertNotIn('-A', command)
        marker = self.root / 'unexpected-input'
        script = ('import sys; from pathlib import Path; '
                  'print("debug1: Server host key: ssh-ed25519 SHA256:' + 'B' * 43 + '",file=sys.stderr,flush=True); '
                  'data=sys.stdin.buffer.read(); '
                  'Path(sys.argv[1]).write_bytes(data) if data else None')
        with self.assertRaisesRegex(NodeError, 'host key differs'):
            run_sftp([sys.executable, '-c', script, str(marker)], b'put secret target\n',
                     cwd=self.root, host_key=connection['ssh_host_key'], timeout=3)
        self.assertFalse(marker.exists())
        with self.assertRaises(NodeError):
            sftp_quote('bad\ncommand')
        self.assertEqual(sftp_quote('path with spaces'), '"path with spaces"')

    def test_sftp_timeout_is_unpublished_and_bounded(self):
        with self.assertRaisesRegex(NodeError, 'timed out'):
            run_sftp([sys.executable, '-c', 'import time; time.sleep(2)'], b'bye\n',
                     cwd=self.root, host_key='SHA256:' + 'A' * 43, timeout=0.05)

    def test_cleanup_removes_only_owned_staging_and_retains_success_identity(self):
        bundle, receipt = self.uploaded()
        result = publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        sibling = self.storage / 'preserved-user-data'
        sibling.write_bytes(b'preserve')
        parent = Path(receipt['incoming_path']).parent
        unexpected = parent / 'unexpected'
        unexpected.write_bytes(b'preserve too')
        with self.assertRaises(NodeError):
            cleanup_transfer(self.config, 'scratch', self.request_id)
        self.assertTrue(Path(receipt['incoming_path']).exists())
        unexpected.unlink()
        self.assertEqual(cleanup_transfer(self.config, 'scratch', self.request_id)['status'], 'CLEANED')
        self.assertFalse(parent.exists())
        self.assertEqual(sibling.read_bytes(), b'preserve')
        self.assertEqual(lookup_transfer(self.config, 'scratch', self.request_id), result)
        self.assertEqual(begin_transfer(self.config, 'scratch', self.request_id, bundle['manifest']), result)
        self.assertEqual(cleanup_transfer(self.config, 'scratch', self.request_id)['status'], 'CLEANED')

    def test_published_hash_cache_tracks_same_size_rewrites_and_extra_empty_directories(self):
        bundle, receipt = self.uploaded()
        result = publish_transfer(self.config, 'scratch', self.request_id, bundle['manifest_digest'])
        original = Path(result['published_root']) / 'inputs/input.txt'
        original.write_bytes(b'x' * original.stat().st_size)
        with self.assertRaises(NodeError):
            lookup_transfer(self.config, 'scratch', self.request_id)
        original.write_bytes(self.source.read_bytes())
        self.assertEqual(lookup_transfer(self.config, 'scratch', self.request_id)['status'], 'PUBLISHED')
        (Path(result['published_root']) / 'inputs/extra-directory').mkdir(mode=0o700)
        with self.assertRaises(NodeError):
            lookup_transfer(self.config, 'scratch', self.request_id)
