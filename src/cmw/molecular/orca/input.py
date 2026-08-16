"""Deterministic ORCA input rendering without a general-purpose input DSL."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from typing import Mapping

from cmw.core.execution_contract import (
    ComputationalTask,
    ExecutionContractError,
    ExecutionIntent,
)
from cmw.core.execution_profiles import ExecutionProfile
from cmw.core.job import JobTarget
from cmw.structure.xyz import XYZGeometry, geometry_hash, read_xyz

from .status import StageType


STAGE_TASKS = {
    StageType.OPT: ComputationalTask.OPTIMIZATION,
    StageType.FREQ: ComputationalTask.FREQUENCY,
    StageType.SP: ComputationalTask.SINGLE_POINT,
}

ORCA_TASK_BEHAVIORS = {
    ComputationalTask.OPTIMIZATION: "Opt",
    ComputationalTask.FREQUENCY: "Freq",
    ComputationalTask.SINGLE_POINT: "SP",
}

ORCA_MEMORY_MB_PER_GB = 1024
ORCA_MEMORY_SAFETY_FRACTION = 0.80


@dataclass(frozen=True)
class OrcaStageSpec:
    """Small inspectable stage contract; keywords remain user-controlled."""

    stage_type: StageType
    keywords: str
    blocks: tuple[str, ...] = ()
    protocol: Mapping[str, object] = field(default_factory=dict)
    task: ComputationalTask | str | None = None

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
        object.__setattr__(self, "protocol", dict(self.protocol))
        selected_task = self.task or STAGE_TASKS[self.stage_type]
        object.__setattr__(self, "task", ComputationalTask(selected_task))

    @property
    def execution_intent(self) -> ExecutionIntent:
        task = ComputationalTask(self.task)
        return ExecutionIntent(
            stage_type=self.stage_type.value,
            task=task,
            required_behavior=ORCA_TASK_BEHAVIORS[task],
        )

    def scientific_identity(self) -> dict[str, object]:
        normalized_blocks = [
            line.rstrip()
            for block in self.blocks
            for line in block.strip().splitlines()
            if line.strip()
        ]
        identity: dict[str, object] = {
            "adapter": "orca",
            "stage_type": self.stage_type.value,
            "keywords": " ".join(self.keywords.split()),
            "blocks": normalized_blocks,
        }
        if self.protocol:
            identity["protocol"] = dict(self.protocol)
        return identity


def validate_orca_execution_contract(
    intent: ExecutionIntent,
    *,
    rendered_behavior: str | None = None,
) -> None:
    """Fail closed unless stage, task, and ORCA operation are mutually consistent."""

    try:
        stage_type = StageType(intent.stage_type)
        expected_task = STAGE_TASKS[stage_type]
        expected_behavior = ORCA_TASK_BEHAVIORS[expected_task]
    except (KeyError, ValueError) as exc:
        raise ExecutionContractError(
            f"{ExecutionContractError.code}: unsupported ORCA execution intent"
        ) from exc
    mismatches: list[str] = []
    if intent.task is not expected_task:
        mismatches.append(
            f"stage {stage_type.value} requires task {expected_task.value}, "
            f"not {intent.task.value}"
        )
    if intent.required_behavior.casefold() != expected_behavior.casefold():
        mismatches.append(
            f"task {expected_task.value} requires ORCA {expected_behavior}, "
            f"not {intent.required_behavior}"
        )
    if (
        rendered_behavior is not None
        and rendered_behavior.casefold() != expected_behavior.casefold()
    ):
        mismatches.append(
            f"stage {stage_type.value} rendered ORCA {rendered_behavior}, "
            f"not {expected_behavior}"
        )
    if mismatches:
        raise ExecutionContractError(
            f"{ExecutionContractError.code}: " + "; ".join(mismatches)
        )


def orca_execution_intent(stage_type: StageType) -> ExecutionIntent:
    """Return the canonical task and ORCA behavior for a scientific stage."""

    task = STAGE_TASKS[stage_type]
    return ExecutionIntent(stage_type.value, task, ORCA_TASK_BEHAVIORS[task])


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


@dataclass(frozen=True)
class ResolvedOrcaResources:
    """Program resources and provenance derived from one execution profile."""

    profile_name: str
    execution_profile_hash: str
    total_memory_gb: float
    safety_fraction: float
    resources: OrcaResources

    @property
    def derived_total_memory_mb(self) -> int:
        return self.resources.nprocs * self.resources.maxcore_mb_per_process

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_profile": self.profile_name,
            "execution_profile_hash": self.execution_profile_hash,
            "nprocs": self.resources.nprocs,
            "total_memory_gb": self.total_memory_gb,
            "memory_safety_fraction": self.safety_fraction,
            "derived_maxcore_mb_per_process": (
                self.resources.maxcore_mb_per_process
            ),
            "derived_total_memory_mb": self.derived_total_memory_mb,
        }


def resolve_orca_resources(
    profile: ExecutionProfile,
    *,
    safety_fraction: float = ORCA_MEMORY_SAFETY_FRACTION,
) -> ResolvedOrcaResources:
    """Derive conservative per-process MaxCore from a total profile budget."""

    if (
        not isinstance(safety_fraction, (int, float))
        or isinstance(safety_fraction, bool)
        or not math.isfinite(float(safety_fraction))
        or not 0 < float(safety_fraction) <= 1
    ):
        raise ValueError("ORCA memory safety fraction must be in (0, 1]")
    total_memory_mb = math.floor(
        profile.orca.total_memory_gb * ORCA_MEMORY_MB_PER_GB
    )
    usable_memory_mb = math.floor(total_memory_mb * float(safety_fraction))
    maxcore_mb_per_process = usable_memory_mb // profile.orca.nprocs
    if maxcore_mb_per_process < 1:
        raise ValueError("ORCA execution profile provides insufficient memory per process")
    resources = OrcaResources(profile.orca.nprocs, maxcore_mb_per_process)
    if resources.nprocs * resources.maxcore_mb_per_process > total_memory_mb:
        raise ValueError("derived ORCA memory exceeds the execution-profile budget")
    return ResolvedOrcaResources(
        profile.name,
        profile.execution_profile_hash,
        profile.orca.total_memory_gb,
        float(safety_fraction),
        resources,
    )


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
    intent = spec.execution_intent
    validate_orca_execution_contract(intent)
    stage_keyword = intent.required_behavior
    lines = [
        f"! {keyword_tokens} {stage_keyword}",
        f"%pal nprocs {resources.nprocs} end",
        f"%maxcore {resources.maxcore_mb_per_process}",
    ]
    lines.extend(str(line) for line in spec.scientific_identity()["blocks"])
    lines.append(f"* xyzfile {charge} {multiplicity} {geometry_path.name}")
    validate_orca_execution_contract(intent, rendered_behavior=lines[0].split()[-1])
    return "\n".join(lines) + "\n"


def parse_rendered_orca_input(path: Path, stage_type: StageType) -> tuple[JobTarget, OrcaResources]:
    """Verify the deterministic adapter contract before recording an attempt."""

    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    if len(lines) < 4 or not lines[0].startswith("! "):
        raise ValueError("ORCA input does not match the CMW rendered-input contract")
    tokens = lines[0][2:].split()
    if not tokens:
        raise ExecutionContractError(
            f"{ExecutionContractError.code}: ORCA operation is missing"
        )
    validate_orca_execution_contract(
        orca_execution_intent(stage_type), rendered_behavior=tokens[-1]
    )
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
