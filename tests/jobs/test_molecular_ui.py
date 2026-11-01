from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

try:
    import textual
except ImportError:
    textual = None


class MolecularUiWaitTests(unittest.IsolatedAsyncioTestCase):
    def pilot(self):
        self.now = 0.

        async def pause(delay):
            self.now += delay

        return SimpleNamespace(app=SimpleNamespace(screen=object(), lanes={}),
                               pause=AsyncMock(side_effect=pause))

    async def test_serialized_endpoint_reads_complete_without_poll_count_limit(self):
        from tests.jobs import molecular_ui_acceptance as acceptance
        pilot = self.pilot()
        clock = SimpleNamespace(monotonic=lambda: self.now)
        # An in-flight read and the selected read each take 2.5 seconds.
        with patch.object(acceptance, 'time', clock):
            await acceptance._until(pilot, lambda: self.now >= 5)
        self.assertGreater(pilot.pause.await_count, 160)

    async def test_missing_observation_still_fails_at_deadline_with_diagnostics(self):
        from tests.jobs import molecular_ui_acceptance as acceptance
        pilot = self.pilot()
        pilot.app.lanes['MINI'] = SimpleNamespace(busy=True, active_request=('locations',),
                                                 pending=[('status',)], error=None)
        with patch.object(acceptance, 'time', SimpleNamespace(monotonic=lambda: self.now)):
            with self.assertRaisesRegex(AssertionError, 'screen=object.*locations.*status'):
                await acceptance._until(pilot, lambda: False, timeout=.1)
        self.assertAlmostEqual(self.now, .1)

    async def test_ready_observation_does_not_wait(self):
        from tests.jobs.molecular_ui_acceptance import _until
        pilot = self.pilot()
        await _until(pilot, lambda: True)
        pilot.pause.assert_not_awaited()

    async def test_back_waits_for_modal_close_and_navigation_restore(self):
        from tests.jobs import molecular_ui_acceptance as acceptance
        pilot = self.pilot()
        pilot.app.screen_stack = [object(), object()]
        pilot.app.navigation_pending = lambda: self.now < .1
        pilot.press = AsyncMock()

        async def pause(delay):
            self.now += delay
            if self.now >= .05:
                pilot.app.screen_stack = pilot.app.screen_stack[:1]

        pilot.pause.side_effect = pause
        with patch.object(acceptance, 'time', SimpleNamespace(monotonic=lambda: self.now)):
            await acceptance._back_to_portal(pilot)
        pilot.press.assert_awaited_once_with('escape')
        self.assertEqual(len(pilot.app.screen_stack), 1)
        self.assertFalse(pilot.app.navigation_pending())


@unittest.skipIf(textual is None, 'Optional Jobs console is not installed')
class MolecularUiTests(unittest.TestCase):
    def test_existing_frame_four_viewports_and_controls(self):
        from tests.jobs.molecular_ui_acceptance import molecular_ui_surface
        with tempfile.TemporaryDirectory(prefix='cmw-molecular-ui-') as temporary:
            report = molecular_ui_surface(Path(temporary)/'fixtures')
        self.assertEqual(len(report['views']), 24)
        self.assertEqual(len(report['controls']), 6)
        self.assertTrue(report['queue_unchanged'])
        self.assertEqual(report['issues'], [])

    def test_actual_endpoint_federated_four_profiles_and_controls(self):
        from tests.jobs.molecular_ui_acceptance import molecular_federated_ui_surface
        with tempfile.TemporaryDirectory(prefix='cmw-molecular-federated-ui-') as temporary:
            report = molecular_federated_ui_surface(Path(temporary)/'fixtures')
        self.assertEqual(len(report['views']), 16)
        self.assertEqual(len(report['controls']), 4)
        self.assertTrue(report['queue_unchanged'])
        self.assertTrue(report['actual_json_endpoint'])
        self.assertTrue(report['same_display_ids_isolated'])
