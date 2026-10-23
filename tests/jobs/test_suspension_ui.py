"""Literal, asynchronous execution controls against isolated fictional attempts."""
from contextlib import redirect_stderr, redirect_stdout
import argparse
import io
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from cmw.jobs.cli import register
from cmw.jobs.store import Store
from cmw.jobs.suspension_presentation import (duration, effective_status, readiness_text,
                                               submission_text, suspension_summary)


class PresentationTests(unittest.TestCase):
    def test_status_keeps_terminal_and_unknown_authoritative(self):
        for status in ('Done', 'Fail', 'Cancelled', 'Unknown', 'Cancelling'):
            self.assertEqual(effective_status(dict(status=status, suspension={'phase': 'Suspended'})), status)
        self.assertEqual(effective_status(dict(status='Run', suspension={'phase': 'Suspended'})), 'Suspended')
        self.assertEqual(effective_status(dict(status='Run', suspension={'phase': 'Suspended', 'verification_fresh': False})), 'Suspended?')
        self.assertIn('Fresh worker verification required', suspension_summary(dict(status='Run', suspension={'phase': 'Suspended', 'verification_fresh': False})))
        self.assertEqual(duration(360001), '100:00:01')
        for missing in (None, -1, float('nan'), True):
            self.assertEqual(duration(missing), 'uncertain')

    def test_acceptance_is_distinct_from_completion_and_qualification(self):
        self.assertIn('completion is pending', submission_text(dict(result='ACCEPTED', phase='Suspending')))
        self.assertIn('has not been confirmed', submission_text(dict(result='PENDING')))
        text = readiness_text(dict(action='suspend', ready=False, reasons=['unsupported runtime']))
        self.assertIn('unsupported runtime', text)
        self.assertIn('Scientific-engine qualification: pending', text)
        self.assertIn('Physical sleep/wake qualification: pending', text)
        self.assertIn('Deadline policy: {"status": "unknown"}', text)
        self.assertNotIn('safe to close', text)
        text = suspension_summary(dict(status='Run', suspension=dict(phase='Suspended', suspended_seconds=None)))
        self.assertIn('Original attempt retained', text)
        self.assertIn('uncertain', text)


class SuspensionCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw suspension cli ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'uncreated-state')

    def call(self, *arguments):
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers())
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            try:
                args = parser.parse_args(['jobs', '--state', str(self.store.root), *arguments])
            except SystemExit as exc:
                return exc.code, output.getvalue()
            result = args.handler(args)
        return result, output.getvalue()

    def test_preview_json_and_human_are_read_only(self):
        response = dict(ready=False, supported=False, action='suspend', revision=0, phase='Run',
                        reasons=["Suspension unavailable for this attempt's runtime."], attempt_id='fixture')
        with patch('cmw.jobs.suspension.readiness', return_value=response) as inspect, \
                patch('cmw.jobs.suspension.submit', side_effect=AssertionError('preview submitted')):
            code, output = self.call('suspend', 'J1.1', '--preview', '--json')
            self.assertEqual((code, json.loads(output)), (0, response))
            self.assertEqual(inspect.call_args.args[1:], ('J1.1', 'suspend'))
            code, output = self.call('continue', 'J1.1', '--preview')
            self.assertEqual(code, 0)
            self.assertIn('UNAVAILABLE', output)
        self.assertFalse(self.store.root.exists())

    def test_explicit_action_literal_reason_and_async_result(self):
        with patch('cmw.jobs.suspension.submit', return_value=dict(result='ACCEPTED', phase='Suspending')) as submit:
            code, output = self.call('suspend', 'J1.1', '--reason', '[bold] u/c/p/x',
                                     '--expected-revision', '4', '--expected-attempt-id', 'immutable')
            self.assertEqual(code, 0)
            self.assertIn('completion is pending', output)
            self.assertEqual(submit.call_args.args[1:], ('J1.1', 'suspend'))
            self.assertEqual(submit.call_args.kwargs, dict(reason='[bold] u/c/p/x',
                             expected_attempt_id='immutable', expected_revision=4))
        self.assertFalse(self.store.root.exists())
        for option in ('--force', '--assume-stopped', '--ignore-deadline', '--pid'):
            self.assertEqual(self.call('suspend', 'J1.1', option)[0], 2)


