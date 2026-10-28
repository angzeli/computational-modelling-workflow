"""Full portal interactions preserve node/attempt identity and deliberate viewports."""
from copy import deepcopy
import threading
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from cmw.progress.models import ProgressSnapshot
from tests.nodes.test_portal import FakeBackend, NodesApp

if NodesApp is not None:
    from cmw.jobs.copy_ui import FrozenText
    from cmw.nodes.portal import row_key
    from cmw.nodes.portal_views import FetchDialog, NodeFiles
    from textual.widgets import DataTable, Static


def many_jobs(owner, *, remote=False):
    backend = FakeBackend(owner, remote=remote)
    template = deepcopy(backend.jobs[0])
    template['resources'].update(mpi_ranks=8, threads_per_rank=1)
    template['reason'] = ''
    backend.jobs = []
    for number in range(1, 37):
        attempt = uuid5(NAMESPACE_URL, owner + '/navigation/' + str(number)).hex
        job = deepcopy(template)
        job.update(id=number, display_id=f'J{number}.1', attempt_id=attempt,
                   name=f'{owner} distinguishable job {number:02d}', order=number,
                   target={'node_id': backend.node_id, 'queue_id': backend.queue_id,
                           'job_id': number, 'attempt_id': attempt})
        backend.jobs.append(job)
    return backend


