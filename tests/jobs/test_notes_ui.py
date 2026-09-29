"""Plain annotation editing through real key paths, without host observation."""
from contextlib import redirect_stdout, redirect_stderr
import argparse
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.cli import register
from cmw.jobs.store import Store


class NoteCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-note-cli-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')

    def call(self, *arguments):
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers())
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            try:
                args = parser.parse_args(['jobs', '--state', str(self.store.root), *arguments])
            except SystemExit as exc:
                return exc.code, output.getvalue()
            with patch('cmw.jobs.activity.project', lambda store: store.snapshot()):
                code = args.handler(args)
        return code, output.getvalue()

    def test_add_replace_clear_revision_and_full_json(self):
        note = '繁體中文 [bold] q\nsecond\tline'
        code, output = self.call('add', '--name', 'Synthetic note test', '--cwd', str(self.root),
                                 '--hold', '--note', note, '--json', '--', '/bin/echo', 'not launched')
        self.assertEqual(code, 0, output)
        job = json.loads(output)
        self.assertEqual(job['note'], note)
        code, output = self.call('annotate', job['display_id'], '--note', 'replacement',
                                 '--expected-revision', str(job['note_revision']), '--json')
        self.assertEqual(code, 0, output)
        current = json.loads(output)
        self.assertGreater(current['note_revision'], job['note_revision'])
        self.assertEqual(self.call('annotate', job['display_id'], '--note', 'stale',
                                  '--expected-revision', str(job['note_revision']), '--json')[0], 2)
        for operation in ('status', 'show'):
            arguments = (operation, '--json') if operation=='status' else (operation, job['display_id'], '--json')
            code, output = self.call(*arguments)
            result = json.loads(output)
            self.assertEqual(result['jobs'][0]['note'] if operation=='status' else result['note'], 'replacement')
        self.assertEqual(self.call('annotate', job['display_id'], '--clear-note', '--json')[0], 0)
        self.assertEqual(self.store.snapshot()['jobs'][0]['note'], '')
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])

    def test_edit_is_explicit_and_missing_or_external_targets_create_nothing(self):
        for arguments in [('annotate','J1.1'), ('annotate','J1.1','--note','x','--clear-note'),
                          ('annotate','J1.1','--clear-note'), ('annotate','E123','--note','x')]:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.call(*arguments)[0], 2)
                self.assertFalse(self.store.root.exists())


