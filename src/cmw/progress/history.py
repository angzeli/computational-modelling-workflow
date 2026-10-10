"""Bounded ephemeral history windows and opaque source-bound page cursors.

An evicted window is reconstructed through the normal adapter and regular-file
reader. No per-event index or observation time is persisted.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import base64
import binascii
import hashlib
import json

from .models import HistoryEntry, HistoryPage

MAX_PAGE_SIZE = 200
MAX_RECENT_ENTRIES = 256
MAX_CACHED_PAGES = 4
MAX_BOUNDARIES = 64


def source_revision(context, states):
    values = [(state.role, state.path, state.generation, state.identity,
               state.health == 'BINDING_CHANGED') for state in states]
    data = json.dumps((context.binding_id, values), separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()[:24]


def version_guard(states):
    """Cross-session cursor guard using metadata already collected by readers."""
    values = [(state.role, state.path, state.identity, state.file_version) for state in states]
    data = json.dumps(values, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()[:24]


class CursorError(ValueError):
    """The caller must restart or correct history navigation."""


@dataclass(frozen=True)
class Query:
    offset: int = 0
    limit: int = 50
    parent_id: str | None = None
    stage: str | None = None
    segment: int | None = None

    def matches(self, entry):
        return ((self.parent_id == '*' or entry.parent_id == self.parent_id)
                and (self.stage is None or entry.stage == self.stage)
                and (self.segment is None or entry.segment == self.segment))


def cursor_for(context, revision, query, offset=None, *, session_id, guard):
    payload = {'binding': context.binding_id, 'revision': revision,
               'session': session_id, 'guard': guard,
               'offset': query.offset if offset is None else offset,
               'parent': query.parent_id, 'stage': query.stage, 'segment': query.segment}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')


def query_from_cursor(context, revision, cursor, *, limit, parent_id, stage, segment, session_id, guard):
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise CursorError(f'History page size must be between 1 and {MAX_PAGE_SIZE}')
    query = Query(limit=limit, parent_id=parent_id, stage=stage, segment=segment)
    if cursor is None:
        return query
    try:
        if len(cursor) > 8192:
            raise CursorError('Cursor is too large')
        payload = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        if not isinstance(payload, dict):
            raise CursorError('Invalid history cursor object')
        if any(payload.get(key) is not None and not isinstance(payload[key], str)
               for key in ('parent', 'stage')):
            raise CursorError('Invalid history filters')
        segment_filter = payload.get('segment')
        if segment_filter is not None and (type(segment_filter) is not int or segment_filter < 1):
            raise CursorError('Invalid history segment')
        if payload['binding'] != context.binding_id or payload['revision'] != revision:
            raise CursorError('Source binding or generation changed; restart history navigation')
        if not isinstance(payload.get('session'), str) or not isinstance(payload.get('guard'), str):
            raise CursorError('History cursor lacks a source-version guard; restart navigation')
        if payload['session'] != session_id and payload['guard'] != guard:
            raise CursorError('Source version changed since the earlier observation session; restart history navigation')
        offset = payload['offset']
        if type(offset) is not int or offset < 0:
            raise CursorError('Invalid history offset')
        for key, supplied in (('parent', parent_id), ('stage', stage), ('segment', segment)):
            if supplied is not None and supplied != payload[key]:
                raise CursorError('History cursor belongs to different filters')
        return Query(offset, limit, payload['parent'], payload['stage'], payload['segment'])
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise CursorError('Invalid history cursor') from exc


@dataclass
class Window:
    query: Query
    entries: OrderedDict = field(default_factory=OrderedDict)
    total: int = 0
    sequence: int = 0
    boundaries: list = field(default_factory=list)
    boundaries_truncated: bool = False
    complete: bool = False
    messages: tuple = ()
    limitations: tuple = ()

    def ingest(self, entries):
        for entry in entries:
            boundary = (entry.stage, entry.segment)
            if boundary not in self.boundaries:
                self.boundaries.append(boundary)
                if len(self.boundaries) > MAX_BOUNDARIES:
                    self.boundaries.pop(0)
                    self.boundaries_truncated = True
            new = entry.sequence > self.sequence
            self.sequence = max(self.sequence, entry.sequence)
            if entry.entry_id in self.entries:
                self.entries[entry.entry_id] = entry
            elif new and self.query.matches(entry):
                ordinal = self.total
                self.total += 1
                if self.query.offset <= ordinal < self.query.offset + self.query.limit:
                    self.entries[entry.entry_id] = entry

    def page(self, context, revision, columns, *, retained, session_id, guard, status=None, limitations=()):
        status = status or ('READY' if self.complete else 'LOADING')
        entries = tuple(self.entries.values())
        complete = self.complete and status == 'READY'
        more = self.total > self.query.offset + len(entries) or not complete
        next_cursor = (cursor_for(context, revision, self.query, self.query.offset + len(entries), session_id=session_id, guard=guard)
                       if entries and self.total > self.query.offset + len(entries) else None)
        previous = (cursor_for(context, revision, self.query, max(0, self.query.offset-self.query.limit), session_id=session_id, guard=guard)
                    if self.query.offset else None)
        limits = (*self.limitations, *limitations)
        if self.boundaries_truncated:
            limits += ('Stage/segment selector retains the latest 64 boundaries; explicit filters still accept older segments.',)
        return HistoryPage(context, entries, columns, status,
                           'COMPLETE' if complete else 'PARTIAL',
                           cursor_for(context, revision, self.query, session_id=session_id, guard=guard), next_cursor, previous,
                           more, self.total if complete else None, retained, revision,
                           tuple(self.boundaries), self.messages,
                           tuple(dict.fromkeys(limits)))


class HistoryCache:
    def __init__(self, context):
        self.context = context
        self.revision = ''
        self.recent = OrderedDict()
        self.sequence = 0
        self.trimmed = False
        self.pages = OrderedDict()
        self.boundaries = []
        self.columns = ()

    def reset(self, revision):
        self.revision = revision
        self.recent.clear()
        self.sequence = 0
        self.trimmed = False
        self.pages.clear()
        self.boundaries.clear()

    def ingest(self, entries):
        normalized = []
        for entry in entries:
            if not isinstance(entry, HistoryEntry) or not entry.entry_id or entry.sequence < 1:
                raise ValueError('Adapter returned an invalid history entry')
            if entry.binding_id and entry.binding_id != self.context.binding_id:
                raise ValueError('Adapter history belongs to another source binding')
            if entry.attempt_id is not None and entry.attempt_id != self.context.attempt_id:
                raise ValueError('Adapter history belongs to another attempt')
            entry = replace(entry, binding_id=self.context.binding_id, attempt_id=self.context.attempt_id)
            normalized.append(entry)
            if entry.entry_id in self.recent or entry.sequence > self.sequence:
                self.recent[entry.entry_id] = entry
            self.sequence = max(self.sequence, entry.sequence)
            while len(self.recent) > MAX_RECENT_ENTRIES:
                self.recent.popitem(last=False)
                self.trimmed = True
            boundary = (entry.stage, entry.segment)
            if boundary not in self.boundaries:
                self.boundaries.append(boundary)
                del self.boundaries[:-MAX_BOUNDARIES]
        for window in self.pages.values():
            window.ingest(normalized)

    def cached(self, query):
        window = self.pages.get(query)
        if window is not None:
            self.pages.move_to_end(query)
        return window

    def add(self, window):
        self.pages[window.query] = window
        self.pages.move_to_end(window.query)
        while len(self.pages) > MAX_CACHED_PAGES:
            self.pages.popitem(last=False)

    def retained(self):
        return len(self.recent) + sum(len(window.entries) for window in self.pages.values())
