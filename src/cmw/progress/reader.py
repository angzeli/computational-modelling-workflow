"""Bounded append readers for explicit regular files, independent of Jobs.

Prefix and last-consumed-byte sentinels detect common same-inode rewrites. They
cannot establish immutability of arbitrary unsampled interior bytes; a live
reader deliberately does not hash or rescan an entire scientific output.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import time

from .models import Evidence, SourceDescriptor, SourceLine, SourceState, SourceUpdate


READ_BUDGET = 64 * 1024
MAX_LINE_BYTES = 16 * 1024
SENTINEL_BYTES = 128
REWRITE_LIMIT = ('Bounded prefix/tail checks cannot detect every in-place interior rewrite; '
                 'live observations are not immutable source verification.')


class RegularFileReader:
    """Keep one descriptor and bounded partial-line state across client polls."""

    def __init__(self, source: SourceDescriptor, *, byte_budget=READ_BUDGET,
                 max_line_bytes=MAX_LINE_BYTES):
        if byte_budget < 1 or max_line_bytes < 1:
            raise ValueError('Reader byte and line budgets must be positive')
        self.source = source
        self.byte_budget = byte_budget
        self.max_line_bytes = max_line_bytes
        self._fd = None
        self._identity = None
        self._signature = None
        self._offset = 0
        self._line_number = 0
        self._partial = b''
        self._dropping_line = False
        self._prefix = b''
        self._tail = b''
        self._generation = 0
        self._last_success = None
        self._last_growth = None
        self._last_change = None
        self._last_size = None
        self._total_bytes = 0
        self._poll_bytes = 0
        self._closed = False
        self._warnings = ()

    def close(self):
        self._close_fd()
        self._closed = True

    def _close_fd(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _reset(self, now):
        self._generation += 1
        self._offset = self._line_number = 0
        self._partial = b''
        self._dropping_line = False
        self._prefix = self._tail = b''
        self._warnings = ()
        self._last_change = now
        self._last_size = None

    @staticmethod
    def _identity_of(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _signature_of(info):
        return info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _read_at(self, count, offset):
        data = os.pread(self._fd, count, offset)
        self._poll_bytes += len(data)
        self._total_bytes += len(data)
        return data

    def _sentinels_match(self):
        if self._prefix and self._read_at(len(self._prefix), 0) != self._prefix:
            return False
        return not self._tail or self._read_at(len(self._tail), self._offset-len(self._tail)) == self._tail

    def _update(self, health, *, lines=(), reset=False, reason=None, has_more=False,
                limitations=()):
        state = SourceState(
            role=self.source.role, path=self.source.path, generation=self._generation,
            health=health, identity=self._identity, offset=self._offset,
            pending_bytes=len(self._partial) + (self.max_line_bytes + 1 if self._dropping_line else 0),
            has_more=has_more, last_success_at=self._last_success,
            last_growth_at=self._last_growth, last_change_at=self._last_change,
            bytes_read=self._poll_bytes, total_bytes_read=self._total_bytes,
            limitations=tuple(dict.fromkeys((REWRITE_LIMIT, *self._warnings, *limitations))),
        )
        return SourceUpdate(self.source, state, tuple(lines), reset, reason)

    def _lines(self, data, start, now):
        lines = []
        limitations = []
        cursor = 0
        while cursor < len(data):
            newline = data.find(b'\n', cursor)
            stop = len(data) if newline < 0 else newline+1
            part = data[cursor:stop]
            line_start = start+cursor-len(self._partial)
            if not self._dropping_line and len(self._partial)+len(part) > self.max_line_bytes:
                self._partial = b''
                self._dropping_line = True
                limitations.append('Oversized source line omitted')
            if not self._dropping_line:
                self._partial += part
            if newline >= 0:
                self._line_number += 1
                if not self._dropping_line:
                    try:
                        text = self._partial.rstrip(b'\r\n').decode('utf-8', errors='strict')
                    except UnicodeDecodeError:
                        limitations.append('Malformed UTF-8 source line omitted')
                    else:
                        lines.append(SourceLine(text, Evidence(
                            self.source.role, self.source.path, self._generation,
                            self._line_number, self._line_number,
                            line_start, start+stop, file_identity=self._identity), now))
                self._partial = b''
                self._dropping_line = False
            cursor = stop
        return lines, tuple(dict.fromkeys(limitations))

    def poll(self, now=None):
        """Read at most one data budget, plus at most two bounded sentinels."""
        now = time.time() if now is None else now
        self._poll_bytes = 0
        if self._closed:
            return self._update('CLOSED', reason='Reader is closed')
        if not Path(self.source.path).is_absolute() or '\x00' in self.source.path:
            return self._update('SOURCE_UNRESOLVED', reason='Source must be an absolute filesystem path')
        reset = False
        reason = None
        try:
            if self.source.root_path and self.source.root_identity is not None:
                root = os.stat(self.source.root_path)
                if not stat.S_ISDIR(root.st_mode) or self._identity_of(root) != self.source.root_identity:
                    self._close_fd()
                    return self._update('BINDING_CHANGED', reason='Declared source directory identity changed; resolve the binding again')
            info = os.stat(self.source.path)
            if not stat.S_ISREG(info.st_mode):
                self._close_fd()
                return self._update('NOT_REGULAR', reason='Only regular source files are observed')
            identity = self._identity_of(info)
            if self._fd is None or identity != self._identity:
                self._close_fd()
                fd = os.open(self.source.path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or self._identity_of(opened) != identity:
                    os.close(fd)
                    return self._update('SOURCE_CHANGED', reason='Source changed while opening; retry the same bounded reader')
                self._fd = fd
                if identity != self._identity:
                    reason = 'initial' if self._identity is None else 'replacement'
                    self._reset(now)
                    reset = True
                    self._identity = identity
                    self._signature = None
                info = opened
            signature = self._signature_of(info)
            if info.st_size < self._offset:
                self._reset(now)
                reset, reason = True, 'truncation'
            elif self._signature is not None and signature != self._signature and not self._sentinels_match():
                self._reset(now)
                reset, reason = True, 'in-place rewrite'
            if signature != self._signature:
                self._last_change = now
            if info.st_size > (self._last_size or 0):
                self._last_growth = now
            self._last_size = info.st_size
            self._signature = signature
            start = self._offset
            data = self._read_at(min(self.byte_budget, max(0, info.st_size-start)), start) if info.st_size > start else b''
            after = os.fstat(self._fd)
            current = os.stat(self.source.path)
            if self.source.root_path and self.source.root_identity is not None:
                root_after = os.stat(self.source.root_path)
                if not stat.S_ISDIR(root_after.st_mode) or self._identity_of(root_after) != self.source.root_identity:
                    self._close_fd()
                    self._reset(now)
                    return self._update('BINDING_CHANGED', reset=True,
                                        reason='Declared source directory changed during a read; data discarded')
            if self._identity_of(current) != self._identity or after.st_size < start+len(data):
                self._close_fd()
                self._reset(now)
                return self._update('SOURCE_CHANGED', reset=True, reason='Source replaced or truncated during a read; data discarded')
            self._offset += len(data)
            if len(self._prefix) < SENTINEL_BYTES:
                self._prefix = (self._prefix+data)[:SENTINEL_BYTES]
            self._tail = (self._tail+data)[-SENTINEL_BYTES:]
            lines, limitations = self._lines(data, start, now)
            self._warnings = tuple(dict.fromkeys((*self._warnings, *limitations)))
            self._last_success = now
            health = 'MALFORMED' if limitations else 'OK'
            return self._update(health, lines=lines, reset=reset, reason=reason,
                                has_more=after.st_size > self._offset, limitations=limitations)
        except PermissionError as exc:
            return self._update('PERMISSION_DENIED', reset=reset, reason=str(exc))
        except FileNotFoundError as exc:
            self._close_fd()
            missing_volume = (self.source.path.startswith('/Volumes/') and
                              not Path('/Volumes', Path(self.source.path).parts[2]).exists())
            return self._update('VOLUME_UNAVAILABLE' if missing_volume else 'MISSING',
                                reset=reset, reason=str(exc))
        except OSError as exc:
            return self._update('READ_ERROR', reset=reset, reason=f'{type(exc).__name__}: {exc}')
