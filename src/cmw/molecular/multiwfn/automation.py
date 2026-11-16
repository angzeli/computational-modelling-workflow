"""Version-aware, operation-level Multiwfn menu automation.

Menu numbers stay internal. Public workflows request an operation and provide
validated semantic configuration instead of accepting arbitrary command lists.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from .runtime import require_supported_version


# Grid option 5 (origin, spacings, point counts) was confirmed by a sandbox run
# on these exact versions only; other versions are not natively qualified.
EXPLICIT_GRID_VERSIONS = frozenset({(2026, 7, 15)})


def _explicit_grid_menu(version: str, parameters: Mapping[str, object]) -> str:
    try:
        fields = tuple(int(value) for value in version.split("."))
    except ValueError as exc:
        raise ValueError(f"invalid Multiwfn version: {version!r}") from exc
    if fields not in EXPLICIT_GRID_VERSIONS:
        raise ValueError(
            f"explicit ESP grids are not natively qualified for Multiwfn {version}; "
            "verified: " + ", ".join(".".join(map(str, item)) for item in sorted(EXPLICIT_GRID_VERSIONS))
        )
    spacing = repr(float(parameters["grid_spacing_bohr"]))
    origin = ",".join(repr(float(value)) for value in parameters["grid_origin_bohr"])
    counts = ",".join(str(int(value)) for value in parameters["grid_dimensions"])
    return f"5\n{origin}\n{spacing},{spacing},{spacing}\n{counts}"


class Operation(str, Enum):
    FMO = "FMO_HOMO_LUMO"
    ESP = "ESP_DENSITY_PAIR"
    IGMH = "IGMH_INTERFRAGMENT"


def menu_stream(
    operation: Operation,
    version: str,
    parameters: Mapping[str, object],
) -> str:
    """Render the verified 3.8-compatible operation profile."""

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
        if "grid_origin_bohr" in parameters:
            grid = _explicit_grid_menu(version, parameters)
            return f"5\n1\n{grid}\n2\n0\n5\n12\n{grid}\n2\n0\nq\n"
        return f"5\n1\n4\n{spacing:g}\n2\n0\n5\n12\n4\n{spacing:g}\n2\n0\nq\n"
    if operation is Operation.IGMH:
        fragment_a = ",".join(str(value) for value in parameters["fragment_a"])
        fragment_b = ",".join(str(value) for value in parameters["fragment_b"])
        spacing = float(parameters["grid_spacing_bohr"])
        return (
            f"20\n11\n2\n{fragment_a}\n{fragment_b}\n4\n{spacing:g}\n3\n0\n0\nq\n"
        )
    raise ValueError(f"unsupported Multiwfn operation: {operation}")
