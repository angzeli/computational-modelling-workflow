"""Bounded, read-only source observations for VASP result evidence.

The digest covers the exact bytes consumed by this reader, including a rejected
line or a one-byte EOF probe. It never hashes a later, separately opened file.
A complete stable observation is not an atomic snapshot across multiple files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import BinaryIO, Iterable, Iterator


MAX_LINE_BYTES = 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
_STAT_KEYS = ("device", "inode", "size", "mtime_ns", "ctime_ns")


@dataclass(frozen=True)
class SourceLine:
    line_number: int
    byte_start: int
    byte_end: int
    text: str


class SourceSnapshotError(ValueError):
    """A source could not be fully observed under the bounded read contract."""

    def __init__(self, code: str, message: str, record: dict[str, object]):
        self.code = code
        self.record = record
        super().__init__(message)


def _stat(value: os.stat_result) -> dict[str, int]:
    return {"device": value.st_dev, "inode": value.st_ino, "size": value.st_size,
            "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}


def _regular_path(path: Path) -> os.stat_result:
    # Inspect the lexical path before resolving it: resolving first would hide
    # a symlink in the filename or an ancestor directory.
    for part in (*reversed(path.parents), path):
        current = part.lstat()
        if stat.S_ISLNK(current.st_mode):
            raise ValueError(f"symlink traversal is unsupported: {part}")
    current = path.lstat()
    if not stat.S_ISREG(current.st_mode):
        raise ValueError(f"source is not a regular file: {path}")
    return current


class SnapshotReader(Iterator[SourceLine]):
    """Stream UTF-8 lines with byte offsets and exact consumed-byte identity.

    Reading is bounded by the size at open (or a smaller explicit byte limit),
    plus at most one byte used to detect growth at the observation boundary.
    A line is limited to MAX_LINE_BYTES, preventing an unbounded readline.
    Exiting early preserves an explicitly incomplete coverage record.
    """

    def __init__(self, path: str | os.PathLike[str], role: str,
                 max_bytes: int | None = None):
        if max_bytes is not None and (isinstance(max_bytes, bool)
                                     or not isinstance(max_bytes, int) or max_bytes < 0):
            raise ValueError("max_bytes must be a nonnegative integer or None")
        candidate = Path(path).expanduser()
        self.path = candidate if candidate.is_absolute() else Path.cwd() / candidate
        self.max_bytes = max_bytes
        self._stream: BinaryIO | None = None
        self._digest = hashlib.sha256()
        self._limit = 0
        self._done = False
        self._entered = False
        self.record: dict[str, object] = {
            "role": role, "path": os.fspath(path), "absolute_path": str(self.path),
            "resolved_path": str(self.path),
            "stat_before": None, "stat_after": None, "path_stat_after": None,
            "bytes_read": 0, "lines_read": 0, "sha256": None,
            "eof_reached": False, "coverage": "incomplete", "stable": False,
            "changes": [], "status": "unread", "error": None,
            "max_bytes": max_bytes, "max_line_bytes": MAX_LINE_BYTES,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _error(self, code: str, message: str) -> SourceSnapshotError:
        self._done = True
        self.record["error"] = {"code": code, "message": message}
        self.record["status"] = "invalid"
        return SourceSnapshotError(code, message, self.record)

    def __enter__(self) -> SnapshotReader:
        if self._entered:
            raise RuntimeError("a SnapshotReader may only be entered once")
        self._entered = True
        try:
            before = _regular_path(self.path)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(self.path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise ValueError("source changed to a nonregular file before opening")
                if _stat(before) != _stat(opened):
                    raise ValueError("source changed while opening")
                self._stream = os.fdopen(descriptor, "rb")
            except BaseException:
                os.close(descriptor)
                raise
            self.record["resolved_path"] = str(self.path.resolve(strict=True))
            self.record["stat_before"] = _stat(opened)
            self.record["status"] = "reading"
            self._limit = min(opened.st_size, self.max_bytes) if self.max_bytes is not None else opened.st_size
            return self
        except (OSError, ValueError) as exc:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            code = ("source.missing" if isinstance(exc, FileNotFoundError) else
                    "source.unreadable" if isinstance(exc, OSError) else "source.unsafe_path")
            raise self._error(code, str(exc)) from exc

    def __iter__(self) -> SnapshotReader:
        return self

    def _consume(self, data: bytes) -> None:
        self._digest.update(data)
        self.record["bytes_read"] += len(data)
        self.record["sha256"] = self._digest.hexdigest()

    def __next__(self) -> SourceLine:
        if self._stream is None:
            raise RuntimeError("SnapshotReader must be used in a with statement")
        if self._done:
            raise StopIteration
        start = self.record["bytes_read"]
        remaining = self._limit - start
        try:
            if remaining == 0:
                # A nonempty probe is evidence of incomplete coverage, and is
                # included in the digest rather than silently discarded.
                probe = self._stream.read(1)
                self._consume(probe)
                self._done = True
                if not probe:
                    self.record["eof_reached"] = True
                    raise StopIteration
                if self.max_bytes is not None and self._limit < self.record["stat_before"]["size"]:
                    raise self._error("source.byte_limit", "source exceeds the requested byte limit")
                raise StopIteration
            data = self._stream.readline(min(MAX_LINE_BYTES + 1, remaining))
        except OSError as exc:
            raise self._error("source.unreadable", str(exc)) from exc
        if not data:
            self._done = True
            self.record["eof_reached"] = True
            raise StopIteration
        self._consume(data)
        if len(data) > MAX_LINE_BYTES:
            raise self._error("source.line_limit", "source line exceeds the supported byte limit")
        if not data.endswith(b"\n") and self.record["bytes_read"] == self._limit:
            if self._limit < self.record["stat_before"]["size"]:
                raise self._error("source.byte_limit", "byte limit ends within a source line")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise self._error("source.encoding", "source is not valid UTF-8") from exc
        self.record["lines_read"] += 1
        return SourceLine(self.record["lines_read"], start, self.record["bytes_read"], text)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._stream is None:
            return
        try:
            after = _stat(os.fstat(self._stream.fileno()))
            self.record["stat_after"] = after
            changes: list[str] = []
            before = self.record["stat_before"]
            if after["size"] > before["size"]:
                changes.append("growth")
            if after["size"] < before["size"]:
                changes.append("truncation")
            if any(after[key] != before[key] for key in ("mtime_ns", "ctime_ns")):
                changes.append("metadata_changed")
            try:
                path_after = _stat(_regular_path(self.path))
                self.record["path_stat_after"] = path_after
                if any(path_after[key] != after[key] for key in ("device", "inode")):
                    changes.append("replacement")
                elif path_after != after:
                    changes.append("path_changed_after_read")
            except FileNotFoundError:
                changes.append("removed")
            except (OSError, ValueError):
                changes.append("path_unavailable_or_unsafe")
            self.record["changes"] = changes
            self.record["stable"] = not changes
            self.record["sha256"] = self._digest.hexdigest()
            complete = (self.record["eof_reached"] and self.record["error"] is None
                        and self.record["bytes_read"] == before["size"])
            self.record["coverage"] = "complete" if complete else "incomplete"
            if self.record["error"] is None:
                self.record["status"] = "changing" if changes else "stable" if complete else "incomplete"
        finally:
            self._stream.close()
            self._stream = None


def read_json_source(path: str | os.PathLike[str], role: str,
                     max_bytes: int = MAX_JSON_BYTES) -> tuple[object, dict[str, object]]:
    """Read a bounded JSON source, rejecting ambiguous keys and nonfinite values."""
    reader = SnapshotReader(path, role, max_bytes=max_bytes)
    with reader:
        text = "".join(line.text for line in reader)
    if not reader.record["stable"] or reader.record["coverage"] != "complete":
        raise reader._error("source.incomplete", "JSON source changed or was incompletely read")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    def nonfinite(value):
        raise ValueError(f"nonfinite JSON number: {value}")

    try:
        result = json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)
        pending = [result]
        while pending:
            value = pending.pop()
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("nonfinite JSON number")
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
    except (ValueError, RecursionError) as exc:
        raise reader._error("source.invalid_json", str(exc)) from exc
    return result, reader.record


def revalidate_sources(source_records: Iterable[dict[str, object]]) -> dict[str, object]:
    """Reobserve consumed sources immediately before publication, without writes.

    Exact descriptor and path identities and the complete streamed digest must
    match. This cannot prevent writes after this check or make a cross-file read
    atomic; callers must retain that limitation in their finalization record.
    """
    findings = []
    observed = []
    for expected in source_records:
        role = expected.get("role")
        if not (expected.get("stable") is True and expected.get("coverage") == "complete"
                and expected.get("eof_reached") is True and expected.get("error") is None
                and isinstance(expected.get("stat_before"), dict)
                and isinstance(expected.get("sha256"), str)):
            findings.append({"code": "source.not_finalizable", "role": role,
                             "message": "source observation is not complete and stable"})
            continue
        locator = expected.get("absolute_path", expected.get("resolved_path"))
        if not isinstance(locator, str):
            findings.append({"code": "source.missing_identity", "role": role,
                             "message": "source locator is missing"})
            continue
        reader = SnapshotReader(locator, str(role))
        try:
            with reader:
                for _line in reader:
                    pass
        except SourceSnapshotError as exc:
            findings.append({"code": exc.code, "role": role, "message": str(exc)})
            observed.append(exc.record)
            continue
        actual = reader.record
        observed.append(actual)
        same = (actual["stable"] and actual["coverage"] == "complete"
                and actual["sha256"] == expected["sha256"]
                and actual["bytes_read"] == expected.get("bytes_read")
                and all(actual["stat_before"].get(key) == expected["stat_before"].get(key)
                        for key in _STAT_KEYS))
        if not same:
            findings.append({"code": "source.identity_changed", "role": role,
                             "message": "source identity or exact bytes changed since parsing"})
    return {"valid": not findings, "findings": findings, "sources": observed}
