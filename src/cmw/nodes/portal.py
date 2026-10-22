"""Compact federated client with independent, bounded node-local observation lanes."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
import time
import sys
from uuid import uuid4

from textual import events
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Input, Static, TextArea

from cmw.jobs.cli import elapsed, machine_text, usage_summary
from cmw.jobs.copy_ui import FrozenText, SelectableStatic
from cmw.jobs.history_ui import ProgressHistory
from cmw.jobs.snapshot import HandoffSnapshot, capture_snapshot
from cmw.jobs.sources_ui import Sources
from cmw.jobs.suspension_presentation import effective_status
from cmw.jobs.tui import JobsApp, Prompt, ScientificProgress, detail, literal, table_name
from cmw.progress.formatting import progress_text
from cmw.progress.models import HistoryPage, ProgressSnapshot
from .codec import decode_typed, encode_typed
from .protocol import NodeError
from .portal_views import FetchDialog, NodeControlConfirm, NodeCopyPreview, NodeFiles


_TRANSPORT_ERRORS = {'UNREACHABLE', 'OUTCOME_UNKNOWN', 'ENDPOINT_CHANGED', 'INVALID_RESPONSE',
                     'SSH_IDENTITY_CHANGED', 'SSH_IDENTITY_UNVERIFIED', 'CLOSED'}


def target_key(target):
    return (target['node_id'], target['queue_id'], target['job_id'], target['attempt_id'])


def row_key(target):
    return '/'.join(map(str, target_key(target)))


@dataclass
class NodeLane:
    backend: object
    pending: deque = field(default_factory=deque)
    busy: bool = False
    state: dict | None = None
    status_envelope: dict | None = None
    received: float | None = None
    error: str | None = None
    failures: int = 0
    next_poll: float = 0
    next_progress: float = 0
    session: str | None = None
    clock_id: str | None = None
    cache: dict = field(default_factory=dict)

    def age(self, now):
        return None if self.received is None else max(0., now - self.received)

    def fresh(self, now):
        age = self.age(now)
        return self.error is None and age is not None and age <= 10


class NodesApp(App):
    TITLE = 'CMW / JOBS'
    CSS = JobsApp.CSS + '''
    #node-summary { height: auto; max-height: 2; padding: 0 1; color: #b7d8dd; }
    #table { min-height: 4; margin: 0 1; }
    #selected { margin: 0 1; max-height: 3; }
    #execution { max-height: 1; }
    #progress { max-height: 8; }
    #message { max-height: 2; }
    #shortcuts { max-height: 2; }
    '''
    BINDINGS = [Binding('enter', 'details', 'Details'), Binding('f', 'files', 'Files'),
        Binding('g', 'history', 'Progress'), Binding('s', 'sources', 'Sources'),
        Binding('y', 'copy_snapshot', 'Copy'), Binding('l', 'logs', 'Logs'),
        Binding('b', 'scheduling', 'Scheduling'), Binding('p', 'dispatch', 'Dispatch'),
        Binding('h', 'hold', 'Hold'), Binding('o', 'order', 'Order'),
        Binding('x', 'cancel', 'Cancel'), Binding('u', 'suspension', 'Suspend/Continue'),
        Binding('q', 'quit', 'Detach', priority=True), Binding('ctrl+c', 'detach', 'Detach', priority=True)]

    def __init__(self, backends, *, backend_factory=None, request_root=None,
                 clock=None, clipboard_backend=None):
        super().__init__()
        if not backends or len(backends) > 8:
            raise ValueError('Select one to eight explicitly enrolled nodes')
        identities = [(backend.node_id, backend.queue_id) for backend in backends.values()]
        if len(set(identities)) != len(identities):
            raise ValueError('A node/queue may appear only once in a portal')
        self.lanes = {label: NodeLane(backend) for label, backend in backends.items()}
        self.backend_factory = backend_factory
        self.request_root = request_root
        self.clock = clock or time.monotonic
        self.clipboard_backend = clipboard_backend
        self.selected_key = None
        self.rows = {}
        self.row_keys = []
        self.columns = None
        self.selection_generation = 0
        self.history_view = self.sources_view = self.sources_request = None
        self.progress_service = self  # Existing history modal's narrow release callback.
        self.session_sources = {}
        self.validations = {}
        self.closed = False
        self.copy_busy = False
        self.operation_busy = set()
        self.unknown_controls = {}
        self.lookup_busy = set()
        self.card_height = None
        self.viewport = None

    def compose(self):
        yield Static('CMW / JOBS                                    ALL NODES', id='heading', markup=False)
        yield Static('Connecting to explicitly enrolled nodes…', id='node-summary', markup=False)
        yield DataTable(id='table', cursor_type='row', zebra_stripes=True)
        yield SelectableStatic(id='selected', markup=False)
        yield Static(id='execution', markup=False)
        yield ScientificProgress(id='progress', markup=False)
        yield Static('Q / Ctrl-C detaches; destination-local execution continues.', id='message', markup=False)
        with Vertical(id='command-bar'):
            yield Static('Enter Details · F Files · G Progress · S Sources · Y Copy · L Logs\nP Dispatch · H Hold · O Order · X Cancel · U Suspend · B Queue · Q Detach', id='shortcuts', markup=False)

    def on_mount(self):
        self.query_one(DataTable).focus()
        self.tick()
        self.set_interval(.25, self.tick)

    def on_unmount(self):
        self.closed = True
        for lane in self.lanes.values():
            lane.pending.clear()
            threading.Thread(target=lane.backend.close, daemon=True).start()

    def on_resize(self):
        if self.is_mounted:
            self.render_state()

    def action_detach(self):
        self.exit()

    def check_action(self, action, parameters):
        if action not in {'detach', 'quit'} and self.screen is not self.screen_stack[0]:
            return False
        return True

    def queue(self, owner, operation, params=None, target=None, callback=None, *, automatic=False):
        lane = self.lanes[owner]
        if self.closed or len(lane.pending) >= 16:
            return False
        request = (operation, params or {}, deepcopy(target), callback)
        if automatic:
            if any(item[0] == operation and item[2] == target for item in lane.pending):
                return False
            lane.pending.append(request)
        else:
            lane.pending.appendleft(request)
        return True

    def tick(self):
        if self.closed or not self.is_running or not list(self.query('#node-summary')):
            return
        now = self.clock()
        for owner, lane in self.lanes.items():
            if not lane.busy and not lane.pending and now >= lane.next_poll:
                self.queue(owner, 'status', automatic=True)
                lane.next_poll = now + 2
            selected = self.selected()
            if (selected and selected[0] == owner and lane.fresh(now)
                    and now >= lane.next_progress and not lane.pending):
                lane.next_progress = now + 4
                self.collect_progress()
                self.queue(owner, 'observe', target=selected[1]['target'], automatic=True)
            if lane.pending and not lane.busy:
                request = lane.pending.popleft()
                lane.busy = True
                def collect(owner=owner, lane=lane, request=request):
                    operation, params, target, callback = request
                    try:
                        result, error = lane.backend.call(operation, params, target), None
                    except Exception as exc:
                        result, error = None, exc
                    if not self.closed:
                        try:
                            self.call_from_thread(self.accept, owner, request, result, error)
                        except RuntimeError:
                            pass
                self.run_worker(collect, thread=True, group='node-' + owner, exit_on_error=False)
        self.render_state()

    def invalidate_session(self, owner):
        lane = self.lanes[owner]
        prefix = (lane.backend.node_id, lane.backend.queue_id)
        self.session_sources = {key: value for key, value in self.session_sources.items() if key[:2] != prefix}
        self.validations = {key: value for key, value in self.validations.items() if value[0][:2] != prefix}
        for view in (self.history_view, self.sources_view):
            if view is not None and view.selection[:2] == prefix and view.is_mounted:
                view.invalidate()

    def accept(self, owner, request, envelope, error):
        lane = self.lanes[owner]
        lane.busy = False
        operation, params, target, callback = request
        now = self.clock()
        if error is not None:
            if getattr(error, 'code', 'UNREACHABLE') in _TRANSPORT_ERRORS:
                lane.error = str(error)
                lane.failures += 1
                lane.next_poll = now + min(30, 2 ** min(5, lane.failures))
                lane.pending.clear()
                self.invalidate_session(owner)
            if target is not None:
                cache = lane.cache.setdefault(target_key(target), {})
                cache[operation + '.error'] = str(error)
        else:
            if (envelope['node_id'], envelope['queue_id']) != (lane.backend.node_id, lane.backend.queue_id):
                self.accept(owner, request, None, NodeError('ENDPOINT_CHANGED', 'Reply belongs to another node or queue'))
                return
            changed = lane.session is not None and (envelope['session_id'] != lane.session or envelope['clock_id'] != lane.clock_id)
            if changed:
                self.invalidate_session(owner)
                # Cached scientific values remain qualified earlier evidence;
                # source cursors and user selections never migrate sessions.
            if changed:
                for cached in lane.cache.values():
                    cached['progress.error'] = 'Endpoint session changed; earlier cached scientific evidence.'
                    cached['observe.error'] = 'Endpoint session changed; earlier cached worker evidence.'
            lane.session, lane.clock_id = envelope['session_id'], envelope['clock_id']
            lane.error, lane.failures = None, 0
            if operation == 'status':
                lane.state = envelope['result']['state']
                lane.status_envelope, lane.received = envelope, now
                for request_id, pending in tuple(self.unknown_controls.items()):
                    if pending[0] == owner:
                        self.check_control_result(owner, request_id, pending[1])
            elif target is not None:
                key = target_key(target)
                cache = lane.cache.setdefault(key, {})
                cache[operation] = (envelope, now)
                cache.pop(operation + '.error', None)
                while len(lane.cache) > 4:
                    oldest = next(iter(lane.cache))
                    if oldest == key:
                        break
                    del lane.cache[oldest]
        if callback is not None:
            callback(envelope, error)
        self.render_state()

    def selected(self):
        return self.rows.get(self.selected_key)

    def label(self, owner, job):
        return f"{owner} / {self.lanes[owner].backend.queue} / {job['display_id']}"

    def connection_text(self, owner):
        lane = self.lanes[owner]
        age = lane.age(self.clock())
        if lane.error:
            state = 'node unreachable'
        elif age is None:
            state = 'connecting'
        elif not lane.fresh(self.clock()):
            state = 'stale transport'
        else:
            state = 'Online'
        return state + (' · received —' if age is None else f' · received {age:.0f}s ago')

    def render_state(self):
        if not self.is_mounted or not list(self.query('#node-summary')):
            return
        now = self.clock()
        summaries = []
        rows = {}
        for owner, lane in self.lanes.items():
            state = lane.state or {}
            controller = state.get('controller', {})
            count = sum(job.get('status') == 'Run' for job in state.get('jobs', ()))
            condition = 'Online' if lane.fresh(now) else 'Unreachable' if lane.error else 'Connecting' if lane.received is None else 'Stale'
            control = 'ctrl on' if controller.get('online') else 'ctrl off'
            summaries.append(f'{owner} {condition} · {control} · Run {count}' if lane.fresh(now) else
                             f'{owner} {condition} · last Run {count}' if lane.state else f'{owner} {condition}')
            for job in state.get('jobs', ()):
                target = job.get('target')
                if target and (target['node_id'], target['queue_id']) == (lane.backend.node_id, lane.backend.queue_id):
                    rows[row_key(target)] = (owner, job)
        self.rows = rows
        self.query_one('#node-summary', Static).update(literal('   |   '.join(summaries)))
        table = self.query_one('#table', DataTable)
        widths = [('node', 'NODE', 7), ('id', 'JOB ID', 8), ('files', 'FILES', 5),
                  ('name', 'NAME', max(12, self.size.width - 56)), ('status', 'STATUS', 12), ('elapsed', 'ELAPSED', 10)]
        mode = tuple(width for _, _, width in widths)
        if mode != self.columns:
            table.clear(columns=True)
            for key, title, width in widths:
                table.add_column(title, key=key, width=width)
            self.columns, self.row_keys = mode, []
        keys = list(rows)
        if keys != self.row_keys:
            table.clear()
            for key in keys:
                table.add_row(*([''] * len(widths)), key=key)
            self.row_keys = keys
        for key, (owner, job) in rows.items():
            lane = self.lanes[owner]
            status = effective_status(job)
            if not lane.fresh(now):
                status = 'Last ' + status
            values = (owner, job['display_id'], '[F]', job.get('name', ''), status, elapsed(job.get('elapsed')))
            for (column, _, width), value in zip(widths, values):
                table.update_cell(key, column, table_name(value, width))
        if self.selected_key is None and keys:
            self.select(keys[0])
        if self.selected_key in keys:
            with table.prevent(DataTable.RowHighlighted):
                table.move_cursor(row=keys.index(self.selected_key), animate=False, scroll=False)
        selected = self.selected()
        if selected:
            owner, job = selected
            lane = self.lanes[owner]
            status = effective_status(job)
            if not lane.fresh(now):
                status = 'Last reported ' + status + ' — ' + ('node unreachable' if lane.error else 'transport stale')
            usage = (lane.state or {}).get('job_usage', {}).get(job['attempt_id'], job.get('usage'))
            usage = deepcopy(usage or {})
            if usage.get('age_seconds') is not None:
                usage['age_seconds'] += lane.age(now) or 0
            if not lane.fresh(now):
                usage['stale'] = True
            connection = (self.connection_text(owner) + ' · ' + status if lane.fresh(now) else
                          status + f' · received {lane.age(now):.0f}s ago')
            text = ('SELECTED · ' + self.label(owner, job) + '\n' +
                    connection + '\n' +
                    usage_summary(usage, width=max(20, self.size.width - 7)))
            self.query_one('#selected', Static).update(literal(text))
            cache = lane.cache.get(target_key(job['target']), {})
            observed = cache.get('observe')
            execution = observed[0]['result']['execution'] if observed else None
            suffix = execution.get('summary') or execution.get('observed_execution') if execution else 'worker evidence unavailable'
            self.query_one('#execution', Static).update(literal(f'Execution: reported by {owner} · ' + suffix + (' (cached)' if not lane.fresh(now) or cache.get('observe.error') else '')))
            action = 'Continue' if effective_status(job) == 'Suspended' else 'Suspend'
            self.query_one('#shortcuts', Static).update('Enter Details · F Files · G Progress · S Sources · Y Copy · L Logs\n' +
                f'P Dispatch · H Hold · O Order · X Cancel · U {action} · B Queue · Q Detach')
            self.render_progress(owner, job, cache)
        else:
            self.query_one('#selected', Static).update('Selected attempt is no longer in the current node snapshot.' if self.selected_key else 'No managed jobs reported. Select an enrolled node through the CLI to submit.')
            self.query_one('#execution', Static).update('')
            self.query_one('#progress', Static).display = False

    def reveal_selection(self):
        if self.selected_key in self.row_keys and list(self.query('#table')):
            table = self.query_one('#table', DataTable)
            with table.prevent(DataTable.RowHighlighted):
                table.move_cursor(row=self.row_keys.index(self.selected_key), animate=False, scroll=True)

    def select(self, key):
        if key == self.selected_key:
            return
        self.selected_key = key
        self.selection_generation += 1
        if self.is_mounted and key in self.row_keys:
            table = self.query_one('#table', DataTable)
            with table.prevent(DataTable.RowHighlighted):
                table.move_cursor(row=self.row_keys.index(key), animate=False)
        for view in (self.history_view, self.sources_view):
            if view and view.is_mounted:
                view.invalidate()
        selected = self.selected()
        if selected:
            self.lanes[selected[0]].next_progress = 0

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id == 'table':
            table = event.data_table
            current = table.ordered_rows[table.cursor_row].key.value if table.row_count else None
            if event.row_key.value == current:
                self.select(event.row_key.value)
                self.render_state()

    def on_data_table_row_selected(self, event):
        if event.data_table.id == 'table':
            self.select(event.row_key.value)
            self.action_details()

    def selected_snapshot(self, owner, job):
        entry = self.lanes[owner].cache.get(target_key(job['target']), {}).get('progress')
        if entry is None:
            return None
        try:
            snapshot = decode_typed(entry[0]['result']['snapshot'], ProgressSnapshot)
        except (ValueError, TypeError, KeyError) as exc:
            self.lanes[owner].cache[target_key(job['target'])]['progress.error'] = 'Invalid scientific response: ' + str(exc)
            return None
        if (snapshot.context.job_id, snapshot.context.attempt_id) != (job['id'], job['attempt_id']):
            return None
        return snapshot

    def render_progress(self, owner, job, cache):
        widget = self.query_one('#progress', Static)
        widget.display = True
        snapshot = self.selected_snapshot(owner, job)
        if snapshot is None:
            widget.update(literal('Scientific: ' + cache.get('progress.error', 'awaiting bounded observation…')))
            widget.styles.max_height = 2
            return
        entry, received = cache['progress']
        # Both timestamps in the formatter originate on the same endpoint clock.
        # Only elapsed transport age comes from this frontend monotonic clock.
        age = max(0., self.clock() - received)
        endpoint_now = entry['observed_at'] + age
        width = max(20, self.size.width - 7)
        budget = min(8, max(3, self.size.height - 15))
        if cache.get('progress.error'):
            self.message('Scientific read unavailable; earlier cached evidence. ' + cache['progress.error'])
        text = progress_text(snapshot, compact=self.size.width < 110 or self.size.height < 36,
                             width=width, now=endpoint_now, max_lines=budget, framed=True)
        rendered = literal(text)
        for index, char in enumerate(rendered.plain):
            if char in '─│┌┐└┘├┤┴':
                rendered.stylize('#5c8996', index, index + 1)
        widget.styles.max_height = max(budget, len(text.splitlines()))
        widget.update(rendered)
        dimensions = (self.size.width, self.size.height)
        if self.card_height != len(text.splitlines()) or self.viewport != dimensions:
            self.card_height, self.viewport = len(text.splitlines()), dimensions
            self.call_after_refresh(self.reveal_selection)

    def collect_progress(self):
        selected = self.selected()
        if selected is None:
            return
        owner, job = selected
        target = job['target']
        view = self.history_view
        if view is not None and not view.closed and not view.expired and view.selection == target_key(target):
            generation = view.request_generation
            def history_result(envelope, error):
                if error or view.closed or view.expired or view.selection != target_key(target):
                    return
                result = envelope['result']
                page = decode_typed(result['page'], HistoryPage)
                if result.get('snapshot') is not None:
                    self.lanes[owner].cache.setdefault(target_key(target), {})['progress'] = (
                        {**envelope, 'result': {'snapshot': result['snapshot']}}, self.clock())
                view.bind_context((owner, deepcopy(target)))
                view.accept_page((owner, deepcopy(target)), page, generation)
            self.queue(owner, 'history', view.parameters(), target, history_result)
        else:
            self.queue(owner, 'progress', target=target, automatic=True)

    def release_history(self, context):
        owner, target = context
        self.queue(owner, 'history.release', target=target)

    def action_history(self):
        selected = self.selected()
        if selected:
            owner, job = selected
            self.push_screen(ProgressHistory(target_key(job['target']), self.label(owner, job)))

    def session_source_for(self, selection):
        return self.session_sources.get(selection)

    def request_sources(self, view, kind, payload=None):
        selected = self.selected()
        if selected is None or target_key(selected[1]['target']) != view.selection:
            view.invalidate()
            return
        owner, job = selected
        target, generation = job['target'], view.request_generation
        params = {}
        if kind == 'validate':
            params = {'sources': [encode_typed(item) for item in payload]}
        elif kind == 'activate':
            validation, confirmed = payload
            saved = self.validations.get(id(validation))
            if saved is None or saved[0] != view.selection or saved[2] != self.lanes[owner].session:
                self.call_later(view.accept_result, kind, None, 'Endpoint session changed; validate again.', generation)
                return
            params = {'validation_id': saved[1], 'confirmed': confirmed}
        self.sources_request = (view, kind, payload)
        def accept(envelope, error):
            self.sources_request = None
            if view.closed or view.expired or generation != view.request_generation:
                return
            result = None
            if error is None:
                data = envelope['result']
                if kind == 'inspect':
                    result = decode_typed(data['inspection'])
                elif kind == 'validate':
                    result = decode_typed(data['validation'])
                    self.validations[id(result)] = (view.selection, data['validation_id'], envelope['session_id'])
                elif kind == 'activate':
                    result = decode_typed(data['selection'])
                    self.session_sources[view.selection] = result
                    self.lanes[owner].cache.pop(view.selection, None)
                    self.lanes[owner].next_progress = 0
            self.call_later(view.accept_result, kind, result, str(error) if error else None, generation)
        self.queue(owner, 'sources.' + kind, params, target, accept)

    def clear_session_source(self, view):
        selected = self.selected()
        if selected is None or target_key(selected[1]['target']) != view.selection:
            view.invalidate()
            return
        owner, job = selected
        def accepted(envelope, error):
            if error:
                self.message(str(error))
            else:
                self.session_sources.pop(view.selection, None)
                self.lanes[owner].cache.pop(view.selection, None)
                self.lanes[owner].next_progress = 0
                if view.is_mounted:
                    view.dismiss(None)
        self.queue(owner, 'sources.clear', target=job['target'], callback=accepted)

    def action_sources(self):
        selected = self.selected()
        if selected:
            owner, job = selected
            self.push_screen(Sources(target_key(job['target']), self.label(owner, job)))

    def selected_request(self, operation, params, callback):
        selected = self.selected()
        if selected:
            owner, job = selected
            key, generation = self.selected_key, self.selection_generation
            def accepted(envelope, error):
                if key != self.selected_key or generation != self.selection_generation:
                    return
                if error:
                    self.message(str(error))
                else:
                    callback(owner, job, envelope['result'])
            self.queue(owner, operation, params, job['target'], accepted)

    def action_files(self):
        self.selected_request('locations', {}, lambda owner, job, result: self.push_screen(
            NodeFiles(owner, job['target'], self.label(owner, job), result,
                      local=self.lanes[owner].backend.connection['transport'] == 'local')))

    def action_logs(self):
        self.selected_request('logs', {'stream': 'stdout', 'bytes': 16384},
            lambda owner, job, result: self.push_screen(FrozenText(self.label(owner, job) + '\n' +
                str(result.get('node_path', '')) + '\n\n' + result['text'], 'BOUNDED STDOUT · execution node')))

    def action_details(self):
        selected = self.selected()
        if selected:
            owner, job = selected
            self.push_screen(FrozenText(self.label(owner, job) + '\n' + self.connection_text(owner) + '\n\n' +
                detail(job) + '\n\n' + json.dumps({'target': job['target'], 'revision': job.get('revision')}, indent=2), 'JOB DETAILS · cached node report'))

    def action_scheduling(self):
        selected = self.selected()
        if selected:
            owner, job = selected
            state = self.lanes[owner].state or {}
            text = self.label(owner, job) + '\n' + self.connection_text(owner) + '\n' + machine_text(state.get('machine_usage'))
            text += '\n\n' + json.dumps({key: state.get(key) for key in ('controller', 'admission', 'external_activity', 'scheduler')}, indent=2)
            self.push_screen(FrozenText(text, 'NODE-LOCAL SCHEDULING · no cross-node resource pool'))

    def capture_copy_snapshot(self, identity=None):
        selected = self.selected()
        if selected is None:
            return None
        owner, job = selected
        target = job['target']
        key = target_key(target)
        if identity is not None and identity != key:
            return None
        lane = self.lanes[owner]
        cache = lane.cache.get(key, {})
        progress = self.selected_snapshot(owner, job)
        observed = cache.get('observe')
        execution = observed[0]['result']['execution'] if observed else None
        envelope = lane.status_envelope
        captured = capture_snapshot(job, progress=progress, execution=execution,
            captured_at=envelope['observed_at'], runtime=envelope['runtime'],
            read_error=cache.get('progress.error') or lane.error,
            execution_error=cache.get('observe.error') or lane.error)
        data = captured.to_dict()
        data['node_origin'] = {'alias': owner, 'queue_alias': lane.backend.queue, **target,
            'connection': self.connection_text(owner), 'runtime_id': envelope['runtime']['fingerprint'],
            'clock_id': envelope['clock_id'], 'session_id': envelope['session_id'],
            'receipt_ages': {'status': lane.age(self.clock()), **{name: max(0., self.clock() - value[1])
                for name, value in cache.items() if isinstance(value, tuple)}},
            'component_origins': {name: {field: value[0][field] for field in
                ('node_id', 'queue_id', 'session_id', 'clock_id', 'sequence', 'observed_at')}
                for name, value in cache.items() if isinstance(value, tuple)},
            'qualification': 'Endpoint clock timestamps; frontend monotonic receipt ages; copied values are observations, not scientific acceptance.'}
        return HandoffSnapshot(json.dumps(data, ensure_ascii=False, allow_nan=False))

    def action_copy_snapshot(self):
        snapshot = self.capture_copy_snapshot()
        if snapshot:
            self.push_screen(NodeCopyPreview(snapshot, target_key(self.selected()[1]['target']), self.capture_copy_snapshot))

    def request_copy(self, text, *, view=None):
        if self.copy_busy:
            return
        self.copy_busy = True
        def copying():
            from cmw.jobs.clipboard import copy_text, CopyResult
            try:
                osc52 = (lambda value: self.call_from_thread(App.copy_to_clipboard, self, value)) if self._driver is not None and sys.stdout.isatty() else None
                result = self.clipboard_backend(text) if self.clipboard_backend else copy_text(text, osc52=osc52)
            except Exception as exc:
                result = CopyResult('FAILED', str(exc))
            if not self.closed:
                self.call_from_thread(self.accept_copy, result, view)
        self.run_worker(copying, thread=True, group='clipboard', exit_on_error=False)

    def accept_copy(self, result, view):
        self.copy_busy = False
        if isinstance(view, FrozenText) and view.is_mounted:
            view.query_one('#copy-status', Static).update(result.message)
        elif isinstance(view, NodeFiles) and view.is_mounted:
            view.query_one('#node-files-status', Static).update(result.message)
        else:
            self.message(result.message)

    def selected_copy_text(self):
        focused = self.focused
        if isinstance(focused, (TextArea, Input)) and focused.selected_text:
            return focused.selected_text
        getter = getattr(self.screen, 'get_selected_text', None)
        return getter() if getter else None

    async def on_event(self, event):
        if isinstance(event, events.Key) and not event.is_forwarded and event.key == 'ctrl+c':
            selected = self.selected_copy_text()
            if selected:
                event.stop()
                event.prevent_default()
                self.request_copy(selected, view=self.screen)
                return
        await super().on_event(event)

    def copy_to_clipboard(self, text):
        self.request_copy(text, view=self.screen)

    def message(self, text):
        if self.is_mounted:
            self.query_one('#message', Static).update(literal(text))

    def new_backend(self, owner):
        if self.backend_factory:
            return self.backend_factory(owner)
        from .connections import ConnectedBackend
        old = self.lanes[owner].backend
        return ConnectedBackend(old.alias, deepcopy(old.connection), old.queue)

    def start_fetch(self, view, specification_path, destination):
        if not Path(specification_path).is_absolute() or not Path(destination).is_absolute():
            view.query_one('#fetch-status', Static).update('Enter absolute local paths for the explicit specification and new destination.')
            return
        view.busy = True
        request_id = uuid4().hex
        view.query_one('#fetch-confirm').disabled = True
        view.query_one('#fetch-status', Static).update('Fetching selected artifacts; monitoring continues independently…')
        def fetching():
            backend = None
            try:
                from .cli import _read_spec
                from .client import fetch
                specification = _read_spec(specification_path)
                backend = self.new_backend(view.owner)
                result = fetch(backend, view.target, specification, Path(destination), root=self.request_root, request_id=request_id)
                text = 'Replica fetched. ' + json.dumps(result, ensure_ascii=True)[:3000]
            except Exception as exc:
                text = 'Fetch request ' + request_id + ' not completed: ' + str(exc)
            finally:
                if backend is not None:
                    backend.close()
            if not self.closed:
                self.call_from_thread(self.finish_fetch, view, text)
        self.run_worker(fetching, thread=True, group='fetch', exit_on_error=False)

    def finish_fetch(self, view, text):
        view.busy = False
        if view.is_mounted:
            view.query_one('#fetch-confirm').disabled = False
            view.query_one('#fetch-status', Static).update(literal(text))
        else:
            self.message(text)

    def open_local_location(self, view, location):
        if self.lanes[view.owner].backend.connection['transport'] != 'local':
            return
        def accepted(envelope, error):
            if error:
                view.query_one('#node-files-status', Static).update(str(error))
                return
            current = next((item for item in envelope['result']['locations'] if item['location_id'] == location['location_id']), None)
            if current != location or not current.get('node_path') or current['accessibility'] != 'AVAILABLE':
                view.query_one('#node-files-status', Static).update('Location changed or is unavailable; reopen Files.')
                return
            def opening():
                from cmw.jobs.folder_opener import request_open
                directory = Path(current['node_path']).parent if current['kind'] == 'file' else Path(current['node_path'])
                result = request_open(directory)
                if not self.closed and view.is_mounted:
                    self.call_from_thread(view.query_one('#node-files-status', Static).update, result.reason)
            self.run_worker(opening, thread=True, group='local-folder-open', exit_on_error=False)
        self.queue(view.owner, 'locations', target=view.target, callback=accepted)

    def preview_control(self, action, *, position=None):
        selected = self.selected()
        if selected is None:
            return
        owner, job = selected
        lane = self.lanes[owner]
        if not lane.fresh(self.clock()) or lane.backend.connection.get('read_only'):
            self.message('Controls unavailable: node is stale, unreachable or enrolled read only. Nothing was queued.')
            return
        target = None if action.startswith(('dispatch.', 'controller.')) else job['target']
        params = {'action': action, **({'position': position} if position is not None else {})}
        selected_key, generation = self.selected_key, self.selection_generation
        def accepted(envelope, error):
            if selected_key != self.selected_key or generation != self.selection_generation:
                return
            if error:
                self.message(str(error))
            elif not envelope['result'].get('ready'):
                self.message('Control refused: ' + '; '.join(envelope['result'].get('reasons', ())))
            else:
                self.push_screen(NodeControlConfirm(owner, job['target'], self.label(owner, job), envelope['result']))
        self.queue(owner, 'control.preview', params, target, accepted)

    def start_control(self, view, reason):
        if view.owner in self.operation_busy:
            return
        lane = self.lanes[view.owner]
        if not lane.fresh(self.clock()) or lane.backend.connection.get('read_only'):
            view.query_one('#node-control-status', Static).update('Node unavailable or read only. Nothing was queued; reopen the preview after reconnect.')
            return
        self.operation_busy.add(view.owner)
        view.busy = True
        request_id = uuid4().hex
        view.request_id = request_id
        view.query_one('#node-control-check').disabled = False
        view.query_one('#node-control-confirm').disabled = True
        def controlling():
            backend = None
            unknown = False
            try:
                from .client import apply_control
                backend = self.new_backend(view.owner)
                result = apply_control(backend, deepcopy(view.preview), root=self.request_root, confirm=True, reason=reason, request_id=request_id)
                text = json.dumps(result, ensure_ascii=True)[:3000]
            except Exception as exc:
                unknown = getattr(exc, 'code', None) in _TRANSPORT_ERRORS
                text = 'Request ' + request_id + ': ' + str(exc) + ' · Query this original ID; no automatic replay.'
            finally:
                if backend is not None:
                    backend.close()
            if not self.closed:
                self.call_from_thread(self.finish_control, view, text, unknown)
        self.run_worker(controlling, thread=True, group='control-' + view.owner, exit_on_error=False)

    def finish_control(self, view, text, unknown=False):
        if unknown:
            self.unknown_controls[view.request_id] = (view.owner, view)
        self.operation_busy.discard(view.owner)
        self.lanes[view.owner].next_poll = 0
        if view.is_mounted:
            view.query_one('#node-control-status', Static).update(literal(text))
        self.message(text)
        # A submitted preview is single-use, including uncertain outcomes.

    def check_control_result(self, owner, request_id, view=None):
        if request_id in self.lookup_busy or self.closed:
            return
        self.lookup_busy.add(request_id)
        def checking():
            backend, known = None, False
            try:
                backend = self.new_backend(owner)
                value = backend.call('request', {'request_id': request_id})['result']['request']
                known = True
                text = ('Request ' + request_id + ': ' + json.dumps(value, ensure_ascii=True)[:2500]
                        if value is not None else 'Request ' + request_id + ': no durable outcome recorded. No replay; review state before a fresh explicit action.')
            except Exception as exc:
                text = 'Request ' + request_id + ': outcome still unknown — ' + str(exc)
            finally:
                if backend is not None:
                    backend.close()
            if not self.closed:
                self.call_from_thread(self.accept_control_lookup, request_id, view, text, known)
        self.run_worker(checking, thread=True, group='request-' + request_id, exit_on_error=False)

    def accept_control_lookup(self, request_id, view, text, known):
        self.lookup_busy.discard(request_id)
        if known:
            self.unknown_controls.pop(request_id, None)
        if view is not None and view.is_mounted:
            view.query_one('#node-control-status', Static).update(literal(text))
        self.message(text)

    def action_hold(self):
        selected = self.selected()
        if selected:
            self.preview_control('release' if selected[1]['status'] == 'Hold' else 'hold')

    def action_dispatch(self):
        selected = self.selected()
        if selected:
            control = self.lanes[selected[0]].state['controller']
            self.preview_control('dispatch.pause' if control.get('dispatch') else
                                 'dispatch.resume' if control.get('online') else 'controller.start')

    def action_cancel(self):
        self.preview_control('cancel')

    def action_suspension(self):
        selected = self.selected()
        if selected:
            self.preview_control('continue' if effective_status(selected[1]) == 'Suspended' else 'suspend')

    def action_order(self):
        selected = self.selected()
        if selected is None:
            return
        identity = self.selected_key
        def ordered(value):
            if value and self.selected_key == identity:
                try:
                    position = int(value)
                except ValueError:
                    self.message('Order must be an integer.')
                    return
                self.preview_control('move', position=position)
        self.push_screen(Prompt('New queue position (held head still blocks following jobs):', order=True), ordered)
