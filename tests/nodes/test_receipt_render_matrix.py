"""Render real node views with deterministic owner receipts at supported widths."""
import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from cmw.nodes.protocol import NodeError

try:
    from rich.cells import cell_len
    from cmw.doctor.presentation import DoctorDetails, DoctorReport
    from cmw.jobs.catalogue_models import CatalogueQuery
    from cmw.jobs.catalogue_ui import CatalogueDetails, CatalogueSearch
    from cmw.jobs.copy_ui import FrozenText
    from cmw.jobs.history_ui import HistoryDetails, ProgressHistory
    from cmw.jobs.review_ui import ReviewScreen
    from cmw.jobs.sources_ui import Sources
    from cmw.jobs.study_ui import StudyScreen
    from cmw.nodes.portal import NodesApp, row_key, target_key
    from cmw.nodes.portal_views import FetchDialog, NodeControlConfirm, NodeCopyPreview, NodeFiles, NodeOrderPrompt
    from tests.jobs.test_doctor_ui import DoctorBackend
    from tests.jobs.test_machine_thermal_ui import machine
    from tests.jobs.test_review_ui import ReviewBackend
    from tests.jobs.test_study_ui import StudyBackend
    from tests.nodes.test_sp_card_render import static_states
except ImportError:
    NodesApp = None


WIDTHS = (77, 80, 120)
HEIGHT = 24


