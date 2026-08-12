"""Deterministic ORCA input rendering without a general-purpose input DSL."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

from cmw.core.job import JobTarget
from cmw.structure.xyz import XYZGeometry, geometry_hash, read_xyz

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
        if {token.lower() for token in self.keywords.split()} & {"opt", "freq", "sp"}:
            raise ValueError("stage-control keywords are generated from the selected stage")
        reserved = ("%pal", "%maxcore", "* xyz", "*xyz")
        block_lines = [line for block in self.blocks for line in block.splitlines()]
        if any(line.lstrip().lower().startswith(reserved) for line in block_lines):
            raise ValueError("resources and geometry directives cannot be duplicated in stage blocks")

    def scientific_identity(self) -> dict[str, object]:
        normalized_blocks = [
            line.rstrip()
            for block in self.blocks
            for line in block.strip().splitlines()
            if line.strip()
        ]
        return {
            "adapter": "orca",
            "stage_type": self.stage_type.value,
            "keywords": " ".join(self.keywords.split()),
            "blocks": normalized_blocks,
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
    validation_policy: Mapping[str, object] | None = None,
) -> JobTarget:
    if multiplicity < 1:
        raise ValueError("multiplicity must be positive")
    calculation = spec.scientific_identity()
    if validation_policy is not None:
        calculation["validation_policy"] = dict(validation_policy)
    return JobTarget(
        stage_type=spec.stage_type.value,
        geometry_sha256=geometry_hash(geometry),
        charge=charge,
        multiplicity=multiplicity,
        calculation=calculation,
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
    lines.extend(str(line) for line in spec.scientific_identity()["blocks"])
    lines.append(f"* xyzfile {charge} {multiplicity} {geometry_path.name}")
    return "\n".join(lines) + "\n"


def parse_rendered_orca_input(path: Path, stage_type: StageType) -> tuple[JobTarget, OrcaResources]:
    """Verify the deterministic adapter contract before recording an attempt."""

    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    if len(lines) < 4 or not lines[0].startswith("! "):
        raise ValueError("ORCA input does not match the CMW rendered-input contract")
    tokens = lines[0][2:].split()
    expected_stage = STAGE_KEYWORDS[stage_type]
    if not tokens or tokens[-1].lower() != expected_stage.lower():
        raise ValueError("ORCA stage keyword does not match the target stage")
    keywords = " ".join(tokens[:-1])
    pal_match = re.fullmatch(r"%pal\s+nprocs\s+(\d+)\s+end", lines[1], re.I)
    maxcore_match = re.fullmatch(r"%maxcore\s+(\d+)", lines[2], re.I)
    geometry_match = re.fullmatch(
        r"\*\s+xyzfile\s+(-?\d+)\s+(\d+)\s+(\S+)", lines[-1], re.I
    )
    if not pal_match or not maxcore_match or not geometry_match:
        raise ValueError("ORCA resources or geometry directive do not match the CMW contract")
    geometry_reference = Path(geometry_match.group(3))
    if geometry_reference.name != str(geometry_reference):
        raise ValueError("ORCA geometry reference must be an adjacent basename")
    geometry = read_xyz(path.parent / geometry_reference)
    spec = OrcaStageSpec(stage_type, keywords, tuple(lines[3:-1]))
    target = make_target(
        geometry,
        charge=int(geometry_match.group(1)),
        multiplicity=int(geometry_match.group(2)),
        spec=spec,
    )
    resources = OrcaResources(int(pal_match.group(1)), int(maxcore_match.group(1)))
    return target, resources
