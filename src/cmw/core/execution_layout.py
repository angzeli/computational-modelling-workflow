"""Deterministic, project-independent paths for executable workflow attempts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .provenance import stable_hash


EXECUTION_LAYOUT_SCHEMA_VERSION = 2
LEGACY_EXECUTION_LAYOUT_SCHEMA_VERSION = 1
DEFAULT_SHORT_TARGET_ID_WIDTH = 12
ATTEMPT_PATTERN = re.compile(r"attempt_(\d{3,})\Z")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
HEX_TARGET_PATTERN = re.compile(r"[0-9a-fA-F]+\Z")
TARGET_MANIFEST_FILENAME = "target-manifest.json"


class ExecutionLayoutVersion(str, Enum):
    """Versioned filesystem projection of stable execution identities."""

    V1 = "v1"
    V2 = "v2"


class ExecutionLayoutError(ValueError):
    """Raised when execution paths or attempt identity are unresolved or conflicting."""

    code = "FAILED_EXECUTION_LAYOUT"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__(message)


def _identifier(value: str, *, name: str) -> str:
    selected = str(value)
    if not IDENTIFIER_PATTERN.fullmatch(selected):
        raise ExecutionLayoutError(
            f"{ExecutionLayoutError.code}: {name} must be one path-safe identifier"
        )
    return selected


def _absolute_root(value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or "{" in str(path) or "}" in str(path):
        raise ExecutionLayoutError(
            f"{ExecutionLayoutError.code}: project root must be an absolute resolved path"
        )
    return path.resolve()


def execution_target_directory(
    project_root: Path | str,
    *,
    system_identifier: str,
    workflow_node_identifier: str,
    target_identifier: str,
) -> Path:
    """Resolve the stable directory shared by all attempts of one target."""

    root = _absolute_root(project_root)
    system = _identifier(system_identifier, name="system identifier")
    node = _identifier(workflow_node_identifier, name="workflow node identifier")
    target = _identifier(target_identifier, name="target identifier")
    return root / "calculation" / system / node / target


def target_display_id(target_identifier: str, *, width: int = DEFAULT_SHORT_TARGET_ID_WIDTH) -> str:
    """Return the non-authoritative display prefix used by layout v2."""

    target = _identifier(target_identifier, name="target identifier")
    if width < 8 or width > len(target):
        raise ExecutionLayoutError(
            f"{ExecutionLayoutError.code}: short target id width must be between 8 "
            "and the full target id length"
        )
    if HEX_TARGET_PATTERN.fullmatch(target) is None:
        raise ExecutionLayoutError(
            f"{ExecutionLayoutError.code}: v2 target identifiers must be hexadecimal"
        )
    return target[:width].lower()


def execution_target_directory_v2(
    scientific_leaf: Path | str,
    *,
    target_identifier: str,
    operational_stage: str | None = None,
    short_id_width: int = DEFAULT_SHORT_TARGET_ID_WIDTH,
) -> Path:
    """Resolve the v2 target path without projecting scientific hashes as layers."""

    root = _absolute_root(scientific_leaf)
    if operational_stage is not None:
        root = root / _identifier(operational_stage, name="operational stage")
    display = target_display_id(target_identifier, width=short_id_width)
    return root / f"target-{display}"


def _authoritative_target_id(target_directory: Path) -> str | None:
    """Read a full target identity from metadata, never from a short path prefix."""

    for filename in (TARGET_MANIFEST_FILENAME, "target.json"):
        path = target_directory / filename
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if filename == TARGET_MANIFEST_FILENAME:
            candidate = record.get("full_target_id")
        else:
            nested = record.get("target")
            candidate = nested.get("target_id") if isinstance(nested, Mapping) else record.get("target_id")
        if candidate:
            return str(candidate)
    return None


def assert_v2_target_available(
    target_directory: Path | str, *, target_identifier: str
) -> None:
    """Fail closed if one display directory aliases a different full identity."""

    target = _absolute_root(target_directory)
    if not os.path.lexists(target):
        return
    stored = _authoritative_target_id(target)
    if stored is None:
        raise ExecutionLayoutError(
            "SHORT_TARGET_DISPLAY_ID_COLLISION: existing v2 target has no "
            "authoritative manifest",
            code="SHORT_TARGET_DISPLAY_ID_COLLISION",
        )
    if stored != target_identifier:
        raise ExecutionLayoutError(
            "SHORT_TARGET_DISPLAY_ID_COLLISION: display path belongs to full "
            f"target {stored}, not {target_identifier}",
            code="SHORT_TARGET_DISPLAY_ID_COLLISION",
        )


def next_attempt_identifier(target_directory: Path | str) -> str:
    """Return the next deterministic attempt_NNN identifier for one target."""

    target = _absolute_root(target_directory)
    attempts = target / "attempts"
    indices = []
    if attempts.is_dir():
        for item in attempts.iterdir():
            match = ATTEMPT_PATTERN.fullmatch(item.name)
            if match and item.is_dir():
                indices.append(int(match.group(1)))
    # Purged attempts intentionally have no attempt-local tombstone.  The
    # campaign cleanup overlay therefore participates in allocation so a
    # historical scientific attempt number is never reused.
    try:
        full_target_id = _authoritative_target_id(target) or target.name
        for ancestor in (target, *target.parents):
            registry_path = ancestor / ".cmw" / "cleanup" / "registry.json"
            if not registry_path.is_file():
                continue
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            entries = registry.get("attempts", {})
            if isinstance(entries, dict):
                for key, entry in entries.items():
                    raw_key = str(key)
                    entry_target_id = (
                        str(entry.get("target_id"))
                        if isinstance(entry, Mapping) and entry.get("target_id")
                        else None
                    )
                    if entry_target_id not in (None, full_target_id):
                        continue
                    attempt_name = raw_key.rsplit("/", 1)[-1]
                    if entry_target_id is None and not raw_key.startswith(full_target_id + "/"):
                        continue
                    match = ATTEMPT_PATTERN.fullmatch(attempt_name)
                    if match:
                        indices.append(int(match.group(1)))
            break
    except (OSError, UnicodeError, ValueError, TypeError):
        # Registry validity is enforced by cleanup/campaign loading.  Attempt
        # allocation remains backward compatible when no overlay is present.
        pass
    return f"attempt_{max(indices, default=0) + 1:03d}"


@dataclass(frozen=True)
class ExecutionLayout:
    """Resolved attempt layout derived only from stable execution identities."""

    project_root: Path
    system_identifier: str
    workflow_node_identifier: str
    target_identifier: str
    attempt_identifier: str
    version: ExecutionLayoutVersion = ExecutionLayoutVersion.V1
    operational_stage: str | None = None
    short_id_width: int = DEFAULT_SHORT_TARGET_ID_WIDTH

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", _absolute_root(self.project_root))
        for field_name, label in (
            ("system_identifier", "system identifier"),
            ("workflow_node_identifier", "workflow node identifier"),
            ("target_identifier", "target identifier"),
            ("attempt_identifier", "attempt identifier"),
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), name=label),
            )
        if ATTEMPT_PATTERN.fullmatch(self.attempt_identifier) is None:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: attempt identifier must use attempt_NNN"
            )
        selected_version = ExecutionLayoutVersion(self.version)
        object.__setattr__(self, "version", selected_version)
        if self.operational_stage is not None:
            object.__setattr__(
                self,
                "operational_stage",
                _identifier(self.operational_stage, name="operational stage"),
            )
        if selected_version is ExecutionLayoutVersion.V2:
            target_display_id(self.target_identifier, width=self.short_id_width)
        elif self.operational_stage is not None:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: operational stage is a v2-only path field"
            )

    @property
    def target_directory(self) -> Path:
        if self.version is ExecutionLayoutVersion.V2:
            return execution_target_directory_v2(
                self.project_root,
                target_identifier=self.target_identifier,
                operational_stage=self.operational_stage,
                short_id_width=self.short_id_width,
            )
        return execution_target_directory(
            self.project_root,
            system_identifier=self.system_identifier,
            workflow_node_identifier=self.workflow_node_identifier,
            target_identifier=self.target_identifier,
        )

    @property
    def attempts_directory(self) -> Path:
        return self.target_directory / "attempts"

    @property
    def working_directory(self) -> Path:
        return self.attempts_directory / self.attempt_identifier

    @property
    def output_directory(self) -> Path:
        return self.working_directory

    @property
    def provenance_directory(self) -> Path:
        return self.working_directory

    @property
    def logs_directory(self) -> Path:
        return self.working_directory

    @property
    def layout_path(self) -> Path:
        return self.provenance_directory / "execution-layout.json"

    def metadata_path(self, filename: str = "attempt.json") -> Path:
        return self.provenance_directory / _identifier(filename, name="metadata filename")

    def input_path(self, filename: str) -> Path:
        return self.working_directory / _identifier(filename, name="input filename")

    def output_path(self, filename: str) -> Path:
        return self.output_directory / _identifier(filename, name="output filename")

    def log_path(self, filename: str) -> Path:
        return self.logs_directory / _identifier(filename, name="log filename")

    def create_working_directory(self) -> None:
        if self.version is ExecutionLayoutVersion.V2:
            assert_v2_target_available(
                self.target_directory, target_identifier=self.target_identifier
            )
        self.working_directory.mkdir(parents=True, exist_ok=False)

    @property
    def display_target_identifier(self) -> str:
        if self.version is ExecutionLayoutVersion.V2:
            return f"target-{target_display_id(self.target_identifier, width=self.short_id_width)}"
        return self.target_identifier

    def validate(self, *, require_existing: bool = False) -> None:
        for path in (
            self.target_directory,
            self.attempts_directory,
            self.working_directory,
            self.output_directory,
            self.provenance_directory,
            self.logs_directory,
        ):
            if not path.is_absolute():
                raise ExecutionLayoutError(
                    f"{ExecutionLayoutError.code}: execution path is unresolved: {path}"
                )
        if require_existing and not self.working_directory.is_dir():
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: working directory is missing: "
                f"{self.working_directory}"
            )

    def validate_attempt_identity(self, attempt_identifier: str) -> None:
        if attempt_identifier != self.attempt_identifier:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: conflicting attempt identity "
                f"{attempt_identifier!r}; expected {self.attempt_identifier!r}"
            )

    def validate_paths(
        self,
        *,
        input_paths: Sequence[Path] = (),
        output_paths: Sequence[Path] = (),
        metadata_paths: Sequence[Path] = (),
        log_paths: Sequence[Path] = (),
    ) -> None:
        """Require resolved attempt-owned paths in their declared directories."""

        self.validate(require_existing=True)
        categories = (
            (input_paths, self.working_directory, "input"),
            (output_paths, self.output_directory, "output"),
            (metadata_paths, self.provenance_directory, "metadata"),
            (log_paths, self.logs_directory, "log"),
        )
        for paths, parent, category in categories:
            for path in paths:
                selected = Path(path)
                if not selected.is_absolute():
                    raise ExecutionLayoutError(
                        f"{ExecutionLayoutError.code}: {category} path is unresolved: "
                        f"{selected}"
                    )
                resolved = selected.resolve()
                try:
                    resolved.relative_to(parent)
                except ValueError as exc:
                    raise ExecutionLayoutError(
                        f"{ExecutionLayoutError.code}: {category} path is outside "
                        f"the execution layout: {resolved}"
                    ) from exc

    def to_dict(self) -> dict[str, object]:
        if self.version is ExecutionLayoutVersion.V2:
            return {
                "schema_version": EXECUTION_LAYOUT_SCHEMA_VERSION,
                "layout_version": self.version.value,
                "root_context": "scientific_leaf",
                "scientific_leaf": ".",
                "root_hint": str(self.project_root),
                "system_identifier": self.system_identifier,
                "workflow_node_identifier": self.workflow_node_identifier,
                "target_identifier": self.target_identifier,
                "target_display_id": self.display_target_identifier,
                "short_id_width": self.short_id_width,
                "operational_stage": self.operational_stage,
                "attempt_identifier": self.attempt_identifier,
                "target_path": self.target_directory.relative_to(self.project_root).as_posix(),
                "working_path": self.working_directory.relative_to(self.project_root).as_posix(),
                "output_path": self.output_directory.relative_to(self.project_root).as_posix(),
                "provenance_path": self.provenance_directory.relative_to(self.project_root).as_posix(),
                "logs_path": self.logs_directory.relative_to(self.project_root).as_posix(),
            }
        return {
            "schema_version": LEGACY_EXECUTION_LAYOUT_SCHEMA_VERSION,
            "layout_version": self.version.value,
            "project_root": str(self.project_root),
            "system_identifier": self.system_identifier,
            "workflow_node_identifier": self.workflow_node_identifier,
            "target_identifier": self.target_identifier,
            "attempt_identifier": self.attempt_identifier,
            "target_directory": str(self.target_directory),
            "working_directory": str(self.working_directory),
            "output_directory": str(self.output_directory),
            "provenance_directory": str(self.provenance_directory),
            "logs_directory": str(self.logs_directory),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        root_override: Path | str | None = None,
    ) -> "ExecutionLayout":
        schema = value.get("schema_version")
        if schema not in (
            LEGACY_EXECUTION_LAYOUT_SCHEMA_VERSION,
            EXECUTION_LAYOUT_SCHEMA_VERSION,
        ):
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: unsupported execution layout schema"
            )
        if schema == LEGACY_EXECUTION_LAYOUT_SCHEMA_VERSION:
            version = ExecutionLayoutVersion.V1
            root = Path(str(value["project_root"]))
        else:
            version = ExecutionLayoutVersion(str(value.get("layout_version", "v2")))
            if version is not ExecutionLayoutVersion.V2:
                raise ExecutionLayoutError(
                    f"{ExecutionLayoutError.code}: schema v2 requires layout version v2"
                )
            root_value = root_override if root_override is not None else value.get("root_hint")
            if root_value is None:
                raise ExecutionLayoutError(
                    f"{ExecutionLayoutError.code}: v2 layout resolution requires root context"
                )
            root = Path(str(root_value))
        layout = cls(
            project_root=root,
            system_identifier=str(value["system_identifier"]),
            workflow_node_identifier=str(value["workflow_node_identifier"]),
            target_identifier=str(value["target_identifier"]),
            attempt_identifier=str(value["attempt_identifier"]),
            version=version,
            operational_stage=(
                str(value["operational_stage"])
                if value.get("operational_stage") is not None
                else None
            ),
            short_id_width=int(value.get("short_id_width", DEFAULT_SHORT_TARGET_ID_WIDTH)),
        )
        expected = layout.to_dict()
        path_keys = (
            ("target_directory", "working_directory", "output_directory", "provenance_directory", "logs_directory")
            if version is ExecutionLayoutVersion.V1
            else ("target_path", "working_path", "output_path", "provenance_path", "logs_path")
        )
        for key in path_keys:
            if str(value.get(key, "")) != expected[key]:
                raise ExecutionLayoutError(
                    f"{ExecutionLayoutError.code}: stored {key} conflicts with identities"
                )
        return layout


def build_target_manifest(
    layout: ExecutionLayout,
    *,
    target: Mapping[str, object],
    source_artifact_ids: Sequence[str] = (),
    execution_intent: Mapping[str, object] | None = None,
    execution_plan_id: str | None = None,
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the authoritative lightweight v2 target-directory manifest."""

    if layout.version is not ExecutionLayoutVersion.V2:
        raise ExecutionLayoutError(
            f"{ExecutionLayoutError.code}: target manifests are required only for v2"
        )
    calculation = target.get("calculation")
    calculation_map = calculation if isinstance(calculation, Mapping) else {}
    fragments = target.get("fragments") or calculation_map.get("fragments")
    fragment_hash = (
        target.get("fragment_partition_hash")
        or calculation_map.get("fragment_partition_hash")
        or (stable_hash(fragments) if fragments is not None else None)
    )
    method_hash = (
        target.get("protocol_hash")
        or target.get("method_hash")
        or calculation_map.get("protocol_hash")
        or calculation_map.get("method_hash")
        or (stable_hash(dict(calculation_map)) if calculation_map else None)
    )
    return {
        "schema_version": 1,
        "layout_version": layout.version.value,
        "target_display_id": layout.display_target_identifier,
        "full_target_id": layout.target_identifier,
        "short_id_width": layout.short_id_width,
        "scientific_node_id": layout.workflow_node_identifier,
        "operation_stage": layout.operational_stage or layout.workflow_node_identifier,
        "operational_stage": layout.operational_stage,
        "system_id": layout.system_identifier,
        "source_artifact_ids": list(source_artifact_ids),
        "geometry_sha256": target.get("geometry_sha256"),
        "fragment_partition_hash": fragment_hash,
        "protocol_method_hash": method_hash,
        "execution_intent": dict(execution_intent or {}),
        "execution_plan_id": execution_plan_id,
        "target_creation_provenance": dict(provenance or {}),
    }


