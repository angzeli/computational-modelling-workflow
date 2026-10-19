"""Progress association cannot change the independently owned control protocol."""
from copy import deepcopy
from pathlib import Path
import signal
import unittest
from unittest.mock import patch

import psutil

from cmw.jobs import suspension
from cmw.jobs.locations import LocationDescriptor
from cmw.jobs.progress import resolve_context
from cmw.jobs.source_declarations import recorded_sources
from cmw.jobs.source_selection import activate_selection, validate_selection
from cmw.jobs.store import JobsError, Store
from cmw.progress.models import SourceDescriptor
from tests.jobs import test_cancellation_coordinator, test_suspension_state


class SourceControlIntegrationTests(unittest.TestCase):
    def fixture(self, fixture_type):
        # Compose the existing mocked process/clock fixture without inheriting
        # and rerunning its unrelated tests. Declarations must enter at add.
        fixture = fixture_type(methodName='runTest')
        self.addCleanup(fixture.doCleanups)
        original_add = Store.add

        def add_with_sources(store, **arguments):
            output = Path(arguments['cwd']) / 'progress'
            output.mkdir()
            sources = tuple(SourceDescriptor(role, str(output / name))
                            for role, name in [('outcar', 'OUTCAR'), ('oszicar', 'OSZICAR')])
            for source in sources:
                Path(source.path).write_text('Synthetic association evidence only\n')
            arguments.update(engine='VASP', progress_sources=sources,
                             suspension_backend='local-command-v1')
            return original_add(store, **arguments)

        with patch.object(Store, 'add', new=add_with_sources):
            fixture.setUp()
        return fixture

    def test_declared_sources_survive_enrollment_control_and_legacy_save(self):
        fixture = self.fixture(test_suspension_state.StateTests)
        store, job_id = fixture.store, fixture.job['id']
        original = fixture.current()
        record = deepcopy(original['progress_sources'])
        coordinator = fixture.coordinator()
        self.assertEqual(store.snapshot()['schema'], 3)
        self.assertEqual(resolve_context(store, job_id).binding_status, 'BOUND')

        for action, pending, completed in [('suspend', 'Suspending', 'Suspended'),
                                           ('continue', 'Resuming', 'Run')]:
            with self.subTest(action=action):
                preview = suspension.readiness(store, job_id, action)
                self.assertTrue(preview['ready'], preview['reasons'])
                result = suspension.submit(store, job_id, action,
                    expected_attempt_id=original['attempt_id'],
                    expected_revision=preview['revision'], expected_phase=preview['phase'],
                    expected_authority=preview['authority'])
                self.assertEqual((result['result'], result['phase']), ('ACCEPTED', pending))
                legacy = fixture.current()
                legacy.pop('progress_sources')
                coordinator.update(legacy, phase=completed)
                self.assertEqual(fixture.current()['progress_sources'], record)

        store.change(job_id, 'cancel', confirm=True)
        with store.transaction() as con:
            legacy = store.get(con, job_id)
            legacy.pop('progress_sources')
            legacy['reason'] = 'Legacy lifecycle save during cancellation'
            store.save(con, legacy)
        current = fixture.current()
        self.assertEqual(current['progress_sources'], record)
        self.assertEqual(current['status'], 'Cancelling')
        self.assertTrue(current['cancel_requested'])
        for field in ('attempt_id', 'worker', 'group', 'claim', 'resources'):
            self.assertEqual(current[field], original[field])
        fixture.send.assert_not_called()

    def test_p2_cancellation_retains_declarations_and_single_termination_sequence(self):
        fixture = self.fixture(test_cancellation_coordinator.CancellationCoordinatorTests)
        original = fixture.current()
        fixture.store.annotate(original['id'], 'Note changed while cancellation is pending')
        legacy = deepcopy(original)
        legacy.pop('progress_sources')
        fixture.coordinator.update(legacy, reason='Pending cancellation with source metadata')
        fixture.table.rows[11]['status'] = psutil.STATUS_STOPPED
        fixture.step()
        fixture.step()
        self.assertEqual(fixture.sent, [(11, signal.SIGTERM), (11, signal.SIGCONT)])
        current = fixture.current()
        self.assertEqual(current['progress_sources'], original['progress_sources'])
        self.assertEqual(current['note'], 'Note changed while cancellation is pending')
        self.assertEqual(current['suspension']['phase'], 'Suspended')
        self.assertFalse(fixture.coordinator.ready_to_release())
        fixture.assert_retained()
        self.assertEqual(resolve_context(fixture.store, original['id']).binding_status, 'BOUND')

    def test_confirmed_source_override_cannot_restore_changed_control_authority(self):
        fixture = self.fixture(test_cancellation_coordinator.CancellationCoordinatorTests)
        store, job_id = fixture.store, fixture.job['id']
        sources = recorded_sources(fixture.current())
        other = fixture.root / 'conflicting-output'
        other.mkdir()
        descriptors = (LocationDescriptor('output', other, origin='recognized-runner'),)
        context = resolve_context(store, job_id, descriptors=descriptors)
        self.assertEqual(context.binding_status, 'SOURCE_CONFLICT')
        with store.transaction() as con:
            current = store.get(con, job_id)
            current['worker'] = {**current['worker'], 'birth': 999.0}
            store.save(con, current)
        before = store.path.read_bytes()
        validation = validate_selection(store, job_id, sources, descriptors=descriptors)
        self.assertTrue(validation.valid)
        self.assertTrue(validation.conflict)
        selection = activate_selection(store, validation, 1, descriptors=descriptors, confirmed=True)
        selected = resolve_context(store, job_id, descriptors=descriptors, session_selection=selection)
        self.assertEqual(selected.binding_status, 'BOUND')
        self.assertTrue(selected.source_conflicts)
        self.assertEqual(store.path.read_bytes(), before)
        with self.assertRaisesRegex(JobsError, 'authority changed'):
            fixture.step()
        self.assertEqual(fixture.sent, [])
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(fixture.current()['progress_sources'], fixture.before['progress_sources'])


if __name__ == '__main__':
    unittest.main()
