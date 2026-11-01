"""Real shared widgets and adapters over reviewed, synthetic molecular attempts."""
from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from cmw.jobs.folder_opener import OpenResult
from cmw.jobs.molecular import submit
from cmw.jobs.store import Store
from cmw.progress.service import ObservationService
from tests.jobs.ui_polish_acceptance import NOW, SIZES, scenario_projection, screenshot_text, settle
from tests.molecular.orca.jobs_fixture import make_spec as orca_spec
from tests.molecular.multiwfn.jobs_fixtures import make_spec as multiwfn_spec
from tests.progress.orca_fixtures import cycle, header, scf


def fixtures(root):
    root.mkdir(parents=True)
    store = Store(root / 'state')
    for index in range(8):
        store.add(argv=['/unexecuted/fixture'], cwd=root, name=f'Synthetic history {index}', hold=True)
    cases = {}
    for profile in ('sp', 'opt', 'FMO', 'ESP', 'IGMH', 'IFCT'):
        spec = orca_spec(root/profile, profile=profile) if profile.islower() else multiwfn_spec(root/profile, profile)
        registered = submit(store, spec)
        assert registered['status'] == 'REGISTERED', registered
        job = registered['job']; plan = job['molecular_submission']['plan']
        if profile.islower():
            Path(plan['progress_sources'][0]['path']).write_text(header(stage=profile.upper()) +
                (scf(soscf=False, converged=False) if profile == 'sp' else cycle(1)+cycle(2, table=False, converged=False)))
        else:
            session = Path(plan['progress_sources'][0]['path'])
            session.write_text('Version '+spec['runtime']['version']+'\n( Number of parallel threads: 2 Current date: synthetic )\nLoaded synthetic.molden successfully!\n')
        cases[profile] = job
    return store, cases


async def render_cases(store, cases, renders):
    from cmw.jobs.copy_ui import CopyPreview
    from cmw.jobs.history_ui import ProgressHistory
    from cmw.jobs.tui import JobsApp, NoteEditor, Inspect
    from cmw.jobs.sources_ui import Sources
    from textual.widgets import DataTable, Footer, Static, TextArea
    report = {'views': [], 'controls': [], 'issues': []}
    base = store.snapshot(now=NOW)
    for profile, selected in cases.items():
        state = deepcopy(base)
        state['controller'].update(online=True, stale=False, dispatch=True, heartbeat=NOW, age_seconds=0)
        for job in state['jobs']:
            job.update(status='Run' if job['id'] == selected['id'] else 'Done', order=None, reason=None)
        projection = scenario_projection(state, 'orca_active')
        service = ObservationService(clock=lambda: NOW)
        opener = Mock(return_value=OpenResult('OPEN_REQUESTED', 'Stubbed opener', str(store.root.parent)))
        app = JobsApp(store, clock=lambda: NOW, progress_service=service, file_opener=opener)
        app.selected_id = selected['display_id']
        app.activity_projection = deepcopy(projection)
        app.job_usage = deepcopy(projection['job_usage'])
        with patch.object(store, 'snapshot', side_effect=lambda **_: deepcopy(state)), \
             patch.object(store, 'transaction', side_effect=AssertionError('UI must not write state')), \
             patch.object(JobsApp, 'collect_activity'), patch.object(JobsApp, 'collect_execution'), \
             patch('subprocess.Popen', side_effect=AssertionError('UI must not execute')), patch('time.time', return_value=NOW):
            async with app.run_test(size=SIZES[0]) as pilot:
                table = app.query_one('#table', DataTable)
                table.move_cursor(row=app.row_ids.index(selected['display_id']), animate=False)
                await settle(app, pilot)
                for size in SIZES:
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    app.refresh_state()
                    await settle(app, pilot)
                    svg = app.export_screenshot()
                    text = screenshot_text(svg)
                    card = str(app.query_one('#progress', Static).render())
                    footer = app.query_one(Footer) if app.query_one(Footer).display else app.query_one('#shortcuts', Static)
                    assert footer.region.bottom <= size[1], (profile, size, footer.region)
                    assert table.size.height - table.header_height >= 3, (profile, size, table.size)
                    assert app.query_one("#progress").region.bottom <= app.query_one("#command-bar").region.y
                    assert app.selected_id == selected['display_id']
                    assert 'SCIENTIFIC' in text and '┌' in card, (profile, size, text)
                    if profile.isupper():
                        assert all(label in card for label in ('ANALYSIS', 'STAGE', 'OUTPUT')), card
                        assert all(label not in card for label in ('FORCE', 'ELECTRONIC', 'SCF')), card
                    assert all(label in text for label in ('Files', 'Progress')), (profile, size, text)
                    if renders:
                        (renders / f'{profile}-{size[0]}x{size[1]}.svg').write_text(svg)
                    report['views'].append({'profile': profile, 'size': list(size), 'table_height': table.size.height,
                        'footer_bottom': footer.region.bottom, 'card': card, 'selection': app.selected_id})
                await pilot.press('enter')
                assert isinstance(app.screen, Inspect)
                await pilot.press('escape', 's')
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert isinstance(app.screen, Sources) and app.screen.inspection is not None
                await pilot.press('escape', 'y')
                assert isinstance(app.screen, CopyPreview)
                assert selected['name'] in app.screen.query_one(TextArea).text
                await pilot.press('escape', 'g')
                await settle(app, pilot)
                assert isinstance(app.screen, ProgressHistory) and app.screen.page.entries
                await pilot.press('escape', 'n')
                assert isinstance(app.screen, NoteEditor)
                await pilot.press('escape', 'f')
                await app.workers.wait_for_complete()
                opener.assert_called_once()
                await pilot.press('q')
                report['controls'].append(profile)
        assert service._closed and not service._contexts and service._inflight == 0
    return report