def table_view(app):
    """Read visible row identities independently of the restoration implementation."""
    table = app.screen_stack[0].query_one('#table', DataTable)
    rows = table.ordered_rows
    top = int(table.scroll_y)
    height = table.scrollable_content_region.height - table.header_height
    return {'selection': app.selected_key, 'cursor': rows[table.cursor_row].key.value,
            'column': table.cursor_column, 'visible': tuple(row.key.value for row in rows[top:top + height]),
            'x': table.scroll_x, 'y': table.scroll_y,
            'targets': (table.scroll_target_x, table.scroll_target_y),
            'relative': table.cursor_row - table.scroll_y,
            'screen': (app.screen_stack[0].scroll_x, app.screen_stack[0].scroll_y)}


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class PortalNavigationTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        backends = {'MBP': many_jobs('MBP'), 'MINI': many_jobs('MINI', remote=True)}
        return NodesApp(backends), backends

    async def until(self, pilot, predicate, description, diagnostics=None):
        for _ in range(160):
            if predicate():
                return
            await pilot.pause(.025)
        detail = '; state=' + repr(diagnostics()) if diagnostics is not None else ''
        self.fail('Frontend condition did not become true: ' + description + detail)

    async def position(self, app, backends, pilot, number=25, *, where='middle'):
        await self.until(pilot, lambda: len(app.rows) == 72 and not app.navigation_pending(),
                         'both nodes populated and startup restoration completed')
        predecessor = backends['MINI'].jobs[number - 2]
        predecessor_key = row_key(predecessor['target'])
        target = backends['MINI'].jobs[number - 1]['target']
        app.select(predecessor_key)
        app.render_state()
        def setup_state():
            return {**table_view(app), 'pending_restores': len(app._navigation_restores),
                    'pending_layout': app.screen_stack[0]._layout_required,
                    'predecessor_progress': app.selected_snapshot('MINI', predecessor) is not None}
        # This is fixture positioning, before the real keyboard interaction.
        # A programmatic selection can precede a queued structural restoration.
        await self.until(pilot, lambda: app.selected_key == predecessor_key
                         and table_view(app)['cursor'] == predecessor_key
                         and app.selected_snapshot('MINI', predecessor) is not None
                         and not app.navigation_pending()
                         and not app.screen_stack[0]._layout_required,
                         'synthetic predecessor selection and card layout settled', setup_state)
        await pilot.press('down')
        await self.until(pilot, lambda: app.selected_key == row_key(target)
                         and app.selected_snapshot('MINI', backends['MINI'].jobs[number - 1]) is not None,
                         'keyboard selection and scientific observation', setup_state)
        await pilot.pause()
        await self.until(pilot, lambda: not app.navigation_pending(), 'initial structural layout settled')
        table = app.query_one('#table', DataTable)
        index = app.row_keys.index(row_key(target))
        height = table.scrollable_content_region.height - table.header_height
        offset = {'top': 0, 'middle': min(4, height - 1), 'bottom': height - 1,
                  'offscreen': height + 15}[where]
        table.scroll_to(x=min(3, table.max_scroll_x), y=index - offset,
                        animate=False, immediate=True)
        await pilot.pause()
        return deepcopy(target)

    async def open_details(self, app, pilot, *, mouse=False):
        table = app.query_one('#table', DataTable)
        if mouse:
            relative = table.header_height + table.cursor_row - int(table.scroll_y)
            self.assertTrue(await pilot.click('#table', offset=(3, relative)))
        else:
            await pilot.press('enter')
        await self.until(pilot, lambda: isinstance(app.screen, FrozenText), 'Details opened')

    async def status_update(self, app, backends, pilot, mutate=lambda backend: None):
        """Release an actual timer-started backend response at an explicit barrier."""
        backend, lane = backends['MINI'], app.lanes['MINI']
        gate = threading.Event()
        backend.blocks['status'] = gate
        received, calls = lane.received, len(backend.calls)
        lane.next_poll = 0
        try:
            await self.until(pilot, lambda: any(call[0] == 'status' for call in backend.calls[calls:]),
                             'gated status request started')
            mutate(backend)
            gate.set()
            await self.until(pilot, lambda: lane.received != received, 'status response accepted')
        finally:
            gate.set()
            backend.blocks.pop('status', None)
        await pilot.pause()

    async def returned(self, app, pilot):
        await self.until(pilot, lambda: len(app.screen_stack) == 1, 'returned to portal')
        await self.until(pilot, lambda: not app.navigation_pending(), 'restoration completed')
        await pilot.pause()

    def assert_same_view(self, before, app):
        self.assertEqual(table_view(app), before)

    async def test_mouse_keyboard_j25_j24_repeat_at_required_geometries(self):
        for size in ((77, 24), (80, 24), (100, 30), (140, 40)):
            with self.subTest(size=size):
                app, backends = self.app()
                async with app.run_test(size=size) as pilot:
                    for number, mouse in ((25, False), (24, True)):
                        target = await self.position(app, backends, pilot, number, where='bottom')
                        before = table_view(app)
                        for _ in range(2):
                            await self.open_details(app, pilot, mouse=mouse)
                            self.assertIn('MINI / research / ' + f'J{number}.1', app.screen.text)
                            await self.status_update(app, backends, pilot)
                            await pilot.press('escape')
                            await self.returned(app, pilot)
                            self.assert_same_view(before, app)
                            await self.status_update(app, backends, pilot)
                            self.assert_same_view(before, app)
                            self.assertEqual(app.selected()[1]['target'], target)

    async def test_append_reorder_preserve_anchor_middle_and_offscreen(self):
        for where in ('top', 'middle', 'offscreen'):
            with self.subTest(position=where):
                app, backends = self.app()
                async with app.run_test(size=(100, 30)) as pilot:
                    target = await self.position(app, backends, pilot, where=where)
                    before = table_view(app)
                    await self.open_details(app, pilot)
                    def append(backend):
                        new = deepcopy(backend.jobs[-1])
                        new.update(id=37, display_id='J37.1', attempt_id='new-37')
                        new['target'].update(job_id=37, attempt_id='new-37')
                        backend.jobs.append(new)
                    await self.status_update(app, backends, pilot, append)
                    await pilot.press('escape')
                    await self.returned(app, pilot)
                    self.assert_same_view(before, app)
                    await self.open_details(app, pilot)
                    await self.status_update(app, backends, pilot,
                                             lambda backend: setattr(backend, 'jobs', backend.jobs[4:] + backend.jobs[:4]))
                    await pilot.press('escape')
                    await self.returned(app, pilot)
                    after = table_view(app)
                    self.assertEqual(after['selection'], before['selection'])
                    self.assertEqual(after['cursor'], before['cursor'])
                    self.assertEqual(after['visible'][0], before['visible'][0])
                    self.assertEqual(after['x'], before['x'])
                    if where != 'offscreen':
                        self.assertEqual(after['visible'], before['visible'])
                        self.assertEqual(after['relative'], before['relative'])
                    self.assertEqual(app.selected()[1]['target'], target)
                    await self.status_update(app, backends, pilot)
                    self.assertEqual(table_view(app), after)

    async def test_height_and_resize_preserve_offscreen_anchor(self):
        app, backends = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.position(app, backends, pilot, where='offscreen')
            before = table_view(app)
            await self.open_details(app, pilot)
            backend = backends['MINI']
            backend.snapshot = ProgressSnapshot(backend.context, 'height-fixture', status='UNAVAILABLE',
                                                phase='No parsed progress', read_health='UNAVAILABLE')
            app.lanes['MINI'].next_progress = 0
            await self.until(pilot, lambda: app.selected_snapshot('MINI', backend.jobs[24]).adapter == 'height-fixture',
                             'scientific card height update')
            await pilot.resize_terminal(80, 24)
            await pilot.press('escape')
            await self.returned(app, pilot)
            after = table_view(app)
            for key in ('selection', 'cursor', 'x', 'y', 'screen'):
                self.assertEqual(after[key], before[key])
            self.assertEqual(after['visible'][0], before['visible'][0])
            await self.status_update(app, backends, pilot)
            self.assertEqual(table_view(app), after)

    async def test_resize_sequence_settles_geometry_with_delayed_modal_updates(self):
        from tests.campaign.geometry import GeometryProbe, RESIZE_SEQUENCE, geometry_problems
        from tests.progress.test_scientific_card import example

        for where in ('top', 'offscreen'):
            with self.subTest(position=where):
                app, backends = self.app()
                backend = backends['MINI']
                backend.snapshot = example()
                probe = GeometryProbe()
                async with app.run_test(size=RESIZE_SEQUENCE[0]) as pilot:
                    await self.until(pilot, lambda: len(app.rows) == 72 and not app.navigation_pending(),
                                     'initial node layout')
                    target = deepcopy(backend.jobs[24]['target'])
                    app.select(row_key(target))
                    await self.until(pilot, lambda: app.selected_key == row_key(target)
                                     and app.selected_snapshot(*app.selected()) is not None
                                     and not app.navigation_pending(), 'selected fixture observation')
                    await pilot.pause()
                    table = app.query_one('#table', DataTable)
                    offset = 0 if where == 'top' else table.scrollable_content_region.height + 15
                    table.scroll_to(y=table.cursor_row - offset, animate=False, immediate=True)
                    await pilot.pause()
                    states = []
                    for index, size in enumerate(RESIZE_SEQUENCE):
                        before = table_view(app)
                        await self.open_details(app, pilot)
                        backend.snapshot = ProgressSnapshot(backend.context, 'height-fixture',
                            status='UNAVAILABLE', phase='No parsed progress', read_health='UNAVAILABLE')
                        app.lanes['MINI'].next_progress = 0
                        await self.until(pilot, lambda: app.selected_snapshot(*app.selected()).adapter == 'height-fixture',
                                         'delayed smaller scientific card')
                        def mutate(node):
                            if index == 1:
                                new = deepcopy(node.jobs[-1])
                                new.update(id=37, display_id='J37.1', attempt_id='resize-new-37')
                                new['target'].update(job_id=37, attempt_id='resize-new-37')
                                node.jobs.append(new)
                            if index == 2:
                                node.jobs = node.jobs[4:] + node.jobs[:4]
                        await self.status_update(app, backends, pilot, mutate)
                        await pilot.resize_terminal(*size)
                        backend.snapshot = example()
                        app.lanes['MINI'].next_progress = 0
                        await self.until(pilot, lambda: app.selected_snapshot(*app.selected()).adapter == 'test',
                                         'delayed full scientific card')
                        await pilot.press('escape')
                        state = await probe.settled(app, size, require_visible=where == 'top')
                        states.append(state)
                        self.assertEqual(state['selected'], row_key(target))
                        if where == 'offscreen' or before['relative'] < len(state['visible']):
                            self.assertEqual(table_view(app)['visible'][0], before['visible'][0])
                        if where == 'offscreen':
                            self.assertFalse(state['selected_visible'])
                        painted = '\n'.join(state['painted_card'])
                        for literal in ('rms', '1.89', '|d eps|', '2.9332', 'EDIFF', '1E-5', 'matched', 'data'):
                            self.assertIn(literal, painted)
                        # The capture oracle rejects the original 100→77 stale-card shape.
                        stale = {**state, 'frame_widths': [93] * 8, 'card_widths': [93] * 8}
                        self.assertIn('card frame retains a different width', geometry_problems(stale, (77, 24)))
                        pending = {**state, 'pending_restores': 1}
                        self.assertIn('layout or navigation restoration is pending', geometry_problems(pending, size))
                    self.assertGreater(states[-1]['card_layout_generation'], states[0]['card_layout_generation'])
                    await self.status_update(app, backends, pilot)
                    later = await probe.settled(app, RESIZE_SEQUENCE[-1], require_visible=where == 'top')
                    self.assertEqual(later['selected'], states[-1]['selected'])
                    self.assertEqual(later['scroll'], states[-1]['scroll'])
                    self.assertFalse(any(call[0].startswith('control.') for call in backend.calls))

    async def test_removed_or_replaced_attempt_never_transfers_selection(self):
        for number, replaced in ((15, False), (15, True), (25, False), (25, True)):
            with self.subTest(number=number, replaced=replaced):
                app, backends = self.app()
                async with app.run_test(size=(100, 30)) as pilot:
                    target = await self.position(app, backends, pilot, number=number)
                    before = table_view(app)
                    await self.open_details(app, pilot)
                    def remove_or_replace(backend):
                        if replaced:
                            backend.jobs[number - 1]['attempt_id'] = 'replacement-attempt'
                            backend.jobs[number - 1]['target']['attempt_id'] = 'replacement-attempt'
                        else:
                            del backend.jobs[number - 1]
                    await self.status_update(app, backends, pilot, remove_or_replace)
                    await pilot.press('escape')
                    await self.returned(app, pilot)
                    self.assertEqual(app.selected_key, row_key(target))
                    self.assertIsNone(app.selected())
                    self.assertTrue(app.query_one('#table', DataTable).has_class('navigation-unavailable'))
                    self.assertIn('no longer', str(app.query_one('#selected', Static).render()))
                    table = app.query_one('#table', DataTable)
                    self.assertEqual(table.scroll_y, min(before['y'], table.max_scroll_y))
                    if before['y'] <= table.max_scroll_y:
                        self.assertEqual(table_view(app)['visible'][0], before['visible'][0])
                    for backend in backends.values():
                        self.assertFalse(any(call[0].startswith('control.') for call in backend.calls))
                    # The fallback has no authority until a new, deliberate row action.
                    mouse = (number == 15) != replaced
                    if mouse:
                        index = int(table.scroll_y) + 1
                        expected_key = table.ordered_rows[index].key.value
                        expected_target = deepcopy(app.rows[expected_key][1]['target'])
                        self.assertTrue(await pilot.click('#table', offset=(3, table.header_height + 1)))
                    else:
                        index = min(table.cursor_row + 1, table.row_count - 1)
                        expected_key = table.ordered_rows[index].key.value
                        expected_target = deepcopy(app.rows[expected_key][1]['target'])
                        await pilot.press('down')
                        self.assertEqual(app.selected_key, expected_key)
                        await pilot.press('enter')
                    await self.until(pilot, lambda: isinstance(app.screen, FrozenText) and app.screen.is_mounted,
                                     'explicit fallback-row inspection')
                    self.assertEqual(app.selected()[1]['target'], expected_target)
                    self.assertNotEqual(app.selected_key, row_key(target))
                    self.assertFalse(table.has_class('navigation-unavailable'))
                    self.assertIn(app.selected()[1]['display_id'], app.screen.text)
                    await pilot.press('escape')
                    await self.returned(app, pilot)
                    self.assertEqual(app.selected()[1]['target'], expected_target)
                    for backend in backends.values():
                        self.assertFalse(any(call[0].startswith('control.') for call in backend.calls))

    async def test_unreachable_return_and_reconnect_keep_new_deliberate_node(self):
        app, backends = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.position(app, backends, pilot)
            before = table_view(app)
            await self.open_details(app, pilot)
            backends['MINI'].fail = True
            app.lanes['MINI'].next_poll = 0
            await self.until(pilot, lambda: app.lanes['MINI'].error is not None, 'node unavailable')
            await pilot.press('escape')
            await self.returned(app, pilot)
            self.assert_same_view(before, app)
            self.assertIn('node unreachable', str(app.query_one('#selected', Static).render()))
            await pilot.press('ctrl+home')
            await self.until(pilot, lambda: app.selected()[0] == 'MBP', 'new deliberate local-node selection')
            selected = deepcopy(app.selected()[1]['target'])
            backends['MINI'].fail = False
            app.lanes['MINI'].next_poll = 0
            await self.until(pilot, lambda: app.lanes['MINI'].error is None, 'remote node reconnected')
            self.assertEqual(app.selected()[1]['target'], selected)
            self.assertEqual(app.selected()[0], 'MBP')

    async def test_nested_files_dialog_restores_only_actual_parent(self):
        app, backends = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            target = await self.position(app, backends, pilot)
            before = table_view(app)
            await pilot.press('f')
            await self.until(pilot, lambda: isinstance(app.screen, NodeFiles) and app.screen.is_mounted, 'remote Files')
            parent = app.screen
            with patch.object(app, 'restore_view', wraps=app.restore_view) as restore:
                await pilot.click('#node-files-fetch')
                await self.until(pilot, lambda: isinstance(app.screen, FetchDialog) and app.screen.is_mounted, 'nested fetch form')
                self.assertEqual(app.screen.target, target)
                await pilot.click('#fetch-back')
                await self.until(pilot, lambda: app.screen is parent and not app.navigation_pending(), 'intermediate Files return')
                self.assertEqual([call.args[0].screen for call in restore.call_args_list], [parent])
                await pilot.click('#node-files-back')
                await self.returned(app, pilot)
            self.assert_same_view(before, app)
            for backend in backends.values():
                self.assertFalse(any(call[0].startswith('fetch') for call in backend.calls))

    async def test_genuine_input_before_deferred_restore_wins(self):
        app, backends = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.position(app, backends, pilot)
            await self.open_details(app, pilot)
            queued = []
            original = app._restore_cursor
            # Gate only the task-owned return callback; application timers remain active.
            with patch.object(app, '_restore_cursor', side_effect=queued.append):
                await pilot.press('escape')
                await self.until(pilot, lambda: bool(queued), 'return callback queued')
            await pilot.press('down')
            new_view = table_view(app)
            self.assertEqual(app.selected()[1]['display_id'], 'J26.1')
            for pending in queued:
                original(pending)
            await self.returned(app, pilot)
            self.assert_same_view(new_view, app)
            await self.status_update(app, backends, pilot)
            self.assert_same_view(new_view, app)
