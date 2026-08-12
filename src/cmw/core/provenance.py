"""Stable, inspectable provenance primitives with atomic JSON persistence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON-compatible data deterministically for identity hashing."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def stable_hash(value: object) -> str:
    """Return a SHA-256 identity for canonical JSON-compatible data."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_hash(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash one artifact without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArtifactRecord:
    """Integrity evidence for one runtime artifact."""

    role: str
    path: str
    size_bytes: int
    sha256: str

    @classmethod
    def from_path(cls, path: Path, *, role: str, relative_to: Path | None = None) -> "ArtifactRecord":
        resolved = path.resolve()
        displayed = resolved
        if relative_to is not None:
            try:
                displayed = resolved.relative_to(relative_to.resolve())
            except ValueError:
                pass
        return cls(role, str(displayed), resolved.stat().st_size, file_hash(resolved))


def atomic_write_json(path: Path, value: object) -> None:
    """Write versioned metadata without exposing a partial JSON document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def git_state(repository: Path | None) -> dict[str, object] | None:
    """Collect optional Git evidence; calculations need not live in a repository."""

    if repository is None:
        return None
    try:
        root = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ("git", "status", "--porcelain", "--untracked-files=no"),
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"available": False, "commit": None, "tracked_worktree_dirty": None}
    return {
        "available": True,
        "repository": root,
        "commit": commit,
        "tracked_worktree_dirty": dirty,
    }


def records_to_dict(records: Mapping[str, ArtifactRecord]) -> dict[str, dict[str, object]]:
    return {name: asdict(record) for name, record in sorted(records.items())}