def write_target_manifest(path: Path, manifest: Mapping[str, object]) -> None:
    """Create or validate a v2 manifest without permitting identity aliasing."""

    target_directory = path.parent
    full_target_id = manifest.get("full_target_id")
    if not isinstance(full_target_id, str) or not full_target_id:
        raise ExecutionLayoutError(
            "TARGET_MANIFEST_CONFLICT: full target identity is required",
            code="TARGET_MANIFEST_CONFLICT",
        )
    assert_v2_target_available(
        target_directory, target_identifier=full_target_id
    )
    target_directory.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stored = json.loads(path.read_text(encoding="utf-8"))
        critical = (
            "layout_version",
            "target_display_id",
            "full_target_id",
            "short_id_width",
        )
        if any(stored.get(key) != manifest.get(key) for key in critical):
            raise ExecutionLayoutError(
                "TARGET_MANIFEST_CONFLICT: stored target manifest differs",
                code="TARGET_MANIFEST_CONFLICT",
            )
        return
    from .provenance import atomic_write_json

    atomic_write_json(path, dict(manifest))


def format_layout_status(layout: ExecutionLayout, *, verbose: bool = False) -> str:
    """Render an operator-oriented layout status without changing identity logic."""

    lines = [
        f"System: {layout.system_identifier}",
        f"Stage: {layout.workflow_node_identifier}",
        f"Target: {layout.display_target_identifier}",
        f"Attempt: {layout.attempt_identifier}",
    ]
    if verbose:
        lines.append(f"Full target ID: {layout.target_identifier}")
    return "\n".join(lines)


