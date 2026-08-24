"""Reusable TDDFT/TDA, state-selection, and NTO planning contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from cmw.core.artifacts import (
    Artifact,
    ArtifactCompatibilityError,
    ArtifactValidation,
    ExcitedStateArtifact,
    NTOArtifact,
    StructureArtifact,
    ValidationStatus,
    validate_artifact_compatibility,
)
from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.orca.status import StageType


class ExcitedStateContractError(ValueError):
    """Raised when an excited-state calculation plan is incomplete."""

    code = "FAILED_PROTOCOL_MISMATCH"


@dataclass(frozen=True)
class ExcitedStateProtocol:
    method: str
    basis: str
    number_of_roots: int
    state_selection: Mapping[str, object]
    tda: bool = False
    generate_ntos: bool = True
    keywords: tuple[str, ...] = ()
    additional_tddft_lines: tuple[str, ...] = ()
    visualization: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.method.strip() or not self.basis.strip():
            raise ExcitedStateContractError("excited-state method and basis are required")
        if (
            isinstance(self.number_of_roots, bool)
            or not isinstance(self.number_of_roots, int)
            or self.number_of_roots < 1
        ):
            raise ExcitedStateContractError(
                "number_of_roots must be a positive integer"
            )
        selection = dict(self.state_selection)
        if not selection:
            raise ExcitedStateContractError("state_selection criteria are required")
        states = selection.get("states")
        if states is not None:
            if (
                not isinstance(states, Sequence)
                or isinstance(states, (str, bytes))
                or not states
                or any(
                    isinstance(item, bool)
                    or not isinstance(item, int)
                    or item < 1
                    or item > self.number_of_roots
                    for item in states
                )
            ):
                raise ExcitedStateContractError(
                    "selected states must be unique one-based indices within number_of_roots"
                )
            if len(set(states)) != len(states):
                raise ExcitedStateContractError("selected state indices must be unique")
        single_line = (
            self.method,
            self.basis,
            *self.keywords,
            *self.additional_tddft_lines,
        )
        if any("\n" in item or "\r" in item for item in single_line):
            raise ExcitedStateContractError("excited-state input values must be single-line")
        object.__setattr__(self, "state_selection", selection)
        object.__setattr__(self, "keywords", tuple(self.keywords))
        object.__setattr__(
            self, "additional_tddft_lines", tuple(self.additional_tddft_lines)
        )
        object.__setattr__(self, "visualization", dict(self.visualization))

    @property
    def keyword_line(self) -> str:
        return " ".join((self.method, self.basis, *self.keywords))

    @property
    def tddft_block(self) -> str:
        rows = [
            "%tddft",
            f"  nroots {self.number_of_roots}",
            f"  tda {'true' if self.tda else 'false'}",
        ]
        rows.extend(f"  {line.strip()}" for line in self.additional_tddft_lines)
        rows.append("end")
        return "\n".join(rows)

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "basis": self.basis,
            "theory": "TDA" if self.tda else "TDDFT",
            "number_of_roots": self.number_of_roots,
            "state_selection": dict(self.state_selection),
            "generate_ntos": self.generate_ntos,
            "keywords": list(self.keywords),
            "additional_tddft_lines": list(self.additional_tddft_lines),
            "visualization": dict(self.visualization),
            "excited_state_required": True,
            "minimum_excited_states": self.number_of_roots,
        }

    def orca_stage_spec(self) -> OrcaStageSpec:
        return OrcaStageSpec(
            StageType.TDDFT,
            self.keyword_line,
            (self.tddft_block,),
            self.to_dict(),
        )


def validate_excited_state_artifact(
    artifact: ExcitedStateArtifact,
    *,
    source: StructureArtifact,
    protocol: ExcitedStateProtocol,
    parents: Sequence[Artifact] = (),
) -> ArtifactValidation:
    """Validate lineage and protocol metadata without accepting execution alone."""

    checks: dict[str, bool | None] = {
        "source_parent": source.artifact_id in artifact.parent_artifacts,
        "parent_identity": False,
        "source_geometry_hash": artifact.metadata.get("source_geometry_hash")
        == source.geometry_hash,
        "method_match": artifact.method == protocol.method,
        "basis_match": artifact.basis == protocol.basis,
        "root_count_match": artifact.protocol.get("number_of_roots")
        == protocol.number_of_roots,
        "state_selection_match": artifact.protocol.get("state_selection")
        == dict(protocol.state_selection),
        "orca_contract": False,
        "artifact_compatibility": False,
    }
    try:
        parent_objects = {source.artifact_id: source}
        for parent in parents:
            artifact_id = getattr(parent, "artifact_id", None)
            if not isinstance(artifact_id, str):
                raise TypeError("excited-state parents must be typed artifacts")
            parent_objects[artifact_id] = parent
        checks["parent_identity"] = set(artifact.parent_artifacts) == set(
            parent_objects
        )
        spec = protocol.orca_stage_spec()
        checks["orca_contract"] = (
            spec.stage_type is StageType.TDDFT
            and spec.execution_intent.task.value == "excited_state"
            and "%tddft" in spec.blocks[0].casefold()
        )
        validate_artifact_compatibility(artifact, tuple(parent_objects.values()))
        checks["artifact_compatibility"] = True
    except (ArtifactCompatibilityError, TypeError, ValueError) as exc:
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            ExcitedStateContractError.code,
            str(exc),
        )
    if not all(value is True for value in checks.values()):
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            ExcitedStateContractError.code,
            "excited-state artifact does not match its declared protocol",
        )
    return ArtifactValidation(
        ValidationStatus.PASSED,
        checks,
        "VALID_EXCITED_STATE_ARTIFACT",
        "excited-state lineage and protocol metadata are consistent",
    )


def planned_excited_state_artifacts(
    source: StructureArtifact,
    protocol: ExcitedStateProtocol,
    *,
    producing_calculation: str = "excited_state",
) -> tuple[ExcitedStateArtifact, NTOArtifact | None]:
    planned = ArtifactValidation(
        ValidationStatus.UNKNOWN,
        {"planned": True, "executed": False},
        "PLANNED_NOT_EXECUTED",
        "excited-state calculation has been planned but not executed",
    )
    excited = ExcitedStateArtifact(
        producing_calculation=producing_calculation,
        method=protocol.method,
        basis=protocol.basis,
        protocol=protocol.to_dict(),
        parent_artifacts=(source.artifact_id,),
        files={"output": "excited_state/stage.out", "wavefunction": "excited_state/stage.gbw"},
        validation=planned,
        provenance={"execution": "planned_only"},
        metadata={
            "source_geometry_hash": source.geometry_hash,
            "state_selection": dict(protocol.state_selection),
        },
    )
    if not protocol.generate_ntos:
        return excited, None
    nto = NTOArtifact(
        producing_calculation="natural_transition_orbitals",
        method=protocol.method,
        basis=protocol.basis,
        protocol={
            "state_selection": dict(protocol.state_selection),
            "source_number_of_roots": protocol.number_of_roots,
        },
        parent_artifacts=(excited.artifact_id,),
        files={"orbitals": "excited_state/nto.molden.input"},
        validation=planned,
        provenance={"execution": "planned_only"},
        metadata={"visualization": dict(protocol.visualization)},
    )
    return excited, nto


__all__ = [
    "ExcitedStateContractError",
    "ExcitedStateProtocol",
    "planned_excited_state_artifacts",
    "validate_excited_state_artifact",
]
