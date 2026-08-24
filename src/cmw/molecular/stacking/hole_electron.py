"""Multiwfn-style hole/electron planning and typed result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Mapping, Sequence

from cmw.core.artifacts import (
    Artifact,
    ArtifactValidation,
    ExcitedStateArtifact,
    HoleElectronArtifact,
    NTOArtifact,
    ValidationStatus,
    validate_artifact_compatibility,
)
from cmw.molecular.multiwfn.adapter import (
    MultiwfnCommandSpec,
    MultiwfnOutputSpec,
    build_command_spec,
    validate_output_specs,
)

from .models import Vector3, vector3


class HoleElectronContractError(ValueError):
    """Raised when hole/electron analysis intent or results are incomplete."""

    code = "FAILED_HOLE_ELECTRON_CONTRACT"


@dataclass(frozen=True)
class HoleElectronMetrics:
    hole_centroid_angstrom: Vector3
    electron_centroid_angstrom: Vector3
    separation_distance_angstrom: float
    overlap: float
    separation_index: float
    fragment_contributions: Mapping[str, Mapping[str, float]]

    def __post_init__(self) -> None:
        hole = vector3(self.hole_centroid_angstrom, name="hole centroid")
        electron = vector3(self.electron_centroid_angstrom, name="electron centroid")
        separation = float(self.separation_distance_angstrom)
        overlap = float(self.overlap)
        separation_index = float(self.separation_index)
        scalars = (separation, overlap, separation_index)
        if not all(math.isfinite(float(item)) for item in scalars):
            raise HoleElectronContractError("hole/electron metrics must be finite")
        if separation < 0:
            raise HoleElectronContractError("hole/electron separation cannot be negative")
        observed_separation = math.sqrt(
            sum(
                (electron[index] - hole[index]) ** 2
                for index in range(3)
            )
        )
        if not math.isclose(
            separation,
            observed_separation,
            abs_tol=1.0e-8,
        ):
            raise HoleElectronContractError(
                "hole/electron separation does not match the declared centroids"
            )
        if not 0.0 <= overlap <= 1.0:
            raise HoleElectronContractError("hole/electron overlap must be in [0, 1]")
        contributions: dict[str, dict[str, float]] = {}
        for fragment, values in self.fragment_contributions.items():
            if not fragment or not isinstance(values, Mapping):
                raise HoleElectronContractError("fragment contributions require named mappings")
            selected = {str(key): float(value) for key, value in values.items()}
            if not selected or not all(math.isfinite(value) for value in selected.values()):
                raise HoleElectronContractError("fragment contributions must be finite")
            contributions[str(fragment)] = selected
        if not contributions:
            raise HoleElectronContractError("fragment contributions are required")
        object.__setattr__(self, "hole_centroid_angstrom", hole)
        object.__setattr__(self, "electron_centroid_angstrom", electron)
        object.__setattr__(self, "separation_distance_angstrom", separation)
        object.__setattr__(self, "overlap", overlap)
        object.__setattr__(self, "separation_index", separation_index)
        object.__setattr__(self, "fragment_contributions", contributions)

    def to_dict(self) -> dict[str, object]:
        return {
            "hole_centroid_angstrom": list(self.hole_centroid_angstrom),
            "electron_centroid_angstrom": list(self.electron_centroid_angstrom),
            "separation_distance_angstrom": self.separation_distance_angstrom,
            "overlap": self.overlap,
            "separation_index": self.separation_index,
            "fragment_contributions": {
                key: dict(value) for key, value in self.fragment_contributions.items()
            },
        }


@dataclass(frozen=True)
class HoleElectronProtocol:
    state_index: int
    menu_contract: str
    menu_sequence: tuple[str, ...]
    outputs: tuple[MultiwfnOutputSpec, ...]
    visualization: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.state_index, bool)
            or not isinstance(self.state_index, int)
            or self.state_index < 1
        ):
            raise HoleElectronContractError(
                "hole/electron state_index must be a positive integer"
            )
        if not self.menu_contract.strip():
            raise HoleElectronContractError("Multiwfn menu contract is required")
        menu = tuple(str(item) for item in self.menu_sequence)
        if not menu or any("\n" in item or "\r" in item for item in menu):
            raise HoleElectronContractError(
                "Multiwfn menu sequence requires explicit single-line entries"
            )
        object.__setattr__(self, "menu_sequence", menu)
        object.__setattr__(self, "outputs", validate_output_specs(self.outputs))
        object.__setattr__(self, "visualization", dict(self.visualization))

    @property
    def stdin_text(self) -> str:
        return "\n".join(self.menu_sequence) + "\n"

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": "hole_electron_analysis",
            "state_index": self.state_index,
            "menu_contract": self.menu_contract,
            "menu_sequence": list(self.menu_sequence),
            "outputs": [item.to_dict() for item in self.outputs],
            "visualization": dict(self.visualization),
        }


@dataclass(frozen=True)
class HoleElectronAnalysisPlan:
    protocol: HoleElectronProtocol
    artifact: HoleElectronArtifact

    def to_dict(self) -> dict[str, object]:
        return {
            "program": "Multiwfn",
            "protocol": self.protocol.to_dict(),
            "stdin": self.protocol.stdin_text,
            "artifact": self.artifact.to_dict(),
        }


def build_hole_electron_command_spec(
    plan: HoleElectronAnalysisPlan,
    *,
    runtime: Mapping[str, object],
    source_path: Path,
    attempt_directory: Path,
    stdin_path: Path,
) -> MultiwfnCommandSpec:
    """Translate a hole/electron plan through the existing Multiwfn adapter."""

    if runtime.get("menu_contract") != plan.protocol.menu_contract:
        raise HoleElectronContractError(
            "Multiwfn runtime menu contract does not match the analysis protocol"
        )
    return build_command_spec(
        runtime=runtime,
        source_path=source_path,
        attempt_directory=attempt_directory,
        stdin_path=stdin_path,
        outputs=plan.protocol.outputs,
    )


def plan_hole_electron_analysis(
    excited_state: ExcitedStateArtifact,
    protocol: HoleElectronProtocol,
    *,
    nto: NTOArtifact | None = None,
) -> HoleElectronAnalysisPlan:
    planned = ArtifactValidation(
        ValidationStatus.UNKNOWN,
        {"planned": True, "executed": False},
        "PLANNED_NOT_EXECUTED",
        "hole/electron analysis has been planned but not executed",
    )
    parents = (excited_state.artifact_id,) + (
        (nto.artifact_id,) if nto is not None else ()
    )
    artifact = HoleElectronArtifact(
        producing_calculation="multiwfn_hole_electron",
        method=excited_state.method,
        basis=excited_state.basis,
        protocol={"state_index": protocol.state_index},
        parent_artifacts=parents,
        files={item.role: item.output_path for item in protocol.outputs},
        validation=planned,
        provenance={"execution": "planned_only", "generating_program": "Multiwfn"},
        metadata={
            "multiwfn_protocol": protocol.to_dict(),
            "expected_metrics": [
                "hole_centroid_angstrom",
                "electron_centroid_angstrom",
                "separation_distance_angstrom",
                "overlap",
                "separation_index",
                "fragment_contributions",
            ],
            "visualization": dict(protocol.visualization),
        },
    )
    return HoleElectronAnalysisPlan(protocol, artifact)


def create_hole_electron_artifact(
    excited_state: ExcitedStateArtifact,
    protocol: HoleElectronProtocol,
    metrics: HoleElectronMetrics,
    *,
    runtime_provenance: Mapping[str, object],
    files: Mapping[str, str],
    nto: NTOArtifact | None = None,
) -> HoleElectronArtifact:
    """Create a validated analysis artifact without interpreting its chemistry."""

    version = runtime_provenance.get("version")
    executable = runtime_provenance.get("executable")
    if (
        not isinstance(version, str)
        or not version
        or not isinstance(executable, str)
        or not executable
    ):
        raise HoleElectronContractError(
            "Multiwfn runtime provenance requires version and executable"
        )
    if runtime_provenance.get("menu_contract") != protocol.menu_contract:
        raise HoleElectronContractError(
            "Multiwfn runtime menu contract does not match the analysis protocol"
        )
    required_roles = {item.role for item in protocol.outputs if item.required}
    if not required_roles.issubset(files):
        raise HoleElectronContractError(
            "hole/electron result files do not satisfy declared output roles"
        )
    missing_files = sorted(
        role
        for role in required_roles
        if not Path(files[role]).is_file() or Path(files[role]).stat().st_size < 1
    )
    if missing_files:
        raise HoleElectronContractError(
            "hole/electron required outputs are missing or empty: "
            + ", ".join(missing_files)
        )
    parents = (excited_state.artifact_id,) + (
        (nto.artifact_id,) if nto is not None else ()
    )
    validation = ArtifactValidation(
        ValidationStatus.PASSED,
        {
            "parent_excited_state": excited_state.validation.passed,
            "metrics_complete": True,
            "runtime_provenance": True,
            "required_outputs": True,
        },
        "VALID_HOLE_ELECTRON_ARTIFACT",
        "hole/electron metrics, lineage, runtime, and outputs are complete",
    )
    artifact = HoleElectronArtifact(
        producing_calculation="multiwfn_hole_electron",
        method=excited_state.method,
        basis=excited_state.basis,
        protocol={"state_index": protocol.state_index},
        parent_artifacts=parents,
        files=dict(files),
        validation=validation,
        provenance={
            "generating_program": "Multiwfn",
            "runtime": dict(runtime_provenance),
        },
        metadata={
            **metrics.to_dict(),
            "multiwfn_protocol": protocol.to_dict(),
            "visualization": dict(protocol.visualization),
        },
    )
    parent_objects: list[Artifact] = [excited_state]
    if nto is not None:
        parent_objects.append(nto)
    validate_artifact_compatibility(artifact, parent_objects)
    return artifact


__all__ = [
    "HoleElectronAnalysisPlan",
    "HoleElectronContractError",
    "HoleElectronMetrics",
    "HoleElectronProtocol",
    "build_hole_electron_command_spec",
    "create_hole_electron_artifact",
    "plan_hole_electron_analysis",
]
