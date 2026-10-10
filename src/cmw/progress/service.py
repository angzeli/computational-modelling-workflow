"""Ephemeral bounded observation scheduling; callers run polls off the UI thread.

This service creates no worker threads or controller. A timed-out caller cannot
release its still-running filesystem operation: its context and global slot stay
occupied until that operation actually returns.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import threading
import time
import uuid

from .models import HistoryPage, ProgressSnapshot, RunContext, SourceState, SourceUpdate
from .history import CursorError, HistoryCache, Window, query_from_cursor, source_revision, version_guard
from .reader import RegularFileReader
from .registry import get_registration


DEFAULT_POLL_SECONDS = 4.0
MAX_SOURCES = 8


@dataclass
class _Observation:
    context: RunContext
    adapter: object
    readers: tuple
    snapshot: ProgressSnapshot | None = None
    busy: bool = False
    retired: bool = False
    last_use: int = 0
    updates: dict = field(default_factory=dict)
    caught_up: bool = False
    history: HistoryCache | None = None
    replay: object | None = None
    history_epoch: int = 0


@dataclass
class _Replay:
    observation: _Observation
    window: Window
    revision: str
    generations: dict


class ObservationService:
    """Share one adapter/readers per immutable binding, with bounded concurrency."""

    def __init__(self, *, max_contexts=8, max_inflight=2,
                 reader_factory=RegularFileReader, registration_getter=get_registration,
                 clock=time.time, monotonic_clock=time.monotonic):
        if max_contexts < 1 or max_inflight < 1:
            raise ValueError('Observation context and concurrency limits must be positive')
        self.max_contexts = max_contexts
        self.max_inflight = max_inflight
        self.reader_factory = reader_factory
        self.registration_getter = registration_getter
        self.clock = clock
        self.monotonic_clock = monotonic_clock
        self.clock_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._contexts = {}
        self._inflight = 0
        self._sequence = 0
        self._closed = False

    @staticmethod
    def _empty(context, status, message):
        return ProgressSnapshot(context, adapter=context.engine, status=status,
                                phase=message, read_health=status,
                                messages=(message,), limitations=context.limitations)

    @staticmethod
    def _close_observation(observation):
        for reader in observation.readers:
            reader.close()
        if observation.replay is not None:
            ObservationService._close_observation(observation.replay.observation)
            observation.replay = None

    @staticmethod
    def _unavailable(context, previous, status, message):
        if previous is None:
            return ObservationService._empty(context, status, message)
        return replace(previous, read_health=status,
                       messages=tuple(dict.fromkeys((*previous.messages, message)))[-32:])

    def retain(self, contexts):
        """Release unselected contexts, retaining an in-flight slot until return."""
        keep = {context.key if isinstance(context, RunContext) else context for context in contexts}
        with self._lock:
            for key, observation in tuple(self._contexts.items()):
                if key not in keep:
                    if observation.busy:
                        observation.retired = True
                    else:
                        self._contexts.pop(key)
                        self._close_observation(observation)

    def close(self):
        """Detach this client without signalling or waiting for any computation."""
        with self._lock:
            self._closed = True
            for key, observation in tuple(self._contexts.items()):
                if observation.busy:
                    observation.retired = True
                else:
                    self._contexts.pop(key)
                    self._close_observation(observation)

    def _prepare(self, context):
        if self._closed:
            return None, self._empty(context, 'CLOSED', 'Observation client is closed')
        registration = self.registration_getter(context.engine)
        if registration is None:
            return None, self._empty(context, 'ADAPTER_UNAVAILABLE', 'Progress adapter unavailable')
        if context.binding_status != 'BOUND':
            return None, self._empty(context, 'SOURCE_UNRESOLVED', '; '.join(context.limitations) or 'Source binding is unresolved')
        if not context.key or not context.sources or len(context.sources) > MAX_SOURCES:
            return None, self._empty(context, 'SOURCE_UNRESOLVED', 'An explicit binding with 1–8 sources is required')
        observation = self._contexts.get(context.key)
        if observation is not None:
            if observation.context != context:
                return None, self._empty(context, 'SOURCE_UNRESOLVED', 'Context changed without a new binding revision')
            if observation.retired:
                return None, self._empty(context, 'RETIRED', 'Selected source binding was released; earlier read is still finishing')
            if observation.busy:
                return None, self._unavailable(context, observation.snapshot, 'BUSY', 'Previous observation is still in flight')
        if self._inflight >= self.max_inflight:
            return None, self._unavailable(context, observation.snapshot if observation else None,
                                           'BUSY', 'Global observation concurrency limit reached')
        if observation is None:
            if len(self._contexts) >= self.max_contexts:
                idle = [(item.last_use, key) for key, item in self._contexts.items() if not item.busy]
                if not idle:
                    return None, self._empty(context, 'BUSY', 'All bounded observation contexts are in flight')
                _, oldest = min(idle)
                self._close_observation(self._contexts.pop(oldest))
            readers = []
            try:
                adapter = registration.factory(context)
                for source in context.sources:
                    readers.append(self.reader_factory(source))
            except Exception as exc:
                for reader in readers:
                    reader.close()
                return None, self._empty(context, 'ADAPTER_ERROR', f'Observation setup failed: {type(exc).__name__}: {exc}')
            observation = _Observation(context, adapter, tuple(readers), history=HistoryCache(context))
            self._contexts[context.key] = observation
        self._sequence += 1
        observation.last_use = self._sequence
        observation.busy = True
        self._inflight += 1
        return observation, None

    @staticmethod
    def _latest(values):
        available = [value for value in values if value is not None]
        return max(available) if available else None

    def _observe(self, observation, *, historical=False, generations=None):
        now = self.clock()
        monotonic_now = self.monotonic_clock()
        roles = getattr(observation.adapter, 'read_roles', lambda: None)()
        updates = []
        for reader in observation.readers:
            role = reader.source.role
            if roles is None or role in roles or role.lower() in roles:
                update = reader.poll(now)
                if generations is not None:
                    expected = generations.get(role)
                    if (expected is None or update.state.identity != expected.identity
                            or update.state.generation > 1):
                        raise CursorError('Source changed during history reconstruction')
                    update = replace(update,
                                     state=replace(update.state, generation=expected.generation),
                                     lines=tuple(replace(line, evidence=replace(line.evidence, generation=expected.generation))
                                                 for line in update.lines))
                observation.updates[role] = update
            else:
                previous = observation.updates.get(role)
                state = (replace(previous.state, bytes_read=0) if previous else
                         SourceState(role, reader.source.path))
                update = SourceUpdate(reader.source, state)
            updates.append(update)
        updates = tuple(updates)
        hook = getattr(observation.adapter, 'set_observation', None)
        if hook is not None:
            hook(monotonic_now, historical=historical or not observation.caught_up,
                 clock_id=self.clock_id)
        if any(update.state.health == 'BINDING_CHANGED' for update in updates):
            result = self._empty(observation.context, 'SOURCE_UNRESOLVED', 'Declared source directory changed; resolve its binding again')
        else:
            result = observation.adapter.update(updates, now)
            if not isinstance(result, ProgressSnapshot) or result.context != observation.context:
                raise ValueError('Adapter returned an invalid or mismatched progress snapshot')
        states = tuple(update.state for update in updates)
        errors = [update for update in updates
                  if update.state.health not in {'OK', 'MALFORMED'}
                  and not (update.source.optional and update.state.health in {'MISSING', 'VOLUME_UNAVAILABLE'})]
        healthy = any(state.health in {'OK', 'MALFORMED'} for state in states)
        health = ('PARTIAL' if healthy else 'UNAVAILABLE') if errors else 'OK'
        if not errors and any(state.health == 'MALFORMED' for state in states):
            health = 'DEGRADED'
        if result.read_health not in {'PENDING', 'OK'}:
            health = result.read_health
        limitations = tuple(dict.fromkeys((*result.limitations, *observation.context.limitations,
                                           *(text for state in states for text in state.limitations))))
        messages = tuple(dict.fromkeys((*result.messages,
                                       *(f'{update.source.role}: {update.reason or update.state.health}' for update in errors))))
        pending = getattr(observation.adapter, 'has_pending_work', False)
        catching_up = any(state.has_more for state in states) or bool(pending() if callable(pending) else pending)
        observation.caught_up = not catching_up and not errors
        if observation.history is not None:
            revision = source_revision(observation.context, states)
            if observation.history.revision != revision:
                observation.history.reset(revision)
                if observation.replay is not None:
                    self._close_observation(observation.replay.observation)
                    observation.replay = None
            observation.history.columns = result.history_columns
            drain = getattr(observation.adapter, 'drain_history', None)
            if drain is not None:
                observation.history.ingest(drain())
        if catching_up:
            messages = (*messages, 'Catching up in bounded chunks; displayed evidence may be historical')
        return replace(result, sources=states, read_health=health,
                       completeness='CATCHING_UP' if catching_up else result.completeness,
                       last_success_at=self._latest(state.last_success_at for state in states),
                       last_source_change_at=self._latest(state.last_change_at for state in states),
                       last_file_growth_at=self._latest(state.last_growth_at for state in states),
                       messages=messages[-32:], limitations=limitations[-32:])

    def snapshot(self, context: RunContext):
        """Return the existing immutable card snapshot without reading files."""
        with self._lock:
            observation = self._contexts.get(context.key)
            return observation.snapshot if observation and observation.context == context else None

    def release_history(self, context: RunContext):
        """Cooperatively retire a history replay while retaining the live card."""
        with self._lock:
            observation = self._contexts.get(context.key)
            if observation is None:
                return
            observation.history_epoch += 1
            if not observation.busy and observation.replay is not None:
                self._close_observation(observation.replay.observation)
                observation.replay = None

    @staticmethod
    def _history_empty(context, status, message, revision=''):
        return HistoryPage(context, status=status, source_revision=revision,
                           messages=(message,))

    def _new_replay(self, observation, query):
        registration = self.registration_getter(observation.context.engine)
        readers = []
        try:
            adapter = registration.factory(observation.context)
            for source in observation.context.sources:
                readers.append(self.reader_factory(source))
        except Exception:
            for reader in readers:
                reader.close()
            raise
        replay_observation = _Observation(observation.context, adapter, tuple(readers))
        replay = _Replay(replay_observation, Window(query), observation.history.revision,
                         {state.role: state for state in observation.snapshot.sources})
        observation.replay = replay
        return replay

    def _history_page(self, observation, *, cursor, limit, parent_id, stage, segment):
        context, cache = observation.context, observation.history
        snapshot = observation.snapshot
        if (snapshot.status == 'SOURCE_UNRESOLVED'
                or any(state.health == 'BINDING_CHANGED' for state in snapshot.sources)):
            return self._history_empty(context, 'CURSOR_INVALID' if cursor else 'SOURCE_UNRESOLVED',
                                       'Declared source binding is unresolved; resolve it again', cache.revision)
        if not callable(getattr(observation.adapter, 'drain_history', None)):
            return self._history_empty(context, 'HISTORY_UNAVAILABLE', 'This adapter does not supply history', cache.revision)
        guard = version_guard(snapshot.sources)
        cursor_args = {'session_id': self.clock_id, 'guard': guard}
        query = query_from_cursor(context, cache.revision, cursor, limit=limit,
                                  parent_id=parent_id, stage=stage, segment=segment, **cursor_args)
        cached = cache.cached(query)
        if snapshot.read_health not in {'OK', 'DEGRADED', 'PENDING'}:
            if cached is not None:
                page = cached.page(context, cache.revision, cache.columns, retained=cache.retained(),
                                   status=snapshot.read_health, **cursor_args)
                return replace(page, messages=tuple(dict.fromkeys((*page.messages, *snapshot.messages))))
            return self._history_empty(context, snapshot.read_health,
                                       '; '.join(snapshot.messages) or 'History sources are unavailable', cache.revision)
        if cached is not None:
            return cached.page(context, cache.revision, cache.columns, retained=cache.retained(),
                               status='READY' if observation.caught_up else 'LOADING', **cursor_args)
        if not cache.trimmed and observation.caught_up:
            window = Window(query)
            window.ingest(sorted(cache.recent.values(), key=lambda item: item.sequence))
            window.complete = True
            cache.add(window)
            return window.page(context, cache.revision, cache.columns, retained=cache.retained(), **cursor_args)
        replay = observation.replay
        if replay is not None and (replay.window.query != query or replay.revision != cache.revision):
            self._close_observation(replay.observation)
            observation.replay = None
            replay = None
        replay = replay or self._new_replay(observation, query)
        result = self._observe(replay.observation, historical=True, generations=replay.generations)
        drain = getattr(replay.observation.adapter, 'drain_history', None)
        replay.window.ingest(drain() if drain else ())
        replay.window.complete = replay.observation.caught_up
        replay.window.messages = (() if replay.window.complete else
                                  ('Reconstructing native history in bounded chunks; observer-only historical timing is unavailable.',))
        replay.window.limitations = result.limitations
        page = replay.window.page(context, cache.revision, result.history_columns,
                                  retained=cache.retained()+len(replay.window.entries), **cursor_args)
        if replay.window.complete:
            cache.add(replay.window)
            self._close_observation(replay.observation)
            observation.replay = None
        return page

    def history(self, context: RunContext, *, cursor=None, limit=50, parent_id=None,
                stage=None, segment=None):
        """Advance live observation and one bounded on-demand history slice.

        Repeat the same query while LOADING. Completed opaque cursors can be used
        after reopening: native evidence is reconstructed, observer time is not.
        At most one live and one replay read budget per source runs per call.
        """
        with self._lock:
            observation, immediate = self._prepare(context)
            epoch = observation.history_epoch if observation else None
        if immediate is not None:
            return self._history_empty(context, immediate.read_health, immediate.phase)
        try:
            try:
                result = self._observe(observation)
                with self._lock:
                    if observation.retired or self._closed or observation.history_epoch != epoch:
                        return self._history_empty(context, 'RETIRED', 'History view was released; late observations ignored')
                    observation.snapshot = result
                page = self._history_page(observation, cursor=cursor, limit=limit,
                                          parent_id=parent_id, stage=stage, segment=segment)
            except CursorError as exc:
                page = self._history_empty(context, 'CURSOR_INVALID', str(exc), observation.history.revision)
            except Exception as exc:
                page = self._history_empty(context, 'READ_ERROR', f'History observation failed: {type(exc).__name__}: {exc}', observation.history.revision)
            with self._lock:
                if observation.retired or self._closed or observation.history_epoch != epoch:
                    return self._history_empty(context, 'RETIRED', 'History view was released; late observations ignored')
                return page
        finally:
            with self._lock:
                observation.busy = False
                self._inflight -= 1
                if observation.history_epoch != epoch and observation.replay is not None:
                    self._close_observation(observation.replay.observation)
                    observation.replay = None
                if observation.retired or self._closed:
                    if self._contexts.get(context.key) is observation:
                        self._contexts.pop(context.key)
                    self._close_observation(observation)

    history_page = history

    def poll(self, context: RunContext):
        """Perform one bounded read per source; concurrent calls never queue work."""
        with self._lock:
            observation, immediate = self._prepare(context)
            epoch = observation.history_epoch if observation else None
        if immediate is not None:
            return immediate
        try:
            try:
                result = self._observe(observation)
            except Exception as exc:
                result = self._unavailable(context, observation.snapshot, 'READ_ERROR',
                                           f'Observation failed: {type(exc).__name__}: {exc}')
            with self._lock:
                if observation.retired or self._closed:
                    return self._empty(context, 'RETIRED', 'Late observation ignored because its source binding was released')
                observation.snapshot = result
                return result
        finally:
            with self._lock:
                observation.busy = False
                self._inflight -= 1
                if observation.history_epoch != epoch and observation.replay is not None:
                    self._close_observation(observation.replay.observation)
                    observation.replay = None
                if observation.retired or self._closed:
                    if self._contexts.get(context.key) is observation:
                        self._contexts.pop(context.key)
                    self._close_observation(observation)
