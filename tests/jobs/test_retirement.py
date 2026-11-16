"""Administrative retirement never infers success or sends process control."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import psutil

from cmw.jobs import retirement, runtime
from cmw.jobs.cli import handle, register
from cmw.jobs.ownership import OwnershipStatus, exclusive, identity, observe_owner
from cmw.jobs.store import JobsError, Store
from tests.fixture_processes import register_process
from tests.jobs.isolated_runtime import install


class RetirementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cmw-retire-sandbox-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root / 'queue')
        self.boot = str(uuid4())
        self.gone = {'version': 2, 'host': socket.gethostname(), 'boot_session': self.boot,
                     'pid': 900001, 'start_token': 123}
        self.job = self.store.add(argv=['/bin/true'], cwd=self.root, name='retirement fixture')
        (self.store.root / 'attempts' / self.job['attempt_id']).mkdir(parents=True, mode=0o700)
        with self.store.transaction() as con:
            self.job.update(status='Unknown', worker={**self.gone, 'pid': 900002}, group=self.gone,
                            claim=uuid4().hex, started_at=1, reason='Recorded worker gone')
            self.store.save(con, self.job)
        # No test census observes unrelated or production processes.
        self.pids = []
        self.addCleanup(patch.stopall)
        patch('cmw.jobs.ownership.psutil.pids', side_effect=lambda: list(self.pids)).start()

    def read(self):
        with self.store.read_transaction() as con:
            return self.store.get(con, self.job['id'])

    def update(self, **fields):
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job.update(fields)
            self.store.save(con, job)
        return job

    def retire(self):
        return retirement.retire(self.store, self.job['id'], reason='Independently reviewed; all owners gone')

    def refuse(self, status, contains):
        before = self.store.path.read_bytes()
        with self.assertRaises(retirement.RetirementRefused) as caught:
            self.retire()
        self.assertEqual(caught.exception.result['status'], status)
        self.assertIn(contains, str(caught.exception))
        self.assertEqual(self.store.path.read_bytes(), before)
        return caught.exception

    def test_all_gone_retired_once_without_success_or_files_or_signals(self):
        other = self.store.add(argv=['/bin/true'], cwd=self.root, name='unaffected')
        with self.store.read_transaction() as con:
            original_other = self.store.get(con, other['id'])
        attempt = self.store.root / 'attempts' / self.job['attempt_id']
        before = {p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}
        with patch('os.kill', side_effect=AssertionError('signal')), patch('os.killpg', side_effect=AssertionError('signal')):
            result = self.retire()
        self.assertEqual(result['status'], 'RETIRED')
        job = self.read()
        self.assertEqual(job['status'], 'Fail')
        self.assertIsNone(job['exit_code'])
        self.assertIsNone(job['finished_at'])
        self.assertIsNone(job['signal'])
        self.assertEqual(job['retirement']['execution_outcome'], 'unknown')
        self.assertEqual(job['retirement']['operator']['euid'], os.geteuid())
        self.assertGreater(job['retirement']['time'], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()})
        with self.store.read_transaction() as con:
            self.assertEqual(self.store.get(con, other['id']), original_other)
            self.assertFalse(self.store.control(con)['dispatch'])
            events = [r[0] for r in con.execute('SELECT message FROM events WHERE job_id=?', (job['id'],))]
        self.assertEqual(sum(e.startswith('Unknown job retired: ') for e in events), 1)
        event = json.loads(next(e.split(': ', 1)[1] for e in events if e.startswith('Unknown job retired: ')))
        self.assertEqual(event, job['retirement'])
        after = self.store.path.read_bytes()
        again = retirement.retire(self.store, job['id'], reason='A different repeated reason')
        self.assertEqual(again['status'], 'ALREADY_RETIRED')
        self.assertEqual(self.store.path.read_bytes(), after)

    def test_dispatch_admission_proceeds_after_retirement(self):
        pending = self.store.add(argv=['/bin/true'], cwd=self.root, name='next')
        with self.assertRaises(JobsError):
            self.store.dispatch(True)
        self.retire()
        self.store.dispatch(True)
        with self.store.read_transaction() as con:
            self.assertTrue(self.store.control(con)['dispatch'])
            self.assertEqual(self.store.get(con, pending['id'])['status'], 'Queue')

    def start_controller(self):
        isolation = install()
        self.addCleanup(isolation.close)
        runtime.start(self.store)
        children = list(runtime._CHILDREN)
        owners = [(child, identity(child.pid)) for child in children if child.poll() is None]
        def cleanup():
            runtime.stop(self.store)
            for child, owner in owners:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if observe_owner(owner).status == OwnershipStatus.VERIFIED:
                        child.terminate()
                    child.wait(timeout=5)
                self.assertEqual(observe_owner(owner).status, OwnershipStatus.GONE)
            runtime.reap_detached()
        self.addCleanup(cleanup)

    def wait(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail('Synthetic controller did not reach the required state: ' + json.dumps(self.store.snapshot()))

    def test_real_controller_dispatches_next_job_after_retirement(self):
        next_job = self.store.add(argv=[sys.executable, '-c', 'pass'], cwd=self.root, name='dispatch after retirement')
        self.retire()
        self.start_controller()
        def done():
            with self.store.read_transaction() as con:
                return self.store.get(con, next_job['id'])['status'] == 'Done'
        self.wait(done)
        self.assertEqual(self.read()['status'], 'Fail')

    def test_running_controller_refuses_without_changing_job(self):
        self.start_controller()
        def online():
            with self.store.read_transaction() as con:
                return self.store.control(con)['owner'] is not None
        self.wait(online)
        original = self.read()
        with self.assertRaises(retirement.RetirementRefused) as caught:
            self.retire()
        self.assertEqual(caught.exception.result['status'], 'REFUSED')
        self.assertIn('controller.lock', str(caught.exception))
        self.assertEqual(self.read(), original)

    def test_other_unknown_still_blocks_dispatch(self):
        other = self.store.add(argv=['/bin/true'], cwd=self.root, name='other unknown')
        with self.store.transaction() as con:
            other.update(status='Unknown')
            self.store.save(con, other)
        self.retire()
        with self.assertRaises(JobsError):
            self.store.dispatch(True)

    def test_live_worker_refuses(self):
        self.update(worker=identity())
        self.refuse('REFUSED', 'job.worker')

    def test_live_recorded_descendant_refuses_even_when_anchor_is_gone(self):
        self.update(recorded_descendants=[identity()])
        self.refuse('REFUSED', 'job.recorded_descendants[0]')

    def test_nested_descendant_of_identity_record_is_checked(self):
        self.update(group={**self.gone, 'descendants': [identity()]})
        self.refuse('REFUSED', 'job.group.descendants[0]')

    def test_controller_recorded_owner_must_be_gone(self):
        with self.store.transaction() as con:
            control = self.store.control(con)
            control['owner'] = identity()
            self.store.set_control(con, control)
        self.refuse('REFUSED', 'controller.owner')

    def test_v2_previous_boot_never_probes_current_pid_or_numeric_domains(self):
        with patch('cmw.jobs.ownership.process_identity.process_evidence', side_effect=AssertionError('PID probe')), \
             patch('cmw.jobs.retirement.group_members', side_effect=AssertionError('old numeric domain probe')):
            result = self.retire()
        evidence = result['retirement']['evidence']
        self.assertTrue(all(row['observation']['deciding_check'] == 'boot_session'
                            for row in evidence['identities'].values()))
        self.assertTrue(all(row['basis'] == 'previous boot session' for row in evidence['domains']))

    def test_v2_same_boot_confirmed_absence_passes(self):
        current = identity()
        self.update(group=current, worker=current)
        with patch('cmw.jobs.ownership.process_identity.process_evidence', side_effect=ProcessLookupError):
            result = self.retire()
        self.assertTrue(all(row['observation']['deciding_check'] == 'pid'
                            for row in result['retirement']['evidence']['identities'].values()))

    def test_previous_boot_domain_with_gone_current_boot_controller(self):
        with self.store.transaction() as con:
            control = self.store.control(con)
            control['owner'] = identity()
            self.store.set_control(con, control)
        with patch('cmw.jobs.ownership.process_identity.process_evidence', side_effect=ProcessLookupError), \
             patch('cmw.jobs.retirement.group_members', side_effect=AssertionError('reused domain')):
            self.assertEqual(self.retire()['status'], 'RETIRED')

    def test_v1_reboot_mismatch_blocks_even_when_pid_is_absent(self):
        legacy = {'host': socket.gethostname(), 'pid': 900003, 'boot': 100., 'birth': 110.}
        self.update(worker=legacy, group=legacy)
        with patch('cmw.jobs.ownership.psutil.boot_time', return_value=200.), \
             patch('cmw.jobs.ownership.psutil.Process', side_effect=AssertionError('must not probe')):
            error = self.refuse('UNVERIFIABLE', 'Legacy v1 mismatch')
        self.assertEqual(error.exit_code, 75)
        self.assertIn('earlier-boot proof', str(error))

    def test_v1_pid_reuse_is_unverifiable_and_names_safe_remedy(self):
        legacy = {'host': socket.gethostname(), 'pid': 900003, 'boot': 100., 'birth': 110.}
        self.update(worker=legacy, group=legacy)
        with patch('cmw.jobs.ownership.psutil.boot_time', return_value=100.), \
             patch('cmw.jobs.ownership.psutil.Process', return_value=Mock(create_time=lambda: 120.)):
            error = self.refuse('UNVERIFIABLE', 'Do not rewrite the identity or signal a reused PID')
        self.assertIn('naturally exits', str(error))

    def test_v1_confirmed_absence_with_matching_boot_passes(self):
        legacy = {'host': socket.gethostname(), 'pid': 900003, 'boot': 100., 'birth': 110.}
        self.update(worker=legacy, group=legacy)
        with patch('cmw.jobs.ownership.psutil.boot_time', return_value=100.), \
             patch('cmw.jobs.ownership.psutil.Process', side_effect=psutil.NoSuchProcess(900003)):
            self.assertEqual(self.retire()['status'], 'RETIRED')

    def test_unreadable_identity_refuses(self):
        self.update(worker=identity())
        with patch('cmw.jobs.ownership.process_identity.process_evidence', side_effect=PermissionError):
            self.refuse('UNVERIFIABLE', 'PermissionError')

    def test_non_unknown_and_missing_domain_refuse(self):
        self.update(status='Run')
        self.refuse('REFUSED', 'Expected Unknown')
        self.update(status='Unknown', group=None)
        self.refuse('UNVERIFIABLE', 'No recorded execution domain')

    def test_malformed_recorded_member_refuses(self):
        self.update(members=[{'pid': 42}])
        self.refuse('UNVERIFIABLE', 'Incomplete or unsupported identity record')

    def test_metadata_runner_and_mpi_identities_are_not_omitted(self):
        directory = self.store.root / 'attempts' / self.job['attempt_id'] / 'suspension'
        directory.mkdir(mode=0o700)
        path = directory / 'registration.json'
        path.write_text(json.dumps({'runner': self.gone, 'root': identity(), 'protected': [self.gone]}))
        self.refuse('REFUSED', 'registration.json.root')

    def test_incomplete_recorded_runner_and_members_refuse(self):
        directory = self.store.root / 'attempts' / self.job['attempt_id'] / 'suspension'
        directory.mkdir(mode=0o700)
        path = directory / 'registration.json'
        for record in ({'root': self.gone, 'runner': {}}, {'root': self.gone},
                       {'runner': self.gone, 'root': self.gone, 'protected': [{}]}):
            with self.subTest(record=record):
                path.write_text(json.dumps(record))
                self.refuse('UNVERIFIABLE', 'Incomplete or unsupported identity record')
        path.unlink()
        self.update(recorded_descendants=[{'identity': {}}])
        self.refuse('UNVERIFIABLE', 'job.recorded_descendants[0].identity')

    def test_malformed_recorded_group_list_refuses(self):
        self.update(process_groups='not a recorded list')
        self.refuse('UNVERIFIABLE', 'Malformed recorded process-domain list')

    def test_metadata_changes_refuse_before_commit(self):
        original = retirement._attempt_metadata
        count = 0
        def changed(*args):
            nonlocal count
            count += 1
            result = original(*args)
            return result if count == 1 else {'added.json': {'value': {}, 'sha256': 'changed'}}
        with patch('cmw.jobs.retirement._attempt_metadata', side_effect=changed):
            self.refuse('UNVERIFIABLE', 'metadata changed')

    def test_missing_attempt_directory_is_not_empty_ownership_evidence(self):
        (self.store.root / 'attempts' / self.job['attempt_id']).rmdir()
        self.refuse('UNVERIFIABLE', 'Recorded attempt directory is missing')

    def test_recorded_capability_requires_its_original_metadata(self):
        self.update(suspension={'capability': {'runner': self.gone}})
        self.refuse('UNVERIFIABLE', 'lost its ownership metadata')

    def test_symlink_attempt_metadata_is_not_followed(self):
        directory = self.store.root / 'attempts' / self.job['attempt_id']
        external = self.root / 'not-evidence.json'
        external.write_text('{}')
        (directory / 'members.json').symlink_to(external)
        self.refuse('UNVERIFIABLE', 'retirement observation')

    def test_held_worker_lock_refuses(self):
        path = self.store.root / 'attempts' / self.job['attempt_id'] / 'worker.lock'
        with exclusive(path):
            self.refuse('REFUSED', 'worker.lock')

    def test_runner_lock_and_recorded_process_group_are_checked(self):
        directory = self.store.root / 'attempts' / self.job['attempt_id'] / 'suspension'
        directory.mkdir(mode=0o700)
        with exclusive(directory / 'runner.lock'):
            self.refuse('REFUSED', 'runner.lock')
        self.update(group=identity(), worker=identity(), process_groups=[12456])
        def census(number, *, session):
            return [99] if number == 12456 else []
        with patch('cmw.jobs.ownership.process_identity.process_evidence', side_effect=ProcessLookupError), \
             patch('cmw.jobs.retirement.group_members', side_effect=census):
            self.refuse('REFUSED', 'group 12456')

    def test_live_unrecorded_session_member_refuses(self):
        # Same-boot anchor is confirmed absent; its recorded SID still has a member.
        self.update(group=identity(), worker=identity())
        with patch('cmw.jobs.ownership.process_identity.process_evidence', side_effect=ProcessLookupError), \
             patch('cmw.jobs.retirement.group_members', return_value=[54321]):
            self.refuse('REFUSED', 'Live members remain: [54321]')

    def test_unreadable_group_census_refuses(self):
        self.update(group=identity(), worker=identity())
        with patch('cmw.jobs.ownership.process_identity.process_evidence', side_effect=ProcessLookupError), \
             patch('cmw.jobs.retirement.group_members', side_effect=PermissionError('census denied')):
            self.refuse('UNVERIFIABLE', 'census denied')

    def test_concurrent_retirements_commit_exactly_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.retire()['status'], range(2)))
        self.assertCountEqual(results, ['RETIRED', 'ALREADY_RETIRED'])

    def test_controller_writer_cannot_change_intent_inside_retirement(self):
        entered, attempted, complete = threading.Event(), threading.Event(), threading.Event()
        original = retirement._gone_evidence
        def evidence(*args):
            entered.set()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(complete.wait(.05))
            return original(*args)
        def controller_tick():
            self.assertTrue(entered.wait(2))
            attempted.set()
            with self.store.transaction() as con:
                job = self.store.get(con, self.job['id'])
                self.assertEqual(job['status'], 'Fail')
            complete.set()
        with ThreadPoolExecutor(max_workers=1) as pool, \
             patch('cmw.jobs.retirement._gone_evidence', side_effect=evidence):
            future = pool.submit(controller_tick)
            self.retire()
            future.result(timeout=3)
        self.assertTrue(complete.is_set())

    def cli(self, *options):
        import argparse
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers(dest='command', required=True))
        args = parser.parse_args(['jobs', '--state', str(self.store.root), *options])
        output = io.StringIO()
        with redirect_stdout(output):
            code = handle(args)
        return code, output.getvalue()

    def test_cli_and_help_are_local_and_explicit(self):
        code, output = self.cli('retire', self.job['display_id'], '--reason', 'reviewed', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'RETIRED')
        with patch('cmw.nodes.jobs_cli.handle', side_effect=AssertionError('endpoint')):
            code, output = self.cli('--node', 'never-contact', 'retire', self.job['display_id'], '--reason', 'reviewed')
        self.assertEqual(code, 2)
        self.assertIn('local only', output)

    def test_cli_unverifiable_exit_75(self):
        self.update(worker={'pid': 12})
        code, output = self.cli('retire', self.job['display_id'], '--reason', 'reviewed', '--json')
        self.assertEqual(code, 75)
        self.assertEqual(json.loads(output)['status'], 'UNVERIFIABLE')

    def test_help_names_supported_recovery_and_legacy_reboot_limit(self):
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exited:
            self.cli('retire', '--help')
        self.assertEqual(exited.exception.code, 0)
        text = ' '.join(output.getvalue().split())
        for phrase in ('worker death', 'restart/reboot', 'contradictory', 'UNVERIFIABLE', 'including after reboot'):
            self.assertIn(phrase, text)

    def test_deployed_31616be_schema4_live_v1_fixture_retires_after_exit(self):
        from tests.jobs.retirement_legacy_fixture import capture_v1, create
        children = []
        for role in ('historical worker', 'historical anchor'):
            child = subprocess.Popen([sys.executable, '-B', '-c', 'import sys; sys.stdin.read()'],
                                     stdin=subprocess.PIPE, start_new_session=True)
            self.addCleanup(self.reap_child, child)
            current = identity(child.pid)
            register_process(child, role='retirement ' + role)
            children.append((child, current))
        def cleanup():
            for child, current in children:
                if not child.stdin.closed:
                    child.stdin.close()
                child.wait(timeout=5)
                self.assertEqual(observe_owner(current).status, OwnershipStatus.GONE)
        self.addCleanup(cleanup)
        worker, anchor = [capture_v1(child.pid) for child, _ in children]
        job, enrolled = create(self.root / 'legacy-queue', worker=worker, group=anchor,
                               controller=worker, members=[anchor])
        self.assertNotIn('version', worker)
        self.assertEqual(set(worker), {'pid', 'host', 'boot', 'birth'})
        candidate = Store(self.root / 'legacy-queue')
        with candidate.read_transaction() as con:
            self.assertEqual(candidate.control(con)['schema'], 4)
            self.assertEqual(candidate.get(con, job['id'])['status'], 'Unknown')
        self.assertEqual(candidate.queue_identity(), enrolled)
        for child, _ in children:
            child.stdin.close()
            child.wait(timeout=5)
        result = retirement.retire(candidate, job['id'], reason='Historical writer compatibility fixture')
        self.assertEqual(result['status'], 'RETIRED')
        self.assertEqual(candidate.queue_identity(), enrolled)
        with candidate.read_transaction() as con:
            self.assertEqual(candidate.control(con)['schema'], 4)
            self.assertEqual(candidate.get(con, job['id'])['status'], 'Fail')
        self.assertTrue(all(row['observation']['record_version'] == 1 and
                            row['observation']['deciding_check'] == 'pid'
                            for row in result['retirement']['evidence']['identities'].values()))

    def test_native_v2_process_is_gone_after_exit(self):
        child = subprocess.Popen([sys.executable, '-B', '-c', 'import sys; sys.stdin.read()'],
                                 stdin=subprocess.PIPE, start_new_session=True)
        self.addCleanup(self.reap_child, child)
        owner = identity(child.pid)
        register_process(child, role='retirement native child')
        def cleanup():
            if child.poll() is None:
                child.stdin.close()
            child.wait(timeout=5)
            self.assertEqual(observe_owner(owner).status, OwnershipStatus.GONE)
        self.addCleanup(cleanup)
        self.pids.append(child.pid)
        self.update(group=owner, worker=owner)
        self.refuse('REFUSED', 'VERIFIED')
        child.stdin.close()
        child.wait(timeout=5)
        self.pids.clear()
        self.assertEqual(self.retire()['status'], 'RETIRED')

    @staticmethod
    def reap_child(child):
        if not child.stdin.closed:
            child.stdin.close()
        child.wait(timeout=5)
