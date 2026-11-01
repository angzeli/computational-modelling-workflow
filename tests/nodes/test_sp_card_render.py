"""Actual local/federated layouts for pending and recovered static observations."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.snapshot import render_snapshot
from cmw.jobs.store import Store
from cmw.nodes.codec import decode_typed
from cmw.progress.formatting import cell_width, progress_text
from cmw.progress.models import ProgressSnapshot
from cmw.progress.vasp_card import vasp_card_groups
from tests.jobs.ui_polish_acceptance import NOW, scenario_projection, screenshot_text
from tests.nodes.test_portal import FakeBackend
from tests.progress.test_scientific_card import example

try:
    from cmw.jobs.tui import JobsApp
    from cmw.nodes.portal import NodesApp, row_key, target_key
    from textual.widgets import DataTable, Static, TextArea
except ImportError:
    JobsApp = NodesApp = None

SIZES = ((77, 24), (80, 24), (100, 30), (140, 40))
DIAGNOSTIC = 'Synthetic retained diagnostic: declaration does not establish scientific acceptance'


def static_states(context):
    valid = replace(example(), context=context, messages=(DIAGNOSTIC,))
    empty = replace(valid, status='SOURCE_UNRESOLVED', iteration=(), metrics=(), verification=(),
                    phase='Waiting for first complete electronic row', read_health='OK')
    empty = replace(empty, card_groups=vasp_card_groups(empty))
    startup = replace(empty, calculation='unknown', settings=(), phase='Waiting for output', read_health='NOT_CREATED')
    startup = replace(startup, card_groups=vasp_card_groups(startup))
    failure = replace(valid, read_health='ERROR', messages=(*valid.messages, 'Synthetic transient reader failure'))
    return {'startup': startup, 'empty': empty, 'scf': valid, 'failure': failure, 'recovery': valid}


def capture_layout(app, scenario, size, *, output=None, prefix):
    card_widget = app.query_one('#progress', Static)
    card = str(card_widget.render())
    table = app.query_one('#table', DataTable)
    svg = app.export_screenshot()
    text = screenshot_text(svg)
    assert all(token in card for token in ('┌─', 'SCF', 'FORCE', 'ELECTRONIC', '└')), (prefix, scenario, size, card)
    assert 'Scientific: BOUND' not in card and 'Configure Sources' not in card, card
    assert DIAGNOSTIC not in card, card
    assert table.size.height - table.header_height >= 3, (prefix, scenario, size, table.region)
    assert card_widget.region.bottom <= app.query_one('#command-bar').region.y, (prefix, scenario, size)
    assert card_widget.size.height >= len(card.splitlines()), (prefix, scenario, size, card)
    assert app.query_one('#command-bar').region.bottom <= size[1], (prefix, scenario, size)
    assert 'Files' in text and 'Progress' in text and 'detach' in text.casefold(), (prefix, scenario, size, text)
    frame = card.splitlines()[next(i for i, line in enumerate(card.splitlines()) if line.startswith('┌─')):]
    assert len({cell_width(line) for line in frame}) == 1, (prefix, scenario, size, frame)
    if scenario in ('startup', 'empty'):
        assert '—' in card and not any(token in card for token in ('3.1087', '@SCF8')), card
    if scenario != 'startup':
        assert 'Target N/A · static' in card and 'EDIFF 1E-5 eV' in card, card
    if scenario == 'failure':
        assert 'cached' in card.lower() and 'ERROR' in card, card
    record = {'app': prefix, 'scenario': scenario, 'size': size, 'card': card,
              'card_height': card_widget.region.height, 'job_rows': table.size.height - table.header_height,
              'controls_bottom': app.query_one('#command-bar').region.bottom}
    if output:
        output.mkdir(parents=True, exist_ok=True)
        (output / f'{prefix}-{scenario}-{size[0]}x{size[1]}.svg').write_text(svg)
    return record


async def local_views(root, output=None):
    store = Store(root / 'state')
    for index in range(8):
        store.add(argv=['/unexecuted/synthetic'], cwd=root, engine='VASP', hold=True,
                  name=f'Synthetic static observation {index + 1}')
    state = store.snapshot(now=NOW)
    state['controller'].update(online=True, stale=False, dispatch=True, heartbeat=NOW, age_seconds=0)
    for job in state['jobs']:
        job.update(status='Done', order=None, elapsed=45.)
    selected = state['jobs'][-1]
    selected.update(status='Run', elapsed=150.)
    context = replace(example().context, binding_id='synthetic-sp-ui', attempt_id=selected['attempt_id'],
                      job_id=selected['id'], jobs_state=str(store.root))
    app = JobsApp(store, clock=lambda: NOW)
    app.selected_id = selected['display_id']
    projection = scenario_projection(state, 'no_external_complete')
    app.activity_projection = deepcopy(projection)
    app.job_usage = deepcopy(projection['job_usage'])
    records = []
    operational = dict(observed_execution='VERIFIED_WORKER', age_seconds=0, stale=False,
                       reconciliation_required=False, evidence={})
    with patch.object(store, 'snapshot', side_effect=lambda **_: deepcopy(state)), \
         patch.object(store, 'transaction', side_effect=AssertionError('No state mutation')), \
         patch.object(JobsApp, 'collect_activity'), patch.object(JobsApp, 'collect_progress'), \
         patch.object(JobsApp, 'collect_execution'), patch.object(JobsApp, 'selected_execution', return_value=operational), \
         patch('subprocess.Popen', side_effect=AssertionError('No process launch')), \
         patch('time.time', return_value=NOW):
        async with app.run_test(size=SIZES[0]) as pilot:
            table = app.query_one('#table', DataTable)
            table.move_cursor(row=app.row_ids.index(selected['display_id']), animate=False)
            for scenario, snapshot in static_states(context).items():
                app.progress_snapshot = snapshot
                for size in SIZES:
                    await pilot.resize_terminal(*size)
                    app.refresh_state()
                    app.render_progress()
                    await pilot.pause()
                    records.append(capture_layout(app, scenario, size, output=output, prefix='local'))
                capture = app.capture_copy_snapshot()
                assert DIAGNOSTIC in render_snapshot(capture, mode='diagnostics')
                assert DIAGNOSTIC in render_snapshot(capture, mode='json')
                assert DIAGNOSTIC in progress_text(snapshot, detailed=True)
                app.action_details()
                await pilot.pause()
                assert DIAGNOSTIC in str(app.screen.query_one('#body', Static).render())
                await pilot.press('escape')
                await pilot.pause()
    return records


async def remote_views(output=None):
    backend = FakeBackend('MINI', remote=True)
    backend.context = replace(backend.context, declarations=backend.context.sources)
    for job in backend.jobs:
        job['resources'].update(mpi_ranks=8, threads_per_rank=1)
        job['reason'] = None
    app = NodesApp({'MINI': backend}, clock=lambda: 112.)
    records = []
    async with app.run_test(size=SIZES[0]) as pilot:
        for _ in range(100):
            if app.rows:
                break
            await pilot.pause(.02)
        assert app.rows
        job = backend.jobs[0]
        app.select(row_key(job['target']))
        lane = app.lanes['MINI']
        for scenario, snapshot in static_states(backend.context).items():
            backend.snapshot = snapshot
            envelope = backend.call('progress', target=job['target'])
            decoded = decode_typed(envelope['result']['snapshot'], ProgressSnapshot)
            assert decoded == snapshot
            lane.cache.setdefault(target_key(job['target']), {})['progress'] = (envelope, 112.)
            for size in SIZES:
                await pilot.resize_terminal(*size)
                app.render_state()
                await pilot.pause()
                records.append(capture_layout(app, scenario, size, output=output, prefix='remote'))
            capture = app.capture_copy_snapshot()
            assert DIAGNOSTIC in render_snapshot(capture, mode='diagnostics')
            assert DIAGNOSTIC in render_snapshot(capture, mode='json')
            assert DIAGNOSTIC in progress_text(decoded, detailed=True)
            app.action_details()
            await pilot.pause()
            details = app.screen.query_one(TextArea).text
            assert DIAGNOSTIC in details and job['attempt_id'] in details
            assert '/remote/MINI/OUTCAR' in details
            await pilot.press('escape')
            await pilot.pause()
        cache = lane.cache[target_key(job['target'])]
        assert app.selected_snapshot('MINI', job).read_health == 'OK'
        cache['progress.error'] = 'Synthetic endpoint observation unavailable'
        app.action_details()
        await pilot.pause()
        details = app.screen.query_one(TextArea).text
        assert 'earlier cached evidence' in details
        assert cache['progress.error'] in details and DIAGNOSTIC in details
        await pilot.press('escape')
    return records


@unittest.skipIf(JobsApp is None or NodesApp is None, 'Jobs UI extra unavailable')
class StaticCardRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_states_keep_local_frame_and_controls(self):
        with tempfile.TemporaryDirectory(prefix='cmw-sp-local-') as directory:
            records = await local_views(Path(directory))
        self.assertEqual(len(records), 20)

    async def test_static_states_keep_federated_frame_and_codec(self):
        records = await remote_views()
        self.assertEqual(len(records), 20)


class StaticCardEndpointTests(unittest.TestCase):
    def test_real_endpoint_retains_table_across_waiting_observed_and_recovery(self):
        import os
        import sys
        from cmw.nodes.connections import ConnectedBackend
        from cmw.nodes.identity import enroll
        from cmw.nodes.transport import ProcessTransport
        from tests.periodic.vasp.result_fixtures import native_texts
        from cmw.progress.models import SourceDescriptor

        with tempfile.TemporaryDirectory(prefix='cmw-sp-endpoint-') as directory:
            root = Path(directory).resolve()
            output = root / 'output'
            output.mkdir()
            spec = {'queues': {'research': str(root / 'queue')}, 'storages': {'scratch': str(output)},
                    'profiles': {'benign': {'engine': 'Command', 'runner': '/bin/echo', 'max_cpus': 1}}}
            config = enroll(root / 'node', spec)
            sources = tuple(SourceDescriptor(role, str(output / filename))
                            for role, filename in (('outcar', 'OUTCAR'), ('oszicar', 'OSZICAR')))
            store = Store(root / 'queue')
            job = store.add(argv=['/unexecuted/synthetic'], cwd=output, engine='VASP', hold=True, name='Synthetic static endpoint',
                            progress_sources=sources)
            before = store.path.read_bytes()
            command = [sys.executable, '-B', '-m', 'cmw.cli', 'nodes', 'endpoint',
                       '--config', str(root / 'node'), '--queue', 'research']
            source = Path(__file__).resolve().parents[2] / 'src'
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
            if source.is_dir():
                env['PYTHONPATH'] = str(source)
            else:
                env.pop('PYTHONPATH', None)
            transport = ProcessTransport(command, timeout=20, env=env)
            entry = {'transport': 'local', 'config_root': str(root / 'node'), 'node_id': config['node_id'],
                     'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
                     'queues': {'research': config['queues']['research']['queue_id']},
                     'default_queue': 'research', 'read_only': True}
            backend = ConnectedBackend('synthetic', entry, transport=transport)
            target = {'node_id': config['node_id'], 'queue_id': config['queues']['research']['queue_id'],
                      'job_id': job['id'], 'attempt_id': job['attempt_id']}
            texts = native_texts(ediffg='.10000000E-02')
            prefix = texts['OUTCAR'].split('--------------------------------------- Iteration')[0]
            try:
                for state in ('startup', 'empty', 'scf', 'failure', 'recovery'):
                    if state == 'empty':
                        (output / 'OUTCAR').write_text(prefix)
                        (output / 'OSZICAR').write_text('')
                    elif state in ('scf', 'recovery'):
                        for name in ('OUTCAR', 'OSZICAR'):
                            (output / name).write_text(texts[name])
                    elif state == 'failure':
                        (output / 'OUTCAR').unlink()
                    response = backend.call('progress', target=target)
                    snapshot = decode_typed(response['result']['snapshot'], ProgressSnapshot)
                    self.assertEqual(snapshot.context.attempt_id, job['attempt_id'])
                    self.assertEqual(snapshot.context.binding_status, 'BOUND')
                    card = progress_text(snapshot, width=73, max_lines=8, framed=True, now=response['observed_at'])
                    for token in ('┌─', 'SCF', 'FORCE', 'ELECTRONIC'):
                        self.assertIn(token, card, (state, card))
                    self.assertNotIn('Scientific: BOUND', card)
                    self.assertNotIn('Configure Sources', card)
                    if state in ('empty', 'scf', 'recovery'):
                        self.assertIn('static', card)
                        self.assertIn('EDIFF 1E-6 eV', card)
                    if state in ('scf', 'recovery'):
                        self.assertTrue(snapshot.metrics)
                self.assertEqual(store.path.read_bytes(), before)
            finally:
                backend.close()
