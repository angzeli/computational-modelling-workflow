"""Extraction and validation of periodic-structure-derived stacking registries."""

from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
from typing import Mapping

from cmw.core.artifacts import (
    ArtifactValidation,
    StackingTemplateArtifact,
    StructureArtifact,
    ValidationStatus,
)
from cmw.core.structure_artifacts import (
    structure_artifact_path,
    validate_structure_artifact,
)
from cmw.structure.xyz import read_xyz

from .models import (
    COMPONENTS,
    PeriodicPairDefinition,
    StackingGeometry,
    core_frame,
    cross,
    dot,
    norm,
    scale,
    subtract,
    unit,
)


class StackingRegistryError(ValueError):
    """Raised when a periodic source cannot define a valid stacking registry."""

    code = "FAILED_STACKING_REGISTRY"


def _failure(checks: Mapping[str, bool | None], reason: str) -> ArtifactValidation:
    return ArtifactValidation(
        ValidationStatus.FAILED,
        checks,
        StackingRegistryError.code,
        reason,
    )


def _mapping(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StackingRegistryError(f"stacking template {name} must be a mapping")
    return value


def template_geometry(artifact: StackingTemplateArtifact) -> StackingGeometry:
    raw = _mapping(artifact.metadata.get("geometry"), name="geometry")
    return StackingGeometry(
        tuple(raw.get("stacking_axis", ())),  # type: ignore[arg-type]
        float(raw.get("plane_separation_angstrom", float("nan"))),
        float(raw.get("centroid_separation_angstrom", float("nan"))),
        tuple(raw.get("slip_vector_angstrom", ())),  # type: ignore[arg-type]
        float(raw.get("rotation_degrees", float("nan"))),
    )


def template_pair_identities(
    artifact: StackingTemplateArtifact,
) -> tuple[str, str]:
    pair = _mapping(artifact.metadata.get("molecular_pair"), name="molecular_pair")
    values = pair.get("identities")
    if not isinstance(values, list) or len(values) != 2:
        raise StackingRegistryError("stacking template requires two molecular identities")
    identities = tuple(str(item).strip() for item in values)
    if not all(identities):
        raise StackingRegistryError("stacking molecular identities cannot be empty")
    return identities  # type: ignore[return-value]


def template_core_indices(
    artifact: StackingTemplateArtifact,
) -> dict[str, tuple[int, ...]]:
    core = _mapping(artifact.metadata.get("core_definition"), name="core_definition")
    if core.get("indexing") != "zero_based":
        raise StackingRegistryError("stacking core indexing must be zero_based")
    raw = _mapping(core.get("local_atom_indices"), name="local core atom indices")
    selected: dict[str, tuple[int, ...]] = {}
    for component in COMPONENTS:
        values = raw.get(component)
        if not isinstance(values, list):
            raise StackingRegistryError(f"component {component} core mapping is missing")
        indices = tuple(int(item) for item in values)
        if len(indices) < 3 or min(indices, default=-1) < 0 or len(set(indices)) != len(indices):
            raise StackingRegistryError(
                f"component {component} core requires three unique zero-based indices"
            )
        selected[component] = indices
    return selected


def validate_stacking_template(
    artifact: StackingTemplateArtifact,
    *,
    source: StructureArtifact | None = None,
) -> ArtifactValidation:
    """Fail closed on incomplete geometry, mappings, or periodic provenance."""

    checks: dict[str, bool | None] = {
        "artifact_type": isinstance(artifact, StackingTemplateArtifact),
        "periodic_source": False,
        "periodic_origin": False,
        "source_hash": False,
        "molecular_pair": False,
        "core_mapping": False,
        "geometry": False,
        "provenance": False,
        "parent_identity": None,
    }
    try:
        source_metadata = _mapping(artifact.metadata.get("source"), name="source")
        checks["periodic_source"] = source_metadata.get("type") == "periodic_structure"
        origin = _mapping(
            artifact.metadata.get("periodic_origin"), name="periodic_origin"
        )
        checks["periodic_origin"] = bool(origin)
        source_hash = source_metadata.get("geometry_hash")
        checks["source_hash"] = isinstance(source_hash, str) and bool(source_hash)
        template_pair_identities(artifact)
        checks["molecular_pair"] = True
        template_core_indices(artifact)
        checks["core_mapping"] = True
        template_geometry(artifact)
        checks["geometry"] = True
        extraction = _mapping(artifact.metadata.get("extraction"), name="extraction")
        checks["provenance"] = (
            artifact.provenance.get("source_hash") == source_hash
            and artifact.provenance.get("extraction_method")
            == extraction.get("method")
            and bool(extraction.get("method"))
        )
        if source is not None:
            checks["parent_identity"] = artifact.parent_artifacts == (source.artifact_id,)
            checks["source_hash"] = checks["source_hash"] and (
                source_hash == source.geometry_hash
            )
    except (KeyError, TypeError, ValueError, StackingRegistryError) as exc:
        return _failure(checks, str(exc))
    required = (
        "artifact_type",
        "periodic_source",
        "periodic_origin",
        "source_hash",
        "molecular_pair",
        "core_mapping",
        "geometry",
        "provenance",
    )
    if not all(checks[item] is True for item in required) or (
        source is not None and checks["parent_identity"] is not True
    ):
        return _failure(checks, "stacking template metadata is incomplete or inconsistent")
    return ArtifactValidation(
        ValidationStatus.PASSED,
        checks,
        "VALID_STACKING_TEMPLATE",
        "periodic stacking registry and provenance are complete",
    )


def extract_stacking_template(
    source: StructureArtifact,
    pair: PeriodicPairDefinition,
    *,
    extraction_method: str,
    producing_calculation: str = "stacking_registry_extraction",
) -> StackingTemplateArtifact:
    """Measure a generic molecular pair registry from an explicit periodic source."""

    source_validation = validate_structure_artifact(source)
    if not source.validation.passed or not source_validation.passed:
        raise StackingRegistryError("periodic source StructureArtifact is not valid")
    if source.metadata.get("periodic") is not True:
        raise StackingRegistryError(
            "stacking registry extraction requires periodic: true source metadata"
        )
    if not extraction_method.strip():
        raise StackingRegistryError("stacking extraction method is required")
    path = structure_artifact_path(source)
    if path is None:
        raise StackingRegistryError("periodic source structure file is not declared")
    geometry = read_xyz(path)
    supplied = [
        index
        for component in COMPONENTS
        for index in pair.molecule_atom_indices[component]
    ]
    if max(supplied, default=-1) >= geometry.atom_count:
        raise StackingRegistryError("periodic molecular partition exceeds atom count")
    points = {
        component: tuple(
            geometry.atoms[index].coordinates
            for index in pair.core_atom_indices[component]
        )
        for component in COMPONENTS
    }
    frame_a = core_frame(points["A"])
    frame_b = core_frame(points["B"])
    axis = frame_a.normal
    displacement = subtract(frame_b.centroid, frame_a.centroid)
    if dot(axis, displacement) < 0:
        axis = scale(axis, -1.0)
    plane_separation = dot(displacement, axis)
    slip_global = subtract(displacement, scale(axis, plane_separation))
    slip_local = (
        dot(slip_global, frame_a.x_axis),
        dot(slip_global, frame_a.y_axis),
        0.0,
    )
    projected_x_b = subtract(frame_b.x_axis, scale(axis, dot(frame_b.x_axis, axis)))
    projected_x_b = unit(projected_x_b, name="second core in-plane axis")
    rotation = math.degrees(
        math.atan2(
            dot(cross(frame_a.x_axis, projected_x_b), axis),
            dot(frame_a.x_axis, projected_x_b),
        )
    )
    measured = StackingGeometry(
        axis,
        plane_separation,
        norm(displacement),
        slip_local,
        rotation,
    )
    metadata = {
        "source": {
            "type": "periodic_structure",
            "artifact_id": source.artifact_id,
            "file": str(path.resolve()),
            "format": source.format,
            "method": source.method,
            "geometry_hash": source.geometry_hash,
        },
        "periodic_origin": source.metadata.get("periodic_origin", {}),
        "molecular_pair": {
            "identities": list(pair.molecule_identities),
            "source_atom_indices": {
                component: list(pair.molecule_atom_indices[component])
                for component in COMPONENTS
            },
        },
        "core_definition": {
            "indexing": "zero_based",
            "source_atom_indices": {
                component: list(pair.core_atom_indices[component])
                for component in COMPONENTS
            },
            "local_atom_indices": {
                component: list(pair.local_core_indices(component))
                for component in COMPONENTS
            },
        },
        "geometry": measured.to_dict(),
        "extraction": {"method": extraction_method},
    }
    artifact = StackingTemplateArtifact(
        producing_calculation=producing_calculation,
        method=source.method,
        parent_artifacts=(source.artifact_id,),
        files={"source_structure": str(path.resolve())},
        provenance={
            "source_hash": source.geometry_hash,
            "extraction_method": extraction_method,
        },
        metadata=metadata,
    )
    validation = validate_stacking_template(artifact, source=source)
    if not validation.passed:
        raise StackingRegistryError(validation.reason)
    return replace(artifact, validation=validation)


__all__ = [
    "StackingRegistryError",
    "extract_stacking_template",
    "template_core_indices",
    "template_geometry",
    "template_pair_identities",
    "validate_stacking_template",
]