try:
    from textual.widgets import TextArea, Static, Button
    from cmw.jobs.tui import JobsApp, NoteEditor, Inspect
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console tests require the optional Jobs extra')
class NoteEditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-note-editor-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.job = self.store.add(argv=['/bin/echo','not launched'], cwd=self.root, name='Duplicate 名稱', hold=True)
        self.other = self.store.add(argv=['/bin/echo','not launched'], cwd=self.root, name='Duplicate 名稱', hold=True)
        observation = patch.object(JobsApp, 'collect_activity')
        observation.start()
        self.addCleanup(observation.stop)

    async def test_edit_keys_save_cancel_and_full_details(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(77,24)) as pilot:
            await pilot.pause()
            await pilot.press('n')
            self.assertIsInstance(app.screen, NoteEditor)
            await pilot.press('q','p','h','x','o','l','b','n','enter','a')
            editor = app.screen
            self.assertEqual(editor.query_one(TextArea).text, 'qphxolbn\na')
            self.assertFalse(self.store.snapshot()['controller']['dispatch'])
            self.assertEqual([j['status'] for j in self.store.snapshot()['jobs']], ['Hold','Hold'])
            await pilot.press('ctrl+s')
            await pilot.pause()
            self.assertNotIsInstance(app.screen, NoteEditor)
            self.assertEqual(self.store.snapshot()['jobs'][0]['note'], 'qphxolbn\na')
            self.assertIn('Note:', str(app.query_one('#selected', Static).render()))
            await pilot.press('enter')
            self.assertIsInstance(app.screen, Inspect)
            self.assertIn('qphxolbn\na', str(app.screen.query_one('#body', Static).render()))
            await pilot.press('escape','n','z','escape')
            self.assertEqual(self.store.snapshot()['jobs'][0]['note'], 'qphxolbn\na')
            await pilot.press('q')

    async def test_refresh_resize_status_and_label_changes_keep_editor_target(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(140,40)) as pilot:
            await pilot.pause()
            await pilot.press('n','q','enter','p')
            editor = app.screen
            area = editor.query_one(TextArea)
            text, cursor = area.text, area.cursor_location
            self.store.rename_id(self.job['id'], 'M1.1')
            with self.store.transaction() as con:
                job = self.store.get(con, self.job['id'])
                job.update(status='Done', order=None, finished_at=100)
                self.store.save(con, job)
            app.refresh_state()
            await pilot.resize_terminal(80,24)
            await pilot.pause()
            self.assertIs(app.screen, editor)
            self.assertEqual((area.text, area.cursor_location), (text, cursor))
            app.selected_id = self.other['display_id']
            await pilot.click('#note-save')
            await pilot.pause()
            jobs = self.store.snapshot()['jobs']
            self.assertEqual((jobs[0]['display_id'],jobs[0]['status'],jobs[0]['note']), ('M1.1','Done',text))
            self.assertEqual(jobs[1]['note'], '')
            await pilot.press('q')

    async def test_conflict_retains_buffer_and_explicit_reload_then_save(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause()
            await pilot.press('n','q')
            editor = app.screen
            self.store.annotate(self.job['id'], 'other editor')
            await pilot.press('ctrl+s')
            self.assertIs(app.screen, editor)
            self.assertEqual(editor.query_one(TextArea).text, 'q')
            self.assertIn('reload', str(editor.query_one('#note-error', Static).render()).lower())
            await pilot.press('shift+tab', 'shift+tab')
            self.assertEqual(editor.focused.id, 'note-reload')
            await pilot.press('enter')
            self.assertEqual(editor.query_one(TextArea).text, 'other editor')
            await pilot.press('end','!','ctrl+s')
            self.assertEqual(self.store.snapshot()['jobs'][0]['note'], 'other editor!')
            await pilot.press('q')

    async def test_validation_and_missing_target_keep_unsaved_buffer(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause()
            await pilot.press('n')
            editor = app.screen
            area = editor.query_one(TextArea)
            area.load_text('x'*4097)
            await pilot.press('ctrl+s')
            self.assertIs(app.screen, editor)
            self.assertEqual(area.text, 'x'*4097)
            self.assertIn('4096', str(editor.query_one('#note-error', Static).render()))
            area.load_text('keep me')
            with self.store.transaction() as con:
                con.execute('DELETE FROM jobs WHERE id=?',(self.job['id'],))
            await pilot.press('ctrl+s')
            self.assertIs(app.screen, editor)
            self.assertEqual(area.text, 'keep me')
            await pilot.press('escape','q')

    async def test_external_refusal_and_ctrl_c_detaches_without_save(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause()
            app.external_selected_id = 'E123'
            await pilot.press('n')
            self.assertNotIsInstance(app.screen, NoteEditor)
            self.assertIn('read-only', str(app.query_one('#message', Static).render()))
            app.external_selected_id = None
            await pilot.press('n','q','ctrl+c')
        self.assertEqual(self.store.snapshot()['jobs'][0]['note'], '')
        self.assertEqual(self.store.snapshot()['jobs'][0]['status'], 'Hold')

    async def test_permission_and_changed_attempt_errors_retain_buffer(self):
        app = JobsApp(self.store)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause()
            await pilot.press('n','q')
            editor = app.screen
            area = editor.query_one(TextArea)
            # Two independent refusals reuse Reload; skip its cosmetic click debounce.
            editor.query_one('#note-reload', Button).active_effect_duration = 0
            with patch.object(self.store, 'annotate', side_effect=PermissionError('Synthetic permission refusal')):
                await pilot.press('ctrl+s')
            self.assertIs(app.screen, editor)
            self.assertEqual(area.text, 'q')
            self.assertIn('permission', str(editor.query_one('#note-error', Static).render()))
            with patch('cmw.jobs.store.sqlite3.connect', side_effect=sqlite3.OperationalError('Synthetic read permission refusal')):
                app.refresh_state()
                await pilot.click('#note-reload')
                self.assertIs(app.screen, editor)
                self.assertEqual(area.text, 'q')
                self.assertIn('permission', str(editor.query_one('#note-error', Static).render()))
            with self.store.transaction() as con:
                job = self.store.get(con, self.job['id'])
                job['attempt_id'] = 'different-synthetic-attempt'
                self.store.save(con, job)
            await pilot.press('ctrl+s')
            self.assertIs(app.screen, editor)
            self.assertEqual(area.text, 'q')
            await pilot.click('#note-reload')
            self.assertEqual(area.text, 'q')
            self.assertIn('original job/attempt', str(editor.query_one('#note-error', Static).render()))
            await pilot.press('escape','q')
