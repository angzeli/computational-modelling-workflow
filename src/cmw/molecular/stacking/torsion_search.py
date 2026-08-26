"""Deterministic peripheral-torsion search for two-fragment assemblies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Mapping, Sequence

from cmw.structure.xyz import Atom, XYZGeometry

from .ground_state import COVALENT_RADII_ANGSTROM


VDW_RADII_ANGSTROM: Mapping[str, float] = {
    "H": 1.20,
    "B": 1.92,
    "C": 1.70,
    "N": 1.55,
    "O": 1.52,
    "F": 1.47,
    "Si": 2.10,
    "P": 1.80,
    "S": 1.80,
    "Cl": 1.75,
    "Br": 1.85,
    "I": 1.98,
}

_PRIMES = (
    2,
    3,
    5,
    7,
    11,
    13,
    17,
    19,
    23,
    29,
    31,
    37,
    41,
    43,
    47,
    53,
    59,
    61,
    67,
    71,
    73,
    79,
    83,
    89,
    97,
    101,
    103,
    107,
    109,
    113,
    127,
    131,
)


@dataclass(frozen=True)
class RotatableBond:
    """One graph-validated bridge bond and the atoms rotated about it."""

    torsion_id: str
    fragment_id: str
    fixed_atom: int
    rotating_atom: int
    rotating_atoms: tuple[int, ...]
    reference_atoms: tuple[int, int, int, int]
    classification: str
    core_graph_depth: int

    @property
    def bond(self) -> tuple[int, int]:
        return (self.fixed_atom, self.rotating_atom)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["bond"] = list(self.bond)
        return value


@dataclass(frozen=True)
class ContactRecord:
    """One element-aware interfragment contact classification."""

    atom_a: int
    element_a: str
    atom_b: int
    element_b: str
    distance_angstrom: float
    hard_threshold_angstrom: float
    soft_threshold_angstrom: float
    classification: str

    @property
    def pair_type(self) -> str:
        return "-".join(sorted((self.element_a, self.element_b)))

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "pair_type": self.pair_type}


@dataclass(frozen=True)
class TorsionSearchProtocol:
    """Bounded, reproducible search and neutral geometric scoring settings."""

    angle_grid_degrees: tuple[float, ...] = (0.0, 60.0, -60.0, 120.0, -120.0, 180.0)
    coarse_candidate_count: int = 4096
    fine_increment_degrees: float = 30.0
    fine_passes: int = 3
    pair_refinement_passes: int = 2
    refinement_seed_count: int = 4
    covalent_bond_scale: float = 1.25
    soft_overlap_weight: float = 1.0
    torsion_displacement_weight: float = 0.02
    peripheral_displacement_weight: float = 0.01

    def __post_init__(self) -> None:
        if not self.angle_grid_degrees or 0.0 not in self.angle_grid_degrees:
            raise ValueError("torsion angle grid must contain zero")
        if self.coarse_candidate_count < 1:
            raise ValueError("coarse candidate count must be positive")
        if (
            self.fine_increment_degrees <= 0
            or self.fine_passes < 0
            or self.pair_refinement_passes < 0
            or self.refinement_seed_count < 1
        ):
            raise ValueError("fine torsion schedule is invalid")
        if self.covalent_bond_scale <= 1.0:
            raise ValueError("covalent bond scale must be greater than one")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TorsionSearchResult:
    """Selected geometry plus a complete deterministic candidate audit."""

    geometry: XYZGeometry
    torsions: tuple[RotatableBond, ...]
    selected_angles_degrees: tuple[float, ...]
    selected_candidate_id: int
    selected_score: float
    selected_contacts: tuple[ContactRecord, ...]
    candidate_records: tuple[Mapping[str, object], ...]
    protocol: TorsionSearchProtocol

    def summary(self) -> dict[str, object]:
        valid = sum(bool(record["hard_valid"]) for record in self.candidate_records)
        return {
            "selected_candidate_id": self.selected_candidate_id,
            "selected_score": self.selected_score,
            "selected_angles_degrees": {
                torsion.torsion_id: angle
                for torsion, angle in zip(
                    self.torsions, self.selected_angles_degrees, strict=True
                )
            },
            "candidate_count": len(self.candidate_records),
            "hard_valid_candidate_count": valid,
            "hard_rejected_candidate_count": len(self.candidate_records) - valid,
            "torsion_count": len(self.torsions),
            "protocol": self.protocol.to_dict(),
        }


def _distance(first: Sequence[float], second: Sequence[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second, strict=True)))


def _bond_graph(
    geometry: XYZGeometry, *, covalent_scale: float
) -> tuple[tuple[int, ...], ...]:
    neighbors = [set() for _ in geometry.atoms]
    for first, atom_a in enumerate(geometry.atoms):
        try:
            radius_a = COVALENT_RADII_ANGSTROM[atom_a.element]
        except KeyError as exc:
            raise ValueError(
                f"no covalent radius is configured for {atom_a.element}"
            ) from exc
        for second in range(first + 1, geometry.atom_count):
            atom_b = geometry.atoms[second]
            try:
                radius_b = COVALENT_RADII_ANGSTROM[atom_b.element]
            except KeyError as exc:
                raise ValueError(
                    f"no covalent radius is configured for {atom_b.element}"
                ) from exc
            if _distance(atom_a.coordinates, atom_b.coordinates) <= covalent_scale * (
                radius_a + radius_b
            ):
                neighbors[first].add(second)
                neighbors[second].add(first)
    return tuple(tuple(sorted(items)) for items in neighbors)


def _component_without_edge(
    graph: Sequence[Sequence[int]], start: int, removed: tuple[int, int]
) -> tuple[int, ...]:
    selected = {start}
    pending = [start]
    blocked = {removed, (removed[1], removed[0])}
    while pending:
        atom = pending.pop()
        for neighbor in graph[atom]:
            if (atom, neighbor) in blocked or neighbor in selected:
                continue
            selected.add(neighbor)
            pending.append(neighbor)
    return tuple(sorted(selected))


def _graph_depth(
    graph: Sequence[Sequence[int]], start: int, targets: set[int]
) -> int:
    if start in targets:
        return 0
    visited = {start}
    frontier = [(start, 0)]
    for atom, depth in frontier:
        for neighbor in graph[atom]:
            if neighbor in targets:
                return depth + 1
            if neighbor not in visited:
                visited.add(neighbor)
                frontier.append((neighbor, depth + 1))
    raise ValueError("peripheral atom is disconnected from the configured core")


def _reference_neighbor(
    geometry: XYZGeometry, graph: Sequence[Sequence[int]], atom: int, excluded: int
) -> int:
    candidates = [item for item in graph[atom] if item != excluded]
    if not candidates:
        raise ValueError("rotatable bond endpoint lacks a torsion reference atom")
    return min(candidates, key=lambda item: (geometry.atoms[item].element == "H", item))


def identify_peripheral_rotatable_bonds(
    geometry: XYZGeometry,
    *,
    core_indices: Sequence[int],
    fragment_id: str = "fragment",
    covalent_scale: float = 1.25,
) -> tuple[RotatableBond, ...]:
    """Detect conservative peripheral C-C and C-N bridge torsions.

    Ring bonds are excluded because removing them does not split the molecular
    graph.  The moving component must contain no configured core atom, which
    makes core-coordinate preservation a structural property of the result.
    """

    core = {int(index) for index in core_indices}
    if len(core) < 3 or min(core, default=-1) < 0 or max(core) >= geometry.atom_count:
        raise ValueError("core indices must identify at least three valid atoms")
    graph = _bond_graph(geometry, covalent_scale=covalent_scale)
    detected: list[RotatableBond] = []
    for first, neighbors in enumerate(graph):
        for second in neighbors:
            if first >= second or first in core and second in core:
                continue
            pair = frozenset((geometry.atoms[first].element, geometry.atoms[second].element))
            if pair not in (frozenset(("C",)), frozenset(("C", "N"))):
                continue
            if len(graph[first]) < 2 or len(graph[second]) < 2:
                continue
            component_first = _component_without_edge(graph, first, (first, second))
            if second in component_first:
                continue
            component_second = tuple(
                index for index in range(geometry.atom_count) if index not in component_first
            )
            first_has_core = bool(core.intersection(component_first))
            second_has_core = bool(core.intersection(component_second))
            if first_has_core == second_has_core:
                continue
            if first_has_core:
                fixed, rotating, moving = first, second, component_second
            else:
                fixed, rotating, moving = second, first, component_first
            if geometry.atoms[fixed].element == "N":
                continue
            reference = (
                _reference_neighbor(geometry, graph, fixed, rotating),
                fixed,
                rotating,
                _reference_neighbor(geometry, graph, rotating, fixed),
            )
            classification = (
                "core_peripheral_aryl"
                if fixed in core
                else "amino_orientation"
                if geometry.atoms[rotating].element == "N"
                else "peripheral_carbon_bridge"
            )
            depth = _graph_depth(graph, fixed, core)
            detected.append(
                RotatableBond(
                    torsion_id="",
                    fragment_id=fragment_id,
                    fixed_atom=fixed,
                    rotating_atom=rotating,
                    rotating_atoms=tuple(sorted(moving)),
                    reference_atoms=reference,
                    classification=classification,
                    core_graph_depth=depth,
                )
            )
    ordered = sorted(
        detected,
        key=lambda item: (
            item.core_graph_depth,
            item.fixed_atom,
            item.rotating_atom,
            item.classification,
        ),
    )
    return tuple(
        RotatableBond(
            torsion_id=f"{fragment_id}_torsion_{position:02d}",
            fragment_id=item.fragment_id,
            fixed_atom=item.fixed_atom,
            rotating_atom=item.rotating_atom,
            rotating_atoms=item.rotating_atoms,
            reference_atoms=item.reference_atoms,
            classification=item.classification,
            core_graph_depth=item.core_graph_depth,
        )
        for position, item in enumerate(ordered, start=1)
    )


def offset_rotatable_bond(torsion: RotatableBond, offset: int) -> RotatableBond:
    """Translate a monomer-local torsion definition into dimer indices."""

    value = int(offset)
    return RotatableBond(
        torsion_id=torsion.torsion_id,
        fragment_id=torsion.fragment_id,
        fixed_atom=torsion.fixed_atom + value,
        rotating_atom=torsion.rotating_atom + value,
        rotating_atoms=tuple(index + value for index in torsion.rotating_atoms),
        reference_atoms=tuple(  # type: ignore[arg-type]
            index + value for index in torsion.reference_atoms
        ),
        classification=torsion.classification,
        core_graph_depth=torsion.core_graph_depth,
    )


def _contact_thresholds(first: str, second: str) -> tuple[float, float]:
    try:
        covalent_sum = COVALENT_RADII_ANGSTROM[first] + COVALENT_RADII_ANGSTROM[second]
        vdw_sum = VDW_RADII_ANGSTROM[first] + VDW_RADII_ANGSTROM[second]
    except KeyError as exc:
        raise ValueError(f"no contact radii are configured for element {exc.args[0]}") from exc
    pair = frozenset((first, second))
    hydrogen_bond_pair = pair in (frozenset(("H", "O")), frozenset(("H", "N")))
    if hydrogen_bond_pair:
        return max(0.82 * covalent_sum, 0.45 * vdw_sum), 0.58 * vdw_sum
    if "H" in pair:
        return max(0.90 * covalent_sum, 0.55 * vdw_sum), 0.72 * vdw_sum
    if pair == frozenset(("C",)):
        return max(1.08 * covalent_sum, 0.62 * vdw_sum), 0.80 * vdw_sum
    return max(1.02 * covalent_sum, 0.60 * vdw_sum), 0.78 * vdw_sum


def classify_interfragment_contacts(
    geometry: XYZGeometry,
    *,
    fragments: Mapping[str, Sequence[int]],
) -> tuple[ContactRecord, ...]:
    """Classify every interfragment contact using element-aware thresholds."""

    if len(fragments) != 2:
        raise ValueError("contact analysis requires exactly two fragments")
    values = tuple(tuple(int(index) for index in item) for item in fragments.values())
    flattened = [index for fragment in values for index in fragment]
    if (
        any(not fragment for fragment in values)
        or len(flattened) != len(set(flattened))
        or set(flattened) != set(range(geometry.atom_count))
    ):
        raise ValueError("fragment definitions must partition every atom exactly once")
    records: list[ContactRecord] = []
    for atom_a in values[0]:
        first = geometry.atoms[atom_a]
        for atom_b in values[1]:
            second = geometry.atoms[atom_b]
            distance = _distance(first.coordinates, second.coordinates)
            hard, soft = _contact_thresholds(first.element, second.element)
            pair = frozenset((first.element, second.element))
            if distance < hard:
                classification = "hard_clash"
            elif pair in (
                frozenset(("H", "O")),
                frozenset(("H", "N")),
            ) and distance <= 2.5:
                classification = "plausible_hydrogen_bond"
            elif distance < soft:
                classification = "soft_overlap"
            else:
                classification = "contact"
            records.append(
                ContactRecord(
                    atom_a,
                    first.element,
                    atom_b,
                    second.element,
                    distance,
                    hard,
                    soft,
                    classification,
                )
            )
    return tuple(
        sorted(records, key=lambda item: (item.distance_angstrom, item.atom_a, item.atom_b))
    )


def _rotate_geometry(
    geometry: XYZGeometry, torsion: RotatableBond, angle_degrees: float
) -> XYZGeometry:
    if abs(angle_degrees) <= 1.0e-14:
        return geometry
    fixed = geometry.atoms[torsion.fixed_atom].coordinates
    rotating = geometry.atoms[torsion.rotating_atom].coordinates
    axis = tuple(rotating[index] - fixed[index] for index in range(3))
    length = math.sqrt(sum(value * value for value in axis))
    if length <= 1.0e-12:
        raise ValueError("rotatable bond has zero length")
    unit = tuple(value / length for value in axis)
    angle = math.radians(angle_degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    selected = set(torsion.rotating_atoms)
    atoms: list[Atom] = []
    for index, atom in enumerate(geometry.atoms):
        if index not in selected:
            atoms.append(atom)
            continue
        vector = tuple(atom.coordinates[item] - fixed[item] for item in range(3))
        cross = (
            unit[1] * vector[2] - unit[2] * vector[1],
            unit[2] * vector[0] - unit[0] * vector[2],
            unit[0] * vector[1] - unit[1] * vector[0],
        )
        projection = sum(unit[item] * vector[item] for item in range(3))
        rotated = tuple(
            vector[item] * cosine
            + cross[item] * sine
            + unit[item] * projection * (1.0 - cosine)
            + fixed[item]
            for item in range(3)
        )
        atoms.append(Atom(atom.element, *rotated))
    return XYZGeometry(tuple(atoms), geometry.comment)


def apply_torsion_angles(
    geometry: XYZGeometry,
    torsions: Sequence[RotatableBond],
    angles_degrees: Sequence[float],
) -> XYZGeometry:
    """Apply ordered torsion deltas while retaining every non-moving atom."""

    if len(torsions) != len(angles_degrees):
        raise ValueError("torsion and angle counts differ")
    result = geometry
    for torsion, angle in zip(torsions, angles_degrees, strict=True):
        result = _rotate_geometry(result, torsion, float(angle))
    return result


def _radical_inverse(index: int, base: int) -> float:
    result = 0.0
    scale = 1.0 / base
    value = index
    while value:
        value, digit = divmod(value, base)
        result += digit * scale
        scale /= base
    return result


def _normalize_angle(value: float) -> float:
    result = (float(value) + 180.0) % 360.0 - 180.0
    return 180.0 if math.isclose(result, -180.0, abs_tol=1.0e-12) else result


def _candidate_angles(
    candidate: int, torsion_count: int, grid: Sequence[float]
) -> tuple[float, ...]:
    if candidate == 0:
        return (0.0,) * torsion_count
    if torsion_count > len(_PRIMES):
        raise ValueError("torsion count exceeds deterministic sequence dimensions")
    return tuple(
        float(
            grid[
                min(
                    int(_radical_inverse(candidate, _PRIMES[index]) * len(grid)),
                    len(grid) - 1,
                )
            ]
        )
        for index in range(torsion_count)
    )


def _evaluate_candidate(
    original: XYZGeometry,
    geometry: XYZGeometry,
    *,
    fragments: Mapping[str, Sequence[int]],
    core_indices: set[int],
    angles: Sequence[float],
    protocol: TorsionSearchProtocol,
    reference_fragment_bonds: Sequence[set[tuple[int, int]]],
) -> tuple[
    tuple[ContactRecord, ...],
    tuple[int, float, float, tuple[float, ...]],
    float | None,
    int,
]:
    contacts = classify_interfragment_contacts(geometry, fragments=fragments)
    clashes = tuple(item for item in contacts if item.classification == "hard_clash")
    worst_overlap = max(
        (item.hard_threshold_angstrom - item.distance_angstrom for item in clashes),
        default=0.0,
    )
    if clashes:
        guidance = (
            len(clashes),
            worst_overlap,
            -contacts[0].distance_angstrom,
            tuple(_normalize_angle(value) for value in angles),
        )
        return contacts, guidance, None, 0
    connectivity_changes = sum(
        _fragment_connectivity_change_count(
            geometry,
            tuple(int(index) for index in fragment),
            reference,
            preserved_bond_scale=max(1.35, protocol.covalent_bond_scale),
            new_bond_scale=1.05,
        )
        for fragment, reference in zip(
            fragments.values(), reference_fragment_bonds, strict=True
        )
    )
    guidance = (
        connectivity_changes,
        0.0,
        -contacts[0].distance_angstrom,
        tuple(_normalize_angle(value) for value in angles),
    )
    if connectivity_changes:
        return contacts, guidance, None, connectivity_changes
    soft_penalty = sum(
        (
            (item.soft_threshold_angstrom - item.distance_angstrom)
            / item.soft_threshold_angstrom
        )
        ** 2
        for item in contacts
        if item.classification == "soft_overlap"
    )
    torsion_penalty = sum((abs(_normalize_angle(value)) / 180.0) ** 2 for value in angles)
    movable = [index for index in range(original.atom_count) if index not in core_indices]
    displacement = math.sqrt(
        sum(
            _distance(original.atoms[index].coordinates, geometry.atoms[index].coordinates) ** 2
            for index in movable
        )
        / max(1, len(movable))
    )
    score = (
        protocol.soft_overlap_weight * soft_penalty
        + protocol.torsion_displacement_weight * torsion_penalty
        + protocol.peripheral_displacement_weight * displacement
    )
    return contacts, guidance, score, 0


def _fragment_bonds(
    geometry: XYZGeometry,
    indices: Sequence[int],
    *,
    covalent_scale: float,
) -> set[tuple[int, int]]:
    selected = tuple(int(index) for index in indices)
    bonds: set[tuple[int, int]] = set()
    for position, first in enumerate(selected):
        atom_a = geometry.atoms[first]
        for second in selected[position + 1 :]:
            atom_b = geometry.atoms[second]
            if _distance(atom_a.coordinates, atom_b.coordinates) <= covalent_scale * (
                COVALENT_RADII_ANGSTROM[atom_a.element]
                + COVALENT_RADII_ANGSTROM[atom_b.element]
            ):
                bonds.add((first, second))
    return bonds


def _fragment_connectivity_change_count(
    geometry: XYZGeometry,
    indices: Sequence[int],
    reference_bonds: set[tuple[int, int]],
    *,
    preserved_bond_scale: float,
    new_bond_scale: float,
) -> int:
    """Count broken reference bonds and new covalent-length contacts.

    A single generous graph cutoff is unsuitable for both decisions: it would
    classify ordinary close nonbonded contacts as new bonds during torsion
    motion. Existing bonds therefore receive a tolerant upper bound, while a
    previously nonbonded pair is rejected only after entering a much stricter
    covalent-length range.
    """

    selected = tuple(int(index) for index in indices)
    changes = 0
    for position, first in enumerate(selected):
        atom_a = geometry.atoms[first]
        for second in selected[position + 1 :]:
            atom_b = geometry.atoms[second]
            covalent_sum = (
                COVALENT_RADII_ANGSTROM[atom_a.element]
                + COVALENT_RADII_ANGSTROM[atom_b.element]
            )
            distance = _distance(atom_a.coordinates, atom_b.coordinates)
            pair = (first, second)
            if pair in reference_bonds:
                changes += distance > preserved_bond_scale * covalent_sum
            else:
                changes += distance < new_bond_scale * covalent_sum
    return changes


def deterministic_torsion_search(
    geometry: XYZGeometry,
    *,
    fragments: Mapping[str, Sequence[int]],
    core_indices: Mapping[str, Sequence[int]],
    torsions: Sequence[RotatableBond],
    protocol: TorsionSearchProtocol = TorsionSearchProtocol(),
) -> TorsionSearchResult:
    """Search a bounded deterministic sequence and select a hard-valid geometry."""

    if tuple(fragments) != tuple(core_indices):
        raise ValueError("fragment and core identities must match in deterministic order")
    all_core = {int(index) for values in core_indices.values() for index in values}
    if any(all_core.intersection(torsion.rotating_atoms) for torsion in torsions):
        raise ValueError("rotatable atom sets must exclude every configured core atom")
    ordered = tuple(torsions)
    reference_fragment_bonds = tuple(
        _fragment_bonds(
            geometry,
            tuple(int(index) for index in fragment),
            covalent_scale=protocol.covalent_bond_scale,
        )
        for fragment in fragments.values()
    )
    records: list[Mapping[str, object]] = []
    best: tuple[float, tuple[float, ...], int, XYZGeometry, tuple[ContactRecord, ...]] | None = None
    best_invalid: tuple[
        tuple[int, float, float, tuple[float, ...]], tuple[float, ...]
    ] | None = None

    def consider(
        candidate_id: int, angles: tuple[float, ...], phase: str
    ) -> tuple[object, ...]:
        nonlocal best, best_invalid
        candidate_geometry = apply_torsion_angles(geometry, ordered, angles)
        contacts, guidance, score, connectivity_changes = _evaluate_candidate(
            geometry,
            candidate_geometry,
            fragments=fragments,
            core_indices=all_core,
            angles=angles,
            protocol=protocol,
            reference_fragment_bonds=reference_fragment_bonds,
        )
        clashes = [item for item in contacts if item.classification == "hard_clash"]
        record: dict[str, object] = {
            "candidate_id": candidate_id,
            "phase": phase,
            "angles_degrees": list(angles),
            "hard_valid": score is not None,
            "hard_clash_count": len(clashes),
            "connectivity_change_count": connectivity_changes,
            "minimum_interfragment_distance_angstrom": contacts[0].distance_angstrom,
            "score": score,
        }
        records.append(record)
        if score is None:
            invalid_key = (guidance, angles)
            if best_invalid is None or invalid_key < best_invalid:
                best_invalid = invalid_key
            return (1, guidance, angles, candidate_id)
        key = (score, tuple(_normalize_angle(value) for value in angles), candidate_id)
        if best is None or key < (best[0], best[1], best[2]):
            best = (score, key[1], candidate_id, candidate_geometry, contacts)
        return (0, score, key[1], candidate_id)

    coarse_candidates: list[tuple[tuple[object, ...], tuple[float, ...]]] = []
    for candidate_id in range(protocol.coarse_candidate_count):
        angles = _candidate_angles(
            candidate_id, len(ordered), protocol.angle_grid_degrees
        )
        rank = consider(
            candidate_id,
            angles,
            "coarse_low_discrepancy",
        )
        coarse_candidates.append((rank, angles))

    if best is None:
        assert best_invalid is not None
        current = best_invalid[1]
    else:
        current = best[1]
    next_id = len(records)
    def active_torsions(angles: tuple[float, ...]) -> tuple[int, ...]:
        candidate_geometry = apply_torsion_angles(geometry, ordered, angles)
        contacts = classify_interfragment_contacts(
            candidate_geometry, fragments=fragments
        )
        clashing_atoms = {
            atom
            for contact in contacts
            if contact.classification == "hard_clash"
            for atom in (contact.atom_a, contact.atom_b)
        }
        selected = tuple(
            index
            for index, torsion in enumerate(ordered)
            if clashing_atoms.intersection(torsion.rotating_atoms)
        )
        return selected or tuple(range(len(ordered)))

    seeds: list[tuple[float, ...]] = []
    for _, angles in sorted(coarse_candidates, key=lambda item: item[0]):
        if angles not in seeds:
            seeds.append(angles)
        if len(seeds) >= protocol.refinement_seed_count:
            break
    for seed_index, seed in enumerate(seeds, start=1):
        local = seed
        for pass_index in range(protocol.fine_passes):
            for torsion_index in active_torsions(local):
                trial_angles = tuple(
                    dict.fromkeys(
                        (
                            local[torsion_index],
                            *protocol.angle_grid_degrees,
                            _normalize_angle(
                                local[torsion_index]
                                - protocol.fine_increment_degrees
                            ),
                            _normalize_angle(
                                local[torsion_index]
                                + protocol.fine_increment_degrees
                            ),
                        )
                    )
                )
                local_candidates: list[
                    tuple[tuple[object, ...], tuple[float, ...]]
                ] = []
                for angle in trial_angles:
                    refined = list(local)
                    refined[torsion_index] = _normalize_angle(angle)
                    refined_tuple = tuple(refined)
                    rank = consider(
                        next_id,
                        refined_tuple,
                        f"fine_seed_{seed_index}_pass_{pass_index + 1}",
                    )
                    local_candidates.append((rank, refined_tuple))
                    next_id += 1
                local = min(local_candidates, key=lambda item: item[0])[1]

    if best is not None:
        current = best[1]
    elif best_invalid is not None:
        current = best_invalid[1]

    pair_deltas = (
        -2.0 * protocol.fine_increment_degrees,
        -protocol.fine_increment_degrees,
        protocol.fine_increment_degrees,
        2.0 * protocol.fine_increment_degrees,
    )
    for pass_index in range(protocol.pair_refinement_passes):
        active = active_torsions(current)
        for position, first in enumerate(active):
            for second in active[position + 1 :]:
                for delta_first in pair_deltas:
                    for delta_second in pair_deltas:
                        refined = list(current)
                        refined[first] = _normalize_angle(
                            refined[first] + delta_first
                        )
                        refined[second] = _normalize_angle(
                            refined[second] + delta_second
                        )
                        consider(
                            next_id,
                            tuple(refined),
                            f"pair_refinement_{pass_index + 1}",
                        )
                        next_id += 1
                if best is not None:
                    current = best[1]
                elif best_invalid is not None:
                    current = best_invalid[1]

    if best is None:
        raise ValueError(
            "deterministic torsion search found no hard-valid candidate; "
            f"best remaining violation count is "
            f"{best_invalid[0][0] if best_invalid else 'unknown'}; "
            f"best angles are {best_invalid[1] if best_invalid else 'unknown'}"
        )
    score, angles, candidate_id, selected_geometry, contacts = best
    return TorsionSearchResult(
        selected_geometry,
        ordered,
        angles,
        candidate_id,
        score,
        contacts,
        tuple(records),
        protocol,
    )


__all__ = [
    "ContactRecord",
    "RotatableBond",
    "TorsionSearchProtocol",
    "TorsionSearchResult",
    "VDW_RADII_ANGSTROM",
    "apply_torsion_angles",
    "classify_interfragment_contacts",
    "deterministic_torsion_search",
    "identify_peripheral_rotatable_bonds",
    "offset_rotatable_bond",
]
