"""Typed scientific artifacts with explicit provenance and compatibility rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Mapping, Sequence

from .execution_contract import (
    ComputationalTask,
    ExecutionContractError,
    ExecutionIntent,
    canonical_stage_type,
)
from .execution_layout import (
    ExecutionLayout,
    ExecutionLayoutError,
    resolve_internal_path,
    resolve_recorded_layout,
)
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

        return stable_hash(self._identity_payload())

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "artifact_type": self.artifact_type,
            "producing_calculation": self.producing_calculation,
            "method": self.method,
            "basis": self.basis,
            "protocol": dict(self.protocol),
            "parent_artifacts": list(self.parent_artifacts),
            "metadata": dict(self.metadata),
        }

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


@dataclass(frozen=True)
class StructureArtifact(Artifact):
    """First-class identity for an input, generated, or calculated structure."""

    TYPE: ClassVar[str] = "StructureArtifact"

    source: str = ""
    format: str = ""
    atom_count: int | None = None
    elemental_composition: Mapping[str, int] = field(default_factory=dict)
    charge: int | None = None
    multiplicity: int | None = None
    geometry_hash: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.atom_count is not None and self.atom_count < 1:
            raise ValueError("structure atom_count must be positive when provided")
        composition = {
            str(element): int(count)
            for element, count in self.elemental_composition.items()
        }
        if any(not element or count < 1 for element, count in composition.items()):
            raise ValueError("elemental composition requires positive counts")
        if self.charge is not None and not isinstance(self.charge, int):
            raise ValueError("structure charge must be an integer when provided")
        if self.multiplicity is not None and (
            not isinstance(self.multiplicity, int) or self.multiplicity < 1
        ):
            raise ValueError("structure multiplicity must be a positive integer")
        object.__setattr__(self, "source", str(self.source))
        object.__setattr__(self, "format", str(self.format).casefold().lstrip("."))
        object.__setattr__(self, "elemental_composition", composition)
        object.__setattr__(self, "geometry_hash", str(self.geometry_hash))

    def _identity_payload(self) -> dict[str, object]:
        payload = super()._identity_payload()
        payload["structure"] = {
            "source": self.source,
            "format": self.format,
            "atom_count": self.atom_count,
            "elemental_composition": dict(sorted(self.elemental_composition.items())),
            "charge": self.charge,
            "multiplicity": self.multiplicity,
            "geometry_hash": self.geometry_hash,
        }
        return payload

    def to_dict(self) -> dict[str, object]:
        value = super().to_dict()
        value.update(
            {
                "source": self.source,
                "format": self.format,
                "atom_count": self.atom_count,
                "elemental_composition": dict(
                    sorted(self.elemental_composition.items())
                ),
                "charge": self.charge,
                "multiplicity": self.multiplicity,
                "geometry_hash": self.geometry_hash,
            }
        )
        return value


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


class LEDFragmentReferenceArtifact(FragmentEnergyArtifact):
    """Frozen fragment evaluated with partner basis functions for LED assembly."""

    TYPE = "LEDFragmentReferenceArtifact"


class InteractionEnergyArtifact(EnergyArtifact):
    TYPE = "InteractionEnergyArtifact"


class CPInteractionArtifact(InteractionEnergyArtifact):
    TYPE = "CPInteractionArtifact"


class DeformationEnergyArtifact(EnergyArtifact):
    """Derived energy cost between distorted and relaxed structures."""

    TYPE = "DeformationEnergyArtifact"


@dataclass(frozen=True)
class LEDArtifact(AnalysisArtifact):
    """Validated numerical local-energy decomposition."""

    TYPE: ClassVar[str] = "LEDArtifact"

    led_result: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "led_result", dict(self.led_result))

    def _identity_payload(self) -> dict[str, object]:
        payload = super()._identity_payload()

        def without_paths(value: object) -> object:
            if isinstance(value, Mapping):
                return {
                    str(key): without_paths(item)
                    for key, item in value.items()
                    if key != "source_path"
                }
            if isinstance(value, (list, tuple)):
                return [without_paths(item) for item in value]
            return value

        if self.led_result:
            payload["led_result"] = without_paths(self.led_result)
        return payload

    def to_dict(self) -> dict[str, object]:
        value = super().to_dict()
        value["led_result"] = dict(self.led_result)
        return value


class DensityArtifact(AnalysisArtifact):
    TYPE = "DensityArtifact"


class IGMHArtifact(AnalysisArtifact):
    TYPE = "IGMHArtifact"


class StackingTemplateArtifact(Artifact):
    """Periodic-structure-derived registry for assembling a molecular pair."""

    TYPE = "StackingTemplateArtifact"


@dataclass(frozen=True)
class DimerStructureArtifact(StructureArtifact):
    """Ordered two-component structure assembled from explicit parent structures."""

    TYPE: ClassVar[str] = "DimerStructureArtifact"


class ConstrainedOptimizationArtifact(OptimizationArtifact):
    """Optimization result produced under an explicit structural constraint set."""

    TYPE = "ConstrainedOptimizationArtifact"


class ExcitedStateArtifact(CalculationArtifact):
    """Electronic excited-state calculation with explicit state-selection intent."""

    TYPE = "ExcitedStateArtifact"


class NTOArtifact(AnalysisArtifact):
    """Natural-transition-orbital analysis derived from an excited-state result."""

    TYPE = "NTOArtifact"


class HoleElectronArtifact(AnalysisArtifact):
    """Hole/electron separation analysis derived from an excited-state result."""

    TYPE = "HoleElectronArtifact"


ARTIFACT_TYPES: dict[str, type[Artifact]] = {
    item.TYPE: item
    for item in (
        Artifact,
        CalculationArtifact,
        EnergyArtifact,
        WavefunctionArtifact,
        AnalysisArtifact,
        StructureArtifact,
        OptimizationArtifact,
        FrequencyArtifact,
        SinglePointArtifact,
        DimerEnergyArtifact,
        FragmentEnergyArtifact,
        LEDFragmentReferenceArtifact,
        InteractionEnergyArtifact,
        CPInteractionArtifact,
        DeformationEnergyArtifact,
        LEDArtifact,
        DensityArtifact,
        IGMHArtifact,
        StackingTemplateArtifact,
        DimerStructureArtifact,
        ConstrainedOptimizationArtifact,
        ExcitedStateArtifact,
        NTOArtifact,
        HoleElectronArtifact,
    )
}


class ArtifactCompatibilityError(ValueError):
    """Raised when parent artifacts cannot support a derived scientific result."""

    code = "FAILED_ARTIFACT_COMPATIBILITY"


def artifact_from_dict(value: Mapping[str, Any]) -> Artifact:
    if value.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported scientific artifact schema")
    artifact_type = str(value.get("artifact_type", ""))
    cls = ARTIFACT_TYPES.get(artifact_type)
    if cls is None:
        raise ValueError(f"unsupported scientific artifact type: {artifact_type!r}")
    arguments: dict[str, object] = {
        "producing_calculation": str(value["producing_calculation"]),
        "method": str(value["method"]) if value.get("method") is not None else None,
        "basis": str(value["basis"]) if value.get("basis") is not None else None,
        "protocol": dict(value.get("protocol", {})),
        "parent_artifacts": tuple(str(item) for item in value.get("parent_artifacts", ())),
        "files": {str(k): str(v) for k, v in dict(value.get("files", {})).items()},
        "validation": ArtifactValidation.from_dict(dict(value.get("validation", {}))),
        "provenance": dict(value.get("provenance", {})),
        "metadata": dict(value.get("metadata", {})),
    }
    if issubclass(cls, StructureArtifact):
        arguments.update(
            {
                "source": str(value.get("source", "")),
                "format": str(value.get("format", "")),
                "atom_count": (
                    int(value["atom_count"])
                    if value.get("atom_count") is not None
                    else None
                ),
                "elemental_composition": {
                    str(k): int(v)
                    for k, v in dict(
                        value.get("elemental_composition", {})
                    ).items()
                },
                "charge": (
                    int(value["charge"])
                    if value.get("charge") is not None
                    else None
                ),
                "multiplicity": (
                    int(value["multiplicity"])
                    if value.get("multiplicity") is not None
                    else None
                ),
                "geometry_hash": str(value.get("geometry_hash", "")),
            }
        )
    if issubclass(cls, LEDArtifact):
        arguments["led_result"] = dict(value.get("led_result", {}))
    artifact = cls(
        **arguments,
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


def _deformation_protocol_signature(artifact: Artifact) -> tuple[str, str, str]:
    protocol = {
        key: value
        for key, value in artifact.protocol.items()
        if key
        not in {
            "fragment",
            "fragment_id",
            "geometry_role",
            "geometry_state",
            "role",
        }
    }
    return _normal(artifact.method), _normal(artifact.basis), stable_hash(protocol)


def _validate_deformation_parents(parents: Sequence[Artifact]) -> None:
    if not parents or any(
        not isinstance(parent, FragmentEnergyArtifact) for parent in parents
    ):
        raise ArtifactCompatibilityError(
            "DeformationEnergyArtifact requires FragmentEnergyArtifact parents"
        )

    by_fragment: dict[str, dict[str, FragmentEnergyArtifact]] = {}
    identities: dict[str, tuple[int, int]] = {}
    for parent in parents:
        fragment_id = parent.metadata.get("fragment_id")
        geometry_state = parent.metadata.get("geometry_state")
        charge = parent.metadata.get("charge")
        multiplicity = parent.metadata.get("multiplicity")
        if not isinstance(fragment_id, str) or not fragment_id.strip():
            raise ArtifactCompatibilityError(
                "deformation parent energies require a fragment_id"
            )
        if geometry_state not in {"distorted", "relaxed"}:
            raise ArtifactCompatibilityError(
                "deformation parent energies require geometry_state "
                "distorted or relaxed"
            )
        if not isinstance(charge, int) or isinstance(charge, bool):
            raise ArtifactCompatibilityError(
                "deformation parent energies require an integer charge"
            )
        if (
            not isinstance(multiplicity, int)
            or isinstance(multiplicity, bool)
            or multiplicity < 1
        ):
            raise ArtifactCompatibilityError(
                "deformation parent energies require a positive multiplicity"
            )
        states = by_fragment.setdefault(fragment_id, {})
        if geometry_state in states:
            raise ArtifactCompatibilityError(
                f"fragment {fragment_id!r} has duplicate {geometry_state} energies"
            )
        states[geometry_state] = parent
        identity = (charge, multiplicity)
        if fragment_id in identities and identities[fragment_id] != identity:
            raise ArtifactCompatibilityError(
                f"fragment {fragment_id!r} has incompatible charge or multiplicity"
            )
        identities[fragment_id] = identity

    incomplete = sorted(
        fragment_id
        for fragment_id, states in by_fragment.items()
        if set(states) != {"distorted", "relaxed"}
    )
    if incomplete:
        raise ArtifactCompatibilityError(
            "deformation energy requires distorted and relaxed energies for the same "
            f"fragment identity: {', '.join(incomplete)}"
        )

    signatures = {_deformation_protocol_signature(parent) for parent in parents}
    if len(signatures) != 1:
        raise ArtifactCompatibilityError(
            "deformation parent energies use incompatible methods, bases, or protocols"
        )


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

    if isinstance(artifact, DeformationEnergyArtifact):
        _validate_deformation_parents(selected)

    if isinstance(artifact, LEDArtifact):
        if artifact.protocol.get("led_contract") == "intermolecular_six_component_v1":
            dimers = [
                parent
                for parent in selected
                if isinstance(parent, DimerEnergyArtifact)
                and parent.protocol.get("led_role") == "dimer"
                and parent.protocol.get("led") is True
                and "dlpno" in _normal(parent.method)
            ]
            references = [
                parent
                for parent in selected
                if isinstance(parent, LEDFragmentReferenceArtifact)
                and parent.protocol.get("led_role") == "fragment_reference"
            ]
            if len(selected) != 3 or len(dimers) != 1 or len(references) != 2:
                raise ArtifactCompatibilityError(
                    "six-component LEDArtifact requires one dimer LED result and "
                    "two LED fragment-reference results"
                )
            if any(
                parent.protocol.get("reference_semantics")
                != "FROZEN_FRAGMENT_IN_DIMER_BASIS"
                for parent in references
            ):
                raise ArtifactCompatibilityError(
                    "LED fragment references require FROZEN_FRAGMENT_IN_DIMER_BASIS semantics"
                )
            method_contracts = {
                parent.protocol.get("led_method_contract_hash")
                for parent in selected
            }
            geometry_hashes = {
                parent.protocol.get("full_dimer_geometry_hash")
                for parent in selected
            }
            partitions = {
                parent.protocol.get("fragment_partition_hash")
                for parent in selected
            }
            if None in method_contracts or len(method_contracts) != 1:
                raise ArtifactCompatibilityError(
                    "LED parent calculations use incompatible scientific inputs"
                )
            if None in geometry_hashes or len(geometry_hashes) != 1:
                raise ArtifactCompatibilityError(
                    "LED parent calculations use different dimer geometries"
                )
            if None in partitions or len(partitions) != 1:
                raise ArtifactCompatibilityError(
                    "LED parent calculations use different fragment partitions"
                )
            if not artifact.led_result:
                raise ArtifactCompatibilityError(
                    "six-component LEDArtifact requires validated numerical results"
                )
        else:
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

    if isinstance(artifact, DimerStructureArtifact):
        templates = [
            parent for parent in selected if isinstance(parent, StackingTemplateArtifact)
        ]
        structures = [parent for parent in selected if isinstance(parent, StructureArtifact)]
        if len(templates) != 1 or not structures:
            raise ArtifactCompatibilityError(
                "DimerStructureArtifact requires a StackingTemplateArtifact and "
                "at least one StructureArtifact parent"
            )

    if isinstance(artifact, ConstrainedOptimizationArtifact):
        if not any(isinstance(parent, StructureArtifact) for parent in selected):
            raise ArtifactCompatibilityError(
                "ConstrainedOptimizationArtifact requires a StructureArtifact parent"
            )
        constraints = artifact.protocol.get("constraints")
        if not isinstance(constraints, Mapping) or not constraints.get("fixed_regions"):
            raise ArtifactCompatibilityError(
                "ConstrainedOptimizationArtifact requires explicit fixed_regions"
            )

    if isinstance(artifact, ExcitedStateArtifact):
        if not any(isinstance(parent, StructureArtifact) for parent in selected):
            raise ArtifactCompatibilityError(
                "ExcitedStateArtifact requires a StructureArtifact parent"
            )
        roots = artifact.protocol.get("number_of_roots")
        if isinstance(roots, bool) or not isinstance(roots, int) or roots < 1:
            raise ArtifactCompatibilityError(
                "ExcitedStateArtifact requires a positive number_of_roots"
            )
        if not isinstance(artifact.protocol.get("state_selection"), Mapping):
            raise ArtifactCompatibilityError(
                "ExcitedStateArtifact requires state_selection metadata"
            )
        if not artifact.metadata.get("source_geometry_hash"):
            raise ArtifactCompatibilityError(
                "ExcitedStateArtifact requires a source_geometry_hash"
            )
        excited_state_contract = artifact.metadata.get("excited_state_contract")
        if excited_state_contract in {"quantitative_v1", "quantitative_v2"}:
            records = artifact.metadata.get("excited_states")
            selected_states = artifact.metadata.get("selected_state_indices")
            if (
                not isinstance(records, Sequence)
                or isinstance(records, (str, bytes))
                or not records
                or not all(isinstance(item, Mapping) for item in records)
            ):
                raise ArtifactCompatibilityError(
                    "quantitative ExcitedStateArtifact requires excited-state records"
                )
            state_indices = {
                item.get("state_index") for item in records if isinstance(item, Mapping)
            }
            if (
                not isinstance(selected_states, Sequence)
                or isinstance(selected_states, (str, bytes))
                or not selected_states
                or not set(selected_states).issubset(state_indices)
            ):
                raise ArtifactCompatibilityError(
                    "quantitative ExcitedStateArtifact requires valid selected states"
                )
            if not all(
                artifact.metadata.get(key)
                for key in (
                    "geometry_source_artifact",
                    "functional",
                    "program",
                    "program_version",
                    "state_selection_rationale",
                )
            ):
                raise ArtifactCompatibilityError(
                    "quantitative ExcitedStateArtifact provenance is incomplete"
                )
            if excited_state_contract == "quantitative_v2":
                selected_identities = artifact.metadata.get(
                    "selected_state_identities"
                )
                record_identities = {
                    (
                        str(item.get("spin_manifold", "")).casefold(),
                        item.get("local_state_index", item.get("state_index")),
                    )
                    for item in records
                    if isinstance(item, Mapping)
                }
                if (
                    not isinstance(selected_identities, Sequence)
                    or isinstance(selected_identities, (str, bytes))
                    or not selected_identities
                    or not all(isinstance(item, Mapping) for item in selected_identities)
                ):
                    raise ArtifactCompatibilityError(
                        "quantitative_v2 ExcitedStateArtifact requires canonical selections"
                    )
                selected_identity_keys = {
                    (
                        str(item.get("spin_manifold", "")).casefold(),
                        item.get("local_state_index"),
                    )
                    for item in selected_identities
                    if isinstance(item, Mapping)
                }
                if not selected_identity_keys.issubset(record_identities):
                    raise ArtifactCompatibilityError(
                        "canonical selected states are absent from excited-state records"
                    )
                if not all(
                    artifact.metadata.get(key)
                    for key in (
                        "scientific_protocol_hash",
                        "parser_version",
                        "fixture_tested_grammar_version",
                        "source_output_identity",
                        "execution_attempt",
                    )
                ):
                    raise ArtifactCompatibilityError(
                        "quantitative_v2 parser provenance is incomplete"
                    )
                source_output_identity = artifact.metadata.get(
                    "source_output_identity"
                )
                if not isinstance(source_output_identity, Mapping) or not all(
                    source_output_identity.get(key)
                    for key in ("source_sha256", "source_size_bytes")
                ):
                    raise ArtifactCompatibilityError(
                        "quantitative_v2 source-output identity is incomplete"
                    )

    if isinstance(artifact, NTOArtifact):
        if not any(isinstance(parent, ExcitedStateArtifact) for parent in selected):
            raise ArtifactCompatibilityError(
                "NTOArtifact requires an ExcitedStateArtifact parent"
            )
        if artifact.metadata.get("nto_contract") == "orbital_pairs_v1":
            pairs = artifact.metadata.get("orbital_pairs")
            runtime = artifact.provenance.get("runtime")
            if (
                not artifact.metadata.get("excited_state_artifact")
                or not artifact.metadata.get("generation_method")
                or not isinstance(pairs, Sequence)
                or isinstance(pairs, (str, bytes))
                or not pairs
                or not isinstance(runtime, Mapping)
                or not runtime.get("program")
                or not runtime.get("version")
            ):
                raise ArtifactCompatibilityError(
                    "state-resolved NTOArtifact provenance is incomplete"
                )
        if artifact.metadata.get("nto_contract") == "multiwfn_nto_v1":
            pairs = artifact.metadata.get("orbital_pairs")
            identity = artifact.metadata.get("selected_state_identity")
            runtime = artifact.provenance.get("runtime")
            execution = artifact.provenance.get("execution_attempt")
            if (
                not isinstance(identity, Mapping)
                or not identity.get("spin_manifold")
                or not identity.get("local_state_index")
                or not isinstance(pairs, Sequence)
                or isinstance(pairs, (str, bytes))
                or not pairs
                or not all(isinstance(item, Mapping) for item in pairs)
                or not artifact.metadata.get("output_mwfn_file")
                or not artifact.metadata.get("source_geometry_hash")
                or not artifact.metadata.get("scientific_protocol_hash")
                or not artifact.metadata.get("renderer_grammar")
                or not artifact.metadata.get("parser_grammar")
                or not artifact.metadata.get("stdin_sha256")
                or not isinstance(
                    artifact.metadata.get("source_wavefunction_identity"), Mapping
                )
                or not isinstance(
                    artifact.metadata.get("orca_source_output_identity"), Mapping
                )
                or not isinstance(runtime, Mapping)
                or not runtime.get("version")
                or not isinstance(execution, Mapping)
                or execution.get("process_exit_code") != 0
            ):
                raise ArtifactCompatibilityError(
                    "Multiwfn NTO artifact execution or parser provenance is incomplete"
                )

    if isinstance(artifact, HoleElectronArtifact):
        if not any(isinstance(parent, ExcitedStateArtifact) for parent in selected):
            raise ArtifactCompatibilityError(
                "HoleElectronArtifact requires an ExcitedStateArtifact parent"
            )
        protocol = artifact.metadata.get("multiwfn_protocol")
        if not isinstance(protocol, Mapping) or not protocol:
            raise ArtifactCompatibilityError(
                "HoleElectronArtifact requires Multiwfn protocol metadata"
            )
        if artifact.metadata.get("hole_electron_contract") == "fragment_resolved_v1":
            partition = artifact.metadata.get("fragment_definitions")
            hole = artifact.metadata.get("hole_population")
            electron = artifact.metadata.get("electron_population")
            if (
                not artifact.metadata.get("source_structure_artifact")
                or not artifact.metadata.get("source_geometry_hash")
                or not artifact.metadata.get("analyzed_state_index")
                or not isinstance(partition, Mapping)
                or not isinstance(hole, Mapping)
                or not isinstance(electron, Mapping)
                or not hole
                or set(hole) != set(electron)
            ):
                raise ArtifactCompatibilityError(
                    "fragment-resolved HoleElectronArtifact metadata is incomplete"
                )
            try:
                normalized = (
                    abs(sum(float(value) for value in hole.values()) - 1.0) <= 1.0e-6
                    and abs(
                        sum(float(value) for value in electron.values()) - 1.0
                    )
                    <= 1.0e-6
                )
            except (TypeError, ValueError):
                normalized = False
            if not normalized:
                raise ArtifactCompatibilityError(
                    "fragment-resolved hole/electron populations must sum to 1"
                )
        if (
            artifact.metadata.get("hole_electron_contract")
            == "multiwfn_nonfragment_hea_v1"
        ):
            identity = artifact.metadata.get("selected_state_identity")
            D_validation = artifact.metadata.get("D_validation")
            runtime = artifact.provenance.get("runtime")
            execution = artifact.provenance.get("execution_attempt")
            if (
                artifact.metadata.get("fragment_resolved") is not False
                or artifact.metadata.get("fragment_contributions")
                or artifact.metadata.get("hole_population")
                or artifact.metadata.get("electron_population")
                or not isinstance(identity, Mapping)
                or not identity.get("spin_manifold")
                or not identity.get("local_state_index")
                or not isinstance(D_validation, Mapping)
                or D_validation.get("consistent") is not True
                or not artifact.metadata.get("source_geometry_hash")
                or not artifact.metadata.get("scientific_protocol_hash")
                or not artifact.metadata.get("renderer_grammar")
                or not artifact.metadata.get("parser_grammar")
                or not artifact.metadata.get("stdin_sha256")
                or not isinstance(
                    artifact.metadata.get("source_wavefunction_identity"), Mapping
                )
                or not isinstance(
                    artifact.metadata.get("orca_source_output_identity"), Mapping
                )
                or not isinstance(runtime, Mapping)
                or not runtime.get("version")
                or not isinstance(execution, Mapping)
                or execution.get("process_exit_code") != 0
            ):
                raise ArtifactCompatibilityError(
                    "Multiwfn non-fragment HEA provenance is incomplete"
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


def artifact_from_result(
    record: Mapping[str, Any], *, metadata_path: Path | str | None = None
) -> Artifact:
    """Map a legacy ORCA or Multiwfn result to the typed artifact model."""

    target = record.get("target")
    if not isinstance(target, Mapping):
        raise ValueError("result lacks a calculation target")
    target_id = str(target.get("target_id", ""))
    if not target_id:
        raise ValueError("result target lacks target_id")
    stage_type = canonical_stage_type(target.get("stage_type", ""))
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

    layout = None
    layout_record = record.get("execution_layout")
    if layout_record is not None:
        if not isinstance(layout_record, Mapping):
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: result execution layout is invalid"
            )
        if metadata_path is None:
            layout = ExecutionLayout.from_mapping(layout_record)
        else:
            layout = resolve_recorded_layout(
                layout_record, metadata_path=metadata_path
            )
        attempt_record = record.get("attempt")
        if not isinstance(attempt_record, Mapping):
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: result attempt identity is missing"
            )
        layout.validate_attempt_identity(str(attempt_record.get("attempt_id", "")))
    artifacts = record.get("artifacts", {})
    files: dict[str, str] = {}
    if isinstance(artifacts, Mapping):
        for role, item in artifacts.items():
            if not isinstance(item, Mapping):
                continue
            path = Path(str(item.get("path", "")))
            if layout is not None and isinstance(layout_record, Mapping):
                path = resolve_internal_path(
                    path,
                    layout_record=layout_record,
                    resolved_layout=layout,
                )
            files[str(role)] = str(path.resolve()) if layout is not None else str(path)
    parents = [str(item) for item in record.get("parent_artifacts", ())]
    source = record.get("source")
    if isinstance(source, Mapping) and source.get("scientific_artifact_id"):
        parents.append(str(source["scientific_artifact_id"]))

    cls: type[Artifact]
    stage_tasks = {
        "OPT": ComputationalTask.OPTIMIZATION,
        "FREQ": ComputationalTask.FREQUENCY,
        "SP": ComputationalTask.SINGLE_POINT,
        "TDDFT": ComputationalTask.EXCITED_STATE,
    }
    task_artifacts: dict[ComputationalTask, type[Artifact]] = {
        ComputationalTask.OPTIMIZATION: OptimizationArtifact,
        ComputationalTask.FREQUENCY: FrequencyArtifact,
        ComputationalTask.SINGLE_POINT: SinglePointArtifact,
        ComputationalTask.EXCITED_STATE: ExcitedStateArtifact,
    }
    if stage_type in stage_tasks:
        task = stage_tasks[stage_type]
        execution_intent = record.get("execution_intent")
        if execution_intent is not None:
            if not isinstance(execution_intent, Mapping):
                raise ExecutionContractError(
                    f"{ExecutionContractError.code}: result execution intent is invalid"
                )
            try:
                parsed_intent = ExecutionIntent.from_mapping(execution_intent)
            except (KeyError, TypeError, ValueError) as exc:
                raise ExecutionContractError(
                    f"{ExecutionContractError.code}: result execution intent is invalid"
                ) from exc
            if parsed_intent.stage_type != stage_type or parsed_intent.task is not task:
                raise ExecutionContractError(
                    f"{ExecutionContractError.code}: result execution intent "
                    f"does not match stage {stage_type}"
                )
        if task is ComputationalTask.SINGLE_POINT and protocol.get(
            "led_role"
        ) == "fragment_reference":
            cls = LEDFragmentReferenceArtifact
        elif task is ComputationalTask.SINGLE_POINT and protocol.get(
            "led_role"
        ) == "dimer":
            cls = DimerEnergyArtifact
        else:
            cls = task_artifacts[task]
    elif stage_type == "MULTIWFN_ESP":
        cls = DensityArtifact
    elif stage_type == "MULTIWFN_IGMH":
        cls = IGMHArtifact
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
    execution_provenance: dict[str, object] = {}
    if layout is not None:
        execution_provenance = {
            "execution_layout": layout.to_dict(),
            "attempt_id": layout.attempt_identifier,
            "producing_execution_node": layout.workflow_node_identifier,
            "resolved_output_path": str(layout.output_directory),
        }
    geometry_provenance: dict[str, object] = {}
    geometry_input = record.get("geometry_input")
    if isinstance(geometry_input, Mapping):
        geometry_provenance = {"geometry_input": dict(geometry_input)}
        if stage_type == "TDDFT" and geometry_input.get("geometry_hash"):
            metadata["source_geometry_hash"] = str(
                geometry_input["geometry_hash"]
            )
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
            **execution_provenance,
            **geometry_provenance,
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
    "ConstrainedOptimizationArtifact",
    "DeformationEnergyArtifact",
    "DensityArtifact",
    "DimerEnergyArtifact",
    "DimerStructureArtifact",
    "EnergyArtifact",
    "ExcitedStateArtifact",
    "FragmentEnergyArtifact",
    "FrequencyArtifact",
    "IGMHArtifact",
    "HoleElectronArtifact",
    "InteractionEnergyArtifact",
    "LEDArtifact",
    "LEDFragmentReferenceArtifact",
    "NTOArtifact",
    "OptimizationArtifact",
    "SinglePointArtifact",
    "StackingTemplateArtifact",
    "StructureArtifact",
    "ValidationStatus",
    "WavefunctionArtifact",
    "artifact_from_dict",
    "artifact_from_result",
    "artifact_matches_type",
    "validate_artifact_compatibility",
]
