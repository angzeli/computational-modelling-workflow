"""Synthetic read-only acceptance shared by source and installed-artifact checks."""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import threading
from unittest.mock import Mock, patch

from cmw.cli import main
from cmw.jobs.folder_opener import OpenResult
from cmw.jobs.progress import resolve_context
from cmw.jobs.store import Store
from cmw.progress.models import SourceDescriptor
from cmw.progress.service import ObservationService
from tests.jobs.progress_extension_fixture import ENGINE, TEXT, register
from tests.periodic.vasp.result_fixtures import native_texts, structure_text


def invoke(store, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(['jobs', '--state', str(store.root), *args])
    assert (code, err.getvalue()) == (0, ''), (code, out.getvalue(), err.getvalue())
    return json.loads(out.getvalue())


def make_cases(root):
    from tests.progress.orca_fixtures import output
    root.mkdir(parents=True)
    store = Store(root/'state')
    cases = []
    for engine in ('VASP', 'ORCA', ENGINE, 'UNKNOWN-EXTENSION'):
        directory = root/engine
        directory.mkdir()
        if engine == 'VASP':
            texts = native_texts(relaxation=True, evaluations=2, footer=False)
            lines = []
            for line in texts['OUTCAR'].splitlines():
                if 'reached required accuracy' in line:
                    continue
                lines.append(line)
                if 'energy  without entropy=' in line:
                    lines.append(' LOOP+: cpu time 5.0: real time 9.0')
            texts['OUTCAR'] = '\n'.join(lines)+'\n'
            texts['POSCAR'] = structure_text()
            roles = ('outcar', 'oszicar', 'poscar')
            sources = tuple(SourceDescriptor(role, str(directory/role.upper())) for role in roles)
            for role in roles:
                (directory/role.upper()).write_text(texts[role.upper()])
        elif engine == 'ORCA':
            source = directory/'declared.out'
            source.write_text(output(cycles=2))
            sources = (SourceDescriptor('orca_output', str(source)),)
        else:
            source = directory/'progress.synthetic'
            source.write_text(TEXT)
            sources = (SourceDescriptor('synthetic', str(source)),)
        # Recognized declaration only; this nonexistent runner is never executed.
        argv = ['/unexecuted/run-vasp.sh', '--input', str(directory), '--output', str(directory),
                '--binary', 'std', '--ranks', '1', '--ncore', '1', '--kpar', '1', '--mpi-mode', 'synthetic',
                '--restart', 'none', '--timeout', '10', '--stop-before', '0', '--managed-foreground']
        if engine == 'ORCA':
            target = directory/'target.json'
            target.write_text(json.dumps({'schema_version': 1, 'target': {'stage_type': 'OPT'}}))
            inputs = directory/'stage.inp'
            inputs.write_text('! HF STO-3G Opt\n# Synthetic declaration; never executed\n')
            argv = ['/unexecuted/run_orca.sh', '--input', str(inputs), '--target', str(target),
                    '--metadata', str(directory/'job.json'), '--output', str(directory/'declared.out'),
                    '--stderr', str(directory/'stage.err')]
        job = store.add(argv=argv, cwd=directory, name=engine+' literal [name]', engine=engine, hold=True)
        cases.append((job, sources))
    return store, cases


def protected_bytes(store, cases):
    return {path: path.read_bytes() for path in store.root.parent.rglob('*') if path.is_file()}


def assert_read_only(store, protected, files):
    assert all(path.read_bytes() == before for path, before in protected.items())
    assert {path for path in store.root.parent.rglob('*') if path.is_file()} == files
    state = store.snapshot()
    assert all(job['status'] == 'Hold' for job in state['jobs'])
    assert not state['controller']['online'] and not state['controller']['dispatch']
    assert all(job.get('group') is None and job.get('worker') is None for job in state['jobs'])


def cli_surface(store, cases):
    report = {}
    with patch('subprocess.Popen', side_effect=AssertionError('Observation must not launch processes')):
        for job, sources in cases:
            args = [item for source in sources for item in ('--source', source.role+'='+source.path)]
            if job['engine'] == 'ORCA':
                default = invoke(store, 'progress', job['display_id'], '--json')
                assert default['context']['sources'][0]['role'] == 'orca_output'
                assert default['context']['sources'][0]['path'] == sources[0].path
                assert default['context']['sources'][0]['stage'] == 'OPT'
                assert default['metrics'], default
            snapshot = invoke(store, 'progress', job['display_id'], *args, '--json')
            history = invoke(store, 'progress', job['display_id'], *args, '--history', '--json')
            assert not snapshot['context']['process_ownership']
            assert snapshot['scientific_validation'] == 'not-assessed'
            if job['engine'] == 'UNKNOWN-EXTENSION':
                assert snapshot['status'] == history['status'] == 'ADAPTER_UNAVAILABLE'
            else:
                assert snapshot['metrics'], snapshot
                assert history['entries'], history
                root = next(entry for entry in history['entries'] if entry['child_count'])
                children = invoke(store, 'progress', job['display_id'], *args, '--history', '--parent', root['entry_id'], '--json')
                assert children['entries']
                if job['engine'] == ENGINE:
                    inner = invoke(store, 'progress', job['display_id'], *args, '--history', '--parent', children['entries'][0]['entry_id'], '--json')
                    assert len(inner['entries'][0]['context']) == 3
                    assert inner['entries'][0]['metrics'][2]['unit'] == 'C/s'
            report[job['engine']] = snapshot['status']
        with patch('cmw.jobs.folder_opener.subprocess.run') as opener:
            opener.return_value = subprocess.CompletedProcess([], 0, '', '')
            job = cases[0][0]
            listing = invoke(store, 'files', job['display_id'], '--json')
            assert any(location['role'] == 'output' for location in listing['locations'])
            opener.assert_not_called()
            with patch('cmw.jobs.folder_opener.sys.platform', 'darwin'):
                opened = invoke(store, 'files', job['display_id'], '--open', '--role', 'output', '--json')
            assert opened['open']['status'] == 'OPEN_REQUESTED'
            assert opener.call_args.args[0] == ['/usr/bin/open', '--', job['cwd']]
            assert not opener.call_args.kwargs.get('shell', False)
    return report


def live_surface(store, cases):
    """One bounded fixture writer per native engine; observers never write."""
    report = {}
    for job, sources in cases[:2]:
        originals = {Path(source.path): Path(source.path).read_bytes() for source in sources}
        remainders = {}
        for path, content in originals.items():
            split = len(content)//2
            # A partial final line must wait for the writer's second flush.
            path.write_bytes(content[:split])
            remainders[path] = content[split:]
        release, done = threading.Event(), threading.Event()
        errors = []
        def writer():
            try:
                assert release.wait(5), 'Fixture writer was never released'
                for path, remainder in remainders.items():
                    with path.open('ab') as output:
                        output.write(remainder)
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()
        thread = threading.Thread(target=writer, name='cmw-synthetic-progress-writer')
        observer = ObservationService()
        context = resolve_context(store, job['display_id'], sources=sources)
        thread.start()
        try:
            first = observer.poll(context)
            assert first.status not in {'ADAPTER_ERROR', 'READ_ERROR'}
            release.set()
            assert done.wait(5), 'Fixture writer failed to finish'
            assert not errors, errors
            for _ in range(32):
                page = observer.history_page(context)
                if page.status != 'LOADING':
                    break
            final = observer.snapshot(context)
            assert final.metrics and page.entries, (final, page)
            assert all(source.bytes_read <= 65536+256 for source in final.sources)
            assert all(timing.value != 0 for row in page.entries for timing in row.timings
                       if timing.source.startswith('observer'))
            assert all(path.read_bytes() == content for path, content in originals.items())
            report[job['engine']] = 'split-byte buffered writer observed'
        finally:
            release.set()
            thread.join(timeout=6)
            observer.close()
        assert not thread.is_alive() and not observer._contexts and observer._inflight == 0
    return report


async def settled(app, pilot):
    for _ in range(600):
        view = app.history_view
        if not app.progress_busy and app.progress_snapshot is not None and (view is None or view.page is not None and view.page.status != 'LOADING'):
            await pilot.pause()
            return
        await asyncio.sleep(.01)
    raise AssertionError('Installed observation did not settle')


async def console_surface(store, cases, *, renders=None):
    from cmw.jobs.tui import JobsApp, NoteEditor
    from cmw.jobs.history_ui import ProgressHistory, HistoryDetails
    from textual.widgets import DataTable, Static, TextArea
    by_id = {job['id']: (() if job['engine'] == 'ORCA' else sources) for job, sources in cases}
    def resolver(selected_store, job_id, **kwargs):
        kwargs['sources'] = by_id[job_id]
        return resolve_context(selected_store, job_id, **kwargs)
    opener = Mock(return_value=OpenResult('OPEN_REQUESTED', 'Stubbed OS boundary', cases[0][0]['cwd']))
    service = ObservationService()
    app = JobsApp(store, progress_resolver=resolver, progress_service=service, file_opener=opener)
    with patch.object(JobsApp, 'collect_activity'), patch('subprocess.Popen', side_effect=AssertionError('No process launch')):
        async with app.run_test(size=(80, 24)) as pilot:
            await settled(app, pilot)
            table = app.query_one('#table', DataTable)
            assert list(table.columns)[:3] == ['id', 'files', 'name']
            assert 'literal [name]' in str(table.get_cell_at((0, 2)))
            for index, (job, _) in enumerate(cases):
                table.move_cursor(row=index, animate=False)
                await pilot.pause()
                await settled(app, pilot)
                assert app.selected_id == job['display_id']
                assert app.progress_snapshot.context.engine == job['engine']
                selection, generation = app.progress_selection, app.progress_generation
                for size in ((140, 40), (80, 24)):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    assert app.selected_id == job['display_id']
                    assert app.progress_generation == generation, 'Resize changed the selected observation identity'
                    assert app.progress_selection == selection and app.progress_snapshot is not None
                    assert app.progress_snapshot.context.engine == job['engine']
                    if job['engine'] != 'UNKNOWN-EXTENSION':
                        assert app.progress_snapshot.metrics
                    assert 'awaiting bounded observation' not in str(app.query_one('#progress', Static).render())
                card = str(app.query_one('#progress', Static).render())
                if job['engine'] == ENGINE:
                    assert 'Maximum component' in card and 'Charge flux' in card
                    assert 'Fmax' not in card
                if renders is not None:
                    (renders/f'{job["engine"]}-card-80x24.svg').write_text(app.export_screenshot())
                await pilot.press('g')
                await settled(app, pilot)
                assert isinstance(app.screen, ProgressHistory)
                view = app.screen
                selected_entry = view.selected_entry()
                for size in ((140, 40), (80, 24)):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    assert app.screen is view and not view.expired
                    assert app.progress_selection == selection and app.progress_generation == generation
                    assert view.selected_entry() == selected_entry
                if job['engine'] == 'UNKNOWN-EXTENSION':
                    assert app.screen.page.status == 'ADAPTER_UNAVAILABLE'
                else:
                    assert app.screen.page.entries
                    if renders is not None:
                        (renders/f'{job["engine"]}-history-80x24.svg').write_text(app.export_screenshot())
                    expandable = next(index for index, entry in enumerate(app.screen.page.entries) if entry.child_count)
                    app.screen.query_one(DataTable).move_cursor(row=expandable, animate=False)
                    await pilot.pause()
                    await pilot.press('enter')
                    await settled(app, pilot)
                    assert len(app.screen.selected_entry().context) >= 2
                    if job['engine'] == ENGINE:
                        await pilot.press('enter')
                        await settled(app, pilot)
                        assert len(app.screen.selected_entry().context) == 3
                    await pilot.press('d')
                    assert isinstance(app.screen, HistoryDetails)
                    detail = app.screen
                    for size in ((140, 40), (80, 24)):
                        await pilot.resize_terminal(*size)
                        await pilot.pause()
                        assert app.screen is detail and not view.expired
                        assert app.progress_selection == selection and app.progress_generation == generation
                    assert 'Source references:' in str(app.screen.query_one('#history-detail', Static).render())
                    await pilot.press('escape')
                await pilot.press('escape')
            # Rapid switching uses the same single observer worker and retains final identity.
            for job, _ in cases * 3:
                app.selected_id = job['display_id']
                app.sync_progress_selection()
            await settled(app, pilot)
            assert app.progress_snapshot.context.engine == cases[-1][0]['engine']
            assert service._inflight <= 1
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await settled(app, pilot)
            await pilot.press('n', 'f', 'g', 'p', 'o')
            assert isinstance(app.screen, NoteEditor)
            assert app.screen.query_one(TextArea).text == 'fgpo'
            opener.assert_not_called()
            await pilot.press('escape', 'f')
            for _ in range(200):
                if not app.files_busy:
                    break
                await asyncio.sleep(.01)
            opener.assert_called_once()
            await pilot.press('g')
            await settled(app, pilot)
            await pilot.press('ctrl+c')
    assert service._closed and not service._contexts and service._inflight == 0
    return {'all_four_cards_and_history': 'PASS', 'three_level_drilldown': 'PASS',
            'rapid_selection_note_literal_files_detach': 'PASS', 'resize_card_history_details_identity': 'PASS', 'reader_contexts_after_close': 0,
            'gui_opener': 'stubbed, no Finder check'}


def progress_surface(root, *, console=False, renders=None):
    from cmw.progress.registry import _REGISTRY
    with patch.dict(_REGISTRY):
        register()
        store, cases = make_cases(root)
        protected = protected_bytes(store, cases)
        files = {path for path in root.rglob('*') if path.is_file()}
        report = {'cli': cli_surface(store, cases), 'native_writers': live_surface(store, cases),
                  'files_open_boundary': 'PASS (stubbed OS call)'}
        if console:
            report['console'] = asyncio.run(console_surface(store, cases, renders=renders))
        assert_read_only(store, protected, files)
    report['source_and_state_unchanged'] = True
    return report
