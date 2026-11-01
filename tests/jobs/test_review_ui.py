"""Selected-only local and node review views, using real Textual rendering."""
from dataclasses import replace
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.review import ReviewService
from cmw.jobs.review_models import DOMAIN_NAMES, ReviewDomain, ReviewRecord, cached_review
from cmw.jobs.snapshot import capture_snapshot, render_snapshot
from cmw.jobs.store import Store
from cmw.nodes.codec import encode_typed

try:
    from textual.widgets import Button, DataTable, Input, Static, TextArea
    from cmw.jobs.review_ui import ReviewScreen
    from cmw.jobs.tui import JobsApp, Inspect
    from cmw.jobs.copy_ui import FrozenText, CopyPreview
    from cmw.nodes.portal import NodesApp
    from tests.nodes.test_portal import FakeBackend
except ImportError:
    JobsApp = None


def review(job, node_id=None, queue_id=None):
    return ReviewRecord(1,node_id,queue_id,job['id'],job['attempt_id'],'','PREVIEW',job['engine'],112.,
        tuple(ReviewDomain(name,'NOT ASSESSED','Synthetic metadata; explicit inspection required') for name in DOMAIN_NAMES),
        'metadata only',64*1024*1024,30,'[]','{}','{}','No scientific files read.')


class FakeReview:
    def __init__(self, job):
        self.record = review(job)
        self.calls, self.complete = [], False

    def preview(self, *args, **kwargs):
        self.calls.append(('preview',{}))
        return cached_review(self.record)

    def start(self, job_id, options, request_id, **kwargs):
        self.calls.append(('start',options))
        self.record = replace(self.record,review_id=uuid4().hex,status='PENDING',message='Synthetic observer pending')
        return self.record

    def poll(self, *args, **kwargs):
        self.calls.append(('poll',{}))
        if self.complete:
            self.record = replace(self.record,status='COMPLETE',coverage='complete synthetic inspection',
                domains=tuple(ReviewDomain(name,'PASS' if name == 'operational' else 'NOT ASSESSED','Synthetic bounded evidence') for name in DOMAIN_NAMES))
        return self.record

    def cancel(self, *args, **kwargs):
        self.calls.append(('cancel',{}))
        self.record = replace(self.record,status='CANCELLED',message='Observer cancelled; job untouched')
        return self.record

    def close(self):
        self.calls.append(('close',{}))


if JobsApp is not None:
    class ReviewBackend(FakeBackend):
        def __init__(self):
            super().__init__('MINI',remote=True)
            for job in self.jobs:
                job['resources'].update(mpi_ranks=8,threads_per_rank=1)
                job['reason'] = ''
            self.review_service = FakeReview(self.jobs[0])
            self.review_service.record = review(self.jobs[0],self.node_id,self.queue_id)

        def call(self, operation, params=None, target=None, **kwargs):
            if not operation.startswith('review.'):
                return super().call(operation,params,target,**kwargs)
            self.calls.append((operation,params,target))
            if self.fail:
                from cmw.nodes.protocol import NodeError
                raise NodeError('DISCONNECTED','Synthetic review channel disconnected')
            method = operation.split('.')[1]
            if method == 'start':
                record = self.review_service.start(target['job_id'],params['options'],uuid4().hex)
            else:
                record = getattr(self.review_service,method)()
            # The fixture's normal envelope keeps the endpoint-local clock and target.
            value = super().call('observe',{},target)
            value['result'] = {'review':encode_typed(record)}
            return value


