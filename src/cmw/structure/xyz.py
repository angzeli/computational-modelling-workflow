"""Strict, dependency-free XYZ geometry parsing and stable identity."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ELEMENTS = frozenset(
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni "
    "Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe "
    "Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg "
    "Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg "
    "Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split()
)


@dataclass(frozen=True)
class Atom:
    """One atomic symbol and Cartesian coordinate in ångström."""

    element: str
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        ghost_match = self.element.startswith("Gh(") and self.element.endswith(")")
        ghost_element = self.element[3:-1] if ghost_match else ""
        if self.element not in ELEMENTS and ghost_element not in ELEMENTS:
            raise ValueError(f"invalid or unsupported XYZ element symbol: {self.element!r}")
        if not all(math.isfinite(value) for value in self.coordinates):
            raise ValueError("XYZ coordinates must be finite")

    @property
    def coordinates(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    @property
    def ghost(self) -> bool:
        return self.element.startswith("Gh")


@dataclass(frozen=True)
class XYZGeometry:
    """Ordered, zero-indexed atoms with Cartesian coordinates in ångström."""

    atoms: tuple[Atom, ...]
    comment: str = ""

    def __post_init__(self) -> None:
        if not self.atoms:
            raise ValueError("XYZ geometry must contain at least one atom")
        if "\n" in self.comment or "\r" in self.comment:
            raise ValueError("XYZ comment must be a single line")

    @property
    def atom_count(self) -> int:
        return len(self.atoms)

    @property
    def elements(self) -> tuple[str, ...]:
        return tuple(atom.element for atom in self.atoms)

    @property
    def coordinates(self) -> tuple[tuple[float, float, float], ...]:
        return tuple(atom.coordinates for atom in self.atoms)


def parse_xyz(text: str, *, source: str = "<text>") -> XYZGeometry:
    """Parse exactly one strict XYZ record; malformed input is never guessed."""

    lines = text.splitlines()
    if len(lines) < 2:
        raise ValueError(f"malformed XYZ in {source}: atom count and comment are required")
    try:
        atom_count = int(lines[0].strip())
    except ValueError as exc:
        raise ValueError(f"invalid XYZ atom count in {source}") from exc
    if atom_count < 1:
        raise ValueError(f"invalid XYZ atom count in {source}: {atom_count}")
    if len(lines) != atom_count + 2:
        raise ValueError(
            f"XYZ atom-count mismatch in {source}: declared {atom_count}, "
            f"found {len(lines) - 2} coordinate rows"
        )

    atoms: list[Atom] = []
    for line_number, line in enumerate(lines[2:], start=3):
        fields = line.split()
        if len(fields) != 4:
            raise ValueError(
                f"malformed XYZ row {line_number} in {source}: expected symbol and 3 coordinates"
            )
        symbol = fields[0]
        if not symbol.startswith("Gh"):
            symbol = symbol[0].upper() + symbol[1:].lower()
        try:
            atoms.append(Atom(symbol, *(float(value.replace("D", "E").replace("d", "e")) for value in fields[1:])))
        except ValueError as exc:
            raise ValueError(f"invalid XYZ row {line_number} in {source}: {exc}") from exc
    return XYZGeometry(tuple(atoms), lines[1])


def read_xyz(path: Path) -> XYZGeometry:
    """Read one strict UTF-8 XYZ geometry."""

    return parse_xyz(path.read_text(encoding="utf-8", errors="strict"), source=str(path))


def format_xyz(geometry: XYZGeometry, *, comment: str | None = None) -> str:
    """Serialize with deterministic coordinates while retaining atom order."""

    selected_comment = geometry.comment if comment is None else comment
    if "\n" in selected_comment or "\r" in selected_comment:
        raise ValueError("XYZ comment must be a single line")
    rows = [str(geometry.atom_count), selected_comment]
    rows.extend(
        f"{atom.element:<4s} {atom.x: .12f} {atom.y: .12f} {atom.z: .12f}"
        for atom in geometry.atoms
    )
    return "\n".join(rows) + "\n"


def write_xyz(path: Path, geometry: XYZGeometry, *, comment: str | None = None) -> None:
    """Write an XYZ geometry atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(format_xyz(geometry, comment=comment))
    try:
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def geometry_identity_data(geometry: XYZGeometry) -> dict[str, object]:
    """Return the scientific geometry identity, excluding comment/charge/spin."""

    return {
        "schema_version": 1,
        "units": "angstrom",
        "indexing": "zero_based",
        "atoms": [
            {"element": atom.element, "coordinates": list(atom.coordinates)}
            for atom in geometry.atoms
        ],
    }


def geometry_hash(geometry: XYZGeometry) -> str:
    """Hash ordered symbols and exact parsed float values, not display formatting."""

    payload = json.dumps(
        geometry_identity_data(geometry), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def geometry_from_rows(
    rows: Iterable[tuple[str, float, float, float]], *, comment: str = ""
) -> XYZGeometry:
    """Build a validated geometry from ordered rows."""

    return XYZGeometry(tuple(Atom(*row) for row in rows), comment)
