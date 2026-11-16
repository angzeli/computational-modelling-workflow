"""Minimal structural validation for Gaussian cube artifacts."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from cmw.structure.xyz import ELEMENTS, XYZGeometry


BOHR_TO_ANGSTROM = 0.529177210903
ELEMENT_ORDER = tuple(
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni "
    "Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe "
    "Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg "
    "Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg "
    "Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split()
)
assert set(ELEMENT_ORDER) == set(ELEMENTS)


@dataclass(frozen=True)
class CubeAtom:
    atomic_number: int
    coordinates_angstrom: tuple[float, float, float]


@dataclass(frozen=True)
class CubeMetadata:
    atom_count: int
    dimensions: tuple[int, int, int]
    origin_angstrom: tuple[float, float, float]
    vectors_angstrom: tuple[tuple[float, float, float], ...]
    atoms: tuple[CubeAtom, ...]
    value_count: int
    # Header values exactly as written, with their unit ("bohr" when the grid
    # counts are positive), so requested-grid checks avoid unit round trips.
    header_unit: str = "bohr"
    origin_header: tuple[float, float, float] = (0.0, 0.0, 0.0)
    vectors_header: tuple[tuple[float, float, float], ...] = ()


def _numbers(line: str, count: int, *, label: str) -> list[float]:
    fields = line.split()
    if len(fields) < count:
        raise ValueError(f"malformed cube {label}")
    try:
        values = [float(value.replace("D", "E").replace("d", "e")) for value in fields[:count]]
    except ValueError as exc:
        raise ValueError(f"non-numeric cube {label}") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"non-finite cube {label}")
    return values


def read_cube(path: Path) -> CubeMetadata:
    return scan_cube(path)


def scan_cube(
    path: Path,
    visit: Callable[[int, float], None] | None = None,
    *,
    on_header: Callable[[CubeMetadata], None] | None = None,
) -> CubeMetadata:
    """Validate a cube in one streaming pass, optionally visiting each value.

    Values are never all held in memory; ``visit(index, value)`` sees them in
    file order (z fastest, then y, then x). ``on_header`` receives the header
    metadata (``value_count`` 0) before the first value.
    """
    with path.open(encoding="utf-8", errors="strict") as handle:
        header = [handle.readline() for _ in range(6)]
        if not header[-1]:
            raise ValueError(f"truncated cube header: {path}")
        atom_header = _numbers(header[2], 4, label="atom/origin row")
        if not atom_header[0].is_integer():
            raise ValueError("cube atom count must be an integer")
        signed_atoms = int(atom_header[0])
        atom_count = abs(signed_atoms)
        if atom_count < 1:
            raise ValueError("cube must contain at least one atom")
        grid_rows = [_numbers(header[index], 4, label="grid row") for index in range(3, 6)]
        if any(not row[0].is_integer() for row in grid_rows):
            raise ValueError("cube grid dimensions must be integers")
        signs = {1 if row[0] > 0 else -1 for row in grid_rows}
        if 0 in {row[0] for row in grid_rows} or len(signs) != 1:
            raise ValueError("cube grid dimensions must be non-zero with consistent units")
        scale = BOHR_TO_ANGSTROM if next(iter(signs)) > 0 else 1.0
        dimensions = tuple(abs(int(row[0])) for row in grid_rows)
        origin = tuple(value * scale for value in atom_header[1:4])
        vectors = tuple(tuple(value * scale for value in row[1:4]) for row in grid_rows)
        atoms: list[CubeAtom] = []
        for _ in range(atom_count):
            line = handle.readline()
            if not line:
                raise ValueError(f"truncated cube header: {path}")
            row = _numbers(line, 5, label="atom row")
            if not row[0].is_integer():
                raise ValueError("cube atomic numbers must be integers")
            number = int(row[0])
            if number < 1 or number > len(ELEMENT_ORDER):
                raise ValueError(f"invalid cube atomic number: {number}")
            atoms.append(CubeAtom(number, tuple(value * scale for value in row[2:5])))
        if signed_atoms < 0:
            descriptor = handle.readline().split()
            if not descriptor:
                raise ValueError("truncated orbital cube descriptor")
            try:
                orbital_count = int(descriptor[0])
            except ValueError as exc:
                raise ValueError("malformed orbital cube descriptor") from exc
            if len(descriptor) < orbital_count + 1:
                raise ValueError("truncated orbital cube descriptor")
        metadata = CubeMetadata(
            atom_count, dimensions, origin, vectors, tuple(atoms), 0,
            header_unit="bohr" if scale != 1.0 else "angstrom",
            origin_header=tuple(atom_header[1:4]),
            vectors_header=tuple(tuple(row[1:4]) for row in grid_rows),
        )
        if on_header is not None:
            on_header(metadata)
        expected = math.prod(dimensions)
        count = 0
        for line in handle:
            for value in _numbers(line, len(line.split()), label="scalar data"):
                if visit is not None and count < expected:
                    visit(count, value)
                count += 1
    if count != expected:
        raise ValueError(f"cube grid-value count mismatch: expected {expected}, found {count}")
    return replace(metadata, value_count=count)


def _close(values_a: Iterable[float], values_b: Iterable[float], tolerance: float) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(values_a, values_b, strict=True))


def validate_cube_geometry(
    cube: CubeMetadata, geometry: XYZGeometry, *, tolerance_angstrom: float = 1.0e-5
) -> None:
    if cube.atom_count != geometry.atom_count:
        raise ValueError("cube atom count does not match source geometry")
    for index, (cube_atom, atom) in enumerate(zip(cube.atoms, geometry.atoms, strict=True), start=1):
        expected_number = ELEMENT_ORDER.index(atom.element) + 1
        if cube_atom.atomic_number != expected_number:
            raise ValueError(f"cube atom identity differs at one-based index {index}")
        if not _close(cube_atom.coordinates_angstrom, atom.coordinates, tolerance_angstrom):
            raise ValueError(f"cube coordinates differ at one-based index {index}")


def validate_cube_compatibility(
    first: CubeMetadata,
    second: CubeMetadata,
    *,
    tolerance_angstrom: float = 1.0e-8,
) -> None:
    if first.dimensions != second.dimensions:
        raise ValueError("cube grid dimensions differ")
    if first.atom_count != second.atom_count:
        raise ValueError("cube atom counts differ")
    if not _close(first.origin_angstrom, second.origin_angstrom, tolerance_angstrom):
        raise ValueError("cube grid origins differ")
    for first_vector, second_vector in zip(first.vectors_angstrom, second.vectors_angstrom, strict=True):
        if not _close(first_vector, second_vector, tolerance_angstrom):
            raise ValueError("cube grid vectors differ")
    for first_atom, second_atom in zip(first.atoms, second.atoms, strict=True):
        if first_atom.atomic_number != second_atom.atomic_number or not _close(
            first_atom.coordinates_angstrom,
            second_atom.coordinates_angstrom,
            tolerance_angstrom,
        ):
            raise ValueError("cube atomic geometries differ")


# Multiwfn prints cube header coordinates with six decimals.
CUBE_HEADER_TOLERANCE_BOHR = 1.0e-6


def validate_cube_grid(
    cube: CubeMetadata,
    *,
    origin_bohr: tuple[float, float, float],
    spacing_bohr: float,
    dimensions: tuple[int, int, int],
    tolerance_bohr: float = CUBE_HEADER_TOLERANCE_BOHR,
) -> None:
    """Require the exact requested axis-aligned grid; never resample."""
    if cube.header_unit != "bohr":
        raise ValueError("cube grid is not expressed in bohr")
    if cube.dimensions != tuple(dimensions):
        raise ValueError(
            f"cube grid dimensions {cube.dimensions} differ from requested {tuple(dimensions)}"
        )
    if not _close(cube.origin_header, origin_bohr, tolerance_bohr):
        raise ValueError("cube grid origin differs from the requested origin")
    for axis, vector in enumerate(cube.vectors_header):
        expected = tuple(spacing_bohr if index == axis else 0.0 for index in range(3))
        if not _close(vector, expected, tolerance_bohr):
            raise ValueError("cube grid vectors differ from the requested spacing")