@unittest.skipIf(NodesApp is None, 'Optional Jobs console unavailable')
class NodeViewRenderMatrixTests(unittest.IsolatedAsyncioTestCase):
    def accept(self, request):
        op, params, target = request[:3]
        envelope = self.backend.call(op, params, target)
        if op == 'status':
            envelope['result']['state']['machine_usage'] = machine()
        self.app.accept(self.owner, request, envelope, None)

    async def settle(self, condition):
        # No background dispatch: an explicit UI request is serviced only here.
        async with asyncio.timeout(10):
            while True:
                while self.lane.pending:
                    self.accept(self.lane.pending.popleft())
                await self.pilot.pause(0)
                if condition():
                    return

    async def open_view(self, route):
        app, root = self.app, self.app.screen_stack[0]

        async def open_action(action, kind, populated=lambda view: True):
            action()
            await self.settle(lambda: isinstance(app.screen, kind) and app.screen.is_mounted
                              and bool(app.screen.query('.node-view-frame')) and populated(app.screen))

        if route == 'overview':
            return
        if route in ('filtered table', 'search'):
            app.catalogue_changed(('apply', CatalogueQuery(), None))
            await self.settle(lambda: self.owner in app.catalogue_pages)
            if route == 'search':
                await open_action(app.action_search, CatalogueSearch)
        elif route in ('pending review', 'complete review', 'frozen review'):
            await open_action(app.action_review, ReviewScreen, lambda view: view.record is not None)
            view = app.screen
            view.action_inspect()
            await self.settle(lambda: not view.busy and view.record.status == 'PENDING')
            if route != 'pending review':
                self.backend.review_service.complete = True
                view.send('review.poll', {'review_id': view.record.review_id})
                await self.settle(lambda: not view.busy and view.record.status == 'COMPLETE')
            if route == 'frozen review':
                await open_action(view.action_copy_review, FrozenText)
        elif route in ('details', 'doctor'):
            await open_action(app.action_details, DoctorDetails)
            if route == 'doctor':
                await open_action(app.screen.action_doctor, DoctorReport, lambda view: view.report is not None)
        elif route in ('history', 'history entry', 'frozen history'):
            await open_action(app.action_history, ProgressHistory, lambda view: bool(view.entries))
            if route != 'history':
                await open_action(app.screen.action_details, HistoryDetails)
            if route == 'frozen history':
                await open_action(app.screen.action_select_text, FrozenText)
        elif route in ('catalogue details', 'study'):
            await open_action(app.action_catalogue, CatalogueDetails, lambda view: view.record is not None)
            if route == 'study':
                await open_action(app.screen.action_study, StudyScreen, lambda view: view.record is not None)
        elif route in ('files', 'fetch'):
            await open_action(app.action_files, NodeFiles)
            if route == 'fetch':
                await open_action(lambda: app.screen.query_one('#node-files-fetch').press(), FetchDialog)
        elif route == 'sources':
            await open_action(app.action_sources, Sources, lambda view: view.inspection is not None)
        else:
            actions = {'scheduling': (app.action_scheduling, FrozenText), 'logs': (app.action_logs, FrozenText),
                       'snapshot': (app.action_copy_snapshot, NodeCopyPreview), 'order': (app.action_order, NodeOrderPrompt),
                       'confirm': (app.action_dispatch, NodeControlConfirm)}
            await open_action(*actions[route])
        if app.screen is not root:
            self.assertTrue(app.view_commands_allowed(app.screen), 'The fixture must first deliver real view data')

    def rendered_lines(self, widget, strips):
        # Probe the composited cells, including viewport clipping and CSS borders.
        r = widget.region
        self.assertGreater(r.width, 0)
        self.assertLessEqual(r.right, self.app.size.width)
        self.assertGreaterEqual(r.y, 0)
        self.assertLessEqual(r.bottom, HEIGHT)
        return [strip.crop(r.x, r.right).text for strip in strips[r.y:r.bottom]]

    def assert_frame(self, lines, width):
        self.assertGreaterEqual(len(lines), 3)
        self.assertEqual((lines[0][0], lines[0][-1]), ('┌', '┐'), lines[0])
        self.assertEqual((lines[-1][0], lines[-1][-1]), ('└', '┘'), lines[-1])
        size = cell_len(lines[0])
        self.assertLessEqual(size, width)
        self.assertTrue(all(cell_len(line) == size for line in lines), lines)
        for line in lines[1:-1]:
            self.assertIn((line[0], line[-1]), (('│', '│'), ('├', '┤')), line)

    def assert_state(self, text, state):
        if state == 'fresh':
            self.assertNotIn('Last reported', text)
            self.assertNotIn('receipt unknown', text.casefold())
        else:
            self.assertIn('Last reported', text)
            if state == 'missing':
                self.assertIn('receipt unknown', text.casefold())

    def visible_rows(self):
        table = self.app.screen_stack[0].query_one('#table')
        height = table.scrollable_content_region.height - table.header_height
        top = int(table.scroll_y)
        return tuple(row.key.value for row in table.ordered_rows[top:top + height])

    def assert_projection(self, route, state, width):
        screen = self.app.screen
        strips = screen._compositor.render_strips()
        self.assertTrue(all(strip.cell_length <= width for strip in strips))
        root = screen is self.app.screen_stack[0]
        frame = screen.query_one('#portal-frame' if root else '.node-view-frame')
        lines = self.rendered_lines(frame, strips)
        self.assert_frame(lines, width)
        if route == 'filtered table' and state == 'missing':
            self.assertTrue(self.lane.fresh(self.app.clock()))
            self.assert_state('\n'.join(self.rendered_lines(screen.query_one('#message'), strips)), state)
        else:
            self.assert_state(lines[0], state)
        if route == 'overview':
            for pane in ('#node-summary', '#selected', '#execution'):
                self.assert_state('\n'.join(self.rendered_lines(screen.query_one(pane), strips)), state)
            progress = screen.query_one('#progress')
            card = str(progress.render()).splitlines()
            visible = self.rendered_lines(progress, strips)
            self.assertGreaterEqual(len(visible), len(card))
            for line, displayed in zip(card, visible):
                self.assertIn(line, displayed, 'The rendered card must fit the visible scientific pane')
            self.assert_frame(card, width)
            self.assert_state(card[0], state)
        elif route == 'filtered table':
            if state != 'fresh':
                self.assertIn('Last', '\n'.join(self.rendered_lines(screen.query_one('#table'), strips)))

    def remove_receipts(self, stack, route):
        # Preserve all data and status, then remove only its receipt: a healthy
        # connection cannot lend a receipt to a record with missing evidence.
        app = self.app
        if route == 'filtered table':
            stack.enter_context(patch.dict(app.catalogue_receipts, {self.owner: None}))
        elif app.screen is not app.screen_stack[0]:
            stack.enter_context(patch.dict(app.screen.node_view_receipts, {self.owner: None}))
        else:
            stack.enter_context(patch.dict(self.lane.receipts, {'status': None}))
            cache = self.lane.cache[target_key(self.backend.jobs[0]['target'])]
            stack.enter_context(patch.dict(cache, {'progress.receipt': None, 'observe.receipt': None}))

    async def matrix(self, route):
        if 'review' in route:
            self.backend = ReviewBackend()
        elif route in ('doctor', 'details'):
            self.backend = DoctorBackend('MINI', remote=True)
        else:
            self.backend = StudyBackend()
        self.owner = self.backend.alias
        now = [112.]
        app = self.app = NodesApp({self.owner: self.backend}, clock=lambda: now[0], wall_clock=lambda: 1700000000.)
        self.lane = app.lanes[self.owner]
        self.backend.snapshot = static_states(self.backend.context)['scf']
        target = self.backend.jobs[0]['target']
        caller = {'runtime': {'identity': {'fingerprint': 'caller'}}, 'capabilities': [], 'findings': [], 'observation': {}}
        with patch.object(app, 'tick'), patch.object(ReviewScreen, 'poll'), \
             patch('cmw.doctor.service.inspect_local', return_value=caller):
            async with app.run_test(size=(WIDTHS[0], HEIGHT)) as self.pilot:
                for op in ('capabilities', 'status'):
                    self.accept((op, {}, None, None))
                app.select(row_key(target))
                for op in ('progress', 'observe'):
                    self.accept((op, {}, target, None))
                await self.open_view(route)
                rows = {}
                for state in ('fresh', 'missing', 'stale', 'disconnected'):
                    with ExitStack() as stack:
                        if state == 'missing':
                            self.remove_receipts(stack, route)
                        elif state == 'stale':
                            stack.enter_context(patch.object(app, 'clock', return_value=now[0] + 16))
                        elif state == 'disconnected':
                            app.refuse_lane(self.owner, NodeError('DISCONNECTED', 'synthetic read failed'), 'observe')
                        for width in WIDTHS:
                            with self.subTest(view=route, state=state, width=width):
                                await self.pilot.resize_terminal(width, HEIGHT)
                                app.render_state()
                                await self.pilot.pause(0)
                                self.assert_projection(route, state, width)
                                visible = self.visible_rows()
                                if state == 'fresh':
                                    self.assertGreaterEqual(len(visible), 3)
                                    rows[width] = visible
                                else:
                                    self.assertEqual(visible, rows[width])

    async def test_details_and_copy_distinguish_absent_progress_from_missing_receipt(self):
        self.backend = StudyBackend()
        self.owner = self.backend.alias
        app = self.app = NodesApp({self.owner: self.backend}, clock=lambda: 112.)
        self.lane = app.lanes[self.owner]
        target = self.backend.jobs[0]['target']
        with patch.object(app, 'tick'):
            async with app.run_test(size=(80, HEIGHT)) as self.pilot:
                for op in ('capabilities', 'status'):
                    self.accept((op, {}, None, None))
                app.select(row_key(target))
                for progress in ('absent', 'unqualified'):
                    if progress == 'unqualified':
                        self.accept(('progress', {}, target, None))
                        del self.lane.cache[target_key(target)]['progress.receipt']
                        self.assertIsNotNone(app.selected_snapshot(self.owner, self.backend.jobs[0]))
                    for action in (app.action_details, app.action_copy_snapshot):
                        with self.subTest(progress=progress, action=action.__name__):
                            action()
                            await self.settle(lambda: app.screen is not app.screen_stack[0]
                                              and bool(app.screen.query('.node-view-frame')))
                            frame = app.screen.query_one('.node-view-frame')
                            self.assertTrue(self.lane.fresh(app.clock()))
                            self.assertEqual(app.view_commands_allowed(app.screen), progress == 'absent')
                            self.assert_state(str(frame.border_title or ''),
                                              'fresh' if progress == 'absent' else 'missing')
                            app.pop_screen()
                            await self.pilot.pause(0)

    async def test_root_and_filtered_table(self):
        for route in ('overview', 'filtered table'):
            with self.subTest(view=route):
                await self.matrix(route)

    async def test_reviews_and_reports(self):
        for route in ('pending review', 'complete review', 'frozen review', 'details', 'doctor', 'scheduling', 'snapshot'):
            with self.subTest(view=route):
                await self.matrix(route)

    async def test_files_and_owner_controls(self):
        for route in ('files', 'fetch', 'logs', 'confirm', 'order'):
            with self.subTest(view=route):
                await self.matrix(route)

    async def test_scientific_and_catalogue_dialogs(self):
        for route in ('history', 'history entry', 'frozen history', 'sources', 'search', 'catalogue details', 'study'):
            with self.subTest(view=route):
                await self.matrix(route)
