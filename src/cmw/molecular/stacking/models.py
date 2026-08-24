"""Molecule-agnostic structural models for vertical stacking workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Mapping, Sequence


Vector3 = tuple[float, float, float]
COMPONENTS = ("A", "B")


def vector3(value: Sequence[float], *, name: str) -> Vector3:
    selected = tuple(float(item) for item in value)
    if len(selected) != 3 or not all(math.isfinite(item) for item in selected):
        raise ValueError(f"{name} must contain three finite values")
    return selected  # type: ignore[return-value]


def add(first: Vector3, second: Vector3) -> Vector3:
    return tuple(a + b for a, b in zip(first, second))  # type: ignore[return-value]


def subtract(first: Vector3, second: Vector3) -> Vector3:
    return tuple(a - b for a, b in zip(first, second))  # type: ignore[return-value]


def scale(value: Vector3, factor: float) -> Vector3:
    return tuple(item * factor for item in value)  # type: ignore[return-value]


def dot(first: Vector3, second: Vector3) -> float:
    return sum(a * b for a, b in zip(first, second))


def cross(first: Vector3, second: Vector3) -> Vector3:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def norm(value: Vector3) -> float:
    return math.sqrt(dot(value, value))


def unit(value: Vector3, *, name: str) -> Vector3:
    length = norm(value)
    if not math.isfinite(length) or length <= 1.0e-12:
        raise ValueError(f"{name} cannot be a zero vector")
    return scale(value, 1.0 / length)


def centroid(points: Sequence[Vector3]) -> Vector3:
    if not points:
        raise ValueError("a centroid requires at least one point")
    count = float(len(points))
    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
        sum(point[2] for point in points) / count,
    )


def _smallest_covariance_axis(points: Sequence[Vector3], center: Vector3) -> Vector3:
    """Return the smallest-eigenvalue axis of a symmetric 3x3 covariance matrix."""

    matrix = [[0.0] * 3 for _ in range(3)]
    for point in points:
        delta = subtract(point, center)
        for row in range(3):
            for column in range(3):
                matrix[row][column] += delta[row] * delta[column]
    vectors = [[1.0 if row == column else 0.0 for column in range(3)] for row in range(3)]
    for _ in range(32):
        first, second = max(
            ((0, 1), (0, 2), (1, 2)),
            key=lambda pair: abs(matrix[pair[0]][pair[1]]),
        )
        off_diagonal = matrix[first][second]
        if abs(off_diagonal) <= 1.0e-14:
            break
        angle = 0.5 * math.atan2(
            2.0 * off_diagonal,
            matrix[second][second] - matrix[first][first],
        )
        cosine, sine = math.cos(angle), math.sin(angle)
        for index in range(3):
            if index in (first, second):
                continue
            left = matrix[index][first]
            right = matrix[index][second]
            matrix[index][first] = matrix[first][index] = cosine * left - sine * right
            matrix[index][second] = matrix[second][index] = sine * left + cosine * right
        left = matrix[first][first]
        right = matrix[second][second]
        matrix[first][first] = (
            cosine * cosine * left
            - 2.0 * sine * cosine * off_diagonal
            + sine * sine * right
        )
        matrix[second][second] = (
            sine * sine * left
            + 2.0 * sine * cosine * off_diagonal
            + cosine * cosine * right
        )
        matrix[first][second] = matrix[second][first] = 0.0
        for index in range(3):
            left = vectors[index][first]
            right = vectors[index][second]
            vectors[index][first] = cosine * left - sine * right
            vectors[index][second] = sine * left + cosine * right
    selected = min(range(3), key=lambda index: matrix[index][index])
    return unit(
        tuple(vectors[row][selected] for row in range(3)),  # type: ignore[arg-type]
        name="core plane normal",
    )


@dataclass(frozen=True)
class CoreFrame:
    centroid: Vector3
    x_axis: Vector3
    y_axis: Vector3
    normal: Vector3


def core_frame(points: Sequence[Vector3]) -> CoreFrame:
    """Build a deterministic right-handed frame from an ordered generic core."""

    if len(points) < 3:
        raise ValueError("a stacking core requires at least three mapped atoms")
    center = centroid(points)
    normal = _smallest_covariance_axis(points, center)
    x_axis: Vector3 | None = None
    x_source: Vector3 | None = None
    for point in points[1:]:
        candidate = subtract(point, points[0])
        projected = subtract(candidate, scale(normal, dot(candidate, normal)))
        if norm(projected) > 1.0e-10:
            x_axis = unit(projected, name="ordered core x axis")
            x_source = candidate
            break
    if x_axis is None or x_source is None:
        raise ValueError("ordered core atoms do not define an in-plane direction")
    orientation: Vector3 | None = None
    for point in points[2:]:
        candidate = cross(x_source, subtract(point, points[0]))
        if norm(candidate) > 1.0e-10:
            orientation = candidate
            break
    if orientation is None:
        raise ValueError("stacking core atoms are collinear")
    if dot(normal, orientation) < 0:
        normal = scale(normal, -1.0)
    y_axis = unit(cross(normal, x_axis), name="ordered core y axis")
    return CoreFrame(center, x_axis, y_axis, normal)


@dataclass(frozen=True)
class PeriodicPairDefinition:
    """Explicit zero-based molecular and core partitions inside a periodic source."""

    molecule_identities: tuple[str, str]
    molecule_atom_indices: Mapping[str, tuple[int, ...]]
    core_atom_indices: Mapping[str, tuple[int, ...]]

    def __post_init__(self) -> None:
        identities = tuple(str(item).strip() for item in self.molecule_identities)
        if len(identities) != 2 or not all(identities):
            raise ValueError("molecular pair identity requires two non-empty values")
        molecules = {
            component: tuple(int(index) for index in self.molecule_atom_indices.get(component, ()))
            for component in COMPONENTS
        }
        cores = {
            component: tuple(int(index) for index in self.core_atom_indices.get(component, ()))
            for component in COMPONENTS
        }
        for component in COMPONENTS:
            atoms = molecules[component]
            core = cores[component]
            if not atoms or len(set(atoms)) != len(atoms) or min(atoms) < 0:
                raise ValueError(f"component {component} requires unique zero-based atom indices")
            if len(core) < 3 or len(set(core)) != len(core) or min(core) < 0:
                raise ValueError(
                    f"component {component} core requires at least three unique indices"
                )
            if not set(core).issubset(atoms):
                raise ValueError(f"component {component} core mapping is outside its molecule")
        if set(molecules["A"]) & set(molecules["B"]):
            raise ValueError("periodic molecular pair atom partitions overlap")
        object.__setattr__(self, "molecule_identities", identities)
        object.__setattr__(self, "molecule_atom_indices", molecules)
        object.__setattr__(self, "core_atom_indices", cores)

    def local_core_indices(self, component: str) -> tuple[int, ...]:
        atoms = self.molecule_atom_indices[component]
        positions = {index: position for position, index in enumerate(atoms)}
        return tuple(positions[index] for index in self.core_atom_indices[component])

    def to_dict(self) -> dict[str, object]:
        return {
            "indexing": "zero_based",
            "molecule_identities": list(self.molecule_identities),
            "molecule_atom_indices": {
                key: list(value) for key, value in self.molecule_atom_indices.items()
            },
            "core_atom_indices": {
                key: list(value) for key, value in self.core_atom_indices.items()
            },
            "local_core_atom_indices": {
                component: list(self.local_core_indices(component))
                for component in COMPONENTS
            },
        }


@dataclass(frozen=True)
class StackingGeometry:
    stacking_axis: Vector3
    plane_separation_angstrom: float
    centroid_separation_angstrom: float
    slip_vector_angstrom: Vector3
    rotation_degrees: float

    def __post_init__(self) -> None:
        axis = unit(vector3(self.stacking_axis, name="stacking axis"), name="stacking axis")
        slip = vector3(self.slip_vector_angstrom, name="slip vector")
        plane = float(self.plane_separation_angstrom)
        separation = float(self.centroid_separation_angstrom)
        rotation = float(self.rotation_degrees)
        values = (plane, separation, rotation)
        if not all(math.isfinite(float(item)) for item in values):
            raise ValueError("stacking geometry values must be finite")
        if plane <= 0:
            raise ValueError("stacking plane separation must be positive")
        if separation + 1.0e-10 < plane:
            raise ValueError("centroid separation cannot be shorter than plane separation")
        if abs(slip[2]) > 1.0e-8:
            raise ValueError("local stacking slip vector must lie in the core plane")
        reconstructed = math.sqrt(
            plane * plane + slip[0] * slip[0] + slip[1] * slip[1]
        )
        if not math.isclose(separation, reconstructed, abs_tol=1.0e-7):
            raise ValueError(
                "centroid separation is inconsistent with plane separation and slip"
            )
        object.__setattr__(self, "stacking_axis", axis)
        object.__setattr__(self, "slip_vector_angstrom", slip)
        object.__setattr__(self, "plane_separation_angstrom", plane)
        object.__setattr__(self, "centroid_separation_angstrom", separation)
        object.__setattr__(self, "rotation_degrees", rotation)

    def to_dict(self) -> dict[str, object]:
        return {
            "stacking_axis": list(self.stacking_axis),
            "plane_separation_angstrom": self.plane_separation_angstrom,
            "centroid_separation_angstrom": self.centroid_separation_angstrom,
            "slip_vector_angstrom": list(self.slip_vector_angstrom),
            "rotation_degrees": self.rotation_degrees,
        }


@dataclass(frozen=True)
class FixedRegion:
    region_id: str
    atom_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.region_id.strip():
            raise ValueError("fixed region id is required")
        indices = tuple(int(index) for index in self.atom_indices)
        if not indices or min(indices) < 0 or len(set(indices)) != len(indices):
            raise ValueError("fixed regions require unique zero-based atom indices")
        object.__setattr__(self, "atom_indices", indices)

    def to_dict(self) -> dict[str, object]:
        return {"region_id": self.region_id, "atom_indices": list(self.atom_indices)}


@dataclass(frozen=True)
class RelaxationProtocol:
    fixed_regions: tuple[FixedRegion, ...]
    preserve_stacking_registry: bool = True
    full_relaxation: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.preserve_stacking_registry, bool):
            raise ValueError("preserve_stacking_registry must be a boolean")
        if not isinstance(self.full_relaxation, bool):
            raise ValueError("full_relaxation must be a boolean")
        regions = tuple(self.fixed_regions)
        if not regions:
            raise ValueError("constrained relaxation requires at least one fixed region")
        if len({region.region_id for region in regions}) != len(regions):
            raise ValueError("fixed region identities must be unique")
        supplied = [index for region in regions for index in region.atom_indices]
        if len(set(supplied)) != len(supplied):
            raise ValueError("fixed regions must not overlap")
        object.__setattr__(self, "fixed_regions", regions)

    def to_dict(self) -> dict[str, object]:
        return {
            "constrained": {
                "indexing": "zero_based",
                "fixed_regions": [region.to_dict() for region in self.fixed_regions],
                "preserve_stacking_registry": self.preserve_stacking_registry,
            },
            "full": {"enabled": self.full_relaxation},
        }


@dataclass(frozen=True)
class GroundStateProtocol:
    method: str
    basis: str
    frequency: bool = False
    keywords: tuple[str, ...] = ()
    protocol: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.method.strip() or not self.basis.strip():
            raise ValueError("ground-state method and basis are required")
        if any("\n" in item or "\r" in item for item in (self.method, self.basis, *self.keywords)):
            raise ValueError("ORCA keywords must be single-line values")
        object.__setattr__(self, "keywords", tuple(self.keywords))
        object.__setattr__(self, "protocol", dict(self.protocol))

    @property
    def keyword_line(self) -> str:
        return " ".join((self.method, self.basis, *self.keywords))

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


__all__ = [
    "COMPONENTS",
    "CoreFrame",
    "FixedRegion",
    "GroundStateProtocol",
    "PeriodicPairDefinition",
    "RelaxationProtocol",
    "StackingGeometry",
    "Vector3",
    "add",
    "centroid",
    "core_frame",
    "cross",
    "dot",
    "norm",
    "scale",
    "subtract",
    "unit",
    "vector3",
]
