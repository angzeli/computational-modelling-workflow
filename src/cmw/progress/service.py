"""Ephemeral bounded observation scheduling; callers run polls off the UI thread.

This service creates no worker threads or controller. A timed-out caller cannot
release its still-running filesystem operation: its context and global slot stay
occupied until that operation actually returns.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time

from .models import ProgressSnapshot, RunContext
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


class ObservationService:
    """Share one adapter/readers per immutable binding, with bounded concurrency."""

    def __init__(self, *, max_contexts=8, max_inflight=2,
                 reader_factory=RegularFileReader, registration_getter=get_registration,
                 clock=time.time):
        if max_contexts < 1 or max_inflight < 1:
            raise ValueError('Observation context and concurrency limits must be positive')
        self.max_contexts = max_contexts
        self.max_inflight = max_inflight
        self.reader_factory = reader_factory
        self.registration_getter = registration_getter
        self.clock = clock
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
            observation = _Observation(context, adapter, tuple(readers))
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

    def _observe(self, observation):
        now = self.clock()
        updates = tuple(reader.poll(now) for reader in observation.readers)
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
        catching_up = any(state.has_more for state in states)
        if catching_up:
            messages = (*messages, 'Catching up in bounded chunks; displayed evidence may be historical')
        return replace(result, sources=states, read_health=health,
                       completeness='CATCHING_UP' if catching_up else result.completeness,
                       last_success_at=self._latest(state.last_success_at for state in states),
                       last_source_change_at=self._latest(state.last_change_at for state in states),
                       last_file_growth_at=self._latest(state.last_growth_at for state in states),
                       messages=messages[-32:], limitations=limitations[-32:])

    def poll(self, context: RunContext):
        """Perform one bounded read per source; concurrent calls never queue work."""
        with self._lock:
            observation, immediate = self._prepare(context)
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
                if observation.retired or self._closed:
                    if self._contexts.get(context.key) is observation:
                        self._contexts.pop(context.key)
                    self._close_observation(observation)
