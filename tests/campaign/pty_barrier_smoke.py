"""Small actual app.run() PTY action/return preflight, without engines or enrollment."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time

from tests.campaign.geometry import RESIZE_SEQUENCE
from tests.campaign.navigation_trace import screen_state
from tests.campaign.paint_barrier import active_screen_refresh
from tests.campaign.pty import Session


def run(directory):
    directory = Path(directory)
    directory.mkdir()
    result = {}
    for view in ('local', 'nodes'):
        settings = directory/(view+'.json')
        settings.write_text(json.dumps({'view': view}))
        session = Session(settings, directory/view, module='tests.campaign.pty_barrier_smoke',
                          deadline=time.monotonic()+30)
        actions = []
        try:
            ready = session.request('ready')
            assert ready['tty'] == [True, True]
            for size in RESIZE_SEQUENCE:
                session.resize(size)
                session.request('size', size=list(size))
                before = session.request('count')
                session.send(b'y')
                actions.append(session.request('received', after=before, opened=True, delivered_hex='79'))
                x, y = session.request('position')
                before = session.request('count')
                keys = f'\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m'.encode()
                session.send(keys)
                actions.append(session.request('received', after=before, opened=False, delivered_hex=keys.hex(),
                                               selector='#copy-back'))
                session.request('later_refresh')
        finally:
            result[view] = {'returncode': session.close(), 'actions': actions,
                            'terminal_bytes': session.output_bytes}
            (directory/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        assert result[view]['returncode'] == 0
    return result


def child(settings, directory):
    from cmw.jobs.tui import JobsApp
    from cmw.jobs.store import Store
    from cmw.jobs.copy_ui import CopyPreview
    from cmw.nodes.portal import NodesApp
    from tests.jobs.test_navigation_ui import SyntheticObserver
    from tests.nodes.test_portal_navigation import many_jobs
    from tests.campaign.visual_contract_v2 import SyntheticMachineSampler
    from textual import events
    view = json.loads(settings.read_text())['view']
    if view == 'local':
        store = Store(directory/'queue')
        store.add(argv=['/unexecuted/fixture'], cwd=directory, name='Synthetic held PTY target', hold=True)
    class App(JobsApp if view == 'local' else NodesApp):
        def __init__(self):
            self.inputs, self.barriers, self.paints = [], [], []
            self.renders = 0
            if view == 'local':
                super().__init__(store, observer=SyntheticObserver(), sampler=SyntheticMachineSampler())
            else:
                super().__init__({'MINI': many_jobs('MINI', remote=True)})

        def refresh_state(self):
            self.renders += 1
            return super().refresh_state()

        def render_state(self):
            self.renders += 1
            return super().render_state()

        def _display(self, screen, renderable):
            super()._display(screen, renderable)
            if renderable is not None and self._running and not self._closed and not self._batch_count:
                self.paints.append({'generation': len(self.paints)+1, 'screen': screen_state(screen),
                                    'at': time.monotonic()})

        async def on_event(self, event):
            if isinstance(event, (events.Key, events.MouseUp)) and not event.is_forwarded:
                self.inputs.append({'event': type(event).__name__, 'key': getattr(event, 'key', None),
                                    'screen': screen_state(self.screen), 'at': time.monotonic(),
                                    'paint_generation': len(self.paints)})
            await super().on_event(event)

        def on_ready(self):
            self.thread = threading.Thread(target=self.control, daemon=True)
            self.thread.start()

        async def until(self, predicate):
            deadline = time.monotonic()+5
            while not predicate():
                assert time.monotonic() < deadline, 'native prerequisite deadline'
                await asyncio.sleep(.01)

        async def command(self, command):
            op = command['operation']
            if op == 'ready':
                await self.until(lambda: self.selected() is not None)
                await active_screen_refresh(self, records=self.barriers)
                return {'tty': [sys.stdin.isatty(), sys.stdout.isatty()]}
            if op == 'size':
                await self.until(lambda: list(self.size) == command['size'])
                await active_screen_refresh(self, records=self.barriers)
                return True
            if op == 'count':
                return {'count': len(self.inputs), 'screen': screen_state(self.screen),
                        'paint_generation': len(self.paints)}
            if op == 'position':
                await active_screen_refresh(self, records=self.barriers)
                region = self.screen.query_one('#copy-back').region
                assert region.width and region.height and region.bottom <= self.size.height
                return [region.x+region.width//2+1, region.y+region.height//2+1]
            if op == 'received':
                await self.until(lambda: len(self.inputs) > command['after']['count'])
                result = await active_screen_refresh(self, records=self.barriers,
                    expected=lambda: isinstance(self.screen, CopyPreview) == command['opened'])
                result.update(before=command['after'], acknowledgment=self.inputs[-1],
                              delivered_hex=command['delivered_hex'], selector=command.get('selector'),
                              paint_generation=len(self.paints))
                assert result['paint_generation'] > command['after']['paint_generation']
                return result
            if op == 'later_refresh':
                before = self.renders
                await self.until(lambda: self.renders > before and not self.navigation_pending())
                await active_screen_refresh(self, records=self.barriers)
                assert self.screen is self.screen_stack[0]
                return True
            raise ValueError(op)

        def control(self):
            with (os.fdopen(int(os.environ['CMW_CAMPAIGN_IN']), 'r') as reader,
                  os.fdopen(int(os.environ['CMW_CAMPAIGN_OUT']), 'w', buffering=1) as writer):
                for line in reader:
                    try:
                        value = self.call_from_thread(self.command, json.loads(line))
                        reply = {'ok': True, 'result': value}
                    except Exception as exc:
                        (directory/'failure.svg').write_text(self.call_from_thread(self.export_screenshot))
                        reply = {'ok': False, 'error': repr(exc), 'command': json.loads(line)}
                    writer.write(json.dumps(reply)+'\n')
    app = App()
    try:
        app.run(mouse=True)
    finally:
        thread = getattr(app, 'thread', None)
        if thread is not None:
            thread.join(timeout=2)
        (directory/'events.json').write_text(json.dumps({'inputs': app.inputs, 'barriers': app.barriers,
            'paints': app.paints, 'control_thread_alive': thread is not None and thread.is_alive()}, indent=2)+'\n')
        assert thread is None or not thread.is_alive()


if __name__ == '__main__':
    if sys.argv[1:2] == ['--run']:
        run(Path(sys.argv[2]))
    else:
        child(Path(sys.argv[1]), Path(sys.argv[2]))
