"""Actual mouse and keyboard routing of Files cells across attempt identities."""

from tests.ui_readiness import await_laid_out
import threading
import unittest

from tests.nodes import test_portal
from tests.nodes.test_portal import FakeBackend, NodesApp

if NodesApp is not None:
    from cmw.jobs.copy_ui import FrozenText
    from cmw.nodes.portal import row_key
    from cmw.nodes.portal_views import NodeFiles
    from textual.coordinate import Coordinate
    from textual.widgets import DataTable, Static


class AttemptFilesBackend(FakeBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for job in self.jobs:
            job['resources'].update(mpi_ranks=8, threads_per_rank=1)
            job['reason'] = ''

    def call(self, operation, params=None, target=None, **kwargs):
        response = super().call(operation, params, target, **kwargs)
        if operation == 'locations':
            for location in response['result']['locations']:
                location['node_path'] = location['declared_path'] = '/remote/' + target['attempt_id']
        return response


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class PortalFilesTests(unittest.IsolatedAsyncioTestCase):
    until = test_portal.PortalTests.until

    async def ready(self, app, pilot, backend):
        await self.until(pilot, lambda: len(app.rows) == 5 and not app.navigation_pending())
        app.select(row_key(backend.jobs[0]['target']))
        app.render_state()
        await pilot.pause()

    async def files_ready(self, app, pilot):
        await self.until(pilot, lambda: isinstance(app.screen, NodeFiles) and app.screen.is_mounted
                         and app.screen.query_one(DataTable).row_count == len(app.screen.locations))

        await await_laid_out(app.screen.query_one('#node-files-fetch'), hit_test=True)

    def assert_files(self, app, job):
        self.assertIsInstance(app.screen, NodeFiles)
        self.assertEqual(len(app.screen_stack), 2)
        self.assertFalse(any(isinstance(screen, FrozenText) for screen in app.screen_stack))
        self.assertEqual(app.screen.target, job['target'])
        self.assertEqual(app.screen.owner, 'mini')
        self.assertEqual(app.screen.label, 'mini / research / ' + job['display_id'])
        title = app.screen.query(Static).first()
        self.assertIn('DECLARED FILE LOCATIONS · ' + app.screen.label, str(title.render()))
        location = next(iter(app.screen.locations.values()))
        self.assertEqual(location['node_path'], '/remote/' + job['attempt_id'])
        self.assertFalse(location['local_open_permitted'])
        self.assertFalse(app.screen.local)

    async def test_click_other_rows_files_then_keyboard_files_and_details(self):
        backend = AttemptFilesBackend('mini', remote=True)
        app = NodesApp({'mini': backend})
        async with app.run_test(size=(170, 45)) as pilot:
            await self.ready(app, pilot, backend)
            a, b = backend.jobs[:2]
            self.assertEqual(app.selected_key, row_key(a['target']))
            table = app.query_one('#table', DataTable)
            column = next(i for i, item in enumerate(table.ordered_columns) if item.key.value == 'files')
            region = table._get_cell_region(Coordinate(app.row_keys.index(row_key(b['target'])), column))
            self.assertTrue(await pilot.click('#table', offset=(region.x + 1, region.y)))
            await self.files_ready(app, pilot)
            self.assert_files(app, b)
            self.assertEqual([target for operation, _, target in backend.calls if operation == 'locations'], [b['target']])
            await pilot.press('escape')
            await pilot.pause()
            self.assertEqual(app.selected_key, row_key(b['target']))
            await pilot.press('f')
            await self.files_ready(app, pilot)
            self.assert_files(app, b)
            await pilot.press('escape')
            await pilot.press('enter')
            await self.until(pilot, lambda: isinstance(app.screen, FrozenText) and app.screen.is_mounted)
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press('escape')
            await pilot.pause()
            self.assertEqual(app.selected_key, row_key(b['target']))

    async def test_click_disconnected_rows_files_does_not_queue_a_request(self):
        from cmw.nodes.protocol import NodeError
        backend = AttemptFilesBackend('mini', remote=True)
        app = NodesApp({'mini': backend}, clock=lambda: 10.)
        async with app.run_test(size=(170, 45)) as pilot:
            await self.ready(app, pilot, backend)
            app.refuse_lane('mini', NodeError('DISCONNECTED', 'stream closed'), 'status')
            await pilot.pause()  # Lay out the changed status line before resolving the cell's coordinates.
            table = app.query_one('#table', DataTable)
            column = next(i for i, item in enumerate(table.ordered_columns) if item.key.value == 'files')
            region = table._get_cell_region(Coordinate(0, column))
            await pilot.click('#table', offset=(region.x + 1, region.y))
            self.assertFalse(any(request[0] == 'locations' for request in app.lanes['mini'].pending))
            self.assertIn('Nothing was queued', str(app.query_one('#message', Static).render()))

    async def test_delayed_files_response_cannot_follow_changed_selection(self):
        backend = AttemptFilesBackend('mini', remote=True)
        gate = backend.blocks['locations'] = threading.Event()
        app = NodesApp({'mini': backend})
        async with app.run_test(size=(170, 45)) as pilot:
            await self.ready(app, pilot, backend)
            await pilot.press('f')
            await self.until(pilot, lambda: any(call[0] == 'locations' for call in backend.calls))
            await pilot.press('down')
            self.assertEqual(app.selected_key, row_key(backend.jobs[1]['target']))
            gate.set()
            await self.until(pilot, lambda: not app.lanes['mini'].busy)
            self.assertEqual(len(app.screen_stack), 1)
            await pilot.press('f')
            await self.files_ready(app, pilot)
            self.assert_files(app, backend.jobs[1])