def resolve_recorded_layout(
    value: Mapping[str, Any], *, metadata_path: Path | str
) -> ExecutionLayout:
    """Resolve a native layout or a migrated immutable v1 record via its overlay."""

    layout = ExecutionLayout.from_mapping(value)
    if layout.version is ExecutionLayoutVersion.V2 or layout.working_directory.is_dir():
        return layout
    old_working = str(layout.working_directory)
    location = Path(metadata_path).expanduser().resolve()
    start = location if location.is_dir() else location.parent
    for ancestor in (start, *start.parents):
        registry_path = ancestor / ".cmw" / "layout-migrations" / "registry.json"
        if not registry_path.is_file():
            continue
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        paths = registry.get("paths", {})
        entry = paths.get(old_working) if isinstance(paths, Mapping) else None
        if not isinstance(entry, Mapping) or not entry.get("new_path"):
            break
        new_working = Path(str(entry["new_path"]))
        target_directory = new_working.parent.parent
        manifest_path = target_directory / TARGET_MANIFEST_FILENAME
        if not manifest_path.is_file():
            break
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("full_target_id") != layout.target_identifier:
            raise ExecutionLayoutError(
                "MIGRATION_REGISTRY_IDENTITY_CONFLICT: target identity differs",
                code="MIGRATION_REGISTRY_IDENTITY_CONFLICT",
            )
        operational_stage = manifest.get("operational_stage")
        scientific_leaf = (
            target_directory.parent.parent
            if operational_stage is not None
            else target_directory.parent
        )
        resolved = ExecutionLayout(
            project_root=scientific_leaf,
            system_identifier=layout.system_identifier,
            workflow_node_identifier=layout.workflow_node_identifier,
            target_identifier=layout.target_identifier,
            attempt_identifier=layout.attempt_identifier,
            version=ExecutionLayoutVersion.V2,
            operational_stage=(
                str(operational_stage) if operational_stage is not None else None
            ),
            short_id_width=int(
                manifest.get("short_id_width", DEFAULT_SHORT_TARGET_ID_WIDTH)
            ),
        )
        if resolved.working_directory != new_working:
            raise ExecutionLayoutError(
                "MIGRATION_REGISTRY_PATH_CONFLICT: overlay path differs from identities",
                code="MIGRATION_REGISTRY_PATH_CONFLICT",
            )
        return resolved
    return layout


