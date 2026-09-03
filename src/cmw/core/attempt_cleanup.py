"""Crash-safe cleanup of provably superseded execution attempts.

Cleanup is operational state.  It never changes a scientific target, artifact
identity, or attempt identity.  Legacy campaigns remain readable without a
cleanup registry; the overlay is created only by an applied cleanup.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from .execution_layout import ATTEMPT_PATTERN, ExecutionLayout, resolve_recorded_layout
from .locks import LockState, acquire_lock, inspect_lock, release_lock
from .provenance import atomic_write_json, canonical_json_bytes, file_hash, read_json, stable_hash


CLEANUP_SCHEMA_VERSION = 1
CLEANUP_TOOL_VERSION = "cmw-attempt-cleanup/1"
DEFAULT_TOMBSTONE_MAX_BYTES = 256 * 1024
DEFAULT_DIAGNOSTIC_TAIL_BYTES = 16 * 1024
MAX_IN_MEMORY_REFERENCE_JSON_BYTES = 64 * 1024 * 1024


class CleanupMode(str, Enum):
    MINIMAL_PROVENANCE = "minimal-provenance"
    PURGE = "purge"


class TransactionState(str, Enum):
    PLANNED = "PLANNED"
    STAGING = "STAGING"
    STAGED = "STAGED"
    METADATA_UPDATING = "METADATA_UPDATING"
    COMMITTED = "COMMITTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


class CleanupExitCode(IntEnum):
    SUCCESS = 0
    INVALID_ARGUMENT = 2
    NO_ELIGIBLE_ATTEMPTS = 10
    BLOCKED = 11
    STALE_PLAN = 12
    ACTIVE_CAMPAIGN = 13
    INVALID_PATH = 14
    TRANSACTION_FAILED = 15
    ROLLBACK_REQUIRED = 16


class CleanupError(RuntimeError):
    """Stable cleanup failure carrying a machine-readable code and exit code."""

    def __init__(self, code: str, message: str, exit_code: CleanupExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class InvalidCleanupPath(CleanupError):
    def __init__(self, message: str) -> None:
        super().__init__("INVALID_PATH", message, CleanupExitCode.INVALID_PATH)


class StaleCleanupPlan(CleanupError):
    def __init__(self, message: str = "campaign state changed after cleanup planning") -> None:
        super().__init__("STALE_PLAN", message, CleanupExitCode.STALE_PLAN)


@dataclass(frozen=True)
class ReferenceEvidence:
    source: str
    field: str
    value: str
    requires_payload: bool
    authoritative: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FileInventory:
    file_count: int
    total_bytes: int
    estimated_reclaimed_bytes: int
    removed_paths: tuple[str, ...]
    categories: Mapping[str, int]
    shared_hardlink_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "removed_paths": list(self.removed_paths),
            "categories": dict(sorted(self.categories.items())),
        }


@dataclass(frozen=True)
class AttemptDecision:
    campaign_id: str
    target_id: str
    attempt_id: str
    attempt_directory: str
    terminal: bool
    reusable: bool
    canonical_attempt_id: str | None
    eligible: bool
    already_cleaned: str | None
    reasons: tuple[str, ...]
    evidence: tuple[ReferenceEvidence, ...]
    inventory: FileInventory | None
    retained_files: tuple[str, ...]
    tombstone: Mapping[str, object] | None = None

    @property
    def key(self) -> str:
        return f"{self.target_id}/{self.attempt_id}"

    def to_dict(self, *, include_tombstone: bool = False) -> dict[str, object]:
        value: dict[str, object] = {
            "campaign_id": self.campaign_id,
            "target_id": self.target_id,
            "attempt_id": self.attempt_id,
            "attempt_directory": self.attempt_directory,
            "terminal": self.terminal,
            "reusable": self.reusable,
            "canonical_attempt_id": self.canonical_attempt_id,
            "eligible": self.eligible,
            "already_cleaned": self.already_cleaned,
            "reasons": list(self.reasons),
            "reference_graph_evidence": [item.to_dict() for item in self.evidence],
            "inventory": self.inventory.to_dict() if self.inventory else None,
            "retained_files": list(self.retained_files),
        }
        if include_tombstone:
            value["tombstone"] = dict(self.tombstone) if self.tombstone else None
        return value


@dataclass(frozen=True)
class CleanupPlan:
    campaign_id: str
    campaign_root: str
    campaign_state_sha256: str
    mode: CleanupMode
    candidates: tuple[AttemptDecision, ...]
    ineligible: tuple[AttemptDecision, ...]
    already_cleaned: tuple[AttemptDecision, ...]
    global_blockers: tuple[str, ...]
    process_lock_evidence: tuple[Mapping[str, object], ...]
    tool_version: str = CLEANUP_TOOL_VERSION
    schema_version: int = CLEANUP_SCHEMA_VERSION
    plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", stable_hash(self.identity_payload()))

    @property
    def total_bytes(self) -> int:
        return sum(item.inventory.total_bytes for item in self.candidates if item.inventory)

    @property
    def estimated_reclaimed_bytes(self) -> int:
        return sum(
            item.inventory.estimated_reclaimed_bytes
            for item in self.candidates
            if item.inventory
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "campaign_id": self.campaign_id,
            "campaign_root": self.campaign_root,
            "campaign_state_sha256": self.campaign_state_sha256,
            "mode": self.mode.value,
            "candidates": [item.to_dict(include_tombstone=True) for item in self.candidates],
            "ineligible": [item.to_dict() for item in self.ineligible],
            "already_cleaned": [item.to_dict() for item in self.already_cleaned],
            "global_blockers": list(self.global_blockers),
            "process_lock_evidence": [dict(item) for item in self.process_lock_evidence],
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.identity_payload(),
            "candidate_count": len(self.candidates),
            "ineligible_count": len(self.ineligible),
            "already_cleaned_count": len(self.already_cleaned),
            "current_bytes": self.total_bytes,
            "estimated_reclaimed_bytes": self.estimated_reclaimed_bytes,
            "plan_sha256": self.plan_sha256,
        }


@dataclass(frozen=True)
class CleanupResult:
    status: str
    transaction_id: str | None
    plan_sha256: str
    mode: CleanupMode
    attempts_cleaned: int
    bytes_removed: int
    post_cleanup_campaign_state_sha256: str | None
    recovery_actions: tuple[Mapping[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": CLEANUP_SCHEMA_VERSION,
            "status": self.status,
            "transaction_id": self.transaction_id,
            "plan_sha256": self.plan_sha256,
            "mode": self.mode.value,
            "attempts_cleaned": self.attempts_cleaned,
            "bytes_removed": self.bytes_removed,
            "post_cleanup_campaign_state_sha256": self.post_cleanup_campaign_state_sha256,
            "recovery_actions": [dict(item) for item in self.recovery_actions],
        }


@dataclass
class _Attempt:
    campaign_id: str
    target_id: str
    target_directory: Path
    attempt_id: str
    directory: Path
    target_record: Mapping[str, object]
    metadata_path: Path | None
    metadata: Mapping[str, object]
    terminal: bool
    reusable: bool
    recoverable: bool
    explicit_superseding_attempt_id: str | None
    recorded_pid: int | None
    pid_state: str
    already_cleaned: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_if_mapping(path: Path) -> dict[str, Any] | None:
    try:
        value = read_json(path)
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return value


def _campaign_manifest_paths(root: Path) -> tuple[Path, ...]:
    names = (
        root / ".cmw" / "campaign.json",
        root / "campaign.json",
        root / "campaign-state.json",
        root / "state.json",
    )
    return tuple(path for path in names if path.is_file())


def resolve_campaign(value: Path | str, *, search_roots: Sequence[Path] = ()) -> Path:
    """Resolve an existing campaign path or unique campaign id, failing closed."""

    raw = Path(str(value)).expanduser()
    candidates: list[Path] = []
    if raw.exists() or raw.is_symlink():
        if raw.is_symlink():
            raise InvalidCleanupPath("campaign root must not be a symlink")
        selected = raw.resolve(strict=True)
        if selected.is_file():
            record = _json_if_mapping(selected)
            declared = record.get("campaign_root") if record else None
            selected = (
                Path(str(declared)).expanduser().resolve(strict=True)
                if declared
                else selected.parent.resolve(strict=True)
            )
        candidates.append(selected)
    else:
        configured = list(search_roots)
        configured.extend(
            Path(item).expanduser()
            for item in os.environ.get("CMW_CAMPAIGN_ROOTS", "").split(os.pathsep)
            if item
        )
        for search_root in configured:
            if not search_root.is_dir():
                continue
            roots = (search_root, *sorted(path for path in search_root.iterdir() if path.is_dir()))
            for root in roots:
                for manifest in _campaign_manifest_paths(root):
                    record = _json_if_mapping(manifest)
                    if record and str(record.get("campaign_id", "")) == str(value):
                        candidates.append(root.resolve())
                        break
    unique = sorted(set(candidates))
    if len(unique) != 1:
        detail = "not found" if not unique else "ambiguous"
        raise InvalidCleanupPath(f"campaign {value!s} is {detail}")
    root = unique[0]
    home = Path.home().resolve()
    if root in (Path(root.anchor), home) or not root.is_dir():
        raise InvalidCleanupPath(f"unsafe campaign root: {root}")
    if not (root / "calculation").is_dir() and not _campaign_manifest_paths(root):
        raise InvalidCleanupPath("campaign root has no calculation tree or campaign manifest")
    return root


def _campaign_descriptor(root: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in _campaign_manifest_paths(root):
        value = _json_if_mapping(path)
        if value:
            merged.update(value)
    return merged


def _campaign_id(root: Path, descriptor: Mapping[str, object]) -> str:
    declared = descriptor.get("campaign_id")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return "legacy-" + stable_hash({"campaign_root": str(root)})[:20]


def _metadata_path(directory: Path) -> Path | None:
    names = (
        "job.json",
        "result.json",
        "analysis-result.json",
        "conversion-result.json",
        "materialization-failure.json",
        "materialization.json",
        "execution-attempt.json",
    )
    return next((directory / name for name in names if (directory / name).is_file()), None)


def _nested(record: Mapping[str, object], *path: str) -> object | None:
    current: object = record
    for item in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(item)
    return current


def _attempt_id(metadata: Mapping[str, object], directory: Path) -> str:
    values = (
        _nested(metadata, "attempt", "attempt_id"),
        _nested(metadata, "execution_attempt", "attempt_id"),
        metadata.get("attempt_id"),
        _nested(metadata, "execution_layout", "attempt_identifier"),
    )
    for value in values:
        if isinstance(value, str) and value:
            return value
    return directory.name


def _target_id(metadata: Mapping[str, object], target: Mapping[str, object], directory: Path) -> str:
    values = (
        _nested(metadata, "target", "target_id"),
        _nested(metadata, "attempt", "target_id"),
        metadata.get("target_id"),
        target.get("full_target_id"),
        target.get("target_id"),
    )
    for value in values:
        if isinstance(value, str) and value:
            return value
    raise InvalidCleanupPath(
        f"attempt has no authoritative full target identity: {directory}"
    )


_TERMINAL = {
    "SUCCESS",
    "FAILED",
    "FATAL",
    "INVALID",
    "INTERRUPTED",
    "CANCELLED",
    "CANCELED",
    "TIMEOUT",
    "COMPLETED",
    "FINALIZED",
}
_NONTERMINAL = {"PREPARED", "PENDING", "RUNNING", "STARTING", "SUBMITTED"}


def _status_values(record: Mapping[str, object]) -> tuple[str, ...]:
    values = (
        record.get("status"),
        _nested(record, "execution", "status"),
        _nested(record, "scientific", "status"),
        _nested(record, "attempt", "status"),
    )
    return tuple(str(value).upper() for value in values if isinstance(value, str))


def _terminal(record: Mapping[str, object]) -> bool:
    values = _status_values(record)
    if any(value in _NONTERMINAL for value in values):
        return False
    return bool(record.get("reusable") is True or any(value in _TERMINAL for value in values))


def _artifact_path(attempt: Path, value: object) -> Path | None:
    if not isinstance(value, Mapping) or not isinstance(value.get("path"), str):
        return None
    selected = Path(str(value["path"]))
    return selected if selected.is_absolute() else attempt / selected


def _reusable(record: Mapping[str, object], target_id: str, attempt: Path) -> bool:
    if record.get("reusable") is not True:
        return False
    stored_target = (
        _nested(record, "target", "target_id")
        or _nested(record, "attempt", "target_id")
        or record.get("target_id")
    )
    if stored_target != target_id:
        return False
    execution = _nested(record, "execution", "status")
    scientific = _nested(record, "scientific", "status")
    if execution is not None and str(execution).upper() not in {"SUCCESS", "COMPLETED"}:
        return False
    if scientific is not None and str(scientific).upper() not in {"VALID", "PASSED"}:
        return False
    artifacts = record.get("artifacts")
    required = record.get("required_artifact_roles", ())
    if not isinstance(artifacts, Mapping):
        return False
    required_roles = tuple(str(item) for item in required) if isinstance(required, Sequence) and not isinstance(required, str) else ()
    for role in required_roles:
        evidence = artifacts.get(role)
        path = _artifact_path(attempt, evidence)
        if path is None or not path.is_file() or not isinstance(evidence, Mapping):
            return False
        try:
            if path.stat().st_size != int(evidence.get("size_bytes", -1)):
                return False
            if file_hash(path) != str(evidence.get("sha256", "")):
                return False
        except (OSError, TypeError, ValueError):
            return False
    return True


def _recorded_pid(record: Mapping[str, object]) -> int | None:
    values = (
        record.get("pid"),
        _nested(record, "process", "pid"),
        _nested(record, "execution", "pid"),
        _nested(record, "runtime", "pid"),
    )
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _pid_state(pid: int | None) -> str:
    if pid is None:
        return "ABSENT"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "STALE"
    except (PermissionError, OSError):
        return "UNCERTAIN"
    return "ACTIVE"


def _registry_path(root: Path) -> Path:
    return root / ".cmw" / "cleanup" / "registry.json"


def load_cleanup_registry(root: Path) -> dict[str, Any]:
    path = _registry_path(root)
    if not path.exists():
        return {"schema_version": CLEANUP_SCHEMA_VERSION, "attempts": {}, "transactions": []}
    value = read_json(path)
    if value.get("schema_version") != CLEANUP_SCHEMA_VERSION:
        raise CleanupError(
            "INVALID_CLEANUP_REGISTRY",
            "unsupported cleanup registry schema",
            CleanupExitCode.BLOCKED,
        )
    if not isinstance(value.get("attempts"), Mapping):
        raise CleanupError("INVALID_CLEANUP_REGISTRY", "cleanup registry attempts are invalid", CleanupExitCode.BLOCKED)
    return value


def _discover_attempts(root: Path, campaign_id: str, registry: Mapping[str, object]) -> list[_Attempt]:
    attempts: list[_Attempt] = []
    registered_attempts = registry.get("attempts", {})
    registered_attempts = registered_attempts if isinstance(registered_attempts, Mapping) else {}
    calculation = root / "calculation"
    if calculation.is_dir():
        for directory in sorted(root.rglob("attempt_*")):
            if not directory.is_dir() or ATTEMPT_PATTERN.fullmatch(directory.name) is None:
                continue
            if directory.parent.name != "attempts" or ".cmw" in directory.relative_to(root).parts:
                continue
            try:
                resolved = directory.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise InvalidCleanupPath(f"attempt path is outside campaign: {directory}") from exc
            if resolved != directory.absolute():
                raise InvalidCleanupPath(f"attempt directory traverses a symlink: {directory}")
            target_directory = directory.parent.parent
            target_record = _json_if_mapping(target_directory / "target.json") or {}
            target_manifest = (
                _json_if_mapping(target_directory / "target-manifest.json") or {}
            )
            identity_record = {**target_record, **target_manifest}
            metadata_path = _metadata_path(directory)
            metadata = _json_if_mapping(metadata_path) if metadata_path else {}
            metadata = metadata or {}
            attempt_id = _attempt_id(metadata, directory)
            if attempt_id != directory.name:
                raise InvalidCleanupPath(f"attempt identity conflicts with directory: {directory}")
            target_id = _target_id(metadata, identity_record, directory)
            registered = registered_attempts.get(f"{target_id}/{attempt_id}")
            cleaned_status = (
                str(registered.get("status"))
                if isinstance(registered, Mapping)
                and registered.get("status") in {"COMPACTED", "PURGED"}
                else None
            )
            layout_path = directory / "execution-layout.json"
            if layout_path.is_file():
                try:
                    stored_layout = read_json(layout_path)
                    layout = resolve_recorded_layout(
                        stored_layout, metadata_path=layout_path
                    )
                    layout.validate_attempt_identity(attempt_id)
                    if layout.working_directory != directory:
                        raise ValueError("layout path differs")
                    if layout.target_identifier != target_id:
                        raise ValueError("layout target differs")
                except (OSError, KeyError, TypeError, ValueError) as exc:
                    raise InvalidCleanupPath(f"invalid execution layout for {directory}: {exc}") from exc
            statuses = _status_values(metadata)
            recoverable = (
                any(value in {"SUCCESS", "COMPLETED"} for value in statuses)
                and not _reusable(metadata, target_id, directory)
            ) or (directory / "finalization-failure.json").is_file()
            superseding = metadata.get("superseded_by_attempt_id") or metadata.get("superseding_attempt_id")
            attempts.append(
                _Attempt(
                    campaign_id,
                    target_id,
                    target_directory,
                    attempt_id,
                    directory,
                    identity_record,
                    metadata_path,
                    metadata,
                    _terminal(metadata),
                    _reusable(metadata, target_id, directory),
                    recoverable,
                    str(superseding) if isinstance(superseding, str) else None,
                    _recorded_pid(metadata),
                    _pid_state(_recorded_pid(metadata)),
                    cleaned_status,
                )
            )
    registered = registry.get("attempts", {})
    if isinstance(registered, Mapping):
        existing = {item.target_id + "/" + item.attempt_id for item in attempts}
        for key, raw in sorted(registered.items()):
            if key in existing or not isinstance(raw, Mapping):
                continue
            target_id, separator, attempt_id = str(key).partition("/")
            status = str(raw.get("status", ""))
            if not separator or status not in {"COMPACTED", "PURGED"}:
                continue
            retained = raw.get("retained_path")
            directory = Path(str(retained)) if retained else root / ".cmw" / "purged" / target_id / attempt_id
            attempts.append(
                _Attempt(
                    campaign_id,
                    target_id,
                    directory.parent.parent,
                    attempt_id,
                    directory,
                    {},
                    None,
                    {},
                    True,
                    False,
                    False,
                    str(raw.get("superseding_attempt_id")) if raw.get("superseding_attempt_id") else None,
                    None,
                    "ABSENT",
                    status,
                )
            )
    return attempts


def _authoritative_json_files(root: Path) -> tuple[Path, ...]:
    selected: list[Path] = []
    for path in root.rglob("*.json"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if ".cmw" in parts and "transactions" in parts:
            continue
        if path.is_file() and not path.is_symlink():
            selected.append(path)
    return tuple(sorted(selected))


_ATTEMPT_ID_FIELDS = {
    "attempt_id",
    "attempt_identifier",
    "canonical_attempt_id",
    "current_attempt_id",
    "source_attempt_id",
    "parent_attempt_id",
    "superseding_attempt_id",
    "superseded_by_attempt_id",
}
_ATTEMPT_PATH_FIELDS = {
    "attempt_directory",
    "working_directory",
    "output_directory",
    "provenance_directory",
    "logs_directory",
    "result_path",
    "metadata_path",
    "command_path",
}
_ARTIFACT_ID_FIELDS = {
    "artifact_id",
    "scientific_artifact_id",
    "source_artifact_id",
    "source_structure_artifact_id",
    "geometry_artifact_id",
    "parent_artifacts",
}


def _walk_references(value: object, *, source: str, field: str = "") -> Iterable[ReferenceEvidence]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if name in _ATTEMPT_ID_FIELDS and isinstance(item, str):
                yield ReferenceEvidence(source, name, item, False)
            elif name in _ARTIFACT_ID_FIELDS and isinstance(item, str):
                yield ReferenceEvidence(source, name, item, True)
            elif name == "parent_artifacts" and isinstance(item, list):
                for parent in item:
                    if isinstance(parent, str):
                        yield ReferenceEvidence(source, name, parent, True)
            elif name in _ATTEMPT_PATH_FIELDS and isinstance(item, str):
                yield ReferenceEvidence(source, name, item, True)
            elif name in {"artifacts", "files", "outputs"} and isinstance(item, Mapping):
                for role, evidence in item.items():
                    if isinstance(evidence, Mapping) and isinstance(evidence.get("path"), str):
                        yield ReferenceEvidence(source, f"{name}.{role}.path", str(evidence["path"]), True)
                    elif isinstance(evidence, str):
                        yield ReferenceEvidence(source, f"{name}.{role}", evidence, True)
            yield from _walk_references(item, source=source, field=name)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_references(item, source=source, field=field)


def _reference_graph(
    root: Path,
) -> tuple[tuple[ReferenceEvidence, ...], tuple[str, ...]]:
    evidence: list[ReferenceEvidence] = []
    oversized: list[str] = []
    for path in _authoritative_json_files(root):
        if path.stat().st_size > MAX_IN_MEMORY_REFERENCE_JSON_BYTES:
            oversized.append(str(path.relative_to(root)))
            continue
        record = _json_if_mapping(path)
        if record is not None:
            evidence.extend(_walk_references(record, source=str(path.relative_to(root))))
    return tuple(evidence), tuple(oversized)


def _references_for(
    attempt: _Attempt,
    graph: Sequence[ReferenceEvidence],
    campaign_root: Path,
) -> tuple[ReferenceEvidence, ...]:
    result: list[ReferenceEvidence] = []
    artifact_id = _nested(attempt.metadata, "scientific_artifact", "artifact_id")
    for evidence in graph:
        source_path = Path(evidence.source)
        if not source_path.is_absolute():
            source_path = campaign_root / source_path
        try:
            source_path.relative_to(attempt.directory)
        except ValueError:
            pass
        else:
            continue
        value = evidence.value
        if evidence.field in _ATTEMPT_ID_FIELDS and value == attempt.attempt_id:
            result.append(evidence)
            continue
        if evidence.field in _ARTIFACT_ID_FIELDS and artifact_id and value == artifact_id:
            result.append(evidence)
            continue
        if evidence.requires_payload:
            try:
                selected = Path(value)
                if not selected.is_absolute():
                    selected = campaign_root / Path(evidence.source).parent / selected
                selected.absolute().relative_to(attempt.directory)
            except ValueError:
                pass
            except OSError:
                result.append(evidence)
            else:
                result.append(evidence)
    return tuple(result)


def _explicit_canonical(descriptor: Mapping[str, object], attempt: _Attempt) -> str | None:
    targets = descriptor.get("targets")
    if isinstance(targets, Mapping):
        target = targets.get(attempt.target_id)
        if isinstance(target, Mapping):
            for field in ("canonical_attempt_id", "current_attempt_id", "finalized_attempt_id", "reusable_attempt_id"):
                value = target.get(field)
                if isinstance(value, str) and value:
                    return value
    for record in (attempt.target_record,):
        for field in ("canonical_attempt_id", "current_attempt_id", "finalized_attempt_id", "reusable_attempt_id"):
            value = record.get(field)
            if isinstance(value, str) and value:
                return value
    return None


def _canonical_by_target(
    attempts: Sequence[_Attempt], descriptor: Mapping[str, object]
) -> tuple[dict[str, str], dict[str, str]]:
    canonical: dict[str, str] = {}
    ambiguous: dict[str, str] = {}
    by_target: dict[str, list[_Attempt]] = {}
    for attempt in attempts:
        if attempt.already_cleaned is None:
            by_target.setdefault(attempt.target_id, []).append(attempt)
    for target_id, values in by_target.items():
        explicit = {value for item in values if (value := _explicit_canonical(descriptor, item))}
        if len(explicit) > 1:
            ambiguous[target_id] = "conflicting explicit canonical attempt identities"
            continue
        if explicit:
            selected = next(iter(explicit))
            match = next((item for item in values if item.attempt_id == selected), None)
            if match is None or not match.reusable:
                ambiguous[target_id] = "explicit canonical attempt is absent or not reusable"
                continue
            canonical[target_id] = selected
            continue
        reusable = [item for item in values if item.reusable]
        if len(reusable) == 1:
            canonical[target_id] = reusable[0].attempt_id
        elif len(reusable) > 1:
            ambiguous[target_id] = "multiple reusable attempts and no explicit canonical selection"
        else:
            ambiguous[target_id] = "no finalized reusable attempt"
    return canonical, ambiguous


def enumerate_campaign_attempts(
    campaign: Path | str, *, search_roots: Sequence[Path] = ()
) -> tuple[Mapping[str, object], ...]:
    """Enumerate present and cleaned attempts through one campaign overlay."""

    root = resolve_campaign(campaign, search_roots=search_roots)
    descriptor = _campaign_descriptor(root)
    campaign_id = _campaign_id(root, descriptor)
    attempts = _discover_attempts(root, campaign_id, load_cleanup_registry(root))
    return tuple(
        {
            "campaign_id": item.campaign_id,
            "target_id": item.target_id,
            "attempt_id": item.attempt_id,
            "attempt_directory": str(item.directory),
            "terminal": item.terminal,
            "reusable": item.reusable,
            "recoverable": item.recoverable,
            "cleanup_status": item.already_cleaned,
        }
        for item in sorted(attempts, key=lambda value: (value.target_id, value.attempt_id))
    )


def resolve_canonical_attempts(
    campaign: Path | str, *, search_roots: Sequence[Path] = ()
) -> Mapping[str, object]:
    """Resolve canonical attempts once using explicit state or unique reuse."""

    root = resolve_campaign(campaign, search_roots=search_roots)
    descriptor = _campaign_descriptor(root)
    campaign_id = _campaign_id(root, descriptor)
    attempts = _discover_attempts(root, campaign_id, load_cleanup_registry(root))
    canonical, ambiguous = _canonical_by_target(attempts, descriptor)
    return {
        "campaign_id": campaign_id,
        "campaign_root": str(root),
        "canonical_attempts": dict(sorted(canonical.items())),
        "ambiguous_targets": dict(sorted(ambiguous.items())),
    }


def _lock_paths(root: Path, attempts: Sequence[_Attempt]) -> tuple[Path, ...]:
    selected = {
        root / ".cmw" / "campaign.lock",
        root / "campaign.lock",
        root / ".cmw" / "watcher.lock",
        root / "watcher.lock",
    }
    for attempt in attempts:
        selected.update(
            {
                attempt.target_directory / "target.json.lock",
                attempt.target_directory / "target.lock",
                attempt.directory / "attempt.lock",
                attempt.directory / "target.json.lock",
            }
        )
    return tuple(sorted(path for path in selected if path.exists()))


def _process_lock_evidence(
    root: Path, attempts: Sequence[_Attempt], descriptor: Mapping[str, object]
) -> tuple[tuple[Mapping[str, object], ...], tuple[str, ...]]:
    evidence: list[Mapping[str, object]] = []
    blockers: list[str] = []
    for path in _lock_paths(root, attempts):
        inspection = inspect_lock(path)
        item = {
            "kind": "lock",
            "path": str(path),
            "state": inspection.state.value,
            "reason": inspection.reason,
            "pid": inspection.owner.pid if inspection.owner else None,
        }
        evidence.append(item)
        if inspection.state in {LockState.ACTIVE_LOCAL, LockState.UNCERTAIN}:
            blockers.append(f"{inspection.state.value}: {path}: {inspection.reason}")
    watcher = descriptor.get("watcher") or descriptor.get("orchestrator")
    if isinstance(watcher, Mapping):
        pid = watcher.get("pid") if isinstance(watcher.get("pid"), int) else None
        state = _pid_state(pid)
        active = watcher.get("active") is True or state == "ACTIVE"
        evidence.append({"kind": "watcher", "pid": pid, "state": state, "active": active})
        if active or state == "UNCERTAIN":
            blockers.append(f"ACTIVE_WATCHER: pid={pid} state={state}")
    for attempt in attempts:
        if attempt.already_cleaned is not None:
            continue
        evidence.append(
            {
                "kind": "attempt_process",
                "target_id": attempt.target_id,
                "attempt_id": attempt.attempt_id,
                "pid": attempt.recorded_pid,
                "state": attempt.pid_state,
            }
        )
        if attempt.pid_state in {"ACTIVE", "UNCERTAIN"}:
            blockers.append(
                f"{attempt.pid_state}_ATTEMPT_PROCESS: {attempt.target_id}/{attempt.attempt_id} pid={attempt.recorded_pid}"
            )
        if not attempt.terminal:
            blockers.append(f"NONTERMINAL_ATTEMPT: {attempt.target_id}/{attempt.attempt_id}")
    return tuple(evidence), tuple(sorted(set(blockers)))


def _category(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".gbw", ".molden", ".mwfn", ".cube", ".densities"}:
        return "heavy_scientific_payload"
    if suffix in {".out", ".err", ".log"}:
        return "execution_log"
    if suffix in {".tmp", ".scratch"} or "scratch" in path.parts:
        return "scratch"
    if suffix == ".json":
        return "provenance_metadata"
    return "other"


def _inventory(root: Path, attempt: Path) -> FileInventory:
    try:
        attempt.relative_to(root)
    except ValueError as exc:
        raise InvalidCleanupPath(f"attempt is outside campaign: {attempt}") from exc
    if attempt in (root, attempt.parent.parent) or attempt.is_symlink():
        raise InvalidCleanupPath(f"unsafe attempt root: {attempt}")
    root_device = root.stat().st_dev
    paths: list[str] = []
    categories: dict[str, int] = {}
    total = 0
    reclaim = 0
    shared = 0
    stack = [attempt]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != root_device:
                    raise InvalidCleanupPath(f"mount boundary inside attempt: {path}")
                relative = str(path.relative_to(attempt))
                paths.append(relative)
                if stat.S_ISLNK(info.st_mode):
                    try:
                        target = path.resolve(strict=True)
                    except FileNotFoundError:
                        categories["broken_symlink"] = categories.get("broken_symlink", 0) + 1
                        continue
                    try:
                        target.relative_to(attempt)
                    except ValueError as exc:
                        raise InvalidCleanupPath(f"symlink escapes attempt: {path} -> {target}") from exc
                    categories["symlink"] = categories.get("symlink", 0) + 1
                    continue
                if stat.S_ISDIR(info.st_mode):
                    stack.append(path)
                    categories["directory"] = categories.get("directory", 0) + 1
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise InvalidCleanupPath(f"unsupported filesystem object in attempt: {path}")
                category = _category(path)
                categories[category] = categories.get(category, 0) + 1
                total += info.st_size
                if info.st_nlink > 1:
                    shared += info.st_size
                    categories["shared_hardlink"] = categories.get("shared_hardlink", 0) + 1
                else:
                    reclaim += info.st_size
    return FileInventory(
        file_count=sum(1 for path in paths if not (attempt / path).is_dir()),
        total_bytes=total,
        estimated_reclaimed_bytes=reclaim,
        removed_paths=tuple(sorted(paths)),
        categories=categories,
        shared_hardlink_bytes=shared,
    )


def _diagnostic_tail(attempt: _Attempt, cap: int) -> dict[str, object] | None:
    if cap < 0:
        raise ValueError("diagnostic tail cap must be non-negative")
    names = ("stage.err", "session.log", "multiwfn-session.log", "stage.out", "finalization-diagnostic.log")
    selected = next((attempt.directory / name for name in names if (attempt.directory / name).is_file() and (attempt.directory / name).stat().st_size), None)
    if selected is None or cap == 0:
        return None
    size = selected.stat().st_size
    with selected.open("rb") as handle:
        handle.seek(max(0, size - cap))
        retained = handle.read(cap)
    text = retained.decode("utf-8", errors="replace")
    manifest = attempt.metadata.get("artifacts")
    source_hash = None
    if isinstance(manifest, Mapping):
        for evidence in manifest.values():
            path = _artifact_path(attempt.directory, evidence)
            if path == selected and isinstance(evidence, Mapping):
                source_hash = evidence.get("sha256")
                break
    return {
        "source": selected.name,
        "original_size_bytes": size,
        "retained_bytes": len(retained),
        "truncated": size > len(retained),
        "source_sha256": source_hash,
        "tail": text,
    }


def _bounded_mapping(value: object, *, depth: int = 0) -> object:
    if depth > 5:
        return "<depth-limit>"
    if isinstance(value, Mapping):
        return {str(key): _bounded_mapping(item, depth=depth + 1) for key, item in list(value.items())[:128]}
    if isinstance(value, list):
        return [_bounded_mapping(item, depth=depth + 1) for item in value[:128]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _first_not_none(*values: object) -> object | None:
    return next((value for value in values if value is not None), None)


def _atomic_json_size(value: object) -> int:
    return len(
        (
            json.dumps(
                value,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    )


def _tombstone(
    attempt: _Attempt,
    *,
    canonical_attempt_id: str,
    inventory: FileInventory,
    reason: str | None,
    diagnostic_tail_bytes: int,
    transaction_id: str,
) -> dict[str, object]:
    metadata = attempt.metadata
    artifacts = metadata.get("artifacts")
    output_hashes: dict[str, object] = {}
    if isinstance(artifacts, Mapping):
        for role, evidence in artifacts.items():
            if isinstance(evidence, Mapping) and isinstance(evidence.get("sha256"), str):
                output_hashes[str(role)] = {
                    "sha256": evidence["sha256"],
                    "size_bytes": evidence.get("size_bytes"),
                }
    attempt_record = metadata.get("attempt") if isinstance(metadata.get("attempt"), Mapping) else {}
    execution = metadata.get("execution") if isinstance(metadata.get("execution"), Mapping) else {}
    scientific = metadata.get("scientific") if isinstance(metadata.get("scientific"), Mapping) else {}
    provenance = metadata.get("provenance") if isinstance(metadata.get("provenance"), Mapping) else {}
    return {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "record_type": "AttemptCleanupTombstone",
        "campaign_id": attempt.campaign_id,
        "target_id": attempt.target_id,
        "attempt_id": attempt.attempt_id,
        "terminal_status": execution.get("status") or metadata.get("status"),
        "failure": {
            "stage": scientific.get("stage_type") or metadata.get("failure_stage"),
            "classification": scientific.get("status") or metadata.get("failure_classification"),
            "reason": scientific.get("reason") or metadata.get("reason"),
        },
        "exit_code": _first_not_none(
            execution.get("process_exit_code"), execution.get("exit_code")
        ),
        "timestamps": {
            "started_at": metadata.get("started_at") or attempt_record.get("created_at"),
            "ended_at": metadata.get("ended_at") or execution.get("ended_at"),
        },
        "superseding_attempt_id": canonical_attempt_id,
        "hashes": {
            "generated_input_sha256": attempt_record.get("generated_input_sha256"),
            "command_sha256": metadata.get("command_sha256"),
            "spec_sha256": metadata.get("spec_sha256"),
        },
        "runtime": _bounded_mapping(attempt_record.get("executable") or provenance.get("executable") or {}),
        "source_identity": _bounded_mapping(
            {
                "scientific_artifact_id": _nested(metadata, "scientific_artifact", "artifact_id"),
                "geometry": metadata.get("geometry_input") or metadata.get("lineage"),
                "protocol": _nested(metadata, "target", "calculation") or metadata.get("protocol"),
                "parent_artifacts": metadata.get("parent_artifacts"),
            }
        ),
        "original": {
            "total_bytes": inventory.total_bytes,
            "file_count": inventory.file_count,
            "authoritative_output_hashes": output_hashes,
        },
        "cleanup": {
            "transaction_id": transaction_id,
            "tool_version": CLEANUP_TOOL_VERSION,
            "reason": reason,
        },
        "diagnostic_tail": _diagnostic_tail(attempt, diagnostic_tail_bytes),
    }


OpenWriterProbe = Callable[[Path], tuple[bool | None, str]]


def _default_open_writer_probe(path: Path) -> tuple[bool | None, str]:
    executable = shutil.which("lsof")
    if executable is None:
        return None, "lsof unavailable"
    try:
        completed = subprocess.run(
            (executable, "-F", "fa", "+D", str(path)),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"open-writer probe failed: {exc}"
    if completed.returncode == 1 and not completed.stdout:
        return False, "no open descriptors"
    if completed.returncode not in (0, 1):
        return None, f"lsof exit {completed.returncode}"
    writable = any(line.startswith("f") and any(flag in line[1:] for flag in ("w", "u")) for line in completed.stdout.splitlines())
    return writable, "writable descriptor detected" if writable else "no writable descriptor detected"


def _campaign_state_hash(root: Path) -> str:
    records: list[dict[str, object]] = []
    relevant_names = {
        "campaign.json",
        "campaign-state.json",
        "state.json",
        "target.json",
        "job.json",
        "result.json",
        "analysis-result.json",
        "conversion-result.json",
        "materialization.json",
        "materialization-failure.json",
        "execution-attempt.json",
        "execution-layout.json",
        "finalization-failure.json",
        "attempt-tombstone.json",
        "registry.json",
    }
    for path in sorted(root.rglob("*.json")):
        if path.name not in relevant_names or "transactions" in path.parts:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            records.append(
                {
                    "path": str(path.relative_to(root)),
                    "sha256": file_hash(path),
                    "size_bytes": path.stat().st_size,
                }
            )
        except OSError as exc:
            raise CleanupError("CAMPAIGN_STATE_UNREADABLE", str(exc), CleanupExitCode.BLOCKED) from exc
    return stable_hash(records)


def build_cleanup_plan(
    campaign: Path | str,
    *,
    mode: CleanupMode | str = CleanupMode.MINIMAL_PROVENANCE,
    reason: str | None = None,
    tombstone_max_bytes: int = DEFAULT_TOMBSTONE_MAX_BYTES,
    diagnostic_tail_bytes: int = DEFAULT_DIAGNOSTIC_TAIL_BYTES,
    search_roots: Sequence[Path] = (),
    open_writer_probe: OpenWriterProbe | None = None,
) -> CleanupPlan:
    """Construct a deterministic, mutation-free superseded-attempt plan."""

    selected_mode = CleanupMode(mode)
    if tombstone_max_bytes < 1024:
        raise ValueError("tombstone maximum must be at least 1024 bytes")
    if diagnostic_tail_bytes < 0:
        raise ValueError("diagnostic tail cap must be non-negative")
    root = resolve_campaign(campaign, search_roots=search_roots)
    descriptor = _campaign_descriptor(root)
    campaign_id = _campaign_id(root, descriptor)
    registry = load_cleanup_registry(root)
    attempts = _discover_attempts(root, campaign_id, registry)
    canonical, ambiguous = _canonical_by_target(attempts, descriptor)
    graph, oversized_reference_records = _reference_graph(root)
    lock_evidence, global_blockers = _process_lock_evidence(root, attempts, descriptor)
    if oversized_reference_records:
        global_blockers = (
            *global_blockers,
            "REFERENCE_GRAPH_OVERSIZED: fail-closed cleanup requires streaming "
            "reference support for " + ", ".join(oversized_reference_records),
        )
    probe = open_writer_probe or _default_open_writer_probe
    candidates: list[AttemptDecision] = []
    ineligible: list[AttemptDecision] = []
    already: list[AttemptDecision] = []
    reusable_count: dict[str, int] = {}
    for item in attempts:
        if item.reusable:
            reusable_count[item.target_id] = reusable_count.get(item.target_id, 0) + 1
    provisional_transaction_id = "<transaction-at-apply>"
    for attempt in sorted(attempts, key=lambda item: (item.target_id, item.attempt_id)):
        if attempt.already_cleaned is not None:
            already.append(
                AttemptDecision(
                    campaign_id,
                    attempt.target_id,
                    attempt.attempt_id,
                    str(attempt.directory),
                    True,
                    False,
                    attempt.explicit_superseding_attempt_id,
                    False,
                    attempt.already_cleaned,
                    (f"already {attempt.already_cleaned.lower()}",),
                    (),
                    None,
                    (),
                )
            )
            continue
        reasons: list[str] = []
        canonical_id = canonical.get(attempt.target_id)
        references = _references_for(attempt, graph, root)
        payload_refs = tuple(reference for reference in references if reference.requires_payload)
        if global_blockers:
            reasons.append("campaign is actively mutating or ownership is uncertain")
        if not attempt.terminal:
            reasons.append("attempt is nonterminal")
        if attempt.pid_state in {"ACTIVE", "UNCERTAIN"}:
            reasons.append(f"attempt process is {attempt.pid_state.lower()}")
        if attempt.target_id in ambiguous:
            reasons.append(ambiguous[attempt.target_id])
        if canonical_id is None:
            reasons.append("no separate canonical finalized reusable attempt")
        elif canonical_id == attempt.attempt_id:
            reasons.append("attempt is canonical")
        else:
            replacement = next(
                (other for other in attempts if other.target_id == attempt.target_id and other.attempt_id == canonical_id),
                None,
            )
            if replacement is None or not replacement.reusable:
                reasons.append("canonical replacement is absent or not reusable")
        if attempt.recoverable:
            reasons.append("attempt is needed for finalization or recovery")
        if attempt.reusable and reusable_count.get(attempt.target_id, 0) < 2:
            reasons.append("attempt is the sole surviving finalized result")
        if payload_refs:
            reasons.append("authoritative references require original attempt payload")
        inventory: FileInventory | None = None
        tombstone: Mapping[str, object] | None = None
        try:
            inventory = _inventory(root, attempt.directory)
        except InvalidCleanupPath as exc:
            reasons.append(str(exc))
        if not reasons and canonical_id is not None:
            writer, writer_reason = probe(attempt.directory)
            lock_evidence = (*lock_evidence, {
                "kind": "open_writer",
                "target_id": attempt.target_id,
                "attempt_id": attempt.attempt_id,
                "state": "ACTIVE" if writer is True else "UNCERTAIN" if writer is None else "ABSENT",
                "reason": writer_reason,
            })
            if writer is True:
                reasons.append("candidate has a live writer")
            elif writer is None:
                # An unavailable optional probe is evidence, not a blocker; process and
                # lock ownership remain the authoritative cross-platform gates.
                pass
        if not reasons and selected_mode is CleanupMode.MINIMAL_PROVENANCE and inventory and canonical_id:
            tombstone = _tombstone(
                attempt,
                canonical_attempt_id=canonical_id,
                inventory=inventory,
                reason=reason,
                diagnostic_tail_bytes=diagnostic_tail_bytes,
                transaction_id=provisional_transaction_id,
            )
            tombstone_size = _atomic_json_size(tombstone)
            if tombstone_size > tombstone_max_bytes:
                reasons.append(
                    f"required tombstone is {tombstone_size} bytes and exceeds {tombstone_max_bytes}-byte cap"
                )
                tombstone = None
        decision = AttemptDecision(
            campaign_id,
            attempt.target_id,
            attempt.attempt_id,
            str(attempt.directory),
            attempt.terminal,
            attempt.reusable,
            canonical_id,
            not reasons,
            None,
            tuple(sorted(set(reasons))) if reasons else ("provably superseded by finalized reusable attempt",),
            references,
            inventory,
            ("attempt-tombstone.json",) if selected_mode is CleanupMode.MINIMAL_PROVENANCE else (),
            tombstone,
        )
        (candidates if decision.eligible else ineligible).append(decision)
    return CleanupPlan(
        campaign_id,
        str(root),
        _campaign_state_hash(root),
        selected_mode,
        tuple(candidates),
        tuple(ineligible),
        tuple(already),
        global_blockers,
        tuple(lock_evidence),
    )


def _transaction_root(root: Path) -> Path:
    return root / ".cmw" / "cleanup" / "transactions"


def _transaction_state(path: Path, state: TransactionState, **extra: object) -> dict[str, object]:
    value = {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "transaction_id": path.name,
        "state": state.value,
        "updated_at": _now(),
        **extra,
    }
    atomic_write_json(path / "state.json", value)
    return value


def _safe_remove_tree(path: Path, *, root: Path) -> int:
    """Remove one validated tree without following links or invoking a shell."""

    resolved_parent = path.parent.resolve(strict=True)
    try:
        resolved_parent.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise InvalidCleanupPath(f"deletion path is outside campaign: {path}") from exc
    if path.is_symlink():
        path.unlink()
        return 0
    if not path.is_dir():
        raise InvalidCleanupPath(f"deletion target is not a directory: {path}")
    removed = 0
    stack: list[tuple[Path, bool]] = [(path, False)]
    while stack:
        current, visited = stack.pop()
        if visited:
            current.rmdir()
            continue
        stack.append((current, True))
        with os.scandir(current) as entries:
            for entry in entries:
                child = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append((child, False))
                else:
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        removed += info.st_size
                    child.unlink()
    return removed


def _registry_with_transaction(
    registry: Mapping[str, object],
    *,
    plan: CleanupPlan,
    transaction_id: str,
) -> dict[str, object]:
    value = dict(registry)
    attempts = dict(value.get("attempts", {}))
    for candidate in plan.candidates:
        attempts[candidate.key] = {
            "status": "COMPACTED" if plan.mode is CleanupMode.MINIMAL_PROVENANCE else "PURGED",
            "mode": plan.mode.value,
            "transaction_id": transaction_id,
            "retained_path": candidate.attempt_directory if plan.mode is CleanupMode.MINIMAL_PROVENANCE else None,
            "superseding_attempt_id": candidate.canonical_attempt_id,
            "completion_state": "COMMITTED",
        }
    transactions = list(value.get("transactions", ()))
    transactions.append(transaction_id)
    return {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "attempts": attempts,
        "transactions": list(dict.fromkeys(str(item) for item in transactions)),
    }


def _write_purge_receipt(
    transaction: Path,
    *,
    plan: CleanupPlan,
    reason: str | None,
    bytes_removed: int,
    post_hash: str,
    state: str,
) -> None:
    atomic_write_json(
        transaction / "receipt.json",
        {
            "cleanup_transaction_id": transaction.name,
            "cleanup_mode": CleanupMode.PURGE.value,
            "tool_version": CLEANUP_TOOL_VERSION,
            "timestamp": _now(),
            "reason": reason,
            "attempts_removed": len(plan.candidates),
            "total_bytes_removed": bytes_removed,
            "pre_cleanup_campaign_state_hash": plan.campaign_state_sha256,
            "post_cleanup_campaign_state_hash": post_hash,
            "cleanup_plan_hash": plan.plan_sha256,
            "state": state,
        },
    )


FaultHook = Callable[[str], None]


def _rollback_transaction(root: Path, transaction: Path) -> None:
    _transaction_state(transaction, TransactionState.ROLLING_BACK)
    manifest_path = transaction / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else {"entries": []}
    entries = manifest.get("entries", ())
    for entry in reversed(list(entries) if isinstance(entries, list) else []):
        if not isinstance(entry, Mapping):
            continue
        original = Path(str(entry["original_path"]))
        staged = Path(str(entry["staged_path"]))
        tombstone = original / "attempt-tombstone.json"
        if tombstone.is_file():
            tombstone.unlink()
            try:
                original.rmdir()
            except OSError:
                pass
        if staged.exists() and not original.exists():
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, original)
    backup = transaction / "registry-before.json"
    registry = _registry_path(root)
    if backup.is_file():
        atomic_write_json(registry, read_json(backup))
    elif registry.is_file():
        current = read_json(registry)
        if any(
            isinstance(item, Mapping) and item.get("transaction_id") == transaction.name
            for item in dict(current.get("attempts", {})).values()
        ):
            registry.unlink()
    _transaction_state(transaction, TransactionState.ROLLED_BACK)


def recover_cleanup_transactions(root: Path) -> tuple[Mapping[str, object], ...]:
    """Recover discoverable incomplete transactions while cleanup lock is held."""

    actions: list[Mapping[str, object]] = []
    transactions = _transaction_root(root)
    if not transactions.is_dir():
        return ()
    for transaction in sorted(path for path in transactions.iterdir() if path.is_dir()):
        state_path = transaction / "state.json"
        if not state_path.is_file():
            if not any(transaction.iterdir()):
                transaction.rmdir()
                actions.append({"transaction_id": transaction.name, "action": "REMOVED_EMPTY_ORPHAN"})
                continue
            raise CleanupError(
                "ORPHANED_CLEANUP_TRANSACTION",
                f"transaction has no state: {transaction}",
                CleanupExitCode.ROLLBACK_REQUIRED,
            )
        state = str(read_json(state_path).get("state", ""))
        if state in {TransactionState.COMMITTED.value, TransactionState.ROLLED_BACK.value}:
            continue
        registry = load_cleanup_registry(root)
        registry_owned = any(
            isinstance(item, Mapping) and item.get("transaction_id") == transaction.name
            for item in dict(registry.get("attempts", {})).values()
        )
        if registry_owned and state in {TransactionState.METADATA_UPDATING.value, TransactionState.FAILED.value}:
            staging = transaction / "staging"
            bytes_removed = _safe_remove_tree(staging, root=root) if staging.is_dir() else 0
            manifest = _json_if_mapping(transaction / "manifest.json") or {}
            if manifest.get("mode") == CleanupMode.PURGE.value:
                snapshot = _json_if_mapping(transaction / "plan.json") or {}
                plan_record = snapshot.get("plan") if isinstance(snapshot.get("plan"), Mapping) else {}
                post_hash = _campaign_state_hash(root)
                atomic_write_json(
                    transaction / "receipt.json",
                    {
                        "cleanup_transaction_id": transaction.name,
                        "cleanup_mode": CleanupMode.PURGE.value,
                        "tool_version": CLEANUP_TOOL_VERSION,
                        "timestamp": _now(),
                        "reason": snapshot.get("reason"),
                        "attempts_removed": len(manifest.get("entries", ())),
                        "total_bytes_removed": bytes_removed,
                        "pre_cleanup_campaign_state_hash": plan_record.get("campaign_state_sha256"),
                        "post_cleanup_campaign_state_hash": post_hash,
                        "cleanup_plan_hash": manifest.get("plan_sha256"),
                        "state": "COMMITTED",
                    },
                )
                for private_record in (
                    transaction / "manifest.json",
                    transaction / "registry-before.json",
                    transaction / "plan.json",
                ):
                    try:
                        private_record.unlink()
                    except FileNotFoundError:
                        pass
            _transaction_state(transaction, TransactionState.COMMITTED, recovered=True, bytes_removed=bytes_removed)
            actions.append({"transaction_id": transaction.name, "action": "RESUMED", "bytes_removed": bytes_removed})
        else:
            _rollback_transaction(root, transaction)
            actions.append({"transaction_id": transaction.name, "action": "ROLLED_BACK"})
    return tuple(actions)


def apply_cleanup_plan(
    plan: CleanupPlan,
    *,
    confirm_plan: str | None = None,
    reason: str | None = None,
    tombstone_max_bytes: int = DEFAULT_TOMBSTONE_MAX_BYTES,
    diagnostic_tail_bytes: int = DEFAULT_DIAGNOSTIC_TAIL_BYTES,
    open_writer_probe: OpenWriterProbe | None = None,
    fault_hook: FaultHook | None = None,
    remove_tree: Callable[..., int] = _safe_remove_tree,
) -> CleanupResult:
    """Apply a fresh plan as an exclusive, recoverable campaign transaction."""

    root = resolve_campaign(plan.campaign_root)
    lock_path = root / ".cmw" / "cleanup.lock"
    inspection = inspect_lock(lock_path)
    try:
        owner = acquire_lock(
            lock_path,
            job_id="cleanup-attempts",
            replace_stale=inspection.state is LockState.STALE_LOCAL,
        )
    except RuntimeError as exc:
        raise CleanupError(
            "ACTIVE_CAMPAIGN",
            f"exclusive cleanup ownership is unavailable: {exc}",
            CleanupExitCode.ACTIVE_CAMPAIGN,
        ) from exc
    transaction: Path | None = None
    recovery: tuple[Mapping[str, object], ...] = ()
    irreversible = False
    try:
        recovery = recover_cleanup_transactions(root)
        fresh = build_cleanup_plan(
            root,
            mode=plan.mode,
            reason=reason,
            tombstone_max_bytes=tombstone_max_bytes,
            diagnostic_tail_bytes=diagnostic_tail_bytes,
            open_writer_probe=open_writer_probe,
        )
        if fresh.plan_sha256 != plan.plan_sha256:
            raise StaleCleanupPlan()
        if fresh.global_blockers:
            raise CleanupError("ACTIVE_CAMPAIGN", "; ".join(fresh.global_blockers), CleanupExitCode.ACTIVE_CAMPAIGN)
        if not fresh.candidates:
            return CleanupResult(
                "NO_ELIGIBLE_ATTEMPTS",
                None,
                fresh.plan_sha256,
                fresh.mode,
                0,
                0,
                _campaign_state_hash(root),
                recovery,
            )
        if fresh.mode is CleanupMode.PURGE:
            if confirm_plan is None or len(confirm_plan) != 64 or confirm_plan != fresh.plan_sha256:
                raise CleanupError(
                    "PLAN_CONFIRMATION_REQUIRED",
                    "purge requires the exact full cleanup plan SHA-256",
                    CleanupExitCode.INVALID_ARGUMENT,
                )
        transaction_id = uuid4().hex
        transaction = _transaction_root(root) / transaction_id
        transaction.mkdir(parents=True, exist_ok=False)
        _transaction_state(transaction, TransactionState.PLANNED, plan_sha256=plan.plan_sha256)
        atomic_write_json(
            transaction / "plan.json",
            {"plan": fresh.to_dict(), "reason": reason},
        )
        registry = load_cleanup_registry(root)
        if _registry_path(root).is_file():
            atomic_write_json(transaction / "registry-before.json", registry)
        entries: list[dict[str, str]] = []
        for candidate in fresh.candidates:
            staged = transaction / "staging" / candidate.target_id / candidate.attempt_id
            entries.append({"original_path": candidate.attempt_directory, "staged_path": str(staged)})
        atomic_write_json(
            transaction / "manifest.json",
            {
                "schema_version": CLEANUP_SCHEMA_VERSION,
                "transaction_id": transaction_id,
                "plan_sha256": plan.plan_sha256,
                "mode": plan.mode.value,
                "entries": entries,
            },
        )
        if fault_hook:
            fault_hook("before_staging")
        _transaction_state(transaction, TransactionState.STAGING)
        for candidate, entry in zip(fresh.candidates, entries):
            original = Path(entry["original_path"])
            staged = Path(entry["staged_path"])
            staged.parent.mkdir(parents=True, exist_ok=True)
            os.replace(original, staged)
            if plan.mode is CleanupMode.MINIMAL_PROVENANCE:
                original.mkdir(parents=False, exist_ok=False)
                tombstone = dict(candidate.tombstone or {})
                cleanup = dict(tombstone.get("cleanup", {}))
                cleanup["transaction_id"] = transaction_id
                tombstone["cleanup"] = cleanup
                if _atomic_json_size(tombstone) > tombstone_max_bytes:
                    raise CleanupError("TOMBSTONE_TOO_LARGE", "final tombstone exceeds configured cap", CleanupExitCode.TRANSACTION_FAILED)
                atomic_write_json(original / "attempt-tombstone.json", tombstone)
        _transaction_state(transaction, TransactionState.STAGED)
        if fault_hook:
            fault_hook("after_staging")
        _transaction_state(transaction, TransactionState.METADATA_UPDATING)
        atomic_write_json(
            _registry_path(root),
            _registry_with_transaction(registry, plan=fresh, transaction_id=transaction_id),
        )
        if fault_hook:
            fault_hook("during_metadata_update")
        # Re-resolve canonical results before any irreversible staged deletion.
        post_overlay_attempts = _discover_attempts(root, fresh.campaign_id, load_cleanup_registry(root))
        post_canonical, post_ambiguous = _canonical_by_target(post_overlay_attempts, _campaign_descriptor(root))
        if post_ambiguous or any(
            post_canonical.get(item.target_id) != item.canonical_attempt_id
            for item in fresh.candidates
        ):
            raise CleanupError("POST_CLEANUP_VALIDATION_FAILED", "canonical attempt resolution changed", CleanupExitCode.TRANSACTION_FAILED)
        irreversible = True
        staging_root = transaction / "staging"
        bytes_removed = remove_tree(staging_root, root=root) if staging_root.is_dir() else 0
        post_hash = _campaign_state_hash(root)
        if plan.mode is CleanupMode.PURGE:
            _write_purge_receipt(
                transaction,
                plan=fresh,
                reason=reason,
                bytes_removed=bytes_removed,
                post_hash=post_hash,
                state="COMMITTED",
            )
            # Purge retains only the bounded campaign-level receipt and state.
            # The staging manifest and pre-cleanup registry could reconstruct
            # attempt-local provenance and therefore must not survive commit.
            for private_record in (
                transaction / "manifest.json",
                transaction / "registry-before.json",
                transaction / "plan.json",
            ):
                try:
                    private_record.unlink()
                except FileNotFoundError:
                    pass
        _transaction_state(
            transaction,
            TransactionState.COMMITTED,
            attempts_cleaned=len(fresh.candidates),
            bytes_removed=bytes_removed,
            post_cleanup_campaign_state_sha256=post_hash,
        )
        return CleanupResult(
            "COMMITTED",
            transaction_id,
            plan.plan_sha256,
            plan.mode,
            len(fresh.candidates),
            bytes_removed,
            post_hash,
            recovery,
        )
    except CleanupError:
        if transaction is not None:
            if irreversible:
                _transaction_state(transaction, TransactionState.FAILED, rollback_required=True)
            else:
                _rollback_transaction(root, transaction)
        raise
    except Exception as exc:
        if transaction is not None:
            if irreversible:
                _transaction_state(transaction, TransactionState.FAILED, rollback_required=True, reason=str(exc))
                raise CleanupError(
                    "ROLLBACK_REQUIRED",
                    f"cleanup failed after irreversible deletion began: {exc}",
                    CleanupExitCode.ROLLBACK_REQUIRED,
                ) from exc
            try:
                _rollback_transaction(root, transaction)
            except Exception as rollback_exc:
                _transaction_state(transaction, TransactionState.FAILED, rollback_required=True, reason=str(rollback_exc))
                raise CleanupError(
                    "ROLLBACK_REQUIRED",
                    f"cleanup failed and rollback was incomplete: {rollback_exc}",
                    CleanupExitCode.ROLLBACK_REQUIRED,
                ) from exc
        raise CleanupError("TRANSACTION_FAILED", str(exc), CleanupExitCode.TRANSACTION_FAILED) from exc
    finally:
        release_lock(lock_path, token=owner.token)


def cleanup_exit_code(plan: CleanupPlan, result: CleanupResult | None = None) -> CleanupExitCode:
    if result is not None and result.status in {"COMMITTED", "DRY_RUN"}:
        return CleanupExitCode.SUCCESS
    if result is not None and result.status == "NO_ELIGIBLE_ATTEMPTS":
        return CleanupExitCode.NO_ELIGIBLE_ATTEMPTS
    if plan.global_blockers:
        return CleanupExitCode.ACTIVE_CAMPAIGN
    if plan.candidates:
        return CleanupExitCode.SUCCESS
    if plan.ineligible:
        return CleanupExitCode.BLOCKED
    return CleanupExitCode.NO_ELIGIBLE_ATTEMPTS


def format_cleanup_plan(plan: CleanupPlan) -> str:
    lines = [
        f"CMW cleanup plan {plan.plan_sha256}",
        f"campaign: {plan.campaign_id}",
        f"root: {plan.campaign_root}",
        f"mode: {plan.mode.value}",
        f"eligible: {len(plan.candidates)}; blocked: {len(plan.ineligible)}; already cleaned: {len(plan.already_cleaned)}",
        f"estimated reclaim: {plan.estimated_reclaimed_bytes} bytes",
    ]
    if plan.global_blockers:
        lines.append("campaign blockers:")
        lines.extend(f"  - {reason}" for reason in plan.global_blockers)
    lines.append("candidates:")
    lines.extend(
        f"  {item.target_id} {item.attempt_id} -> {item.canonical_attempt_id} ({item.inventory.total_bytes if item.inventory else 0} bytes)"
        for item in plan.candidates
    )
    if not plan.candidates:
        lines.append("  (none)")
    lines.append("blocked attempts:")
    lines.extend(
        f"  {item.target_id} {item.attempt_id}: {'; '.join(item.reasons)}"
        for item in plan.ineligible
    )
    if not plan.ineligible:
        lines.append("  (none)")
    return "\n".join(lines)


__all__ = [
    "CLEANUP_SCHEMA_VERSION",
    "CLEANUP_TOOL_VERSION",
    "DEFAULT_DIAGNOSTIC_TAIL_BYTES",
    "DEFAULT_TOMBSTONE_MAX_BYTES",
    "AttemptDecision",
    "CleanupError",
    "CleanupExitCode",
    "CleanupMode",
    "CleanupPlan",
    "CleanupResult",
    "FileInventory",
    "InvalidCleanupPath",
    "ReferenceEvidence",
    "StaleCleanupPlan",
    "TransactionState",
    "apply_cleanup_plan",
    "build_cleanup_plan",
    "cleanup_exit_code",
    "enumerate_campaign_attempts",
    "format_cleanup_plan",
    "load_cleanup_registry",
    "recover_cleanup_transactions",
    "resolve_canonical_attempts",
    "resolve_campaign",
]
