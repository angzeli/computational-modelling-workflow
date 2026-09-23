"""Immutable ordered periodic geometry and source-expansion provenance indices."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re

import numpy as np

from cmw.core.provenance import stable_hash

from .xyz import ELEMENTS


IDENTITY_DECIMAL_PLACES = 12
CARTESIAN_TOLERANCE_ANGSTROM = 1e-8


def _validate_triple(value: tuple[float, float, float], name: str) -> None:
    if type(value) is not tuple or len(value) != 3:
        raise ValueError(f"{name} must be an immutable tuple of three coordinates")
    if any(type(item) not in (int, float) or not math.isfinite(item) for item in value):
        raise ValueError(f"{name} must contain finite numeric coordinates")


@dataclass(frozen=True, slots=True)
class PeriodicAtom:
    """One output-ordered atom; indices refer to source rows and expanded sites."""

    element: str
    fractional: tuple[float, float, float]
    cartesian: tuple[float, float, float]
    source_site_index: int
    expanded_index: int
    symmetry_operation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.element, str) or self.element not in ELEMENTS:
            raise ValueError("Periodic atom requires a recognized element")
        _validate_triple(self.fractional, "fractional")
        _validate_triple(self.cartesian, "cartesian")
        if any(type(index) is not int or index < 0
               for index in (self.source_site_index, self.expanded_index)):
            raise ValueError("Source-site and expanded indices must be nonnegative integers")
        if (type(self.symmetry_operation_ids) is not tuple or not self.symmetry_operation_ids
                or any(not isinstance(value, str) or not value for value in self.symmetry_operation_ids)
                or len(set(self.symmetry_operation_ids)) != len(self.symmetry_operation_ids)):
            raise ValueError("Symmetry operation IDs must be a nonempty tuple of distinct strings")

    def to_dict(self) -> dict[str, object]:
        """Return a detached JSON-compatible representation."""
        return {"element": self.element, "fractional": list(self.fractional),
                "cartesian": list(self.cartesian), "source_site_index": self.source_site_index,
                "expanded_index": self.expanded_index,
                "symmetry_operation_ids": list(self.symmetry_operation_ids)}


@dataclass(frozen=True, slots=True)
class PeriodicStructure:
    """A represented right-handed cell and immutable atoms in output order.

    Cell vectors are rows in ångström; Cartesian coordinates equal fractional
    row coordinates times the cell. Construction never wraps or reorders atoms.
    """

    cell: tuple[tuple[float, float, float], ...]
    atoms: tuple[PeriodicAtom, ...]
    source_sha256: str
    selected_block: str

    def __post_init__(self) -> None:
        if type(self.cell) is not tuple or len(self.cell) != 3:
            raise ValueError("Periodic cell must be an immutable 3x3 tuple")
        for row in self.cell:
            _validate_triple(row, "cell row")
        volume = float(np.linalg.det(np.asarray(self.cell, dtype=float)))
        if not math.isfinite(volume) or volume <= 0:
            raise ValueError("Periodic cell must have finite positive volume")
        if (type(self.atoms) is not tuple or not self.atoms
                or any(type(atom) is not PeriodicAtom for atom in self.atoms)):
            raise ValueError("Periodic atoms must be a nonempty tuple of PeriodicAtom values")
        if sorted(atom.expanded_index for atom in self.atoms) != list(range(len(self.atoms))):
            raise ValueError("Expanded indices must form a complete zero-based bijection")
        for atom in self.atoms:
            expected = np.asarray(atom.fractional, dtype=float) @ np.asarray(self.cell, dtype=float)
            if any(not math.isclose(actual, float(reference), rel_tol=0,
                                    abs_tol=CARTESIAN_TOLERANCE_ANGSTROM)
                   for actual, reference in zip(atom.cartesian, expected)):
                raise ValueError("Cartesian coordinates must equal fractional coordinates times cell")
        if not isinstance(self.source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise ValueError("Source identity must be a lowercase SHA-256 digest")
        if not isinstance(self.selected_block, str) or not self.selected_block:
            raise ValueError("Selected CIF block name is required")

    @property
    def structure_id(self) -> str:
        """Identify ordered geometry at 12 decimals, modulo integer lattice images.

        This retains the represented basis and origin; there is no symmetry,
        primitive-cell, atom-permutation or collective-origin equivalence.
        """
        cell = [[round(float(value), IDENTITY_DECIMAL_PLACES) + 0.0 for value in row]
                for row in self.cell]
        positions = [[round(float(value) % 1.0, IDENTITY_DECIMAL_PLACES) % 1.0
                      for value in atom.fractional] for atom in self.atoms]
        return stable_hash({"identity_schema": 1, "cell": cell,
                            "elements": [atom.element for atom in self.atoms],
                            "fractional": positions})

    def to_dict(self) -> dict[str, object]:
        """Return geometry and provenance without exposing mutable model state."""
        return {"cell": [list(row) for row in self.cell],
                "atoms": [atom.to_dict() for atom in self.atoms],
                "source_sha256": self.source_sha256, "selected_block": self.selected_block,
                "structure_id": self.structure_id}
