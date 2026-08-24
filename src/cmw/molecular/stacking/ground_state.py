"""Generic quantitative checks for ground-state two-fragment stacking workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Mapping, Sequence

from cmw.structure.xyz import XYZGeometry

from .models import core_frame, cross, dot, norm, scale, subtract


HARTREE_TO_KJ_MOL = 2625.4996394799

# Pyykko-style single-bond covalent radii in angstrom for common molecular
# elements. Unknown elements fail closed instead of receiving a guessed radius.
COVALENT_RADII_ANGSTROM: Mapping[str, float] = {
    "H": 0.31,
    "B": 0.84,
    "C": 0.76,
    "N": 0.71,
    "O": 0.66,
    "F": 0.57,
    "Si": 1.11,
    "P": 1.07,
    "S": 1.05,
    "Cl": 1.02,
    "Br": 1.20,
    "I": 1.39,
}


@dataclass(frozen=True)
class InterfragmentContact:
    atom_a: int
    element_a: str
    atom_b: int
    element_b: str
    distance_angstrom: float


@dataclass(frozen=True)
class StackingGeometryMetrics:
    core_plane_separation_angstrom: float
    centroid_distance_angstrom: float
    slip_vector_angstrom: tuple[float, float, float]
    slip_magnitude_angstrom: float
    relative_rotation_degrees: float
    plane_normal_angle_degrees: float
    shortest_interfragment_contacts: tuple[InterfragmentContact, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["shortest_interfragment_contacts"] = [
            asdict(item) for item in self.shortest_interfragment_contacts
        ]
        return value


@dataclass(frozen=True)
class GeometryComparison:
    initial: StackingGeometryMetrics
    final: StackingGeometryMetrics
    changes: Mapping[str, float]
    checks: Mapping[str, bool]
    connectivity_changes: Mapping[str, tuple[tuple[int, int], ...]]
    maximum_fixed_atom_displacement_angstrom: float | None
    validation_status: str
    validation_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "initial": self.initial.to_dict(),
            "final": self.final.to_dict(),
            "changes": dict(self.changes),
            "checks": dict(self.checks),
            "connectivity_changes": {
                key: [list(pair) for pair in value]
                for key, value in self.connectivity_changes.items()
            },
            "maximum_fixed_atom_displacement_angstrom": (
                self.maximum_fixed_atom_displacement_angstrom
            ),
            "validation_status": self.validation_status,
            "validation_reason": self.validation_reason,
        }


@dataclass(frozen=True)
class EnergyDecomposition:
    interaction_hartree: float
    interaction_kj_mol: float
    deformation_hartree: float
    deformation_kj_mol: float
    association_hartree: float
    association_kj_mol: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class MolecularConnectivityComparison:
    preserved: bool
    broken_bonds: tuple[tuple[int, int], ...]
    formed_bonds: tuple[tuple[int, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "preserved": self.preserved,
            "broken_bonds": [list(item) for item in self.broken_bonds],
            "formed_bonds": [list(item) for item in self.formed_bonds],
        }


def _partition(
    geometry: XYZGeometry, fragments: Mapping[str, Sequence[int]]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if len(fragments) != 2:
        raise ValueError("stacking geometry requires exactly two fragments")
    values = tuple(tuple(int(index) for index in item) for item in fragments.values())
    flattened = [index for fragment in values for index in fragment]
    if (
        any(not fragment for fragment in values)
        or len(flattened) != len(set(flattened))
        or set(flattened) != set(range(geometry.atom_count))
    ):
        raise ValueError("fragment definitions must partition every atom exactly once")
    return values  # type: ignore[return-value]


def _distance(first, second) -> float:
    return norm(subtract(first, second))


def stacking_geometry_metrics(
    geometry: XYZGeometry,
    *,
    fragments: Mapping[str, Sequence[int]],
    core_indices: Mapping[str, Sequence[int]],
    contact_count: int = 10,
) -> StackingGeometryMetrics:
    """Measure one two-fragment stack in a deterministic component-A frame."""

    fragment_a, fragment_b = _partition(geometry, fragments)
    names = tuple(fragments)
    if set(core_indices) != set(names):
        raise ValueError("core definitions must match the two fragment identities")
    cores: list[tuple[int, ...]] = []
    for name, fragment in zip(names, (fragment_a, fragment_b), strict=True):
        selected = tuple(int(index) for index in core_indices[name])
        if len(selected) < 3 or not set(selected).issubset(fragment):
            raise ValueError(f"core definition for {name} is outside its fragment")
        cores.append(selected)
    frame_a = core_frame(
        tuple(geometry.atoms[index].coordinates for index in cores[0])
    )
    frame_b = core_frame(
        tuple(geometry.atoms[index].coordinates for index in cores[1])
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
    projected_b = subtract(
        frame_b.x_axis,
        scale(frame_a.normal, dot(frame_b.x_axis, frame_a.normal)),
    )
    projected_length = norm(projected_b)
    if projected_length <= 1.0e-12:
        raise ValueError("second core lacks a stable in-plane rotation axis")
    projected_b = scale(projected_b, 1.0 / projected_length)
    rotation = math.degrees(
        math.atan2(
            dot(cross(frame_a.x_axis, projected_b), frame_a.normal),
            dot(frame_a.x_axis, projected_b),
        )
    )
    tilt = math.degrees(
        math.acos(max(-1.0, min(1.0, dot(frame_a.normal, normal_b))))
    )
    contacts = sorted(
        (
            InterfragmentContact(
                index_a,
                geometry.atoms[index_a].element,
                index_b,
                geometry.atoms[index_b].element,
                _distance(
                    geometry.atoms[index_a].coordinates,
                    geometry.atoms[index_b].coordinates,
                ),
            )
            for index_a in fragment_a
            for index_b in fragment_b
        ),
        key=lambda item: (item.distance_angstrom, item.atom_a, item.atom_b),
    )
    return StackingGeometryMetrics(
        plane,
        norm(displacement),
        slip,
        math.hypot(slip[0], slip[1]),
        rotation,
        tilt,
        tuple(contacts[: max(1, int(contact_count))]),
    )


def _bonds(geometry: XYZGeometry, indices: Sequence[int]) -> set[tuple[int, int]]:
    selected = tuple(int(index) for index in indices)
    radii: dict[int, float] = {}
    for index in selected:
        element = geometry.atoms[index].element
        if element not in COVALENT_RADII_ANGSTROM:
            raise ValueError(f"no covalent radius is configured for element {element}")
        radii[index] = COVALENT_RADII_ANGSTROM[element]
    return {
        (first, second)
        for position, first in enumerate(selected)
        for second in selected[position + 1 :]
        if _distance(
            geometry.atoms[first].coordinates,
            geometry.atoms[second].coordinates,
        )
        <= 1.25 * (radii[first] + radii[second])
    }


def compare_stacking_geometries(
    initial: XYZGeometry,
    final: XYZGeometry,
    *,
    fragments: Mapping[str, Sequence[int]],
    core_indices: Mapping[str, Sequence[int]],
    fixed_atom_indices: Sequence[int] = (),
    fixed_tolerance_angstrom: float = 1.0e-5,
    clash_scale: float = 0.65,
    dissociation_centroid_increase_angstrom: float = 4.0,
    contact_count: int = 10,
) -> GeometryComparison:
    """Compare atom-preserving structures and fail closed on topology changes."""

    if initial.elements != final.elements:
        raise ValueError("geometry comparison requires identical ordered elements")
    fragment_values = _partition(initial, fragments)
    _partition(final, fragments)
    initial_metrics = stacking_geometry_metrics(
        initial,
        fragments=fragments,
        core_indices=core_indices,
        contact_count=contact_count,
    )
    final_metrics = stacking_geometry_metrics(
        final,
        fragments=fragments,
        core_indices=core_indices,
        contact_count=contact_count,
    )
    initial_bonds = set().union(
        *(_bonds(initial, fragment) for fragment in fragment_values)
    )
    final_bonds = set().union(
        *(_bonds(final, fragment) for fragment in fragment_values)
    )
    broken = tuple(sorted(initial_bonds - final_bonds))
    formed = tuple(sorted(final_bonds - initial_bonds))
    interfragment_bonds = tuple(
        sorted(
            (first, second)
            for first in fragment_values[0]
            for second in fragment_values[1]
            if _distance(
                final.atoms[first].coordinates, final.atoms[second].coordinates
            )
            <= 1.25
            * (
                COVALENT_RADII_ANGSTROM[final.atoms[first].element]
                + COVALENT_RADII_ANGSTROM[final.atoms[second].element]
            )
        )
    )
    clashes = tuple(
        sorted(
            (first, second)
            for first in fragment_values[0]
            for second in fragment_values[1]
            if _distance(
                final.atoms[first].coordinates, final.atoms[second].coordinates
            )
            < clash_scale
            * (
                COVALENT_RADII_ANGSTROM[final.atoms[first].element]
                + COVALENT_RADII_ANGSTROM[final.atoms[second].element]
            )
        )
    )
    maximum_fixed = (
        max(
            (
                _distance(
                    initial.atoms[int(index)].coordinates,
                    final.atoms[int(index)].coordinates,
                )
                for index in fixed_atom_indices
            ),
            default=0.0,
        )
        if fixed_atom_indices
        else None
    )
    checks = {
        "ordered_atom_identity_preserved": True,
        "intrafragment_connectivity_preserved": not broken and not formed,
        "no_interfragment_covalent_bond": not interfragment_bonds,
        "no_catastrophic_interfragment_clash": not clashes,
        "fixed_atoms_preserved": (
            maximum_fixed is None or maximum_fixed <= fixed_tolerance_angstrom
        ),
        "stack_not_substantially_dissociated": (
            final_metrics.centroid_distance_angstrom
            - initial_metrics.centroid_distance_angstrom
            <= dissociation_centroid_increase_angstrom
        ),
    }
    changes = {
        "core_plane_separation_angstrom": (
            final_metrics.core_plane_separation_angstrom
            - initial_metrics.core_plane_separation_angstrom
        ),
        "centroid_distance_angstrom": (
            final_metrics.centroid_distance_angstrom
            - initial_metrics.centroid_distance_angstrom
        ),
        "slip_magnitude_angstrom": (
            final_metrics.slip_magnitude_angstrom
            - initial_metrics.slip_magnitude_angstrom
        ),
        "relative_rotation_degrees": (
            final_metrics.relative_rotation_degrees
            - initial_metrics.relative_rotation_degrees
        ),
        "plane_normal_angle_degrees": (
            final_metrics.plane_normal_angle_degrees
            - initial_metrics.plane_normal_angle_degrees
        ),
    }
    valid = all(checks.values())
    return GeometryComparison(
        initial_metrics,
        final_metrics,
        changes,
        checks,
        {
            "broken_intrafragment_bonds": broken,
            "formed_intrafragment_bonds": formed,
            "formed_interfragment_bonds": interfragment_bonds,
            "catastrophic_clashes": clashes,
        },
        maximum_fixed,
        "PASSED" if valid else "FAILED",
        (
            "ordered fragments and configured structural contracts are preserved"
            if valid
            else "one or more structural preservation checks failed"
        ),
    )


def compare_molecular_connectivity(
    initial: XYZGeometry, final: XYZGeometry
) -> MolecularConnectivityComparison:
    """Compare ordered molecular graphs using one explicit covalent-radius rule."""

    if initial.elements != final.elements:
        raise ValueError("connectivity comparison requires identical ordered elements")
    indices = tuple(range(initial.atom_count))
    before = _bonds(initial, indices)
    after = _bonds(final, indices)
    broken = tuple(sorted(before - after))
    formed = tuple(sorted(after - before))
    return MolecularConnectivityComparison(not broken and not formed, broken, formed)


def torsion_angle_degrees(
    geometry: XYZGeometry, atom_indices: Sequence[int]
) -> float:
    """Return one signed Cartesian dihedral for four zero-based atoms."""

    indices = tuple(int(index) for index in atom_indices)
    if len(indices) != 4 or len(set(indices)) != 4 or any(
        index < 0 or index >= geometry.atom_count for index in indices
    ):
        raise ValueError("torsion definition requires four distinct valid atom indices")
    p0, p1, p2, p3 = (
        geometry.atoms[index].coordinates for index in indices
    )
    b0 = subtract(p0, p1)
    b1 = subtract(p2, p1)
    b2 = subtract(p3, p2)
    axis = scale(b1, 1.0 / norm(b1))
    v = subtract(b0, scale(axis, dot(b0, axis)))
    w = subtract(b2, scale(axis, dot(b2, axis)))
    if norm(v) <= 1.0e-12 or norm(w) <= 1.0e-12:
        raise ValueError("torsion atoms are collinear")
    return math.degrees(math.atan2(dot(cross(axis, v), w), dot(v, w)))


def cp_interaction_energy(
    dimer_hartree: float, fragment_a_ghost_hartree: float, fragment_b_ghost_hartree: float
) -> float:
    values = (dimer_hartree, fragment_a_ghost_hartree, fragment_b_ghost_hartree)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("counterpoise energies must be finite")
    return dimer_hartree - fragment_a_ghost_hartree - fragment_b_ghost_hartree


def energy_decomposition(
    *,
    interaction_hartree: float,
    distorted_fragment_energies_hartree: Sequence[float],
    relaxed_fragment_energies_hartree: Sequence[float],
) -> EnergyDecomposition:
    distorted = tuple(float(value) for value in distorted_fragment_energies_hartree)
    relaxed = tuple(float(value) for value in relaxed_fragment_energies_hartree)
    if not distorted or len(distorted) != len(relaxed):
        raise ValueError("deformation energy requires paired distorted and relaxed energies")
    values = (interaction_hartree, *distorted, *relaxed)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("deformation energies must be finite")
    deformation = sum(actual - reference for actual, reference in zip(distorted, relaxed))
    association = interaction_hartree + deformation
    return EnergyDecomposition(
        interaction_hartree,
        interaction_hartree * HARTREE_TO_KJ_MOL,
        deformation,
        deformation * HARTREE_TO_KJ_MOL,
        association,
        association * HARTREE_TO_KJ_MOL,
    )


def heterostack_mixing_metric(
    heterostack: float, homostack_a: float, homostack_b: float
) -> float:
    values = (heterostack, homostack_a, homostack_b)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("heterostack mixing inputs must be finite")
    return heterostack - 0.5 * (homostack_a + homostack_b)


__all__ = [
    "COVALENT_RADII_ANGSTROM",
    "EnergyDecomposition",
    "GeometryComparison",
    "HARTREE_TO_KJ_MOL",
    "InterfragmentContact",
    "MolecularConnectivityComparison",
    "StackingGeometryMetrics",
    "compare_stacking_geometries",
    "compare_molecular_connectivity",
    "cp_interaction_energy",
    "energy_decomposition",
    "heterostack_mixing_metric",
    "stacking_geometry_metrics",
    "torsion_angle_degrees",
]
