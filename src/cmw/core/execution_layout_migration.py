"""Transactional migration and version-aware resolution for execution layouts.

The filesystem path is not scientific identity.  Migration moves target trees
with same-filesystem renames, preserves v1 records byte-for-byte, and records an
overlay that maps immutable historical paths to their v2 locations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import Enum, IntEnum
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Callable, Mapping, Sequence

from .execution_layout import (
    DEFAULT_SHORT_TARGET_ID_WIDTH,
    ExecutionLayout,
    ExecutionLayoutError,
    ExecutionLayoutVersion,
    TARGET_MANIFEST_FILENAME,
    build_target_manifest,
    execution_target_directory_v2,
    write_target_manifest,
)
from .provenance import atomic_write_json, file_hash, read_json, stable_hash


MIGRATION_SCHEMA_VERSION = 1
MIGRATION_TOOL_VERSION = "cmw-execution-layout-migration/1"
SCIENTIFIC_HASH_SUFFIXES = {
    ".out",
    ".xyz",
    ".cube",
    ".cub",
    ".gbw",
    ".molden",
    ".input",
}
STREAMING_METADATA_THRESHOLD_BYTES = 32 * 1024 * 1024


class MigrationState(str, Enum):
    PLANNED = "PLANNED"
    STAGING = "STAGING"
    MOVING = "MOVING"
    METADATA_UPDATING = "METADATA_UPDATING"
    VALIDATING = "VALIDATING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    RESUME_PENDING = "RESUME_PENDING"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


class MigrationExitCode(IntEnum):
    SUCCESS = 0
    INVALID_ARGUMENT = 2
    NOTHING_TO_MIGRATE = 10
    BLOCKED = 11
    STALE_PLAN = 12
    TRANSACTION_FAILED = 15
    ROLLBACK_REQUIRED = 16


class LayoutMigrationError(RuntimeError):
    """Stable migration failure with a machine-readable code."""

    def __init__(
        self, code: str, message: str, exit_code: MigrationExitCode
    ) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(message)


class StaleMigrationPlan(LayoutMigrationError):
    def __init__(self, message: str = "campaign changed after migration planning") -> None:
        super().__init__("STALE_MIGRATION_PLAN", message, MigrationExitCode.STALE_PLAN)


@dataclass(frozen=True)
class FilesystemEntry:
    path: str
    entry_type: str
    size_bytes: int
    allocated_bytes: int
    device: int
    inode: int
    link_count: int
    link_target: str | None = None
    scientific_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TargetMigration:
    full_target_id: str
    target_display_id: str
    short_id_width: int
    source_path: str
    destination_path: str
    scientific_leaf: str
    operational_stage: str | None
    system_identifier: str
    workflow_node_identifier: str
    attempt_ids: tuple[str, ...]
    entries: tuple[FilesystemEntry, ...]
    logical_bytes: int
    physical_bytes: int
    symlink_count: int
    hardlink_entry_count: int
    target_manifest: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["attempt_ids"] = list(self.attempt_ids)
        value["entries"] = [item.to_dict() for item in self.entries]
        value["target_manifest"] = dict(self.target_manifest)
        return value


@dataclass(frozen=True)
class MutableReference:
    path: str
    sha256: str
    replacements: int
    update_mode: str = "json"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MigrationPlan:
    campaign_id: str
    campaign_root: str
    calculation_root: str
    source_layout_version: str
    destination_layout_version: str
    short_id_width: int
    scope_roots: tuple[str, ...]
    targets: tuple[TargetMigration, ...]
    excluded_targets: tuple[Mapping[str, object], ...]
    mutable_references: tuple[MutableReference, ...]
    immutable_records_left_untouched: tuple[str, ...]
    blockers: tuple[str, ...]
    tool_version: str = MIGRATION_TOOL_VERSION
    schema_version: int = MIGRATION_SCHEMA_VERSION
    plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", stable_hash(self.identity_payload()))

    @property
    def attempt_count(self) -> int:
        return sum(len(item.attempt_ids) for item in self.targets)

    @property
    def file_entry_count(self) -> int:
        return sum(len(item.entries) for item in self.targets)

    @property
    def symlink_count(self) -> int:
        return sum(item.symlink_count for item in self.targets)

    @property
    def logical_bytes(self) -> int:
        return sum(item.logical_bytes for item in self.targets)

    @property
    def physical_bytes(self) -> int:
        return sum(item.physical_bytes for item in self.targets)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "campaign_id": self.campaign_id,
            "campaign_root": self.campaign_root,
            "calculation_root": self.calculation_root,
            "source_layout_version": self.source_layout_version,
            "destination_layout_version": self.destination_layout_version,
            "short_id_width": self.short_id_width,
            "scope_roots": list(self.scope_roots),
            "targets": [item.to_dict() for item in self.targets],
            "excluded_targets": [dict(item) for item in self.excluded_targets],
            "mutable_references": [item.to_dict() for item in self.mutable_references],
            "immutable_records_left_untouched": list(
                self.immutable_records_left_untouched
            ),
            "blockers": list(self.blockers),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.identity_payload(),
            "target_count": len(self.targets),
            "attempt_count": self.attempt_count,
            "file_entry_count": self.file_entry_count,
            "symlink_count": self.symlink_count,
            "logical_bytes": self.logical_bytes,
            "physical_bytes": self.physical_bytes,
            "expected_moved_bytes": self.logical_bytes,
            "plan_sha256": self.plan_sha256,
        }


def _campaign_paths(value: Path | str) -> tuple[Path, Path]:
    selected = Path(value).expanduser().resolve(strict=True)
    if selected.is_symlink() or not selected.is_dir():
        raise LayoutMigrationError(
            "INVALID_CAMPAIGN", "campaign must be a real directory", MigrationExitCode.INVALID_ARGUMENT
        )
    if selected.name == "calculation":
        return selected.parent, selected
    calculation = selected / "calculation"
    if not calculation.is_dir():
        raise LayoutMigrationError(
            "INVALID_CAMPAIGN",
            "campaign has no calculation directory",
            MigrationExitCode.INVALID_ARGUMENT,
        )
    return selected, calculation


def _campaign_id(root: Path) -> str:
    for path in (
        root / ".cmw" / "campaign.json",
        root / "campaign.json",
        root / "campaign-state.json",
        root / "state.json",
    ):
        if path.is_file():
            try:
                value = read_json(path)
            except (OSError, UnicodeError, ValueError, TypeError):
                continue
            if value.get("campaign_id"):
                return str(value["campaign_id"])
    return "legacy-" + stable_hash({"campaign_root": str(root)})[:20]


def _scientific_hash(path: Path, mode: int) -> str | None:
    if stat.S_ISREG(mode) and path.suffix.casefold() in SCIENTIFIC_HASH_SUFFIXES:
        return file_hash(path)
    return None


def inventory_tree(root: Path) -> tuple[FilesystemEntry, ...]:
    """Inventory filesystem entries using lstat; symbolic links are never followed."""

    entries: list[FilesystemEntry] = []

    def visit(directory: Path, relative: Path) -> None:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            path = directory / child.name
            rel = relative / child.name
            information = child.stat(follow_symlinks=False)
            mode = information.st_mode
            if stat.S_ISLNK(mode):
                entry_type = "symlink"
                link_target = os.readlink(path)
            elif stat.S_ISDIR(mode):
                entry_type = "directory"
                link_target = None
            elif stat.S_ISREG(mode):
                entry_type = "file"
                link_target = None
            else:
                entry_type = "other"
                link_target = None
            entries.append(
                FilesystemEntry(
                    path=rel.as_posix(),
                    entry_type=entry_type,
                    size_bytes=information.st_size,
                    allocated_bytes=getattr(information, "st_blocks", 0) * 512,
                    device=information.st_dev,
                    inode=information.st_ino,
                    link_count=information.st_nlink,
                    link_target=link_target,
                    scientific_sha256=_scientific_hash(path, mode),
                )
            )
            if entry_type == "directory":
                visit(path, rel)

    visit(root, Path())
    return tuple(entries)


def _inventory_totals(
    entries: Sequence[FilesystemEntry],
) -> tuple[int, int, int, int]:
    logical = sum(item.size_bytes for item in entries if item.entry_type == "file")
    physical = 0
    counted: set[tuple[int, int]] = set()
    for item in entries:
        if item.entry_type != "file":
            continue
        identity = (item.device, item.inode)
        if identity not in counted:
            physical += item.allocated_bytes
            counted.add(identity)
    symlinks = sum(item.entry_type == "symlink" for item in entries)
    hardlinks = sum(
        item.entry_type == "file" and item.link_count > 1 for item in entries
    )
    return logical, physical, symlinks, hardlinks


def _target_payload(record: Mapping[str, object]) -> dict[str, object]:
    nested = record.get("target")
    return dict(nested) if isinstance(nested, Mapping) else dict(record)


def _v2_manifest(
    layout: ExecutionLayout,
    target_directory: Path,
    *,
    operational_stage: str | None,
) -> dict[str, object]:
    target_record: dict[str, object] = {}
    target_path = target_directory / "target.json"
    if target_path.is_file():
        target_record = read_json(target_path)
    target = _target_payload(target_record)
    source_ids = target.get("source_artifact_ids") or target_record.get("source_artifact_ids") or ()
    if not isinstance(source_ids, (list, tuple)):
        source_ids = ()
    intent = target.get("execution_intent")
    provenance = {
        "migration_source_layout": "v1",
        "migration_source_path": str(target_directory),
    }
    return build_target_manifest(
        ExecutionLayout(
            project_root=layout.project_root,
            system_identifier=layout.system_identifier,
            workflow_node_identifier=layout.workflow_node_identifier,
            target_identifier=layout.target_identifier,
            attempt_identifier=layout.attempt_identifier,
            version=ExecutionLayoutVersion.V2,
            operational_stage=operational_stage,
            short_id_width=layout.short_id_width,
        ),
        target=target,
        source_artifact_ids=tuple(str(item) for item in source_ids),
        execution_intent=dict(intent) if isinstance(intent, Mapping) else {},
        provenance=provenance,
    )


def _is_within_any(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _replacement_count(value: object, replacements: Mapping[str, str]) -> int:
    if isinstance(value, Mapping):
        return sum(_replacement_count(item, replacements) for item in value.values())
    if isinstance(value, list):
        return sum(_replacement_count(item, replacements) for item in value)
    if isinstance(value, str):
        return sum(1 for source in replacements if value == source or value.startswith(source + os.sep))
    return 0


def _mutable_references(
    campaign_root: Path,
    source_roots: Sequence[Path],
    replacements: Mapping[str, str],
) -> tuple[MutableReference, ...]:
    references: list[MutableReference] = []
    for path in sorted(campaign_root.rglob("*.json")):
        if path.is_symlink() or not path.is_file() or _is_within_any(path, source_roots):
            continue
        if ".cmw" in path.relative_to(campaign_root).parts:
            continue
        if ".failed_" in path.name or ".backup" in path.name:
            continue
        if path.stat().st_size > STREAMING_METADATA_THRESHOLD_BYTES:
            count = _stream_reference_count(path, replacements)
            if count:
                references.append(
                    MutableReference(
                        path=str(path),
                        sha256=file_hash(path),
                        replacements=count,
                        update_mode="stream_text",
                    )
                )
            continue
        try:
            value = read_json(path)
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        count = _replacement_count(value, replacements)
        if count:
            references.append(
                MutableReference(
                    path=str(path), sha256=file_hash(path), replacements=count
                )
            )
    return tuple(references)


def _stream_reference_count(path: Path, replacements: Mapping[str, str]) -> int:
    encoded = _encoded_replacements(replacements)
    pattern = _replacement_pattern(encoded)
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            count += sum(1 for _ in pattern.finditer(line))
    return count


def _encoded_replacements(replacements: Mapping[str, str]) -> dict[bytes, bytes]:
    encoded: dict[bytes, bytes] = {}
    for source, destination in replacements.items():
        encoded[source.encode("utf-8")] = destination.encode("utf-8")
        escaped_source = json.dumps(source, ensure_ascii=True)[1:-1].encode("ascii")
        escaped_destination = json.dumps(destination, ensure_ascii=True)[1:-1].encode("ascii")
        encoded[escaped_source] = escaped_destination
    return encoded


def _replacement_pattern(replacements: Mapping[bytes, bytes]) -> re.Pattern[bytes]:
    alternatives = sorted(
        (re.escape(source) for source in replacements),
        key=len,
        reverse=True,
    )
    if not alternatives:
        return re.compile(b"(?!)")
    return re.compile(b"|".join(alternatives))


def _nearest_existing(path: Path) -> Path:
    selected = path
    while not os.path.lexists(selected):
        if selected.parent == selected:
            raise LayoutMigrationError(
                "INVALID_DESTINATION", str(path), MigrationExitCode.BLOCKED
            )
        selected = selected.parent
    return selected


def build_migration_plan(
    campaign: Path | str,
    *,
    source_version: ExecutionLayoutVersion | str = ExecutionLayoutVersion.V1,
    destination_version: ExecutionLayoutVersion | str = ExecutionLayoutVersion.V2,
    short_id_width: int = DEFAULT_SHORT_TARGET_ID_WIDTH,
    include_roots: Sequence[Path | str] = (),
) -> MigrationPlan:
    """Build a deterministic read-only v1-to-v2 migration plan."""

    source_version = ExecutionLayoutVersion(source_version)
    destination_version = ExecutionLayoutVersion(destination_version)
    if (source_version, destination_version) != (
        ExecutionLayoutVersion.V1,
        ExecutionLayoutVersion.V2,
    ):
        raise LayoutMigrationError(
            "UNSUPPORTED_MIGRATION",
            "this release supports only v1 to v2",
            MigrationExitCode.INVALID_ARGUMENT,
        )
    campaign_root, calculation_root = _campaign_paths(campaign)
    scope_roots = tuple(
        sorted(Path(value).expanduser().resolve(strict=True) for value in include_roots)
    )
    layouts_by_target: dict[Path, list[tuple[Path, ExecutionLayout]]] = {}
    excluded_by_target: dict[Path, Mapping[str, object]] = {}
    all_v1_target_roots: set[Path] = set()
    for layout_path in sorted(calculation_root.rglob("execution-layout.json")):
        if layout_path.is_symlink() or not layout_path.is_file():
            continue
        raw = read_json(layout_path)
        if raw.get("schema_version") != 1:
            continue
        layout = ExecutionLayout.from_mapping(raw)
        target = layout_path.parents[2]
        all_v1_target_roots.add(target)
        try:
            target.relative_to(calculation_root)
        except ValueError as exc:
            raise LayoutMigrationError(
                "SOURCE_OUTSIDE_CAMPAIGN", str(target), MigrationExitCode.BLOCKED
            ) from exc
        in_scope = not scope_roots or any(
            layout.project_root == scope
            or layout.project_root.is_relative_to(scope)
            for scope in scope_roots
        )
        if not in_scope:
            excluded_by_target[target] = {
                "full_target_id": layout.target_identifier,
                "source_path": str(target),
                "disposition": "HISTORICAL_KEEP_V1",
                "reason": (
                    "scientific leaf is outside the authorized migration scope"
                    + (
                        "; stored v1 identities do not reproduce the manually constructed path"
                        if layout.working_directory != layout_path.parent
                        else ""
                    )
                ),
            }
            continue
        if layout.working_directory != layout_path.parent:
            raise LayoutMigrationError(
                "INVALID_V1_LAYOUT",
                f"layout path conflicts with identities: {layout_path}",
                MigrationExitCode.BLOCKED,
            )
        layouts_by_target.setdefault(target, []).append((layout_path, layout))

    blockers: list[str] = []
    targets: list[TargetMigration] = []
    destination_ids: dict[Path, str] = {}
    for source, records in sorted(
        layouts_by_target.items(), key=lambda item: str(item[0])
    ):
        layouts = [layout for _, layout in records]
        identity = layouts[0]
        if any(
            layout.target_identifier != identity.target_identifier
            or layout.project_root != identity.project_root
            or layout.workflow_node_identifier != identity.workflow_node_identifier
            for layout in layouts[1:]
        ):
            blockers.append(f"conflicting attempt layouts under {source}")
            continue
        operational_stage = (
            "orca_to_molden"
            if identity.workflow_node_identifier.casefold() == "orca_to_molden"
            else None
        )
        destination = execution_target_directory_v2(
            identity.project_root,
            target_identifier=identity.target_identifier,
            operational_stage=operational_stage,
            short_id_width=short_id_width,
        )
        previous = destination_ids.get(destination)
        if previous not in (None, identity.target_identifier):
            blockers.append(
                "SHORT_TARGET_DISPLAY_ID_COLLISION: "
                f"{destination} maps {previous} and {identity.target_identifier}"
            )
        destination_ids[destination] = identity.target_identifier
        if os.path.lexists(destination) and destination != source:
            stored = None
            manifest_path = destination / TARGET_MANIFEST_FILENAME
            if manifest_path.is_file():
                stored = read_json(manifest_path).get("full_target_id")
            if stored != identity.target_identifier:
                blockers.append(
                    "SHORT_TARGET_DISPLAY_ID_COLLISION: destination exists for "
                    f"another or unresolved target: {destination}"
                )
        source_device = os.lstat(source).st_dev
        destination_device = os.lstat(_nearest_existing(destination.parent)).st_dev
        if source_device != destination_device:
            blockers.append(f"CROSS_FILESYSTEM_MIGRATION_UNSUPPORTED: {source} -> {destination}")
        entries = inventory_tree(source)
        logical, physical, symlinks, hardlinks = _inventory_totals(entries)
        target_layout = ExecutionLayout(
            project_root=identity.project_root,
            system_identifier=identity.system_identifier,
            workflow_node_identifier=identity.workflow_node_identifier,
            target_identifier=identity.target_identifier,
            attempt_identifier=identity.attempt_identifier,
            version=ExecutionLayoutVersion.V2,
            operational_stage=operational_stage,
            short_id_width=short_id_width,
        )
        targets.append(
            TargetMigration(
                full_target_id=identity.target_identifier,
                target_display_id=target_layout.display_target_identifier,
                short_id_width=short_id_width,
                source_path=str(source),
                destination_path=str(destination),
                scientific_leaf=str(identity.project_root),
                operational_stage=operational_stage,
                system_identifier=identity.system_identifier,
                workflow_node_identifier=identity.workflow_node_identifier,
                attempt_ids=tuple(
                    sorted({layout.attempt_identifier for layout in layouts})
                ),
                entries=entries,
                logical_bytes=logical,
                physical_bytes=physical,
                symlink_count=symlinks,
                hardlink_entry_count=hardlinks,
                target_manifest=_v2_manifest(
                    target_layout, source, operational_stage=operational_stage
                ),
            )
        )
    # A nested operational target is moved first and is not part of the parent
    # payload that subsequently moves.  Remove those entries from the parent's
    # expected inventory before hashing the plan.
    adjusted_targets: list[TargetMigration] = []
    for target in targets:
        source = Path(target.source_path)
        descendant_prefixes: list[str] = []
        for other in targets:
            if other is target:
                continue
            try:
                relative = Path(other.source_path).relative_to(source).as_posix()
            except ValueError:
                continue
            descendant_prefixes.append(relative)
        entries = tuple(
            entry
            for entry in target.entries
            if not any(
                entry.path == prefix or entry.path.startswith(prefix + "/")
                for prefix in descendant_prefixes
            )
        )
        logical, physical, symlinks, hardlinks = _inventory_totals(entries)
        adjusted_targets.append(
            replace(
                target,
                entries=entries,
                logical_bytes=logical,
                physical_bytes=physical,
                symlink_count=symlinks,
                hardlink_entry_count=hardlinks,
            )
        )
    targets = adjusted_targets
    # Child targets (notably ORCA-to-Molden) must leave their v1 parent first.
    targets.sort(key=lambda item: (-len(Path(item.source_path).parts), item.source_path))
    replacements: dict[str, str] = {}
    for item in targets:
        replacements[item.source_path] = item.destination_path
    mutable = _mutable_references(
        campaign_root,
        tuple(sorted(all_v1_target_roots)),
        replacements,
    )
    immutable_records = {
        str(Path(target.source_path) / entry.path)
        for target in targets
        for entry in target.entries
        if entry.entry_type == "file" and entry.path.endswith(".json")
    }
    immutable_records.update(
        str(path)
        for path in campaign_root.rglob("*.json")
        if path.is_file() and not path.is_symlink()
        and (".failed_" in path.name or ".backup" in path.name)
    )
    return MigrationPlan(
        campaign_id=_campaign_id(campaign_root),
        campaign_root=str(campaign_root),
        calculation_root=str(calculation_root),
        source_layout_version=source_version.value,
        destination_layout_version=destination_version.value,
        short_id_width=short_id_width,
        scope_roots=tuple(str(path) for path in scope_roots),
        targets=tuple(targets),
        excluded_targets=tuple(
            excluded_by_target[path] for path in sorted(excluded_by_target)
        ),
        mutable_references=mutable,
        immutable_records_left_untouched=tuple(sorted(immutable_records)),
        blockers=tuple(sorted(set(blockers))),
    )


def migration_registry_path(campaign_root: Path | str) -> Path:
    return Path(campaign_root).resolve() / ".cmw" / "layout-migrations" / "registry.json"


def load_migration_registry(campaign_root: Path | str) -> dict[str, Any]:
    path = migration_registry_path(campaign_root)
    if not path.is_file():
        return {"schema_version": MIGRATION_SCHEMA_VERSION, "migrations": [], "paths": {}}
    value = read_json(path)
    if value.get("schema_version") != MIGRATION_SCHEMA_VERSION or not isinstance(value.get("paths"), Mapping):
        raise LayoutMigrationError(
            "INVALID_MIGRATION_REGISTRY",
            str(path),
            MigrationExitCode.BLOCKED,
        )
    return value


def _registry_overlay(campaign_root: Path, old_path: Path) -> Path | None:
    registry = load_migration_registry(campaign_root)
    value = registry.get("paths", {}).get(str(old_path))
    if isinstance(value, Mapping) and value.get("new_path"):
        return Path(str(value["new_path"]))
    return None


def resolve_target(
    campaign_root: Path | str,
    *,
    full_target_id: str,
    scientific_leaf: Path | str | None = None,
    operational_stage: str | None = None,
    short_id_width: int = DEFAULT_SHORT_TARGET_ID_WIDTH,
    legacy_path: Path | str | None = None,
) -> Path:
    """Resolve v2, native v1, or registry-migrated targets by full identity."""

    root = Path(campaign_root).resolve()
    if legacy_path is not None:
        legacy = Path(legacy_path)
        overlaid = _registry_overlay(root, legacy)
        if overlaid is not None:
            return overlaid
        if legacy.is_dir():
            return legacy.resolve()
    if scientific_leaf is not None:
        candidate = execution_target_directory_v2(
            scientific_leaf,
            target_identifier=full_target_id,
            operational_stage=operational_stage,
            short_id_width=short_id_width,
        )
        manifest = candidate / TARGET_MANIFEST_FILENAME
        if manifest.is_file() and read_json(manifest).get("full_target_id") == full_target_id:
            return candidate
    for manifest in root.rglob(TARGET_MANIFEST_FILENAME):
        if manifest.is_file() and read_json(manifest).get("full_target_id") == full_target_id:
            return manifest.parent
    for target_path in root.rglob("target.json"):
        target = _target_payload(read_json(target_path))
        if target.get("target_id") == full_target_id:
            return target_path.parent
    raise LayoutMigrationError(
        "TARGET_NOT_FOUND", full_target_id, MigrationExitCode.INVALID_ARGUMENT
    )


def resolve_attempt(
    campaign_root: Path | str,
    *,
    full_target_id: str,
    attempt_id: str,
    **target_arguments: object,
) -> Path:
    target = resolve_target(
        campaign_root, full_target_id=full_target_id, **target_arguments
    )
    attempt = target / "attempts" / attempt_id
    if not attempt.is_dir():
        raise LayoutMigrationError(
            "ATTEMPT_NOT_FOUND", str(attempt), MigrationExitCode.INVALID_ARGUMENT
        )
    return attempt


def _replace_paths(value: object, replacements: Mapping[str, str]) -> object:
    if isinstance(value, Mapping):
        return {key: _replace_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_paths(item, replacements) for item in value]
    if isinstance(value, str):
        for source in sorted(replacements, key=len, reverse=True):
            if value == source or value.startswith(source + os.sep):
                return replacements[source] + value[len(source) :]
    return value


def _journal_path(root: Path, transaction_id: str) -> Path:
    return root / ".cmw" / "layout-migrations" / "transactions" / f"{transaction_id}.json"


def _write_journal(
    path: Path,
    *,
    state: MigrationState,
    plan: MigrationPlan,
    moved: Sequence[str],
    error: str | None = None,
) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "transaction_id": plan.plan_sha256[:20],
            "state": state.value,
            "plan_sha256": plan.plan_sha256,
            "campaign_root": plan.campaign_root,
            "moved_target_ids": list(moved),
            "error": error,
            "plan": plan.to_dict(),
        },
    )


def _backup_metadata(
    plan: MigrationPlan, evidence_directory: Path, transaction_id: str
) -> None:
    root = Path(plan.campaign_root)
    backup_root = evidence_directory / "metadata-backup" / transaction_id
    for reference in plan.mutable_references:
        source = Path(reference.path)
        relative = source.relative_to(root)
        destination = backup_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)


def _replacement_map(plan: MigrationPlan) -> dict[str, str]:
    return {item.source_path: item.destination_path for item in plan.targets}


def _update_mutable_metadata(plan: MigrationPlan) -> None:
    replacements = _replacement_map(plan)
    for reference in plan.mutable_references:
        path = Path(reference.path)
        if file_hash(path) != reference.sha256:
            raise StaleMigrationPlan(f"mutable metadata changed: {path}")
        if reference.update_mode == "stream_text":
            _stream_replace_paths(path, replacements)
        else:
            atomic_write_json(path, _replace_paths(read_json(path), replacements))


def _stream_replace_paths(path: Path, replacements: Mapping[str, str]) -> None:
    """Atomically replace literal JSON path strings without loading a large record."""

    encoded = _encoded_replacements(replacements)
    pattern = _replacement_pattern(encoded)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".migration.tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with path.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
            for line in source:
                updated = pattern.sub(lambda match: encoded[match.group(0)], line)
                destination.write(updated)
            destination.flush()
            os.fsync(destination.fileno())
        shutil.copystat(path, temporary, follow_symlinks=False)
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _registry_record(plan: MigrationPlan, transaction_id: str) -> dict[str, Any]:
    path = migration_registry_path(plan.campaign_root)
    registry = load_migration_registry(plan.campaign_root)
    paths = dict(registry.get("paths", {}))
    for target in plan.targets:
        paths[target.source_path] = {
            "new_path": target.destination_path,
            "full_target_id": target.full_target_id,
            "attempt_id": None,
            "transaction_id": transaction_id,
            "from_layout": "v1",
            "to_layout": "v2",
            "plan_sha256": plan.plan_sha256,
            "state": MigrationState.COMMITTED.value,
        }
        for attempt_id in target.attempt_ids:
            old_attempt = str(Path(target.source_path) / "attempts" / attempt_id)
            new_attempt = str(Path(target.destination_path) / "attempts" / attempt_id)
            paths[old_attempt] = {
                "new_path": new_attempt,
                "full_target_id": target.full_target_id,
                "attempt_id": attempt_id,
                "transaction_id": transaction_id,
                "from_layout": "v1",
                "to_layout": "v2",
                "plan_sha256": plan.plan_sha256,
                "state": MigrationState.COMMITTED.value,
            }
    migrations = list(registry.get("migrations", []))
    migrations.append(
        {
            "transaction_id": transaction_id,
            "plan_sha256": plan.plan_sha256,
            "state": MigrationState.COMMITTED.value,
            "target_count": len(plan.targets),
        }
    )
    return {"schema_version": MIGRATION_SCHEMA_VERSION, "migrations": migrations, "paths": paths}


def _validate_destination(target: TargetMigration) -> None:
    destination = Path(target.destination_path)
    if not destination.is_dir() or Path(target.source_path).exists():
        raise LayoutMigrationError(
            "MIGRATION_VALIDATION_FAILED",
            f"ambiguous source/destination for {target.full_target_id}",
            MigrationExitCode.TRANSACTION_FAILED,
        )
    after = tuple(
        item for item in inventory_tree(destination) if item.path != TARGET_MANIFEST_FILENAME
    )
    if after != target.entries:
        raise LayoutMigrationError(
            "MIGRATION_VALIDATION_FAILED",
            f"filesystem inventory changed for {target.full_target_id}",
            MigrationExitCode.TRANSACTION_FAILED,
        )
    manifest = read_json(destination / TARGET_MANIFEST_FILENAME)
    if manifest.get("full_target_id") != target.full_target_id:
        raise LayoutMigrationError(
            "MIGRATION_VALIDATION_FAILED",
            f"target manifest identity changed for {target.full_target_id}",
            MigrationExitCode.TRANSACTION_FAILED,
        )


def _prune_empty_legacy_parents(plan: MigrationPlan) -> None:
    """Remove only empty v1 display scaffolding below each scientific leaf."""

    for target in plan.targets:
        scientific_leaf = Path(target.scientific_leaf)
        current = Path(target.source_path).parent
        while current != scientific_leaf:
            try:
                current.relative_to(scientific_leaf)
            except ValueError as exc:
                raise LayoutMigrationError(
                    "INVALID_SOURCE_HIERARCHY",
                    f"legacy source is outside its scientific leaf: {current}",
                    MigrationExitCode.TRANSACTION_FAILED,
                ) from exc
            try:
                current.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                break
            current = current.parent


FaultHook = Callable[[MigrationState, str | None], None]


def apply_migration_plan(
    plan: MigrationPlan,
    *,
    confirm_plan: str,
    evidence_directory: Path | str,
    _fault_hook: FaultHook | None = None,
) -> Mapping[str, object]:
    """Apply a fresh plan through a journaled same-filesystem rename transaction."""

    if confirm_plan != plan.plan_sha256:
        raise StaleMigrationPlan("--confirm-plan does not match the migration plan")
    if plan.blockers:
        raise LayoutMigrationError(
            "MIGRATION_BLOCKED", "; ".join(plan.blockers), MigrationExitCode.BLOCKED
        )
    fresh = build_migration_plan(
        plan.campaign_root,
        short_id_width=plan.short_id_width,
        include_roots=plan.scope_roots,
    )
    if fresh.plan_sha256 != plan.plan_sha256:
        raise StaleMigrationPlan()
    root = Path(plan.campaign_root)
    evidence = Path(evidence_directory).expanduser().resolve()
    try:
        evidence.relative_to(root)
    except ValueError:
        pass
    else:
        raise LayoutMigrationError(
            "INVALID_EVIDENCE_DIRECTORY",
            "migration evidence must be outside the campaign",
            MigrationExitCode.INVALID_ARGUMENT,
        )
    evidence.mkdir(parents=True, exist_ok=True)
    transaction_id = plan.plan_sha256[:20]
    journal = _journal_path(root, transaction_id)
    moved: list[str] = []

    def transition(state: MigrationState, target_id: str | None = None) -> None:
        _write_journal(journal, state=state, plan=plan, moved=moved)
        if _fault_hook is not None:
            _fault_hook(state, target_id)

    try:
        transition(MigrationState.PLANNED)
        _backup_metadata(plan, evidence, transaction_id)
        transition(MigrationState.STAGING)
        for target in plan.targets:
            Path(target.destination_path).parent.mkdir(parents=True, exist_ok=True)
        transition(MigrationState.MOVING)
        for target in plan.targets:
            source = Path(target.source_path)
            destination = Path(target.destination_path)
            if source.is_dir() and not os.path.lexists(destination):
                os.replace(source, destination)
            elif not source.exists() and destination.is_dir():
                pass
            else:
                raise LayoutMigrationError(
                    "AMBIGUOUS_MOVE_STATE",
                    f"cannot resume {source} -> {destination}",
                    MigrationExitCode.ROLLBACK_REQUIRED,
                )
            moved.append(target.full_target_id)
            _write_journal(journal, state=MigrationState.MOVING, plan=plan, moved=moved)
            if _fault_hook is not None:
                _fault_hook(MigrationState.MOVING, target.full_target_id)
        transition(MigrationState.METADATA_UPDATING)
        for target in plan.targets:
            write_target_manifest(
                Path(target.destination_path) / TARGET_MANIFEST_FILENAME,
                target.target_manifest,
            )
        _update_mutable_metadata(plan)
        transition(MigrationState.VALIDATING)
        for target in plan.targets:
            _validate_destination(target)
        _prune_empty_legacy_parents(plan)
        atomic_write_json(
            migration_registry_path(root), _registry_record(plan, transaction_id)
        )
        transition(MigrationState.COMMITTED)
    except Exception as exc:
        _write_journal(
            journal,
            state=MigrationState.RESUME_PENDING,
            plan=plan,
            moved=moved,
            error=str(exc),
        )
        if isinstance(exc, LayoutMigrationError):
            raise
        raise LayoutMigrationError(
            "MIGRATION_INTERRUPTED", str(exc), MigrationExitCode.ROLLBACK_REQUIRED
        ) from exc
    return {
        "status": MigrationState.COMMITTED.value,
        "transaction_id": transaction_id,
        "plan_sha256": plan.plan_sha256,
        "target_count": len(plan.targets),
        "attempt_count": plan.attempt_count,
        "registry_path": str(migration_registry_path(root)),
        "journal_path": str(journal),
    }


def rollback_migration(
    campaign: Path | str,
    *,
    transaction_id: str,
    evidence_directory: Path | str,
) -> Mapping[str, object]:
    root, _ = _campaign_paths(campaign)
    journal_path = _journal_path(root, transaction_id)
    record = read_json(journal_path)
    plan = migration_plan_from_mapping(dict(record["plan"]))
    _write_journal(journal_path, state=MigrationState.ROLLING_BACK, plan=plan, moved=())
    for target in reversed(plan.targets):
        source = Path(target.source_path)
        destination = Path(target.destination_path)
        if destination.is_dir() and not os.path.lexists(source):
            manifest = destination / TARGET_MANIFEST_FILENAME
            if manifest.is_file():
                manifest.unlink()
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
        elif source.is_dir() and not destination.exists():
            continue
        else:
            raise LayoutMigrationError(
                "AMBIGUOUS_ROLLBACK_STATE",
                f"cannot rollback {destination} -> {source}",
                MigrationExitCode.ROLLBACK_REQUIRED,
            )
    backup_root = Path(evidence_directory).resolve() / "metadata-backup" / transaction_id
    for reference in plan.mutable_references:
        destination = Path(reference.path)
        backup = backup_root / destination.relative_to(root)
        if backup.is_file():
            shutil.copy2(backup, destination, follow_symlinks=False)
    registry = load_migration_registry(root)
    if migration_registry_path(root).is_file():
        paths = {
            key: value
            for key, value in registry.get("paths", {}).items()
            if not isinstance(value, Mapping) or value.get("transaction_id") != transaction_id
        }
        migrations = [
            item
            for item in registry.get("migrations", [])
            if not isinstance(item, Mapping) or item.get("transaction_id") != transaction_id
        ]
        atomic_write_json(
            migration_registry_path(root),
            {"schema_version": MIGRATION_SCHEMA_VERSION, "migrations": migrations, "paths": paths},
        )
    _write_journal(journal_path, state=MigrationState.ROLLED_BACK, plan=plan, moved=())
    return {"status": MigrationState.ROLLED_BACK.value, "transaction_id": transaction_id}


def audit_migration(
    campaign: Path | str, *, transaction_id: str
) -> Mapping[str, object]:
    """Revalidate a committed transaction without mutating campaign state."""

    root, _ = _campaign_paths(campaign)
    journal_path = _journal_path(root, transaction_id)
    record = read_json(journal_path)
    if MigrationState(str(record.get("state"))) is not MigrationState.COMMITTED:
        raise LayoutMigrationError(
            "TRANSACTION_NOT_COMMITTED",
            str(record.get("state")),
            MigrationExitCode.BLOCKED,
        )
    plan = migration_plan_from_mapping(dict(record["plan"]))
    for target in plan.targets:
        _validate_destination(target)
    registry = load_migration_registry(root)
    paths = registry.get("paths", {})
    expected_registry_entries = len(plan.targets) + plan.attempt_count
    matching_entries = sum(
        isinstance(item, Mapping) and item.get("transaction_id") == transaction_id
        for item in paths.values()
    )
    if matching_entries != expected_registry_entries:
        raise LayoutMigrationError(
            "MIGRATION_VALIDATION_FAILED",
            "migration registry is incomplete",
            MigrationExitCode.BLOCKED,
        )
    replacements = _replacement_map(plan)
    stale_mutable: list[str] = []
    for reference in plan.mutable_references:
        path = Path(reference.path)
        if reference.update_mode == "stream_text":
            count = _stream_reference_count(path, replacements)
        else:
            count = _replacement_count(read_json(path), replacements)
        if count:
            stale_mutable.append(str(path))
    if stale_mutable:
        raise LayoutMigrationError(
            "MIGRATION_VALIDATION_FAILED",
            "stale mutable paths remain: " + ", ".join(stale_mutable),
            MigrationExitCode.BLOCKED,
        )
    return {
        "status": "VALIDATED",
        "transaction_id": transaction_id,
        "plan_sha256": plan.plan_sha256,
        "target_count": len(plan.targets),
        "attempt_count": plan.attempt_count,
        "file_entry_count": plan.file_entry_count,
        "symlink_count": plan.symlink_count,
        "logical_bytes": plan.logical_bytes,
        "physical_bytes": plan.physical_bytes,
        "scientific_hash_count": sum(
            entry.scientific_sha256 is not None
            for target in plan.targets
            for entry in target.entries
        ),
        "registry_path": str(migration_registry_path(root)),
        "registry_entry_count": matching_entries,
        "stale_mutable_reference_count": 0,
        "historical_v1_target_count": len(plan.excluded_targets),
    }


def migration_plan_from_mapping(value: Mapping[str, Any]) -> MigrationPlan:
    targets = tuple(
        TargetMigration(
            full_target_id=str(item["full_target_id"]),
            target_display_id=str(item["target_display_id"]),
            short_id_width=int(item["short_id_width"]),
            source_path=str(item["source_path"]),
            destination_path=str(item["destination_path"]),
            scientific_leaf=str(item["scientific_leaf"]),
            operational_stage=(str(item["operational_stage"]) if item.get("operational_stage") is not None else None),
            system_identifier=str(item["system_identifier"]),
            workflow_node_identifier=str(item["workflow_node_identifier"]),
            attempt_ids=tuple(str(value) for value in item.get("attempt_ids", ())),
            entries=tuple(FilesystemEntry(**entry) for entry in item.get("entries", ())),
            logical_bytes=int(item["logical_bytes"]),
            physical_bytes=int(item["physical_bytes"]),
            symlink_count=int(item["symlink_count"]),
            hardlink_entry_count=int(item["hardlink_entry_count"]),
            target_manifest=dict(item["target_manifest"]),
        )
        for item in value.get("targets", ())
    )
    plan = MigrationPlan(
        campaign_id=str(value["campaign_id"]),
        campaign_root=str(value["campaign_root"]),
        calculation_root=str(value["calculation_root"]),
        source_layout_version=str(value["source_layout_version"]),
        destination_layout_version=str(value["destination_layout_version"]),
        short_id_width=int(value["short_id_width"]),
        scope_roots=tuple(str(item) for item in value.get("scope_roots", ())),
        targets=targets,
        excluded_targets=tuple(
            dict(item) for item in value.get("excluded_targets", ())
        ),
        mutable_references=tuple(
            MutableReference(**item) for item in value.get("mutable_references", ())
        ),
        immutable_records_left_untouched=tuple(
            str(item) for item in value.get("immutable_records_left_untouched", ())
        ),
        blockers=tuple(str(item) for item in value.get("blockers", ())),
    )
    stored = value.get("plan_sha256")
    if stored not in (None, plan.plan_sha256):
        raise StaleMigrationPlan("stored migration plan hash is invalid")
    return plan


def resume_migration(
    campaign: Path | str,
    *,
    transaction_id: str,
    evidence_directory: Path | str,
    _fault_hook: FaultHook | None = None,
) -> Mapping[str, object]:
    root, _ = _campaign_paths(campaign)
    record = read_json(_journal_path(root, transaction_id))
    plan = migration_plan_from_mapping(dict(record["plan"]))
    state = MigrationState(str(record["state"]))
    if state is MigrationState.COMMITTED:
        return {
            "status": state.value,
            "transaction_id": transaction_id,
            "plan_sha256": plan.plan_sha256,
        }
    if state not in {MigrationState.RESUME_PENDING, MigrationState.FAILED}:
        raise LayoutMigrationError(
            "INVALID_TRANSACTION_STATE", state.value, MigrationExitCode.BLOCKED
        )
    # Reconstructing a fresh v1 plan is impossible after the first rename;
    # the journaled plan is authoritative and each move is checked explicitly.
    return _resume_journaled_plan(
        plan,
        evidence_directory=Path(evidence_directory),
        fault_hook=_fault_hook,
    )


def _resume_journaled_plan(
    plan: MigrationPlan,
    *,
    evidence_directory: Path,
    fault_hook: FaultHook | None,
) -> Mapping[str, object]:
    # Verify every target is in exactly one of the source or destination states.
    for target in plan.targets:
        source_exists = Path(target.source_path).is_dir()
        destination_exists = Path(target.destination_path).is_dir()
        if source_exists == destination_exists:
            raise LayoutMigrationError(
                "AMBIGUOUS_MOVE_STATE",
                target.full_target_id,
                MigrationExitCode.ROLLBACK_REQUIRED,
            )
    root = Path(plan.campaign_root)
    transaction_id = plan.plan_sha256[:20]
    journal = _journal_path(root, transaction_id)
    moved: list[str] = []
    try:
        for target in plan.targets:
            source = Path(target.source_path)
            destination = Path(target.destination_path)
            if source.is_dir():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
            moved.append(target.full_target_id)
            _write_journal(journal, state=MigrationState.MOVING, plan=plan, moved=moved)
            if fault_hook:
                fault_hook(MigrationState.MOVING, target.full_target_id)
        _write_journal(journal, state=MigrationState.METADATA_UPDATING, plan=plan, moved=moved)
        for target in plan.targets:
            write_target_manifest(Path(target.destination_path) / TARGET_MANIFEST_FILENAME, target.target_manifest)
        # Metadata may already be updated.  Apply only while its pre-image hash matches.
        for reference in plan.mutable_references:
            path = Path(reference.path)
            if file_hash(path) == reference.sha256:
                if reference.update_mode == "stream_text":
                    _stream_replace_paths(path, _replacement_map(plan))
                else:
                    atomic_write_json(path, _replace_paths(read_json(path), _replacement_map(plan)))
        _write_journal(journal, state=MigrationState.VALIDATING, plan=plan, moved=moved)
        for target in plan.targets:
            _validate_destination(target)
        _prune_empty_legacy_parents(plan)
        atomic_write_json(migration_registry_path(root), _registry_record(plan, transaction_id))
        _write_journal(journal, state=MigrationState.COMMITTED, plan=plan, moved=moved)
    except Exception as exc:
        _write_journal(journal, state=MigrationState.RESUME_PENDING, plan=plan, moved=moved, error=str(exc))
        if isinstance(exc, LayoutMigrationError):
            raise
        raise LayoutMigrationError("MIGRATION_INTERRUPTED", str(exc), MigrationExitCode.ROLLBACK_REQUIRED) from exc
    return {
        "status": MigrationState.COMMITTED.value,
        "transaction_id": transaction_id,
        "plan_sha256": plan.plan_sha256,
        "registry_path": str(migration_registry_path(root)),
    }


def format_migration_plan(plan: MigrationPlan) -> str:
    lines = [
        f"Campaign: {plan.campaign_id}",
        f"Layout: {plan.source_layout_version} -> {plan.destination_layout_version}",
        f"Targets: {len(plan.targets)}; attempts: {plan.attempt_count}",
        f"Historical v1 targets excluded: {len(plan.excluded_targets)}",
        f"Entries: {plan.file_entry_count}; symlinks: {plan.symlink_count}",
        f"Logical bytes: {plan.logical_bytes}; physical bytes: {plan.physical_bytes}",
        f"Plan SHA-256: {plan.plan_sha256}",
    ]
    if plan.blockers:
        lines.append("Blockers: " + "; ".join(plan.blockers))
    return "\n".join(lines)


__all__ = [
    "FilesystemEntry",
    "LayoutMigrationError",
    "MigrationExitCode",
    "MigrationPlan",
    "MigrationState",
    "MutableReference",
    "StaleMigrationPlan",
    "TargetMigration",
    "apply_migration_plan",
    "audit_migration",
    "build_migration_plan",
    "format_migration_plan",
    "inventory_tree",
    "load_migration_registry",
    "migration_plan_from_mapping",
    "migration_registry_path",
    "resolve_attempt",
    "resolve_target",
    "resume_migration",
    "rollback_migration",
]
