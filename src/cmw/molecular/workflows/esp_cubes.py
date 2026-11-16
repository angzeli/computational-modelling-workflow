"""Scientific configuration for paired density and ESP cube generation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class EspConfiguration:
    grid_spacing_bohr: float
    # Explicit common grid: axis-aligned, isotropic spacing. Both or neither.
    grid_origin_bohr: tuple[float, float, float] | None = None
    grid_dimensions: tuple[int, int, int] | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.grid_spacing_bohr) or self.grid_spacing_bohr <= 0:
            raise ValueError("ESP grid spacing must be finite and positive")
        if (self.grid_origin_bohr is None) != (self.grid_dimensions is None):
            raise ValueError("--grid-origin-bohr and --grid-dimensions must be given together")
        if self.grid_origin_bohr is not None:
            origin = tuple(self.grid_origin_bohr)
            if len(origin) != 3 or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) for value in origin
            ):
                raise ValueError("ESP grid origin must be three finite values in bohr")
            dimensions = tuple(self.grid_dimensions or ())
            if len(dimensions) != 3 or not all(
                type(value) is int and value >= 2 for value in dimensions
            ):
                raise ValueError("ESP grid dimensions must be three integers >= 2")
            object.__setattr__(self, "grid_origin_bohr", tuple(float(v) for v in origin))
            object.__setattr__(self, "grid_dimensions", dimensions)

    @property
    def explicit_grid(self) -> bool:
        return self.grid_origin_bohr is not None

    def grid_dict(self) -> dict[str, object]:
        """Identity keys added only for an explicit grid (spacing-only stays unchanged)."""
        if not self.explicit_grid:
            return {}
        return {"grid_origin_bohr": list(self.grid_origin_bohr),
                "grid_dimensions": list(self.grid_dimensions)}

    def end_bohr(self) -> tuple[float, float, float]:
        assert self.grid_origin_bohr is not None and self.grid_dimensions is not None
        return tuple(origin + (count - 1) * self.grid_spacing_bohr
                     for origin, count in zip(self.grid_origin_bohr, self.grid_dimensions))


def explicit_grid_from_calculation(calculation: Mapping[str, object]) -> EspConfiguration | None:
    """Rebuild an explicit grid from a stored target calculation, else None."""
    if "grid_origin_bohr" not in calculation:
        return None
    return EspConfiguration(
        float(calculation["grid_spacing_bohr"]),
        tuple(calculation["grid_origin_bohr"]),
        tuple(calculation["grid_dimensions"]),
    )


def estimated_cube_bytes(dimensions: tuple[int, int, int], atom_count: int) -> int:
    """Multiwfn cube size: 14 bytes per value, 6 per line, a line break per z-run."""
    nx, ny, nz = dimensions
    return 400 + 60 * atom_count + 14 * nx * ny * nz + nx * ny * math.ceil(nz / 6)
