"""Version-aware, operation-level Multiwfn menu automation.

Menu numbers stay internal. Public workflows request an operation and provide
validated semantic configuration instead of accepting arbitrary command lists.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from .runtime import require_supported_version


class Operation(str, Enum):
    FMO = "FMO_HOMO_LUMO"
    ESP = "ESP_DENSITY_PAIR"
    IGMH = "IGMH_INTERFRAGMENT"


def menu_stream(
    operation: Operation,
    version: str,
    parameters: Mapping[str, object],
) -> str:
    """Render the supported 3.8 operation profile."""

    require_supported_version(version)
    if operation is Operation.FMO:
        homo = int(parameters["homo_index"])
        lumo = int(parameters["lumo_index"])
        spacing = float(parameters["grid_spacing_bohr"])
        return f"5\n4\n{spacing:g}\n7\n{homo}\nhomo.cube\n7\n{lumo}\nlumo.cube\n0\nq\n"
    if operation is Operation.ESP:
        spacing = float(parameters["grid_spacing_bohr"])
        return (
            f"5\n4\n{spacing:g}\n1\ndensity.cube\n12\nesp.cube\n0\nq\n"
        )
    if operation is Operation.IGMH:
        fragment_a = ",".join(str(value) for value in parameters["fragment_a"])
        fragment_b = ",".join(str(value) for value in parameters["fragment_b"])
        spacing = float(parameters["grid_spacing_bohr"])
        return (
            f"20\n11\n2\n{fragment_a}\n{fragment_b}\n4\n{spacing:g}\n3\n0\n0\nq\n"
        )
    raise ValueError(f"unsupported Multiwfn operation: {operation}")
