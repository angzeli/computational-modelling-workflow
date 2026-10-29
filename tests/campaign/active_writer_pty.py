"""Separate 60-second native catalogue gate with one benign heartbeat writer.

Twelve three-page cycles cover 120 held synthetic VASP rows. Only the extra
Python waiter may execute. Search, page, Details and return use terminal input;
control messages observe state and geometry. This is additional qualification,
never a replacement for the original 240-second history gate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

ROWS, CYCLES, PAGE_ROWS, WALL_SECONDS = 120, 12, 50, 60


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=str)+'\n')


def bounded(predicate, deadline):
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError('Active-writer condition deadline exceeded')


def admission_diagnostic(store, writer_id):
    snapshot = store.snapshot()
    keys = ('id', 'attempt_id', 'order', 'status', 'reason', 'worker', 'claim', 'started_at')
    jobs = sorted(snapshot['jobs'], key=lambda job: (job.get('order') is None, job.get('order') or 0))
    return {'writer_id': writer_id, 'controller': snapshot['controller'],
            'queue': [{key: job.get(key) for key in keys} for job in jobs],
            'events': snapshot.get('events', []), 'observed_at': time.monotonic()}


def run(directory):
    from cmw.jobs import runtime
    from cmw.jobs.store import Store
    from tests.campaign.closure_pty import monitored
    from tests.campaign.closure_workflows import configure_children, installed_origin
    from tests.campaign.pty import Session
    from tests.jobs.isolated_runtime import install
    from tests.jobs.test_catalogue_store import populate_history

    root = Path(directory).resolve(); root.mkdir(mode=0o700)
    report = {'bounds': {'workload_seconds': WALL_SECONDS, 'cleanup_seconds': 30,
        'history_rows': ROWS, 'cycles': CYCLES, 'page_rows': PAGE_ROWS, 'benign_payloads': 1},
        'origin': installed_origin(), 'cycles': [], 'passed': False,
        'visual_inspection_required': True, 'scientific_acceptance_claimed': False}
    started = time.monotonic(); deadline = started+WALL_SECONDS
    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('Active-writer gate deadline')))
    signal.alarm(WALL_SECONDS)
    session = None; store = None; writer = None; cleanup_started = None
    finish = root/'finish-benign-writer'
    configure_children()
    try:
        with monitored(root, 'active-writer-pty-parent'), install():
            try:
                store = Store(root/'state')
                held = store.add(argv=['/unexecuted/synthetic-vasp'], cwd=root,
                                 name='Synthetic 00000', engine='VASP', hold=True)
                populate_history(store, root, ROWS, first=held)
                expected = [(job['id'], job['attempt_id']) for job in store.snapshot()['jobs']]
                code = 'from pathlib import Path; import time\np=Path('+repr(str(finish))+')\nwhile not p.exists(): time.sleep(.05)'
                writer = store.add(argv=[sys.executable, '-I', '-B', '-c', code], cwd=root,
                                   name='benign heartbeat writer', hold=True)
                # Supported fixture ordering: held queue heads block later jobs.
                store.change(writer['id'], 'move', position=1)
                store.change(writer['id'], 'release')
                report['admission_before_start'] = admission_diagnostic(store, writer['id'])
                runtime.start(store)
                try:
                    bounded(lambda: any(job['id'] == writer['id'] and job['status'] == 'Run'
                                        for job in store.snapshot()['jobs']), min(deadline, time.monotonic()+10))
                finally:
                    report['admission_after_wait'] = admission_diagnostic(store, writer['id'])
                    save(root/'result.json', report)
                report['run_gate_reached'] = True
                settings = root/'settings.json'
                save(settings, {'state': str(store.root), 'expected': expected})
                session = Session(settings, root/'pty', module='tests.campaign.active_writer_pty', deadline=deadline)
                session.resize((140, 40))
                assert session.request('ready')['tty'] == [True, True]

                def send(data):
                    count = session.request('input_count')
                    session.send(data)
                    session.request('input_received', after=count)

                def click(selector):
                    x, y = session.request('position', selector=selector)
                    send(f'\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m'.encode())

                for cycle in range(CYCLES):
                    before = store.snapshot()['controller']['heartbeat']
                    send(b'/'); session.request('search')
                    send(b'\x01\x0bSynthetic')
                    session.request('search_text', text='Synthetic')
                    click('#catalogue-apply')
                    pages = []; generation = None
                    for offset in (0, 50, 100):
                        page = session.request('page', offset=offset)
                        assert page['targets'] == [list(value) for value in expected[offset:offset+PAGE_ROWS]]
                        assert page['next_cursor'] is not None if offset < 100 else page['next_cursor'] is None
                        if generation is None:
                            generation = page['generation']
                            bounded(lambda: store.snapshot()['controller']['heartbeat'] > before,
                                    min(deadline, time.monotonic()+4))
                        assert page['generation'] == generation
                        # Native row click selects the first visible held attempt.
                        click('#first-row')
                        selected = session.request('inspect', target=list(expected[offset]))
                        send(b'\x1b'); session.request('returned', target=selected)
                        send(b't'); detail = session.request('details', target=selected)
                        if cycle in (0, CYCLES-1):
                            detail['capture'] = session.request('capture', name=f'cycle-{cycle}-page-{offset}-details')
                        send(b'\x1b')
                        returned = session.request('returned', target=selected)
                        assert returned['selected'] == selected
                        if cycle in (0, CYCLES-1):
                            page['capture'] = session.request('capture', name=f'cycle-{cycle}-page-{offset}-returned')
                        pages.append({'page': page, 'details': detail, 'returned': returned})
                        if offset < 100:
                            send(b'/'); session.request('search')
                            click('#catalogue-owner')
                            send(b'\x1b[B\r')
                            session.request('owner_selected')
                            click('#catalogue-next')
                    snapshot = store.snapshot()
                    after = snapshot['controller']['heartbeat']
                    assert after > before
                    assert all(job['status'] == 'Hold' and job['started_at'] is None
                               for job in snapshot['jobs'] if job['id'] != writer['id'])
                    assert next(job for job in snapshot['jobs'] if job['id'] == writer['id'])['status'] == 'Run'
                    report['cycles'].append({'cycle': cycle, 'heartbeat_before': before,
                                             'heartbeat_after': after, 'pages': pages})
                    save(root/'result.json', report)
                assert len(report['cycles']) == CYCLES
                report['workload_seconds'] = time.monotonic()-started
                assert report['workload_seconds'] <= WALL_SECONDS
            except BaseException as exc:
                report['primary_error'] = type(exc).__name__+': '+str(exc)
                if store is not None and writer is not None:
                    try:
                        report['admission_at_failure'] = admission_diagnostic(store, writer['id'])
                    except BaseException as diagnostic_error:
                        report['admission_diagnostic_error'] = repr(diagnostic_error)
                raise
            finally:
                # The cleanup deadline is separate, includes all nested helpers,
                # and never rescues a failed/timed-out workload.
                signal.alarm(0); cleanup_started = time.monotonic(); signal.alarm(30)
                cleanup_errors = []
                if session is not None:
                    try:
                        report['pty_returncode'] = session.close()
                    except BaseException as exc:
                        cleanup_errors.append(repr(exc))
                if store is not None and writer is not None:
                    finish.touch()
                    try:
                        current = next(job for job in store.snapshot()['jobs'] if job['id'] == writer['id'])
                        if current['status'] in {'Hold', 'Queue'} and current['started_at'] is None:
                            report['writer_not_started'] = current
                        snapshot = bounded(lambda: (value if any(job['id'] == writer['id'] and job['status'] in {'Done','Fail','Unknown'}
                            for job in value['jobs']) else None) if (value := store.snapshot()) else None,
                            cleanup_started+10) if not report.get('writer_not_started') else None
                        if snapshot is not None:
                            completed = next(job for job in snapshot['jobs'] if job['id'] == writer['id'])
                            receipt = store.root/'attempts'/writer['attempt_id']/'payload-exit.json'
                            report['writer'] = {'job': completed, 'receipt': json.loads(receipt.read_text())}
                            assert completed['status'] == 'Done' and report['writer']['receipt']['exit_code'] == 0
                    except BaseException as exc:
                        cleanup_errors.append(repr(exc))
                    try:
                        runtime.stop(store)
                        bounded(lambda: not store.snapshot()['controller']['online'], cleanup_started+20)
                        for child in tuple(runtime._CHILDREN):
                            assert child.wait(timeout=max(.01, cleanup_started+23-time.monotonic())) == 0
                        runtime.reap_detached()
                    except BaseException as exc:
                        cleanup_errors.append(repr(exc))
                report['cleanup_errors'] = cleanup_errors
                save(root/'result.json', report)
                assert not cleanup_errors, cleanup_errors
        assert report['pty_returncode'] == 0
        child = json.loads((root/'pty'/'ownership.json').read_text())
        parent_owned = json.loads((root/'ownership.json').read_text())['ownership']
        assert child['passed'], 'PTY child ownership coverage failed'
        report['child_ownership_passed'] = True
        helpers = [(path, json.loads(path.read_text())) for path in sorted(root.rglob('*.ownership.json'))]
        ledgers = [parent_owned, child['ownership'], *(owned for _, owned in helpers)]
        direct = [row for ledger in ledgers for row in ledger['records'] if row['capture_origin'] == 'direct-launch']
        for path, owned, origin_path in [(root/'pty'/'ownership.json', child['ownership'], root/'pty'/'origin.json'),
                *((path, owned, path.with_suffix('.origin.json')) for path, owned in helpers)]:
            frontend, = [row for row in owned['records'] if row['capture_origin'] == 'explicit-root']
            assert any(all(row[key] == frontend[key] for key in ('host','boot','pid','birth'))
                and row.get('exit', {}).get('returncode') == 0
                and row['exit']['exit_evidence'] == 'original Popen wait/reap' for row in direct), str(path)
            origin = json.loads(origin_path.read_text())
            assert origin['python'] == report['origin']['python'] and origin['package'] == report['origin']['package']
        for path, owned in helpers:
            assert owned['outcomes']['execution_passed'], str(path)
        from tests.campaign.support import BUDGETS
        ui = json.loads((root/'pty'/'input.json').read_text())
        assert not ui['control_thread_alive'] and ui['native_inputs']
        assert ui['tick_intervals']['p95'] <= BUDGETS['ui_tick_p95_seconds']
        assert ui['render_seconds']['p95'] <= BUDGETS['ui_render_p95_seconds']
        assert session is not None and 0 < session.output_bytes <= 8*1024*1024
        final = store.snapshot()
        assert all(job['status'] == 'Hold' and job['started_at'] is None
                   and not (store.root/'attempts'/job['attempt_id']/'payload-exit.json').exists()
                   for job in final['jobs'] if job['id'] != writer['id'])
        report['held_rows_unchanged'] = ROWS
        report['passed'] = True
    except BaseException as exc:
        report['error'] = type(exc).__name__+': '+str(exc)
        raise
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, previous)
        report['elapsed_seconds'] = time.monotonic()-started
        report['cleanup_seconds'] = time.monotonic()-cleanup_started if cleanup_started is not None else None
        from tests.campaign.closure_workflows import observation_rollup
        report.update(observation_rollup(root, report['passed']))
        save(root/'result.json', report)
    return report


def child(settings, directory):
    from cmw.jobs.activity import Observer
    from cmw.jobs.catalogue_ui import CatalogueDetails, CatalogueSearch
    from cmw.jobs.store import Store
    from cmw.jobs.tui import Inspect, JobsApp
    from textual import events
    from textual.containers import Vertical
    from textual.widgets import Button, DataTable, Input, Select
    from tests.campaign.closure_pty import monitored
    from tests.campaign.closure_workflows import installed_origin
    from tests.jobs.isolated_runtime import fixture_collect
    from tests.campaign.support import summary
    settings = json.loads(Path(settings).read_text()); directory = Path(directory)
    save(directory/'origin.json', installed_origin())

    class App(JobsApp):
        def __init__(self):
            self.inputs = []; self.controls = []
            self.ticks = []; self.renders = []; self.last_tick = None
            super().__init__(Store(Path(settings['state'])), observer=Observer(collector=fixture_collect))

        def on_ready(self):
            self.set_interval(.05, self.record_tick)
            self.control_thread = threading.Thread(target=self.control, daemon=True)
            self.control_thread.start()

        def record_tick(self):
            now = time.perf_counter()
            if self.last_tick is not None: self.ticks.append(now-self.last_tick)
            self.last_tick = now

        def refresh_state(self):
            started = time.perf_counter()
            try: return super().refresh_state()
            finally: self.renders.append(time.perf_counter()-started)

        async def on_event(self, event):
            if isinstance(event, (events.Key, events.MouseUp, events.MouseScrollUp, events.MouseScrollDown)) and not event.is_forwarded:
                self.inputs.append({'type': type(event).__name__, 'key': getattr(event, 'key', None), 'at': time.monotonic()})
            await super().on_event(event)

        async def until(self, predicate):
            deadline = time.monotonic()+4
            while not predicate():
                assert time.monotonic() < deadline, 'UI observation deadline'
                await asyncio.sleep(.025)

        def selected_identity(self):
            job = self.selected()
            return [job['id'], job['attempt_id']] if job else None

        def table_geometry(self):
            table = self.screen_stack[0].query_one('#table', DataTable)
            rows = table.ordered_rows
            key = rows[table.cursor_row].key.value if rows and table.cursor_row < len(rows) else None
            cursor = self.navigation_row_identity(table, key) if key else None
            top = int(table.scroll_y)
            visible = rows[top:top+max(0, table.scrollable_content_region.height-table.header_height)]
            return {'region': list(table.region), 'content_region': list(table.content_region),
                'scroll': [table.scroll_x, table.scroll_y], 'scroll_target': [table.scroll_target_x, table.scroll_target_y],
                'cursor_key': key, 'cursor_identity': cursor,
                'ordered': [self.navigation_row_identity(table,row.key.value) for row in rows],
                'visible': [self.navigation_row_identity(table,row.key.value) for row in visible]}

        def check_cursor(self, target):
            geometry = self.table_geometry()
            assert geometry['cursor_identity'] is not None and list(geometry['cursor_identity'][-2:]) == target, geometry
            assert any(list(value[-2:]) == target for value in geometry['visible']), geometry
            assert geometry['scroll'] == geometry['scroll_target'], geometry
            return geometry

        def ui_state(self):
            # Observe the actual screen and immutable selection without refreshing.
            screen = self.screen
            shell = [widget for widget in screen.query(Vertical) if widget.parent is screen]
            widgets = [*shell, *screen.query(Button)] if screen is not self.screen_stack[0] else [screen.query_one('#table', DataTable)]
            return {'widgets': {widget.id or type(widget).__name__: {'region': list(widget.region),
                        'content_region': list(widget.content_region)} for widget in widgets},
                'table': self.table_geometry(), 'terminal': list(os.get_terminal_size(1)), 'app': list(self.size),
                'screen': list(screen.size), 'class': type(screen).__name__,
                'selected': self.selected_identity(), 'pending_layout': bool(screen._layout_required),
                'pending_restores': len(self._navigation_restores),
                'painted': [strip.text for strip in screen._compositor.render_strips()]}

        async def command(self, command):
            operation = command['operation']; self.controls.append(command)
            if operation == 'input_count': return len(self.inputs)
            if operation == 'input_received':
                await self.until(lambda: len(self.inputs) > command['after'])
                await asyncio.wait_for(self.screen.wait_for_refresh(), 2)
                return self.inputs[-1]
            if operation == 'ready':
                await self.until(lambda: len(self.row_ids) == ROWS+1)
                return {'tty': [sys.stdin.isatty(), sys.stdout.isatty()]}
            if operation == 'search':
                await self.until(lambda: isinstance(self.screen, CatalogueSearch)
                                 and self.screen.is_mounted and self.screen.query_one('#catalogue-text', Input).has_focus)
                return True
            if operation == 'search_text':
                await self.until(lambda: self.screen.query_one('#catalogue-text', Input).value == command['text'])
                return True
            if operation == 'owner_selected':
                await self.until(lambda: self.screen.query_one('#catalogue-owner', Select).value == 'LOCAL')
                return True
            if operation == 'position':
                selector = command['selector']
                widget = self.screen.query_one('#table' if selector == '#first-row' else selector)
                await asyncio.wait_for(self.screen.wait_for_refresh(), 2)
                region = widget.content_region
                assert widget.display and not getattr(widget, 'disabled', False)
                if selector == '#first-row':
                    # Use the actual ID cell; Files has a different click action.
                    coordinate = widget.get_cell_coordinate(widget.ordered_rows[0].key, 'id')
                    cell = widget._get_cell_region(coordinate)
                    x = region.x+cell.x-int(widget.scroll_x)+cell.width//2
                    y = region.y+cell.y-int(widget.scroll_y)+cell.height//2
                    assert region.x <= x < region.right and region.y <= y < region.bottom
                    self.controls[-1]['observed_cell'] = {'column': 'id', 'cell_region': list(cell),
                                                          'terminal_position': [x+1,y+1]}
                    return [x+1,y+1]
                return [region.x+region.width//2+1, region.y+region.height//2+1]
            if operation == 'page':
                offset = command['offset']; expected = settings['expected'][offset:offset+PAGE_ROWS]
                await self.until(lambda: not self.catalogue_busy and not self._navigation_restores
                    and (page := self.catalogue_pages.get('LOCAL')) is not None
                    and [[row.job_id,row.attempt_id] for row in page.records] == expected)
                assert not self.catalogue_errors
                page = self.catalogue_pages['LOCAL']
                assert page.matching_count == ROWS and page.total_count == ROWS+1
                geometry = self.table_geometry()
                assert [list(value[-2:]) for value in geometry['ordered']] == expected, geometry
                return {'targets': expected, 'table': geometry, 'generation': page.generation, 'next_cursor': page.next_cursor,
                        'matching_count': page.matching_count, 'total_count': page.total_count}
            if operation == 'inspect':
                await self.until(lambda: isinstance(self.screen, Inspect) and self.selected_identity() == command['target'])
                return self.selected_identity()
            if operation in {'selected', 'returned'}:
                await self.until(lambda: self.screen is self.screen_stack[0] and not self._navigation_restores
                                 and self.selected_identity() == command['target'])
                geometry = self.check_cursor(command['target'])
                return self.selected_identity() if operation == 'selected' else {'selected': self.selected_identity(), 'table': geometry}
            if operation == 'details':
                await self.until(lambda: isinstance(self.screen, CatalogueDetails) and self.screen.record is not None)
                record = self.screen.record
                assert [record.job_id,record.attempt_id] == command['target'] == self.selected_identity()
                return {'selected': self.selected_identity(), 'record': record.to_dict()}
            if operation == 'capture':
                name = command['name']; before = self.ui_state()
                (directory/(name+'-transition.svg')).write_text(self.export_screenshot())
                started = time.monotonic(); previous = None
                while True:
                    state = self.ui_state()
                    geometry_fits = all(0 <= (r := widget['region'])[0] and 0 <= r[1]
                        and r[0]+r[2] <= 140 and r[1]+r[3] <= 40 for widget in state['widgets'].values())
                    signature = (state['terminal'],state['app'],state['screen'],state['class'],state['selected'],
                                 state['widgets'], state['table'])
                    if (geometry_fits and not state['pending_layout'] and not state['pending_restores']
                            and state['terminal'] == state['app'] == state['screen'] == [140,40]
                            and signature == previous): break
                    previous = signature
                    assert time.monotonic()-started < 2, state
                    await asyncio.wait_for(self.screen.wait_for_refresh(), 2-(time.monotonic()-started))
                (directory/(name+'.svg')).write_text(self.export_screenshot())
                record = {'transition': before, 'settled': state, 'transition_image': name+'-transition.svg', 'image': name+'.svg'}
                save(directory/(name+'.json'), record)
                return record
            raise ValueError(operation)

        def control(self):
            with (os.fdopen(int(os.environ['CMW_CAMPAIGN_IN'])) as reader,
                  os.fdopen(int(os.environ['CMW_CAMPAIGN_OUT']), 'w', buffering=1) as writer):
                for line in reader:
                    try: reply = {'ok': True, 'result': self.call_from_thread(self.command,json.loads(line))}
                    except Exception as exc:
                        reply = {'ok': False, 'error': repr(exc)}
                        name = 'command-'+str(len(self.controls))+'-failure'
                        try:
                            (directory/(name+'.svg')).write_text(self.call_from_thread(self.export_screenshot))
                            save(directory/(name+'.json'), {'command': json.loads(line), 'error': repr(exc),
                                'geometry': self.call_from_thread(self.ui_state), 'image': name+'.svg'})
                        except Exception as capture_error:
                            save(directory/(name+'.json'), {'command': line, 'error': repr(exc), 'capture_error': repr(capture_error)})
                    writer.write(json.dumps(reply)+'\n')

    with monitored(directory, 'active-writer-pty-child'):
        app = App()
        try: app.run(mouse=True)
        finally:
            thread = getattr(app,'control_thread',None)
            if thread is not None: thread.join(timeout=2)
            save(directory/'input.json', {'native_inputs':app.inputs,'controls':app.controls,
                'control_thread_alive':thread is not None and thread.is_alive(),
                'tick_intervals':summary(app.ticks),'render_seconds':summary(app.renders)})
            assert thread is None or not thread.is_alive()


if __name__ == '__main__':
    if len(sys.argv) == 3:
        child(Path(sys.argv[1]), Path(sys.argv[2]))
    else:
        parser = argparse.ArgumentParser(); parser.add_argument('output', type=Path)
        run(parser.parse_args().output)
