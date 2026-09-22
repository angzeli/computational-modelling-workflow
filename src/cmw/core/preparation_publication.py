"""Publish prepared inputs and a separate Scratch record without overwriting.

The two locations are not one atomic transaction. Scratch intent precedes input
publication; only a finalized matching record constitutes completed preparation.
No stale ownership is reclaimed and no published input is removed on failure.
"""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import os
from pathlib import Path
import shutil
import sys
from typing import Mapping, Sequence
from uuid import uuid4

from .provenance import atomic_write_json, canonical_json_bytes, read_json


class PublicationError(ValueError):
    """A rejected or incomplete preparation; never an execution result."""

    def __init__(self, message: str, *, code: str = "PUBLICATION_REJECTED",
                 record_path: str | None = None, incomplete: bool = False):
        super().__init__(message)
        self.code = code
        self.record_path = record_path
        self.incomplete = incomplete


def content_identity(content: bytes) -> dict[str, object]:
    return {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _direct(path: Path, *, exists: bool = True) -> Path:
    try:
        resolved = path.resolve(strict=exists)
    except (OSError, RuntimeError) as exc:
        raise PublicationError(f"Missing or unsafe path: {path}") from exc
    if path != resolved or path.is_symlink():
        raise PublicationError(f"Symlink traversal is not allowed: {path}")
    return path


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def _directory_identity(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def publication_plan(*, output: Path | str, scratch_root: Path | str,
                     record_directory: Path | str, sources: Sequence[Path | str] = (),
                     kind: str = "directory", scratch_mount: Path | str | None = None
                     ) -> dict[str, object]:
    """Resolve explicit destinations and all prerequisites without writing.

    Output/root/mount paths are relative to cwd; a relative record directory is
    relative to scratch_root. Destination parents must already exist.
    """
    if kind not in {"directory", "file"}:
        raise PublicationError("Publication kind must be directory or file")
    target = _direct(_absolute(output), exists=False)
    root = _direct(_absolute(scratch_root))
    if not root.is_dir():
        raise PublicationError("Scratch root must be an existing directory")
    mount = None if scratch_mount is None else _direct(_absolute(scratch_mount))
    # macOS volume paths must not degrade into ordinary directories on the boot
    # filesystem when a removable volume is absent.
    if mount is None and sys.platform == "darwin" and root.parts[1:2] == ("Volumes",):
        mount = Path(*root.parts[:3])
    if mount is not None and (not os.path.ismount(mount) or not root.is_relative_to(mount)):
        raise PublicationError("Configured Scratch mount is not mounted or does not contain the root")
    record_dir = Path(record_directory).expanduser()
    record_dir = _direct(_absolute(record_dir if record_dir.is_absolute() else root / record_dir), exists=False)
    if record_dir == root or not record_dir.is_relative_to(root):
        raise PublicationError("Record directory must be a strict child of the Scratch root")
    for path in (target.parent, record_dir.parent):
        _direct(path)
        if not path.is_dir():
            raise PublicationError(f"Destination parent must already exist: {path}")
        if not os.access(path, os.W_OK | os.X_OK):
            raise PublicationError(f"Destination parent is not writable: {path}")
    if _overlap(target, record_dir) or target == root or root.is_relative_to(target):
        raise PublicationError("Input and Scratch metadata destinations overlap")
    for path in (target, record_dir):
        if os.path.lexists(path):
            raise PublicationError(f"Refusing existing destination: {path}")
    protected = []
    for source in sources:
        path = _direct(_absolute(source))
        if _overlap(target, path) or _overlap(record_dir, path):
            raise PublicationError(f"Destination overlaps a protected source: {path}")
        protected.append(str(path))
    if kind == "directory" and sys.platform not in {"darwin", "linux"}:
        raise PublicationError("Exclusive directory publication is supported on macOS and Linux only")
    return {"input_path": str(target), "kind": kind, "scratch_root": str(root),
            "scratch_mount": None if mount is None else str(mount),
            "record_directory": str(record_dir), "record_path": str(record_dir / "preparation.json"),
            "protected_sources": protected,
            "input_parent_identity": _directory_identity(target.parent),
            "record_parent_identity": _directory_identity(record_dir.parent)}


def _rename_exclusive(source: Path, destination: Path) -> None:
    """Use the OS no-replace primitive, including for existing empty directories."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
    elif sys.platform == "linux":
        try:
            function = libc.renameat2
        except AttributeError as exc:
            raise PublicationError("renameat2 no-replace support is unavailable") from exc
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise PublicationError("No supported exclusive directory publication primitive")
    function.restype = ctypes.c_int
    if function(*args) != 0:
        code = ctypes.get_errno()
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(code, "Destination appeared during publication", str(destination))
        raise OSError(code, os.strerror(code), str(destination))


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_record(path: Path, record: dict[str, object]) -> None:
    atomic_write_json(path, record)
    _sync_directory(path.parent)


def _write_candidate(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _cleanup_owned_stage(stage: Path, identity: list[int] | None) -> None:
    """Remove only this preparation's staging directory, never its published input."""
    if identity is None or not os.path.lexists(stage):
        return
    if stage.is_symlink() or _directory_identity(stage) != identity:
        raise PublicationError("Staging ownership changed; automatic cleanup refused")
    shutil.rmtree(stage)
    _sync_directory(stage.parent)


def publish_preparation(files: Mapping[str, bytes], record: Mapping[str, object],
                        plan: Mapping[str, object]) -> dict[str, object]:
    """Publish one validated snapshot; on incomplete commit retain Scratch intent."""
    candidates = dict(files)
    if not candidates or any(Path(name).name != name or name in {".", ".."} or
                             not isinstance(data, bytes) for name, data in candidates.items()):
        raise PublicationError("Candidates must be safe basenames containing bytes")
    current = publication_plan(output=str(plan["input_path"]), scratch_root=str(plan["scratch_root"]),
                               record_directory=str(plan["record_directory"]),
                               sources=plan.get("protected_sources", ()), kind=str(plan["kind"]),
                               scratch_mount=plan.get("scratch_mount"))
    if current != dict(plan):
        raise PublicationError("Destination identities changed since resolution")
    target = Path(current["input_path"])
    record_dir = Path(current["record_directory"])
    record_path = Path(current["record_path"])
    if current["kind"] == "file" and set(candidates) != {target.name}:
        raise PublicationError("A file publication requires exactly its destination basename")
    identifier = uuid4().hex
    stage = target.parent / f".cmw-preparation-{identifier}"
    value = dict(record)
    value.update(schema_version=1, preparation_id=identifier,
                 prepared_inputs={name: content_identity(data) for name, data in sorted(candidates.items())},
                 publication={"state": "intent", "kind": current["kind"],
                              "input_path": str(target), "record_path": str(record_path),
                              "scratch_root": current["scratch_root"], "staging_path": str(stage),
                              "created_at": datetime.now(timezone.utc).isoformat()})
    canonical_json_bytes(value)  # Reject non-serializable/nonfinite records before any write.
    acquired = False
    published = False
    stage_identity = None
    cleanup_attempted = False
    cleanup_complete = False
    try:
        record_dir.mkdir(mode=0o700)
        acquired = True
        _sync_directory(record_dir.parent)
        _write_record(record_path, value)
        stage.mkdir(mode=0o700)
        stage_identity = _directory_identity(stage)
        for name, data in candidates.items():
            _write_candidate(stage / name, data)
        _sync_directory(stage)
        if _directory_identity(target.parent) != current["input_parent_identity"]:
            raise PublicationError("Input parent changed during staging")
        _direct(target.parent)
        if current["kind"] == "directory":
            _rename_exclusive(stage, target)
        else:
            os.link(stage / target.name, target)
        published = True
        _sync_directory(target.parent)
        cleanup_attempted = True
        _cleanup_owned_stage(stage, stage_identity)
        cleanup_complete = True
        value["publication"] = dict(value["publication"], state="complete",
                                    completed_at=datetime.now(timezone.utc).isoformat())
        _write_record(record_path, value)
        return value
    except (OSError, ValueError) as exc:
        cleanup_error = str(exc) if cleanup_attempted and not cleanup_complete else None
        if not cleanup_attempted:
            try:
                _cleanup_owned_stage(stage, stage_identity)
            except (OSError, ValueError) as cleanup_exc:
                cleanup_error = str(cleanup_exc)
        if acquired:
            failed = dict(value, publication=dict(
                value["publication"], state="incomplete" if published else "failed",
                error=str(exc), cleanup_error=cleanup_error,
            ))
            failed["publication"].pop("completed_at", None)
            try:
                _write_record(record_path, failed)
            except (OSError, ValueError):
                # A failed directory sync may follow a successful atomic record
                # replacement. Replacing with an explicit incomplete state before
                # syncing again keeps visible evidence conservative even when the
                # secondary sync also fails. If replacement itself is impossible,
                # the raised error remains authoritative; no durability is claimed.
                pass
        raise PublicationError(str(exc), code="PUBLICATION_INCOMPLETE" if published else "PUBLICATION_FAILED",
                               record_path=str(record_path) if acquired else None,
                               incomplete=published) from exc


def inspect_publication(record_path: Path | str, *, input_path: Path | str | None = None
                        ) -> dict[str, object]:
    """Read-only consistency inspection; this does not resume or repair anything."""
    findings: list[dict[str, object]] = []

    def finding(code: str, severity: str, message: str, evidence: object = None) -> None:
        findings.append({"code": code, "severity": severity, "scope": "publication",
                         "message": message, "evidence": evidence})

    try:
        path = _direct(_absolute(record_path))
        value = read_json(path)
        if value.get("schema_version") != 1 or not isinstance(value.get("publication"), dict):
            raise ValueError("Unsupported or malformed preparation record")
        state = value["publication"]
        if state.get("state") != "complete":
            finding("PUBLICATION_INCOMPLETE", "error", "Record does not establish completed preparation", state)
        destination = _direct(_absolute(input_path if input_path is not None else state["input_path"]), exists=False)
        if str(destination) != state["input_path"]:
            finding("PUBLICATION_RELOCATED", "warning", "Inspecting an explicit location different from the recorded locator")
        identities = value["prepared_inputs"]
        if not isinstance(identities, dict) or not identities:
            raise ValueError("Missing prepared-input identities")
        if any(Path(name).name != name or name in {".", ".."} for name in identities):
            raise ValueError("Unsafe prepared-input basename")
        if state.get("kind") == "directory":
            if not destination.is_dir():
                raise ValueError("Published input directory is absent")
            names = {p.name for p in destination.iterdir()}
            if names != set(identities):
                finding("PUBLICATION_NAMES_MISMATCH", "error", "Input names differ from preparation record", sorted(names))
            inputs = {name: destination / name for name in identities}
        elif state.get("kind") == "file" and set(identities) == {destination.name}:
            inputs = {destination.name: destination}
        else:
            raise ValueError("Unsupported publication kind or file identity")
        for name, source in inputs.items():
            if not source.is_file() or source.is_symlink():
                finding("PUBLICATION_INPUT_MISSING", "error", f"Missing/nonregular input: {name}")
            elif content_identity(source.read_bytes()) != identities[name]:
                finding("PUBLICATION_CONTENT_MISMATCH", "error", f"Prepared bytes changed: {name}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        finding("PUBLICATION_RECORD_INVALID", "error", str(exc))
    invalid = any(item["severity"] == "error" for item in findings)
    if not invalid:
        finding("PUBLICATION_CONSISTENT", "info", "Recorded preparation and input bytes are consistent")
    return {"status": "invalid" if invalid else "valid", "exit_code": 1 if invalid else 0,
            "findings": findings}
