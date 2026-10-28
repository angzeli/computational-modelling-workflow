"""Actual terminal I/O, reusing the viewport campaign's pipe/barrier pattern."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

from tests.campaign.support import BUDGETS, MeterTransport, assert_native, assert_review, summary
from tests.campaign.geometry import GeometryProbe, RESIZE_SEQUENCE


class Session:
    def __init__(self, settings, directory):
        self.directory = directory
        directory.mkdir(mode=0o700)
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
        control_in, self.writer = os.pipe()
        self.reader, control_out = os.pipe()
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'TEXTUAL_DRIVER')}
        env.update(TERM='xterm-256color', PYTHONDONTWRITEBYTECODE='1',
            CMW_JOBS_STATE=str(directory/'unused-default'), CMW_CAMPAIGN_IN=str(control_in),
            CMW_CAMPAIGN_OUT=str(control_out))
        driver = Path(__file__).resolve().parents[2]
        self.process = subprocess.Popen([sys.executable, '-I', '-B', '-c',
            'import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("tests.campaign.pty",run_name="__main__")',
            str(driver), str(settings), str(directory)], stdin=slave, stdout=slave, stderr=slave,
            pass_fds=(control_in, control_out), start_new_session=True, env=env, cwd=driver)
        os.close(slave); os.close(control_in); os.close(control_out)
        self.buffer = b''
        self.output_bytes = 0
        self.thread = threading.Thread(target=self.drain, daemon=True)
        self.thread.start()

    def drain(self):
        tail = b''
        with (self.directory/'terminal.ansi').open('wb') as output:
            while True:
                try:
                    ready, _, _ = select.select([self.master], [], [], .2)
                    if not ready:
                        if self.process.poll() is not None:
                            return
                        continue
                    data = os.read(self.master, 65536)
                    if not data:
                        return
                    self.output_bytes += len(data)
                    if self.output_bytes > 8 * 1024 * 1024:
                        return
                    output.write(data)
                    if b'\x1b[6n' in tail + data:
                        os.write(self.master, b'\x1b[1;1R')
                    tail = data[-3:]
                except OSError:
                    return

    def send(self, keys):
        os.write(self.master, keys)

    def resize(self, size):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', size[1], size[0], 0, 0))
        self.process.send_signal(signal.SIGWINCH)

    def request(self, operation, **values):
        os.write(self.writer, (json.dumps({'operation': operation, **values})+'\n').encode())
        deadline = time.monotonic()+25
        while b'\n' not in self.buffer:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise AssertionError('PTY barrier timed out: '+operation)
            ready, _, _ = select.select([self.reader], [], [], remaining)
            if not ready:
                continue
            data = os.read(self.reader, 65536)
            if not data:
                raise AssertionError('PTY child exited before barrier: '+operation)
            self.buffer += data
        line, self.buffer = self.buffer.split(b'\n', 1)
        value = json.loads(line)
        assert value['ok'], value
        return value['result']

    def close(self):
        # EOF releases the child control reader even when the app exits on a key.
        os.close(self.writer)
        if self.process.poll() is None:
            self.send(b'q')
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()  # Only this Popen-owned terminal child.
                self.process.wait(timeout=10)
        self.thread.join(timeout=2)
        for fd in (self.master, self.reader):
            os.close(fd)
        assert not self.thread.is_alive()
        return self.process.returncode


def run(settings, directory):
    view = json.loads(settings.read_text())['view']
    session = Session(settings, directory)
    report = {}
    try:
        report['native_screen'] = session.request('ready')
        assert report['native_screen']['tty'] == [True, True]
        assert report['native_screen']['metrics'] > 0 and 'SCF' in report['native_screen']['card']
        report['native_screenshots'] = []
        if view == 'nodes':
            session.request('select_native', owner='10000')
        for index, size in enumerate(RESIZE_SEQUENCE):
            session.resize(size)
            report['native_screenshots'].append(session.request('capture', size=list(size),
                name=f'native-{view}-{index}-{size[0]}x{size[1]}', require_visible=True))
        if view == 'nodes':
            session.request('select_native', owner='1000')
        session.resize((100, 30))
        report['native_screen'] = session.request('capture', size=[100, 30], name='native-before-details')
        session.send(b'\r')
        session.request('modal', opened=True)
        session.send(b'd')
        report['doctor'] = session.request('doctor_complete')
        session.send(b'\x1b')
        session.request('details')
        session.send(b'\x1b')
        returned = session.request('modal', opened=False)
        assert returned['selected'] == report['native_screen']['selected']
        session.send(b'r')
        session.request('review_ready')
        session.send(b'i')
        report['selected_review'] = session.request('review_complete')
        session.send(b'\x1b')
        session.request('modal', opened=False)
        session.send(b'/')
        session.request('filter_ready')
        session.send(b'Synthetic native')
        position = session.request('filter_position')
        x, y = position
        session.send(f'\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m'.encode())
        report['filtered'] = session.request('filter_applied')
        if view == 'nodes':
            report['unavailable_node'] = session.request('unavailable')
            assert report['unavailable_node']['healthy_progress']
        report['settled'] = session.request('ticks')
    finally:
        report['returncode'] = session.close()
        report['terminal_bytes'] = session.output_bytes
    assert report['returncode'] == 0, report
    terminal = (directory/'terminal.ansi').read_bytes()
    assert b'CMW' in terminal and b'SCF' in terminal and b'RESULT REVIEW' in terminal
    metrics = json.loads((directory/'metrics.json').read_text())
    assert metrics['ui_tick_intervals']['p95'] <= BUDGETS['ui_tick_p95_seconds'], metrics
    assert metrics['ui_render_seconds']['p95'] <= BUDGETS['ui_render_p95_seconds'], metrics
    report['metrics'] = metrics
    (directory/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def child(settings_path, directory):
    from cmw.doctor.presentation import DoctorReport
    from cmw.jobs.catalogue_ui import CatalogueSearch
    from cmw.jobs.activity import Observer
    from cmw.jobs.store import Store
    from cmw.jobs.tui import JobsApp
    from cmw.jobs.review_ui import ReviewScreen
    from cmw.nodes.codec import decode_typed
    from cmw.nodes.connections import ConnectedBackend
    from cmw.progress.models import ProgressSnapshot
    from cmw.nodes.portal import NodesApp, row_key
    from textual.widgets import Button, Input, Static
    from tests.jobs.isolated_runtime import fixture_collect

    settings = json.loads(settings_path.read_text())
    view = settings['view']
    clients = {} if view == 'local' else {str(row['rows']): ConnectedBackend(str(row['rows']), row['entry'],
        transport=MeterTransport(row['command'], timeout=5,
            host_key=row['entry'].get('ssh_host_key'))) for row in settings['fixtures']}

    base = JobsApp if view == 'local' else NodesApp
    class App(base):
        def __init__(self):
            self.intervals, self.renders = [], []
            self.last_tick = None
            self.geometry = GeometryProbe()
            if view == 'local':
                super().__init__(Store(settings['fixtures'][0]['state']),
                    observer=Observer(collector=fixture_collect))
            else:
                super().__init__(clients, request_root=directory/'requests')

        def on_ready(self):
            self.set_interval(.05, self.record_tick)
            self.control_thread = threading.Thread(target=self.control, daemon=True)
            self.control_thread.start()

        def record_tick(self):
            now = time.perf_counter()
            if self.last_tick is not None:
                self.intervals.append(now-self.last_tick)
            self.last_tick = now

        def render_state(self):
            start = time.perf_counter()
            try:
                return super().render_state()
            finally:
                self.renders.append(time.perf_counter()-start)

        def refresh_state(self):
            start = time.perf_counter()
            try:
                return super().refresh_state()
            finally:
                self.renders.append(time.perf_counter()-start)

        async def until(self, predicate):
            deadline = time.monotonic()+20
            while not predicate():
                if time.monotonic() >= deadline:
                    raise AssertionError('Native terminal condition timed out')
                await asyncio.sleep(.05)

        def snapshot(self):
            selected = self.selected()
            science = self.progress_snapshot if view == 'local' else (self.selected_snapshot(*selected) if selected else None)
            return {'selected': self.selected_id if view == 'local' else self.selected_key,
                'tty': [sys.stdin.isatty(), sys.stdout.isatty()],
                'metrics': len(science.metrics) if science else 0,
                'card': str(self.screen_stack[0].query_one('#progress', Static).render())}

        def native_oracle(self):
            if view == 'local':
                return assert_native(self.progress_snapshot, 1)
            owner, job = self.selected()
            return assert_native(self.selected_snapshot(owner, job), 2 if owner == '10000' else 1)

        async def command(self, command):
            operation = command['operation']
            if operation == 'ready':
                await self.until(lambda: self.snapshot()['metrics'] > 0 and 'SCF' in self.snapshot()['card'])
                self.native_oracle()
                (directory/'native-screen.svg').write_text(self.export_screenshot())
                return self.snapshot()
            if operation == 'modal':
                await self.until(lambda: (self.screen is not self.screen_stack[0]) == command['opened'])
                return self.snapshot()
            if operation == 'doctor_complete':
                await self.until(lambda: isinstance(self.screen, DoctorReport) and not self.screen.pending)
                assert self.screen.report is not None
                (directory/'doctor.svg').write_text(self.export_screenshot())
                return {'target': self.screen.report.get('target'), 'pending': self.screen.pending}
            if operation == 'details':
                await self.until(lambda: self.screen is not self.screen_stack[0] and not isinstance(self.screen, DoctorReport))
                return self.snapshot()
            if operation == 'filter_ready':
                await self.until(lambda: isinstance(self.screen, CatalogueSearch))
                return True
            if operation == 'filter_position':
                await self.until(lambda: self.screen.query_one('#catalogue-text', Input).value == 'Synthetic native')
                region = self.screen.query_one('#catalogue-apply', Button).region
                return [region.x+region.width//2+1, region.y+region.height//2+1]
            if operation == 'filter_applied':
                await self.until(lambda: self.screen is self.screen_stack[0] and self.catalogue_query is not None)
                await self.until(lambda: len(self.row_ids if view == 'local' else self.rows) == (1 if view == 'local' else 2))
                return {'visible_rows': len(self.row_ids if view == 'local' else self.rows), 'query': self.catalogue_query.text}
            if operation == 'select_native':
                row = next(row for row in settings['fixtures'] if str(row['rows']) == command['owner'])
                self.select(row_key(row['target']))
                self.collect_progress()
                await self.until(lambda: self.selected_key == row_key(row['target']) and self.snapshot()['metrics'] > 0)
                return self.snapshot()
            if operation == 'capture':
                transition = self.geometry.snapshot(self)
                transition_image = command['name']+'-transition.svg'
                (directory/transition_image).write_text(self.export_screenshot())
                try:
                    settled = await self.geometry.settled(self, command['size'],
                        require_visible=command.get('require_visible', False))
                except AssertionError as exc:
                    failure = {'transition_image': transition_image, 'transition_geometry': transition,
                               'failure': exc.args[0]}
                    (directory/(command['name']+'-settlement-failure.json')).write_text(json.dumps(failure, indent=2)+'\n')
                    raise
                expected = self.native_oracle()
                (directory/(command['name']+'.svg')).write_text(self.export_screenshot())
                return {**self.snapshot(), 'size': list(self.size), 'image': command['name']+'.svg',
                    'transition_image': transition_image, 'transition_geometry': transition,
                    'geometry': settled, 'literal_metrics': expected}
            if operation == 'review_ready':
                await self.until(lambda: isinstance(self.screen, ReviewScreen) and self.screen.record is not None and not self.screen.busy)
                self.screen.query_one('#review-bytes', Input).value = str(BUDGETS['review_per_source_bytes'])
                self.screen.query_one('#review-seconds', Input).value = str(BUDGETS['review_deadline_seconds'])
                self.screen.query_one('#review-scroll').focus()
                return {'status': self.screen.record.status}
            if operation == 'review_complete':
                await self.until(lambda: self.screen.record is not None and self.screen.record.status not in {'PREVIEW', 'CACHED', 'PENDING'} and not self.screen.busy)
                record = self.screen.record
                assert_review(record)
                (directory/'selected-review.svg').write_text(self.export_screenshot())
                return record.to_dict()
            if operation == 'unavailable':
                # The socket is bound by the parent but never listens. This is
                # genuine loopback refusal, not a fabricated backend response.
                owner = str(settings['fixtures'][-1]['rows'])
                backend = clients[owner]
                await self.until(lambda: not self.lanes[owner].busy)
                backend.close()
                argv = list(backend.transport.argv)
                argv[argv.index('-p')+1] = str(settings['unavailable_port'])
                backend.transport.argv = argv
                backend.transport.expected_refusal_reason = 'deliberate unavailable-owner control'
                first_refusal = len(backend.transport.samples)
                self.queue(owner, 'status')
                await self.until(lambda: self.lanes[owner].error is not None)
                refusals = [sample for sample in backend.transport.samples[first_refusal:]
                            if sample['outcome'] != 'ok']
                assert refusals, 'Unavailable-owner control did not attempt a failing request'
                for sample in refusals:
                    sample.update(expected_refusal=True, refusal_reason='deliberate unavailable-owner control')
                healthy = str(settings['fixtures'][0]['rows'])
                await self.until(lambda: not self.lanes[healthy].busy)
                selected = next((job for name, job in self.rows.values() if name == healthy and job['id'] == 1), None)
                assert selected is not None
                # Demand replies produced after the peer failure; a retained
                # scientific snapshot alone cannot establish healthy service.
                lane = self.lanes[healthy]
                previous_sequence = lane.status_envelope['sequence']
                previous_received = lane.received
                replies = []
                assert self.queue(healthy, 'status', callback=lambda reply, error: replies.append((reply, error)))
                await self.until(lambda: bool(replies))
                response, error = replies.pop()
                assert error is None, error
                assert response['sequence'] > previous_sequence and lane.received > previous_received
                assert lane.fresh(self.clock())
                assert self.queue(healthy, 'progress', target=selected['target'],
                    callback=lambda reply, error: replies.append((reply, error)))
                await self.until(lambda: bool(replies))
                progress, error = replies.pop()
                assert error is None, error
                assert progress['sequence'] > response['sequence']
                assert_native(decode_typed(progress['result']['snapshot'], ProgressSnapshot), 1)
                return {'failed_owner': owner, 'error': str(self.lanes[owner].error),
                    'healthy_progress': True, 'healthy_prior_sequence': previous_sequence,
                    'healthy_status_sequence': response['sequence'],
                    'healthy_progress_sequence': progress['sequence']}
            if operation == 'ticks':
                count = len(self.intervals)
                await self.until(lambda: len(self.intervals) >= count+20)
                return self.snapshot()
            raise ValueError(operation)

        def control(self):
            with (os.fdopen(int(os.environ['CMW_CAMPAIGN_IN']), 'r') as reader,
                  os.fdopen(int(os.environ['CMW_CAMPAIGN_OUT']), 'w', buffering=1) as writer):
                for line in reader:
                    try:
                        value = self.call_from_thread(self.command, json.loads(line))
                        reply = {'ok': True, 'result': value}
                    except Exception as exc:
                        reply = {'ok': False, 'error': repr(exc)}
                    writer.write(json.dumps(reply)+'\n')

    app = App()
    try:
        app.run(mouse=True)
    finally:
        control_thread = getattr(app, 'control_thread', None)
        if control_thread is not None:
            control_thread.join(timeout=2)
        report = {'ui_tick_intervals': summary(app.intervals), 'ui_render_seconds': summary(app.renders),
            'control_thread_alive': control_thread is not None and control_thread.is_alive(),
            'requests': {owner: client.transport.samples for owner, client in clients.items()},
            'owned_transports': [record for client in clients.values() for record in client.transport.owners],
            'ssh_wire_sessions': [wire for client in clients.values() for wire in client.transport.wire_sessions]}
        (directory/'metrics.json').write_text(json.dumps(report, indent=2)+'\n')
        assert not report['control_thread_alive'], report


if __name__ == '__main__':
    child(Path(sys.argv[1]), Path(sys.argv[2]))
