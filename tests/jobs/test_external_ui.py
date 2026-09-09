"""External presentation fixtures never scan the host process table."""
from __future__ import annotations

from copy import deepcopy
import tempfile
import asyncio
import contextlib
import io
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cmw.jobs.cli import external_detail, guard_text, safe_text, status_text
from cmw.jobs.store import Store


def projection():
    return {
        'external_activity': {
            'state': 'BUSY', 'reason': 'External VASP observed', 'source': 'client',
            'observed_at': time.time(), 'age_seconds': 0, 'stale': False,
            'scope': 'current local user / recognized accessible executables',
            'coverage': {'warnings': ['Fixture coverage']},
            'observations': [{'id': 'E101-birth-17', 'engine': 'VASP', 'pid': 101,
                              'status': 'sleeping', 'nproc': 2, 'age_seconds': 60,
                              'exe': '/fixture/[bold]vasp_std\x1b[31m', 'cwd': None,
                              'ownership': 'External / unattributed', 'evidence': ['executable identity']}],
        },
        'admission': {'permitted': False, 'reason': 'External VASP observed', 'conflict': False},
    }


class ExternalTextTests(unittest.TestCase):
    def test_human_fields_are_observations_and_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Store(Path(directory) / 'state').snapshot()
            state.update(projection())
            text = safe_text(status_text(state))
            self.assertIn('Controller: Offline', text)
            self.assertIn('Admission: BLOCKED', text)
            self.assertIn('NPROC = observed processes', text)
            self.assertIn('OS sleeping', text)
            self.assertIn('[bold]vasp_std', text)
            self.assertNotIn('\x1b', text)
            self.assertEqual(state['jobs'], [])
            self.assertFalse((Path(directory) / 'state').exists())
            detail = external_detail(state['external_activity']['observations'][0])
            self.assertIn('CPUs requested: —', detail)
            self.assertIn('Working directory: —', detail)
            self.assertIn('Scientific status: Not evaluated', detail)
            state['admission']['conflict'] = True
            self.assertIn('CONFLICT', guard_text(state))

    def test_cli_json_external_show_and_boundary(self):
        from cmw.cli import build_parser
        from cmw.jobs import activity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'state'
            observer = activity.Observer(lambda *_: projection()['external_activity'])
            def invoke(*args):
                output = io.StringIO()
                parsed = build_parser().parse_args(['jobs', '--state', str(root), *args, '--json'])
                with contextlib.redirect_stdout(output), patch.object(activity, 'DEFAULT_OBSERVER', observer):
                    code = parsed.handler(parsed)
                return code, json.loads(output.getvalue())
            code, state = invoke('status')
            self.assertEqual(code, 0)
            self.assertEqual(state['jobs'], [])
            self.assertEqual(state['external_activity']['state'], 'BUSY')
            self.assertFalse(state['admission']['permitted'])
            code, item = invoke('show', 'E101-birth-17')
            self.assertEqual(code, 0)
            self.assertEqual(item['pid'], 101)
            code, error = invoke('logs', 'E101-birth-17')
            self.assertEqual(code, 2)
            self.assertIn('read-only', error['reason'])
            self.assertFalse(root.exists())


try:
    from cmw.jobs.tui import ExternalInspect, JobsApp
    from textual.widgets import DataTable, Static
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console tests require optional jobs extra')
class ExternalTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_observation_is_independent_of_repaints(self):
        from cmw.jobs.activity import Observer
        calls = []
        def collect(*args):
            calls.append(True)
            return projection()['external_activity']
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state')
            app = JobsApp(store, observer=Observer(collect))
            async with app.run_test(size=(100, 36)) as pilot:
                async def observed():
                    while not app.activity_projection:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(observed(), timeout=2)
                self.assertEqual(app.activity_projection['external_activity']['source'], 'client')
                for _ in range(10):
                    app.refresh_state()
                self.assertEqual(len(calls), 1)
                self.assertFalse(store.snapshot()['controller']['online'])
                self.assertFalse(store.root.exists())
                await pilot.press('q')

    async def test_external_read_only_selection_resize_and_disappearance(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state')
            app = JobsApp(store)
            with patch.object(app, 'collect_activity'):
                async with app.run_test(size=(130, 40)) as pilot:
                    app.accept_activity(projection())
                    await pilot.pause()
                    table = app.query_one('#external', DataTable)
                    self.assertEqual(app.query_one('#table', DataTable).row_count, 0)
                    self.assertEqual(table.row_count, 1)
                    table.focus()
                    await pilot.pause()
                    self.assertEqual(app.external_selected_id, 'E101-birth-17')
                    await pilot.press('l', 'h', 'o', 'x')
                    self.assertIs(app.screen, app.screen_stack[0])
                    self.assertIn('read-only', str(app.query_one('#message', Static).render()))
                    await pilot.press('enter')
                    self.assertIsInstance(app.screen, ExternalInspect)
                    await pilot.press('escape')
                    await pilot.resize_terminal(64, 28)
                    await pilot.pause()
                    self.assertEqual(app.external_selected_id, 'E101-birth-17')
                    self.assertEqual(len(table.columns), 5)
                    stale = projection()
                    stale['external_activity'].update(state='UNAVAILABLE', stale=True, reason='Fixture timeout')
                    app.accept_activity(stale)
                    self.assertEqual(table.row_count, 1)
                    self.assertIn('STALE', str(app.query_one('#guard', Static).render()))
                    cleared = projection()
                    cleared['external_activity'].update(state='NO_MATCH', reason='No recognized external computation observed', observations=[])
                    app.accept_activity(cleared)
                    await pilot.pause()
                    self.assertEqual(table.row_count, 0)
                    self.assertIsNone(app.external_selected_id)
                    self.assertFalse((Path(directory) / 'state').exists())
                    await pilot.press('q')

    async def test_external_actions_do_not_modify_selected_managed_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state')
            store.add(argv=['/bin/echo', 'fixture'], cwd=directory, name='managed fixture')
            app = JobsApp(store)
            with patch.object(app, 'collect_activity'):
                async with app.run_test(size=(100, 36)) as pilot:
                    app.accept_activity(projection())
                    app.query_one('#external', DataTable).focus()
                    await pilot.pause()
                    before = deepcopy(store.snapshot()['jobs'])
                    await pilot.press('l', 'h', 'o', 'x')
                    self.assertEqual(store.snapshot()['jobs'], before)
                    app.query_one('#table', DataTable).focus()
                    await pilot.pause()
                    await pilot.press('h')
                    self.assertEqual(store.snapshot()['jobs'][0]['status'], 'Hold')
                    await pilot.press('ctrl+c')
