"""Generic fixed-region contracts for hierarchical molecular relaxation."""

from __future__ import annotations

from typing import Mapping

from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.orca.status import StageType

from .models import GroundStateProtocol, RelaxationProtocol


class RelaxationContractError(ValueError):
    """Raised when a requested constraint set cannot be rendered safely."""

    code = "FAILED_RELAXATION_CONTRACT"


def validate_relaxation_protocol(
    protocol: RelaxationProtocol,
    *,
    atom_count: int,
) -> None:
    if atom_count < 1:
        raise RelaxationContractError("relaxation atom count must be positive")
    invalid = sorted(
        index
        for region in protocol.fixed_regions
        for index in region.atom_indices
        if index >= atom_count
    )
    if invalid:
        raise RelaxationContractError(
            f"fixed atom index exceeds structure atom count: {invalid[0]}"
        )


def render_orca_fixed_region_block(protocol: RelaxationProtocol) -> str:
    """Render deterministic zero-based ORCA Cartesian atom constraints."""

    indices = sorted(
        index for region in protocol.fixed_regions for index in region.atom_indices
    )
    rows = ["%geom", "  Constraints"]
    rows.extend(f"    {{ C {index} C }}" for index in indices)
    rows.extend(("  end", "end"))
    return "\n".join(rows)


def build_relaxation_stage_specs(
    relaxation: RelaxationProtocol,
    method: GroundStateProtocol,
) -> Mapping[str, OrcaStageSpec]:
    """Return constrained and optional full OPT plans using the existing renderer."""

    constrained_protocol = {
        **dict(method.protocol),
        "method": method.method,
        "basis": method.basis,
        "constraints": {
            "indexing": "zero_based",
            "fixed_regions": [
                region.to_dict() for region in relaxation.fixed_regions
            ],
            "preserve_stacking_registry": relaxation.preserve_stacking_registry,
        },
    }
    stages: dict[str, OrcaStageSpec] = {
        "constrained_optimization": OrcaStageSpec(
            StageType.OPT,
            method.keyword_line,
            (render_orca_fixed_region_block(relaxation),),
            constrained_protocol,
        )
    }
    if relaxation.full_relaxation:
        stages["full_optimization"] = OrcaStageSpec(
            StageType.OPT,
            method.keyword_line,
            protocol={
                **dict(method.protocol),
                "method": method.method,
                "basis": method.basis,
                "constraints": {"fixed_regions": []},
            },
        )
    return stages


__all__ = [
    "RelaxationContractError",
    "build_relaxation_stage_specs",
    "render_orca_fixed_region_block",
    "validate_relaxation_protocol",
]
