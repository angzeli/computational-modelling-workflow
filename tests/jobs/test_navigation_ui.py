"""Real navigation events over synthetic held jobs; no process scans or engines."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.freshness import clock_stamp
from cmw.jobs.store import Store
from cmw.jobs.telemetry import Sampler
from cmw.progress.models import SourceDescriptor
from cmw.progress.registry import register_adapter
from cmw.progress.service import ObservationService
from tests.jobs.test_history_cli import FictionalHistoryAdapter

try:
    from textual.containers import VerticalScroll
    from textual.widgets import DataTable, Static, TextArea
    from cmw.jobs.copy_ui import CopyPreview, FrozenText
    from cmw.jobs.history_ui import HistoryDetails, ProgressHistory
    from cmw.jobs.sources_ui import Sources
    from cmw.jobs.tui import (FilesChooser, Inspect, JobsApp, NoteEditor, Prompt,
                              SharingInspect, ExternalInspect)
except ImportError:
    JobsApp = None


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def arm(self):
        self.entered.clear()
        self.release.clear()

    def wait(self):
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError('Synthetic navigation update did not release')


class SyntheticObserver:
    def __init__(self):
        self.gate = Gate()
        self.observations = []

    def scan(self, store, state=None, *, source='client'):
        self.gate.wait()
        return {'state': 'NO_MATCH', 'reason': 'Synthetic clear observation',
                'observations': deepcopy(self.observations), 'observed_clock': clock_stamp(), 'observed_at': time.time(),
                'checked_at': time.time(), 'age_seconds': 0, 'stale': False,
                'source': source, 'scope': 'Synthetic fixture', '_pids': [],
                'coverage': {'complete': True, 'warnings': []}}


class SyntheticSampler:
    def sample(self, *args):
        machine = Sampler._empty_machine()
        machine.update(cpu_percent=22, ram_used_bytes=4 * 2**30,
                       ram_total_bytes=36 * 2**30, cpu_quality='complete',
                       memory_quality='complete', observed_at=time.time(), observed_clock=clock_stamp(), age_seconds=0)
        return {'machine_usage': machine, 'jobs': {}, 'external': {}}

    def close(self):
        pass


class GatedProgress(ObservationService):
    minimal = False

    def __init__(self):
        super().__init__()
        self.gate = Gate()

    def poll(self, *args, **kwargs):
        self.gate.wait()
        snapshot = super().poll(*args, **kwargs)
        return replace(snapshot, status="ADAPTER_UNAVAILABLE") if self.minimal else snapshot


class OrderedStore(Store):
    """A read projection can reorder rows without scheduling or starting anything."""
    reverse_projection = False

    def snapshot(self, **kwargs):
        result = super().snapshot(**kwargs)
        if self.reverse_projection:
            result['jobs'].reverse()
        return result


if JobsApp is not None:
    class ObservedJobsApp(JobsApp):
        activity_accepts = 0
        progress_accepts = 0
        refreshes = 0

        def refresh_state(self):
            super().refresh_state()
            self.refreshes += 1

        def accept_activity(self, *args, **kwargs):
            super().accept_activity(*args, **kwargs)
            self.activity_accepts += 1

        def accept_progress(self, *args, **kwargs):
            super().accept_progress(*args, **kwargs)
            self.progress_accepts += 1


def table_view(table):
    """Record rendered row identities and offsets, including deliberate cursor hiding."""
    y = table.header_height if table.show_header else 0
    visible, cursor_y = [], None
    keys = [row.key.value for row in table.ordered_rows]
    for index, row in enumerate(table.ordered_rows):
        height = table.get_row_height(row.key)
        relative = y - table.scroll_y
        if relative + height > table.header_height and relative < table.content_size.height:
            visible.append(row.key.value)
        if index == table.cursor_row:
            cursor_y = relative
        y += height
    return {'key': keys[table.cursor_row] if 0 <= table.cursor_row < len(keys) else None,
            'column': table.cursor_column, 'scroll': (table.scroll_x, table.scroll_y),
            'targets': (table.scroll_target_x, table.scroll_target_y),
            'visible': tuple(visible), 'cursor_y': cursor_y,
            'screen_scroll': tuple(table.screen.scroll_offset)}


@unittest.skipIf(JobsApp is None, 'Navigation requires the optional Jobs console')
class NavigationUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cmw-nav-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = OrderedStore(self.root / 'state')
        self.jobs = [self.store.add(argv=['/unexecuted/fixture'], cwd=self.root,
                                   name=f'Distinct held fixture {i + 1:02}',
                                   engine='FICTIONAL-HISTORY', hold=True)
                     for i in range(30)]
        self.source = self.root / 'flux.txt'
        self.source.write_text('4,2,1,3.75\n4,2,2,3.25\n5,3,1,2.0\n')
        from cmw.progress.registry import _REGISTRY
        registration = patch.dict(_REGISTRY)
        registration.start()
        self.addCleanup(registration.stop)
        register_adapter('FICTIONAL-HISTORY', FictionalHistoryAdapter)
        self.observer = SyntheticObserver()
        self.service = GatedProgress()
        self.addCleanup(self.observer.gate.release.set)
        self.addCleanup(self.service.gate.release.set)
        self.opener = Mock(side_effect=AssertionError('Navigation must not open a folder'))

    def app(self):
        self.observer = SyntheticObserver()
        self.service = GatedProgress()
        self.addCleanup(self.observer.gate.release.set)
        self.addCleanup(self.service.gate.release.set)
        return ObservedJobsApp(self.store, observer=self.observer,
                               sampler=SyntheticSampler(), progress_service=self.service,
                               progress_sources=(SourceDescriptor('flux', str(self.source)),),
                               progress_engine='FICTIONAL-HISTORY', file_opener=self.opener)

    async def wait(self, pilot, predicate):
        for _ in range(300):
            if predicate():
                await pilot.pause()
                return
            await asyncio.sleep(.01)
        self.fail('Navigation event barrier did not complete')

    async def ready(self, app, pilot):
        await self.wait(pilot, lambda: app.activity_accepts and app.progress_accepts
                        and not app.progress_busy and app.progress_snapshot is not None)

    async def position(self, app, pilot, number=25, *, middle=False):
        table = app.query_one('#table', DataTable)
        await pilot.press('home', *(['down'] * (number - 1)))
        await self.wait(pilot, lambda: app.selected_id == f'J{number}.1' and not app.progress_busy)
        rows = table.content_size.height - table.header_height
        y = number - 1 - rows // 2 if middle else number - rows
        table.scroll_to(y=max(0, y), animate=False, immediate=True)
        await pilot.pause()
        return table

    async def returned(self, app, pilot, base):
        await self.wait(pilot, lambda: app.screen is base and not app.navigation_pending(base))

    async def updates_while_open(self, app, pilot):
        a, p = app.activity_accepts, app.progress_accepts
        self.observer.gate.arm()
        self.service.gate.arm()
        app.collect_activity()
        app.collect_progress()
        await self.wait(pilot, lambda: self.observer.gate.entered.is_set()
                        and self.service.gate.entered.is_set())
        with self.source.open('a') as stream:
            stream.write('5,3,2,1.5\n')
        self.observer.gate.release.set()
        self.service.gate.release.set()
        await self.wait(pilot, lambda: app.activity_accepts > a and app.progress_accepts > p)

    async def test_details_mouse_and_enter_preserve_j25_j24_through_real_updates(self):
        for number, mouse in ((25, False), (25, True), (24, False), (24, True)):
            with self.subTest(number=number, mouse=mouse):
                app = self.app()
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.ready(app, pilot)
                    base = app.screen
                    table = await self.position(app, pilot, number, middle=number == 24)
                    before = table_view(table)
                    attempt = app.selected()['attempt_id']
                    for _ in range(2):
                        if mouse:
                            await pilot.click('#table', offset=(3, int(before['cursor_y'])))
                        else:
                            await pilot.press('enter')
                        self.assertIsInstance(app.screen, Inspect)
                        await self.updates_while_open(app, pilot)
                        await pilot.press('pagedown', 'escape')
                        await self.returned(app, pilot, base)
                        self.assertEqual(table_view(table), before)
                        self.assertEqual(app.selected()['attempt_id'], attempt)
                    refreshes = app.refreshes
                    await self.wait(pilot, lambda: app.refreshes >= refreshes + 2)
                    self.assertEqual(table_view(table), before)
                self.assertTrue(all(job['status'] == 'Hold' for job in self.store.snapshot()['jobs']))

    async def test_resize_round_trip_while_details_open_restores_exact_origin(self):
        for size in ((77, 24), (80, 24), (100, 30), (140, 40)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    base = app.screen
                    table = await self.position(app, pilot, 24, middle=True)
                    before = table_view(table)
                    await pilot.press('enter')
                    await pilot.resize_terminal(size[0] + 20, size[1])
                    await pilot.pause()
                    await pilot.resize_terminal(*size)
                    await pilot.press('escape')
                    await self.returned(app, pilot, base)
                    self.assertEqual(table_view(table), before)
                    await self.updates_while_open(app, pilot)
                    self.assertEqual(table_view(table), before)

    async def test_deliberately_hidden_cursor_and_horizontal_scroll_are_retained(self):
        app = self.app()
        async with app.run_test(size=(77, 24)) as pilot:
            await self.ready(app, pilot)
            base = app.screen
            table = await self.position(app, pilot)
            table.add_column('Synthetic overflow', key='fixture-wide', width=100, default='')
            await pilot.pause()
            table.scroll_to(x=35, y=2, animate=False, immediate=True)
            await pilot.pause()
            before = table_view(table)
            self.assertNotIn('J25.1', before['visible'])
            self.assertGreater(before['scroll'][0], 0)
            await pilot.press('enter', 'escape')
            await self.returned(app, pilot, base)
            self.assertEqual(table_view(table), before)
            await pilot.press('up')
            await pilot.pause()
            self.assertEqual(app.selected_id, 'J24.1')
            self.assertIn('J24.1', table_view(table)['visible'])

    async def test_nested_details_frozen_text_restores_only_each_actual_parent(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            base = app.screen
            table = await self.position(app, pilot)
            before = table_view(table)
            await pilot.press('enter', 'pagedown')
            details = app.screen
            scroll = details.query_one(VerticalScroll)
            await pilot.pause()
            position = tuple(scroll.scroll_offset)
            self.assertGreater(position[1], 0)
            await pilot.press('y')
            self.assertIsInstance(app.screen, FrozenText)
            await pilot.resize_terminal(100, 24)
            await pilot.resize_terminal(80, 24)
            await pilot.press('escape')
            await self.returned(app, pilot, details)
            self.assertEqual(tuple(scroll.scroll_offset), position)
            self.assertIs(app.screen, details)
            await pilot.press('escape')
            await self.returned(app, pilot, base)
            self.assertEqual(table_view(table), before)

    async def test_logs_sources_copy_files_scheduling_and_safe_cancel_returns(self):
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(app, pilot)
            base = app.screen
            table = await self.position(app, pilot, 24, middle=True)
            before = table_view(table)
            for key, kind in (('l', Inspect), ('s', Sources), ('y', CopyPreview),
                              ('f', FilesChooser), ('b', SharingInspect),
                              ('n', NoteEditor), ('x', Prompt)):
                with self.subTest(surface=kind.__name__):
                    await pilot.press(key)
                    await self.wait(pilot, lambda: isinstance(app.screen, kind))
                    if kind is CopyPreview:
                        await pilot.click('#copy-back')
                    elif kind is Sources:
                        await pilot.click('#sources-back')
                    else:
                        await pilot.press('escape')
                    await self.returned(app, pilot, base)
                    self.assertEqual(table_view(table), before)
            self.opener.assert_not_called()
            self.assertTrue(all(j['status'] == 'Hold' for j in self.store.snapshot()['jobs']))
            self.assertTrue(all(not j['note'] for j in self.store.snapshot()['jobs']))

    async def test_history_and_nested_entry_details_preserve_history_and_jobs_views(self):
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(app, pilot)
            base = app.screen
            table = await self.position(app, pilot)
            before = table_view(table)
            await pilot.press('g')
            await self.wait(pilot, lambda: isinstance(app.screen, ProgressHistory)
                            and app.screen.page is not None and not app.progress_busy)
            history = app.screen
            history_table = history.query_one(DataTable)
            await pilot.press('down')
            history_before = table_view(history_table)
            await pilot.press('d')
            self.assertIsInstance(app.screen, HistoryDetails)
            await pilot.press('y')
            self.assertIsInstance(app.screen, FrozenText)
            await pilot.press('escape')
            await self.wait(pilot, lambda: isinstance(app.screen, HistoryDetails))
            await pilot.press('escape')
            await self.returned(app, pilot, history)
            self.assertEqual(table_view(history_table), history_before)
            await pilot.press('escape')
            await self.returned(app, pilot, base)
            self.assertEqual(table_view(table), before)

    async def test_append_and_reorder_resolve_immutable_target_and_visible_anchor(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            base = app.screen
            table = await self.position(app, pilot, 24, middle=True)
            before = table_view(table)
            attempt = app.selected()['attempt_id']
            await pilot.press('enter')
            self.store.add(argv=['/unexecuted/fixture'], cwd=self.root, name='Appended', hold=True)
            app.refresh_state()
            await pilot.press('escape')
            await self.returned(app, pilot, base)
            self.assertEqual(table_view(table), before)
            await pilot.press('enter')
            self.store.reverse_projection = True
            app.refresh_state()
            await pilot.press('escape')
            await self.returned(app, pilot, base)
            after = table_view(table)
            self.assertEqual(app.selected()['attempt_id'], attempt)
            self.assertEqual(after['key'], 'J24.1')
            self.assertEqual(after['visible'][0], before['visible'][0])
            self.assertNotEqual(table.cursor_row, 23)

    async def test_removed_or_replaced_attempt_cannot_transfer_selection_authority(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                with self.store.transaction() as con:
                    con.execute('INSERT OR REPLACE INTO jobs(id,data) VALUES (?,?)',
                                (25, json.dumps(self.jobs[24])))
                app = self.app()
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.ready(app, pilot)
                    base = app.screen
                    await self.position(app, pilot)
                    await pilot.press('enter')
                    with self.store.transaction() as con:
                        if replacement:
                            job = self.store.get(con, 25)
                            job['attempt_id'] = 'replacement-fixture-attempt'
                            con.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), 25))
                        else:
                            con.execute('DELETE FROM jobs WHERE id=?', (25,))
                    app.refresh_state()
                    await pilot.press('escape')
                    await self.returned(app, pilot, base)
                    self.assertIsNone(app.selected())
                    self.assertIn('no longer available', str(app.query_one('#selected', Static).render()).lower())
                    await pilot.press('x')
                    self.assertIs(app.screen, base)
                    self.assertIsNone(app.progress_identity())

    async def test_external_cursor_survives_refresh_before_highlight_delivery(self):
        app = self.app()
        self.observer.observations = [
            {'id': f'E{i:02}-birth', 'engine': 'FICTIONAL', 'pid': 1000 + i,
             'status': 'sleeping', 'nproc': 1, 'age_seconds': 60,
             'exe': '/unexecuted/fixture', 'cwd': None,
             'ownership': 'External / unattributed', 'evidence': ['Synthetic identity']}
            for i in range(1, 31)]
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('tab')
            external = app.query_one('#external', DataTable)
            self.assertIs(app.focused, external)
            external.move_cursor(row=23, animate=False)
            await self.wait(pilot, lambda: app.external_selected_id == 'E24-birth')
            generations = (app.files_generation, app.suspension_selection_generation)

            external.action_cursor_down()
            self.assertEqual(external.cursor_row, 24)
            self.assertEqual(app.external_selected_id, 'E24-birth')
            app.refresh_state()

            self.assertEqual(app.external_selected_id, 'E25-birth')
            self.assertEqual(external.cursor_row, 24)
            await pilot.pause()
            self.assertEqual(app.external_selected_id, 'E25-birth')
            self.assertEqual(external.cursor_row, 24)
            self.assertEqual((app.files_generation, app.suspension_selection_generation),
                             tuple(value + 1 for value in generations))

    async def test_managed_and_external_focus_q_and_mouse_return(self):
        app = self.app()
        self.observer.observations = [
            {'id': f'E{i:02}-birth', 'engine': 'FICTIONAL', 'pid': 1000 + i,
             'status': 'sleeping', 'nproc': 1, 'age_seconds': 60,
             'exe': '/unexecuted/fixture', 'cwd': None,
             'ownership': 'External / unattributed', 'evidence': ['Synthetic identity']}
            for i in range(1, 31)]
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(app, pilot)
            base = app.screen
            managed = await self.position(app, pilot)
            await pilot.press('tab')
            external = app.query_one('#external', DataTable)
            self.assertIs(app.focused, external)
            await pilot.press(*(['down'] * 24))
            await pilot.pause()
            before, managed_before = table_view(external), table_view(managed)
            self.assertEqual(app.external_selected_id, 'E25-birth')
            await pilot.press('enter')
            self.assertIsInstance(app.screen, ExternalInspect)
            await pilot.press('q')
            await self.returned(app, pilot, base)
            self.assertIs(app.focused, external)
            self.assertEqual(table_view(external), before)
            self.assertEqual(table_view(managed), managed_before)
            await pilot.click('#external', offset=(3, int(before['cursor_y'])))
            self.assertIsInstance(app.screen, ExternalInspect)
            await pilot.press('escape')
            await self.returned(app, pilot, base)
            self.assertEqual(table_view(external), before)
            await pilot.press('shift+tab')
            self.assertIs(app.focused, managed)
            self.assertIsNone(app.external_selected_id)
            await pilot.pause()
            before_managed = table_view(managed)
            await pilot.press('enter', 'q')
            await self.returned(app, pilot, base)
            self.assertEqual(table_view(managed), before_managed)

    async def test_scientific_height_change_and_later_resize_keep_visible_anchor(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            base = app.screen
            table = await self.position(app, pilot)
            before = table_view(table)
            height = table.content_size.height
            await pilot.press('enter')
            self.service.minimal = True
            accepted = app.progress_accepts
            app.collect_progress()
            await self.wait(pilot, lambda: app.progress_accepts > accepted)
            await pilot.pause()
            self.assertGreater(table.content_size.height, height)
            await pilot.press('escape')
            await self.returned(app, pilot, base)
            after = table_view(table)
            self.assertEqual(after['visible'][0], before['visible'][0])
            self.assertEqual(after['cursor_y'], before['cursor_y'])
            self.assertEqual(after['key'], 'J25.1')
            await pilot.resize_terminal(100, 24)
            await pilot.resize_terminal(80, 24)
            await self.wait(pilot, lambda: not app.navigation_pending(base))
            self.assertEqual(table_view(table), after)

    async def test_old_cancel_and_order_confirmation_cannot_target_replacement_attempt(self):
        for key, answer in (('x', 'CANCEL'), ('o', '1')):
            with self.subTest(action=key):
                with self.store.transaction() as con:
                    con.execute('UPDATE jobs SET data=? WHERE id=?',
                                (json.dumps(self.jobs[24]), 25))
                app = self.app()
                async with app.run_test(size=(100, 30)) as pilot:
                    await self.ready(app, pilot)
                    base = app.screen
                    await self.position(app, pilot)
                    await pilot.press(key)
                    self.assertIsInstance(app.screen, Prompt)
                    with self.store.transaction() as con:
                        replacement = self.store.get(con, 25)
                        replacement['attempt_id'] = 'replacement-after-confirmation'
                        con.execute('UPDATE jobs SET data=? WHERE id=?',
                                    (json.dumps(replacement), 25))
                    app.refresh_state()
                    await pilot.press(*answer, 'enter')
                    await self.returned(app, pilot, base)
                    with self.store.transaction() as con:
                        current = self.store.get(con, 25)
                    self.assertEqual(current['attempt_id'], replacement['attempt_id'])
                    self.assertEqual(current['status'], 'Hold')
                    self.assertEqual(current['order'], replacement['order'])
                    self.assertIn('expired', str(app.query_one('#message', Static).render()).lower())

    async def test_user_key_before_deferred_offsets_cancels_old_return(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            base = app.screen
            table = await self.position(app, pilot)
            await pilot.press('enter')
            callbacks = []
            restore = app._restore_offsets
            with patch.object(app, '_restore_offsets', side_effect=lambda token: callbacks.append(token)):
                await pilot.press('escape')
                await self.wait(pilot, lambda: app.screen is base and bool(callbacks))
                await pilot.press('up')
                await self.wait(pilot, lambda: app.selected_id == 'J24.1')
                newer = table_view(table)
            for token in callbacks:
                restore(token)
            await pilot.pause()
            self.assertEqual(table_view(table), newer)
            self.assertEqual(app.selected_id, 'J24.1')


if __name__ == '__main__':
    unittest.main()
