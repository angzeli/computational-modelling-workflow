"""Read-only lifecycle evidence never replaces execution or recovery authority."""
from contextlib import closing, contextmanager, ExitStack
from dataclasses import FrozenInstanceError, is_dataclass
import fcntl
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import psutil

from cmw.jobs import lifecycle, ownership
from cmw.jobs.store import Store


HOST = 'invented-lifecycle-host'
BOOT = 101.0
NOW = 1000.0


def owner(pid, birth=200.0):
    return {'pid': pid, 'birth': birth, 'host': HOST, 'boot': BOOT}


@contextmanager
def processes(records):
    """Exact PID probes only; unrelated-process discovery and signals fail tests."""
    def process(pid=None):
        entry = records.get(pid)
        if isinstance(entry, BaseException):
            raise entry
        if entry is None:
            raise psutil.NoSuchProcess(pid)
        identity, status = entry
        result = Mock(pid=pid)
        result.create_time.return_value = identity['birth']
        result.status.return_value = status
        return result
    with patch.object(socket, 'gethostname', return_value=HOST), \
         patch.object(psutil, 'boot_time', return_value=BOOT), \
         patch.object(psutil, 'Process', side_effect=process) as lookup, \
         patch.object(psutil, 'pids', side_effect=AssertionError('machine-wide enumeration forbidden')), \
         patch.object(psutil, 'process_iter', side_effect=AssertionError('machine-wide discovery forbidden')), \
         patch.object(os, 'kill', side_effect=AssertionError('observer must not signal')), \
         patch.object(os, 'killpg', side_effect=AssertionError('observer must not signal')):
        yield lookup


