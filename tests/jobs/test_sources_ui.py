"""Sources inspection, explicit confirmation and bounded attempt-local workers."""
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.store import Store
from cmw.progress.models import SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import write_run

try:
    from cmw.jobs.tui import JobsApp, NoteEditor
    from cmw.jobs.sources_ui import Sources
    from cmw.jobs.history_ui import ProgressHistory
    from textual.widgets import Button, Checkbox, Input, Static, TextArea
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Optional Jobs console not installed')
class SourcesUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-sources-ui-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / 'literal 中文 $(unused); output'
        self.output.mkdir()
        write_run(self.output, relaxation=True, evaluations=2)
        self.store = Store(self.root / 'state')
        self.job = self.store.add(argv=['/bin/bash', '-e', '-o', 'pipefail', '-c', 'never execute this'],
                                  cwd=self.root, name='same continuation name', engine='VASP', hold=True)
        self.other = self.store.add(argv=['/opaque/other'], cwd=self.root, name='same continuation name',
                                    engine='VASP', hold=True)
        activity = patch.object(JobsApp, 'collect_activity')
        activity.start()
        self.addCleanup(activity.stop)

    async def settle(self, app, pilot, predicate=None):
        if predicate is None:
            predicate = lambda: app.progress_snapshot is not None
        for _ in range(300):
            await pilot.pause(.01)
            if not app.progress_busy and app.sources_request is None and predicate():
                return
        self.fail('Bounded Sources worker did not settle')

    async def open_sources(self, app, pilot):
        await pilot.press('s')
        await self.settle(app, pilot, lambda: isinstance(app.screen, Sources) and app.screen.inspection is not None)
        return app.screen

    async def configure(self, screen, pilot):
        if not screen.configuring:
            screen.query_one('#sources-edit', Button).press()
            await pilot.pause()
        screen.query_one('#sources-directory', Input).value = str(self.output)
        await pilot.pause()
        screen.query_one('#sources-propose', Button).press()
        await pilot.pause()
        self.assertEqual(screen.query_one('#source-path-0', Input).value, str(self.output / 'OUTCAR'))
        self.assertEqual(screen.query_one('#source-path-1', Input).value, str(self.output / 'OSZICAR'))
        self.assertEqual(screen.query_one('#source-path-2', Input).value, '')

    async def validate(self, app, screen, pilot):
        screen.query_one('#sources-validate', Button).press()
        await self.settle(app, pilot, lambda: screen.validation is not None)
        self.assertFalse(screen.validation.errors, screen.validation.errors)

    async def activate(self, app, screen, pilot):
        screen.query_one('#sources-use', Button).press()
        await self.settle(app, pilot, lambda: not isinstance(app.screen, Sources)
                          and app.progress_snapshot is not None)

    async def test_validate_then_activate_switch_return_and_reopen_are_read_only(self):
        before = self.store.path.read_bytes()
        original_argv = self.job['argv'][:]
        app = JobsApp(self.store)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(app.progress_snapshot.context.binding_status, 'SOURCE_UNRESOLVED')
            screen = await self.open_sources(app, pilot)
            self.assertTrue(screen.configuring)
            await self.configure(screen, pilot)
            await self.validate(app, screen, pilot)
            self.assertEqual(app.session_sources, {})
            self.assertEqual(app.progress_snapshot.context.binding_status, 'SOURCE_UNRESOLVED')
            self.assertFalse(screen.query_one('#sources-use', Button).disabled)
            await self.activate(app, screen, pilot)
            self.assertEqual(len(app.session_sources), 1)
            self.assertTrue(app.progress_snapshot.metrics)
            self.assertIn('session', app.progress_snapshot.context.association)
            self.assertEqual(app.progress_snapshot.context.job_id, self.job['id'])
            await pilot.press('down')
            await self.settle(app, pilot)
            self.assertEqual(app.progress_snapshot.context.job_id, self.other['id'])
            self.assertEqual(app.progress_snapshot.context.binding_status, 'SOURCE_UNRESOLVED')
            await pilot.press('up')
            await self.settle(app, pilot)
            self.assertEqual(app.progress_snapshot.context.job_id, self.job['id'])
            self.assertTrue(app.progress_snapshot.metrics)
            await pilot.press('q')
        reopened = JobsApp(self.store)
        async with reopened.run_test(size=(77, 24)) as pilot:
            await self.settle(reopened, pilot)
            self.assertEqual(reopened.session_sources, {})
            self.assertEqual(reopened.progress_snapshot.context.binding_status, 'SOURCE_UNRESOLVED')
            await pilot.press('q')
        self.assertEqual(self.store.path.read_bytes(), before)
        state = self.store.snapshot()
        self.assertEqual(state['jobs'][0]['argv'], original_argv)
        self.assertFalse(state['controller']['online'])
        self.assertFalse(state['controller']['dispatch'])
        self.assertTrue(all(job['status'] == 'Hold' for job in state['jobs']))

    async def test_declared_stdout_field_observes_when_oszicar_is_empty(self):
        stdout = self.output / 'explicit-stream.log'
        stdout.write_text((self.output / 'OSZICAR').read_text())
        (self.output / 'OSZICAR').write_text('')
        before = self.store.path.read_bytes()
        for include_oszicar in (True, False):
            with self.subTest(include_oszicar=include_oszicar):
                app = JobsApp(self.store)
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.settle(app, pilot)
                    screen = await self.open_sources(app, pilot)
                    await self.configure(screen, pilot)
                    roles = [plan.role for plan in screen.plans]
                    self.assertEqual(roles.count('stdout'), 1)
                    stdout_index = roles.index('stdout')
                    self.assertEqual(screen.query_one(f'#source-path-{stdout_index}', Input).value, '')
                    screen.query_one(f'#source-path-{stdout_index}', Input).value = str(stdout)
                    if not include_oszicar:
                        oszicar_index = roles.index('oszicar')
                        screen.query_one(f'#source-path-{oszicar_index}', Input).value = ''
                    await pilot.pause()
                    await self.validate(app, screen, pilot)
                    await self.activate(app, screen, pilot)
                    snapshot = app.progress_snapshot
                    self.assertTrue(any(item.source_role == 'stdout' for item in snapshot.verification))
                    self.assertTrue(any(item.latest.semantic_id == 'vasp.electronic.dE'
                                        for item in snapshot.metrics))
                    expected_roles = {'outcar', 'stdout'} | ({'oszicar'} if include_oszicar else set())
                    self.assertEqual({item.role for item in snapshot.context.sources}, expected_roles)
                    self.assertEqual(next(item.path for item in snapshot.context.sources
                                          if item.role == 'stdout'), str(stdout))
                    await pilot.press('q')
        self.assertEqual(self.store.path.read_bytes(), before)

    async def test_automatic_inspection_is_optional_and_preserves_history_s(self):
        with self.store.transaction() as con:
            job = self.store.get(con, self.job['id'])
            job['argv'] = ['/unexecuted/run-vasp.sh', '--input', str(self.output), '--output', str(self.output)]
            self.store.save(con, job)
        app = JobsApp(self.store)
        async with app.run_test(size=(77, 24)) as pilot:
            await self.settle(app, pilot)
            self.assertTrue(app.progress_snapshot.metrics)
            original = app.progress_snapshot.context.binding_id
            self.assertNotIn('Configure Sources', str(app.query_one('#progress', Static).render()))
            screen = await self.open_sources(app, pilot)
            self.assertFalse(screen.configuring)
            self.assertEqual(str(screen.query_one('#sources-validation', Static).render()), '')
            self.assertIn(str(self.output / 'OUTCAR'), str(screen.query_one('#sources-inspection', Static).render()))
            self.assertEqual(app.progress_snapshot.context.binding_id, original)
            self.assertFalse(screen.query_one('#sources-use', Button).display)
            await pilot.press('escape', 'g')
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, ProgressHistory)
            with patch.object(ProgressHistory, 'action_stage') as stage:
                await pilot.press('s')
                stage.assert_called_once()
            self.assertIsInstance(app.screen, ProgressHistory)
            self.assertEqual(app.session_sources, {})
            await pilot.press('escape', 'q')

    async def test_literal_shortcuts_and_path_edits_invalidate_validation(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            screen = await self.open_sources(app, pilot)
            path = screen.query_one('#source-path-0', Input)
            path.focus()
            await pilot.press('s', 'u', 'f', 'g', 'n', 'p', 'h', 'o', 'x', 'b', 'q')
            self.assertEqual(path.value, 'sufgnphoxbq')
            self.assertIs(app.screen, screen)
            await self.configure(screen, pilot)
            await self.validate(app, screen, pilot)
            path.value += '.changed'
            await pilot.pause()
            self.assertIsNone(screen.validation)
            self.assertTrue(screen.query_one('#sources-use', Button).disabled)
            self.assertEqual(app.session_sources, {})
            await pilot.press('escape', 'n', 's', 'q', 'u')
            self.assertIsInstance(app.screen, NoteEditor)
            self.assertEqual(app.screen.query_one(TextArea).text, 'squ')
            await pilot.press('escape', 'q')

    async def test_conflict_requires_confirmation_and_retains_qualification(self):
        other_output = self.root / 'declared elsewhere'
        other_output.mkdir()
        write_run(other_output)
        declarations = tuple(SourceDescriptor(role, str(other_output / role.upper())) for role in ('outcar', 'oszicar'))
        conflict = self.store.add(argv=['/unexecuted/run-vasp.sh', '--input', str(self.output), '--output', str(self.output)],
                                  cwd=self.root, name='conflict', engine='VASP', hold=True,
                                  progress_sources=declarations)
        app = JobsApp(self.store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('down', 'down')
            await self.settle(app, pilot)
            self.assertEqual(app.progress_snapshot.context.binding_status, 'SOURCE_CONFLICT')
            screen = await self.open_sources(app, pilot)
            body = str(screen.query_one('#sources-inspection', Static).render())
            self.assertIn(str(self.output), body)
            self.assertIn(str(other_output), body)
            await self.configure(screen, pilot)
            await self.validate(app, screen, pilot)
            self.assertTrue(screen.validation.conflict)
            self.assertTrue(screen.query_one('#sources-use', Button).disabled)
            screen.query_one('#sources-confirm', Checkbox).value = True
            await pilot.pause()
            self.assertFalse(screen.query_one('#sources-use', Button).disabled)
            await self.activate(app, screen, pilot)
            self.assertTrue(app.progress_snapshot.metrics)
            self.assertEqual(app.progress_snapshot.context.job_id, conflict['id'])
            self.assertTrue(app.progress_snapshot.context.source_conflicts)
            self.assertIn('conflict', str(app.query_one('#progress', Static).render()).lower())
            screen = await self.open_sources(app, pilot)
            screen.query_one('#sources-clear', Button).press()
            await self.settle(app, pilot, lambda: not isinstance(app.screen, Sources))
            self.assertFalse(app.session_sources)
            self.assertEqual(app.progress_snapshot.context.binding_status, 'SOURCE_CONFLICT')
            await pilot.press('q')

    async def test_database_replacement_during_read_discards_late_snapshot(self):
        started, release = threading.Event(), threading.Event()
        service = ObservationService()
        original = service.poll
        first = True
        def delayed(context):
            nonlocal first
            snapshot = original(context)
            if first:
                first = False
                started.set()
                release.wait(5)
            return snapshot
        service.poll = delayed
        app = JobsApp(self.store, progress_service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(100):
                if started.is_set():
                    break
                await pilot.pause(.01)
            self.assertTrue(started.is_set())
            replacement = self.root / 'replacement.sqlite3'
            replacement.write_bytes(self.store.path.read_bytes())
            replacement.replace(self.store.path)
            release.set()
            await self.settle(app, pilot, lambda: app.progress_snapshot is not None)
            self.assertIn('earlier source results were discarded', str(app.query_one('#message', Static).render()))
            self.assertEqual(app.progress_snapshot.context.job_id, self.job['id'])
            self.assertFalse(app.session_sources)
            await pilot.press('q')

    async def test_delayed_validation_uses_existing_slot_and_back_discards_result(self):
        from cmw.jobs.source_selection import validate_selection
        started, release = threading.Event(), threading.Event()
        service = ObservationService()
        service.poll = Mock(wraps=service.poll)
        def delayed(*args, **kwargs):
            started.set()
            release.wait(5)
            return validate_selection(*args, **kwargs)
        app = JobsApp(self.store, progress_service=service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            screen = await self.open_sources(app, pilot)
            await self.configure(screen, pilot)
            with patch('cmw.jobs.source_selection.validate_selection', side_effect=delayed):
                screen.query_one('#sources-validate', Button).press()
                for _ in range(100):
                    if started.is_set():
                        break
                    await pilot.pause(.01)
                self.assertTrue(started.is_set())
                reads = service.poll.call_count
                await pilot.press('escape', 'down')
                for _ in range(5):
                    app.collect_progress()
                self.assertEqual(service.poll.call_count, reads)
                self.assertTrue(app.progress_busy)
                release.set()
                await self.settle(app, pilot, lambda: app.progress_snapshot is not None)
            self.assertFalse(app.session_sources)
            self.assertEqual(app.progress_snapshot.context.job_id, self.other['id'])
            self.assertEqual(app.progress_snapshot.context.binding_status, 'SOURCE_UNRESOLVED')
            await pilot.press('q')


if __name__ == '__main__':
    unittest.main()
