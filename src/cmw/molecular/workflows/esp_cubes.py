"""Scientific configuration for paired density and ESP cube generation."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class EspConfiguration:
    grid_spacing_bohr: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.grid_spacing_bohr) or self.grid_spacing_bohr <= 0:
            raise ValueError("ESP grid spacing must be finite and positive")
