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
        # Main function 200, subfunction 3: batch orbital cube export.
        # Scheme 1 writes orbNNNNNN.cub files, normalized by the workflow.
        return f"200\n3\n{homo},{lumo}\n4\n{spacing:g}\n1\n0\nq\n"
    if operation is Operation.ESP:
        spacing = float(parameters["grid_spacing_bohr"])
        # Main function 5 exports density.cub and totesp.cub by default.
        return f"5\n1\n4\n{spacing:g}\n2\n0\n5\n12\n4\n{spacing:g}\n2\n0\nq\n"
    if operation is Operation.IGMH:
        fragment_a = ",".join(str(value) for value in parameters["fragment_a"])
        fragment_b = ",".join(str(value) for value in parameters["fragment_b"])
        spacing = float(parameters["grid_spacing_bohr"])
        return (
            f"20\n11\n2\n{fragment_a}\n{fragment_b}\n4\n{spacing:g}\n3\n0\n0\nq\n"
        )
    raise ValueError(f"unsupported Multiwfn operation: {operation}")