class LifecycleProcessTests(unittest.TestCase):
    def setUp(self):
        self.worker = owner(81001)

    def test_complete_same_birth_identity_is_matched_and_rechecked(self):
        with processes({81001: (self.worker, psutil.STATUS_SLEEPING)}) as lookup:
            result = lifecycle.observe_process(self.worker).to_dict()
        self.assertEqual(result['verdict'], 'matched')
        self.assertGreaterEqual(lookup.call_count, 2)

    def test_missing_and_invalid_identity_never_probes_an_unrelated_pid(self):
        with processes({}) as lookup:
            for value in (None, {}, {'pid': 81001}):
                with self.subTest(value=value):
                    self.assertEqual(lifecycle.observe_process(value).to_dict()['verdict'], 'missing')
            for pid in (0, -1, True, '81001'):
                with self.subTest(pid=pid):
                    self.assertEqual(lifecycle.observe_process({**self.worker, 'pid': pid}).to_dict()['verdict'],
                                     'unavailable')
            lookup.assert_not_called()

    def test_host_and_boot_mismatch_do_not_probe_current_host_pid(self):
        with processes({}) as lookup:
            for key, value in (('host', 'another-host'), ('boot', BOOT-1)):
                with self.subTest(key=key):
                    result = lifecycle.observe_process({**self.worker, key: value}).to_dict()
                    self.assertEqual(result['verdict'], 'mismatch')
            lookup.assert_not_called()

    def test_absent_reused_and_access_denied_are_distinct(self):
        cases = [(None, 'gone'), ((owner(81001, 300.0), psutil.STATUS_SLEEPING), 'mismatch'),
                 (psutil.AccessDenied(81001), 'unavailable'), (PermissionError('fixture'), 'unavailable')]
        for record, expected in cases:
            with self.subTest(expected=expected, record=record), processes({81001: record}):
                self.assertEqual(lifecycle.observe_process(self.worker).to_dict()['verdict'], expected)

    def test_zombie_dead_and_stopped_are_not_running_computation_claims(self):
        for status, expected in [(psutil.STATUS_ZOMBIE, 'gone'), (psutil.STATUS_DEAD, 'gone'),
                                 (psutil.STATUS_STOPPED, 'matched')]:
            with self.subTest(status=status), processes({81001: (self.worker, status)}):
                result = lifecycle.observe_process(self.worker).to_dict()
                self.assertEqual(result['verdict'], expected)
                self.assertIn(status, json.dumps(result).lower())

    def test_identity_changing_during_status_read_is_not_matched(self):
        with processes({81001: (self.worker, psutil.STATUS_SLEEPING)}), \
             patch.object(ownership, 'identity', side_effect=[self.worker, owner(81001, 300.0)]):
            result = lifecycle.observe_process(self.worker).to_dict()
        self.assertEqual(result['verdict'], 'mismatch')

    def test_unreadable_zombie_birth_does_not_prove_original_process_is_gone(self):
        with processes({81001: psutil.ZombieProcess(81001)}):
            result = lifecycle.observe_process(self.worker).to_dict()
        self.assertEqual(result['verdict'], 'unavailable')
        self.assertEqual(result['code'], 'zombie_identity_unverified')


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='cmw-lifecycle-observation-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = Store(self.root/'state')
        self.job = self.store.add(argv=['/unexecuted/opaque'], cwd=self.root,
                                  name='invented job', engine='Unsupported', cpus=2, memory_gib=1)
        self.worker, self.anchor, self.controller = owner(81001), owner(81002), owner(81003)
        self.attempt = self.store.root/'attempts'/self.job['attempt_id']
        self.lock = self.attempt/'worker.lock'
        self.attempt.mkdir(parents=True, mode=0o700)
        self.lock.write_text('invented lock')
        self.control_lock = self.store.root/'controller.lock'
        self.control_lock.write_text('invented controller lock')
        with self.store.transaction() as con:
            row = self.store.get(con, self.job['id'])
            row.update(status='Run', order=None, worker=self.worker, group=self.anchor,
                       claim='invented-claim', claimed_at=900.0, started_at=900.0)
            self.store.save(con, row)
        self.job = self.store.snapshot(now=NOW)['jobs'][0]

    @contextmanager
    def hold(self, *paths):
        with ExitStack() as stack:
            for path in paths:
                handle = stack.enter_context(path.open('r'))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                stack.callback(fcntl.flock, handle, fcntl.LOCK_UN)
            yield

    def update(self, **changes):
        with self.store.transaction() as con:
            row = self.store.get(con, self.job['id'])
            row.update(changes)
            self.store.save(con, row)
        self.job = self.store.snapshot(now=NOW)['jobs'][0]

    @contextmanager
    def live(self, *, worker=True, anchor=True, controller=False, stopped=False):
        records = {}
        for present, item in [(worker, self.worker), (anchor, self.anchor), (controller, self.controller)]:
            if present:
                records[item['pid']] = (item, psutil.STATUS_STOPPED if stopped else psutil.STATUS_SLEEPING)
        with processes(records), \
             patch.object(os, 'getsid', return_value=self.anchor['pid']), \
             patch.object(os, 'getpgid', return_value=self.anchor['pid']):
            yield

    def observe(self, **kwargs):
        return lifecycle.observe_execution(self.store, self.job, now=NOW, **kwargs).to_dict(now=NOW)