def molecular_ui_surface(root, *, renders=None):
    if renders:
        renders.mkdir(parents=True, exist_ok=True)
    store, cases = fixtures(Path(root).resolve())
    before = store.path.read_bytes()
    report = asyncio.run(render_cases(store, cases, renders))
    assert store.path.read_bytes() == before
    report['queue_unchanged'] = True
    return report


class JsonEndpointTransport:
    """Real local endpoint results cross the same strict JSON/typed boundary."""
    def __init__(self, backend):
        self.backend = backend
        self.block = None
        self.started = threading.Event()

    def exchange(self, operation, params, target, request_id=None):
        if self.block is not None and operation == 'progress':
            self.started.set()
            assert self.block.wait(10), 'Synthetic delayed reply was not released'
        result = self.backend.call(operation, params, target, request_id=request_id)
        return json.loads(json.dumps(result, allow_nan=False))

    def close(self):
        self.backend.close()


async def _until(pilot, condition):
    for _ in range(160):
        if condition():
            return
        await pilot.pause(.025)
    raise AssertionError('The actual endpoint/frontend observation did not settle')


def _endpoint_projection(store):
    state = store.snapshot(now=NOW)
    return {**state, **scenario_projection(state, 'no_external_complete')}


async def federated_render_cases(root, renders=None):
    from cmw.jobs.copy_ui import FrozenText
    from cmw.jobs.history_ui import ProgressHistory
    from cmw.jobs.sources_ui import Sources
    from cmw.nodes.backend import LocalBackend
    from cmw.nodes.connections import ConnectedBackend
    from cmw.nodes.identity import enroll
    from cmw.nodes.portal import NodesApp, row_key, target_key
    from cmw.nodes.portal_views import NodeFiles, NodeCopyPreview
    from textual.widgets import DataTable, Static

    backends, all_cases, stores = {}, {}, {}
    for owner in ('MBP', 'MINI'):
        store, cases = fixtures(root / owner)
        config_root = root / owner / 'node'
        config = enroll(config_root, {'queues': {'research': str(store.root)},
                                      'storages': {'fixtures': str(root / owner)},
            'profiles': {'analysis': {'engine': 'Multiwfn', 'max_cpus': 2,
                'runner': cases['FMO']['molecular_submission']['plan']['argv'][1],
                'runtime': cases['FMO']['molecular_submission']['spec']['runtime']}}})
        endpoint = LocalBackend(config_root, 'research', clock=lambda: NOW,
            activity_projector=_endpoint_projection)
        entry = {'transport': 'local', 'config_root': str(config_root),
            'node_id': config['node_id'], 'host_id': config['host_id'],
            'runtime_id': config['runtime']['fingerprint'],
            'queues': {'research': config['queues']['research']['queue_id']},
            'default_queue': 'research', 'read_only': True}
        backends[owner] = ConnectedBackend(owner, entry, transport=JsonEndpointTransport(endpoint))
        all_cases[owner], stores[owner] = cases, store
    before = {owner: store.path.read_bytes() for owner, store in stores.items()}
    views, controls = [], []
    app = NodesApp(backends)
    try:
        async with app.run_test(size=SIZES[0]) as pilot:
            await _until(pilot, lambda: len(app.rows) == 28)
            for profile in ('FMO', 'ESP', 'IGMH', 'IFCT'):
                job = all_cases['MINI'][profile]
                peer = all_cases['MBP'][profile]
                assert job['display_id'] == peer['display_id'] and job['attempt_id'] != peer['attempt_id']
                target = dict(node_id=backends['MINI'].node_id, queue_id=backends['MINI'].queue_id,
                              job_id=job['id'], attempt_id=job['attempt_id'])
                app.select(row_key(target))
                app.collect_progress()
                try:
                    await _until(pilot, lambda: app.selected_snapshot('MINI', {**job, 'target': target}) is not None)
                except AssertionError as exc:
                    lane = app.lanes['MINI']
                    cache = lane.cache.get(target_key(target), {})
                    active = lane.active_request
                    diagnostic = {'profile': profile, 'target': target, 'selection': app.selected_key,
                        'active': None if active is None else
                            {'operation': active[0], 'target': active[2], 'automatic': active[4]},
                        'active_age': None if lane.request_started is None else app.clock() - lane.request_started,
                        'lane_error': str(lane.error)[:512] if lane.error is not None else None,
                        'busy': lane.busy, 'pending_count': len(lane.pending),
                        'pending': [{'operation': item[0], 'target': item[2]} for item in list(lane.pending)[:8]],
                        'cached_operations': sorted(cache)[:16],
                        'cache_errors': {key: str(cache[key])[:512]
                            for key in sorted(key for key in cache if key.endswith('.error'))[:8]}}
                    raise AssertionError(f'{exc}; {json.dumps(diagnostic, sort_keys=True)}') from exc
                for size in SIZES:
                    await pilot.resize_terminal(*size)
                    app.render_state()
                    await pilot.pause()
                    table = app.query_one('#table', DataTable)
                    card = str(app.query_one('#progress', Static).render())
                    assert table.size.height - table.header_height >= 3, (profile, size)
                    assert app.query_one('#progress').region.bottom <= app.query_one('#command-bar').region.y
                    assert app.query_one('#command-bar').region.bottom <= size[1]
                    assert app.selected_key == row_key(target)
                    assert '┌─' in card and all(label in card for label in ('ANALYSIS', 'STAGE', 'OUTPUT'))
                    assert all(label not in card for label in ('FORCE', 'ELECTRONIC', 'SCF', '%'))
                    assert 'MINI / research / ' + job['display_id'] in str(app.query_one('#selected').render())
                    if renders:
                        (renders / f'federated-{profile}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
                    views.append({'profile': profile, 'size': size, 'card': card,
                                  'useful_rows': table.size.height - table.header_height})
                await pilot.press('enter')
                assert isinstance(app.screen, FrozenText) and 'Multiwfn' in app.screen.text
                await pilot.press('escape', 'g')
                try:
                    await _until(pilot, lambda: isinstance(app.screen, ProgressHistory) and app.screen.page is not None)
                except AssertionError as exc:
                    lane = app.lanes['MINI']
                    view = app.screen if isinstance(app.screen, ProgressHistory) else app.history_view
                    cache = lane.cache.get(target_key(target), {})
                    active = lane.active_request
                    diagnostic = {'profile': profile, 'target': target, 'selection': app.selected_key,
                        'screen': type(app.screen).__name__,
                        'history': None if view is None else {'closed': view.closed, 'expired': view.expired,
                            'generation': view.request_generation, 'page': view.page is not None},
                        'lane_error': str(lane.error)[:512] if lane.error is not None else None,
                        'busy': lane.busy, 'active': None if active is None else
                            {'operation': active[0], 'target': active[2], 'automatic': active[4]},
                        'pending_count': len(lane.pending),
                        'pending': [{'operation': item[0], 'target': item[2], 'automatic': item[4]}
                            for item in list(lane.pending)[:8]],
                        'cache_errors': {key: str(cache[key])[:512]
                            for key in sorted(key for key in cache if key.endswith('.error'))[:8]}}
                    raise AssertionError(f'{exc}; {json.dumps(diagnostic, sort_keys=True)}') from exc
                assert app.screen.page.entries
                await pilot.press('escape', 's')
                await _until(pilot, lambda: isinstance(app.screen, Sources) and app.screen.inspection is not None)
                assert any(source.role == 'multiwfn_session' for source in app.screen.inspection.context.sources)
                await pilot.press('escape', 'f')
                await _until(pilot, lambda: isinstance(app.screen, NodeFiles))
                await pilot.press('escape', 'y')
                await _until(pilot, lambda: isinstance(app.screen, NodeCopyPreview))
                await pilot.press('escape')
                assert app.selected_key == row_key(target)
                controls.append(profile)
            # A delayed reply for one node cannot follow an equal display ID to another.
            remote = backends['MINI']
            job = all_cases['MINI']['FMO']
            target = dict(node_id=remote.node_id, queue_id=remote.queue_id,
                          job_id=job['id'], attempt_id=job['attempt_id'])
            transport = remote.transport
            gate = threading.Event()
            transport.block = gate
            try:
                app.select(row_key(target))
                app.collect_progress()
                await _until(pilot, transport.started.is_set)
                peer = all_cases['MBP']['FMO']
                peer_target = dict(node_id=backends['MBP'].node_id, queue_id=backends['MBP'].queue_id,
                                   job_id=peer['id'], attempt_id=peer['attempt_id'])
                app.select(row_key(peer_target))
                gate.set()
                app.collect_progress()
                await _until(pilot, lambda: app.selected_snapshot('MBP', {**peer, 'target': peer_target}) is not None)
                assert app.selected_key == row_key(peer_target)
                assert app.selected_snapshot('MBP', {**peer, 'target': peer_target}).context.attempt_id == peer['attempt_id']
                await _until(pilot, lambda: not app.lanes['MINI'].busy)
            finally:
                gate.set()
                transport.block = None
            old_session = app.lanes['MINI'].session
            transport.backend.close()
            transport.backend = LocalBackend(root / 'MINI' / 'node', 'research', clock=lambda: NOW,
                activity_projector=_endpoint_projection)
            app.select(row_key(target))
            app.lanes['MINI'].next_poll = 0
            app.lanes['MINI'].next_progress = 0
            await _until(pilot, lambda: app.lanes['MINI'].session != old_session
                and app.selected_snapshot('MINI', {**job, 'target': target}) is not None)
            assert app.selected_snapshot('MINI', {**job, 'target': target}).context.attempt_id == job['attempt_id']
            await pilot.press('q')
    finally:
        for backend in backends.values():
            backend.close()
    assert all(store.path.read_bytes() == before[owner] for owner, store in stores.items())
    return {'views': views, 'controls': controls, 'queue_unchanged': True,
            'actual_json_endpoint': True, 'same_display_ids_isolated': True,
            'delayed_reply_isolated': True, 'reconnected_attempt_preserved': True}


def molecular_federated_ui_surface(root, *, renders=None):
    if renders:
        renders.mkdir(parents=True, exist_ok=True)
    return asyncio.run(federated_render_cases(Path(root).resolve(), renders))