try:
    from textual.widgets import Button, DataTable, Input, Static, TextArea
    from cmw.jobs.tui import JobsApp, NoteEditor
    from cmw.jobs.suspension_ui import SuspensionConfirm
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Optional Jobs console not installed')
class SuspensionPortalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw suspension portal ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.job = self.store.add(argv=['/unexecuted/fictional'], cwd=self.root,
                                  name='Fictional [literal] supported job', hold=True)
        self.other = self.store.add(argv=['/unexecuted/fictional'], cwd=self.root,
                                    name='Separate auxiliary fixture', hold=True)
        self.record('Run', 1)
        self.inspect = patch('cmw.jobs.suspension.readiness', side_effect=self.readiness).start()
        self.submit = patch('cmw.jobs.suspension.submit', side_effect=self.submitting).start()
        self.activity = patch.object(JobsApp, 'collect_activity').start()
        self.addCleanup(patch.stopall)

    def record(self, phase, revision):
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job.update(status='Run', order=None, worker={'fixture': 'protected-worker'},
                       suspension=dict(phase=phase, revision=revision, inhibited=phase != 'Run',
                                       suspended_seconds=61, reason='fixture reason'))
            self.store.save(con, job)

    def readiness(self, store, job_id, action):
        job = store.snapshot()['jobs'][0]
        return dict(supported=True, ready=True, reasons=[], job_id=job['id'], display_id=job['display_id'],
                    name=job['name'], attempt_id=job['attempt_id'], action=action,
                    revision=job['suspension']['revision'], phase=job['suspension']['phase'],
                    authority={'claim': 'fictional', 'worker': job['worker']}, backend='fictional-supported-v1', protocol=1,
                    computational_scope='fictional owned members; no live processes', protected_scope='worker and pinned leader',
                    deadline={'policy': 'pause-aware execution budget', 'hard_wall_deadline': 'fixed fixture limit'},
                    storage=[str(self.root)], resources={'memory_gib': 1, 'cpus': 1},
                    qualification={'engine': False, 'sleep_wake': False, 'native_mpi': 'separate gate'},
                    other_active=[{'display_id': self.other['display_id'], 'status': 'Run', 'role': 'auxiliary'}])

    def submitting(self, store, job_id, action, **kwargs):
        result = self.readiness(store, job_id, action)
        phase = 'Suspending' if action == 'suspend' else 'Resuming'
        self.record(phase, result['revision'] + 1)
        return {**result, 'result': 'ACCEPTED', 'phase': phase}

    async def open(self, app, pilot):
        await pilot.press('u')
        await pilot.pause()
        self.assertIsInstance(app.screen, SuspensionConfirm)
        return app.screen

    async def submitted(self, app, pilot, modal):
        for _ in range(80):
            if app.screen is not modal:
                return
            await pilot.pause(.05)
        self.fail('Submission did not dismiss its confirmation')

    async def test_visible_button_and_confirmation_at_all_required_sizes(self):
        for width, height in ((77, 24), (80, 24), (140, 40)):
            with self.subTest(size=(width, height)):
                app = JobsApp(self.store)
                async with app.run_test(size=(width, height)) as pilot:
                    await pilot.pause()
                    button = app.query_one('#suspension-action', Button)
                    self.assertEqual(str(button.label), '[U] Suspend job')
                    self.assertLessEqual(button.region.bottom, height)
                    self.assertGreaterEqual(button.region.y, 0)
                    shortcuts = app.query_one('#shortcuts', Static)
                    if shortcuts.display:
                        self.assertGreaterEqual(shortcuts.region.height, 2)
                        self.assertLessEqual(button.region.bottom, shortcuts.region.y)
                        self.assertLessEqual(shortcuts.region.bottom, height)
                    self.assertFalse(button.disabled)
                    self.assertIn('NAME', [str(column.label) for column in app.query_one('#table', DataTable).columns.values()])
                    if os.environ.get('CMW_SUSPENSION_UI_EVIDENCE'):
                        path = Path(os.environ['CMW_SUSPENSION_UI_EVIDENCE'])
                        path.mkdir(parents=True, exist_ok=True)
                        app.save_screenshot(f'dashboard-{width}x{height}.svg', path=path)
                    await pilot.click('#suspension-action')
                    await pilot.pause()
                    modal = app.screen
                    self.assertIsInstance(modal, SuspensionConfirm)
                    text = str(modal.query_one('#suspension-readiness', Static).render())
                    self.assertIn(self.job['attempt_id'], text)
                    self.assertIn('RAM commitment retained: 1 GiB', text)
                    self.assertIn('hard_wall_deadline', text)
                    self.assertIn(self.other['display_id'], text)
                    self.assertLessEqual(modal.query_one('#suspension-confirm').region.bottom, height)
                    if os.environ.get('CMW_SUSPENSION_UI_EVIDENCE'):
                        path = Path(os.environ['CMW_SUSPENSION_UI_EVIDENCE'])
                        path.mkdir(parents=True, exist_ok=True)
                        app.save_screenshot(f'confirmation-{width}x{height}.svg', path=path)
                    await pilot.press('escape', 'q')
        self.submit.assert_not_called()

    async def test_delayed_readiness_does_not_interrupt_note_editor(self):
        entered, release = Event(), Event()
        original = self.readiness
        def delayed(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('UI fixture readiness was not released')
            return original(*args)
        self.inspect.side_effect = delayed
        app = JobsApp(self.store)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('u')
            self.assertTrue(entered.is_set())
            await pilot.press('n', 'u')
            self.assertIsInstance(app.screen, NoteEditor)
            release.set()
            await pilot.pause()
            self.assertIsInstance(app.screen, NoteEditor)
            self.assertEqual(app.screen.query_one(TextArea).text, 'u')
            await pilot.press('escape', 'q')
        self.submit.assert_not_called()

    async def test_reason_letters_key_repeat_and_duplicate_click_do_not_toggle(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            modal = await self.open(app, pilot)
            await pilot.press('u', 'c', 'p', 'x', 'q')
            self.assertEqual(modal.query_one(Input).value, 'ucpxq')
            self.assertFalse(self.store.snapshot()['controller']['dispatch'])
            app.submit_suspension(modal, modal.query_one(Input).value)
            app.submit_suspension(modal, 'duplicate click')
            await self.submitted(app, pilot, modal)
            self.assertEqual(self.submit.call_count, 1)
            self.assertEqual(self.submit.call_args.args[2], 'suspend')
            self.assertEqual(self.submit.call_args.kwargs['reason'], 'ucpxq')
            self.assertEqual(self.submit.call_args.kwargs['expected_phase'], 'Run')
            self.assertEqual(self.submit.call_args.kwargs['expected_authority']['claim'], 'fictional')
            self.assertNotIsInstance(app.screen, SuspensionConfirm)
            self.assertTrue(app.query_one('#suspension-action', Button).disabled)
            await pilot.press('x')
            self.assertEqual(app.screen.__class__.__name__, 'Prompt')
            await pilot.press('escape', 'q')

    async def test_stale_confirmation_refuses_revision_and_selection(self):
        for change in ('revision', 'selection', 'worker', 'attempt'):
            with self.subTest(change=change):
                self.record('Run', 1)
                app = JobsApp(self.store)
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    modal = await self.open(app, pilot)
                    if change == 'revision':
                        self.record('Run', 2)
                        app.refresh_state()
                    elif change == 'selection':
                        app.selected_id = self.other['display_id']
                    else:
                        with self.store.transaction() as con:
                            job = self.store.get(con, self.job['id'])
                            job['worker' if change == 'worker' else 'attempt_id'] = {'fixture': 'changed'} if change == 'worker' else 'changed'
                            self.store.save(con, job)
                        app.refresh_state()
                    app.submit_suspension(modal, 'must refuse')
                    self.assertIn('stale', str(modal.query_one('#suspension-error', Static).render()))
                    await pilot.press('escape', 'q')
        self.submit.assert_not_called()

    async def test_suspended_continue_and_note_letters_preserve_observations(self):
        self.record('Suspended', 3)
        app = JobsApp(self.store)
        async with app.run_test(size=(77, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(str(app.query_one('#suspension-action', Button).label), '[U] Continue job')
            self.assertIn('Suspended', str(app.query_one('#selected', Static).render()))
            await pilot.press('n', 'u', 'c', 'p', 'x', 'ctrl+s')
            await pilot.pause()
            self.assertEqual(self.store.snapshot()['jobs'][0]['note'], 'ucpx')
            modal = await self.open(app, pilot)
            self.assertEqual(modal.request['action'], 'continue')
            await pilot.resize_terminal(140, 40)
            await pilot.pause()
            app.submit_suspension(modal, 'manual')
            await self.submitted(app, pilot, modal)
            self.assertEqual(self.submit.call_args.args[2], 'continue')
            self.assertEqual(self.store.snapshot()['jobs'][0]['note'], 'ucpx')
            await pilot.press('q')
        self.assertEqual(self.store.snapshot()['jobs'][0]['suspension']['phase'], 'Resuming')
