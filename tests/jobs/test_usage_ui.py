"""Deterministic presentation: no CPU load or research-process observations."""
from copy import deepcopy
import io
import contextlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from cmw.jobs.cli import machine_text, usage_cells, usage_detail
from cmw.jobs.store import Store
from tests.jobs.test_external_ui import projection


def usage(**updates):
    value = dict(cpu_cores=7.8, rss_bytes=18.3 * 2**30, cpu_quality='fresh', memory_quality='fresh',
                 cpu_members=8, memory_members=8, total_members=8, observed_at=time.time(),
                 age_seconds=0, stale=False, reason='Synthetic measurement')
    value.update(updates)
    return value


def machine():
    return dict(cpu_percent=61.2, ram_used_bytes=23.8 * 2**30, ram_total_bytes=36 * 2**30,
                cpu_quality='fresh', memory_quality='fresh', observed_at=time.time(), age_seconds=0,
                stale=False, reason='Synthetic machine')


class UsageFormatTests(unittest.TestCase):
    def test_units_quality_zero_and_large_values(self):
        self.assertEqual(usage_cells(usage()), ('780%', '18.3 GiB RSS'))
        self.assertEqual(usage_cells(usage(cpu_cores=7.1))[0], '710%')
        self.assertEqual(usage_cells(usage(cpu_cores=0.1))[0], '10%')
        self.assertEqual(usage_cells(usage(cpu_cores=128.7))[0], '12870%')
        self.assertEqual(usage_cells(usage(cpu_cores=0))[0], '0%')
        self.assertEqual(usage_cells(usage(cpu_cores=None, cpu_quality='warming-up'))[0], '…')
        self.assertEqual(usage_cells(None), ('—', '—'))
        self.assertEqual(usage_cells(usage(memory_quality='partial'))[1], '18.3 GiB RSS*')
        self.assertEqual(usage_cells(usage(stale=True))[0], '780%~')
        self.assertIn('CPU 61%', machine_text(machine()))
        self.assertIn('23.8 / 36.0 GiB', machine_text(machine()))
        text = usage_detail(usage(memory_quality='partial', memory_members=6))
        self.assertIn('6/8 members', text)
        self.assertIn('shared pages', text)
        self.assertIn('100% = one logical CPU', text)

    def test_show_managed_uses_additive_projection(self):
        from cmw.cli import build_parser
        from cmw.jobs import activity
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state')
            row = store.add(argv=['/bin/echo', 'fixture'], cwd=directory, name='fixture', cpus=8, memory_gib=20)
            state = store.snapshot()
            state['jobs'][0].update(status='Run', usage=usage())
            state['machine_usage'] = machine()
            args = build_parser().parse_args(['jobs', '--state', str(store.root), 'show', row['display_id'], '--json'])
            buffer = io.StringIO()
            with patch.object(activity, 'project', return_value=state) as projected, contextlib.redirect_stdout(buffer):
                self.assertEqual(args.handler(args), 0)
            projected.assert_called_once()
            result = json.loads(buffer.getvalue())
            self.assertEqual(result['usage']['cpu_cores'], 7.8)
            self.assertEqual(result['resources']['cpus'], 8)
            self.assertEqual(result['resources']['memory_gib'], 20)


try:
    from cmw.jobs.tui import JobsApp, Inspect, ExternalInspect
    from textual.widgets import DataTable, Static
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console tests require optional jobs extra')
class UsageTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_cached_columns_details_stale_resize_and_terminal_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state')
            row = store.add(argv=['/bin/echo', 'fixture'], cwd=directory, name='fixture', cpus=8, memory_gib=20)
            with store.transaction() as con:
                managed = store.get(con, row['id'])
                managed.update(status='Run', order=None, started_at=time.time())
                store.save(con, managed)
            state = projection()
            state['machine_usage'] = machine()
            state['job_usage'] = {row['attempt_id']: usage(cpu_cores=0.1)}
            state['external_activity']['observations'][0]['usage'] = usage(memory_quality='partial', memory_members=6)
            app = JobsApp(store)
            with patch.object(app, 'collect_activity'):
                async with app.run_test(size=(130, 40)) as pilot:
                    app.accept_activity(state)
                    await pilot.pause()
                    managed_table = app.query_one('#table', DataTable)
                    external_table = app.query_one('#external', DataTable)
                    self.assertEqual(str(managed_table.get_cell(row['display_id'], 'cpu_now')), '10%')
                    self.assertEqual(str(external_table.get_cell('E101-birth-17', 'ram_now')), '18.3 GiB RSS*')
                    self.assertIn('CPU 61%', str(app.query_one('#machine', Static).render()))
                    await pilot.press('enter')
                    self.assertIsInstance(app.screen, Inspect)
                    self.assertIn('CPU NOW: 10%', str(app.screen.query_one('#body', Static).render()))
                    await pilot.press('escape')
                    external_table.focus()
                    await pilot.pause()
                    await pilot.press('enter')
                    self.assertIsInstance(app.screen, ExternalInspect)
                    self.assertIn('6/8 members', str(app.screen.query_one('#external-body', Static).render()))
                    # Details consume new cached values rather than freezing at open.
                    warmed = deepcopy(state)
                    warmed['external_activity']['observations'][0]['usage'] = usage(cpu_cores=None, cpu_quality='warming-up')
                    app.accept_activity(warmed)
                    app.screen.refresh_content()
                    self.assertIn('warming-up', str(app.screen.query_one('#external-body', Static).render()))
                    await pilot.press('escape')
                    await pilot.resize_terminal(64, 28)
                    await pilot.pause()
                    self.assertEqual(app.external_selected_id, 'E101-birth-17')
                    self.assertEqual(list(external_table.columns), ['id', 'engine', 'cpu_now', 'ram_now', 'age'])
                    self.assertGreaterEqual(app.query_one('#controller').region.y, 0)
                    self.assertLessEqual(app.query_one('#shortcuts').region.bottom, 28)
                    stale = deepcopy(state)
                    stale['external_activity']['observations'][0]['usage']['observed_at'] = time.time() - 10
                    app.accept_activity(stale)
                    self.assertEqual(str(external_table.get_cell('E101-birth-17', 'cpu_now')), '780%~')
                    with store.transaction() as con:
                        managed = store.get(con, row['id'])
                        managed.update(status='Done', finished_at=time.time())
                        store.save(con, managed)
                    app.refresh_state()
                    self.assertIsNone(app.current_job(row['display_id'])['usage'])
                    self.assertNotIn(row['attempt_id'], app.job_usage)
                    self.assertEqual(str(managed_table.get_cell(row['display_id'], 'cpu_now')), '—')
                    self.assertEqual(app.current_job(row['display_id'])['resources']['cpus'], 8)
                    before = store.snapshot()['events']
                    for _ in range(5):
                        app.refresh_state()
                    self.assertEqual(store.snapshot()['events'], before)
                    await pilot.press('q')