@unittest.skipIf(JobsApp is None,'Optional Jobs UI extra unavailable')
class ReviewUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cmw-review-ui-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root/'queue')
        self.job = self.store.add(argv=['/never/executed'],cwd=self.root,name='Synthetic selected review',hold=True)
        for method in ('collect_activity','collect_progress','collect_execution'):
            patched = patch.object(JobsApp,method)
            patched.start(); self.addCleanup(patched.stop)

    async def until(self,pilot,predicate):
        for _ in range(100):
            if predicate():
                return
            await pilot.pause(.04)
        self.fail('Review UI did not reach the expected bounded state')

    def render(self,app,label,size):
        destination = os.environ.get('CMW_REVIEW_RENDER_DIR')
        if destination:
            path = Path(destination)
            path.mkdir(parents=True,exist_ok=True)
            (path/f'{label}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())

    def assert_layout(self,screen,size):
        self.assertGreaterEqual(screen.query_one('#review-scroll').size.height,7)
        summary = str(screen.query_one('#review-body',Static).render()).splitlines()[:7]
        self.assertEqual([line.split(':')[0] for line in summary],[name.replace('_',' ').upper() for name in DOMAIN_NAMES])
        for name in ('review-policy','review-spec','review-export','review-inspect','review-cancel','review-copy','review-back'):
            widget = screen.query_one('#'+name)
            self.assertGreaterEqual(widget.region.x,0)
            self.assertLessEqual(widget.region.right,size[0])
            self.assertGreater(widget.region.y,0)
            self.assertLessEqual(widget.region.bottom,size[1])

    async def test_local_preview_renders_four_sizes_without_inspection_or_queue_mutation(self):
        before = self.store.path.read_bytes()
        for size in ((77,24),(80,24),(100,30),(140,40)):
            app = JobsApp(self.store)
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                selected = app.selected_id
                with patch.object(ReviewService,'start',side_effect=AssertionError('Opening review inspected science')):
                    await pilot.press('r')
                    await self.until(pilot,lambda:isinstance(app.screen,ReviewScreen) and app.screen.record is not None and not app.screen.busy)
                    self.assertEqual(app.screen.record.status,'PREVIEW')
                    self.assert_layout(app.screen,size)
                    self.render(app,'local-preview',size)
                    await pilot.press('escape')
                    self.assertEqual(app.selected_id,selected)
                    await pilot.press('r')
                    await self.until(pilot,lambda:isinstance(app.screen,ReviewScreen) and not app.screen.busy)
                    await pilot.press('escape')
                await pilot.press('q')
        self.assertEqual(self.store.path.read_bytes(),before)

    async def test_remote_preview_renders_four_sizes_and_never_opens_owner_paths(self):
        for size in ((77,24),(80,24),(100,30),(140,40)):
            backend = ReviewBackend()
            app = NodesApp({'MINI':backend})
            async with app.run_test(size=size) as pilot:
                await self.until(pilot,lambda:bool(app.rows))
                selected = app.selected_key
                await pilot.press('r')
                await self.until(pilot,lambda:isinstance(app.screen,ReviewScreen) and app.screen.record is not None and not app.screen.busy)
                self.assertEqual(app.screen.record.node_id,backend.node_id)
                self.assert_layout(app.screen,size)
                self.render(app,'remote-preview',size)
                await pilot.press('escape')
                self.assertEqual(app.selected_key,selected)
                self.assertEqual([op for op,_,_ in backend.calls if op.startswith('review.')],['review.preview'])
                await pilot.press('q')
            self.assertFalse(Path('/Volumes/SameSpelled/remote-only').exists())

    async def test_explicit_inspect_cancel_and_cached_details_copy_agree(self):
        app = JobsApp(self.store)
        fake = FakeReview(self.job)
        app.review_service = fake
        before = self.store.path.read_bytes()
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.press('r')
            await self.until(pilot,lambda:isinstance(app.screen,ReviewScreen) and not app.screen.busy)
            view = app.screen
            await pilot.press('i')
            await self.until(pilot,lambda:view.record.status=='PENDING' and not view.busy)
            await pilot.press('c')
            await self.until(pilot,lambda:view.record.status=='CANCELLED' and not view.busy)
            self.assertTrue(any(name=='cancel' for name,_ in fake.calls))
            view.query_one('#review-policy',Input).value = '/owner/policy.json'
            view.query_one('#review-spec',Input).value = '/owner/spec.json'
            view.query_one('#review-export',Input).value = '/owner/reviews/new-review.json'
            fake.complete = True
            await pilot.click('#review-inspect')
            await self.until(pilot,lambda:view.record.status=='COMPLETE' and not view.busy)
            settings = [opts for name,opts in fake.calls if name=='start'][-1]
            self.assertEqual(settings['export_path'],'/owner/reviews/new-review.json')
            self.assertEqual(settings['policy_path'],'/owner/policy.json')
            await pilot.click('#review-copy')
            self.assertIsInstance(app.screen,FrozenText)
            self.assertIn('OPERATIONAL: PASS',app.screen.query_one(TextArea).text)
            await pilot.press('escape','escape')
            record = app.selected_review(self.job)
            self.assertEqual(record.status,'CACHED')
            self.assertEqual(record.domains[0].status,'UNKNOWN')
            await pilot.press('enter')
            self.assertIsInstance(app.screen,Inspect)
            self.assertIn('OPERATIONAL: UNKNOWN',str(app.screen.query_one('#body',Static).render()))
            await pilot.press('escape')
            copied = app.capture_copy_snapshot().to_dict()['result_review']
            self.assertEqual(copied,json.loads(json.dumps(record.to_dict())))
            self.assertIn('OPERATIONAL: UNKNOWN',render_snapshot(app.capture_copy_snapshot()))
            await pilot.press('r')
            await self.until(pilot,lambda:isinstance(app.screen,ReviewScreen) and not app.screen.busy)
            self.assertEqual(len([name for name,_ in fake.calls if name=='start']),2)
            await pilot.press('escape','q')
        self.assertEqual(self.store.path.read_bytes(),before)

    async def test_remote_result_copy_and_details_agree_and_disconnect_stays_pending(self):
        backend = ReviewBackend()
        app = NodesApp({'MINI':backend})
        async with app.run_test(size=(100,30)) as pilot:
            await self.until(pilot,lambda:bool(app.rows))
            await pilot.press('r')
            await self.until(pilot,lambda:isinstance(app.screen,ReviewScreen) and app.screen.record is not None and not app.screen.busy)
            view = app.screen
            await pilot.press('i')
            await self.until(pilot,lambda:view.record.status=='PENDING' and not view.busy)
            pending = view.record
            backend.fail = True
            await self.until(pilot,lambda:view.unavailable)
            self.assertEqual(view.record,pending)
            self.assertIn('cached observation retained',str(view.query_one('#review-status',Static).render()))
            backend.fail = False
            backend.review_service.complete = True
            view.unavailable = False
            await self.until(pilot,lambda:view.record.status=='COMPLETE' and not view.busy)
            await pilot.press('escape','enter')
            self.assertIsInstance(app.screen,FrozenText)
            self.assertIn('OPERATIONAL: UNKNOWN',app.screen.query_one(TextArea).text)
            await pilot.press('escape')
            copied = app.capture_copy_snapshot().to_dict()['result_review']
            owner, job = app.selected()
            self.assertEqual(copied,json.loads(json.dumps(app.selected_review(owner,job).to_dict())))
            await pilot.press('q')
        self.assertEqual(backend.jobs[0]['status'],'Run')


class ReviewSnapshotTests(unittest.TestCase):
    def test_frozen_review_rejects_other_attempt_and_redacts_owner_paths(self):
        job = {'id':1,'attempt_id':uuid4().hex,'engine':'VASP','status':'Done'}
        value = review(job)
        with self.assertRaises(ValueError):
            capture_snapshot(job,review=replace(value,attempt_id=uuid4().hex))
        value = replace(value,provenance_json='{"state_directory":"/owner/private/queue"}')
        captured = capture_snapshot(job,review=value)
        self.assertNotIn('/owner/private/queue',render_snapshot(captured,mode='json',redact_paths=True))
        self.assertIn('not assessed',captured.to_dict()['scientific_acceptance'])


if __name__ == '__main__':
    unittest.main()
