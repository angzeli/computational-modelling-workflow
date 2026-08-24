"""Deterministic assembly of vertical dimers from stacking templates."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math
from pathlib import Path
from typing import Mapping, Sequence

from cmw.core.artifacts import (
    Artifact,
    ArtifactValidation,
    DimerStructureArtifact,
    StackingTemplateArtifact,
    StructureArtifact,
    ValidationStatus,
)
from cmw.core.provenance import stable_hash
from cmw.core.structure_artifacts import (
    structure_artifact_path,
    validate_structure_artifact,
)
from cmw.structure.xyz import Atom, XYZGeometry, geometry_hash, read_xyz, write_xyz

from .models import add, core_frame, cross, dot, scale, subtract
from .registry import (
    template_core_indices,
    template_geometry,
    template_pair_identities,
    validate_stacking_template,
)


class StackingAssemblyError(ValueError):
    """Raised when monomers and a registry cannot form a validated dimer."""

    code = "FAILED_STACKING_ASSEMBLY"


def _molecule_identity(artifact: StructureArtifact) -> str:
    value = artifact.metadata.get("molecule_id")
    if not isinstance(value, str) or not value.strip():
        raise StackingAssemblyError(
            "monomer StructureArtifact requires a non-empty molecule_id"
        )
    return value.strip()


def _validated_geometry(artifact: StructureArtifact) -> XYZGeometry:
    validation = validate_structure_artifact(artifact)
    if not artifact.validation.passed or not validation.passed:
        raise StackingAssemblyError("monomer StructureArtifact is not valid")
    path = structure_artifact_path(artifact)
    if path is None:
        raise StackingAssemblyError("monomer structure path is missing")
    return read_xyz(path)


def _transform_second_monomer(
    geometry: XYZGeometry,
    *,
    core_indices: tuple[int, ...],
    target_frame,
    plane_separation: float,
    slip_vector: tuple[float, float, float],
    rotation_degrees: float,
) -> tuple[Atom, ...]:
    frame = core_frame(tuple(geometry.atoms[index].coordinates for index in core_indices))
    angle = math.radians(rotation_degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    target_x = add(scale(target_frame.x_axis, cosine), scale(target_frame.y_axis, sine))
    target_y = add(scale(target_frame.y_axis, cosine), scale(target_frame.x_axis, -sine))
    target_centroid = add(
        target_frame.centroid,
        add(
            scale(target_frame.x_axis, slip_vector[0]),
            add(
                scale(target_frame.y_axis, slip_vector[1]),
                scale(target_frame.normal, plane_separation),
            ),
        ),
    )
    transformed: list[Atom] = []
    for atom in geometry.atoms:
        relative = subtract(atom.coordinates, frame.centroid)
        local = (
            dot(relative, frame.x_axis),
            dot(relative, frame.y_axis),
            dot(relative, frame.normal),
        )
        position = add(
            target_centroid,
            add(
                scale(target_x, local[0]),
                add(scale(target_y, local[1]), scale(target_frame.normal, local[2])),
            ),
        )
        transformed.append(Atom(atom.element, *position))
    return tuple(transformed)


def _observed_geometry(
    geometry: XYZGeometry,
    *,
    split: int,
    core_a: tuple[int, ...],
    core_b: tuple[int, ...],
) -> dict[str, object]:
    frame_a = core_frame(tuple(geometry.atoms[index].coordinates for index in core_a))
    frame_b = core_frame(
        tuple(geometry.atoms[split + index].coordinates for index in core_b)
    )
    normal_b = frame_b.normal
    if dot(frame_a.normal, normal_b) < 0:
        normal_b = scale(normal_b, -1.0)
    displacement = subtract(frame_b.centroid, frame_a.centroid)
    plane = abs(dot(displacement, frame_a.normal))
    slip = (
        dot(displacement, frame_a.x_axis),
        dot(displacement, frame_a.y_axis),
        0.0,
    )
    projected_x_b = subtract(
        frame_b.x_axis,
        scale(frame_a.normal, dot(frame_b.x_axis, frame_a.normal)),
    )
    length = math.sqrt(dot(projected_x_b, projected_x_b))
    if length <= 1.0e-12:
        raise StackingAssemblyError("assembled second core lacks an in-plane axis")
    projected_x_b = scale(projected_x_b, 1.0 / length)
    rotation = math.degrees(
        math.atan2(
            dot(cross(frame_a.x_axis, projected_x_b), frame_a.normal),
            dot(frame_a.x_axis, projected_x_b),
        )
    )
    return {
        "plane_separation_angstrom": plane,
        "centroid_separation_angstrom": math.sqrt(dot(displacement, displacement)),
        "slip_vector_angstrom": list(slip),
        "rotation_degrees": rotation,
        "interplane_alignment": abs(dot(frame_a.normal, normal_b)),
    }


def _angle_difference(first: float, second: float) -> float:
    difference = (first - second + 180.0) % 360.0 - 180.0
    return abs(difference)


def validate_dimer_structure(
    artifact: DimerStructureArtifact,
    *,
    parents: Sequence[Artifact] = (),
    tolerance: float = 1.0e-7,
) -> ArtifactValidation:
    """Validate structure evidence, component identity, and registry preservation."""

    base = validate_structure_artifact(artifact)
    checks: dict[str, bool | None] = dict(base.checks)
    checks.update(
        {
            "artifact_type": isinstance(artifact, DimerStructureArtifact),
            "parent_identity": None,
            "atom_identity": False,
            "component_registry": False,
            "registry_preserved": False,
            "provenance_preserved": False,
        }
    )
    if not base.passed:
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            StackingAssemblyError.code,
            base.reason,
        )
    try:
        component = artifact.metadata.get("components")
        if not isinstance(component, Mapping):
            raise StackingAssemblyError("dimer component metadata is missing")
        component_a = component["A"]  # type: ignore[index]
        component_b = component["B"]  # type: ignore[index]
        if not isinstance(component_a, Mapping) or not isinstance(
            component_b, Mapping
        ):
            raise StackingAssemblyError("dimer component entries must be mappings")
        split = int(component_a["atom_count"])
        count_b = int(component_b["atom_count"])
        core_a = tuple(  # type: ignore[index]
            int(item) for item in component_a["core_atom_indices"]
        )
        core_b = tuple(  # type: ignore[index]
            int(item) for item in component_b["core_atom_indices"]
        )
        path = structure_artifact_path(artifact)
        assert path is not None
        geometry = read_xyz(path)
        checks["component_registry"] = (
            split > 0
            and count_b > 0
            and split + count_b == geometry.atom_count
            and component_a.get("atom_indices") == list(range(split))
            and component_b.get("atom_indices")
            == list(range(split, split + count_b))
            and len(core_a) >= 3
            and len(core_b) >= 3
            and min(core_a, default=-1) >= 0
            and min(core_b, default=-1) >= 0
            and max(core_a, default=split) < split
            and max(core_b, default=count_b) < count_b
        )
        expected_elements = artifact.metadata.get("ordered_element_identity")
        checks["atom_identity"] = (
            stable_hash({"elements": list(geometry.elements)}) == expected_elements
        )
        registry = artifact.metadata.get("stacking_registry")
        if not isinstance(registry, Mapping):
            raise StackingAssemblyError("dimer stacking registry metadata is missing")
        expected = registry.get("geometry")
        if not isinstance(expected, Mapping):
            raise StackingAssemblyError("dimer stacking geometry metadata is missing")
        observed = _observed_geometry(
            geometry, split=split, core_a=core_a, core_b=core_b
        )
        slip_expected = tuple(  # type: ignore[index]
            float(item) for item in expected["slip_vector_angstrom"]
        )
        slip_observed = tuple(  # type: ignore[arg-type]
            float(item) for item in observed["slip_vector_angstrom"]
        )
        checks["registry_preserved"] = (
            math.isclose(
                float(observed["plane_separation_angstrom"]),
                float(expected["plane_separation_angstrom"]),
                abs_tol=tolerance,
            )
            and math.isclose(
                float(observed["centroid_separation_angstrom"]),
                float(expected["centroid_separation_angstrom"]),
                abs_tol=tolerance,
            )
            and all(
                math.isclose(actual, wanted, abs_tol=tolerance)
                for actual, wanted in zip(slip_observed, slip_expected)
            )
            and _angle_difference(
                float(observed["rotation_degrees"]),
                float(expected["rotation_degrees"]),
            )
            <= max(tolerance, 1.0e-6)
        )
        template_id = registry.get("template_artifact")
        monomer_ids = [
            component_a.get("source_artifact"),
            component_b.get("source_artifact"),
        ]
        checks["provenance_preserved"] = (
            artifact.provenance.get("stacking_template") == template_id
            and artifact.provenance.get("monomer_artifacts") == monomer_ids
            and template_id in artifact.parent_artifacts
            and all(item in artifact.parent_artifacts for item in monomer_ids)
        )
        if parents:
            checks["parent_identity"] = set(artifact.parent_artifacts) == {
                parent.artifact_id for parent in parents
            }
    except (KeyError, TypeError, ValueError, StackingAssemblyError) as exc:
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            StackingAssemblyError.code,
            str(exc),
        )
    required = (
        "artifact_type",
        "atom_identity",
        "component_registry",
        "registry_preserved",
        "provenance_preserved",
    )
    if not all(checks[item] is True for item in required) or (
        parents and checks["parent_identity"] is not True
    ):
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            StackingAssemblyError.code,
            "assembled dimer does not preserve its declared atoms or stacking registry",
        )
    return ArtifactValidation(
        ValidationStatus.PASSED,
        checks,
        "VALID_DIMER_STRUCTURE",
        "assembled dimer preserves atom identity, provenance, and stacking registry",
    )


def assemble_vertical_dimer(
    monomer_a: StructureArtifact,
    template: StackingTemplateArtifact,
    output_path: str | Path,
    *,
    monomer_b: StructureArtifact | None = None,
    charge: int | None = None,
    multiplicity: int | None = None,
    producing_calculation: str = "vertical_dimer_assembly",
) -> DimerStructureArtifact:
    """Assemble one ordered dimer without inferring molecular or core identity."""

    if not validate_stacking_template(template).passed:
        raise StackingAssemblyError("StackingTemplateArtifact is not valid")
    selected_b = monomer_b or monomer_a
    expected_ids = template_pair_identities(template)
    actual_ids = (_molecule_identity(monomer_a), _molecule_identity(selected_b))
    if actual_ids != expected_ids:
        raise StackingAssemblyError(
            f"monomer identities {actual_ids!r} do not match template {expected_ids!r}"
        )
    geometry_a = _validated_geometry(monomer_a)
    geometry_b = _validated_geometry(selected_b)
    cores = template_core_indices(template)
    for component, geometry in (("A", geometry_a), ("B", geometry_b)):
        if max(cores[component], default=-1) >= geometry.atom_count:
            raise StackingAssemblyError(
                f"component {component} core mapping exceeds monomer atom count"
            )
    frame_a = core_frame(tuple(geometry_a.atoms[index].coordinates for index in cores["A"]))
    registry = template_geometry(template)
    transformed_b = _transform_second_monomer(
        geometry_b,
        core_indices=cores["B"],
        target_frame=frame_a,
        plane_separation=registry.plane_separation_angstrom,
        slip_vector=registry.slip_vector_angstrom,
        rotation_degrees=registry.rotation_degrees,
    )
    dimer = XYZGeometry(
        (*geometry_a.atoms, *transformed_b),
        "CMW vertical dimer assembled from a periodic stacking template",
    )
    target = Path(output_path)
    write_xyz(target, dimer)
    persisted_dimer = read_xyz(target)

    if charge is None:
        if monomer_a.charge is None or selected_b.charge is None:
            raise StackingAssemblyError(
                "dimer charge must be explicit when monomer charge is absent"
            )
        charge = monomer_a.charge + selected_b.charge
    if multiplicity is None:
        if monomer_a.multiplicity == selected_b.multiplicity == 1:
            multiplicity = 1
        else:
            raise StackingAssemblyError(
                "dimer multiplicity must be explicit for non-singlet monomers"
            )
    parents = tuple(
        dict.fromkeys((monomer_a.artifact_id, selected_b.artifact_id, template.artifact_id))
    )
    offset = geometry_a.atom_count
    component_metadata = {
        "A": {
            "molecule_id": actual_ids[0],
            "source_artifact": monomer_a.artifact_id,
            "atom_count": geometry_a.atom_count,
            "atom_indices": list(range(geometry_a.atom_count)),
            "core_atom_indices": list(cores["A"]),
        },
        "B": {
            "molecule_id": actual_ids[1],
            "source_artifact": selected_b.artifact_id,
            "atom_count": geometry_b.atom_count,
            "atom_indices": list(range(offset, offset + geometry_b.atom_count)),
            "core_atom_indices": list(cores["B"]),
        },
    }
    artifact = DimerStructureArtifact(
        producing_calculation=producing_calculation,
        parent_artifacts=parents,
        files={"structure": str(target)},
        provenance={
            "assembly_operation": "vertical_dimer",
            "monomer_artifacts": [monomer_a.artifact_id, selected_b.artifact_id],
            "stacking_template": template.artifact_id,
        },
        metadata={
            "molecular_pair": list(actual_ids),
            "components": component_metadata,
            "stacking_registry": {
                "template_artifact": template.artifact_id,
                "geometry": registry.to_dict(),
            },
            "ordered_element_identity": stable_hash(
                {"elements": list(persisted_dimer.elements)}
            ),
        },
        source="vertical_dimer_assembly",
        format="xyz",
        atom_count=persisted_dimer.atom_count,
        elemental_composition=dict(Counter(persisted_dimer.elements)),
        charge=charge,
        multiplicity=multiplicity,
        geometry_hash=geometry_hash(persisted_dimer),
    )
    parent_objects_by_id = {
        parent.artifact_id: parent for parent in (monomer_a, selected_b, template)
    }
    parent_objects: tuple[Artifact, ...] = tuple(parent_objects_by_id.values())
    validation = validate_dimer_structure(artifact, parents=parent_objects)
    if not validation.passed:
        raise StackingAssemblyError(validation.reason)
    return replace(artifact, validation=validation)


__all__ = [
    "StackingAssemblyError",
    "assemble_vertical_dimer",
    "validate_dimer_structure",
]
