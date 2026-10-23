"""Real shared widgets and adapters over reviewed, synthetic molecular attempts."""
from __future__ import annotations

import asyncio
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
    from cmw.jobs.tui import JobsApp, NoteEditor
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
                    assert table.size.height >= 3, (profile, size, table.size)
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
                await pilot.press('y')
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
