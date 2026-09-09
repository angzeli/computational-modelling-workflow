from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import importlib.util
if importlib.util.find_spec('textual') is None:
    raise unittest.SkipTest('Console tests require the optional CMW jobs extra')

from textual.widgets import DataTable, Input, Static

from cmw.jobs.store import Store
from cmw.jobs.tui import JobsApp, Inspect


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Existing managed-client tests do not need the host process table.
        observation = patch.object(JobsApp, 'collect_activity')
        observation.start()
        self.addCleanup(observation.stop)

    async def test_interactions_selection_resize_logs_and_render(self):
        with tempfile.TemporaryDirectory(prefix='cmw tui ') as directory:
            store = Store(Path(directory)/'state')
            for index in range(24):
                store.add(argv=['/bin/echo','[bold] literal'], cwd=directory,
                          name=f'{index} 长名称 / very long prepared computation', engine='Synthetic',cpus=8,memory_gib=20)
            with store.transaction() as con:
                for index, status in enumerate(('Run','Done','Fail','Unknown'),1):
                    job = store.get(con,index)
                    job.update(status=status, order=None, started_at=1700000000,
                               finished_at=1700000100 if status in {'Done','Fail'} else None,
                               reason='Fixed synthetic evidence')
                    store.save(con,job)
                store.normalize(con)
            app = JobsApp(store,clock=lambda:1700000300)
            async with app.run_test(size=(130,40)) as pilot:
                await pilot.pause()
                table=app.query_one(DataTable)
                self.assertEqual(table.row_count,24)
                self.assertEqual(len(table.columns),10)
                await pilot.press('down','down','down','down')
                selected=app.selected_id
                self.assertEqual(selected,'J5.1')
                await pilot.press('h')
                self.assertEqual(app.selected()['status'],'Hold')
                await pilot.press('h')
                self.assertEqual(app.selected()['status'],'Queue')
                await pilot.press('o')
                await pilot.press('2','enter')
                await pilot.pause()
                self.assertEqual(app.selected()['order'],2)
                await pilot.press('enter')
                self.assertIsInstance(app.screen,Inspect)
                await pilot.press('escape')
                await pilot.press('l')
                self.assertIsInstance(app.screen,Inspect)
                await pilot.press('escape')
                await pilot.resize_terminal(64,28)
                await pilot.pause()
                self.assertEqual(app.selected_id,selected)
                self.assertEqual(len(table.columns),5)
                await pilot.press('pagedown')
                self.assertGreater(table.cursor_row,4)
                await pilot.press('home')
                await pilot.resize_terminal(130,40)
                await pilot.pause()
                await pilot.press('down')
                app.refresh_state()
                self.assertEqual(app.selected_id,app.row_ids[table.cursor_row])
                # Non-ideal states and fixed observation time are inspectable.
                artifact=os.environ.get('CMW_JOBS_RENDER')
                if artifact:
                    Path(artifact).write_text(app.export_screenshot())
                await pilot.press('q')
            self.assertFalse(store.snapshot()['controller']['online'])

    async def test_cancel_confirmation_and_stale_action(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state')
            store.add(argv=['/bin/echo','ok'],cwd=directory,name='cancel me')
            app=JobsApp(store)
            async with app.run_test(size=(100,32)) as pilot:
                await pilot.pause()
                await pilot.press('x')
                await pilot.press('C','A','N','C','E','L','enter')
                await pilot.pause()
                self.assertEqual(app.selected()['status'],'Cancelled')
                await pilot.press('h')
                self.assertIn('Not applied',str(app.query_one('#message',Static).render()))
                await pilot.press('ctrl+c')

    async def test_log_cache_reads_only_changed_bounded_tails(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state')
            job=store.add(argv=['/bin/echo','ok'],cwd=directory,name='logs')
            logfile=Path(job['logs']['stdout'])
            logfile.parent.mkdir(parents=True)
            logfile.write_text('\x1b[31m[bold] not markup\x1b[0m')
            app=JobsApp(store)
            async with app.run_test(size=(100,32)) as pilot:
                await pilot.pause()
                with patch('cmw.jobs.tui.tail',wraps=__import__('cmw.jobs.cli',fromlist=['tail']).tail) as mocked:
                    await pilot.press('l')
                    await pilot.pause()
                    first=mocked.call_count
                    app.screen.refresh_content()
                    self.assertEqual(mocked.call_count,first)
                    logfile.write_text('changed')
                    app.screen.refresh_content()
                    self.assertEqual(mocked.call_count,first+2)
                await pilot.press('escape','q')

    async def test_fixed_render_evidence(self):
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state')
            for name in ('Prepared-SP', 'relax-long-name', 'density-map', 'old-SP', 'old-relax', 'cancelled'):
                store.add(argv=['/bin/bash','/example/prepared case/run.sh'], cwd=directory,
                          name=name,engine='Synthetic',cpus=8,memory_gib=20,mpi_ranks=8,threads_per_rank=1)
            state=store.snapshot(now=1700000600)
            states=['Run','Queue','Hold','Done','Fail','Cancelled']
            for i,job in enumerate(state['jobs']):
                job.update(status=states[i],order={1:1,2:2}.get(i),
                           started_at=None if i in (1,2,5) else 1700000000,
                           enqueued_at=1699999900, finished_at=1700000450 if i>=3 else None,
                           elapsed=None if i in (1,2,5) else 600 if i==0 else 450,
                           cwd='/example/prepared case/long path with Unicode 计算',
                           reason=['—','Waiting for J1.1','User hold','Execution completed','Exit code 7','Cancelled before execution'][i])
                job['logs']={k:f'/example/cmw-jobs/attempts/{i}/{k}.log' for k in ('stdout','stderr')}
            state['controller'].update(online=True,dispatch=True,stale=False,age_seconds=1)
            state['events']=[{'id':1,'time':1700000000,'job_id':1,'message':'Execution started'},
                             {'id':2,'time':1700000001,'job_id':2,'message':'Enqueued at order 1'}]
            class FixedStore:
                def snapshot(self, **kwargs):
                    return deepcopy(state)
            app=JobsApp(FixedStore(),clock=lambda:1700000600)
            async with app.run_test(size=(130,32)) as pilot:
                await pilot.pause()
                await pilot.press('down')
                location=os.environ.get('CMW_JOBS_FIXED_RENDER')
                if location:
                    Path(location+'-wide.svg').write_text(app.export_screenshot())
                await pilot.resize_terminal(64,28)
                await pilot.pause()
                self.assertEqual(app.selected_id,'J2.1')
                if location:
                    Path(location+'-narrow.svg').write_text(app.export_screenshot())
                await pilot.press('q')

    async def test_confirmation_revalidates_concurrent_change(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state')
            job=store.add(argv=['/bin/echo','ok'],cwd=directory,name='concurrent selection')
            app=JobsApp(store)
            async with app.run_test(size=(100,32)) as pilot:
                await pilot.pause()
                await pilot.press('x')
                store.change(job['id'],'hold')
                await pilot.press('C','A','N','C','E','L','enter')
                await pilot.pause()
                self.assertEqual(app.selected()['status'],'Hold')
                self.assertIn('changed since selection',str(app.query_one('#message',Static).render()))
                await pilot.press('q')
