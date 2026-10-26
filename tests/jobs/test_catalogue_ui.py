"""Actual keyboard/mouse catalogue navigation using disposable metadata only."""
from dataclasses import replace
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.store import Store
from cmw.jobs.catalogue import CatalogueStore
from cmw.jobs.catalogue_models import CatalogueQuery, CataloguePage, CatalogueRecord
from cmw.nodes.codec import encode_typed,decode_typed
from cmw.nodes.protocol import NodeError
try:
    from textual.widgets import DataTable,Input,Static
    from cmw.jobs.catalogue_ui import CatalogueSearch,CatalogueDetails
    from cmw.jobs.tui import JobsApp,Inspect
    from cmw.nodes.portal import NodesApp,row_key
    from tests.nodes.test_portal import FakeBackend
except ImportError:
    JobsApp = None


if JobsApp is not None:
    class CatalogueBackend(FakeBackend):
        def __init__(self,alias='MINI'):
            super().__init__(alias,remote=True)
            self.queries = []
            self.wait_query = None
            self.total = len(self.jobs)
            for index,job in enumerate(self.jobs):
                job['name'] = 'α literal.* family' if index % 2 else 'Other calculation'
                job['resources'].update(mpi_ranks=8,threads_per_rank=1)
                job['reason'] = ''

        def record(self,job):
            return CatalogueRecord(1,self.node_id,self.queue_id,job['id'],job['attempt_id'],job['display_id'],
                job['name'],job['engine'],job['status'],'synthetic note',0,job['order'],'','project','','series','',(),0,None)

        def call(self,operation,params=None,target=None,**kwargs):
            if operation == 'show':
                result = super().call('observe',{},target)
                result['result'] = {'job':deepcopy(next(job for job in self.jobs if job['target']==target))}
                return result
            if operation == 'catalogue.query':
                query = decode_typed(params['query'],CatalogueQuery)
                self.queries.append(query)
                if self.wait_query:
                    self.wait_query.wait(4)
                if self.fail:
                    raise NodeError('DISCONNECTED','Synthetic catalogue disconnected')
                records = [self.record(job) for job in self.jobs if query.text.casefold() in job['name'].casefold()]
                page = CataloguePage(1,self.node_id,self.queue_id,tuple(records[:query.limit]),None,'generation',
                    self.total,len(records),(('Run',1),('Hold',self.total-1)),'COMPLETE',112.)
                result = super().call('observe',{},self.jobs[0]['target'])
                result['result'] = {'page':encode_typed(page)}
                return result
            if operation == 'catalogue.show':
                result = super().call('observe',{},target)
                result['result'] = {'record':encode_typed(self.record(next(job for job in self.jobs if job['target']==target)))}
                return result
            if operation == 'catalogue.relations':
                from cmw.jobs.catalogue_relations import RelationPage,RelationRecord,QUALIFICATION
                result = super().call('observe',{},target)
                parent = dict(node_id='a'*32,queue_id='b'*32,job_id=1,attempt_id='c'*32)
                relation = RelationRecord('d'*32,'continuation',json.dumps(parent),'intent','UNRESOLVED_REMOTE','ACTIVE',111.,None,None,QUALIFICATION)
                page = RelationPage(1,self.node_id,self.queue_id,target['job_id'],target['attempt_id'],1,(relation,),'COMPLETE',112.)
                result['result'] = {'relations':encode_typed(page)}
                return result
            return super().call(operation,params,target,**kwargs)