class LifecycleLockTests(LifecycleFixture):
    def test_held_and_unheld_lock_preserve_existing_file(self):
        before = self.lock.stat()
        with self.hold(self.lock):
            self.assertEqual(lifecycle.observe_lock(self.store, self.job['attempt_id']).to_dict()['verdict'], 'held')
        self.assertEqual(lifecycle.observe_lock(self.store, self.job['attempt_id']).to_dict()['verdict'], 'unheld')
        after = self.lock.stat()
        self.assertEqual((before.st_ino, before.st_mode, before.st_mtime_ns),
                         (after.st_ino, after.st_mode, after.st_mtime_ns))
        self.assertEqual(self.lock.read_text(), 'invented lock')

    def test_missing_and_invalid_attempt_paths_are_never_created(self):
        self.lock.unlink()
        self.attempt.rmdir()
        result = lifecycle.observe_lock(self.store, self.job['attempt_id']).to_dict()
        self.assertEqual(result['verdict'], 'missing')
        self.assertFalse(self.attempt.exists())
        for identifier in (None, '', '../redirect', '/tmp/redirect', 'f'*31):
            with self.subTest(identifier=identifier):
                self.assertEqual(lifecycle.observe_lock(self.store, identifier).to_dict()['verdict'], 'unavailable')

    def test_symlink_lock_and_attempt_directory_are_rejected(self):
        target = self.root/'unrelated.lock'
        target.write_text('unrelated')
        self.lock.unlink()
        self.lock.symlink_to(target)
        self.assertEqual(lifecycle.observe_lock(self.store, self.job['attempt_id']).to_dict()['verdict'], 'unavailable')
        self.lock.unlink()
        self.attempt.rmdir()
        self.attempt.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(lifecycle.observe_lock(self.store, self.job['attempt_id']).to_dict()['verdict'], 'unavailable')
        self.assertEqual(target.read_text(), 'unrelated')

    def test_permission_denied_is_unavailable_not_unheld(self):
        with patch.object(os, 'open', side_effect=PermissionError('invented denied lock')):
            self.assertEqual(lifecycle.observe_lock(self.store, self.job['attempt_id']).to_dict()['verdict'], 'unavailable')

    def test_lock_replaced_while_probed_cannot_be_verified(self):
        flock = fcntl.flock
        replaced = False
        def replace(fd, operation):
            nonlocal replaced
            result = flock(fd, operation)
            if operation & fcntl.LOCK_EX and not replaced:
                replaced = True
                old = self.lock.with_name('original.lock')
                self.lock.rename(old)
                self.lock.write_text('replacement')
            return result
        with patch.object(fcntl, 'flock', side_effect=replace):
            result = lifecycle.observe_lock(self.store, self.job['attempt_id']).to_dict()
        self.assertEqual(result['verdict'], 'unavailable')


