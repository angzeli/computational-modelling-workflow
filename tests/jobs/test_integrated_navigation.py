"""Seeded real-keyboard navigation through catalogue, Details and Doctor."""
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.store import Store
from tests.jobs.test_navigation_ui import SyntheticObserver, SyntheticSampler, table_view
try:
    from cmw.jobs.tui import JobsApp, Inspect
    from cmw.jobs.catalogue_ui import CatalogueSearch
    from cmw.doctor.presentation import DoctorReport
    from textual.widgets import DataTable, Input
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Optional Jobs UI unavailable')
class IntegratedNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, pilot, predicate):
        for _ in range(120):
            if predicate():
                await pilot.pause()
                return
            await pilot.pause(.025)
        self.fail('Bounded navigation event did not complete')

    async def test_refresh_during_unmount_does_not_read_state_or_missing_widgets(self):
        with tempfile.TemporaryDirectory(prefix='cmw-detach-refresh-') as tmp:
            store = Store(Path(tmp)/'absent-queue')
            app = JobsApp(store, observer=SyntheticObserver(), sampler=SyntheticSampler())
            app.refresh_state()  # Before Mount, as after a fully detached screen.
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                app.screen.query_one('#table', DataTable).remove()
                await pilot.pause()
                with patch.object(store, 'snapshot', side_effect=AssertionError('Detached state read')):
                    app.refresh_state()
                await pilot.press('q')

    async def test_seeded_filter_modal_returns_preserve_selected_attempt_during_updates(self):
        for seed in (7, 23):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix='cmw-integrated-navigation-') as tmp:
                root = Path(tmp).resolve()
                store = Store(root/'queue')
                jobs = [store.add(argv=['/never/executed'], cwd=root, name=f'case {i:02}', hold=True)
                        for i in range(40)]
                rng = random.Random(seed)
                actions = rng.sample(['append', 'refresh', 'resize'], 3)
                captured = []
                def inspect(**kwargs):
                    captured.append(kwargs)
                    return {'target': kwargs}
                app = JobsApp(store, observer=SyntheticObserver(), sampler=SyntheticSampler())
                with patch('cmw.doctor.service.inspect_local', side_effect=inspect), \
                        patch('cmw.doctor.service.format_report', side_effect=lambda value: json.dumps(value)):
                    async with app.run_test(size=(80, 24)) as pilot:
                        await self.until(pilot, lambda: bool(app.row_ids))
                        await pilot.press('home', *(['down']*19))
                        await self.until(pilot, lambda: app.selected_id == 'J20.1')
                        identity = (jobs[19]['id'], jobs[19]['attempt_id'])
                        await pilot.press('/')
                        self.assertIsInstance(app.screen, CatalogueSearch)
                        app.screen.query_one('#catalogue-text', Input).value = 'case'
                        await pilot.click('#catalogue-apply')
                        await self.until(pilot, lambda: bool(app.catalogue_pages) and not app.catalogue_busy)
                        table = app.query_one('#table', DataTable)
                        table.focus()
                        await pilot.press('home', *(['down']*19))
                        await self.until(pilot, lambda: app.selected_id == 'J20.1')
                        for action in actions:
                            before = table_view(table)
                            await pilot.press('enter')
                            self.assertIsInstance(app.screen, Inspect)
                            await pilot.press('d')
                            await self.until(pilot, lambda: isinstance(app.screen, DoctorReport) and not app.screen.pending)
                            if action == 'append':
                                store.add(argv=['/never/executed'], cwd=root, name='case appended', hold=True)
                            elif action == 'resize':
                                await pilot.resize_terminal(100, 30)
                                await pilot.resize_terminal(80, 24)
                            app.refresh_state()
                            await pilot.press('escape', 'escape')
                            await self.until(pilot, lambda: not app.navigation_pending())
                            self.assertEqual((app.selected()['id'], app.selected()['attempt_id']), identity)
                            self.assertEqual(table_view(table)['key'], before['key'])
                            self.assertEqual(table_view(table)['visible'], before['visible'])
                        self.assertTrue(all(item['job_id'] == identity[0] and item['expected_attempt_id'] == identity[1]
                                            for item in captured))
                        self.assertEqual(len(captured), 3)
                        self.assertTrue(all(job['status'] == 'Hold' for job in store.snapshot()['jobs']))
                destination = os.environ.get('CMW_CAMPAIGN_TRACE_DIR')
                if destination:
                    path = Path(destination); path.mkdir(parents=True, exist_ok=True)
                    (path/f'navigation-{seed}.json').write_text(json.dumps({'seed':seed,'actions':actions,
                        'immutable_target':list(identity),'inspections':len(captured)}, indent=2)+'\n')
