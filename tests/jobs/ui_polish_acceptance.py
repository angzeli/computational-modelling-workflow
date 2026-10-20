"""Actual console acceptance from fictional native output and isolated Jobs state.

The same callable runs from the source tree and against an installed wheel.
Only process/telemetry projections and the desktop folder opener are stubbed;
the source association, native adapters, bounded readers and widgets are real.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import os
from pathlib import Path
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from cmw.jobs.activity import admission
from cmw.jobs.folder_opener import OpenResult
from cmw.jobs.locations import LocationDescriptor
from cmw.jobs.progress import resolve_context
from cmw.jobs.store import Store
from cmw.progress.models import SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts, structure_text
from tests.progress.orca_fixtures import cycle, header


SIZES = ((77, 24), (80, 24), (100, 30), (140, 40))
SCENARIOS = ('vasp_active', 'orca_active', 'no_external_complete',
             'no_external_warnings', 'external_vasp', 'ownership_uncertain',
             'controller_offline')
NOW = 1730000000.0
WARNINGS = tuple(f'Fictional helper {index}: executable unavailable; no recognized engine name (non-blocking)'
                 for index in range(1, 5))


def make_fixture(root):
    root.mkdir(parents=True)
    store = Store(root / 'state')
    for index in range(12):
        store.add(argv=['/unexecuted/fixture'], cwd=root,
                  name=f'Synthetic archive {index + 1:02d}', engine='UNKNOWN', hold=True)
    cases = {}
    for engine in ('VASP', 'ORCA'):
        directory = root / engine
        directory.mkdir()
        if engine == 'VASP':
            texts = native_texts(relaxation=True, evaluations=26, footer=False, nelm=90,
                                 ediffg='-0.01500000', forces=((.041, 0, 0), (.7, 0, 0)))
            lines, evaluation = [], 0
            for line in texts['OUTCAR'].splitlines():
                if 'reached required accuracy' in line:
                    continue
                if '0.041000 0.000000 0.000000' in line and evaluation == 25:
                    line = line.replace('0.041000 0.000000 0.000000', '0.052000 0.000000 0.000000')
                lines.append(line)
                if 'energy  without entropy=' in line:
                    evaluation += 1
                    duration = 8675 if evaluation == 26 else 3674
                    lines.append(f' LOOP+: cpu time 18.000: real time {duration}.000')
            lines.extend((' Iteration 27(1)', ' EDDAV: synthetic active calculation',
                          ' Iteration 27(2)', ' EDDAV: synthetic active calculation'))
            texts['OUTCAR'] = '\n'.join(lines) + '\n'
            texts['OSZICAR'] += ('DAV: 1 -4.0000000000 -4.00E+00 -3.00E-02 12 0.003 0.004\n'
                                 'DAV: 2 -4.0020000000 -2.00E-03 -1.50E-03 12 0.002 0.003\n')
            texts['POSCAR'] = structure_text(flags=((True,) * 3, (False,) * 3))
            sources = tuple(SourceDescriptor(role.lower(), str(directory / role))
                            for role in ('OUTCAR', 'OSZICAR', 'POSCAR'))
            for source in sources:
                Path(source.path).write_text(texts[source.role.upper()])
        else:
            source = directory / 'declared.out'
            source.write_text(header() + cycle(1) + cycle(2) + cycle(3, soscf=False, table=False, converged=False))
            sources = (SourceDescriptor('orca_output', str(source), stage='OPT'),)
        job = store.add(argv=['/unexecuted/fixture'], cwd=directory,
                        name=f'Synthetic {engine} literal [sample] geometry continuation',
                        engine=engine, hold=True)
        cases[engine] = (job, sources)
    return store, cases


def scenario_state(base, cases, scenario):
    state = deepcopy(base)
    state['events'] = []
    state['controller'].update(online=True, stale=False, dispatch=True, heartbeat=NOW,
                               age_seconds=0, reason='Synthetic controller')
    engine = 'ORCA' if scenario == 'orca_active' else 'VASP'
    selected = cases[engine][0]
    for job in state['jobs']:
        job.update(status='Done', order=None, reason=None, elapsed=275.0)
        if job['id'] == selected['id']:
            job.update(status='Run', elapsed=12680.0, started_at=NOW - 12680)
    if scenario == 'vasp_active':
        queued = next(job for job in state['jobs'] if job['id'] == cases['ORCA'][0]['id'])
        queued.update(status='Queue', order=1, elapsed=None)
    if scenario == 'external_vasp':
        next(job for job in state['jobs'] if job['id'] == selected['id']).update(status='Hold', order=1)
    if scenario == 'ownership_uncertain':
        next(job for job in state['jobs'] if job['id'] == selected['id']).update(status='Unknown')
    if scenario == 'controller_offline':
        state['controller'].update(online=False, reason='Synthetic controller offline')
    return state, selected


def scenario_projection(state, scenario):
    usage = dict(cpu_cores=2.35, rss_bytes=2 * 1024**3, cpu_quality='fresh', memory_quality='fresh',
                 cpu_members=3, memory_members=3, total_members=3,
                 observed_at=NOW, age_seconds=0, stale=False, reason='Synthetic telemetry')
    guard = dict(state='NO_MATCH', reason='No recognized external computation observed',
                 source='client', observed_at=NOW, age_seconds=0, stale=False,
                 scope='Current local user; recognized executables; accessible process metadata',
                 coverage={'complete': True, 'warnings': []}, observations=[])
    if scenario == 'no_external_warnings':
        guard['coverage']['warnings'] = list(WARNINGS)
        guard['coverage'].update(limitations=list(WARNINGS), uncertainties=0)
    if scenario == 'external_vasp':
        guard.update(state='BUSY', reason='Recognized external computation observed', observations=[{
            'id': 'E-synthetic-73', 'engine': 'VASP', 'pid': 1234567, 'status': 'running',
            'nproc': 3, 'age_seconds': 170.0, 'exe': '/synthetic/vasp_std', 'cwd': '/synthetic/external',
            'ownership': 'External / unattributed', 'evidence': ['Fictional verified executable identity'],
            'usage': deepcopy(usage)}])
    if scenario == 'ownership_uncertain':
        guard.update(state='UNCERTAIN', reason='Synthetic recognized identity cannot be verified')
        guard['coverage'] = {'complete': False, 'warnings': ['Synthetic payload ownership unresolved']}
    with patch('time.time', return_value=NOW):
        decision = admission(state, guard)
    return {'external_activity': guard, 'admission': decision,
            'machine_usage': dict(cpu_percent=43.0, ram_used_bytes=7 * 1024**3,
                                  ram_total_bytes=24 * 1024**3, cpu_quality='fresh', memory_quality='fresh',
                                  observed_at=NOW, age_seconds=0, stale=False, reason='Synthetic telemetry'),
            'job_usage': {job['attempt_id']: deepcopy(usage) for job in state['jobs'] if job['status'] == 'Run'}}


async def settle(app, pilot):
    for _ in range(1200):
        view = app.history_view
        snapshot = app.progress_snapshot
        if (not app.progress_busy and snapshot is not None and not any(source.has_more for source in snapshot.sources)
                and (view is None or view.page is not None and view.page.status != 'LOADING')):
            await pilot.pause()
            return
        await asyncio.sleep(.01)
    raise AssertionError('Synthetic bounded observation did not settle')


def screenshot_text(svg):
    return '\n'.join(''.join(element.itertext()).replace('\xa0', ' ')
                     for element in ET.fromstring(svg).iter() if element.tag.endswith('}text'))


def layout_record(app, scenario, size, selected, svg):
    from textual.widgets import DataTable, Footer, Static
    text = screenshot_text(svg)
    table = app.query_one('#table', DataTable)
    footer = app.query_one(Footer) if app.query_one(Footer).display else app.query_one('#shortcuts', Static)
    card = str(app.query_one('#progress', Static).render())
    issues = []
    def check(condition, message):
        if not condition:
            issues.append(message)
    check(footer.region.bottom <= size[1], 'Shortcut footer extends below terminal')
    check(table.size.height >= 3, 'Managed table has fewer than header plus two rows')
    check(app.selected_id == selected['display_id'], 'Resize/refresh changed selected job identity')
    check(table.scroll_y <= table.cursor_row < table.scroll_y + table.size.height - table.header_height,
          'Selected managed row is outside the table viewport')
    check('FILES' in text and 'NAME' in text and 'STATUS' in text, 'Primary table headers not rendered')
    check('data ' in text or 'Evidence' in text or 'evidence' in text, 'Scientific data age not visible')
    if scenario == 'orca_active':
        check('Eh' in card, 'Native ORCA energy unit missing')
    else:
        from cmw.progress.formatting import progress_text
        check('eV/Å' in card and 'OUTCAR SCF' in card, 'Force unit or OUTCAR iteration provenance missing')
        check(any(timing.source == 'native' and timing.clock == 'wall' and timing.value == 8675
                  for timing in app.progress_snapshot.completed_durations),
              'Completed native wall duration lost its value or source')
        check('(wall, native)' in progress_text(app.progress_snapshot, detailed=True),
              'Native timing qualification missing from Details')
    check('8.675e+03s' not in card and '8675s' not in card,
          'Duration retains unnecessary scientific/raw seconds form')
    check(app.query_one('#external', DataTable).display == (scenario == 'external_vasp'),
          'Empty external table did not collapse or occupied table disappeared')
    if scenario == 'no_external_warnings':
        check('4' in str(app.query_one('#external-heading', Static).render()), 'Coverage warning count hidden')
    if scenario in ('ownership_uncertain', 'controller_offline'):
        check(any(word in text.lower() for word in ('uncertain', 'unknown', 'offline', 'warning', 'unavailable')),
              'Genuine warning is not visibly identified')
    if scenario != 'orca_active':
        force = next(series.latest for series in app.progress_snapshot.metrics if series.latest.semantic_id.endswith('.free'))
        check(force.context[0].index == 26, 'Native force lost evaluation 26 identity')
        check(app.progress_snapshot.iteration[0].index == 27, 'Active evaluation is not 27')
        check('@E26' in text or '@26' in text or '#26' in text, 'Last completed force context is not visible')
        check('provisional' in text.lower(), 'Provisional criterion qualification hidden')
        electronic = {series.latest.semantic_id: series.latest for series in app.progress_snapshot.metrics
                      if series.latest.semantic_id.startswith('vasp.electronic.')}
        for suffix, value, rendered in (('dE', .002, '2e-03 eV @SCF2'),
                                        ('d_eps', .0015, '1.5e-03 eV @SCF2'),
                                        ('rms', .002, '2e-03 @SCF2')):
            metric = electronic.get('vasp.electronic.' + suffix)
            check(metric is not None and metric.value == value
                  and tuple((level.kind, level.index) for level in metric.context) == (('electronic', 2),)
                  and metric.qualification == 'source-local',
                  f'Current electronic {suffix} lost its value or source-local context')
            check(rendered in card and rendered in text, f'Current electronic {suffix} not rendered')
        check('source-local' in card and 'source-local' in text,
              'Current electronic source-local qualification hidden')
        correspondence = app.progress_snapshot.verification[0]
        check(correspondence.source_role == 'oszicar' and correspondence.observed_frontier == 2
              and correspondence.counterpart_role == 'outcar' and correspondence.checked_frontier is None,
              'Pending OUTCAR correspondence gained a fabricated checked frontier')
    return {'scenario': scenario, 'size': list(size), 'selected': app.selected_id,
            'table_height': table.size.height, 'table_scroll_y': table.scroll_y,
            'footer_bottom': footer.region.bottom, 'progress': card,
            'guard_color': str(app.query_one('#guard', Static).styles.color),
            'coverage_color': str(app.query_one('#external-heading', Static).styles.color),
            'rendered_text': text, 'issues': issues}


async def controls(app, pilot, selected, opener, projection):
    from cmw.jobs.history_ui import ProgressHistory
    from cmw.jobs.tui import Inspect, NoteEditor, SharingInspect
    from textual.widgets import Static, TextArea
    await pilot.press('enter')
    assert isinstance(app.screen, Inspect)
    detail = str(app.screen.query_one('#body', Static).render())
    assert selected['name'] in detail and 'Source' in detail
    await pilot.press('escape', 'n', 'f', 'g', 'p', 'h', 'o', 'x', 'b', 'q')
    assert isinstance(app.screen, NoteEditor)
    assert app.screen.query_one(TextArea).text == 'fgphoxbq'
    opener.assert_not_called()
    await pilot.press('escape', 'f')
    for _ in range(200):
        if not app.files_busy:
            break
        await asyncio.sleep(.01)
    opener.assert_called_once()
    await pilot.press('g')
    await settle(app, pilot)
    assert isinstance(app.screen, ProgressHistory) and app.screen.page.entries
    await pilot.press('escape', 'b')
    assert isinstance(app.screen, SharingInspect)
    diagnostics = str(app.screen.query_one('#sharing-body', Static).render())
    for warning in projection['external_activity']['coverage']['warnings']:
        assert warning in diagnostics, ('Coverage evidence missing from Scheduling', warning)
    assert projection['admission']['reason'] in diagnostics
    await pilot.press('escape')


async def console_cases(store, cases, *, renders=None, scenarios=SCENARIOS, sizes=SIZES):
    from cmw.jobs.tui import JobsApp
    from textual.widgets import DataTable
    base = store.snapshot(now=NOW)
    report = {'views': [], 'controls': [], 'reader_contexts_after_close': 0}
    source_map = {job['id']: sources for job, sources in cases.values()}
    def resolver(selected_store, job_id, **kwargs):
        kwargs['sources'] = source_map.get(job_id, ())
        return resolve_context(selected_store, job_id, **kwargs)
    for scenario in scenarios:
        state, selected = scenario_state(base, cases, scenario)
        projection = scenario_projection(state, scenario)
        canonical_admission = deepcopy(projection['admission'])
        service = ObservationService(clock=lambda: NOW)
        opener = Mock(return_value=OpenResult('OPEN_REQUESTED', 'Stubbed desktop boundary', str(store.root.parent)))
        # Textual chooses color filters at construction, before a test pilot starts.
        with patch.dict(os.environ):
            os.environ.pop('NO_COLOR', None)
            app = JobsApp(store, clock=lambda: NOW, progress_service=service, progress_resolver=resolver,
                          location_descriptors=(LocationDescriptor('output', str(store.root.parent)),), file_opener=opener)
        app.selected_id = selected['display_id']
        app.activity_projection = deepcopy(projection)
        app.job_usage = deepcopy(projection['job_usage'])
        with patch.object(store, 'snapshot', side_effect=lambda **_: deepcopy(state)), \
                patch.object(JobsApp, 'collect_activity'), \
                patch.object(store, 'transaction', side_effect=AssertionError('Read-only acceptance may not write Jobs state')), \
                patch('subprocess.Popen', side_effect=AssertionError('No process launch in UI acceptance')), \
                patch('time.time', return_value=NOW):
            async with app.run_test(size=sizes[0]) as pilot:
                await settle(app, pilot)
                table = app.query_one('#table', DataTable)
                table.move_cursor(row=app.row_ids.index(selected['display_id']), animate=False)
                await pilot.pause()
                generation, binding = app.progress_generation, app.progress_selection
                for size in sizes:
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    app.refresh_state()
                    await pilot.pause()
                    assert app.progress_generation == generation and app.progress_selection == binding
                    assert app.activity_projection['admission'] == canonical_admission
                    svg = app.export_screenshot()
                    if renders:
                        (renders / f'{scenario}-{size[0]}x{size[1]}.svg').write_text(svg)
                    report['views'].append(layout_record(app, scenario, size, selected, svg))
                await controls(app, pilot, selected, opener, projection)
                report['controls'].append(scenario)
                await pilot.press('q')
        assert service._closed and not service._contexts and service._inflight == 0
    return report


def ui_polish_surface(root, *, renders=None, scenarios=SCENARIOS, sizes=SIZES):
    root = root.resolve()
    if renders:
        renders.mkdir(parents=True, exist_ok=True)
    store, cases = make_fixture(root)
    before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
    report = asyncio.run(console_cases(store, cases, renders=renders, scenarios=scenarios, sizes=sizes))
    after = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
    assert before == after, 'Synthetic native output or Jobs state changed during observation'
    report['source_and_state_unchanged'] = True
    report['issues'] = [{'scenario': view['scenario'], 'size': view['size'], 'issue': issue}
                        for view in report['views'] for issue in view['issues']]
    normal = next((view for view in report['views'] if view['scenario'] == 'vasp_active'), None)
    if normal is not None:
        for view in report['views']:
            if (view['scenario'] in ('ownership_uncertain', 'controller_offline')
                    and view['guard_color'] == normal['guard_color']):
                report['issues'].append({'scenario': view['scenario'], 'size': view['size'],
                                         'issue': 'Normal occupancy and genuine warning have identical styling'})
    return report