class LifecycleObservationTests(LifecycleFixture):
    def test_critical_stale_run_keeps_database_and_commitments_unchanged(self):
        original = self.store.path.read_bytes()
        files = {p.relative_to(self.store.root): p.read_bytes()
                 for p in self.store.root.rglob('*') if p.is_file()}
        with self.live(worker=False, anchor=False), \
             patch.object(Store, 'transaction', side_effect=AssertionError('observer cannot write')):
            result = self.observe()
        self.assertEqual(result['persisted_status'], 'Run')
        self.assertEqual(result['observed_execution'], 'WORKER_MISSING')
        self.assertTrue(result['reconciliation_required'])
        self.assertEqual(result['evidence']['worker']['verdict'], 'gone')
        self.assertEqual(result['evidence']['worker_lock']['verdict'], 'unheld')
        self.assertEqual(result['evidence']['controller']['verdict'], 'offline')
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual({p.relative_to(self.store.root): p.read_bytes()
                          for p in self.store.root.rglob('*') if p.is_file()}, files)
        state = self.store.snapshot(now=NOW)
        self.assertEqual(state['jobs'][0]['status'], 'Run')
        self.assertIsNone(state['jobs'][0]['finished_at'])
        self.assertEqual(state['jobs'][0]['resources'], self.job['resources'])
        self.assertFalse(state['controller']['dispatch'])

    def test_offline_controller_does_not_discredit_verified_detached_worker(self):
        with self.hold(self.lock), self.live():
            result = self.observe()
        self.assertEqual(result['evidence']['worker']['verdict'], 'matched')
        self.assertEqual(result['evidence']['worker_lock']['verdict'], 'held')
        self.assertEqual(result['evidence']['controller']['verdict'], 'offline')
        self.assertFalse(result['reconciliation_required'])
        self.assertNotEqual(result['observed_execution'], 'VERIFIED_ACTIVE')
        self.assertTrue(result['limitations'])

    def test_online_controller_and_verified_worker_remain_separate_evidence(self):
        with self.store.transaction() as con:
            control = self.store.control(con)
            control.update(owner=self.controller, heartbeat=NOW)
            self.store.set_control(con, control)
        with self.hold(self.lock, self.control_lock), self.live(controller=True):
            result = self.observe()
        self.assertEqual(result['evidence']['worker']['verdict'], 'matched')
        self.assertEqual(result['evidence']['controller']['verdict'], 'online')
        self.assertFalse(result['reconciliation_required'])

    def test_alive_worker_without_held_lock_does_not_verify_execution(self):
        with self.live():
            result = self.observe()
        self.assertEqual(result['evidence']['worker']['verdict'], 'matched')
        self.assertEqual(result['evidence']['worker_lock']['verdict'], 'unheld')
        self.assertTrue(result['reconciliation_required'])
        self.assertNotEqual(result['observed_execution'], 'VERIFIED_ACTIVE')

    def test_absent_launcher_cannot_prove_surviving_rank_has_ended(self):
        # Legacy source records no exhaustive rank list. Absence of this anchor
        # cannot be promoted to absence of any unrecorded original rank.
        with self.hold(self.lock), self.live(anchor=False):
            result = self.observe()
        self.assertEqual(result['evidence']['worker']['verdict'], 'matched')
        self.assertNotIn(result['evidence']['domain']['verdict'], ('drained', 'gone', 'complete'))
        self.assertTrue(result['reconciliation_required'])
        self.assertEqual(result['persisted_status'], 'Run')

    def test_readable_anchor_with_wrong_topology_remains_unverified(self):
        with self.hold(self.lock), self.live(), patch.object(os, 'getsid', return_value=99999):
            result = self.observe()
        self.assertTrue(result['reconciliation_required'])
        self.assertNotEqual(result['observed_execution'], 'VERIFIED_ACTIVE')

    def test_denied_worker_is_not_called_absent(self):
        with processes({self.worker['pid']: psutil.AccessDenied(self.worker['pid'])}):
            result = self.observe()
        self.assertEqual(result['evidence']['worker']['verdict'], 'unavailable')
        self.assertNotEqual(result['observed_execution'], 'WORKER_MISSING')
        self.assertTrue(result['reconciliation_required'])

    def test_simulated_reboot_does_not_probe_or_adopt_old_pids(self):
        with processes({}) as lookup, patch.object(psutil, 'boot_time', return_value=BOOT+100):
            result = self.observe()
        self.assertEqual(result['evidence']['worker']['verdict'], 'mismatch')
        self.assertTrue(result['reconciliation_required'])
        lookup.assert_not_called()

    def test_stopped_living_identity_is_not_reported_dead(self):
        with self.hold(self.lock), self.live(stopped=True):
            result = self.observe()
        self.assertEqual(result['evidence']['worker']['verdict'], 'matched')
        self.assertNotEqual(result['observed_execution'], 'WORKER_MISSING')
        self.assertEqual(result['persisted_status'], 'Run')

    def test_scientific_progress_and_engine_support_are_not_lifecycle_inputs(self):
        stale_science = {**self.job, 'progress': {'observed_at': 1.0, 'parser_status': 'OK',
                                              'last_evidence_time': 2.0},
                        'telemetry': {'cpu_quality': 'unavailable'}}
        with self.hold(self.lock), self.live():
            plain = self.observe()
            annotated = lifecycle.observe_execution(self.store, stale_science, now=NOW).to_dict(now=NOW)
        self.assertEqual(annotated['observed_execution'], plain['observed_execution'])
        self.assertFalse(annotated['reconciliation_required'])
        self.assertEqual(self.job['engine'], 'Unsupported')

    def test_unknown_stays_unknown_even_if_worker_observation_is_good(self):
        self.update(status='Unknown')
        before = self.store.path.read_bytes()
        with self.hold(self.lock), self.live():
            result = self.observe()
        self.assertEqual(result['persisted_status'], 'Unknown')
        self.assertTrue(result['reconciliation_required'])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_nonactive_job_does_not_probe_processes_or_receipts(self):
        for status in ('Queue', 'Hold', 'Done', 'Fail', 'Cancelled'):
            self.update(status=status)
            with self.subTest(status=status), processes({}) as lookup:
                result = self.observe()
            self.assertEqual(result['observed_execution'], 'NOT_APPLICABLE')
            self.assertFalse(result['reconciliation_required'])
            lookup.assert_not_called()

    def test_observation_result_is_frozen_and_ages_independently(self):
        with self.live():
            value = lifecycle.observe_execution(self.store, self.job, now=NOW)
        self.assertTrue(is_dataclass(value))
        with self.assertRaises(FrozenInstanceError):
            value.persisted_status = 'Done'
        initial = value.to_dict(now=NOW)
        later = value.to_dict(now=NOW+3600)
        self.assertEqual(initial['age_seconds'], 0)
        self.assertEqual(later['age_seconds'], 3600)
        self.assertTrue(later['stale'])
        self.assertEqual(later['persisted_status'], 'Run')

    def test_observation_key_ignores_notes_but_binds_lifecycle(self):
        key = lifecycle.observation_key(self.store, self.job)
        hash(key)
        changed_note = {**self.job, 'note': 'human annotation', 'note_revision': 9,
                        'note_updated_at': NOW, 'elapsed': 9999}
        self.assertEqual(lifecycle.observation_key(self.store, changed_note), key)
        for field, value in [('claim', 'replacement'), ('attempt_id', 'b'*32),
                             ('status', 'Cancelling'), ('worker', owner(81999))]:
            with self.subTest(field=field):
                self.assertNotEqual(lifecycle.observation_key(self.store, {**self.job, field: value}), key)

    def test_valid_note_update_during_observation_does_not_invalidate_lifecycle(self):
        original = lifecycle.observe_process
        updated = False
        def annotate(record):
            nonlocal updated
            if not updated:
                updated = True
                self.store.annotate(self.job['id'], 'concurrent human note')
            return original(record)
        with self.hold(self.lock), self.live(), patch.object(lifecycle, 'observe_process', side_effect=annotate):
            result = self.observe()
        self.assertFalse(result['stale'])
        self.assertFalse(result['reconciliation_required'])
        self.assertEqual(self.store.snapshot(now=NOW)['jobs'][0]['note'], 'concurrent human note')

    def test_legitimate_worker_update_during_observation_invalidates_result(self):
        original = lifecycle.observe_process
        updated = False
        def update(record):
            nonlocal updated
            if not updated:
                updated = True
                with self.store.transaction() as con:
                    row = self.store.get(con, self.job['id'])
                    row.update(status='Cancelling', cancel_requested=True)
                    self.store.save(con, row)
            return original(record)
        with self.hold(self.lock), self.live(), patch.object(lifecycle, 'observe_process', side_effect=update):
            result = self.observe()
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.assertEqual(result['evidence']['source']['verdict'], 'unavailable')
        self.assertEqual(self.store.snapshot(now=NOW)['jobs'][0]['status'], 'Cancelling')

    def test_replaced_database_is_not_the_same_observation_target(self):
        key = lifecycle.observation_key(self.store, self.job)
        original = self.store.path.with_suffix('.original')
        self.store.path.rename(original)
        self.store.path.write_bytes(original.read_bytes())
        self.assertNotEqual(lifecycle.observation_key(self.store, self.job), key)