@unittest.skipIf(JobsApp is None,'Optional Jobs UI extra unavailable')
class CatalogueUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-catalogue-ui-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root/'queue')
        self.jobs = [self.store.add(argv=['/never/executed'],cwd=self.root,name='α literal.* family' if i%2 else 'Other calculation',hold=True) for i in range(8)]
        self.catalogue = CatalogueStore(self.store)
        for job in self.jobs:
            self.catalogue.edit(job['id'],{'project':'fictional','series':'series'},expected_revision=0,expected_attempt_id=job['attempt_id'])
        for method in ('collect_activity','collect_progress','collect_execution'):
            patched = patch.object(JobsApp,method)
            patched.start(); self.addCleanup(patched.stop)

    async def until(self,pilot,predicate):
        for _ in range(120):
            if predicate():
                return
            await pilot.pause(.025)
        self.fail('Catalogue UI did not reach bounded expected state')

    async def search(self,app,pilot,text):
        await pilot.press('/')
        self.assertIsInstance(app.screen,CatalogueSearch)
        app.screen.query_one('#catalogue-text',Input).value = text
        await pilot.click('#catalogue-apply')
        await self.until(pilot,lambda:bool(app.catalogue_pages) and not app.catalogue_busy)
        await pilot.pause()

    def render(self,app,label,size):
        destination = os.environ.get('CMW_CATALOGUE_RENDER_DIR')
        if destination:
            root = Path(destination);root.mkdir(parents=True,exist_ok=True)
            (root/f'{label}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())

    async def test_local_filter_clears_hidden_target_preserves_queue_and_clear_view(self):
        before = [(job['id'],job['order'],job['status']) for job in self.store.snapshot()['jobs']]
        app = JobsApp(self.store)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause()
            self.assertEqual(app.selected_id,'J1.1')
            await self.search(app,pilot,'literal.*')
            self.assertEqual(app.row_ids,['J2.1','J4.1','J6.1','J8.1'])
            self.assertIsNone(app.selected())
            self.assertIsNone(app.selected_id)
            with patch.object(self.store,'change',side_effect=AssertionError('Hidden control target')):
                await pilot.press('x','h','u')
            table = app.query_one('#table',DataTable)
            table.focus(); await pilot.press('down')
            await pilot.pause()
            chosen = app.selected_id
            self.assertIn(chosen,app.row_ids)
            await pilot.press('enter')
            self.assertIsInstance(app.screen,Inspect)
            await pilot.press('escape')
            self.assertEqual(app.selected_id,chosen)
            await pilot.press('/')
            await pilot.click('#catalogue-clear')
            await pilot.pause()
            self.assertEqual(app.selected_id,chosen)
            self.assertEqual(len(app.row_ids),8)
            self.assertIn('Filter cleared',str(app.query_one('#message',Static).render()))
            self.assertEqual([(job['id'],job['order'],job['status']) for job in self.store.snapshot()['jobs']],before)
            await pilot.press('q')

    async def test_search_typing_is_inert_and_local_detail_series_is_explicit(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(100,30)) as pilot:
            await pilot.pause()
            await pilot.press('/')
            with patch.object(CatalogueStore,'query',side_effect=AssertionError('Per-keystroke query')):
                await pilot.press('a','b','c','backspace')
                await pilot.press('escape')
            await pilot.press('t')
            await self.until(pilot,lambda:isinstance(app.screen,CatalogueDetails) and app.screen.record is not None)
            self.assertIn('HUMAN ORGANIZATION',str(app.screen.query_one('#catalogue-body',Static).render()))
            await pilot.click('#catalogue-members')
            await self.until(pilot,lambda:bool(app.catalogue_pages))
            self.assertEqual(app.catalogue_query.series,'series')
            await pilot.press('q')

    async def test_local_superseded_query_cancels_and_late_result_cannot_replace(self):
        app = JobsApp(self.store)
        entered,released = threading.Event(),threading.Event()
        original = CatalogueStore.query
        def slow(store,query,**kwargs):
            if query.text == 'Other':
                entered.set()
                while not released.is_set() and not kwargs['cancelled'].is_set():
                    released.wait(.01)
            return original(store,query,**kwargs)
        async with app.run_test(size=(80,24)) as pilot:
            with patch.object(CatalogueStore,'query',slow):
                app.catalogue_changed(('apply',CatalogueQuery(text='Other'),None))
                await self.until(pilot,entered.is_set)
                app.catalogue_changed(('apply',CatalogueQuery(text='literal.*'),None))
                await self.until(pilot,lambda:not app.catalogue_busy and bool(app.catalogue_pages))
                self.assertEqual(app.catalogue_query.text,'literal.*')
                self.assertEqual(app.row_ids,['J2.1','J4.1','J6.1','J8.1'])
            await pilot.press('q')

    async def test_remote_duplicate_ids_filter_disconnect_and_late_response(self):
        first,second = CatalogueBackend('PRO'),CatalogueBackend('MINI')
        app = NodesApp({'PRO':first,'MINI':second})
        async with app.run_test(size=(100,30)) as pilot:
            await self.until(pilot,lambda:len(app.rows)==10)
            selected = app.selected_key
            await self.search(app,pilot,'literal.*')
            await self.until(pilot,lambda:len(app.catalogue_pages)==2)
            self.assertEqual(len(app.rows),4)
            self.assertNotIn(selected,app.rows)
            self.assertIsNone(app.selected())
            app.query_one('#table',DataTable).focus()
            await pilot.press('down')
            await pilot.pause()
            self.assertIsNotNone(app.selected())
            selected = app.selected_key
            await pilot.press('t')
            await self.until(pilot,lambda:isinstance(app.screen,CatalogueDetails) and app.screen.record is not None)
            await pilot.press('escape')
            self.assertEqual(app.selected_key,selected)
            first.fail=True
            app.catalogue_changed(('apply',CatalogueQuery(text='Other'),None))
            await self.until(pilot,lambda:'PRO' in app.catalogue_errors)
            self.assertIn('unavailable / partial',app.catalogue_summary())
            self.assertTrue(all(owner=='MINI' for owner,_ in app.rows.values()))
            second.wait_query = threading.Event()
            app.catalogue_changed(('apply',CatalogueQuery(text='literal.*'),None))
            await self.until(pilot,lambda:bool(second.queries) and second.queries[-1].text=='literal.*')
            app.catalogue_changed(('clear',None,None))
            second.wait_query.set()
            await pilot.pause(.25)
            self.assertIsNone(app.catalogue_query)
            self.assertFalse(app.catalogue_pages)
            await pilot.press('q')

    async def test_local_remote_four_sizes_actual_mouse_search_and_render(self):
        for size in ((77,24),(80,24),(100,30),(140,40)):
            for remote in (False,True):
                app = NodesApp({'MINI':CatalogueBackend()}) if remote else JobsApp(self.store)
                async with app.run_test(size=size) as pilot:
                    await self.until(pilot,lambda:bool(app.rows) if remote else bool(app.row_ids))
                    await pilot.press('/')
                    for name in ('catalogue-apply','catalogue-clear','catalogue-back','catalogue-tags'):
                        region = app.screen.query_one('#'+name).region
                        self.assertGreaterEqual(region.y,0)
                        self.assertLessEqual(region.bottom,size[1])
                        self.assertLessEqual(region.right,size[0])
                    self.assertGreaterEqual(app.screen.query_one('#catalogue-text',Input).content_size.height,1)
                    self.render(app,'remote-search' if remote else 'local-search',size)
                    app.screen.query_one('#catalogue-text',Input).value = 'literal.*'
                    await pilot.click('#catalogue-apply')
                    await self.until(pilot,lambda:bool(app.catalogue_pages))
                    self.assertIsNone(app.selected())
                    self.render(app,'remote-filtered' if remote else 'local-filtered',size)
                    app.query_one('#table',DataTable).focus()
                    await pilot.press('down','t')
                    await self.until(pilot,lambda:isinstance(app.screen,CatalogueDetails) and app.screen.record is not None)
                    self.assertGreater(app.screen.query_one('#catalogue-details-scroll').size.height,5)
                    self.render(app,'remote-catalogue' if remote else 'local-catalogue',size)
                    if remote:
                        body = str(app.screen.query_one('#catalogue-body',Static).render())
                        self.assertIn('UNRESOLVED_REMOTE',body)
                        self.assertIn('continuation · intent · ACTIVE',body)
                        self.assertNotIn('parent_json',body)
                        await pilot.press('end')
                        await pilot.pause()
                        self.render(app,'remote-relationship',size)
                    await pilot.press('escape','q')

    async def test_local_paging_invalidated_cursor_is_clear_and_unknown_summary_is_global(self):
        with self.store.transaction() as con:
            job = self.store.get(con,self.jobs[0]['id'])
            job['status'] = 'Unknown'
            self.store.save(con,job)
        app = JobsApp(self.store)
        async with app.run_test(size=(100,30)) as pilot:
            app.catalogue_changed(('apply',CatalogueQuery(text='literal.*',limit=2),None))
            await self.until(pilot,lambda:bool(app.catalogue_pages))
            self.assertEqual(len(app.row_ids),2)
            self.assertIn('Unknown 1',str(app.query_one('#controller',Static).render()))
            self.assertIsNone(app.selected())
            await pilot.press('/')
            from textual.widgets import Select
            app.screen.query_one('#catalogue-owner',Select).value = 'LOCAL'
            self.catalogue.edit(self.jobs[0]['id'],{'project':'changed'},expected_revision=1,expected_attempt_id=self.jobs[0]['attempt_id'])
            await pilot.click('#catalogue-next')
            await self.until(pilot,lambda:'LOCAL' in app.catalogue_errors)
            self.assertIn('cursor',app.catalogue_errors['LOCAL'].lower())
            self.assertFalse(app.row_ids)
            self.assertIsNone(app.selected())
            await pilot.press('/')
            await pilot.click('#catalogue-clear')
            await pilot.pause()
            self.assertIsNone(app.selected())
            await pilot.press('q')

    async def test_remote_older_metadata_requires_exact_show_before_controls(self):
        backend = CatalogueBackend()
        original = backend.call
        entered,released = threading.Event(),threading.Event()
        def bounded(operation,params=None,target=None,**kwargs):
            if operation == 'show':
                entered.set(); released.wait(4)
            result = original(operation,params,target,**kwargs)
            if operation == 'status':
                result['result']['state']['jobs'] = result['result']['state']['jobs'][:1]
            return result
        backend.call = bounded
        app = NodesApp({'MINI':backend})
        async with app.run_test(size=(100,30)) as pilot:
            await self.until(pilot,lambda:bool(app.rows))
            await self.search(app,pilot,'literal.*')
            self.assertEqual(len(app.rows),2)
            self.assertIsNone(app.selected())
            app.query_one('#table',DataTable).focus()
            await pilot.press('down')
            await self.until(pilot,entered.is_set)
            self.assertIsNone(app.selected())
            with patch.object(app,'queue',side_effect=AssertionError('Unhydrated control request')):
                app.preview_control('cancel')
            released.set()
            await self.until(pilot,lambda:app.selected() is not None)
            selected = deepcopy(app.selected()[1]['target'])
            await pilot.press('x')
            await self.until(pilot,lambda:any(operation=='control.preview' for operation,_,_ in backend.calls))
            self.assertEqual([target for operation,_,target in backend.calls if operation=='control.preview'][-1],selected)
            await pilot.press('escape','q')

    async def test_saved_filter_view_survives_append_and_snapshot_reorder(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(80,24)) as pilot:
            await self.search(app,pilot,'literal.*')
            table = app.query_one('#table',DataTable)
            table.focus(); await pilot.press('down','down'); await pilot.pause()
            selected = app.selected_id
            await pilot.press('enter')
            self.store.add(argv=['/never/executed'],cwd=self.root,name='appended',hold=True)
            original = self.store.snapshot
            def reordered(**kwargs):
                state = original(**kwargs)
                state['jobs'].reverse()
                return state
            with patch.object(self.store,'snapshot',reordered):
                app.refresh_state()
                await pilot.press('escape'); await pilot.pause()
                self.assertEqual(app.selected_id,selected)
                self.assertEqual(app.row_ids,['J2.1','J4.1','J6.1','J8.1'])
                self.assertEqual(table.ordered_rows[table.cursor_row].key.value,selected)
            await pilot.press('q')

    async def test_bounded_portal_refresh_for_1000_and_10000_metadata_histories(self):
        timings = []
        for count in (1000,10000):
            backend = CatalogueBackend()
            template = deepcopy(backend.jobs[1])
            backend.jobs = []
            for index in range(count):
                job = deepcopy(template)
                job.update(id=index+1,display_id=f'J{index+1}.1',attempt_id=f'{index+1:032x}',order=index+1)
                job['target'].update(job_id=index+1,attempt_id=job['attempt_id'])
                backend.jobs.append(job)
            backend.jobs[0]['status'] = 'Run'
            backend.total = count
            original = backend.call
            def bounded(operation,params=None,target=None,**kwargs):
                result = original(operation,params,target,**kwargs)
                if operation == 'status':
                    result['result']['state']['jobs'] = result['result']['state']['jobs'][:50]
                    result['result']['status_counts'] = {'Run':1,'Hold':count-1}
                    result['result']['job_count'] = count
                return result
            backend.call = bounded
            app = NodesApp({'MINI':backend})
            async with app.run_test(size=(100,30)) as pilot:
                await self.until(pilot,lambda:bool(app.rows))
                await self.search(app,pilot,'literal.*')
                self.assertEqual(len(app.rows),50)
                table = app.query_one('#table',DataTable)
                table.focus()
                await pilot.press(*(['down']*30))
                await pilot.pause()
                selected,offset = app.selected_key,table.scroll_y
                await pilot.press('t')
                await self.until(pilot,lambda:isinstance(app.screen,CatalogueDetails) and app.screen.record is not None)
                await pilot.click('#catalogue-detail-back')
                await pilot.pause()
                self.assertEqual(app.selected_key,selected)
                self.assertEqual(table.scroll_y,offset)
                rows = tuple(table.ordered_rows)
                start = time.perf_counter()
                for _ in range(10):
                    app.render_state()
                duration = (time.perf_counter()-start)/10
                self.assertEqual(tuple(table.ordered_rows),rows)
                timings.append({'history_jobs':count,'visible_rows':len(app.rows),'mean_refresh_seconds':duration,
                                'scope':'actual Textual render from bounded owner metadata/status pages; no scientific files'})
                self.assertLess(duration,1.0)
                await pilot.press('q')
        destination = os.environ.get('CMW_CATALOGUE_PERFORMANCE_FILE')
        if destination:
            Path(destination).write_text(json.dumps(timings,indent=2)+'\n')

    async def test_next_owner_page_reissues_unfinished_peer_generation(self):
        first,second = CatalogueBackend('PRO'),CatalogueBackend('MINI')
        second.wait_query = threading.Event()
        app = NodesApp({'PRO':first,'MINI':second})
        async with app.run_test(size=(100,30)) as pilot:
            await self.until(pilot,lambda:len(app.rows)==10)
            app.catalogue_changed(('apply',CatalogueQuery(text='literal.*'),None))
            await self.until(pilot,lambda:'PRO' in app.catalogue_pages and bool(second.queries))
            app.catalogue_pages['PRO'] = replace(app.catalogue_pages['PRO'],next_cursor='synthetic-next')
            app.catalogue_changed(('next',app.catalogue_query,'PRO'))
            second.wait_query.set()
            await self.until(pilot,lambda:len(app.catalogue_pages)==2)
            self.assertEqual(len(second.queries),2)
            self.assertEqual(first.queries[-1].cursor,'synthetic-next')
            self.assertFalse(app.catalogue_errors)
            self.assertNotIn('pending',app.catalogue_summary())
            await pilot.press('q')
