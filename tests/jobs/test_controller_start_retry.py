"""Controller replacement retries observations, never authority or launch effects."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cmw.jobs import ownership, runtime
from cmw.jobs.store import JobsError
from tests.jobs.ownership_fault_sweep import FAULTS, SingleFault
from tests.jobs.ownership_sweep_reporting import emit


class StartModel:
    """A private start lock and modeled queue/processes; no subprocess is started."""

    def __init__(self, root):
        self.root = root
        self.now, self.delays = 0., []
        self.owner = {'version': 2, 'host': 'fixture', 'pid': 10001,
                      'boot_session': '00000000-0000-0000-0000-000000000001', 'start_token': 1}
        self.record = dict(owner=self.owner, dispatch=False, stop=True, reason='Stopped')
        self.mutations, self.launches, self.commits = [], [], []
        self.launched = False
        self.status = ownership.OwnershipStatus.GONE
        self.locked = False
        self.child = SimpleNamespace(poll=lambda: None)
        self.old_child = SimpleNamespace(poll=lambda: 0)

    def ensure_private_root(self):
        pass

    def snapshot(self):
        return {'controller': dict(owner=self.owner, online=self.launched)}

    def control(self, con):
        return deepcopy(self.record)

    def rows(self, con):
        return []

    def set_control(self, con, record):
        self.mutations.append(deepcopy(record))
        self.record = deepcopy(record)

    @contextmanager
    def transaction(self):
        record = deepcopy(self.record)
        try:
            yield object()
        except BaseException:
            self.record = record
            raise
        else:
            self.commits.append(1)

    def observe(self, owner):
        return SimpleNamespace(status=self.status, deciding_check='start_token')

    def lock(self, path, *, strict=False):
        assert strict, 'Uncertainty must not be silently converted into lock ownership'
        return self.locked

    def launch(self, store):
        self.launches.append(1)
        self.launched = True
        return self.child

    def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay
        if self.now > 8:
            raise AssertionError('Controller start exceeded its bounded retry budget')

    def install(self, stack, injector):
        stack.enter_context(patch.object(runtime, '_CHILDREN', [self.old_child]))
        stack.enter_context(patch.object(runtime, 'observe_owner', injector.wrap('observe_owner', self.observe)))
        stack.enter_context(patch.object(runtime, 'lock_held', injector.wrap('lock_held', self.lock)))
        stack.enter_context(patch.object(runtime, '_launch_controller', side_effect=self.launch))
        stack.enter_context(patch.object(runtime.time, 'monotonic', side_effect=lambda: self.now))
        stack.enter_context(patch.object(runtime.time, 'sleep', side_effect=self.sleep))
        for name in ('snapshot', 'control', 'rows'):
            stack.enter_context(patch.object(self, name, injector.wrap('Store.' + name, getattr(self, name))))
        for child in (self.child, self.old_child):
            stack.enter_context(patch.object(child, 'poll', injector.wrap('child.poll', child.poll)))


class ControllerStartRetryTests(unittest.TestCase):
    def run_start(self, point=None, fault=None, setup=None):
        with tempfile.TemporaryDirectory(prefix='cmw-start-retry-') as temporary, ExitStack() as stack:
            model = StartModel(Path(temporary))
            injector = SingleFault(point, fault)
            model.install(stack, injector)
            if setup is not None:
                setup(model, stack)
            runtime.start(model)
            self.assertEqual(model.launches, [1])
            self.assertEqual(len(model.mutations), 1)
            self.assertEqual(model.commits, [1])
            self.assertTrue(model.record['dispatch'])
            self.assertFalse(model.record['stop'])
            self.assertLessEqual(model.now, 7.)
            return model, injector

    def test_single_fault_at_every_start_read_occurrence_recovers_without_duplicate_launch(self):
        _, baseline = self.run_start()
        self.assertTrue({'observe_owner', 'lock_held', 'Store.snapshot', 'Store.control',
                         'Store.rows', 'child.poll'} <= {point.api for point in baseline.trace})
        self.assertTrue(any(point.function.startswith('_prepare_start') for point in baseline.trace))
        rows = []
        expected_rows = len(baseline.trace) * len(FAULTS)
        try:
            for point in baseline.trace:
                for fault in FAULTS:
                    row = dict(point=point.label(), fault=fault, result='failed', injected_count=0)
                    rows.append(row)
                    with self.subTest(point=point.label(), fault=fault):
                        model, injection = self.run_start(point, fault)
                        row['injected_count'] = injection.fired
                        self.assertEqual(injection.fired, 1)
                        self.assertTrue(model.delays)
                        row['result'] = 'passed'
            self.assertEqual(len(rows), expected_rows)
        finally:
            summary = dict(scenario='controller_replacement', baseline_read_occurrences=len(baseline.trace),
                           expected_rows=expected_rows, attempted_rows=len(rows),
                           passed_rows=sum(row['result'] == 'passed' for row in rows),
                           failed_rows=sum(row['result'] == 'failed' for row in rows),
                           completed=len(rows) == expected_rows)
            emit('CONTROLLER_START', **summary)
            evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
            if evidence:
                output = Path(evidence) / 'controller-start-fault-sweep.json'
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(dict(summary=summary, rows=rows), indent=2) + '\n')

    def test_unverifiable_owner_then_gone_is_retried(self):
        def setup(model, stack):
            status = ownership.OwnershipStatus
            reads = Mock(side_effect=[SimpleNamespace(status=status.UNVERIFIABLE, deciding_check='start_token'),
                                      SimpleNamespace(status=status.GONE, deciding_check='process'),
                                      SimpleNamespace(status=status.GONE, deciding_check='process')])
            stack.enter_context(patch.object(runtime, 'observe_owner', reads))
        model, _ = self.run_start(setup=setup)
        self.assertEqual(model.delays, [.05])

    def test_gone_owner_waits_for_lock_release(self):
        def setup(model, stack):
            stack.enter_context(patch.object(runtime, 'lock_held', side_effect=[True, False, False]))
        model, _ = self.run_start(setup=setup)
        self.assertEqual(model.delays, [.05])

    def test_unverifiable_owner_that_recovers_to_verified_is_never_replaced(self):
        status = ownership.OwnershipStatus
        with tempfile.TemporaryDirectory(prefix='cmw-start-retry-') as temporary, ExitStack() as stack:
            model = StartModel(Path(temporary))
            model.install(stack, SingleFault())
            stack.enter_context(patch.object(runtime, 'observe_owner', side_effect=[
                SimpleNamespace(status=status.UNVERIFIABLE, deciding_check='start_token'),
                SimpleNamespace(status=status.VERIFIED, deciding_check='identity')]))
            with self.assertRaisesRegex(JobsError, 'still alive'):
                runtime.start(model)
            self.assertEqual(model.delays, [.05])
            self.assertEqual((model.mutations, model.launches, model.commits), ([], [], []))

    def test_persistent_uncertainty_has_a_bounded_retryable_refusal_without_mutation(self):
        for operand in ('identity', 'lock'):
            with self.subTest(operand=operand), tempfile.TemporaryDirectory(prefix='cmw-start-retry-') as temporary, \
                    ExitStack() as stack:
                model = StartModel(Path(temporary))
                model.install(stack, SingleFault())
                if operand == 'identity':
                    model.status = ownership.OwnershipStatus.UNVERIFIABLE
                else:
                    model.locked = True
                with self.assertRaisesRegex(runtime.ControllerStartUnavailable, 'ownership temporarily unverifiable; retry start') as caught:
                    runtime.start(model)
                self.assertEqual(caught.exception.code, 'OWNERSHIP_UNVERIFIABLE')
                self.assertIn('controller.', caught.exception.deciding_check)
                self.assertEqual(model.now, 2.)
                self.assertLessEqual(max(model.delays), .25)
                self.assertEqual((model.mutations, model.launches, model.commits), ([], [], []))
                model.status, model.locked = ownership.OwnershipStatus.GONE, False
                runtime.start(model)
                self.assertEqual((len(model.mutations), len(model.launches), len(model.commits)), (1, 1, 1))

    def test_identity_is_revalidated_after_the_transactional_queue_read(self):
        with tempfile.TemporaryDirectory(prefix='cmw-start-retry-') as temporary, ExitStack() as stack:
            model = StartModel(Path(temporary))
            model.install(stack, SingleFault())
            def rows(con):
                model.status = ownership.OwnershipStatus.VERIFIED
                return []
            stack.enter_context(patch.object(model, 'rows', side_effect=rows))
            with self.assertRaisesRegex(JobsError, 'still alive'):
                runtime.start(model)
            self.assertEqual((model.mutations, model.launches, model.commits), ([], [], []))

    def test_ambiguous_prepare_commit_is_not_replayed(self):
        with tempfile.TemporaryDirectory(prefix='cmw-start-retry-') as temporary, ExitStack() as stack:
            model = StartModel(Path(temporary))
            model.install(stack, SingleFault())
            @contextmanager
            def transaction():
                yield object()
                raise OSError('ambiguous commit outcome')
            stack.enter_context(patch.object(model, 'transaction', transaction))
            with self.assertRaisesRegex(OSError, 'ambiguous commit'):
                runtime.start(model)
            self.assertEqual(len(model.mutations), 1)
            self.assertEqual(model.launches, [])
            self.assertEqual(model.delays, [])