class LifecycleReceiptSourceTests(LifecycleFixture):
    def receipt(self, content):
        (self.attempt/'payload-exit.json').write_text(content)

    def test_valid_legacy_receipt_is_unbound_when_worker_crashes_before_final_state(self):
        self.receipt(json.dumps({'exit_code': 0, 'recorded_at': NOW-10}))
        before = self.store.path.read_bytes()
        with self.live(worker=False, anchor=False):
            result = self.observe()
        receipt = result['evidence']['receipt']
        self.assertEqual(receipt['verdict'], 'unbound')
        self.assertFalse(receipt['terminal_authority'])
        self.assertEqual(receipt['attempt_binding'], 'path_only')
        self.assertEqual(result['observed_execution'], 'WORKER_MISSING')
        self.assertEqual(result['persisted_status'], 'Run')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_missing_malformed_oversized_and_unbound_receipts_remain_distinct(self):
        cases = [('not json', 'malformed'), ('[]', 'malformed'),
                 (json.dumps({'exit_code': True, 'recorded_at': NOW-1}), 'malformed'),
                 (json.dumps({'exit_code': 0, 'recorded_at': float('nan')}), 'malformed'),
                 (' '*4097, 'unavailable'),
                 (json.dumps({'exit_code': 0, 'recorded_at': NOW-1}), 'unbound')]
        with self.live(worker=False, anchor=False):
            self.assertEqual(self.observe()['evidence']['receipt']['verdict'], 'missing')
            for content, expected in cases:
                with self.subTest(expected=expected, content=content[:30]):
                    self.receipt(content)
                    result = self.observe()
                    evidence = result['evidence']['receipt']
                    self.assertEqual(evidence['verdict'], expected)
                    self.assertFalse(evidence.get('terminal_authority', False))
                    self.assertEqual(result['persisted_status'], 'Run')

    def test_stale_future_and_contradictory_receipts_never_authorize_completion(self):
        for timestamp in (899.0, NOW+1):
            with self.subTest(timestamp=timestamp):
                self.receipt(json.dumps({'exit_code': 0, 'recorded_at': timestamp}))
                with self.live(worker=False, anchor=False):
                    result = self.observe()
                self.assertEqual(result['evidence']['receipt']['verdict'], 'stale')
                self.assertFalse(result['evidence']['receipt']['terminal_authority'])
        self.update(exit_code=1)
        self.receipt(json.dumps({'exit_code': 0, 'recorded_at': NOW-1}))
        with self.live(worker=False, anchor=False):
            result = self.observe()
        self.assertEqual(result['evidence']['receipt']['verdict'], 'contradictory')
        self.assertFalse(result['evidence']['receipt']['terminal_authority'])

    def test_redirected_receipt_is_not_read(self):
        target = self.root/'unrelated.json'
        target.write_text(json.dumps({'exit_code': 0, 'recorded_at': NOW-1}))
        (self.attempt/'payload-exit.json').symlink_to(target)
        with self.live(worker=False, anchor=False):
            result = self.observe()
        self.assertEqual(result['evidence']['receipt']['verdict'], 'unavailable')
        self.assertEqual(json.loads(target.read_text())['exit_code'], 0)

    def test_receipt_changed_during_read_is_not_published_as_stable(self):
        self.receipt(json.dumps({'exit_code': 0, 'recorded_at': NOW-1}))
        read = os.read
        def change(fd, size):
            result = read(fd, size)
            self.receipt(json.dumps({'exit_code': 121, 'recorded_at': NOW-1}))
            return result
        with self.live(worker=False, anchor=False), patch.object(os, 'read', side_effect=change):
            result = self.observe()
        self.assertEqual(result['evidence']['receipt']['verdict'], 'unavailable')

    def test_schema_three_and_other_unknown_versions_are_refused_without_probe_or_migration(self):
        for schema in (3, 99, True, '2'):
            with self.subTest(schema=schema):
                with closing(sqlite3.connect(self.store.path)) as con, con:
                    raw = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
                    raw['schema'] = schema
                    con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(raw),))
                before = self.store.path.read_bytes()
                with processes({}) as lookup:
                    result = self.observe()
                self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
                self.assertEqual(result['evidence']['source']['verdict'], 'unavailable')
                self.assertEqual(self.store.path.read_bytes(), before)
                lookup.assert_not_called()

    def test_schema_one_is_observed_without_migration(self):
        with closing(sqlite3.connect(self.store.path)) as con, con:
            raw = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
            raw['schema'] = 1
            con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(raw),))
        before = self.store.path.read_bytes()
        with self.live():
            result = self.observe()
        self.assertEqual(result['evidence']['source']['schema'], 1)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_missing_database_is_not_created(self):
        missing = Store(self.root/'missing-state')
        with processes({}) as lookup:
            result = lifecycle.observe_execution(missing, self.job, now=NOW).to_dict(now=NOW)
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.assertFalse(missing.root.exists())
        lookup.assert_not_called()

    def test_stale_supplied_row_is_refused_before_process_observation(self):
        self.store.annotate(self.job['id'], 'valid separate note')
        with processes({}) as lookup:
            stale = {**self.job, 'claim': 'stale-claim'}
            result = lifecycle.observe_execution(self.store, stale, now=NOW).to_dict(now=NOW)
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        lookup.assert_not_called()

    def test_controller_authority_change_during_observation_invalidates_result(self):
        original = lifecycle.observe_process
        updated = False
        def update(record):
            nonlocal updated
            if not updated:
                updated = True
                with self.store.transaction() as con:
                    control = self.store.control(con)
                    control.update(owner=self.controller, dispatch=True)
                    self.store.set_control(con, control)
            return original(record)
        with self.hold(self.lock), self.live(), patch.object(lifecycle, 'observe_process', side_effect=update):
            result = self.observe()
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.assertEqual(result['evidence']['source']['verdict'], 'unavailable')
        self.assertTrue(self.store.snapshot(now=NOW)['controller']['dispatch'])

    def test_controller_stale_heartbeat_is_not_worker_death(self):
        with self.store.transaction() as con:
            control = self.store.control(con)
            control.update(owner=self.controller, heartbeat=NOW-60)
            self.store.set_control(con, control)
        with self.hold(self.lock, self.control_lock), self.live(controller=True):
            result = self.observe()
        self.assertEqual(result['evidence']['controller']['verdict'], 'stale')
        self.assertEqual(result['observed_execution'], 'VERIFIED_WORKER')
        self.assertFalse(result['reconciliation_required'])

    def test_serialized_result_cannot_mutate_retained_observation(self):
        with self.hold(self.lock), self.live():
            result = lifecycle.observe_execution(self.store, self.job, now=NOW)
        changed = result.to_dict(now=NOW)
        changed['evidence']['worker']['verdict'] = 'gone'
        changed['state_identity']['root'] = '/unrelated'
        intact = result.to_dict(now=NOW)
        self.assertEqual(intact['evidence']['worker']['verdict'], 'matched')
        self.assertEqual(intact['state_identity']['root'], str(self.store.root))

    def test_database_replaced_during_probes_invalidates_original_binding(self):
        observe = lifecycle.observe_process
        replaced = False
        def replace(record):
            nonlocal replaced
            if not replaced:
                replaced = True
                old = self.store.path.with_suffix('.old')
                self.store.path.rename(old)
                self.store.path.write_bytes(old.read_bytes())
            return observe(record)
        with self.hold(self.lock), self.live(), patch.object(lifecycle, 'observe_process', side_effect=replace):
            result = self.observe()
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.assertEqual(result['evidence']['source']['verdict'], 'unavailable')

    def test_redirected_database_is_not_opened_or_adopted(self):
        target = self.root/'unrelated.sqlite3'
        self.store.path.rename(target)
        self.store.path.symlink_to(target)
        before = target.read_bytes()
        with processes({}) as lookup:
            result = self.observe()
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.assertEqual(target.read_bytes(), before)
        lookup.assert_not_called()

    def test_controller_lock_without_owner_does_not_claim_offline_authority(self):
        with self.hold(self.control_lock), self.live(worker=False, anchor=False):
            result = self.observe()
        self.assertEqual(result['evidence']['controller']['verdict'], 'unavailable')
        self.assertEqual(result['evidence']['controller']['lock']['verdict'], 'held')

    def test_worker_death_or_stop_after_first_check_invalidates_positive_result(self):
        receipt = lifecycle._observe_receipt
        for late_status, expected in [(None, 'OBSERVATION_UNAVAILABLE'),
                                      (psutil.STATUS_STOPPED, 'OBSERVATION_UNAVAILABLE'),
                                      (psutil.STATUS_RUNNING, 'VERIFIED_WORKER')]:
            with self.subTest(late_status=late_status):
                records = {self.worker['pid']: (self.worker, psutil.STATUS_SLEEPING),
                           self.anchor['pid']: (self.anchor, psutil.STATUS_SLEEPING)}
                def change(*args):
                    result = receipt(*args)
                    if late_status is None:
                        records.pop(self.worker['pid'])
                    else:
                        records[self.worker['pid']] = (self.worker, late_status)
                    return result
                with self.hold(self.lock), processes(records), \
                     patch.object(os, 'getsid', return_value=self.anchor['pid']), \
                     patch.object(os, 'getpgid', return_value=self.anchor['pid']), \
                     patch.object(lifecycle, '_observe_receipt', side_effect=change):
                    result = self.observe()
                self.assertEqual(result['observed_execution'], expected)

    def test_replaced_lock_after_first_check_invalidates_positive_result(self):
        receipt = lifecycle._observe_receipt
        def replace(*args):
            result = receipt(*args)
            self.lock.rename(self.lock.with_name('original.lock'))
            self.lock.write_text('new unheld lock')
            return result
        with self.hold(self.lock), self.live(), patch.object(lifecycle, '_observe_receipt', side_effect=replace):
            result = self.observe()
        self.assertEqual(result['observed_execution'], 'OBSERVATION_UNAVAILABLE')
        self.assertEqual(result['evidence']['source']['verdict'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
