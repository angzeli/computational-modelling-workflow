"""Compact federated client with independent, bounded node-local observation lanes."""
from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
import sys
from uuid import uuid4

from textual import events
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Input, Static, TextArea

from cmw.jobs.navigation import NavigationApp
from cmw.jobs.cli import elapsed, machine_text, machine_detail, usage_summary
from cmw.jobs.copy_ui import FrozenText, SelectableStatic
from cmw.jobs.history_ui import ProgressHistory
from cmw.jobs.snapshot import HandoffSnapshot, capture_snapshot
from cmw.jobs.sources_ui import Sources
from cmw.jobs.review_ui import ReviewScreen
from cmw.jobs.catalogue_ui import NodeCatalogue
from cmw.jobs.review_models import ReviewRecord, cached_review, review_text
from cmw.jobs.suspension_presentation import effective_status
from cmw.jobs.tui import JobsApp, ScientificProgress, detail, literal, table_name
from cmw.jobs.telemetry import MAX_AGE as TELEMETRY_MAX_AGE
from cmw.progress.formatting import compact_frame, fit_text, progress_text
from cmw.progress.models import HistoryPage, ProgressSnapshot
from .codec import decode_typed, encode_typed
from .connections import observed_machine_usage, observed_status_usage
from .contracts import (Reachability, NodeEvent, NodeState, transition, estimate_clock_skew,
                        POLL_INTERVAL_SECONDS, STALE_AFTER_SECONDS)
from .view_freshness import NodeViews, ViewReceipt
from .protocol import NodeError
from .transport_events import classify_failure, compatible_capabilities, FailureKind
from .portal_views import NodeControlConfirm, NodeCopyPreview, NodeFiles, NodeOrderPrompt


def target_key(target):
    return (target['node_id'], target['queue_id'], target['job_id'], target['attempt_id'])


def row_key(target):
    return '/'.join(map(str, target_key(target)))


class NodesTable(DataTable):
    BINDINGS = [Binding('enter', 'select_cursor', 'Details')]

    async def _on_click(self, event):
        # Match the local JobsTable's rendered-cell hit test, but select the
        # complete remote target before dispatching an identity-bound request.
        row, column = event.style.meta.get('row'), event.style.meta.get('column')
        if (isinstance(row, int) and isinstance(column, int) and 0 <= row < self.row_count
                and 0 <= column < len(self.ordered_columns)
                and self.ordered_columns[column].key.value == 'files'):
            event.stop()
            event.prevent_default()  # Suppress RowSelected / Details for this click.
            self.app.select(self.ordered_rows[row].key.value)
            self.app.render_state()
            self.app.action_files()


@dataclass
class NodeLane:
    backend: object
    pending: deque = field(default_factory=deque)
    busy: bool = False
    active_request: tuple | None = None
    state: dict | None = None
    status_envelope: dict | None = None
    received: float | None = None
    request_started: float | None = None
    request_wall_started: float | None = None
    status_transport_age: float = 0.
    error: str | None = None
    reachability: Reachability = field(default_factory=Reachability)
    handshake_required: bool = True
    status_confirmed: bool = False
    reconnect_required: bool = False
    refusal_generation: int = 0
    observation_generation: int = 0
    receipts: dict = field(default_factory=dict)
    refusal: FailureKind | None = None
    clock_skew: object | None = None
    next_poll: float = 0
    next_progress: float = 0
    session: str | None = None
    clock_id: str | None = None
    cache: dict = field(default_factory=dict)

    def age(self, now):
        return None if self.received is None else now - self.received

    def fresh(self, now):
        age = self.age(now)
        receipt = self.receipts.get('status')
        return (receipt is not None and receipt.generation == self.observation_generation
                and self.error is None and not self.handshake_required and self.status_confirmed and self.refusal is None
                and self.reachability.state == NodeState.REACHABLE
                and age is not None and 0 <= age <= STALE_AFTER_SECONDS)


