"""Typed scientific artifacts with explicit provenance and compatibility rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, ClassVar, Mapping, Sequence

from .provenance import stable_hash


ARTIFACT_SCHEMA_VERSION = 1


class ValidationStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ArtifactValidation:
    """Machine-readable validation decision attached to a scientific artifact."""

    status: ValidationStatus = ValidationStatus.UNKNOWN
    checks: Mapping[str, bool | None] = field(default_factory=dict)
    code: str = "NOT_VALIDATED"
    reason: str = "validation has not been performed"

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", dict(self.checks))

    @property
    def passed(self) -> bool:
        return self.status is ValidationStatus.PASSED

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "checks": dict(self.checks),
            "code": self.code,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactValidation":
        return cls(
            ValidationStatus(str(value.get("status", ValidationStatus.UNKNOWN.value))),
            dict(value.get("checks", {})),
            str(value.get("code", "NOT_VALIDATED")),
            str(value.get("reason", "validation has not been performed")),
        )


@dataclass(frozen=True)
class Artifact:
    """Semantic identity for one calculation product or derived result."""

    TYPE: ClassVar[str] = "Artifact"

    producing_calculation: str
    method: str | None = None
    basis: str | None = None
    protocol: Mapping[str, object] = field(default_factory=dict)
    parent_artifacts: tuple[str, ...] = ()
    files: Mapping[str, str] = field(default_factory=dict)
    validation: ArtifactValidation = field(default_factory=ArtifactValidation)
    provenance: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.producing_calculation:
            raise ValueError("producing_calculation is required")
        if len(set(self.parent_artifacts)) != len(self.parent_artifacts):
            raise ValueError("parent artifact identities must be unique")
        object.__setattr__(self, "protocol", dict(self.protocol))
        object.__setattr__(self, "parent_artifacts", tuple(self.parent_artifacts))
        object.__setattr__(self, "files", {str(k): str(v) for k, v in self.files.items()})
        object.__setattr__(self, "provenance", dict(self.provenance))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def artifact_type(self) -> str:
        return self.TYPE

    @property
    def artifact_id(self) -> str:
        """Stable semantic identity; file locations and verdicts are evidence, not intent."""

        return stable_hash(
            {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "artifact_type": self.artifact_type,
                "producing_calculation": self.producing_calculation,
                "method": self.method,
                "basis": self.basis,
                "protocol": dict(self.protocol),
                "parent_artifacts": list(self.parent_artifacts),
                "metadata": dict(self.metadata),
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "producing_calculation": self.producing_calculation,
            "method": self.method,
            "basis": self.basis,
            "protocol": dict(self.protocol),
            "parent_artifacts": list(self.parent_artifacts),
            "files": dict(self.files),
            "validation": self.validation.to_dict(),
            "provenance": dict(self.provenance),
            "metadata": dict(self.metadata),
        }


class CalculationArtifact(Artifact):
    TYPE = "CalculationArtifact"


class EnergyArtifact(CalculationArtifact):
    TYPE = "EnergyArtifact"


class WavefunctionArtifact(CalculationArtifact):
    TYPE = "WavefunctionArtifact"


class AnalysisArtifact(Artifact):
    TYPE = "AnalysisArtifact"


class OptimizationArtifact(CalculationArtifact):
    TYPE = "OptimizationArtifact"


class FrequencyArtifact(CalculationArtifact):
    TYPE = "FrequencyArtifact"


class SinglePointArtifact(EnergyArtifact):
    TYPE = "SinglePointArtifact"


class DimerEnergyArtifact(EnergyArtifact):
    TYPE = "DimerEnergyArtifact"


class FragmentEnergyArtifact(EnergyArtifact):
    TYPE = "FragmentEnergyArtifact"


class InteractionEnergyArtifact(EnergyArtifact):
    TYPE = "InteractionEnergyArtifact"


class CPInteractionArtifact(InteractionEnergyArtifact):
    TYPE = "CPInteractionArtifact"


class LEDArtifact(AnalysisArtifact):
    TYPE = "LEDArtifact"


class DensityArtifact(AnalysisArtifact):
    TYPE = "DensityArtifact"


class IGMHArtifact(AnalysisArtifact):
    TYPE = "IGMHArtifact"


ARTIFACT_TYPES: dict[str, type[Artifact]] = {
    item.TYPE: item
    for item in (
        Artifact,
        CalculationArtifact,
        EnergyArtifact,
        WavefunctionArtifact,
        AnalysisArtifact,
        OptimizationArtifact,
        FrequencyArtifact,
        SinglePointArtifact,
        DimerEnergyArtifact,
        FragmentEnergyArtifact,
        InteractionEnergyArtifact,
        CPInteractionArtifact,
        LEDArtifact,
        DensityArtifact,
        IGMHArtifact,
    )
}


class ArtifactCompatibilityError(ValueError):
    """Raised when parent artifacts cannot support a derived scientific result."""


def artifact_from_dict(value: Mapping[str, Any]) -> Artifact:
    if value.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported scientific artifact schema")
    artifact_type = str(value.get("artifact_type", ""))
    cls = ARTIFACT_TYPES.get(artifact_type)
    if cls is None:
        raise ValueError(f"unsupported scientific artifact type: {artifact_type!r}")
    artifact = cls(
        producing_calculation=str(value["producing_calculation"]),
        method=str(value["method"]) if value.get("method") is not None else None,
        basis=str(value["basis"]) if value.get("basis") is not None else None,
        protocol=dict(value.get("protocol", {})),
        parent_artifacts=tuple(str(item) for item in value.get("parent_artifacts", ())),
        files={str(k): str(v) for k, v in dict(value.get("files", {})).items()},
        validation=ArtifactValidation.from_dict(dict(value.get("validation", {}))),
        provenance=dict(value.get("provenance", {})),
        metadata=dict(value.get("metadata", {})),
    )
    if value.get("artifact_id") not in (None, artifact.artifact_id):
        raise ValueError("stored artifact identity does not match artifact content")
    return artifact


def artifact_matches_type(artifact: Artifact, expected_type: str) -> bool:
    return any(
        getattr(cls, "TYPE", cls.__name__) == expected_type or cls.__name__ == expected_type
        for cls in type(artifact).mro()
    )


def _normal(value: str | None) -> str:
    return "".join((value or "").casefold().split())


def _protocol_signature(artifact: Artifact) -> tuple[str, str, str]:
    protocol = {
        key: value
        for key, value in artifact.protocol.items()
        if key not in {"fragment", "fragment_id", "role"}
    }
    return _normal(artifact.method), _normal(artifact.basis), stable_hash(protocol)


def validate_artifact_compatibility(
    artifact: Artifact, parents: Sequence[Artifact]
) -> None:
    """Fail closed when typed parents cannot support the requested artifact."""

    by_id = {parent.artifact_id: parent for parent in parents}
    missing = [item for item in artifact.parent_artifacts if item not in by_id]
    if missing:
        raise ArtifactCompatibilityError(
            f"missing declared parent artifacts: {', '.join(missing)}"
        )
    selected = [by_id[item] for item in artifact.parent_artifacts]
    invalid = [parent.artifact_id for parent in selected if not parent.validation.passed]
    if invalid:
        raise ArtifactCompatibilityError(
            f"derived artifact has invalid parents: {', '.join(invalid)}"
        )

    if isinstance(artifact, CPInteractionArtifact):
        if not artifact.metadata.get("derived_quantity"):
            raise ArtifactCompatibilityError(
                "CPInteractionArtifact requires an explicit derived_quantity equation"
            )
        dimers = [
            parent
            for parent in selected
            if isinstance(parent, DimerEnergyArtifact)
            or parent.metadata.get("energy_role") in {"dimer", "supersystem"}
        ]
        fragments = [
            parent
            for parent in selected
            if isinstance(parent, FragmentEnergyArtifact)
            or parent.metadata.get("energy_role") == "fragment"
        ]
        if len(dimers) != 1 or len(fragments) < 2:
            raise ArtifactCompatibilityError(
                "CPInteractionArtifact requires one dimer/supersystem energy and at least two fragment energies"
            )
        signatures = {_protocol_signature(parent) for parent in (*dimers, *fragments)}
        if len(signatures) != 1:
            raise ArtifactCompatibilityError(
                "counterpoise parent energies use different methods, bases, or protocols"
            )

    if isinstance(artifact, LEDArtifact):
        compatible = [
            parent
            for parent in selected
            if isinstance(parent, CalculationArtifact)
            and "dlpno" in _normal(parent.method)
            and parent.protocol.get("led") is True
        ]
        if not compatible:
            raise ArtifactCompatibilityError(
                "LEDArtifact requires a validated DLPNO calculation with LED enabled"
            )

    if isinstance(artifact, IGMHArtifact):
        if not any(isinstance(parent, DensityArtifact) for parent in selected):
            raise ArtifactCompatibilityError(
                "IGMHArtifact requires a validated DensityArtifact parent"
            )


def _method_metadata(calculation: Mapping[str, Any]) -> tuple[str | None, str | None, dict[str, object]]:
    protocol = dict(calculation.get("protocol", {}))
    method = protocol.get("method", calculation.get("method"))
    basis = protocol.get("basis", calculation.get("basis"))
    keywords = str(calculation.get("keywords", "")).split()
    if method is None and keywords:
        method = keywords[0]
    if basis is None:
        basis = next(
            (token for token in keywords if "basis" in token.casefold() or token.casefold().startswith(("def2-", "sto-", "cc-p"))),
            None,
        )
    return (
        str(method) if method is not None else None,
        str(basis) if basis is not None else None,
        protocol,
    )


def artifact_from_result(record: Mapping[str, Any]) -> Artifact:
    """Map a legacy ORCA or Multiwfn result to the typed artifact model."""

    target = record.get("target")
    if not isinstance(target, Mapping):
        raise ValueError("result lacks a calculation target")
    target_id = str(target.get("target_id", ""))
    if not target_id:
        raise ValueError("result target lacks target_id")
    stage_type = str(target.get("stage_type", ""))
    calculation = dict(target.get("calculation", {}))
    method, basis, protocol = _method_metadata(calculation)

    validation_record = record.get("validation")
    scientific = dict(record.get("scientific", {}))
    reusable = bool(record.get("reusable"))
    if isinstance(validation_record, Mapping):
        checks = dict(validation_record.get("checks", {}))
        checks.update(
            {
                "execution_success": record.get("execution", {}).get("status") == "SUCCESS",
                "scientific_valid": scientific.get("status") == "VALID",
                "result_reusable": reusable,
            }
        )
        code = str(
            validation_record.get("status", "PASSED")
            if reusable
            else scientific.get("status", "RESULT_NOT_REUSABLE")
        )
        reason = str(
            validation_record.get("reason", "validation passed")
            if reusable
            else scientific.get("reason", "result is not reusable")
        )
    else:
        checks = {
            "execution_success": record.get("execution", {}).get("status") == "SUCCESS",
            "scientific_valid": scientific.get("status") == "VALID",
        }
        code = str(scientific.get("status", "UNKNOWN"))
        reason = str(scientific.get("reason", ""))
    validation = ArtifactValidation(
        ValidationStatus.PASSED if reusable else ValidationStatus.FAILED,
        checks,
        code,
        reason,
    )

    artifacts = record.get("artifacts", {})
    files = {
        str(role): str(item.get("path", ""))
        for role, item in artifacts.items()
        if isinstance(item, Mapping)
    } if isinstance(artifacts, Mapping) else {}
    parents = [str(item) for item in record.get("parent_artifacts", ())]
    source = record.get("source")
    if isinstance(source, Mapping) and source.get("scientific_artifact_id"):
        parents.append(str(source["scientific_artifact_id"]))

    cls: type[Artifact]
    if stage_type == "OPT":
        cls = OptimizationArtifact
    elif stage_type == "FREQ":
        cls = FrequencyArtifact
    elif stage_type == "SP":
        cls = SinglePointArtifact
    elif stage_type == "MULTIWFN_ESP":
        cls = DensityArtifact
    elif stage_type.startswith("MULTIWFN_"):
        cls = AnalysisArtifact
    else:
        cls = CalculationArtifact
    metadata: dict[str, object] = {"stage_type": stage_type}
    lineage = record.get("lineage")
    if isinstance(lineage, Mapping) and lineage.get("parent_target_id"):
        metadata["parent_calculation"] = str(lineage["parent_target_id"])
    if isinstance(source, Mapping) and source.get("target_id"):
        metadata["source_calculation"] = str(source["target_id"])
    evidence = record.get("evidence")
    if isinstance(evidence, Mapping) and evidence.get("final_energy_hartree") is not None:
        metadata["final_energy_hartree"] = evidence["final_energy_hartree"]
    return cls(
        producing_calculation=target_id,
        method=method,
        basis=basis,
        protocol=protocol,
        parent_artifacts=tuple(dict.fromkeys(parents)),
        files=files,
        validation=validation,
        provenance={
            "attempt": dict(record.get("attempt", {})),
            "provenance": dict(record.get("provenance", {})),
            "validation": dict(record.get("validation", {})),
        },
        metadata=metadata,
    )


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ARTIFACT_TYPES",
    "AnalysisArtifact",
    "Artifact",
    "ArtifactCompatibilityError",
    "ArtifactValidation",
    "CPInteractionArtifact",
    "CalculationArtifact",
    "DensityArtifact",
    "DimerEnergyArtifact",
    "EnergyArtifact",
    "FragmentEnergyArtifact",
    "FrequencyArtifact",
    "IGMHArtifact",
    "InteractionEnergyArtifact",
    "LEDArtifact",
    "OptimizationArtifact",
    "SinglePointArtifact",
    "ValidationStatus",
    "WavefunctionArtifact",
    "artifact_from_dict",
    "artifact_from_result",
    "artifact_matches_type",
    "validate_artifact_compatibility",
]
