"""Deterministic ORCA input rendering without a general-purpose input DSL."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from cmw.core.job import JobTarget
from cmw.structure.xyz import XYZGeometry, geometry_hash

from .status import StageType


STAGE_KEYWORDS = {
    StageType.OPT: "Opt",
    StageType.FREQ: "Freq",
    StageType.SP: "SP",
}


@dataclass(frozen=True)
class OrcaStageSpec:
    """Small inspectable stage contract; keywords remain user-controlled."""

    stage_type: StageType
    keywords: str
    blocks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.keywords.strip():
            raise ValueError("ORCA stage keywords must not be empty")
        if "\n" in self.keywords or "\r" in self.keywords:
            raise ValueError("ORCA keywords must be one line")
        if any("\x00" in block for block in self.blocks):
            raise ValueError("ORCA blocks must not contain NUL bytes")

    def scientific_identity(self) -> dict[str, object]:
        return {
            "adapter": "orca",
            "stage_type": self.stage_type.value,
            "keywords": " ".join(self.keywords.split()),
            "blocks": [block.strip() for block in self.blocks],
        }


@dataclass(frozen=True)
class OrcaResources:
    nprocs: int = 1
    maxcore_mb_per_process: int = 1000

    def __post_init__(self) -> None:
        if self.nprocs < 1:
            raise ValueError("nprocs must be positive")
        if self.maxcore_mb_per_process < 1:
            raise ValueError("maxcore must be positive")

    def to_dict(self) -> dict[str, int]:
        return {
            "nprocs": self.nprocs,
            "maxcore_mb_per_process": self.maxcore_mb_per_process,
        }


def make_target(
    geometry: XYZGeometry,
    *,
    charge: int,
    multiplicity: int,
    spec: OrcaStageSpec,
) -> JobTarget:
    if multiplicity < 1:
        raise ValueError("multiplicity must be positive")
    return JobTarget(
        stage_type=spec.stage_type.value,
        geometry_sha256=geometry_hash(geometry),
        charge=charge,
        multiplicity=multiplicity,
        calculation=spec.scientific_identity(),
    )


def render_orca_input(
    *,
    geometry_path: Path,
    charge: int,
    multiplicity: int,
    spec: OrcaStageSpec,
    resources: OrcaResources,
) -> str:
    """Render a compact ORCA input referencing one adjacent XYZ file."""

    if geometry_path.name != str(geometry_path):
        raise ValueError("ORCA geometry reference must be a basename in the attempt directory")
    keyword_tokens = " ".join(spec.keywords.split())
    stage_keyword = STAGE_KEYWORDS[spec.stage_type]
    lines = [
        f"! {keyword_tokens} {stage_keyword}",
        f"%pal nprocs {resources.nprocs} end",
        f"%maxcore {resources.maxcore_mb_per_process}",
    ]
    lines.extend(block.strip() for block in spec.blocks if block.strip())
    lines.append(f"* xyzfile {charge} {multiplicity} {geometry_path.name}")
    return "\n".join(lines) + "\n"
