"""Stable generic history navigation and cooperative synthetic observer lifetime."""
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.folder_opener import OpenResult
from cmw.jobs.locations import LocationDescriptor
from cmw.jobs.store import Store
from cmw.progress.models import SourceDescriptor
from cmw.progress.registry import register_adapter
from cmw.progress.service import ObservationService
from tests.jobs.test_history_cli import FictionalHistoryAdapter

try:
    from cmw.jobs.tui import JobsApp, NoteEditor
    from cmw.jobs.history_ui import ProgressHistory, HistoryDetails
    from textual.widgets import DataTable, Static, TextArea
    from textual.containers import VerticalScroll
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console tests require the optional Jobs extra')
class HistoryUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='cmw-history-ui-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.store=Store(self.root/'state')
        self.jobs=[self.store.add(argv=['/unexecuted/fixture'],cwd=self.root,name='Synthetic history '+str(index),
                    engine='FICTIONAL-HISTORY',hold=True) for index in range(2)]
        self.source=self.root/'flux.txt'
        self.source.write_text('4,2,1,3.75\n4,2,2,3.25\n5,3,1,2.0\n')
        self.sources=(SourceDescriptor('flux',str(self.source)),)
        self.opener=Mock(return_value=OpenResult('OPEN_REQUESTED','Synthetic explicit open',str(self.root)))
        for target in (patch.object(JobsApp,'collect_activity'),):
            target.start()
            self.addCleanup(target.stop)
        from cmw.progress.registry import _REGISTRY
        registration=patch.dict(_REGISTRY)
        registration.start()
        self.addCleanup(registration.stop)
        register_adapter('FICTIONAL-HISTORY',FictionalHistoryAdapter)

    def app(self,**kwargs):
        return JobsApp(self.store,progress_sources=self.sources,file_opener=self.opener,
                       location_descriptors=(LocationDescriptor('output',str(self.root)),),**kwargs)

    async def settled(self,app,pilot):
        for _ in range(300):
            view=app.history_view
            if not app.progress_busy and (view is None or view.page is not None and view.page.status!='LOADING'):
                await pilot.pause()
                return
            await asyncio.sleep(.01)
        self.fail('Bounded observation did not settle')

    async def started(self,event):
        for _ in range(200):
            if event.is_set():
                return
            await asyncio.sleep(.01)
        self.fail('Expected worker did not start')

    async def test_three_level_drilldown_full_timing_details_and_notes_files_keys(self):
        app=self.app()
        before=self.store.path.read_bytes()
        async with app.run_test(size=(80,24)) as pilot:
            await self.settled(app,pilot)
            self.assertIn('2.5s wall/native',str(app.query_one('#progress',Static).render()))
            await pilot.press('g')
            await self.settled(app,pilot)
            view=app.screen
            self.assertIsInstance(view,ProgressHistory)
            self.assertEqual(app.selected_id,self.jobs[0]['display_id'])
            self.assertEqual(view.selected_entry().native_label,'geometry 4')
            await pilot.press('d')
            self.assertIsInstance(app.screen,HistoryDetails)
            text=str(app.screen.query_one('#history-detail',Static).render())
            self.assertIn('observer-derived',text)
            self.assertIn('no full duration',text)
            await pilot.press('escape','enter')
            await self.settled(app,pilot)
            self.assertEqual(len(view.selected_entry().context),2)
            await pilot.press('d')
            self.assertIn('No recoverable native timing',str(app.screen.query_one('#history-detail',Static).render()))
            await pilot.press('escape','enter')
            await self.settled(app,pilot)
            self.assertEqual(len(view.selected_entry().context),3)
            await pilot.press('d')
            self.assertIn('wall, native',str(app.screen.query_one('#history-detail',Static).render()))
            await pilot.press('escape','backspace')
            await self.settled(app,pilot)
            self.assertEqual(len(view.selected_entry().context),2)
            await pilot.press('escape','n','f','g','p','o')
            self.assertIsInstance(app.screen,NoteEditor)
            self.assertEqual(app.screen.query_one(TextArea).text,'fgpo')
            self.opener.assert_not_called()
            await pilot.press('escape','f')
            for _ in range(100):
                if not app.files_busy:
                    break
                await asyncio.sleep(.01)
            self.opener.assert_called_once()
            await pilot.press('q')
        self.assertEqual(self.store.path.read_bytes(),before)

    async def test_older_selection_scroll_and_open_detail_survive_append(self):
        self.source.write_text(''.join(f'{index},1,1,2.5\n' for index in range(1,61)))
        app=self.app()
        async with app.run_test(size=(80,24)) as pilot:
            await self.settled(app,pilot)
            await pilot.press('g')
            await self.settled(app,pilot)
            view=app.screen
            table=view.query_one(DataTable)
            table.move_cursor(row=12,animate=False)
            await pilot.pause()
            selected=view.selected_entry().entry_id
            position=table.scroll_offset
            with self.source.open('a') as output:
                output.write('61,1,1,2.0\n')
            app.collect_progress()
            await self.settled(app,pilot)
            self.assertEqual(view.selected_entry().entry_id,selected)
            self.assertEqual(table.scroll_offset,position)
            await pilot.press('d')
            detail=app.screen
            scroll=detail.query_one(VerticalScroll)
            scroll.scroll_to(y=8,animate=False,force=True)
            await pilot.pause()
            detail_position=scroll.scroll_offset
            with self.source.open('a') as output:
                output.write('62,1,1,1.0\n')
            app.collect_progress()
            await self.settled(app,pilot)
            detail.refresh_content()
            self.assertIs(app.screen,detail)
            self.assertEqual(detail.entry_id,selected)
            self.assertEqual(scroll.scroll_offset,detail_position)
            await pilot.press('escape',']')
            await self.settled(app,pilot)
            self.assertEqual(view.selected_entry().native_label,'geometry 51')
            await pilot.press('[')
            await self.settled(app,pilot)
            self.assertEqual(view.selected_entry().native_label,'geometry 1')
            await pilot.press('escape','q')

    async def test_parser_pending_work_catches_up_and_ctrl_c_detaches_from_history(self):
        from dataclasses import replace
        service=ObservationService()
        original=service.poll
        calls=[]
        def pending_once(context):
            snapshot=original(context)
            calls.append(snapshot)
            return replace(snapshot,completeness='CATCHING_UP') if len(calls)==1 else snapshot
        service.poll=Mock(side_effect=pending_once)
        app=self.app(progress_service=service)
        async with app.run_test(size=(80,24)) as pilot:
            for _ in range(100):
                if len(calls)==2 and not app.progress_busy:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(len(calls),2)
            self.assertFalse(any(source.has_more for source in app.progress_snapshot.sources))
            await pilot.press('g')
            await self.settled(app,pilot)
            await pilot.press('ctrl+c')
        self.assertTrue(service._closed)
        self.assertFalse(service._contexts)

    async def test_replaced_sources_invalidate_page_and_qualify_open_old_detail(self):
        app=self.app()
        async with app.run_test(size=(80,24)) as pilot:
            await self.settled(app,pilot)
            await pilot.press('g')
            await self.settled(app,pilot)
            view=app.screen
            old=view.page.source_revision
            await pilot.press('d')
            details=app.screen
            replacement=self.root/'replacement'
            replacement.write_text('9,1,1,2.0\n')
            replacement.replace(self.source)
            app.collect_progress()
            await self.settled(app,pilot)
            self.assertNotEqual(view.page.source_revision,old)
            details.refresh_content()
            self.assertIn('earlier cached evidence',str(details.query_one('#history-detail',Static).render()))
            self.assertEqual([entry.native_label for entry in view.entries.values()],['geometry 9'])
            await pilot.press('escape','escape','q')

    async def test_stage_and_segment_filter_and_queued_unknown_empty_states(self):
        app=self.app()
        async with app.run_test(size=(77,24)) as pilot:
            await self.settled(app,pilot)
            await pilot.press('g')
            await self.settled(app,pilot)
            view=app.screen
            await pilot.press('s','v')
            await self.settled(app,pilot)
            self.assertEqual((view.stage,view.segment),('fixture-stage',1))
            self.assertEqual(len(view.page.entries),2)
            await pilot.press('escape')
            app.progress_engine='NO_HISTORY_ADAPTER'
            app.sync_progress_selection()
            await self.settled(app,pilot)
            await pilot.press('g')
            await self.settled(app,pilot)
            self.assertEqual(app.screen.page.status,'ADAPTER_UNAVAILABLE')
            self.assertIn('No observed entries',str(app.screen.query_one('#history-state',Static).render()))
            await pilot.press('escape','q')

    async def test_close_and_job_switch_during_blocked_history_do_not_start_replacements(self):
        for operation in ('close','job','stage'):
            with self.subTest(operation=operation):
                started,release=threading.Event(),threading.Event()
                service=ObservationService()
                app=self.app(progress_service=service)
                async with app.run_test(size=(80,24)) as pilot:
                    await self.settled(app,pilot)
                    original=service.history_page
                    def gated(context,**kwargs):
                        started.set()
                        release.wait(5)
                        return original(context,**kwargs)
                    with patch.object(service,'history_page',side_effect=gated) as observe:
                        await pilot.press('g')
                        await self.started(started)
                        view=app.screen
                        if operation=='close':
                            await pilot.press('escape')
                        elif operation=='job':
                            app.selected_id=self.jobs[1]['display_id']
                            app.sync_progress_selection()
                        else:
                            view.stage='other-stage'
                            view.request()
                        for _ in range(10):
                            app.collect_progress()
                        self.assertEqual(observe.call_count,1)
                        self.assertTrue(app.progress_busy)
                        release.set()
                        for _ in range(200):
                            if not app.progress_busy:
                                break
                            await asyncio.sleep(.01)
                        if operation=='job':
                            self.assertTrue(view.expired)
                            self.assertIsNone(view.page)
                        elif operation=='close':
                            self.assertIsNone(app.history_view)
                        else:
                            await self.settled(app,pilot)
                            self.assertEqual(view.page.entries,())
                    if app.history_view:
                        await pilot.press('escape')
                    await pilot.press('q')
                self.assertFalse(service._contexts)


if __name__=='__main__':
    unittest.main()
