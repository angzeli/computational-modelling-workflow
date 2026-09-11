"""Sharing commands and deterministic console fixtures; no scientific execution."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from cmw.cli import build_parser
from cmw.jobs import activity, sharing
from cmw.jobs.cli import sharing_text, usage_cells
from cmw.jobs.store import Store
from tests.jobs.test_usage_ui import machine, usage


@contextmanager
def host_capacity():
    with patch('psutil.cpu_count', return_value=16), patch('psutil.virtual_memory', return_value=SimpleNamespace(total=64 * 2**30)):
        yield


def external(directory):
    owner = {'pid': 101, 'birth': 123, 'boot': 1, 'host': 'fixture'}
    member = {'identity': owner, 'exe': '/fixture/vasp_std', 'launcher': None,
              'pgid': 101, 'sid': 101, 'cwd': directory}
    item = {'id': 'Eabc123', 'engine': 'VASP', 'identity': owner, 'pid': 101, 'members': [member],
            'exe': '/fixture/vasp_std', 'status': 'sleeping', 'cwd': directory, 'nproc': 1,
            'age_seconds': 20, 'ownership': 'External / unattributed', 'evidence': ['Fixture identities'],
            'usage': usage(cpu_cores=7.13)}
    return {'state': 'BUSY', 'reason': 'Recognized external computation observed', 'observations': [item],
            'observed_at': time.time(), 'age_seconds': 0, 'stale': False, 'source': 'client',
            'scope': 'fixture only', 'coverage': {'complete': True, 'warnings': []}}


class SharingCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-sharing-cli-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = Store(self.directory / 'state')

    def invoke(self, *arguments):
        args = build_parser().parse_args(['jobs', '--state', str(self.store.root), *arguments])
        output = io.StringIO()
        with redirect_stdout(output):
            code = args.handler(args)
        return code, json.loads(output.getvalue())

    def test_config_and_declarations_never_enable_dispatch(self):
        code, config = self.invoke('config', '--json')
        self.assertEqual((code, config['mode']), (0, 'sequential'))
        self.assertFalse(self.store.root.exists())
        code, error = self.invoke('config', '--mode', 'bounded-sharing', '--json')
        self.assertEqual(code, 2)
        self.assertIn('four explicit', error['reason'])
        with host_capacity():
            code, policy = self.invoke('config', '--mode', 'bounded-sharing', '--cpu-budget', '10',
                '--memory-budget-gib', '24', '--cpu-reserve', '1', '--min-available-gib', '4', '--json')
        self.assertEqual((code, policy['mode']), (0, 'bounded-sharing'))
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])
        code, row = self.invoke('add', '--name', 'prepared analysis', '--cwd', str(self.directory),
            '--role', 'auxiliary', '--cpus', '1', '--memory-gib', '1', '--independent', '--trust-resources',
            '--write-scope', str(self.directory), '--json', '--', '/bin/echo', 'synthetic')
        self.assertEqual(code, 0)
        self.assertEqual(row['scheduling']['resource_contract'], 'trusted-declared')
        self.assertEqual(row['scheduling']['role'], 'auxiliary')
        code, primary = self.invoke('add', '--name', 'primary', '--cwd', str(self.directory), '--allow-auxiliary',
                                   '--cpus', '8', '--memory-gib', '8', '--json', '--', '/bin/echo', 'synthetic')
        self.assertEqual(code, 0)
        code, changed = self.invoke('sharing', primary['display_id'], '--revoke-auxiliary', '--json')
        self.assertEqual(code, 0)
        self.assertFalse(changed['scheduling']['allow_auxiliary'])
        code, policy = self.invoke('config', '--mode', 'sequential', '--json')
        self.assertEqual((code, policy['mode']), (0, 'sequential'))
        self.assertEqual(policy['cpu_budget'], 10)
        self.assertFalse(self.store.snapshot()['controller']['dispatch'])

    def test_external_reservation_is_metadata_and_stays_busy(self):
        guard = external(str(self.directory))
        state = self.store.snapshot()
        state['external_activity'] = guard
        with patch.object(activity, 'project', return_value=state):
            code, result = self.invoke('reserve', 'Eabc123', '--cpus', '8', '--memory-gib', '8', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(result['external_reservation']['source'], 'user supplied')
        self.assertEqual(guard['state'], 'BUSY')
        self.assertEqual(self.store.snapshot()['jobs'], [])
        self.assertFalse(self.store.snapshot()['controller']['online'])
        state = self.store.snapshot()
        state['external_activity'] = guard
        state['admission'] = {'external_reservation_state': 'valid', 'reason': 'Fixture'}
        with patch.object(activity, 'project', return_value=state):
            code, observed = self.invoke('show', 'Eabc123', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(observed['scheduling_context']['reservation_state'], 'valid')
        self.assertEqual(observed['ownership'], 'External / unattributed')
        code, result = self.invoke('unreserve', '--json')
        self.assertEqual(code, 0)
        self.assertIsNone(result['external_reservation'])


try:
    from cmw.jobs.tui import JobsApp, SharingInspect
    from textual.widgets import DataTable, Static
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Console tests require optional jobs extra')
class SharingTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_roles_external_reservation_and_narrow_scheduling_details(self):
        with tempfile.TemporaryDirectory(prefix='cmw-sharing-render-') as directory:
            base = Path(directory)
            for name in ('primary', 'auxiliary'):
                (base / name).mkdir()
            store = Store(base / 'state')
            with host_capacity():
                store.configure_sharing('bounded-sharing', cpu_budget=10, memory_gib=24, cpu_reserve=1, min_available_gib=4)
            primary = store.add(argv=['/bin/echo', 'fixture'], cwd=base/'primary', name='primary', cpus=8, memory_gib=8, allow_auxiliary=True)
            auxiliary = store.add(argv=['/bin/echo', 'fixture'], cwd=base/'auxiliary', name='auxiliary', cpus=1, memory_gib=1,
                                  role='auxiliary', independent=True, resource_contract='trusted-declared')
            with store.transaction() as con:
                for row in (primary, auxiliary):
                    row = store.get(con, row['id'])
                    row.update(status='Run', order=None, started_at=time.time())
                    if row['id'] == auxiliary['id']:
                        row['sharing_anchor'] = {'kind': 'managed', 'id': primary['display_id'], 'attempt_id': primary['attempt_id']}
                    store.save(con, row)
            snapshot = store.snapshot
            def online(**kwargs):
                value = snapshot(**kwargs)
                value['controller'].update(online=True, stale=False, dispatch=True, age_seconds=0)
                return value
            guard = external(str(base/'primary'))
            clear = {**deepcopy(guard), 'state': 'NO_MATCH', 'reason': 'No recognized external computation observed', 'observations': []}
            projection = {'external_activity': clear, 'machine_usage': machine(), 'job_usage': {
                primary['attempt_id']: usage(cpu_cores=7.13), auxiliary['attempt_id']: usage(cpu_cores=0.5)}}
            app = JobsApp(store)
            with patch.object(store, 'snapshot', side_effect=online), patch.object(app, 'collect_activity'):
                async with app.run_test(size=(130, 40)) as pilot:
                    app.accept_activity(projection)
                    await pilot.pause()
                    table = app.query_one('#table', DataTable)
                    self.assertEqual(str(table.get_cell(primary['display_id'], 'role')), 'Primary')
                    self.assertEqual(str(table.get_cell(auxiliary['display_id'], 'role')), 'Auxiliary')
                    self.assertEqual(str(table.get_cell(primary['display_id'], 'cpu_now')), '713%')
                    self.assertIn('CPU 9/10', str(app.query_one('#sharing', Static).render()))
                    await pilot.press('down', 'b')
                    self.assertIsInstance(app.screen, SharingInspect)
                    self.assertIn('Auxiliary slot already occupied', str(app.screen.query_one('#sharing-body', Static).render()))
                    self.assertIn('Anchor: J1.1', str(app.screen.query_one('#sharing-body', Static).render()))
                    await pilot.press('escape')
                    now = time.time()
                    samples = []
                    for stamp in (now - 0.9, now - 0.1):
                        samples.append({'observed_at': stamp, 'logical_cpus': 16, 'machine_usage': machine(),
                                        'jobs': {primary['attempt_id']: usage(cpu_cores=9, rss_bytes=2**30, observed_at=stamp),
                                                 auxiliary['attempt_id']: usage(cpu_cores=0.5, rss_bytes=2**28, observed_at=stamp)}})
                    clear['observed_at'] = time.time()
                    app.accept_activity({**projection, 'sharing_evidence': {'samples': samples}})
                    self.assertIn('CPU exceeds declared', app.activity_projection['admission']['warnings'][0])
                    await pilot.press('b')
                    self.assertIn('sustained observed CPU', str(app.screen.query_one('#sharing-body', Static).render()))
                    await pilot.press('escape')
                    with store.transaction() as con:
                        row = store.get(con, primary['id'])
                        row.update(status='Done', finished_at=time.time())
                        store.save(con, row)
                    guard['observed_at'] = time.time()
                    store.set_external_reservation(sharing.make_reservation(guard['observations'][0], guard, 8, 8))
                    app.accept_activity({**projection, 'external_activity': guard})
                    await pilot.pause()
                    self.assertEqual(app.activity_projection['admission']['external_reservation_state'], 'valid')
                    self.assertEqual(usage_cells(app.external_observation('Eabc123')['usage'])[0], '713%')
                    await pilot.resize_terminal(64, 28)
                    await pilot.pause()
                    self.assertEqual(app.selected_id, auxiliary['display_id'])
                    self.assertIn('A', str(table.get_cell(auxiliary['display_id'], 'id')))
                    self.assertGreaterEqual(app.query_one('#controller').region.y, 0)
                    self.assertLessEqual(app.query_one('#shortcuts').region.bottom, 28)
                    expired = deepcopy(guard)
                    expired['observed_at'] = time.time()
                    expired['observations'][0]['members'][0]['identity']['birth'] += 1
                    app.accept_activity({**projection, 'external_activity': expired})
                    self.assertEqual(app.activity_projection['admission']['external_reservation_state'], 'expired')
                    await pilot.press('b')
                    self.assertIn('External reservation: expired', str(app.screen.query_one('#sharing-body', Static).render()))
                    await pilot.press('escape', 'q')
