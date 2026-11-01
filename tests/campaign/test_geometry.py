"""Capture settlement checks and a live local resize sequence over invented data."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from tests.campaign.geometry import GeometryProbe, RESIZE_SEQUENCE
from tests.campaign.support import assert_native, fixture
from tests.jobs.test_navigation_ui import SyntheticObserver, SyntheticSampler

try:
    from cmw.jobs.store import Store
    from cmw.jobs.tui import JobsApp
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Jobs UI extra unavailable')
class GeometryTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_native_resize_sequence_keeps_painted_fields(self):
        with tempfile.TemporaryDirectory(prefix='cmw-resize-') as temporary:
            record = fixture(Path(temporary).resolve() / 'fixture', 30)
            app = JobsApp(Store(record['state']), observer=SyntheticObserver(), sampler=SyntheticSampler())
            probe = GeometryProbe()
            async with app.run_test(size=RESIZE_SEQUENCE[0]) as pilot:
                async def wait_for_progress():
                    while app.progress_snapshot is None or not app.progress_snapshot.metrics:
                        await pilot.pause(.025)
                await asyncio.wait_for(wait_for_progress(), timeout=5)
                target = app._selected_target
                for size in RESIZE_SEQUENCE:
                    await pilot.press('enter')
                    await pilot.resize_terminal(*size)
                    await pilot.press('escape')
                    state = await probe.settled(app, size, require_visible=True)
                    assert_native(app.progress_snapshot, 1)
                    self.assertEqual(state['selected'], target)
                    painted = '\n'.join(state['painted_card'])
                    for value in ('rms', '1e-04', '|d eps|', '4e-08', '|dE|', '5e-08',
                                  'EDIFF', '1E-6', 'matched', 'data', '0.5', 'N/A'):
                        self.assertIn(value, painted)
                    self.assertGreaterEqual(len(state['visible']), 3)
                    machine = app.screen.query_one('#machine')
                    self.assertTrue(machine.display)
                    self.assertGreater(machine.content_region.height, 0)

    async def test_settlement_deadline_includes_refresh_wait(self):
        class UnrefreshedScreen:
            async def wait_for_refresh(self):
                await asyncio.Event().wait()
        class App:
            screen = UnrefreshedScreen()
        class Probe(GeometryProbe):
            def snapshot(self, app):
                return {'terminal_size': None, 'app_size': [77, 24], 'screen_size': [77, 24],
                    'modal_open': False, 'frame_widths': [93] * 8, 'card_widths': [93] * 8,
                    'painted_frame_widths': [93] * 8, 'shortcut_strip_visible': True,
                    'card_content_region': [1, 13, 74, 8], 'card_lines': 8,
                    'painted_shortcuts': ['Q Detach'], 'pending_restores': 1, 'pending_layout': True,
                    'scroll': [0, 0], 'scroll_target': [0, 0], 'selected': 'attempt', 'cursor': 'attempt',
                    'selected_visible': True, 'card_layout_generation': 1}
        with self.assertRaises(AssertionError) as caught:
            await Probe().settled(App(), (77, 24), timeout=.03)
        failure = caught.exception.args[0]
        self.assertEqual(failure['geometry_deadline_seconds'], .03)
        self.assertIn('card frame retains a different width', failure['problems'])
        self.assertIn('layout or navigation restoration is pending', failure['problems'])
