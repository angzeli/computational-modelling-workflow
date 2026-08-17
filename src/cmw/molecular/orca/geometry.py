"""Validated StructureArtifact-to-ORCA geometry input contracts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from cmw.core.artifacts import StructureArtifact, artifact_from_dict
from cmw.core.structure_artifacts import (
    structure_artifact_path,
    validate_structure_artifact,
)
from cmw.structure.xyz import Atom, XYZGeometry, geometry_hash, read_xyz


ORCA_GEOMETRY_SCHEMA_VERSION = 1
ORCA_ATOM_LABEL = re.compile(r"([A-Z][a-z]?)(?:\([1-9]\d*\))?:?\Z")


class OrcaGeometryMode(str, Enum):
    """Explicitly distinguish the artifact contract from legacy rendering modes."""

    XYZFILE = "xyzfile"
    LEGACY_XYZFILE = "legacy_xyzfile"
    LEGACY_INLINE = "legacy_inline"


class OrcaGeometryContractError(ValueError):
    """Raised when ORCA geometry evidence contradicts its StructureArtifact."""

    code = "FAILED_GEOMETRY_CONTRACT"


def _fail(reason: str) -> OrcaGeometryContractError:
    return OrcaGeometryContractError(f"{OrcaGeometryContractError.code}: {reason}")


def _normalized_orca_element(label: str) -> str:
    match = ORCA_ATOM_LABEL.fullmatch(label)
    if match is None:
        raise _fail(f"unsupported ORCA XYZ atom label: {label!r}")
    return match.group(1)


def _parse_orca_xyzfile(path: Path) -> tuple[XYZGeometry, tuple[str, ...]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise _fail(f"ORCA geometry file is unreadable: {path}") from exc
    if len(lines) < 2:
        raise _fail("ORCA XYZ file lacks atom-count and comment rows")
    try:
        atom_count = int(lines[0].strip())
    except ValueError as exc:
        raise _fail("ORCA XYZ atom count is invalid") from exc
    if atom_count < 1 or len(lines) != atom_count + 2:
        raise _fail("ORCA XYZ atom count does not match its coordinate rows")
    atoms: list[Atom] = []
    labels: list[str] = []
    for line_number, line in enumerate(lines[2:], start=3):
        fields = line.split()
        if len(fields) != 4:
            raise _fail(
                f"ORCA XYZ row {line_number} must contain a label and 3 coordinates"
            )
        label = fields[0]
        try:
            atom = Atom(
                _normalized_orca_element(label),
                *(float(value.replace("D", "E").replace("d", "e")) for value in fields[1:]),
            )
        except ValueError as exc:
            raise _fail(f"invalid ORCA XYZ row {line_number}: {exc}") from exc
        labels.append(label)
        atoms.append(atom)
    return XYZGeometry(tuple(atoms), lines[1]), tuple(labels)


def read_orca_xyzfile_geometry(path: Path) -> XYZGeometry:
    """Read an ORCA XYZ file while normalizing ghost and fragment labels."""

    return _parse_orca_xyzfile(path)[0]


def _write_orca_xyzfile(
    path: Path,
    geometry: XYZGeometry,
    labels: Sequence[str],
    *,
    comment: str,
) -> None:
    if len(labels) != geometry.atom_count:
        raise _fail("ORCA atom labels do not match the selected geometry")
    rows = [str(geometry.atom_count), comment]
    rows.extend(
        f"{label:<8s} {atom.x: .17g} {atom.y: .17g} {atom.z: .17g}"
        for label, atom in zip(labels, geometry.atoms, strict=True)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class OrcaGeometryInput:
    """One materialized ORCA geometry derived from a validated structure artifact."""

    source_artifact: StructureArtifact
    source_path: str
    input_geometry_file: str
    atom_indices: tuple[int, ...]
    atom_labels: tuple[str, ...]
    atom_count: int
    geometry_hash: str
    charge: int
    multiplicity: int
    electronic_state_override: bool = False
    mode: OrcaGeometryMode | str = OrcaGeometryMode.XYZFILE
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_artifact, StructureArtifact):
            raise _fail("ORCA geometry source must be a StructureArtifact")
        mode = OrcaGeometryMode(self.mode)
        if mode is not OrcaGeometryMode.XYZFILE:
            raise _fail("artifact-backed geometry inputs must use xyzfile mode")
        source_path = Path(self.source_path)
        input_path = Path(self.input_geometry_file)
        if not source_path.is_absolute() or not input_path.is_absolute():
            raise _fail("source and input geometry paths must be resolved absolute paths")
        indices = tuple(int(index) for index in self.atom_indices)
        labels = tuple(str(label) for label in self.atom_labels)
        if (
            self.atom_count < 1
            or len(indices) != self.atom_count
            or len(labels) != self.atom_count
            or len(set(indices)) != len(indices)
        ):
            raise _fail("ORCA geometry atom selection is inconsistent")
        if not self.geometry_hash:
            raise _fail("ORCA geometry hash is required")
        if self.multiplicity < 1:
            raise _fail("ORCA geometry multiplicity must be positive")
        source_charge = self.source_artifact.charge
        source_multiplicity = self.source_artifact.multiplicity
        if (
            not self.electronic_state_override
            and (self.charge, self.multiplicity)
            != (source_charge, source_multiplicity)
        ):
            raise _fail(
                "ORCA electronic state conflicts with its source StructureArtifact"
            )
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "source_path", str(source_path.resolve()))
        object.__setattr__(self, "input_geometry_file", str(input_path.resolve()))
        object.__setattr__(self, "atom_indices", indices)
        object.__setattr__(self, "atom_labels", labels)
        object.__setattr__(self, "provenance", dict(self.provenance))

    @property
    def input_path(self) -> Path:
        return Path(self.input_geometry_file)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ORCA_GEOMETRY_SCHEMA_VERSION,
            "mode": self.mode.value,
            "source_structure_artifact_id": self.source_artifact.artifact_id,
            "source_structure_artifact": self.source_artifact.to_dict(),
            "source_path": self.source_path,
            "input_geometry_file": self.input_geometry_file,
            "format": "xyz",
            "atom_indices": list(self.atom_indices),
            "atom_labels": list(self.atom_labels),
            "atom_count": self.atom_count,
            "geometry_hash": self.geometry_hash,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
            "electronic_state_override": self.electronic_state_override,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OrcaGeometryInput":
        if value.get("schema_version") != ORCA_GEOMETRY_SCHEMA_VERSION:
            raise _fail("unsupported ORCA geometry contract schema")
        artifact_value = value.get("source_structure_artifact")
        if not isinstance(artifact_value, Mapping):
            raise _fail("source StructureArtifact record is missing")
        override = value.get("electronic_state_override", False)
        if not isinstance(override, bool):
            raise _fail("electronic_state_override must be boolean")
        try:
            artifact = artifact_from_dict(artifact_value)
        except (KeyError, TypeError, ValueError) as exc:
            raise _fail("source StructureArtifact record is invalid") from exc
        if not isinstance(artifact, StructureArtifact):
            raise _fail("ORCA geometry source is not a StructureArtifact")
        contract = cls(
            source_artifact=artifact,
            source_path=str(value.get("source_path", "")),
            input_geometry_file=str(value.get("input_geometry_file", "")),
            atom_indices=tuple(int(item) for item in value.get("atom_indices", ())),
            atom_labels=tuple(str(item) for item in value.get("atom_labels", ())),
            atom_count=int(value.get("atom_count", 0)),
            geometry_hash=str(value.get("geometry_hash", "")),
            charge=int(value.get("charge", 0)),
            multiplicity=int(value.get("multiplicity", 0)),
            electronic_state_override=override,
            mode=str(value.get("mode", "")),
            provenance=dict(value.get("provenance", {})),
        )
        if value.get("source_structure_artifact_id") != artifact.artifact_id:
            raise _fail("stored StructureArtifact identity is inconsistent")
        return contract


def _validated_source_geometry(
    artifact: StructureArtifact, *, base_dir: str | Path | None
) -> tuple[Path, XYZGeometry]:
    validation = validate_structure_artifact(artifact, base_dir=base_dir)
    if not artifact.validation.passed or not validation.passed:
        raise _fail(f"invalid StructureArtifact: {validation.code}: {validation.reason}")
    if artifact.format != "xyz":
        raise _fail(f"ORCA xyzfile rendering does not support format {artifact.format!r}")
    source = structure_artifact_path(artifact, base_dir=base_dir)
    if source is None:
        raise _fail("StructureArtifact does not declare a geometry file")
    return source.resolve(), read_xyz(source)


def prepare_orca_geometry_input(
    artifact: StructureArtifact,
    destination: str | Path,
    *,
    base_dir: str | Path | None = None,
    atom_indices: Sequence[int] | None = None,
    atom_labels: Sequence[str] | None = None,
    charge: int | None = None,
    multiplicity: int | None = None,
    allow_electronic_state_override: bool = False,
    provenance: Mapping[str, object] | None = None,
) -> OrcaGeometryInput:
    """Validate, select, and materialize one adjacent ORCA xyzfile input."""

    source_path, source_geometry = _validated_source_geometry(
        artifact, base_dir=base_dir
    )
    resolved_artifact = replace(
        artifact,
        files={**dict(artifact.files), "structure": str(source_path)},
    )
    input_path = Path(destination).expanduser()
    if not input_path.is_absolute():
        raise _fail("ORCA input geometry destination must be an absolute path")
    input_path = input_path.resolve()
    if input_path == source_path:
        raise _fail("ORCA input geometry must not overwrite its source artifact")
    selected_indices = tuple(
        range(source_geometry.atom_count) if atom_indices is None else atom_indices
    )
    if (
        not selected_indices
        or len(set(selected_indices)) != len(selected_indices)
        or any(index < 0 or index >= source_geometry.atom_count for index in selected_indices)
    ):
        raise _fail("ORCA geometry atom selection is empty, duplicated, or out of range")
    selected = XYZGeometry(
        tuple(source_geometry.atoms[index] for index in selected_indices),
        source_geometry.comment,
    )
    labels = tuple(atom_labels or selected.elements)
    if len(labels) != selected.atom_count:
        raise _fail("ORCA atom labels do not match the selected atom count")
    for label, atom in zip(labels, selected.atoms, strict=True):
        if _normalized_orca_element(label) != atom.element:
            raise _fail(
                f"ORCA atom label {label!r} contradicts source element {atom.element!r}"
            )
    selected_charge = int(artifact.charge if charge is None else charge)
    selected_multiplicity = int(
        artifact.multiplicity if multiplicity is None else multiplicity
    )
    if selected_multiplicity < 1:
        raise _fail("ORCA geometry multiplicity must be positive")
    if (
        not allow_electronic_state_override
        and (selected_charge, selected_multiplicity)
        != (artifact.charge, artifact.multiplicity)
    ):
        raise _fail("ORCA electronic state conflicts with its source StructureArtifact")
    _write_orca_xyzfile(
        input_path,
        selected,
        labels,
        comment=f"CMW StructureArtifact {resolved_artifact.artifact_id}",
    )
    contract = OrcaGeometryInput(
        source_artifact=resolved_artifact,
        source_path=str(source_path),
        input_geometry_file=str(input_path),
        atom_indices=selected_indices,
        atom_labels=labels,
        atom_count=selected.atom_count,
        geometry_hash=geometry_hash(selected),
        charge=selected_charge,
        multiplicity=selected_multiplicity,
        electronic_state_override=allow_electronic_state_override,
        provenance={
            "source_structure_artifact_id": resolved_artifact.artifact_id,
            **dict(provenance or {}),
        },
    )
    validate_orca_geometry_input(contract)
    return contract


def validate_orca_geometry_input(contract: OrcaGeometryInput) -> None:
    """Fail closed when source, selection, materialized file, or hash diverges."""

    source_path, source_geometry = _validated_source_geometry(
        contract.source_artifact, base_dir=None
    )
    if str(source_path) != contract.source_path:
        raise _fail("resolved StructureArtifact path conflicts with the geometry contract")
    if any(
        index < 0 or index >= source_geometry.atom_count
        for index in contract.atom_indices
    ):
        raise _fail("stored atom selection is outside the source StructureArtifact")
    selected = XYZGeometry(
        tuple(source_geometry.atoms[index] for index in contract.atom_indices),
        source_geometry.comment,
    )
    if (
        selected.atom_count != contract.atom_count
        or geometry_hash(selected) != contract.geometry_hash
    ):
        raise _fail("source StructureArtifact no longer matches the ORCA geometry contract")
    if not contract.input_path.is_file():
        raise _fail(f"ORCA input geometry file is missing: {contract.input_path}")
    materialized, labels = _parse_orca_xyzfile(contract.input_path)
    if labels != contract.atom_labels:
        raise _fail("ORCA input atom labels conflict with the geometry contract")
    if (
        materialized.atom_count != contract.atom_count
        or geometry_hash(materialized) != contract.geometry_hash
    ):
        raise _fail("ORCA input geometry file conflicts with the StructureArtifact")


def legacy_geometry_provenance(path: Path, *, mode: OrcaGeometryMode) -> dict[str, object]:
    """Classify supported legacy geometry without pretending it is artifact-backed."""

    if mode not in (OrcaGeometryMode.LEGACY_XYZFILE, OrcaGeometryMode.LEGACY_INLINE):
        raise _fail("legacy provenance requires an explicit legacy geometry mode")
    return {
        "schema_version": ORCA_GEOMETRY_SCHEMA_VERSION,
        "mode": mode.value,
        "source_structure_artifact": None,
        "input_geometry_file": str(path.resolve()),
        "validation": "legacy_compatibility_only",
    }


__all__ = [
    "ORCA_GEOMETRY_SCHEMA_VERSION",
    "OrcaGeometryContractError",
    "OrcaGeometryInput",
    "OrcaGeometryMode",
    "legacy_geometry_provenance",
    "prepare_orca_geometry_input",
    "read_orca_xyzfile_geometry",
    "validate_orca_geometry_input",
]
