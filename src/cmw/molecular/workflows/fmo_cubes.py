"""Scientific configuration for restricted HOMO/LUMO cube generation."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .source import ValidatedSource


@dataclass(frozen=True)
class FmoConfiguration:
    grid_spacing_bohr: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.grid_spacing_bohr) or self.grid_spacing_bohr <= 0:
            raise ValueError("FMO grid spacing must be finite and positive")


def frontier_orbitals(source: ValidatedSource) -> tuple[int, int]:
    semantics = source.wavefunction
    if semantics.spin_mode != "restricted" or source.multiplicity != 1:
        raise ValueError(
            "open-shell/unrestricted frontier orbitals are unsupported; spin channels are not guessed"
        )
    if semantics.homo_index is None or semantics.lumo_index is None:
        raise ValueError("restricted source must record one-based HOMO and LUMO indices")
    if semantics.orbital_indexing != "one_based":
        raise ValueError("FMO requires an explicit one_based frontier-orbital contract")
    if semantics.lumo_index <= semantics.homo_index:
        raise ValueError("LUMO index must be greater than HOMO index")
    if semantics.lumo_index != semantics.homo_index + 1:
        raise ValueError("LUMO must be the first unoccupied orbital after the HOMO")
    if semantics.frontier_orbitals is not None:
        frontier = semantics.frontier_orbitals
        if (
            frontier.spin_mode != "restricted"
            or frontier.indexing != "one_based"
            or frontier.homo.index != semantics.homo_index
            or frontier.lumo.index != semantics.lumo_index
            or abs(frontier.homo.occupation - 2.0) > 1.0e-6
            or abs(frontier.lumo.occupation) > 1.0e-6
        ):
            raise ValueError("typed frontier semantics contradict the occupation boundary")
    return semantics.homo_index, semantics.lumo_index
