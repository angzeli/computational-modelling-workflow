"""Resume the same published successor after loss of live parent evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import errno
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs import successor_vasp
from cmw.jobs.runner_assets import file_identity
from cmw.jobs.store import JobsError
from cmw.jobs.successor import SuccessorService
from cmw.jobs.successor_models import SuccessorUnverifiable
from tests.jobs.successor_fixtures import make_vasp_fixture, successor_case


class SuccessorRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-successor-retry-')
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture = make_vasp_fixture(Path(temporary.name) / 'owner')

    def setUp(self):
        self.store = self.fixture['store']
        self.job = self.fixture['job']
        self.spec, self.spec_path = successor_case(self.fixture, uuid4().hex)
        self.output = Path(self.spec['adapter']['output'])
        self.service = SuccessorService(self.store)
        plan = self.service.plan(self.job['id'], str(self.spec_path), self.job['attempt_id'])
        self.assertEqual(plan.status, 'READY', plan.payload_json)
        self.digest = plan.plan_digest
        publication = json.loads(plan.payload_json)['adapter']['publication_plan']
        self.record = Path(publication['record_directory']) / 'successor.json'
        self.initial_jobs = self.store.snapshot()['jobs']

    def materialize(self):
        # Use an independent service for every retry, as separate CLI requests do.
        return SuccessorService(self.store).materialize(
            self.job['id'], str(self.spec_path), self.digest, self.job['attempt_id'])

    def register(self):
        return self.service.register(self.job['id'], str(self.record), self.digest,
                                     uuid4().hex, self.job['attempt_id'])

    def after_publication(self, action):
        publish = successor_vasp.materialize_vasp

        def publish_then_act(*args, **kwargs):
            result = publish(*args, **kwargs)
            self.assertTrue((self.output / 'INCAR').is_file())
            action()
            return result

        with patch.object(successor_vasp, 'materialize_vasp', side_effect=publish_then_act):
            return self.materialize()

    def pending(self):
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
            result = self.after_publication(lambda: writer.execute('BEGIN EXCLUSIVE'))
        self.assertEqual(result.status, 'PENDING_VERIFICATION', result.payload_json)
        self.assertEqual(Path(json.loads(result.payload_json)['record_path']), self.record)
        record = json.loads(self.record.read_text())
        self.assertEqual(record['status'], 'PENDING_VERIFICATION')
        self.assertEqual(record['plan_digest'], self.digest)
        for name in ('INCAR', 'KPOINTS', 'POSCAR', 'POTCAR'):
            self.assertEqual(record['prepared'][name]['sha256'], file_identity(self.output / name)['sha256'])
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def contradict_exit_receipt(self):
        # A readable row now contradicts the genuine disposable attempt's exit receipt.
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            original_exit = job['exit_code']
            self.assertEqual(original_exit, 0)
            job['exit_code'] = 17
            self.store.save(con, job)

        def restore():
            with self.store.transaction() as con:
                original = self.store.get(con, self.job['id'])
                original['exit_code'] = original_exit
                self.store.save(con, original)

        self.addCleanup(restore)

    def assert_incomplete(self):
        self.assertEqual(json.loads(self.record.read_text())['status'], 'INCOMPLETE')
        with self.assertRaises(JobsError):
            self.register()
        self.assertEqual(len(self.store.snapshot()['jobs']), len(self.initial_jobs))

    def missing_record(self):
        """Publish normally, then fail precisely the first successor record link."""
        link = os.link

        def disk_full(source, destination, *args, **kwargs):
            if Path(destination) == self.record:
                raise OSError(errno.ENOSPC, 'injected successor record disk full', str(destination))
            return link(source, destination, *args, **kwargs)

        with patch('cmw.jobs.successor.os.link', side_effect=disk_full):
            with self.assertRaises(OSError) as failed:
                self.materialize()
        self.assertEqual(failed.exception.errno, errno.ENOSPC)
        with self.assertRaises(FileNotFoundError):
            self.record.lstat()
        preparation = json.loads((self.record.parent / 'preparation.json').read_text())
        self.assertEqual(preparation['publication']['state'], 'complete')
        self.assertEqual(preparation['successor_recovery']['plan_digest'], self.digest)
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def publication_snapshot(self):
        """Compare bytes, names and identities without following fixture symlinks."""
        result = {}
        roots = (self.output, self.record.parent, Path(self.spec['runtime']['run_directory']))
        for root in roots:
            for path in (root, *root.iterdir()):
                observed = path.lstat()
                identity = (observed.st_dev, observed.st_ino, observed.st_mode,
                            observed.st_size, observed.st_mtime_ns)
                if stat.S_ISREG(observed.st_mode):
                    contents = path.read_bytes()
                elif stat.S_ISLNK(observed.st_mode):
                    contents = os.readlink(path)
                else:
                    contents = None
                result[str(path)] = (identity, contents)
        return result

    def assert_missing_record_refused(self, reason):
        before = self.publication_snapshot()
        with self.assertRaisesRegex((JobsError, OSError), reason):
            self.materialize()
        self.assertEqual(self.publication_snapshot(), before)
        with self.assertRaises(FileNotFoundError):
            self.record.lstat()
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def test_missing_record_recovers_same_publication_and_registers(self):
        self.missing_record()
        original = self.publication_snapshot()
        result = self.materialize()
        self.assertEqual(result.status, 'MATERIALIZED', result.payload_json)
        self.assertFalse(json.loads(result.payload_json)['replayed'])
        # No input, working directory or existing preparation evidence is rewritten.
        current = self.publication_snapshot()
        for path, evidence in original.items():
            if path != str(self.record.parent):
                self.assertEqual(current[path], evidence)
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)
        registered = self.register()
        self.assertEqual(registered.status, 'REGISTERED')
        self.assertEqual(json.loads(registered.payload_json)['child']['status'], 'Hold')
        self.assertEqual(len(self.store.snapshot()['jobs']), len(self.initial_jobs) + 1)

    def test_preparation_changed_before_recovery_context_is_not_overwritten(self):
        path = self.record.parent / 'preparation.json'
        edited = []

        def alter():
            value = json.loads(path.read_text())
            value['publication']['state'] = 'injected external edit'
            path.write_text(json.dumps(value))
            edited.append(path.read_bytes())

        with self.assertRaisesRegex(JobsError, 'Preparation evidence changed'):
            self.after_publication(alter)
        self.assertEqual(path.read_bytes(), edited[0])
        with self.assertRaises(FileNotFoundError):
            self.record.lstat()
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def test_missing_record_altered_input_refuses_without_writes(self):
        self.missing_record()
        with (self.output / 'INCAR').open('ab') as stream:
            stream.write(b'\n# altered after publication\n')
        self.assert_missing_record_refused('differs from approved bytes: INCAR')

    def test_missing_record_extra_input_refuses_without_writes(self):
        self.missing_record()
        (self.output / 'unexpected').write_text('unapproved synthetic file')
        self.assert_missing_record_refused('extra=.*unexpected')

    def test_missing_record_extra_metadata_or_working_file_refuses(self):
        self.missing_record()
        for directory in (self.record.parent, Path(self.spec['runtime']['run_directory'])):
            with self.subTest(directory=directory.name):
                extra = directory / 'unexpected'
                extra.write_text('unapproved synthetic file')
                try:
                    self.assert_missing_record_refused('contains unexpected entries')
                finally:
                    extra.unlink()

    def test_missing_record_absent_input_refuses_without_writes(self):
        self.missing_record()
        (self.output / 'KPOINTS').unlink()
        self.assert_missing_record_refused('missing=.*KPOINTS')

    def test_missing_record_unreadable_input_refuses_without_writes(self):
        self.missing_record()
        before = self.publication_snapshot()
        target = self.output / 'INCAR'
        original_open = os.open

        def denied(path, *args, **kwargs):
            if Path(path) == target:
                raise PermissionError(errno.EACCES, 'injected unreadable input', str(path))
            return original_open(path, *args, **kwargs)

        with patch('cmw.jobs.runner_assets.os.open', side_effect=denied):
            with self.assertRaisesRegex(PermissionError, 'unreadable input'):
                self.materialize()
        self.assertEqual(self.publication_snapshot(), before)
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def test_missing_record_unreadable_entry_is_not_absent(self):
        self.missing_record()
        before = self.publication_snapshot()
        original_lstat = Path.lstat

        def denied(path, *args, **kwargs):
            if path == self.record:
                raise PermissionError(errno.EACCES, 'injected unreadable record entry', str(path))
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, 'lstat', denied):
            with self.assertRaisesRegex(PermissionError, 'unreadable record entry'):
                self.materialize()
        self.assertEqual(self.publication_snapshot(), before)
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def test_missing_record_symlink_input_refuses_without_writes(self):
        self.missing_record()
        target = self.output / 'INCAR'
        outside = self.output.parent / 'copied-incar'
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
        self.assert_missing_record_refused('symlinks')
        self.assertEqual(outside.read_bytes(), target.read_bytes())

    def test_missing_record_changed_approval_refuses_without_writes(self):
        self.missing_record()
        self.digest = '0' * 64
        self.assert_missing_record_refused('does not match this approved request')

    def test_missing_record_unverifiable_parent_stays_pending_then_promotes(self):
        self.missing_record()
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            pending = self.materialize()
        self.assertEqual(pending.status, 'PENDING_VERIFICATION', pending.payload_json)
        self.assertIn('temporarily unreadable', json.loads(pending.payload_json)['reason'])
        self.assertEqual(json.loads(self.record.read_text())['status'], 'PENDING_VERIFICATION')
        with self.assertRaises(SuccessorUnverifiable):
            self.register()
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)
        self.assertEqual(self.materialize().status, 'MATERIALIZED')
        self.assertEqual(self.register().status, 'REGISTERED')

    def test_missing_record_unavailable_parent_does_not_excuse_changed_inputs(self):
        self.missing_record()
        (self.output / 'INCAR').write_text('unapproved synthetic input')
        before = self.publication_snapshot()
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            with self.assertRaisesRegex(JobsError, 'differs from approved bytes: INCAR'):
                self.materialize()
        self.assertEqual(self.publication_snapshot(), before)
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def test_missing_record_contradicted_parent_refuses_without_writes(self):
        self.missing_record()
        self.contradict_exit_receipt()
        before = self.publication_snapshot()
        jobs = self.store.snapshot()['jobs']
        with self.assertRaisesRegex(JobsError, 'Parent closure changed'):
            self.materialize()
        self.assertEqual(self.publication_snapshot(), before)
        self.assertEqual(self.store.snapshot()['jobs'], jobs)

    def test_missing_record_replaced_destination_identity_refuses(self):
        self.missing_record()
        for location in (self.output, self.record.parent, Path(self.spec['runtime']['run_directory'])):
            with self.subTest(location=location.name):
                original = location.with_name(location.name + '-original')
                location.rename(original)
                shutil.copytree(original, location)
                try:
                    self.assert_missing_record_refused('directory identity changed')
                finally:
                    shutil.rmtree(location)
                    original.rename(location)

    def test_missing_record_unbound_or_malformed_recovery_evidence_refuses(self):
        self.missing_record()
        path = self.record.parent / 'preparation.json'
        original = json.loads(path.read_text())
        for mode in ('missing', 'malformed', 'changed-plan', 'changed-preparation'):
            with self.subTest(mode=mode):
                value = json.loads(json.dumps(original))
                if mode == 'missing':
                    value.pop('successor_recovery')
                    reason = 'lacks approved recovery evidence'
                elif mode == 'malformed':
                    value['successor_recovery']['plan'] = None
                    reason = 'Malformed successor publication recovery evidence'
                elif mode == 'changed-plan':
                    value['successor_recovery']['plan']['execution'] = 'unapproved plan edit'
                    reason = 'does not match this approved request'
                else:
                    value['successor']['plan_digest'] = '0' * 64
                    reason = 'Preparation evidence differs'
                path.write_text(json.dumps(value))
                self.assert_missing_record_refused(reason)

    def test_missing_record_two_retries_create_once_and_observe_result(self):
        self.missing_record()
        ready = Barrier(2, timeout=15)
        link = os.link
        creates = []

        def count_create(source, destination, *args, **kwargs):
            result = link(source, destination, *args, **kwargs)
            if Path(destination) == self.record:
                creates.append(destination)
            return result

        def retry():
            ready.wait()
            result = self.materialize()
            self.assertEqual(result.status, 'MATERIALIZED', result.payload_json)
            return json.loads(result.payload_json)['replayed']

        with patch('cmw.jobs.successor.os.link', side_effect=count_create):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(retry) for _ in range(2)]
                self.assertCountEqual([future.result(timeout=30) for future in futures], [False, True])
        self.assertEqual(creates, [self.record])
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def test_pending_queue_recovery_completes_same_publication_then_registers_once(self):
        self.pending()
        # Recovered access alone must not release a still-pending publication.
        with self.assertRaises(SuccessorUnverifiable):
            self.register()
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)
        original = {path.name: path.stat().st_ino for path in self.output.iterdir()}
        result = self.materialize()
        self.assertEqual(result.status, 'MATERIALIZED')
        self.assertEqual(Path(json.loads(result.payload_json)['record_path']), self.record)
        self.assertEqual(json.loads(self.record.read_text())['status'], 'MATERIALIZED')
        self.assertEqual(original, {path.name: path.stat().st_ino for path in self.output.iterdir()})
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)
        registered = self.register()
        self.assertEqual(registered.status, 'REGISTERED')
        self.assertEqual(json.loads(registered.payload_json)['child']['status'], 'Hold')
        self.assertEqual(len(self.store.snapshot()['jobs']), len(self.initial_jobs) + 1)

    def test_contradiction_immediately_after_publication_is_incomplete(self):
        with self.assertRaises(JobsError):
            self.after_publication(self.contradict_exit_receipt)
        self.assert_incomplete()

    def test_contradiction_when_resuming_pending_is_incomplete(self):
        self.pending()
        self.contradict_exit_receipt()
        with self.assertRaises(JobsError):
            self.materialize()
        self.assert_incomplete()

    def test_bytes_changed_before_initial_post_publication_check_are_incomplete(self):
        def alter():
            with (self.output / 'INCAR').open('ab') as out:
                out.write(b'\n# not the approved prepared input\n')

        with self.assertRaises(JobsError):
            self.after_publication(alter)
        self.assert_incomplete()

    def test_altered_published_bytes_are_incomplete_even_if_queue_is_still_locked(self):
        self.pending()
        incar = self.output / 'INCAR'
        original = incar.read_bytes()
        incar.write_bytes(original + b'\n# altered after publication\n')
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(JobsError):
                self.materialize()
        self.assert_incomplete()
        # Restoring the bytes must not resurrect a terminal contradiction.
        incar.write_bytes(original)
        with self.assertRaises(JobsError):
            self.materialize()
        self.assert_incomplete()

    def test_copied_pending_record_directory_cannot_be_promoted(self):
        self.pending()
        location = self.record.parent
        original = location.with_name(location.name + '-original')
        location.rename(original)
        shutil.copytree(original, location)
        with self.assertRaisesRegex(JobsError, 'publication directory identity changed'):
            self.materialize()
        self.assertEqual(json.loads(self.record.read_text())['status'], 'PENDING_VERIFICATION')
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)

    def test_legacy_completed_record_keeps_original_registration_route(self):
        self.materialize()
        record = json.loads(self.record.read_text())
        for field in ('working_identity', 'parent_selector', 'parent_display_id', 'publication_directory'):
            record.pop(field)
        self.record.write_text(json.dumps(record))
        with self.assertRaisesRegex(JobsError, 'does not support materialization replay'):
            self.materialize()
        self.assertEqual(self.register().status, 'REGISTERED')

    def test_two_retries_share_one_promotion(self):
        self.pending()
        ready = Barrier(2, timeout=15)

        def retry():
            ready.wait()
            result = self.materialize()
            self.assertEqual(result.status, 'MATERIALIZED')
            payload = json.loads(result.payload_json)
            self.assertEqual(Path(payload['record_path']), self.record)
            return payload['replayed']

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(retry) for _ in range(2)]
            self.assertCountEqual([f.result(timeout=30) for f in futures], [False, True])
        self.assertEqual(json.loads(self.record.read_text())['status'], 'MATERIALIZED')
        self.assertEqual(self.store.snapshot()['jobs'], self.initial_jobs)