class NodesApp(NodeViews, NodeCatalogue, NavigationApp):
    TITLE = 'CMW / JOBS'
    CSS = JobsApp.CSS + '''
    .node-view-frame { border: solid $accent; border-title-color: $warning; border-subtitle-color: $warning; }
    #portal-frame { border: solid #5c8996; border-title-align: left; border-subtitle-align: left; }
    #node-summary { height: auto; max-height: 2; padding: 0; color: #b7d8dd; }
    #table { min-height: 4; margin: 0; }
    #selected { margin: 0; max-height: 3; }
    #execution { margin: 0; max-height: 1; }
    #progress { margin: 0; max-height: 8; }
    #message { max-height: 2; }
    #shortcuts { height: 1; padding: 0; }
    '''
    BINDINGS = [Binding('enter', 'details', 'Details'), Binding('f', 'files', 'Files'),
        Binding('g', 'history', 'Progress'), Binding('s', 'sources', 'Sources'),
        Binding('y', 'copy_snapshot', 'Copy'), Binding('l', 'logs', 'Logs'),
        Binding('b', 'scheduling', 'Scheduling'), Binding('r', 'review', 'Review'), Binding('slash','search','Search'), Binding('t','catalogue','Catalogue'), Binding('p', 'dispatch', 'Dispatch'),
        Binding('h', 'hold', 'Hold'), Binding('o', 'order', 'Order'),
        Binding('x', 'cancel', 'Cancel'), Binding('u', 'suspension', 'Suspend/Continue'),
        Binding('q', 'quit', 'Detach', priority=True), Binding('ctrl+c', 'detach', 'Detach', priority=True)]

    def __init__(self, backends, *, backend_factory=None, request_root=None,
                 clock=None, wall_clock=None, clipboard_backend=None):
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
        self.wall_clock = wall_clock or time.time
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
        self.init_catalogue()

    def compose(self):
        with Vertical(id='portal-frame') as frame:
            frame.border_title = 'CMW / JOBS · ALL NODES'
            frame.border_subtitle = 'Enter Details · F Files · G Progress · S Sources · Y Copy · L Logs'
            yield Static('Connecting to explicitly enrolled nodes…', id='node-summary', markup=False)
            yield NodesTable(id='table', cursor_type='row', zebra_stripes=True)
            yield SelectableStatic(id='selected', markup=False)
            yield Static(id='execution', markup=False)
            yield ScientificProgress(id='progress', markup=False)
            yield Static('/ Search · T Catalogue · Q / Ctrl-C detaches; execution continues.', id='message', markup=False)
            with Vertical(id='command-bar'):
                yield Static(self.shortcut_text('Suspend'), id='shortcuts', markup=False)

    def shortcut_text(self, action):
        controls = f'P Dispatch · H Hold · O Order · X Cancel · U {action} · B Queue · R Review · Q Detach'
        return controls if len(controls) <= self.size.width - 2 else controls.replace(' · ', '·')

    def on_mount(self):
        self.query_one(DataTable).focus()
        self.tick()
        self.set_interval(.25, self.tick)

    async def on_unmount(self):
        self.closed = True
        self.catalogue_generation += 1
        self.catalogue_cancel.set()
        for lane in self.lanes.values():
            lane.pending.clear()
        await asyncio.gather(*(asyncio.to_thread(lane.backend.close) for lane in self.lanes.values()))

    def on_resize(self):
        if self.is_mounted:
            # Resize reaches the app before its refreshed size is installed.
            self.call_after_refresh(self.render_state)

    def action_detach(self):
        self.exit()

    def check_action(self, action, parameters):
        if self.screen is not self.screen_stack[0]:
            return False
        if action in {'files', 'logs', 'dispatch', 'hold', 'order', 'cancel', 'suspension'}:
            selected = self.selected()
            return bool(selected and self.lanes[selected[0]].fresh(self.clock()))
        return True

    def navigation_context(self, screen):
        if screen is self.screen_stack[0]:
            return (tuple((label, lane.backend.node_id, lane.backend.queue_id)
                          for label, lane in self.lanes.items()),
                    self.selected_key, self.selection_generation)
        return super().navigation_context(screen)

    def navigation_selection(self, screen):
        return self.selected_key if screen is self.screen_stack[0] else super().navigation_selection(screen)

    def navigation_restore_selection(self, screen, snapshot):
        if screen is self.screen_stack[0]:
            return snapshot.selection in self.rows
        return super().navigation_restore_selection(screen, snapshot)

    def queue(self, owner, operation, params=None, target=None, callback=None, *, automatic=False):
        lane = self.lanes[owner]
        if self.closed or len(lane.pending) >= 16:
            return False
        request = (operation, params or {}, deepcopy(target), callback, automatic)
        if automatic:
            if any(item is not None and item[:3] == request[:3]
                   for item in (*lane.pending, lane.active_request)):
                return False
            if (operation == 'progress' and target is not None
                    and self.rows.get(self.selected_key, (None,))[0] == owner
                    and row_key(target) == self.selected_key
                    and 'progress' not in lane.cache.get(target_key(target), {})):
                # First selected evidence precedes background reads, never an
                # explicit operation or the request already owned by a worker.
                position = next((index for index, item in enumerate(lane.pending) if item[4]),
                                len(lane.pending))
                lane.pending.insert(position, request)
            else:
                lane.pending.append(request)
        else:
            lane.pending.appendleft(request)
        return True

    def tick(self):
        if self.closed or not self.is_running or not list(self.query('#node-summary')):
            return
        now = self.clock()
        for owner, lane in self.lanes.items():
            self.expire_receipts(lane, now)
            selected = self.selected()
            if (selected and selected[0] == owner and lane.fresh(now)
                    and now >= lane.next_progress
                    and (not lane.pending or 'progress' not in
                         lane.cache.get(target_key(selected[1]['target']), {}))):
                lane.next_progress = now + 4
                self.collect_progress(automatic=True)
                self.queue(owner, 'observe', target=selected[1]['target'], automatic=True)
            # A slow status reply must not monopolize every idle tick. Queue it
            # after selected reads so both remain serviced by the bounded lane.
            if not lane.busy and now >= lane.next_poll:
                self.queue(owner, 'capabilities' if lane.handshake_required else 'status', automatic=True)
                lane.next_poll = now + POLL_INTERVAL_SECONDS
            if lane.pending and not lane.busy:
                if lane.handshake_required or not lane.status_confirmed:
                    required = 'capabilities' if lane.handshake_required else 'status'
                    first = next((item for item in lane.pending if item[0] == required), None)
                    if first is None:
                        continue
                    lane.pending.remove(first)
                    lane.pending.appendleft(first)
                request = (*lane.pending.popleft(), lane.refusal_generation)
                lane.busy = True
                lane.active_request = request
                lane.request_started = now
                lane.request_wall_started = self.wall_clock()
                def collect(owner=owner, lane=lane, request=request):
                    operation, params, target, callback = request[:4]
                    try:
                        if operation == 'capabilities' and lane.reconnect_required:
                            reconnect = getattr(lane.backend, 'reconnect', lane.backend.close)
                            reconnect()
                            lane.reconnect_required = False
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

    def expire_receipts(self, lane, now):
        receipt = lane.receipts.get('status')
        if (receipt is not None and receipt.generation == lane.observation_generation
                and now - receipt.monotonic > STALE_AFTER_SECONDS):
            lane.observation_generation += 1

    def invalidate_session(self, owner):
        if self.catalogue_query is not None:
            self.catalogue_errors[owner] = 'Owner disconnected or session changed; apply search again'
        lane = self.lanes[owner]
        prefix = (lane.backend.node_id, lane.backend.queue_id)
        self.session_sources = {key: value for key, value in self.session_sources.items() if key[:2] != prefix}
        self.validations = {key: value for key, value in self.validations.items() if value[0][:2] != prefix}
        for view in (self.history_view, self.sources_view):
            if view is not None and view.selection[:2] == prefix and view.is_mounted:
                view.invalidate()

    def refuse_lane(self, owner, error, operation):
        lane = self.lanes[owner]
        failure = classify_failure(error, operation=operation)
        if not failure.controls_refused:
            return failure
        lane.error = str(error)
        lane.refusal_generation += 1
        lane.observation_generation += 1
        if failure.event is not None:
            lane.reachability = transition(lane.reachability, failure.event)
        lane.refusal = failure.kind if failure.kind in {
            FailureKind.SECURITY, FailureKind.READINESS, FailureKind.AUTHENTICATION,
            FailureKind.INCOMPATIBLE} else lane.refusal
        lane.handshake_required = lane.reconnect_required = True
        lane.status_confirmed = False
        lane.next_poll = self.clock() + (30 if lane.refusal is not None else POLL_INTERVAL_SECONDS)
        abandoned = tuple(lane.pending)
        lane.pending.clear()
        self.invalidate_session(owner)
        self.render_state()
        for queued_operation, _, queued_target, queued_callback in (item[:4] for item in abandoned):
            if queued_target is not None:
                cache = lane.cache.setdefault(target_key(queued_target), {})
                cache[queued_operation + '.error'] = str(error)
            if queued_callback is not None:
                queued_callback(None, NodeError(getattr(error, 'code', 'DISCONNECTED'),
                    'Queued request was not sent because the owner connection changed: ' + str(error)))
        return failure

    def accept(self, owner, request, envelope, error):
        lane = self.lanes[owner]
        lane.busy = False
        lane.active_request = None
        operation, params, target, callback = request[:4]
        now = self.clock()
        self.expire_receipts(lane, now)
        if len(request) > 5 and request[5] != lane.refusal_generation:
            # A separate control worker can refuse identity while this read is
            # in flight. Its older handshake must not clear the newer refusal.
            if callback is not None:
                callback(None, NodeError('HANDSHAKE_REQUIRED',
                    'Reply predates a newer owner refusal; fresh handshake required'))
            self.render_state()
            return
        if error is None and operation == 'capabilities':
            try:
                compatible_capabilities(envelope)
            except NodeError as exc:
                error, envelope = exc, None
        if error is not None:
            self.refuse_lane(owner, error, operation)
            if target is not None:
                cache = lane.cache.setdefault(target_key(target), {})
                cache[operation + '.error'] = str(error)
        else:
            if (envelope['node_id'], envelope['queue_id']) != (lane.backend.node_id, lane.backend.queue_id):
                self.accept(owner, request, None, NodeError('ENDPOINT_CHANGED', 'Reply belongs to another node or queue'))
                return
            changed = lane.session is not None and (envelope['session_id'] != lane.session or envelope['clock_id'] != lane.clock_id)
            if changed:
                lane.observation_generation += 1
                self.invalidate_session(owner)
                # Cached scientific values remain qualified earlier evidence;
                # source cursors and user selections never migrate sessions.
            if changed:
                for cached in lane.cache.values():
                    cached['progress.error'] = 'Endpoint session changed; earlier cached scientific evidence.'
                    cached['observe.error'] = 'Endpoint session changed; earlier cached worker evidence.'
            lane.session, lane.clock_id = envelope['session_id'], envelope['clock_id']
            if operation == 'capabilities':
                lane.reachability = transition(lane.reachability, NodeEvent.HANDSHAKE_SUCCESS, received_at=now)
                lane.error = lane.refusal = None
                lane.handshake_required = False
                lane.status_confirmed = False
                lane.next_poll = 0
            elif changed or lane.handshake_required:
                # An old in-flight reply cannot clear a refusal or establish a
                # new endpoint session's authority without capabilities.
                error = NodeError('HANDSHAKE_REQUIRED',
                    'Fresh compatible capabilities required after connection change')
                self.refuse_lane(owner, error, operation)
            else:
                lane.reachability = transition(lane.reachability, NodeEvent.SUCCESS, received_at=now)
            try:
                lane.clock_skew = estimate_clock_skew(lane.request_wall_started,
                    self.wall_clock(), envelope['observed_at'])
            except (NodeError, KeyError):
                lane.clock_skew = None
            # A read can finish after status expires. A later status must not
            # qualify that earlier payload, even if its endpoint session matches.
            generation = lane.observation_generation if operation == 'status' or lane.fresh(now) else -1
            receipt = ViewReceipt(now, self.wall_clock(), generation)
            if error is None and operation == 'status':
                lane.receipts['status'] = receipt
                lane.status_confirmed = error is None and not lane.handshake_required
                lane.status_envelope, lane.received = envelope, now
                lane.status_transport_age = max(0., now - lane.request_started) if lane.request_started is not None else 0.
                lane.state = observed_status_usage(envelope, now, now, transport_age=lane.status_transport_age)
                for request_id, pending in tuple(self.unknown_controls.items()):
                    if pending[0] == owner:
                        self.check_control_result(owner, request_id, pending[1])
            elif error is None and target is None:
                lane.receipts[operation] = receipt
            elif error is None:
                key = target_key(target)
                cache = lane.cache.setdefault(key, {})
                cache[operation] = (envelope, now)
                cache[operation + '.receipt'] = receipt
                cache.pop(operation + '.error', None)
                while len(lane.cache) > 4:
                    oldest = next(iter(lane.cache))
                    if oldest == key:
                        break
                    del lane.cache[oldest]
        if callback is not None:
            callback(envelope, error)
        self.render_state()
        if operation == 'capabilities' and error is None:
            self.tick()
        elif operation == 'status' and lane.fresh(now):
            self.refresh_recovered_views(owner)
            self.refresh_catalogue_owner(owner)

    def selected(self):
        selected = self.rows.get(self.selected_key)
        return selected if selected and not selected[1].get("_catalogue_pending") else None

    def label(self, owner, job):
        return f"{owner} / {self.lanes[owner].backend.queue} / {job['display_id']}"

    def connection_text(self, owner):
        lane = self.lanes[owner]
        age = lane.age(self.clock())
        if lane.refusal is not None:
            state = lane.refusal.value + ' refused'
        elif lane.reachability.state == NodeState.UNREACHABLE:
            state = 'node unreachable'
        elif lane.error:
            state = f'connection lost ({lane.reachability.consecutive_failures}/3)'
        elif age is None:
            state = 'connecting'
        elif not lane.fresh(self.clock()):
            state = 'stale transport'
        else:
            state = 'Online'
        skew = lane.clock_skew
        clock = (' · clock skew unassessed' if skew is None else
                 f' · clock {skew.seconds:+.1f}s ±{skew.uncertainty_seconds:.1f}s ({skew.level.value.lower()})')
        return state + (' · received —' if age is None else f' · received {age:.0f}s ago') + clock

    def machine_usage(self, owner):
        lane = self.lanes[owner]
        return observed_machine_usage(lane.status_envelope, lane.received, self.clock(),
            connected=lane.fresh(self.clock()), transport_age=lane.status_transport_age)

    def render_state(self):
        if not self.is_mounted or not list(self.query('#node-summary')):
            return
        self.sync_node_views()
        origin = self.screen_stack[0]
        table = origin.query_one('#table', DataTable)
        # Keyboard movement precedes its queued highlight. Preserve that current
        # identity before a status/progress refresh can restore the older row.
        if (table.has_focus and not self.navigation_pending(origin)
                and not self.navigation_origin_covered(origin) and self.selected() is not None
                and 0 <= table.cursor_row < len(self.row_keys)):
            key = self.row_keys[table.cursor_row]
            if key != self.selected_key and key in self.rows:
                self.select(key)
        view = self.capture_view(origin)
        before = (self.columns, tuple(self.row_keys), self.card_height, self.viewport)
        with table.prevent(DataTable.RowHighlighted):
            self._render_state()
        after = (self.columns, tuple(self.row_keys), self.card_height, self.viewport)
        if (before != after and not self.navigation_origin_covered(origin)
                and not self.navigation_pending(origin)):
            self.restore_view(view)

    def _render_state(self):
        now = self.clock()
        summaries = []
        retained = {}
        rows = {}
        for owner, lane in self.lanes.items():
            state = lane.state or {}
            controller = state.get('controller', {})
            counts = (lane.status_envelope or {}).get('result',{}).get('status_counts',{})
            count = counts.get('Run',sum(job.get('status') == 'Run' for job in state.get('jobs', ())))
            unknown = counts.get('Unknown',sum(job.get('status') == 'Unknown' for job in state.get('jobs', ())))
            condition = ('Online' if lane.fresh(now) else lane.refusal.value if lane.refusal is not None
                         else 'Unreachable' if lane.reachability.state == NodeState.UNREACHABLE
                         else 'Disconnected' if lane.error else 'Connecting' if lane.received is None else 'Stale')
            control = 'ctrl on' if controller.get('online') else 'ctrl off'
            age = lane.age(now)
            received = '—' if age is None else f'{age:.0f}s'
            identity = table_name(owner, 7 if self.size.width < 100 else 16)
            result = (lane.status_envelope or {}).get('result',{})
            partial_counts = 'status_counts' not in result and result.get('has_more',False)
            count_scope = 'page Run' if partial_counts else 'Run' if lane.fresh(now) else 'last Run'
            status = f'{identity} {condition} {received} · {count_scope} {count}' + (f' Unknown {unknown}' if unknown else '')
            if partial_counts:
                status += ' · other blockers unknown'
            machine = machine_text(self.machine_usage(owner)).removeprefix('Machine: ')
            if self.size.width >= 100:
                status += f' · {control}'
            width = max(1, self.size.width - 2)
            if not lane.fresh(now) and lane.state is not None:
                caption = self.receipt_caption(owner, self.view_receipt(owner, 'status'))
                retained[owner] = caption
                status = caption.format(f'{condition} · {count_scope} {count}', width)
            summary = status + ('\nMachine: ' if len(self.lanes) == 1 else ' · ') + machine
            summaries.append('\n'.join(fit_text(line, width) for line in summary.splitlines()))
            for job in state.get('jobs', ()):
                target = job.get('target')
                if target and (target['node_id'], target['queue_id']) == (lane.backend.node_id, lane.backend.queue_id):
                    rows[row_key(target)] = (owner, job)
        rows = self.catalogue_rows(rows)
        self.rows = rows
        summary = self.query_one('#node-summary', Static)
        summary.styles.max_height = max(2, len(self.lanes))
        summary.update(literal('\n'.join(summaries)))
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
            if not lane.fresh(now) or job.get('_catalogue_stale'):
                status = 'Last ' + status
            values = (owner, job['display_id'], '[F]', job.get('name', ''), status, elapsed(job.get('elapsed')))
            for (column, _, width), value in zip(widths, values):
                table.update_cell(key, column, table_name(value, width))
        if self.selected_key is None and keys and self.catalogue_auto_select:
            self.select(keys[0])
        if self.selected_key in keys:
            with table.prevent(DataTable.RowHighlighted):
                table.move_cursor(row=keys.index(self.selected_key), animate=False, scroll=False)
        selected = self.selected()
        frame = self.query_one('#portal-frame')
        owner = selected[0] if selected else next(iter(self.lanes))
        title = 'CMW / JOBS · ALL NODES'
        if self.catalogue_query is not None:
            for label in self.catalogue_pages:
                if self.catalogue_page_stale(label):
                    retained[label] = self.receipt_caption(label, self.catalogue_receipts.get(label))
        if retained:
            caption = retained.get(owner) or next(iter(retained.values()))
            title = caption.format(title, self.size.width - 6)
        frame.border_title = literal(title)
        table.set_class(selected is None, 'navigation-unavailable')
        if selected:
            owner, job = selected
            lane = self.lanes[owner]
            status = effective_status(job)

            usage = (lane.state or {}).get('job_usage', {}).get(job['attempt_id'], job.get('usage'))
            usage = deepcopy(usage or {})
            if usage.get('age_seconds') is not None:
                usage['age_seconds'] += lane.age(now) or 0
                if usage['age_seconds'] > TELEMETRY_MAX_AGE:
                    usage['stale'] = True
            if not lane.fresh(now):
                usage['stale'] = True
            identity = 'SELECTED · ' + self.label(owner, job)
            if lane.fresh(now):
                connection = self.connection_text(owner) + ' · ' + status
            else:
                reason = self.connection_text(owner).split(' · received')[0]
                identity += f' · {status} — {reason}'
                connection = self.receipt_caption(owner, self.view_receipt(owner, 'status')).format(
                    '', max(1, self.size.width - 5))
            text = (identity + '\n' + connection + '\n' +
                    usage_summary(usage, width=max(20, self.size.width - 7)))
            self.query_one('#selected', Static).update(literal(text))
            cache = lane.cache.get(target_key(job['target']), {})
            observed = cache.get('observe')
            execution = observed[0]['result']['execution'] if observed else None
            suffix = execution.get('summary') or execution.get('observed_execution') if execution else 'worker evidence unavailable'
            execution_text = f'Execution: reported by {owner} · {suffix}'
            if observed and (not self.receipt_current(owner, cache.get('observe.receipt')) or cache.get('observe.error')):
                execution_text = self.receipt_caption(owner, cache.get('observe.receipt')).format(
                    f'Execution: {suffix}', max(1, self.size.width - 5))
            self.query_one('#execution', Static).update(literal(execution_text))
            action = 'Continue' if effective_status(job) == 'Suspended' else 'Suspend'
            self.query_one('#shortcuts', Static).update(self.shortcut_text(action) if lane.fresh(now) else
                'Owner commands disabled until fresh status · B Queue · R Review · Q Detach')
            self.render_progress(owner, job, cache)
        else:
            self.query_one('#selected', Static).update('Selected attempt is no longer in the current node snapshot.' if self.selected_key else 'No managed jobs reported. Select an enrolled node through the CLI to submit.')
            self.query_one('#execution', Static).update('')
            self.query_one('#progress', Static).display = False
        if self.catalogue_query is not None:
            if selected is None:
                self.query_one('#selected',Static).update('No current visible attempt selected. Select a catalogue row explicitly.')
            self.message(self.catalogue_summary())

    def select(self, key):
        if key == self.selected_key:
            return
        self.selected_key = key
        self.selection_generation += 1
        # Superseded automatic reads cannot inform the new selection. Preserve
        # explicit requests and any request already owned by a running worker.
        for lane in self.lanes.values():
            lane.pending = deque(request for request in lane.pending
                if not (request[4] and request[0] in {'progress', 'observe', 'history'}
                        and request[2] is not None and row_key(request[2]) != key))
        self.catalogue_hydrate(key)
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
        width = max(20, self.size.width - 7)
        snapshot = self.selected_snapshot(owner, job)
        if snapshot is None:
            self.card_height, self.viewport = 3, tuple(self.size)
            message = 'Scientific: ' + cache.get('progress.error', 'awaiting bounded observation…')
            widget.update(literal(compact_frame([message], width)))
            widget.styles.max_height = 3
            return
        entry, received = cache['progress']
        # Both timestamps in the formatter originate on the same endpoint clock.
        # Only elapsed transport age comes from this frontend monotonic clock.
        age = max(0., self.clock() - received)
        endpoint_now = entry['observed_at'] + age
        budget = min(8, max(3, self.size.height - 15))
        if cache.get('progress.error'):
            self.message('Scientific read unavailable; earlier cached evidence. ' + cache['progress.error'])
        receipt = cache.get('progress.receipt')
        caption = (self.receipt_caption(owner, receipt)
                   if not self.receipt_current(owner, receipt) or cache.get('progress.error') else None)
        text = progress_text(snapshot, compact=self.size.width < 110 or self.size.height < 36,
                             width=width, now=endpoint_now, max_lines=budget, require_frame=True,
                             receipt_caption=caption)
        rendered = literal(text)
        for index, char in enumerate(rendered.plain):
            if char in '─│┌┐└┘├┤┴':
                rendered.stylize('#5c8996', index, index + 1)
        widget.styles.max_height = max(budget, len(text.splitlines()))
        widget.update(rendered)
        self.card_height, self.viewport = len(text.splitlines()), tuple(self.size)

    def collect_progress(self, *, automatic=False):
        selected = self.selected()
        if selected is None:
            return
        owner, job = selected
        target = job['target']
        view = self.history_view
        if view is not None and not view.closed and not view.expired and view.selection == target_key(target):
            if self.lanes[owner].state is not None and not self.lanes[owner].fresh(self.clock()):
                return
            generation = view.request_generation
            def history_result(envelope, error):
                if error or view.closed or view.expired or generation != view.request_generation or view.selection != target_key(target):
                    return
                result = envelope['result']
                page = decode_typed(result['page'], HistoryPage)
                if result.get('snapshot') is not None:
                    decode_typed(result['snapshot'], ProgressSnapshot)
                    cache = self.lanes[owner].cache.setdefault(target_key(target), {})
                    cache['progress'] = ({**envelope, 'result': {'snapshot': result['snapshot']}}, self.clock())
                    cache['progress.receipt'] = self.view_receipt(owner, 'history', target)
                    cache.pop('progress.error', None)
                view.bind_context((owner, deepcopy(target)))
                view.accept_page((owner, deepcopy(target)), page, generation)
                self.view_received(view, owner, self.view_receipt(owner, 'history', target))
            self.queue(owner, 'history', view.parameters(), target, history_result, automatic=automatic)
        else:
            self.queue(owner, 'progress', target=target, automatic=True)

    def release_history(self, context):
        owner, target = context
        lane = self.lanes[owner]
        lane.pending = deque(request for request in lane.pending
            if not (request[4] and request[0] == 'history' and request[2] == target))
        if not lane.handshake_required or lane.error is None:
            self.queue(owner, 'history.release', target=target)

    def action_history(self):
        selected = self.selected()
        if selected:
            owner, job = selected
            view = ProgressHistory(target_key(job['target']), self.label(owner, job))
            self.push_screen(self.bind_node_view(view, owner))

    def session_source_for(self, selection):
        return self.session_sources.get(selection)

    def request_sources(self, view, kind, payload=None):
        selected = self.selected()
        if selected is None or target_key(selected[1]['target']) != view.selection:
            view.invalidate()
            return
        owner, job = selected
        if not self.lanes[owner].fresh(self.clock()) or (kind != 'inspect' and not self.view_commands_allowed(view)):
            self.call_later(view.accept_result, kind, None, 'Owner unavailable; reopen Sources after a fresh status.', view.request_generation)
            return
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
            receipt = self.view_receipt(owner, 'sources.' + kind, target) if error is None else None
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
            async def deliver():
                if view.closed or view.expired or generation != view.request_generation:
                    return
                await view.accept_result(kind, result, str(error) if error else None, generation)
                if error is None and not view.closed and not view.expired and generation == view.request_generation:
                    self.view_received(view, owner, receipt)
            self.call_later(deliver)
        self.queue(owner, 'sources.' + kind, params, target, accept)

    def clear_session_source(self, view):
        if not self.view_commands_allowed(view):
            return
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
            if not self.lanes[owner].fresh(self.clock()):
                self.message('Sources unavailable until a fresh owner status arrives.')
                return
            view = Sources(target_key(job['target']), self.label(owner, job))
            self.push_screen(self.bind_node_view(view, owner))

    def selected_request(self, operation, params, callback):
        selected = self.selected()
        if selected:
            owner, job = selected
            if not self.lanes[owner].fresh(self.clock()):
                self.message('Owner commands disabled until fresh status. Nothing was queued.')
                return
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
        def show(owner, job, result):
            view = NodeFiles(owner, job['target'], self.label(owner, job), result,
                            local=self.lanes[owner].backend.connection['transport'] == 'local')
            self.push_screen(self.bind_node_view(view, owner,
                received=self.view_receipt(owner, 'locations', job['target'])))
        self.selected_request('locations', {}, show)

    def action_logs(self):
        def show(owner, job, result):
            view = FrozenText(self.label(owner, job) + '\n' + str(result.get('node_path', ''))
                              + '\n\n' + result['text'], 'BOUNDED STDOUT · execution node')
            self.push_screen(self.bind_node_view(view, owner,
                received=self.view_receipt(owner, 'logs', job['target'])))
        self.selected_request('logs', {'stream': 'stdout', 'bytes': 16384}, show)

    def selected_review(self, owner, job):
        cache = self.lanes[owner].cache.get(target_key(job['target']), {})
        entry = cache.get('review')
        if entry is None:
            return None
        try:
            record = decode_typed(entry[0]['result']['review'], ReviewRecord)
        except ValueError:
            return None
        if (record.node_id, record.queue_id, record.job_id, record.attempt_id) != target_key(job['target']):
            return None
        return cached_review(record)

    def action_review(self):
        selected = self.selected()
        if selected is None:
            return
        owner, job = selected
        target = deepcopy(job['target'])
        def request(operation, params, callback):
            if not self.lanes[owner].fresh(self.clock()):
                callback(None, NodeError('STALE', 'Owner status is not fresh; awaiting compatible handshake and status'))
                return
            if operation in {'review.start', 'review.cancel'} and not self.view_commands_allowed(view):
                callback(None, NodeError('STALE', 'Review commands require a new observation for this attempt'))
                return

            def accepted(envelope, error):
                record = None
                if error is None:
                    try:
                        record = decode_typed(envelope['result']['review'], ReviewRecord)
                        if (record.node_id,record.queue_id,record.job_id,record.attempt_id) != target_key(target):
                            raise NodeError('WRONG_TARGET','Review belongs to another selected attempt')
                        cache = self.lanes[owner].cache[target_key(target)]
                        cache['review'] = cache[operation]
                        receipt = cache['review.receipt'] = self.view_receipt(owner, operation, target)
                        self.view_received(view, owner, receipt)
                    except (ValueError,TypeError,KeyError) as exc:
                        error = exc
                callback(record,error)
            if not self.queue(owner, operation, params, target, accepted):
                callback(None,NodeError('BUSY','Owner observation queue is full or closed'))
        view = ReviewScreen(self.label(owner, job), request, cached=self.selected_review(owner, job))
        self.push_screen(self.bind_node_view(view, owner, received=self.view_receipt(owner, 'review', target)))

    def snapshot_receipt(self, owner, job, components):
        # A composite still contains last-reported data if any displayed component does.
        target = job['target']
        receipts = [self.view_receipt(owner, 'status')]
        cache = self.lanes[owner].cache.get(target_key(target), {})
        shown = cache.get('show')
        if shown and shown[0]['result'].get('job') is job:
            receipts.append(self.view_receipt(owner, 'show', target))
        receipts.extend(self.view_receipt(owner, name, target) for name in components if name in cache)
        if None in receipts:
            return None
        return min(receipts, key=lambda receipt: (receipt.generation, receipt.monotonic))

    def action_details(self):
        selected = self.selected()
        if selected:
            owner, job = selected
            text = (self.label(owner, job) + '\n' + self.connection_text(owner) + '\n\n' +
                detail(job) + '\n\n' + machine_detail(self.machine_usage(owner)) + '\n\n' +
                json.dumps({'target': job['target'], 'revision': job.get('revision')}, indent=2))
            snapshot = self.selected_snapshot(owner, job)
            if snapshot is not None:
                cache = self.lanes[owner].cache[target_key(job['target'])]
                entry, received = cache['progress']
                endpoint_now = entry['observed_at'] + max(0., self.clock() - received)
                text += '\n\nSCIENTIFIC EVIDENCE — parser success does not verify execution\n'
                if cache.get('progress.error'):
                    text += 'Scientific read unavailable; earlier cached evidence. ' + cache['progress.error'] + '\n'
                text += progress_text(snapshot, detailed=True, now=endpoint_now)
            review = self.selected_review(owner,job)
            if review is not None:
                text += '\n\n' + review_text(review)
            from cmw.doctor.presentation import DoctorDetails
            target = deepcopy(job['target'])
            def inspect():
                if self.view_commands_allowed(view):
                    self.open_doctor(owner, target, self.label(owner, job))

            view = DoctorDetails(text, 'JOB DETAILS · cached node report', target_key(target), inspect)
            receipt = self.snapshot_receipt(owner, job, ('progress', 'review'))
            self.push_screen(self.bind_node_view(view, owner, received=receipt))

    def open_doctor(self, owner, target, label):
        from cmw.doctor.presentation import DoctorReport
        captured_target = deepcopy(target)
        backend = self.lanes[owner].backend
        cached = deepcopy(self.lanes[owner].status_envelope)
        started = self.clock()
        view = DoctorReport(target_key(captured_target), label,
                            context='Cached status at request: ' + self.connection_text(owner))
        self.push_screen(self.bind_node_view(view, owner, received=self.view_receipt(owner, 'status')))

        def inspected(envelope, error):
            if view not in self.screen_stack:
                return
            if error:
                def refused():
                    from cmw.doctor.nodes import compose_refusal
                    return compose_refusal(backend, error, target=captured_target, cached=cached)
                view.load(refused)
                return
            captured_envelope = deepcopy(envelope)
            request_seconds = max(0., self.clock() - started)
            receipt = self.view_receipt(owner, 'doctor.inspect', captured_target)
            received_at = receipt.wall

            def compose():
                from cmw.doctor.nodes import compose_report
                return compose_report(backend, captured_envelope, target=captured_target,
                                      request_seconds=request_seconds, received_at=received_at)

            view.load(compose, on_displayed=lambda: self.view_received(view, owner, receipt))

        # This bounded operation rejects old endpoints explicitly. A broad
        # capabilities probe also hashes unrelated runner profiles and storages.
        if not self.queue(owner, 'doctor.inspect', {}, captured_target, inspected):
            self.call_after_refresh(view.unavailable,
                'Observation queue is busy; reopen Doctor after pending requests finish.')

    def action_scheduling(self):
        selected = self.selected()
        if selected:
            owner, job = selected
            state = self.lanes[owner].state or {}
            text = self.label(owner, job) + '\n' + self.connection_text(owner) + '\n' + machine_detail(self.machine_usage(owner))
            text += '\n\n' + json.dumps({key: state.get(key) for key in ('controller', 'admission', 'external_activity', 'scheduler')}, indent=2)
            view = FrozenText(text, 'NODE-LOCAL SCHEDULING · no cross-node resource pool')
            self.push_screen(self.bind_node_view(view, owner, received=self.view_receipt(owner, 'status')))

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
            execution_error=cache.get('observe.error') or lane.error,
            machine_usage=self.machine_usage(owner), review=self.selected_review(owner,job))
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
            owner, job = self.selected()
            target = job['target']
            components = ('observe', 'progress', 'review')

            def refresh(identity):
                captured = self.capture_copy_snapshot(identity)
                if captured is not None:
                    receipt = self.snapshot_receipt(owner, self.selected()[1], components)
                    self.view_received(view, owner, receipt)
                return captured

            view = NodeCopyPreview(snapshot, target_key(target), refresh)
            self.push_screen(self.bind_node_view(view, owner,
                received=self.snapshot_receipt(owner, job, components)))

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
        if view.busy or not self.view_commands_allowed(view):
            return
        if not Path(specification_path).is_absolute() or not Path(destination).is_absolute():
            view.query_one('#fetch-status', Static).update('Enter absolute local paths for the explicit specification and new destination.')
            return
        view.busy = True
        request_id = uuid4().hex
        view.query_one('#fetch-confirm').disabled = True
        view.query_one('#fetch-status', Static).update('Fetching selected artifacts; monitoring continues independently…')
        def fetching():
            backend = error = None
            try:
                from .cli import _read_spec
                from .client import fetch
                specification = _read_spec(specification_path)
                backend = self.new_backend(view.owner)
                result = fetch(backend, view.target, specification, Path(destination), root=self.request_root, request_id=request_id)
                ack_error = result.get('result', {}).get('acknowledgment_error')
                if ack_error:
                    error = NodeError(ack_error['code'], ack_error['message'])
                text = 'Replica fetched. ' + json.dumps(result, ensure_ascii=True)[:3000]
            except Exception as exc:
                if backend is not None and isinstance(exc, NodeError):
                    error = exc
                text = 'Fetch request ' + request_id + ' not completed: ' + str(exc)
            finally:
                if backend is not None:
                    backend.close()
            if not self.closed:
                self.call_from_thread(self.finish_fetch, view, text, error)
        self.run_worker(fetching, thread=True, group='fetch', exit_on_error=False)

    def finish_fetch(self, view, text, error=None):
        view.busy = False
        if error is not None:
            self.refuse_lane(view.owner, error, 'fetch.prepare')
        if view.is_mounted:
            view.node_commands_changed(self.view_commands_allowed(view))
            view.query_one('#fetch-status', Static).update(literal(text))
        else:
            self.message(text)

    def open_local_location(self, view, location):
        if not self.view_commands_allowed(view) or self.lanes[view.owner].backend.connection['transport'] != 'local':
            return
        def accepted(envelope, error):
            if not view.is_mounted or not self.view_commands_allowed(view):
                return
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
                view = NodeControlConfirm(owner, job['target'], self.label(owner, job), envelope['result'])
                self.push_screen(self.bind_node_view(view, owner, received=self.view_receipt(owner, 'control.preview', target)))
        self.queue(owner, 'control.preview', params, target, accepted)

    def start_control(self, view, reason):
        if view.busy or view.owner in self.operation_busy:
            return
        lane = self.lanes[view.owner]
        if not lane.fresh(self.clock()) or not self.view_commands_allowed(view) or lane.backend.connection.get('read_only'):
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
            error = None
            try:
                from .client import apply_control
                backend = self.new_backend(view.owner)
                result = apply_control(backend, deepcopy(view.preview), root=self.request_root, confirm=True, reason=reason, request_id=request_id)
                text = json.dumps(result, ensure_ascii=True)[:3000]
            except Exception as exc:
                error = exc
                unknown = classify_failure(exc, operation='control').outcome_unknown
                text = 'Request ' + request_id + ': ' + str(exc) + ' · Query this original ID; no automatic replay.'
            finally:
                if backend is not None:
                    backend.close()
            if not self.closed:
                self.call_from_thread(self.finish_control, view, text, unknown, error)
        self.run_worker(controlling, thread=True, group='control-' + view.owner, exit_on_error=False)

    def finish_control(self, view, text, unknown=False, error=None):
        if unknown:
            self.unknown_controls[view.request_id] = (view.owner, view)
        self.operation_busy.discard(view.owner)
        self.lanes[view.owner].next_poll = 0
        if error is not None:
            self.refuse_lane(view.owner, error, 'control')
        if unknown:
            self.check_control_result(view.owner, view.request_id, view)
        if view.is_mounted:
            view.query_one('#node-control-status', Static).update(literal(text))
        self.message(text)
        # A submitted preview is single-use, including uncertain outcomes.

    def check_control_result(self, owner, request_id, view=None):
        if request_id in self.lookup_busy or self.closed:
            return
        self.lookup_busy.add(request_id)
        def checking():
            backend, known, error = None, False, None
            try:
                backend = self.new_backend(owner)
                value = backend.call('request', {'request_id': request_id})['result']['request']
                known = True
                text = ('Request ' + request_id + ': ' + json.dumps(value, ensure_ascii=True)[:2500]
                        if value is not None else 'Request ' + request_id + ': no durable outcome recorded. No replay; review state before a fresh explicit action.')
            except Exception as exc:
                error = exc
                text = 'Request ' + request_id + ': outcome still unknown — ' + str(exc)
            finally:
                if backend is not None:
                    backend.close()
            if not self.closed:
                self.call_from_thread(self.accept_control_lookup, owner, request_id, view, text, known, error)
        self.run_worker(checking, thread=True, group='request-' + request_id, exit_on_error=False)

    def accept_control_lookup(self, owner, request_id, view, text, known, error):
        self.lookup_busy.discard(request_id)
        if error is not None:
            self.refuse_lane(owner, error, 'request')
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
        owner, _ = selected
        lane = self.lanes[owner]
        if not lane.fresh(self.clock()) or lane.backend.connection.get('read_only'):
            self.message('Order unavailable until the node is fresh and writable.')
            return
        identity = self.selected_key
        receipt = self.view_receipt(owner, 'status')
        def ordered(value):
            if value and self.selected_key == identity and self.receipt_current(owner, receipt):
                try:
                    position = int(value)
                except ValueError:
                    self.message('Order must be an integer.')
                    return
                self.preview_control('move', position=position)
        view = NodeOrderPrompt('New queue position (held head still blocks following jobs):', order=True)
        self.push_screen(self.bind_node_view(view, owner, received=receipt), ordered)
