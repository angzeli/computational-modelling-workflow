"""Reusable TDDFT/TDA, state-selection, and NTO planning contracts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import math
from pathlib import Path
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
from cmw.core.provenance import stable_hash
from cmw.molecular.excited_states import (
    SPIN_MANIFOLDS,
    ExcitedStateRecord,
    StateSelectionResult,
    StateSelectionStatus,
)
from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.orca.status import StageType


class ExcitedStateContractError(ValueError):
    """Raised when an excited-state calculation plan is incomplete."""

    code = "FAILED_PROTOCOL_MISMATCH"


CALCULATION_SPIN_SCOPES = frozenset({*SPIN_MANIFOLDS, "mixed"})


@dataclass(frozen=True)
class NTOOrbitalRecord:
    """One hole/electron NTO pair for one excited state."""

    state_index: int
    pair_index: int
    weight: float
    hole_file_role: str
    electron_file_role: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.state_index, "state_index"),
            (self.pair_index, "pair_index"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ExcitedStateContractError(
                    f"NTO {name} must be a positive integer"
                )
        weight = float(self.weight)
        if not math.isfinite(weight) or weight < 0:
            raise ExcitedStateContractError(
                "NTO pair weight must be a non-negative finite value"
            )
        for role in (self.hole_file_role, self.electron_file_role):
            if not role.strip():
                raise ExcitedStateContractError("NTO orbital file roles are required")
        object.__setattr__(self, "weight", weight)

    def to_dict(self) -> dict[str, object]:
        return {
            "state_index": self.state_index,
            "pair_index": self.pair_index,
            "weight": self.weight,
            "hole_file_role": self.hole_file_role,
            "electron_file_role": self.electron_file_role,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "NTOOrbitalRecord":
        return cls(
            int(value["state_index"]),
            int(value["pair_index"]),
            float(value["weight"]),
            str(value["hole_file_role"]),
            str(value["electron_file_role"]),
        )


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
    functional: str | None = None
    spin_manifold: str = "singlet"
    nto_threshold: float = 1.0e-4

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
        functional = str(self.functional or self.method).strip()
        if not functional:
            raise ExcitedStateContractError("excited-state functional is required")
        spin_manifold = str(self.spin_manifold).strip().casefold()
        if spin_manifold not in CALCULATION_SPIN_SCOPES:
            raise ExcitedStateContractError(
                "spin manifold must be singlet, triplet, or mixed"
            )
        nto_threshold = float(self.nto_threshold)
        if not math.isfinite(nto_threshold) or nto_threshold <= 0:
            raise ExcitedStateContractError(
                "NTO threshold must be a positive finite value"
            )
        object.__setattr__(self, "state_selection", selection)
        object.__setattr__(self, "keywords", tuple(self.keywords))
        object.__setattr__(
            self, "additional_tddft_lines", tuple(self.additional_tddft_lines)
        )
        object.__setattr__(self, "visualization", dict(self.visualization))
        object.__setattr__(self, "functional", functional)
        object.__setattr__(self, "spin_manifold", spin_manifold)
        object.__setattr__(self, "nto_threshold", nto_threshold)

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
        if self.spin_manifold in {"triplet", "mixed"}:
            rows.append("  triplets true")
        if self.generate_ntos:
            rows.append("  DoNTO true")
            states = self.state_selection.get("states")
            if isinstance(states, Sequence) and not isinstance(states, (str, bytes)):
                rows.append("  NTOStates " + ",".join(str(item) for item in states))
            rows.append(f"  NTOThresh {self.nto_threshold:g}")
        rows.extend(f"  {line.strip()}" for line in self.additional_tddft_lines)
        rows.append("end")
        return "\n".join(rows)

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "basis": self.basis,
            "functional": self.functional,
            "theory": "TDA" if self.tda else "TDDFT",
            "spin_manifold": self.spin_manifold,
            "number_of_roots": self.number_of_roots,
            "state_selection": dict(self.state_selection),
            "generate_ntos": self.generate_ntos,
            "keywords": list(self.keywords),
            "additional_tddft_lines": list(self.additional_tddft_lines),
            "visualization": dict(self.visualization),
            "nto_threshold": self.nto_threshold,
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
        "source_valid": source.validation.passed,
        "parent_identity": False,
        "geometry_source_artifact": artifact.metadata.get(
            "geometry_source_artifact"
        )
        == source.artifact_id,
        "source_geometry_hash": artifact.metadata.get("source_geometry_hash")
        == source.geometry_hash,
        "method_match": artifact.method == protocol.method,
        "basis_match": artifact.basis == protocol.basis,
        "functional_match": artifact.protocol.get("functional")
        == protocol.functional
        and artifact.metadata.get("functional") == protocol.functional,
        "spin_manifold_match": artifact.protocol.get("spin_manifold")
        == protocol.spin_manifold,
        "root_count_match": artifact.protocol.get("number_of_roots")
        == protocol.number_of_roots,
        "state_selection_match": artifact.protocol.get("state_selection")
        == dict(protocol.state_selection),
        "orca_contract": False,
        "artifact_compatibility": False,
        "state_observables": False,
        "selection_rationale": False,
        "runtime_provenance": False,
        "output_available": False,
        "parsed_protocol": False,
        "source_output_identity": False,
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
        records = _state_records(artifact.metadata.get("excited_states"))
        selected = artifact.metadata.get("selected_state_indices")
        selected_identities = artifact.metadata.get("selected_state_identities")
        available_identities = {item.canonical_key for item in records}
        identity_selection_valid = False
        if isinstance(selected_identities, Sequence) and not isinstance(
            selected_identities, (str, bytes)
        ):
            parsed_identities = {
                (
                    str(item.get("spin_manifold", "")).casefold(),
                    int(item.get("local_state_index", 0)),
                )
                for item in selected_identities
                if isinstance(item, Mapping)
            }
            identity_selection_valid = bool(parsed_identities) and parsed_identities.issubset(
                available_identities
            )
        checks["state_observables"] = (
            all(item.state_index <= protocol.number_of_roots for item in records)
            and all(
                protocol.spin_manifold == "mixed"
                or item.spin_manifold == protocol.spin_manifold
                for item in records
            )
            and (
                protocol.spin_manifold != "mixed"
                or {item.spin_manifold for item in records} == set(SPIN_MANIFOLDS)
            )
            and (
                identity_selection_valid
                or (
                    isinstance(selected, list)
                    and bool(selected)
                    and set(int(item) for item in selected).issubset(
                        {item.state_index for item in records}
                    )
                )
            )
        )
        rationale = artifact.metadata.get("state_selection_rationale")
        checks["selection_rationale"] = (
            isinstance(rationale, str) and bool(rationale.strip())
        )
        runtime = artifact.provenance.get("runtime")
        checks["runtime_provenance"] = (
            isinstance(runtime, Mapping)
            and artifact.metadata.get("program") == runtime.get("program")
            and artifact.metadata.get("program_version") == runtime.get("version")
            and bool(runtime.get("program"))
            and bool(runtime.get("version"))
        )
        output = artifact.files.get("output")
        checks["output_available"] = (
            bool(output)
            and Path(str(output)).is_file()
            and Path(str(output)).stat().st_size > 0
        )
        if artifact.metadata.get("excited_state_contract") == "quantitative_v2":
            parser_evidence = artifact.provenance.get("parser")
            if not isinstance(parser_evidence, Mapping) or not checks["output_available"]:
                raise ExcitedStateContractError(
                    "parser-backed artifact lacks parser or output evidence"
                )
            _validate_parsed_records_against_protocol(
                records,
                protocol,
                runtime if isinstance(runtime, Mapping) else {},
                parser_evidence,
                Path(str(output)),
            )
            checks["parsed_protocol"] = True
            checks["source_output_identity"] = True
        else:
            checks["parsed_protocol"] = True
            checks["source_output_identity"] = True
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


def _state_records(value: object) -> tuple[ExcitedStateRecord, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ExcitedStateContractError("excited-state records must be a sequence")
    if not all(isinstance(item, Mapping) for item in value):
        raise ExcitedStateContractError("excited-state records must be mappings")
    records = tuple(ExcitedStateRecord.from_mapping(item) for item in value)
    if not records or len({item.canonical_key for item in records}) != len(records):
        raise ExcitedStateContractError(
            "excited-state records require unique canonical identities"
        )
    return records


def _validate_parsed_records_against_protocol(
    records: Sequence[ExcitedStateRecord],
    protocol: ExcitedStateProtocol,
    runtime_provenance: Mapping[str, object],
    parser_provenance: Mapping[str, object],
    output_path: Path,
) -> None:
    parsed = [item for item in records if item.protocol_metadata]
    if not parsed:
        return
    if len(parsed) != len(records):
        raise ExcitedStateContractError(
            "parsed and legacy excited-state records cannot be mixed"
        )
    protocol_identities = {
        stable_hash(dict(item.protocol_metadata)) for item in parsed
    }
    source_identities = {
        str(item.source_provenance.get("source_sha256", "")) for item in parsed
    }
    if len(protocol_identities) != 1 or len(source_identities) != 1 or "" in source_identities:
        raise ExcitedStateContractError(
            "parsed excited states do not share one protocol and source identity"
        )
    observed = parsed[0].protocol_metadata
    if (
        str(observed.get("functional", "")).casefold()
        != str(protocol.functional).casefold()
        or str(observed.get("basis", "")).casefold()
        != str(protocol.basis).casefold()
        or bool(observed.get("tda")) != protocol.tda
    ):
        raise ExcitedStateContractError(
            "parsed excited-state method, basis, or theory does not match the protocol"
        )
    expected_roots = {
        "singlet": int(observed.get("requested_singlet_roots", 0)),
        "triplet": int(observed.get("requested_triplet_roots", 0)),
    }
    required_manifolds = (
        set(SPIN_MANIFOLDS)
        if protocol.spin_manifold == "mixed"
        else {protocol.spin_manifold}
    )
    if any(
        expected_roots[manifold] != protocol.number_of_roots
        for manifold in required_manifolds
    ):
        raise ExcitedStateContractError(
            "parsed root counts do not match the excited-state protocol"
        )
    observed_version = str(observed.get("orca_version", ""))
    if observed_version != str(runtime_provenance.get("version", "")):
        raise ExcitedStateContractError(
            "parsed ORCA version does not match runtime provenance"
        )
    expected_sha = str(parser_provenance.get("source_sha256", ""))
    expected_size = parser_provenance.get("source_size_bytes")
    if not expected_sha or expected_size is None:
        raise ExcitedStateContractError(
            "parser provenance requires source hash and size"
        )
    if output_path.stat().st_size != int(expected_size):
        raise ExcitedStateContractError(
            "excited-state output size does not match parser provenance"
        )
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    if digest != expected_sha or source_identities != {expected_sha}:
        raise ExcitedStateContractError(
            "excited-state output hash does not match parsed records"
        )


def create_excited_state_artifact(
    source: StructureArtifact,
    protocol: ExcitedStateProtocol,
    states: Sequence[ExcitedStateRecord],
    *,
    state_selection_rationale: str,
    runtime_provenance: Mapping[str, object],
    files: Mapping[str, str],
    selection_results: Sequence[StateSelectionResult] = (),
    parser_provenance: Mapping[str, object] | None = None,
    execution_attempt: Mapping[str, object] | None = None,
    parents: Sequence[Artifact] = (),
    producing_calculation: str = "excited_state",
) -> ExcitedStateArtifact:
    """Create a manuscript-ready excited-state artifact from supplied evidence."""

    records = tuple(states)
    if not records or len({item.canonical_key for item in records}) != len(records):
        raise ExcitedStateContractError(
            "excited-state evidence requires unique canonical state records"
        )
    if any(item.state_index > protocol.number_of_roots for item in records):
        raise ExcitedStateContractError(
            "excited-state record exceeds the configured number of roots"
        )
    if any(
        protocol.spin_manifold != "mixed"
        and item.spin_manifold != protocol.spin_manifold
        for item in records
    ) or (
        protocol.spin_manifold == "mixed"
        and {item.spin_manifold for item in records} != set(SPIN_MANIFOLDS)
    ):
        raise ExcitedStateContractError(
            "excited-state spin metadata does not match the protocol"
        )
    rationale = str(state_selection_rationale).strip()
    if not rationale:
        raise ExcitedStateContractError("state-selection rationale is required")
    results = tuple(selection_results)
    selected_identities = tuple(
        item.selected_identity
        for item in results
        if item.status is StateSelectionStatus.SELECTED
        and item.selected_identity is not None
    )
    requested = protocol.state_selection.get("states")
    if not selected_identities:
        selected_indices = (
            tuple(int(item) for item in requested)
            if isinstance(requested, Sequence)
            and not isinstance(requested, (str, bytes))
            else tuple(item.state_index for item in records if item.selection_labels)
        )
        selected_identities = tuple(
            item.identity for item in records if item.state_index in selected_indices
        )
    available = {item.canonical_key for item in records}
    selected_keys = {
        (item.spin_manifold, item.local_state_index) for item in selected_identities
    }
    if not selected_keys or not selected_keys.issubset(available):
        raise ExcitedStateContractError(
            "selected excited states are absent from quantitative state evidence"
        )
    selected = tuple(item.local_state_index for item in selected_identities)
    program = runtime_provenance.get("program")
    version = runtime_provenance.get("version")
    if (
        not isinstance(program, str)
        or not program
        or not isinstance(version, str)
        or not version
    ):
        raise ExcitedStateContractError(
            "excited-state runtime provenance requires program and version"
        )
    output = files.get("output")
    if not output or not Path(output).is_file() or Path(output).stat().st_size < 1:
        raise ExcitedStateContractError(
            "excited-state output file is missing or empty"
        )
    parser_evidence = dict(parser_provenance or {})
    attempt_evidence = dict(execution_attempt or {})
    if parser_evidence and not attempt_evidence:
        raise ExcitedStateContractError(
            "parser-backed excited-state artifacts require execution-attempt provenance"
        )
    _validate_parsed_records_against_protocol(
        records,
        protocol,
        runtime_provenance,
        parser_evidence,
        Path(output),
    )
    parent_objects = {source.artifact_id: source}
    parent_objects.update({item.artifact_id: item for item in parents})
    validation = ArtifactValidation(
        ValidationStatus.PASSED,
        {
            "source_geometry": source.validation.passed,
            "state_records": True,
            "state_selection": True,
            "runtime_provenance": True,
            "output_available": True,
        },
        "VALID_EXCITED_STATE_ARTIFACT",
        "excited-state observables, selection, runtime, and lineage are complete",
    )
    artifact = ExcitedStateArtifact(
        producing_calculation=producing_calculation,
        method=protocol.method,
        basis=protocol.basis,
        protocol=protocol.to_dict(),
        parent_artifacts=tuple(parent_objects),
        files=dict(files),
        validation=validation,
        provenance={
            "runtime": dict(runtime_provenance),
            "parser": parser_evidence,
            "execution_attempt": attempt_evidence,
        },
        metadata={
            "excited_state_contract": (
                "quantitative_v2"
                if parser_provenance or results
                else "quantitative_v1"
            ),
            "geometry_source_artifact": source.artifact_id,
            "source_geometry_hash": source.geometry_hash,
            "scientific_protocol_hash": stable_hash(protocol.to_dict()),
            "functional": protocol.functional,
            "program": program,
            "program_version": version,
            "tddft_settings": {
                "theory": "TDA" if protocol.tda else "TDDFT",
                "spin_manifold": protocol.spin_manifold,
                "number_of_roots": protocol.number_of_roots,
            },
            "excited_states": [item.to_dict() for item in records],
            "selected_state_indices": list(selected),
            "selected_state_identities": [item.to_dict() for item in selected_identities],
            "state_selection_results": [item.to_dict() for item in results],
            "state_selection_rationale": rationale,
            "source_output_identity": parser_evidence,
            "parser_version": parser_evidence.get("parser_version"),
            "fixture_tested_grammar_version": parser_evidence.get(
                "fixture_tested_grammar_version"
            ),
            "execution_attempt": attempt_evidence,
        },
    )
    result = validate_excited_state_artifact(
        artifact,
        source=source,
        protocol=protocol,
        parents=parents,
    )
    if not result.passed:
        raise ExcitedStateContractError(result.reason)
    return replace(artifact, validation=result)


def validate_nto_artifact(
    artifact: NTOArtifact,
    *,
    excited_state: ExcitedStateArtifact,
) -> ArtifactValidation:
    """Fail closed on NTO state identity, orbital pairs, files, and runtime."""

    checks: dict[str, bool | None] = {
        "excited_state_parent": artifact.parent_artifacts
        == (excited_state.artifact_id,),
        "excited_state_valid": excited_state.validation.passed,
        "excited_state_identity": artifact.metadata.get(
            "excited_state_artifact"
        )
        == excited_state.artifact_id,
        "generation_method": False,
        "orbital_pairs": False,
        "orbital_files": False,
        "runtime_provenance": False,
        "visualization": isinstance(artifact.metadata.get("visualization"), Mapping),
    }
    try:
        generation_method = artifact.metadata.get("generation_method")
        checks["generation_method"] = (
            isinstance(generation_method, str) and bool(generation_method.strip())
        )
        raw_pairs = artifact.metadata.get("orbital_pairs")
        if not isinstance(raw_pairs, Sequence) or isinstance(raw_pairs, (str, bytes)):
            raise ExcitedStateContractError("NTO orbital_pairs must be a sequence")
        if not all(isinstance(item, Mapping) for item in raw_pairs):
            raise ExcitedStateContractError("NTO orbital_pairs must be mappings")
        pairs = tuple(NTOOrbitalRecord.from_mapping(item) for item in raw_pairs)
        pair_ids = {(item.state_index, item.pair_index) for item in pairs}
        excited_indices = {
            item.state_index
            for item in _state_records(excited_state.metadata.get("excited_states"))
        }
        checks["orbital_pairs"] = (
            bool(pairs)
            and len(pair_ids) == len(pairs)
            and all(item.state_index in excited_indices for item in pairs)
        )
        required_roles = {
            role
            for item in pairs
            for role in (item.hole_file_role, item.electron_file_role)
        }
        checks["orbital_files"] = all(
            role in artifact.files
            and Path(artifact.files[role]).is_file()
            and Path(artifact.files[role]).stat().st_size > 0
            for role in required_roles
        )
        runtime = artifact.provenance.get("runtime")
        checks["runtime_provenance"] = (
            isinstance(runtime, Mapping)
            and bool(runtime.get("program"))
            and bool(runtime.get("version"))
        )
        validate_artifact_compatibility(artifact, (excited_state,))
    except (ArtifactCompatibilityError, KeyError, TypeError, ValueError) as exc:
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
            "NTO artifact metadata, lineage, or orbital evidence is incomplete",
        )
    return ArtifactValidation(
        ValidationStatus.PASSED,
        checks,
        "VALID_NTO_ARTIFACT",
        "NTO state identity, orbitals, runtime, and lineage are complete",
    )


def create_nto_artifact(
    excited_state: ExcitedStateArtifact,
    orbital_pairs: Sequence[NTOOrbitalRecord],
    *,
    generation_method: str,
    runtime_provenance: Mapping[str, object],
    files: Mapping[str, str],
    visualization: Mapping[str, object] | None = None,
    producing_calculation: str = "natural_transition_orbitals",
) -> NTOArtifact:
    """Create a validated NTO artifact from explicit orbital-pair evidence."""

    pairs = tuple(orbital_pairs)
    method = str(generation_method).strip()
    if not method:
        raise ExcitedStateContractError("NTO generation method is required")
    artifact = NTOArtifact(
        producing_calculation=producing_calculation,
        method=excited_state.method,
        basis=excited_state.basis,
        protocol={
            "generation_method": method,
            "state_indices": sorted({item.state_index for item in pairs}),
        },
        parent_artifacts=(excited_state.artifact_id,),
        files=dict(files),
        validation=ArtifactValidation(
            ValidationStatus.PASSED,
            {"supplied_evidence": True},
            "VALID_NTO_ARTIFACT",
            "NTO evidence was supplied for validation",
        ),
        provenance={"runtime": dict(runtime_provenance)},
        metadata={
            "nto_contract": "orbital_pairs_v1",
            "excited_state_artifact": excited_state.artifact_id,
            "generation_method": method,
            "orbital_pairs": [item.to_dict() for item in pairs],
            "visualization": dict(visualization or {}),
        },
    )
    validation = validate_nto_artifact(artifact, excited_state=excited_state)
    if not validation.passed:
        raise ExcitedStateContractError(validation.reason)
    return replace(artifact, validation=validation)


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
            "geometry_source_artifact": source.artifact_id,
            "source_geometry_hash": source.geometry_hash,
            "functional": protocol.functional,
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
        metadata={
            "excited_state_artifact": excited.artifact_id,
            "generation_method": "ORCA TDDFT/TDA natural transition orbitals",
            "planned_state_selection": dict(protocol.state_selection),
            "visualization": dict(protocol.visualization),
        },
    )
    return excited, nto


__all__ = [
    "ExcitedStateContractError",
    "ExcitedStateProtocol",
    "ExcitedStateRecord",
    "NTOOrbitalRecord",
    "create_excited_state_artifact",
    "create_nto_artifact",
    "planned_excited_state_artifacts",
    "validate_excited_state_artifact",
    "validate_nto_artifact",
]
