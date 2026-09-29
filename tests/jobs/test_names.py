"""Managed name identification with deterministic, non-executing console fixtures."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.cli import name_text, status_text
from cmw.jobs.store import Store


NAMES = (
    'HOF-PDOS-TABAPY-CuII-opt-continuation-from-J47',
    'HOF-PDOS-TABAPY-CuII-opt-continuation-from-J48',
    '繁體中文結構最佳化 / mixed Latin 長名稱 / final-B',
    '[bold] literal\nsecond\tline\x1b[31m / café e\u0301 🧪 suffix',
    'duplicate name', 'duplicate name',
)


class NameTextTests(unittest.TestCase):
    def test_status_has_full_literal_single_line_name(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state')
            job = store.add(argv=['/bin/echo', 'synthetic'], cwd=directory, name=NAMES[0])
            state = store.snapshot(now=1700000600)
            state['jobs'][0]['name'] = NAMES[3]
            rendered = status_text(state)
            row = next(line for line in rendered.splitlines() if job['display_id'] in line)
            self.assertIn(name_text(NAMES[3]), row)
            self.assertNotIn('\x1b', row)
            self.assertIn('[bold]', row)
            self.assertIn('suffix', row)
            self.assertEqual(state['jobs'][0]['name'], NAMES[3])


try:
    from rich.cells import cell_len
    from textual.widgets import DataTable, Static
    from cmw.jobs.tui import JobsApp, Inspect, table_name
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console tests require optional jobs extra')
class NameTuiTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, directory):
        store = Store(Path(directory) / 'state')
        for name in NAMES:
            store.add(argv=['/bin/echo', 'synthetic'], cwd=directory, name=name,
                      engine='Fixture', cpus=8, memory_gib=12)
        state = store.snapshot(now=1700000600)
        for index, job in enumerate(state['jobs']):
            job.update(status='Done', order=None, elapsed=600, started_at=1700000000,
                       finished_at=1700000600, reason='Synthetic exit 0', cwd='/synthetic/prepared')
        state['events'] = []
        class FixedStore:
            def snapshot(self, **kwargs):
                return deepcopy(state)
        return state, FixedStore()

    async def test_cell_aware_middle_ellipsis_is_literal_and_preserves_suffix(self):
        for name in NAMES:
            for width in (12, 23, 38):
                with self.subTest(name=name, width=width):
                    value = table_name(name, width)
                    self.assertLessEqual(cell_len(value.plain), width)
                    self.assertNotIn('\n', value.plain)
                    self.assertNotIn('\t', value.plain)
                    self.assertNotIn('\x1b', value.plain)
                    self.assertEqual(value.spans, [])
                    if cell_len(name_text(name)) > width:
                        self.assertIn('…', value.plain)
                        self.assertTrue(value.plain.endswith(name_text(name)[-4:]))
        self.assertNotEqual(table_name(NAMES[0], 23), table_name(NAMES[1], 23))
        self.assertIn('[bold]', table_name(NAMES[3], 38).plain)

    async def test_identification_at_required_widths_and_duplicate_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            state, store = self.fixture(directory)
            app = JobsApp(store, clock=lambda: 1700000600)
            with patch.object(app, 'collect_activity'):
                async with app.run_test(size=(77, 24)) as pilot:
                    await pilot.pause()
                    table = app.query_one('#table', DataTable)
                    for width, height in ((77, 24), (80, 24), (110, 32), (140, 40)):
                        await pilot.resize_terminal(width, height)
                        await pilot.pause()
                        columns = list(table.columns)
                        self.assertEqual(columns[columns.index('id') + 1], 'name')
                        self.assertIn('status', columns)
                        self.assertIn('elapsed', columns)
                        self.assertGreaterEqual(table.columns['name'].width, 30)
                        self.assertLessEqual(sum(c.width + 2 for c in table.columns.values()), table.size.width)
                        self.assertGreaterEqual(table.size.height, 5)
                        self.assertLessEqual(app.query_one('#shortcuts').region.bottom, height)
                        self.assertEqual(table.row_count, len(NAMES))
                        self.assertTrue(all(row.height == 1 for row in table.rows.values()))
                        with patch.object(table, 'clear', wraps=table.clear) as clear:
                            app.refresh_state()
                            clear.assert_not_called()
                        location = os.environ.get('CMW_JOBS_NAMES_RENDER')
                        if location:
                            Path(location + f'-history-{width}x{height}.svg').write_text(app.export_screenshot())
                    table.move_cursor(row=5)
                    app.refresh_state()
                    await pilot.pause()
                    self.assertEqual(app.selected_id, 'J6.1')
                    self.assertEqual(str(table.get_cell('J5.1', 'name')), str(table.get_cell('J6.1', 'name')))
                    await pilot.resize_terminal(80, 24)
                    await pilot.pause()
                    self.assertEqual(app.selected_id, 'J6.1')
                    table.move_cursor(row=0)
                    await pilot.pause()
                    await pilot.press('enter')
                    self.assertIsInstance(app.screen, Inspect)
                    self.assertIn(NAMES[0], str(app.screen.query_one('#body', Static).render()))
                    await pilot.press('escape')
                    await pilot.resize_terminal(42, 24)
                    await pilot.pause()
                    self.assertIn('name', table.columns)
                    self.assertEqual(table.columns['name'].width, 12)
                    self.assertGreater(table.virtual_size.width, table.size.width)
                    await pilot.press('q')

    async def test_wide_roles_usage_and_compact_details_are_consistent(self):
        from tests.jobs.test_usage_ui import machine, usage
        with tempfile.TemporaryDirectory() as directory:
            state, store = self.fixture(directory)
            state['mode'] = 'Bounded Sharing'
            state['jobs'][0].update(status='Run', finished_at=None)
            state['jobs'][1].update(status='Queue', order=1, started_at=None, finished_at=None, elapsed=None)
            state['jobs'][1]['scheduling']['role'] = 'auxiliary'
            app = JobsApp(store, clock=lambda: 1700000600)
            with patch.object(app, 'collect_activity'):
                async with app.run_test(size=(140, 40)) as pilot:
                    app.accept_activity({'machine_usage': machine(), 'job_usage': {
                        state['jobs'][0]['attempt_id']: usage(cpu_cores=7.13)}})
                    await pilot.pause()
                    table = app.query_one('#table', DataTable)
                    self.assertEqual(str(table.get_cell('J1.1', 'cpu_now')), '713%')
                    self.assertEqual(str(table.get_cell('J1.1', 'ram_now')), '18.3 GiB RSS')
                    self.assertEqual(str(table.get_cell('J2.1', 'role')), 'Auxiliary')
                    location = os.environ.get('CMW_JOBS_NAMES_RENDER')
                    if location:
                        Path(location + '-mixed-wide.svg').write_text(app.export_screenshot())
                    await pilot.resize_terminal(77, 24)
                    await pilot.pause()
                    self.assertEqual(list(table.columns), ['id', 'name', 'status', 'elapsed'])
                    self.assertIn('A', str(table.get_cell('J2.1', 'id')))
                    await pilot.press('enter')
                    detail = str(app.screen.query_one('#body', Static).render())
                    self.assertIn('713%', detail)
                    self.assertIn('18.3 GiB RSS', detail)
                    self.assertIn('CPUs requested: 8', detail)
                    await pilot.press('escape', 'q')
