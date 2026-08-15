"""Format-extensible construction and validation of structure artifacts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from cmw.structure.xyz import geometry_hash, read_xyz

from .artifacts import (
    ArtifactValidation,
    StructureArtifact,
    ValidationStatus,
)


@dataclass(frozen=True)
class StructureSummary:
    """Format-neutral structural metadata returned by a registered reader."""

    atom_count: int
    elemental_composition: Mapping[str, int]
    geometry_hash: str

    def __post_init__(self) -> None:
        if self.atom_count < 1:
            raise ValueError("structure summary atom_count must be positive")
        composition = {
            str(element): int(count)
            for element, count in self.elemental_composition.items()
        }
        if any(not element or count < 1 for element, count in composition.items()):
            raise ValueError("structure summary requires positive elemental counts")
        if sum(composition.values()) != self.atom_count:
            raise ValueError("structure summary composition does not match atom_count")
        if not self.geometry_hash:
            raise ValueError("structure summary requires a geometry hash")
        object.__setattr__(self, "elemental_composition", composition)


@dataclass(frozen=True)
class StructureFormatHandler:
    """Reader registration for one structure format and its common suffixes."""

    name: str
    suffixes: tuple[str, ...]
    reader: Callable[[Path], StructureSummary]

    def __post_init__(self) -> None:
        name = self.name.casefold().lstrip(".")
        if not name:
            raise ValueError("structure format name is required")
        suffixes = tuple(
            suffix.casefold() if suffix.startswith(".") else f".{suffix.casefold()}"
            for suffix in self.suffixes
        )
        if not suffixes or len(set(suffixes)) != len(suffixes):
            raise ValueError("structure format suffixes must be unique and non-empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "suffixes", suffixes)


class StructureValidationError(ValueError):
    """Raised when a structure cannot produce a validated artifact."""


_FORMAT_HANDLERS: dict[str, StructureFormatHandler] = {}


def register_structure_format(handler: StructureFormatHandler) -> None:
    """Register a format reader; duplicate names and suffixes fail closed."""

    if handler.name in _FORMAT_HANDLERS:
        raise ValueError(f"structure format is already registered: {handler.name}")
    claimed_suffixes = {
        suffix
        for registered in _FORMAT_HANDLERS.values()
        for suffix in registered.suffixes
    }
    conflicts = claimed_suffixes.intersection(handler.suffixes)
    if conflicts:
        raise ValueError(
            "structure suffix is already registered: " + ", ".join(sorted(conflicts))
        )
    _FORMAT_HANDLERS[handler.name] = handler


def registered_structure_formats() -> tuple[str, ...]:
    return tuple(_FORMAT_HANDLERS)


def _read_xyz_summary(path: Path) -> StructureSummary:
    geometry = read_xyz(path)
    return StructureSummary(
        geometry.atom_count,
        dict(Counter(geometry.elements)),
        geometry_hash(geometry),
    )


register_structure_format(
    StructureFormatHandler("xyz", (".xyz",), _read_xyz_summary)
)


def _resolve_format(path: Path, declared: str) -> StructureFormatHandler | None:
    normalized = declared.casefold().lstrip(".")
    if normalized:
        return _FORMAT_HANDLERS.get(normalized)
    suffix = path.suffix.casefold()
    return next(
        (
            handler
            for handler in _FORMAT_HANDLERS.values()
            if suffix in handler.suffixes
        ),
        None,
    )


def _structure_path(
    artifact: StructureArtifact, *, base_dir: str | Path | None
) -> Path | None:
    raw = artifact.files.get("structure")
    if raw is None and len(artifact.files) == 1:
        raw = next(iter(artifact.files.values()))
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir) / path
    return path


def validate_structure_artifact(
    artifact: StructureArtifact, *, base_dir: str | Path | None = None
) -> ArtifactValidation:
    """Validate file evidence and metadata without applying chemistry-specific rules."""

    path = _structure_path(artifact, base_dir=base_dir)
    checks: dict[str, bool | None] = {
        "source_available": bool(artifact.source),
        "structure_file_declared": path is not None,
        "file_exists": path.is_file() if path is not None else False,
        "format_available": bool(artifact.format),
        "format_recognized": False,
        "readable_geometry": None,
        "atom_count_available": artifact.atom_count is not None,
        "atom_count_match": None,
        "composition_available": bool(artifact.elemental_composition),
        "composition_match": None,
        "charge_available": artifact.charge is not None,
        "multiplicity_available": artifact.multiplicity is not None,
        "geometry_hash_generated": bool(artifact.geometry_hash),
        "geometry_hash_match": None,
    }
    if path is None or not path.is_file():
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            "MISSING_STRUCTURE_FILE",
            "structure file is missing or was not declared",
        )

    handler = _resolve_format(path, artifact.format)
    checks["format_recognized"] = handler is not None
    if handler is None:
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            "UNRECOGNIZED_STRUCTURE_FORMAT",
            f"no structure reader is registered for {artifact.format or path.suffix!r}",
        )

    try:
        summary = handler.reader(path)
    except (OSError, UnicodeError, ValueError) as exc:
        checks["readable_geometry"] = False
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            "UNREADABLE_STRUCTURE",
            f"structure geometry could not be read: {exc}",
        )

    checks.update(
        {
            "readable_geometry": True,
            "atom_count_match": artifact.atom_count == summary.atom_count,
            "composition_match": dict(artifact.elemental_composition)
            == dict(summary.elemental_composition),
            "geometry_hash_match": artifact.geometry_hash == summary.geometry_hash,
        }
    )
    required = (
        "source_available",
        "structure_file_declared",
        "file_exists",
        "format_available",
        "format_recognized",
        "readable_geometry",
        "atom_count_available",
        "atom_count_match",
        "composition_available",
        "composition_match",
        "charge_available",
        "multiplicity_available",
        "geometry_hash_generated",
        "geometry_hash_match",
    )
    if all(checks[item] is True for item in required):
        return ArtifactValidation(
            ValidationStatus.PASSED,
            checks,
            "VALID_STRUCTURE",
            "structure file and metadata are consistent",
        )
    missing = any(
        checks[item] is False
        for item in (
            "source_available",
            "format_available",
            "atom_count_available",
            "composition_available",
            "charge_available",
            "multiplicity_available",
            "geometry_hash_generated",
        )
    )
    return ArtifactValidation(
        ValidationStatus.FAILED,
        checks,
        "MISSING_STRUCTURE_METADATA" if missing else "INCONSISTENT_STRUCTURE_METADATA",
        "structure metadata is incomplete or inconsistent with the geometry file",
    )


def structure_artifact_from_file(
    path: str | Path,
    *,
    source: str,
    producing_calculation: str = "structure_import",
    structure_format: str | None = None,
    charge: int = 0,
    multiplicity: int = 1,
    provenance: Mapping[str, object] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> StructureArtifact:
    """Create a validated structure artifact through a registered format reader."""

    structure_path = Path(path)
    handler = _resolve_format(structure_path, structure_format or "")
    if handler is None:
        raise StructureValidationError(
            f"no structure reader is registered for {structure_format or structure_path.suffix!r}"
        )
    try:
        summary = handler.reader(structure_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise StructureValidationError(
            f"structure geometry could not be read: {exc}"
        ) from exc
    artifact = StructureArtifact(
        producing_calculation=producing_calculation,
        files={"structure": str(structure_path)},
        source=source,
        format=handler.name,
        atom_count=summary.atom_count,
        elemental_composition=summary.elemental_composition,
        charge=charge,
        multiplicity=multiplicity,
        geometry_hash=summary.geometry_hash,
        provenance=dict(provenance or {}),
        metadata=dict(metadata or {}),
    )
    validation = validate_structure_artifact(artifact)
    if not validation.passed:
        raise StructureValidationError(validation.reason)
    return replace(artifact, validation=validation)


__all__ = [
    "StructureFormatHandler",
    "StructureSummary",
    "StructureValidationError",
    "register_structure_format",
    "registered_structure_formats",
    "structure_artifact_from_file",
    "validate_structure_artifact",
]
