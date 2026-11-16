"""Read-only study navigation preserves selected attempts and compact dimensions."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.study_models import StudyResult
from cmw.nodes.codec import encode_typed
from cmw.nodes.protocol import NodeError
from tests import waiting
try:
    from textual.widgets import Static
    from cmw.jobs.catalogue_ui import CatalogueDetails
    from cmw.jobs.study_ui import StudyScreen
    from cmw.jobs.tui import JobsApp
    from cmw.nodes.portal import NodesApp
    from tests.jobs.test_catalogue_ui import CatalogueBackend
    from cmw.jobs.store import Store
except ImportError:
    JobsApp=None


def result(target):
    return StudyResult(1,target['node_id'],target['queue_id'],'e'*32,'view','COMPLETE','f'*64,
        json.dumps({'selected_target':target,'cases':[
            {'case_id':'encut-400','target':target,'status':'Done','binding':'verified',
             'process':'exit 0','completeness':'complete','native_convergence':'electronic PASS',
             'policy':'not assessed','value':-10.,'delta':None,'reason':'explicit compare required'},
            {'case_id':'encut-500','status':'missing','binding':'unregistered','reason':'no selected result'}],
            'coverage':'1 / 2 recorded cases; no scientific acceptance','metric':'energy_sigma_zero_eV',
            'normalization':'none','verdict':'NOT ASSESSED'}))


if JobsApp is not None:
    class StudyBackend(CatalogueBackend):
        def __init__(self):
            super().__init__('MINI');self.study_requests=[];self.study_fail=False;self.wrong_target=False
        def call(self,operation,params=None,target=None,**kwargs):
            if operation=='study.view':
                self.study_requests.append((operation,deepcopy(params),deepcopy(target)))
                if self.study_fail:raise NodeError('DISCONNECTED','Synthetic owner unavailable')
                value=result(target)
                if self.wrong_target:value=result({**target,'attempt_id':'9'*32})
                envelope=super().call('observe',{},target);envelope['result']={'study':encode_typed(value)}
                return envelope
            return super().call(operation,params,target,**kwargs)


@unittest.skipIf(JobsApp is None,'Optional Jobs UI extra unavailable')
class StudyUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='cmw-study-ui-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.store=Store(self.root/'state')
        enrollment=self.store.enroll_queue('a'*32,'linux:synthetic-only')
        self.job=self.store.add(argv=['/never/executed'],cwd=self.root,name='Reviewed study',hold=True)
        self.target={'node_id':'a'*32,'queue_id':enrollment['queue_id'],'job_id':self.job['id'],'attempt_id':self.job['attempt_id']}
        for method in ('collect_activity','collect_progress','collect_execution'):
            patcher=patch.object(JobsApp,method);patcher.start();self.addCleanup(patcher.stop)

    async def until(self, pilot, predicate):
        await waiting.until(predicate, pilot=pilot, timeout=10, what='Study view did not reach bounded expected state')

    def render(self,app,label,size):
        directory=os.environ.get('CMW_STUDY_RENDER_DIR')
        if directory:
            root=Path(directory);root.mkdir(parents=True,exist_ok=True)
            (root/f'{label}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())

    async def test_local_remote_four_sizes_catalogue_entry_readonly_and_return(self):
        before=self.store.path.read_bytes()
        with patch('cmw.jobs.study.StudyService') as factory:
            factory.return_value.view_for_job.return_value=result(self.target)
            for size in ((77,24),(80,24),(100,30),(140,40)):
                for remote in (False,True):
                    backend=StudyBackend() if remote else None
                    app=NodesApp({'MINI':backend}) if remote else JobsApp(self.store)
                    async with app.run_test(size=size) as pilot:
                        await self.until(pilot,lambda:bool(app.rows) if remote else bool(app.row_ids))
                        selected=app.selected_key if remote else app.selected_id
                        await pilot.press('t')
                        await self.until(pilot,lambda:isinstance(app.screen,CatalogueDetails) and app.screen.record is not None)
                        self.render(app,'remote-catalogue' if remote else 'local-catalogue',size)
                        region=app.screen.query_one('#catalogue-study').region
                        self.assertLessEqual(region.right,size[0]);self.assertLessEqual(region.bottom,size[1])
                        await pilot.click('#catalogue-study')
                        await self.until(pilot,lambda:isinstance(app.screen,StudyScreen) and app.screen.record is not None)
                        body=str(app.screen.query_one('#study-body',Static).render())
                        self.assertIn('NOT ASSESSED',body);self.assertIn('encut-500',body)
                        self.assertIn('explicit compare required',body)
                        self.assertGreater(app.screen.query_one('#study-scroll').size.height,5)
                        self.render(app,'remote-study' if remote else 'local-study',size)
                        for selector in ('#study-back','#study-status'):
                            region=app.screen.query_one(selector).region
                            self.assertGreaterEqual(region.y,0);self.assertLessEqual(region.bottom,size[1]);self.assertLessEqual(region.right,size[0])
                        await pilot.press('escape');self.assertIsInstance(app.screen,CatalogueDetails)
                        await pilot.press('escape');self.assertEqual(app.selected_key if remote else app.selected_id,selected)
                        await pilot.press('q')
                    if remote:
                        self.assertEqual(len(backend.study_requests),1)
                        self.assertEqual(backend.study_requests[0][1],{})
            factory.return_value.plan.assert_not_called();factory.return_value.materialize.assert_not_called()
            factory.return_value.register.assert_not_called();factory.return_value.compare.assert_not_called()
            factory.return_value.view_for_job.assert_called_with(self.job['id'],expected_attempt_id=self.job['attempt_id'])
        self.assertEqual(before,self.store.path.read_bytes())

    async def test_remote_unavailable_and_wrong_attempt_are_qualified_without_local_paths(self):
        for kind in ('unavailable','wrong'):
            backend=StudyBackend();backend.study_fail=kind=='unavailable';backend.wrong_target=kind=='wrong'
            app=NodesApp({'MINI':backend})
            async with app.run_test(size=(77,24)) as pilot:
                await self.until(pilot,lambda:bool(app.rows));await pilot.press('t')
                await self.until(pilot,lambda:isinstance(app.screen,CatalogueDetails) and app.screen.record is not None)
                with patch('cmw.jobs.study.StudyService',side_effect=AssertionError('Frontend opened owner files')):
                    await pilot.press('s')
                    await self.until(pilot,lambda:isinstance(app.screen,StudyScreen) and
                        'incomplete owner coverage' in str(app.screen.query_one('#study-status',Static).render()))
                self.assertIsNone(app.screen.record)
                self.assertIn('No comparison',str(app.screen.query_one('#study-body',Static).render()))
                self.render(app,'remote-'+kind,(77,24))
                await pilot.press('escape','escape','q')
