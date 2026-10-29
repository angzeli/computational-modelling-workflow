"""Additional native-terminal evidence for completed private workflow fixtures.

Call run(settings, new_directory) inside the 120 s owned-run budget. Settings is
closure_workflows' pty_cases mapping (or a report containing that mapping).
No fixture receipts, native outputs, product timers or refreshes are fabricated.
The parent sends real terminal keys/mouse reports; controls only observe, pin the
initial immutable target and bound review input limits. Every capture preserves
its transition even if settlement fails. SVGs still require independent visual
inspection; a successful driver is not visual or scientific qualification.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import threading
import time

from tests.campaign.geometry import GeometryProbe, RESIZE_SEQUENCE
from tests.campaign.pty import Session
from tests.campaign.support import BUDGETS, OBSERVATION_RULES, ProcessLedger, summary
from tests.campaign.qualification_v2 import outcomes

BUDGET_SECONDS = 120
FRAME_SECONDS = 2.0
CONDITION_SECONDS = 8.0


def write(path, value):
    path.write_text(json.dumps(value, indent=2, default=str)+'\n')


def contains(text, token):
    # A terminal may wrap a pinned UUID/digest inside the token.
    return token in text or ''.join(token.split()) in ''.join(text.split())



@contextmanager
def monitored(directory, label):
    """Record direct launches and classify sampled descendants without quieting UI."""
    ledger = ProcessLedger(label)
    stopped = threading.Event()
    report = {'samples': [], 'sampling_errors': [], 'observation_rules': OBSERVATION_RULES}
    def sample():
        while not stopped.wait(OBSERVATION_RULES['sample_cadence_seconds']):
            try:
                report['samples'].append(ledger.sample())
            except BaseException as exc:
                report['sampling_errors'].append(repr(exc))
                return
    sampler = threading.Thread(target=sample, name='closure-pty-resource-sampler', daemon=True)
    body_failed = False
    with ledger:
        try:
            report['samples'].append(ledger.sample())
            sampler.start()
            yield report
        except BaseException:
            body_failed = True
            raise
        finally:
            stopped.set()
            if sampler.ident is not None:
                sampler.join(timeout=2)
            ledger.finish_sampling()
            try:
                report['remaining'] = ledger.cleanup(time.monotonic()+5)
            except BaseException as exc:
                report['cleanup_error'] = repr(exc)
            finally:
                report['ownership'] = ledger.report()
                report['sampler_stopped'] = not sampler.is_alive()
                report['sample_cadence_seconds'] = summary([b['at']-a['at']
                    for a, b in zip(report['samples'], report['samples'][1:])])
                resources = [row for sample in report['samples'] for row in sample['processes']]
                report['resource_bounds_passed'] = all(('rss_bytes' not in row or row['rss_bytes'] <= BUDGETS['process_rss_bytes'])
                    and ('fds' not in row or row['fds'] <= BUDGETS['process_fds']) for row in resources)
                report['peak_owned_subprocesses'] = max((sum(row['pid'] != os.getpid()
                    for row in sample['processes']) for sample in report['samples']), default=0)
                observation = report['ownership']
                if report['sampling_errors']:
                    observation.update(observation_accounting='FAIL', resource_observability='RESOURCE_QUALIFICATION_INCOMPLETE')
                report['outcomes'] = outcomes(functional_passed=not body_failed,
                    safety_passed=observation['control_identity_safety'] == 'PASS',
                    cleanup_passed=not report.get('remaining') and 'cleanup_error' not in report
                        and not observation['cleanup_errors'] and report['sampler_stopped'],
                    observation=observation, resource_limits_passed=report['resource_bounds_passed']
                        and report['peak_owned_subprocesses'] <= BUDGETS['owned_subprocesses'])
                report['passed'] = report['outcomes']['execution_passed']
                report['qualified'] = report['outcomes']['qualified']
                write(directory/'ownership.json', report)
    assert report['passed'], report


def run(settings_path, directory):
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700)
    with monitored(directory, 'closure-pty-parent'):
        return run_cases(settings_path, directory)


def run_cases(settings_path, directory):
    settings = json.loads(Path(settings_path).read_text())
    cases = settings.get('pty_cases', settings)
    assert set(cases) >= {'study', 'analysis'}, 'Both genuine workflow cases are required'
    started = time.monotonic()
    deadline = started+BUDGET_SECONDS
    report = {'schema': 1, 'budget_seconds': BUDGET_SECONDS, 'native_screenshots': [],
              'settings': str(Path(settings_path).resolve()), 'cases': {},
              'visual_inspection_required': True, 'scientific_acceptance_claimed': False}
    try:
        for kind, view_mode in ((kind, view) for kind in ('study', 'analysis') for view in ('local', 'nodes')):
            assert time.monotonic() < deadline, 'Extra PTY budget exhausted'
            case = cases[kind]
            assert all(key in case for key in ('state', 'job_id', 'attempt_id'))
            label = kind+'-'+view_mode
            case_settings = directory/(label+'-settings.json')
            write(case_settings, {**case, 'kind': kind, 'view': view_mode})
            child_dir = directory/label
            session = Session(case_settings, child_dir, module='tests.campaign.closure_pty', deadline=deadline)
            case_report = report['cases'][label] = {'fixture': case, 'frames': []}
            def request(operation, **values):
                return session.request(operation, **values)
            def send(keys):
                before = request('input_count')
                session.send(keys)
                request('input_received', after=before)
            def capture(name, view, size):
                frame = request('capture', name=name, view=view, size=list(size))
                case_report['frames'].append(frame)
                report['native_screenshots'].append({**frame, 'directory': str(child_dir)})
                return frame
            def cycle(view):
                for index, size in enumerate(RESIZE_SEQUENCE):
                    session.resize(size)
                    capture(f'{view}-{index}-{size[0]}x{size[1]}', view, size)
                    if view != 'analysis':
                        # Exercise ordinary keyboard scroll after a settled refresh.
                        send(b'\x1b[6~')
                        capture(f'{view}-{index}-pagedown', view, size)
                        send(b'\x1b[5~')
                        request('ready_view', view=view)
                    selector = {'catalogue': '#catalogue-details-scroll', 'study': '#study-scroll',
                                'review': '#review-scroll', 'doctor': '#copy-text', 'analysis': '#table'}[view]
                    x, y = request('position', selector=selector)
                    send(f'\x1b[<64;{x};{y}M'.encode())
                    capture(f'{view}-{index}-mouse-scroll', view, size)
            def click(selector):
                x, y = request('position', selector=selector)
                send(f'\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m'.encode())
            try:
                case_report['ready'] = request('ready')
                assert case_report['ready']['tty'] == [True, True]
                if kind == 'analysis':
                    cycle('analysis')
                else:
                    send(b't')
                    request('ready_view', view='catalogue')
                    cycle('catalogue')
                    click('#catalogue-study')
                    request('ready_view', view='study')
                    cycle('study')
                    click('#study-back')
                    request('ready_view', view='catalogue')
                    click('#catalogue-detail-back')
                    request('ready_view', view='base')
                    send(b'r')
                    request('review_limits')
                    click('#review-inspect')
                    case_report['review'] = request('review_complete')
                    cycle('review')
                    send(b'y')
                    request('ready_view', view='review-copy')
                    capture('review-frozen-text', 'review-copy', (140, 40))
                    send(b'\x1b')
                    request('ready_view', view='review')
                    send(b'\x1b')
                    request('ready_view', view='base')
                    send(b'\r')
                    request('ready_view', view='details')
                    send(b'd')
                    case_report['doctor'] = request('ready_view', view='doctor')
                    cycle('doctor')
                    send(b'\x1b')
                    request('ready_view', view='details')
                    send(b'\x1b')
                    request('ready_view', view='base')
                # Frozen observation and explicit mouse Refresh retain the target.
                send(b'y')
                request('ready_view', view='copy')
                capture('copy-before-refresh', 'copy', (140, 40))
                click('#copy-refresh')
                capture('copy-after-mouse-refresh', 'copy', (140, 40))
                send(b'\x1b')
                case_report['returned'] = request('ready_view', view='base')
                assert case_report['returned']['selected'] == case_report['ready']['selected']
                session.send(b'q')
            finally:
                try:
                    case_report['returncode'] = session.close()
                except BaseException as exc:
                    case_report['cleanup_error'] = repr(exc)
                    raise
                finally:
                    case_report['terminal_bytes'] = session.output_bytes
                    metrics = child_dir/'metrics.json'
                    if metrics.exists():
                        case_report['metrics'] = json.loads(metrics.read_text())
                    write(directory/'result.json', report)
            assert case_report['returncode'] == 0, case_report
            assert case_report['terminal_bytes'] > 0
            assert not case_report['metrics']['control_thread_alive']
            assert all(sample['outcome'] == 'ok' for sample in case_report['metrics']['requests'])
            for field, bound in (('ui_tick_intervals', 'ui_tick_p95_seconds'),
                                 ('ui_render_seconds', 'ui_render_p95_seconds')):
                assert case_report['metrics'][field]['p95'] <= BUDGETS[bound], case_report['metrics']
        report['driver_passed'] = True
    except BaseException as exc:
        report['driver_passed'] = False
        report['error'] = type(exc).__name__+': '+str(exc)
        raise
    finally:
        report['elapsed_seconds'] = time.monotonic()-started
        if report['elapsed_seconds'] > BUDGET_SECONDS:
            report['driver_passed'] = False
            report['budget_exceeded'] = True
        write(directory/'result.json', report)
    assert report['driver_passed'], report
    return report


def child(settings_path, directory):
    import cmw
    from cmw.doctor.presentation import DoctorDetails, DoctorReport
    from cmw.jobs.activity import Observer
    from cmw.jobs.catalogue_ui import CatalogueDetails
    from cmw.jobs.copy_ui import CopyPreview, FrozenText
    from cmw.jobs.review_ui import ReviewScreen
    from cmw.jobs.store import Store
    from cmw.jobs.study_ui import StudyScreen
    from cmw.jobs.tui import Inspect, JobsApp
    from cmw.nodes.connections import ConnectedBackend
    from cmw.nodes.portal import NodesApp, row_key, target_key
    from tests.campaign.support import MeterTransport
    from textual import events
    from textual.containers import Vertical
    from textual.widgets import Button, DataTable, Input, Static, TextArea
    from tests.jobs.isolated_runtime import fixture_collect

    case = json.loads(settings_path.read_text())
    store = Store(Path(case['state']))
    job = next(row for row in store.snapshot()['jobs'] if row['id'] == case['job_id'])
    assert job['attempt_id'] == case['attempt_id'], 'Fixture attempt drifted'
    kind, view_mode = case['kind'], case['view']
    client = None
    if view_mode == 'nodes':
        assert case.get('entry') and case.get('command') and case.get('target'), 'Original enrolled endpoint required'
        client = ConnectedBackend('WORKFLOW', case['entry'], transport=MeterTransport(case['command'], timeout=8))
    assert job['engine'] == ('Multiwfn' if kind == 'analysis' else 'VASP')
    if kind == 'analysis':
        assert case.get('literal_text'), 'Analysis requires fixture-derived literal field expectations'
    else:
        assert all(case.get(key) for key in ('study_id', 'plan_digest', 'series', 'name'))

    class App(JobsApp if view_mode == 'local' else NodesApp):
        def __init__(self):
            self.geometry = GeometryProbe()
            self.intervals, self.renders, self.controls, self.native_inputs = [], [], [], []
            self.last_tick = None
            if view_mode == 'local':
                super().__init__(store, observer=Observer(collector=fixture_collect))
            else:
                super().__init__({'WORKFLOW': client}, request_root=directory/'requests')

        def on_ready(self):
            self.set_interval(.05, self.record_tick)
            self.control_thread = threading.Thread(target=self.control, daemon=True)
            self.control_thread.start()

        async def on_event(self, event):
            if isinstance(event, (events.Key, events.MouseUp, events.MouseScrollUp, events.MouseScrollDown)) and not event.is_forwarded:
                self.native_inputs.append({'event': type(event).__name__, 'key': getattr(event, 'key', None),
                                           'at': time.monotonic(), 'screen': type(self.screen).__name__})
            await super().on_event(event)

        def record_tick(self):
            now = time.perf_counter()
            if self.last_tick is not None:
                self.intervals.append(now-self.last_tick)
            self.last_tick = now

        def refresh_state(self):
            started = time.perf_counter()
            try:
                return super().refresh_state()
            finally:
                self.renders.append(time.perf_counter()-started)

        def render_state(self):
            started = time.perf_counter()
            try:
                return super().render_state()
            finally:
                self.renders.append(time.perf_counter()-started)

        def science(self):
            selected = self.selected()
            return (self.progress_snapshot if view_mode == 'local' else
                    self.selected_snapshot(*selected) if selected else None)

        async def until(self, predicate):
            deadline = time.monotonic()+CONDITION_SECONDS
            while not predicate():
                if time.monotonic() >= deadline:
                    raise AssertionError('Native UI condition deadline exceeded')
                await asyncio.sleep(.025)

        def identity(self):
            selected = self.selected()
            if selected is not None and view_mode == 'nodes':
                selected = selected[1]
            return [selected['id'], selected['attempt_id']] if selected else None

        def identity_check(self):
            assert self.identity() == [case['job_id'], case['attempt_id']], self.identity()

        def view_ready(self, view):
            screen = self.screen
            if view in {'base', 'analysis'}:
                return screen is self.screen_stack[0] and not self._navigation_restores
            if view == 'catalogue':
                return isinstance(screen, CatalogueDetails) and screen.record is not None
            if view == 'study':
                return isinstance(screen, StudyScreen) and screen.record is not None
            if view == 'review':
                return isinstance(screen, ReviewScreen) and screen.record is not None and not screen.busy
            if view == 'doctor':
                return isinstance(screen, DoctorReport) and not screen.pending and screen.report is not None
            if view == 'details':
                return isinstance(screen, (Inspect, DoctorDetails))
            if view == 'copy':
                return isinstance(screen, CopyPreview)
            if view == 'review-copy':
                return type(screen) is FrozenText
            raise ValueError(view)

        def body(self, view):
            return {'catalogue': '#catalogue-body', 'study': '#study-body', 'review': '#review-body',
                    'doctor': '#copy-text', 'copy': '#copy-text', 'review-copy': '#copy-text'}[view]

        def modal_snapshot(self, view):
            screen = self.screen
            body = screen.query_one(self.body(view))
            text = body.text if isinstance(body, TextArea) else str(body.render())
            painted = screen._compositor.render_strips()
            widgets = {}
            shell = next(widget for widget in screen.children if isinstance(widget, Vertical))
            for widget in [shell, body.parent, body, *screen.query(Button)]:
                region = widget.region
                widgets[widget.id or type(widget).__name__] = {
                    'region': list(region), 'content_region': list(widget.content_region),
                    'scroll': [widget.scroll_x, widget.scroll_y],
                    'scroll_target': [widget.scroll_target_x, widget.scroll_target_y],
                    'painted': [strip.crop(max(0, region.x), min(self.size.width, region.right)).text
                                for strip in painted[max(0, region.y):min(self.size.height, region.bottom)]]}
            return {'terminal_size': list(os.get_terminal_size(1)), 'app_size': list(self.size),
                    'screen_size': list(screen.size), 'screen_class': type(screen).__name__,
                    'selected': self.identity(), 'pending_restores': len(self._navigation_restores),
                    'pending_layout': bool(screen._layout_required), 'widgets': widgets,
                    'body_text': text, 'painted': [strip.text for strip in painted]}

        def literal_check(self, view, geometry):
            self.identity_check()
            if view == 'analysis':
                snapshot = self.science()
                assert snapshot is not None and snapshot.context.engine == 'Multiwfn'
                assert not snapshot.metrics, 'Analysis fixture must not invent SCF/geometry metrics'
                assert snapshot.calculation == case['profile']
                expected = ['ANALYSIS', 'STAGE', 'OUTPUT', *case['literal_text']]
                text = '\n'.join(geometry['painted_card'])
                assert all(contains(text, token) for token in expected), {'expected': expected, 'painted': text}
                return expected
            text = geometry['body_text']
            if view == 'catalogue':
                record = self.screen.record
                assert record.attempt_id == case['attempt_id'] and record.series == case['series']
                expected = ['HUMAN ORGANIZATION', case['attempt_id'], 'Series: '+case['series'], case['name']]
            elif view == 'study':
                record = self.screen.record
                assert record.study_id == case['study_id'] and record.plan_digest == case['plan_digest']
                expected = [case['study_id'], case['plan_digest'], 'Read only', case['attempt_id']]
            elif view == 'review':
                assert self.screen.record.attempt_id == case['attempt_id']
                assert self.screen.record.job_id == case['job_id']
                assert self.screen.record.status == 'COMPLETE'
                expected = ['OPERATIONAL', 'SOURCE BINDING', 'ELECTRONIC', 'POLICY', case['attempt_id']]
            elif view == 'doctor':
                expected = [case['attempt_id']]
            else:
                if view == 'copy':
                    assert self.screen.identity == (tuple(self.identity()) if view_mode == 'local' else target_key(case['target']))
                expected = [case['attempt_id']]
            title = {'catalogue': 'CATALOGUE', 'study': 'STUDY SERIES', 'review': 'RESULT REVIEW',
                     'doctor': 'RUNTIME / CAPABILITY DOCTOR', 'copy': 'COPY SNAPSHOT',
                     'review-copy': 'RESULT REVIEW'}[view]
            assert contains('\n'.join(geometry['painted']), title), geometry['painted']
            assert all(contains(text, token) for token in expected), {'view': view, 'expected': expected, 'body': text}
            return expected

        async def capture(self, command):
            view, size, name = command['view'], command['size'], command['name']
            snapshot = self.geometry.snapshot if view == 'analysis' else lambda app: self.modal_snapshot(view)
            transition = snapshot(self)
            transition_image = name+'-transition.svg'
            (directory/transition_image).write_text(self.export_screenshot())
            write(directory/(name+'-transition.json'), transition)
            started = time.monotonic()
            try:
                if view == 'analysis':
                    settled = await self.geometry.settled(self, size, require_visible=True, timeout=FRAME_SECONDS)
                else:
                    previous = None
                    while True:
                        state = self.modal_snapshot(view)
                        problems = []
                        for key in ('terminal_size', 'app_size', 'screen_size'):
                            if state[key] != size:
                                problems.append(key)
                        if state['pending_layout'] or state['pending_restores']:
                            problems.append('pending layout/navigation callbacks')
                        for key, value in state['widgets'].items():
                            x, y, w, h = value['region']
                            # A scrollable body may extend below its viewport;
                            # modal shell and actionable buttons must fit fully.
                            if x < 0 or x+w > size[0] or (key != self.body(view)[1:] and (y < 0 or y+h > size[1])):
                                problems.append(key+' lies outside terminal')
                            if value['scroll'] != value['scroll_target']:
                                problems.append(key+' scroll restoration pending')
                        signature = (state['screen_size'], state['selected'], state['widgets'])
                        if not problems and previous == signature:
                            settled = {**state, 'settlement_seconds': time.monotonic()-started}
                            break
                        previous = signature if not problems else None
                        remaining = FRAME_SECONDS-(time.monotonic()-started)
                        assert remaining > 0, {'problems': problems, 'last_geometry': state}
                        await asyncio.wait_for(self.screen.wait_for_refresh(), remaining)
                expected = self.literal_check(view, settled)
                image = name+'.svg'
                (directory/image).write_text(self.export_screenshot())
                result = {'size': size, 'view': view, 'image': image, 'literal_metrics': expected,
                          'literal_validation': 'painted-card' if view == 'analysis' else 'model-body-and-painted-title',
                          'selected': self.identity(), 'transition_image': transition_image,
                          'transition_geometry': transition, 'geometry': settled}
                write(directory/(name+'.json'), result)
                return result
            except BaseException as exc:
                write(directory/(name+'-settlement-failure.json'), {'transition_image': transition_image,
                    'transition_geometry': transition, 'error': repr(exc), 'elapsed_seconds': time.monotonic()-started})
                raise

        async def command(self, command):
            operation = command['operation']
            self.controls.append({'operation': operation, 'at': time.monotonic()})
            if operation == 'input_count':
                return len(self.native_inputs)
            if operation == 'input_received':
                await self.until(lambda: len(self.native_inputs) > command['after'])
                await asyncio.wait_for(self.screen.wait_for_refresh(), FRAME_SECONDS)
                return self.native_inputs[-1]
            if operation == 'ready':
                if view_mode == 'local':
                    await self.until(lambda: job['display_id'] in self.row_ids)
                    self.selected_id = job['display_id']
                    self.pin_selection()
                    table = self.screen.query_one('#table', DataTable)
                    table.move_cursor(row=self.row_ids.index(job['display_id']), animate=False)
                    self.sync_progress_selection()
                else:
                    await self.until(lambda: row_key(case['target']) in self.rows)
                    self.select(row_key(case['target']))
                    self.collect_progress()
                await self.until(lambda: self.science() is not None)
                if kind == 'analysis':
                    await self.until(lambda: all(contains(str(self.screen.query_one('#progress', Static).render()), token)
                                                for token in case['literal_text']))
                self.identity_check()
                return {'selected': self.identity(), 'tty': [sys.stdin.isatty(), sys.stdout.isatty()]}
            if operation == 'ready_view':
                await self.until(lambda: self.view_ready(command['view']))
                await asyncio.wait_for(self.screen.wait_for_refresh(), FRAME_SECONDS)
                self.identity_check()
                if command['view'] == 'doctor':
                    return {'selected': self.identity(), 'report': self.screen.report}
                return {'selected': self.identity(), 'screen': type(self.screen).__name__}
            if operation == 'position':
                await asyncio.wait_for(self.screen.wait_for_refresh(), FRAME_SECONDS)
                button = self.screen.query_one(command['selector'])
                assert not getattr(button, 'disabled', False) and button.display
                region = button.region
                assert region.width and region.height and 0 <= region.x < self.size.width and region.bottom <= self.size.height
                return [region.x+region.width//2+1, region.y+region.height//2+1]
            if operation == 'capture':
                await self.until(lambda: self.view_ready(command['view']))
                return await self.capture(command)
            if operation == 'review_limits':
                await self.until(lambda: self.view_ready('review'))
                self.screen.query_one('#review-bytes', Input).value = str(BUDGETS['review_per_source_bytes'])
                self.screen.query_one('#review-seconds', Input).value = str(BUDGETS['review_deadline_seconds'])
                return True
            if operation == 'review_complete':
                await self.until(lambda: self.view_ready('review') and self.screen.record.status not in {'PREVIEW', 'CACHED', 'PENDING'})
                self.screen.query_one('#review-scroll').focus()
                return self.screen.record.to_dict()
            raise ValueError(operation)

        def control(self):
            with (os.fdopen(int(os.environ['CMW_CAMPAIGN_IN']), 'r') as reader,
                  os.fdopen(int(os.environ['CMW_CAMPAIGN_OUT']), 'w', buffering=1) as writer):
                for line in reader:
                    try:
                        reply = {'ok': True, 'result': self.call_from_thread(self.command, json.loads(line))}
                    except Exception as exc:
                        failure_name = f'control-{len(self.controls):03d}-failure'
                        try:
                            rendered = self.call_from_thread(self.export_screenshot)
                            (directory/(failure_name+'.svg')).write_text(rendered)
                        except Exception as capture_error:
                            write(directory/(failure_name+'-capture-error.json'), {'error': repr(capture_error)})
                        write(directory/(failure_name+'.json'), {'command': json.loads(line), 'error': repr(exc)})
                        reply = {'ok': False, 'error': repr(exc), 'failure_image': failure_name+'.svg'}
                    writer.write(json.dumps(reply, default=str)+'\n')

    with monitored(directory, 'closure-pty-'+view_mode+'-'+kind):
        app = App()
        try:
            app.run(mouse=True)
        finally:
            thread = getattr(app, 'control_thread', None)
            if thread is not None:
                thread.join(timeout=2)
            write(directory/'metrics.json', {'python': sys.executable, 'package_origin': cmw.__file__,
                'ui_tick_intervals': summary(app.intervals), 'ui_render_seconds': summary(app.renders),
                'controls': app.controls, 'native_inputs': app.native_inputs,
                'requests': client.transport.samples if client is not None else [],
                'owned_transports': client.transport.owners if client is not None else [],
                'control_thread_alive': thread is not None and thread.is_alive()})
            assert thread is None or not thread.is_alive()


if __name__ == '__main__':
    if sys.argv[1:2] == ['--run']:
        run(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        output = Path(sys.argv[2])
        try:
            child(Path(sys.argv[1]), output)
        except BaseException as exc:
            metrics = output/'metrics.json'
            previous = json.loads(metrics.read_text()) if metrics.exists() else {}
            write(metrics, {**previous, 'child_error': type(exc).__name__+': '+str(exc),
                            'python': sys.executable})
            raise