def resolve_internal_path(
    stored_path: Path | str,
    *,
    layout_record: Mapping[str, Any],
    resolved_layout: ExecutionLayout,
) -> Path:
    """Resolve relative or migrated absolute attempt-owned metadata paths."""

    stored = Path(stored_path)
    if not stored.is_absolute():
        return resolved_layout.working_directory / stored
    original = ExecutionLayout.from_mapping(layout_record)
    if original.working_directory == resolved_layout.working_directory:
        return stored
    try:
        relative = stored.relative_to(original.working_directory)
    except ValueError:
        return stored
    return resolved_layout.working_directory / relative


__all__ = [
    "EXECUTION_LAYOUT_SCHEMA_VERSION",
    "LEGACY_EXECUTION_LAYOUT_SCHEMA_VERSION",
    "DEFAULT_SHORT_TARGET_ID_WIDTH",
    "TARGET_MANIFEST_FILENAME",
    "ExecutionLayout",
    "ExecutionLayoutError",
    "ExecutionLayoutVersion",
    "assert_v2_target_available",
    "build_target_manifest",
    "execution_target_directory",
    "execution_target_directory_v2",
    "format_layout_status",
    "next_attempt_identifier",
    "resolve_recorded_layout",
    "resolve_internal_path",
    "target_display_id",
    "write_target_manifest",
]
