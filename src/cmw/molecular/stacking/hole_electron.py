"""Multiwfn-style hole/electron planning and typed result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from pathlib import Path
from typing import Mapping, Sequence

from cmw.core.artifacts import (
    Artifact,
    ArtifactValidation,
    ExcitedStateArtifact,
    HoleElectronArtifact,
    NTOArtifact,
    StructureArtifact,
    ValidationStatus,
    validate_artifact_compatibility,
)
from cmw.core.provenance import stable_hash
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


class DeferredStateSelectionError(HoleElectronContractError):
    """Raised when state-resolved analysis is requested before state selection."""

    code = "DEFERRED_STATE_SELECTION"


@dataclass(frozen=True)
class FragmentDefinition:
    """One generic zero-based atom partition used by an analysis."""

    fragment_id: str
    atom_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        fragment_id = str(self.fragment_id).strip()
        indices = tuple(int(item) for item in self.atom_indices)
        if not fragment_id:
            raise HoleElectronContractError("fragment identifier is required")
        if not indices or min(indices) < 0 or len(set(indices)) != len(indices):
            raise HoleElectronContractError(
                "fragment definitions require unique zero-based atom indices"
            )
        object.__setattr__(self, "fragment_id", fragment_id)
        object.__setattr__(self, "atom_indices", indices)

    def to_dict(self) -> dict[str, object]:
        return {
            "fragment_id": self.fragment_id,
            "atom_indices": list(self.atom_indices),
        }


def _population(values: Mapping[str, float], *, name: str) -> dict[str, float]:
    selected = {str(key).strip(): float(value) for key, value in values.items()}
    if not selected or any(not key for key in selected):
        raise HoleElectronContractError(f"{name} requires named fragments")
    if any(
        not math.isfinite(value) or value < 0.0 or value > 1.0
        for value in selected.values()
    ):
        raise HoleElectronContractError(f"{name} values must be within [0, 1]")
    if not math.isclose(sum(selected.values()), 1.0, abs_tol=1.0e-6):
        raise HoleElectronContractError(f"{name} values must sum to 1")
    return selected


@dataclass(frozen=True)
class HoleElectronMetrics:
    hole_centroid_angstrom: Vector3
    electron_centroid_angstrom: Vector3
    separation_distance_angstrom: float
    overlap: float
    separation_index: float
    fragment_contributions: Mapping[str, Mapping[str, float]] = field(
        default_factory=dict
    )
    hole_extent_angstrom: float | None = None
    electron_extent_angstrom: float | None = None
    hole_population: Mapping[str, float] = field(default_factory=dict)
    electron_population: Mapping[str, float] = field(default_factory=dict)
    reported_D_angstrom: float | None = None
    derived_D_from_reported_centroids_angstrom: float | None = None
    centroid_decimal_places: int | None = None
    reported_D_decimal_places: int | None = None
    D_consistency: Mapping[str, object] = field(default_factory=dict)

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
        if (
            self.derived_D_from_reported_centroids_angstrom is not None
            and not math.isclose(
                float(self.derived_D_from_reported_centroids_angstrom),
                observed_separation,
                abs_tol=1.0e-12,
            )
        ):
            raise HoleElectronContractError(
                "derived D does not match the declared reported centroids"
            )
        precision_fields = (
            self.reported_D_angstrom,
            self.centroid_decimal_places,
            self.reported_D_decimal_places,
        )
        D_consistency: dict[str, object]
        if any(value is not None for value in precision_fields):
            if not all(value is not None for value in precision_fields):
                raise HoleElectronContractError(
                    "precision-aware D validation requires reported D and both precisions"
                )
            reported_D = float(self.reported_D_angstrom)
            if not math.isclose(separation, reported_D, abs_tol=1.0e-12):
                raise HoleElectronContractError(
                    "separation distance must preserve the printed Multiwfn D value"
                )
            from cmw.molecular.multiwfn.excited_states import (
                validate_reported_D_precision,
            )

            validation = validate_reported_D_precision(
                hole,
                electron,
                reported_D_angstrom=reported_D,
                centroid_decimal_places=int(self.centroid_decimal_places),
                reported_D_decimal_places=int(self.reported_D_decimal_places),
            )
            if not validation.consistent:
                raise HoleElectronContractError(
                    "printed D is inconsistent with centroid rounding intervals"
                )
            D_consistency = validation.to_dict()
        else:
            if not math.isclose(
                separation,
                observed_separation,
                abs_tol=1.0e-8,
            ):
                raise HoleElectronContractError(
                    "hole/electron separation does not match the declared centroids"
                )
            reported_D = None
            D_consistency = dict(self.D_consistency)
        if not 0.0 <= overlap <= 1.0:
            raise HoleElectronContractError("hole/electron overlap must be in [0, 1]")
        hole_extent = (
            float(self.hole_extent_angstrom)
            if self.hole_extent_angstrom is not None
            else None
        )
        electron_extent = (
            float(self.electron_extent_angstrom)
            if self.electron_extent_angstrom is not None
            else None
        )
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in (hole_extent, electron_extent)
        ):
            raise HoleElectronContractError(
                "hole and electron extents must be non-negative finite values"
            )
        contributions: dict[str, dict[str, float]] = {}
        for fragment, values in self.fragment_contributions.items():
            if not fragment or not isinstance(values, Mapping):
                raise HoleElectronContractError(
                    "fragment contributions require named mappings"
                )
            selected = {str(key): float(value) for key, value in values.items()}
            if not selected or not all(
                math.isfinite(value) for value in selected.values()
            ):
                raise HoleElectronContractError(
                    "fragment contributions must be finite"
                )
            contributions[str(fragment)] = selected
        if contributions:
            derived_hole = {
                fragment: values["hole"]
                for fragment, values in contributions.items()
                if "hole" in values
            }
            derived_electron = {
                fragment: values["electron"]
                for fragment, values in contributions.items()
                if "electron" in values
            }
            hole_population = _population(
                self.hole_population or derived_hole,
                name="hole population",
            )
            electron_population = _population(
                self.electron_population or derived_electron,
                name="electron population",
            )
            if set(hole_population) != set(electron_population):
                raise HoleElectronContractError(
                    "hole and electron populations require the same fragment identifiers"
                )
            if set(contributions) != set(hole_population):
                raise HoleElectronContractError(
                    "fragment contributions and populations use different fragments"
                )
            for fragment, values in contributions.items():
                if (
                    not math.isclose(
                        values.get("hole", float("nan")),
                        hole_population[fragment],
                    )
                    or not math.isclose(
                        values.get("electron", float("nan")),
                        electron_population[fragment],
                    )
                ):
                    raise HoleElectronContractError(
                        "fragment contributions conflict with canonical populations"
                    )
        else:
            if self.hole_population or self.electron_population:
                raise HoleElectronContractError(
                    "fragment populations require fragment contributions"
                )
            hole_population = {}
            electron_population = {}
        object.__setattr__(self, "hole_centroid_angstrom", hole)
        object.__setattr__(self, "electron_centroid_angstrom", electron)
        object.__setattr__(self, "separation_distance_angstrom", separation)
        object.__setattr__(self, "overlap", overlap)
        object.__setattr__(self, "separation_index", separation_index)
        object.__setattr__(self, "hole_extent_angstrom", hole_extent)
        object.__setattr__(self, "electron_extent_angstrom", electron_extent)
        object.__setattr__(self, "fragment_contributions", contributions)
        object.__setattr__(self, "hole_population", hole_population)
        object.__setattr__(self, "electron_population", electron_population)
        object.__setattr__(self, "reported_D_angstrom", reported_D)
        object.__setattr__(
            self,
            "derived_D_from_reported_centroids_angstrom",
            observed_separation,
        )
        object.__setattr__(self, "D_consistency", D_consistency)

    def to_dict(self) -> dict[str, object]:
        return {
            "hole_centroid_angstrom": list(self.hole_centroid_angstrom),
            "electron_centroid_angstrom": list(self.electron_centroid_angstrom),
            "separation_distance_angstrom": self.separation_distance_angstrom,
            "overlap": self.overlap,
            "separation_index": self.separation_index,
            "reported_D_angstrom": self.reported_D_angstrom,
            "derived_D_from_reported_centroids_angstrom": (
                self.derived_D_from_reported_centroids_angstrom
            ),
            "centroid_decimal_places": self.centroid_decimal_places,
            "reported_D_decimal_places": self.reported_D_decimal_places,
            "D_consistency": dict(self.D_consistency),
            "hole_extent_angstrom": self.hole_extent_angstrom,
            "electron_extent_angstrom": self.electron_extent_angstrom,
            "hole_population": dict(self.hole_population),
            "electron_population": dict(self.electron_population),
            "fragment_contributions": {
                key: dict(value) for key, value in self.fragment_contributions.items()
            },
        }


@dataclass(frozen=True)
class HoleElectronProtocol:
    state_index: int | None
    menu_contract: str
    menu_sequence: tuple[str, ...]
    outputs: tuple[MultiwfnOutputSpec, ...]
    visualization: Mapping[str, object] = field(default_factory=dict)
    fragments: tuple[FragmentDefinition, ...] = ()
    execution_ready: bool = True
    state_selection: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state_index is not None and (
            isinstance(self.state_index, bool)
            or not isinstance(self.state_index, int)
            or self.state_index < 1
        ):
            raise HoleElectronContractError(
                "hole/electron state_index must be a positive integer"
            )
        execution_ready = bool(self.execution_ready)
        if execution_ready and self.state_index is None:
            raise HoleElectronContractError(
                "execution-ready hole/electron analysis requires state_index"
            )
        if execution_ready and not self.menu_contract.strip():
            raise HoleElectronContractError("Multiwfn menu contract is required")
        menu = tuple(str(item) for item in self.menu_sequence)
        if execution_ready and not menu:
            raise HoleElectronContractError(
                "execution-ready Multiwfn analysis requires a menu sequence"
            )
        if any("\n" in item or "\r" in item for item in menu):
            raise HoleElectronContractError(
                "Multiwfn menu sequence requires explicit single-line entries"
            )
        object.__setattr__(self, "menu_sequence", menu)
        object.__setattr__(self, "outputs", validate_output_specs(self.outputs))
        object.__setattr__(self, "visualization", dict(self.visualization))
        object.__setattr__(self, "execution_ready", execution_ready)
        object.__setattr__(self, "state_selection", dict(self.state_selection))
        fragments = tuple(self.fragments)
        if fragments:
            if len(fragments) < 2:
                raise HoleElectronContractError(
                    "fragment-resolved analysis requires at least two fragments"
                )
            identifiers = [item.fragment_id for item in fragments]
            indices = [index for item in fragments for index in item.atom_indices]
            if len(set(identifiers)) != len(identifiers):
                raise HoleElectronContractError("fragment identifiers must be unique")
            if len(set(indices)) != len(indices):
                raise HoleElectronContractError("fragment atom mappings overlap")
        object.__setattr__(self, "fragments", fragments)

    def validate_fragment_partition(self, atom_count: int) -> None:
        if atom_count < 1:
            raise HoleElectronContractError("source atom count must be positive")
        if not self.fragments:
            raise HoleElectronContractError(
                "fragment-resolved analysis requires explicit fragment definitions"
            )
        supplied = {
            index for fragment in self.fragments for index in fragment.atom_indices
        }
        expected = set(range(atom_count))
        if supplied != expected:
            missing = sorted(expected - supplied)
            invalid = sorted(supplied - expected)
            detail = f"missing={missing}" if missing else f"invalid={invalid}"
            raise HoleElectronContractError(
                "fragment definitions must cover the source structure exactly: "
                + detail
            )

    @property
    def stdin_text(self) -> str:
        return "\n".join(self.menu_sequence) + "\n"

    def require_execution_ready(self) -> None:
        if not self.execution_ready:
            criteria = self.state_selection.get("criteria", ())
            detail = (
                ", ".join(str(item) for item in criteria)
                or "configured criteria"
            )
            raise DeferredStateSelectionError(
                "hole/electron execution is deferred until quantitative excited-state "
                f"selection resolves: {detail}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": "hole_electron_analysis",
            "state_index": self.state_index,
            "execution_ready": self.execution_ready,
            "state_selection": dict(self.state_selection),
            "menu_contract": self.menu_contract,
            "menu_sequence": list(self.menu_sequence),
            "outputs": [item.to_dict() for item in self.outputs],
            "visualization": dict(self.visualization),
            "fragment_definitions": {
                "indexing": "zero_based",
                "fragments": [item.to_dict() for item in self.fragments],
            },
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

    plan.protocol.require_execution_ready()

    if runtime.get("menu_contract") != plan.protocol.menu_contract:
        raise HoleElectronContractError(
            "Multiwfn runtime menu contract does not match the analysis protocol"
        )
    try:
        stored_stdin = stdin_path.expanduser().resolve(strict=True).read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        raise HoleElectronContractError(
            "Multiwfn stdin is missing or unreadable"
        ) from exc
    if stored_stdin != plan.protocol.stdin_text:
        raise HoleElectronContractError(
            "Multiwfn stdin does not match the planned menu sequence"
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
                "hole_extent_angstrom",
                "electron_extent_angstrom",
                "hole_population",
                "electron_population",
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
    source_structure: StructureArtifact | None = None,
) -> HoleElectronArtifact:
    """Create a validated analysis artifact without interpreting its chemistry."""

    protocol.require_execution_ready()

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
    if protocol.fragments:
        if source_structure is None:
            raise HoleElectronContractError(
                "fragment-resolved analysis requires a source StructureArtifact"
            )
        if source_structure.atom_count is None:
            raise HoleElectronContractError(
                "source StructureArtifact requires an atom count"
            )
        if not source_structure.validation.passed:
            raise HoleElectronContractError(
                "source StructureArtifact is not validated"
            )
        protocol.validate_fragment_partition(source_structure.atom_count)
        fragment_ids = {item.fragment_id for item in protocol.fragments}
        if (
            set(metrics.hole_population) != fragment_ids
            or set(metrics.electron_population) != fragment_ids
        ):
            raise HoleElectronContractError(
                "fragment populations do not match the declared fragment partition"
            )
        if (
            metrics.hole_extent_angstrom is None
            or metrics.electron_extent_angstrom is None
        ):
            raise HoleElectronContractError(
                "fragment-resolved analysis requires hole and electron extents"
            )
        if excited_state.metadata.get("geometry_source_artifact") != (
            source_structure.artifact_id
        ):
            raise HoleElectronContractError(
                "excited-state geometry source does not match the analysis structure"
            )
        if excited_state.metadata.get("source_geometry_hash") != (
            source_structure.geometry_hash
        ):
            raise HoleElectronContractError(
                "excited-state geometry hash does not match the analysis structure"
            )
        states = excited_state.metadata.get("excited_states")
        if not isinstance(states, Sequence) or isinstance(states, (str, bytes)):
            raise HoleElectronContractError(
                "fragment-resolved analysis requires quantitative excited states"
            )
        state_indices = {
            int(item["state_index"])
            for item in states
            if isinstance(item, Mapping) and "state_index" in item
        }
        if protocol.state_index not in state_indices:
            raise HoleElectronContractError(
                "requested hole/electron state is absent from the excited-state artifact"
            )
        if nto is not None:
            pairs = nto.metadata.get("orbital_pairs")
            if not isinstance(pairs, Sequence) or isinstance(pairs, (str, bytes)):
                raise HoleElectronContractError(
                    "fragment-resolved analysis requires state-resolved NTO metadata"
                )
            nto_states = {
                int(item["state_index"])
                for item in pairs
                if isinstance(item, Mapping) and "state_index" in item
            }
            if protocol.state_index not in nto_states:
                raise HoleElectronContractError(
                    "requested hole/electron state is absent from the NTO artifact"
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
    partition = (
        {
            "indexing": "zero_based",
            "fragments": [item.to_dict() for item in protocol.fragments],
        }
        if protocol.fragments
        else None
    )
    validation = ArtifactValidation(
        ValidationStatus.PASSED,
        {
            "parent_excited_state": excited_state.validation.passed,
            "metrics_complete": True,
            "runtime_provenance": True,
            "required_outputs": True,
            "fragment_partition": True if protocol.fragments else None,
            "state_identity": True if protocol.fragments else None,
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
            **(
                {"fragment_partition_hash": stable_hash(partition)}
                if partition is not None
                else {}
            ),
        },
        metadata={
            **metrics.to_dict(),
            "multiwfn_protocol": protocol.to_dict(),
            "visualization": dict(protocol.visualization),
            "analyzed_state_index": protocol.state_index,
            **(
                {
                    "hole_electron_contract": "fragment_resolved_v1",
                    "source_structure_artifact": source_structure.artifact_id,
                    "source_geometry_hash": source_structure.geometry_hash,
                    "fragment_definitions": partition,
                    "fragment_partition_hash": stable_hash(partition),
                }
                if source_structure is not None and partition is not None
                else {}
            ),
        },
    )
    parent_objects: list[Artifact] = [excited_state]
    if nto is not None:
        parent_objects.append(nto)
    validate_artifact_compatibility(artifact, parent_objects)
    if protocol.fragments:
        result = validate_hole_electron_artifact(
            artifact,
            excited_state=excited_state,
            protocol=protocol,
            source_structure=source_structure,
            nto=nto,
        )
        if not result.passed:
            raise HoleElectronContractError(result.reason)
    return replace(artifact, validation=result) if protocol.fragments else artifact


def validate_hole_electron_artifact(
    artifact: HoleElectronArtifact,
    *,
    excited_state: ExcitedStateArtifact,
    protocol: HoleElectronProtocol,
    source_structure: StructureArtifact,
    nto: NTOArtifact | None = None,
) -> ArtifactValidation:
    """Validate quantitative fragment populations against one structure and state."""

    checks: dict[str, bool | None] = {
        "parent_identity": set(artifact.parent_artifacts)
        == {
            excited_state.artifact_id,
            *({nto.artifact_id} if nto is not None else set()),
        },
        "source_structure": artifact.metadata.get("source_structure_artifact")
        == source_structure.artifact_id,
        "geometry_hash": artifact.metadata.get("source_geometry_hash")
        == source_structure.geometry_hash,
        "excited_state_geometry": (
            excited_state.metadata.get("geometry_source_artifact")
            == source_structure.artifact_id
            and excited_state.metadata.get("source_geometry_hash")
            == source_structure.geometry_hash
        ),
        "state_identity": False,
        "fragment_partition": False,
        "fragment_populations": False,
        "quantitative_metrics": False,
        "runtime_provenance": False,
        "required_outputs": False,
        "artifact_compatibility": False,
    }
    try:
        if source_structure.atom_count is None:
            raise HoleElectronContractError(
                "source StructureArtifact requires an atom count"
            )
        protocol.validate_fragment_partition(source_structure.atom_count)
        expected_partition = {
            "indexing": "zero_based",
            "fragments": [item.to_dict() for item in protocol.fragments],
        }
        checks["fragment_partition"] = (
            artifact.metadata.get("fragment_definitions") == expected_partition
            and artifact.metadata.get("fragment_partition_hash")
            == stable_hash(expected_partition)
        )
        fragment_ids = {item.fragment_id for item in protocol.fragments}
        hole = artifact.metadata.get("hole_population")
        electron = artifact.metadata.get("electron_population")
        if not isinstance(hole, Mapping) or not isinstance(electron, Mapping):
            raise HoleElectronContractError(
                "fragment-resolved artifact requires population mappings"
            )
        normalized_hole = _population(hole, name="hole population")
        normalized_electron = _population(
            electron, name="electron population"
        )
        checks["fragment_populations"] = (
            set(normalized_hole) == fragment_ids
            and set(normalized_electron) == fragment_ids
        )
        states = excited_state.metadata.get("excited_states")
        if not isinstance(states, Sequence) or isinstance(states, (str, bytes)):
            raise HoleElectronContractError(
                "excited-state artifact lacks quantitative state evidence"
            )
        excited_states = {
            int(item["state_index"])
            for item in states
            if isinstance(item, Mapping) and "state_index" in item
        }
        nto_state_available = True
        if nto is not None:
            pairs = nto.metadata.get("orbital_pairs")
            if not isinstance(pairs, Sequence) or isinstance(pairs, (str, bytes)):
                raise HoleElectronContractError(
                    "NTO artifact lacks state-resolved orbital evidence"
                )
            nto_states = {
                int(item["state_index"])
                for item in pairs
                if isinstance(item, Mapping) and "state_index" in item
            }
            nto_state_available = protocol.state_index in nto_states
        checks["state_identity"] = (
            artifact.metadata.get("analyzed_state_index") == protocol.state_index
            and protocol.state_index in excited_states
            and nto_state_available
        )
        checks["quantitative_metrics"] = all(
            isinstance(artifact.metadata.get(key), (int, float))
            and math.isfinite(float(artifact.metadata[key]))
            for key in (
                "separation_distance_angstrom",
                "overlap",
                "separation_index",
                "hole_extent_angstrom",
                "electron_extent_angstrom",
            )
        )
        runtime = artifact.provenance.get("runtime")
        checks["runtime_provenance"] = (
            isinstance(runtime, Mapping)
            and bool(runtime.get("version"))
            and bool(runtime.get("executable"))
            and runtime.get("menu_contract") == protocol.menu_contract
        )
        required_roles = {item.role for item in protocol.outputs if item.required}
        checks["required_outputs"] = all(
            role in artifact.files
            and Path(artifact.files[role]).is_file()
            and Path(artifact.files[role]).stat().st_size > 0
            for role in required_roles
        )
        parent_objects: list[Artifact] = [excited_state]
        if nto is not None:
            parent_objects.append(nto)
        validate_artifact_compatibility(artifact, parent_objects)
        checks["artifact_compatibility"] = True
    except (TypeError, ValueError) as exc:
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            HoleElectronContractError.code,
            str(exc),
        )
    if not all(value is True for value in checks.values()):
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            HoleElectronContractError.code,
            "hole/electron fragment, state, geometry, or metric evidence is incomplete",
        )
    return ArtifactValidation(
        ValidationStatus.PASSED,
        checks,
        "VALID_HOLE_ELECTRON_ARTIFACT",
        "hole/electron fragment populations, state, geometry, and lineage are complete",
    )


__all__ = [
    "HoleElectronAnalysisPlan",
    "HoleElectronContractError",
    "FragmentDefinition",
    "HoleElectronMetrics",
    "HoleElectronProtocol",
    "build_hole_electron_command_spec",
    "create_hole_electron_artifact",
    "plan_hole_electron_analysis",
    "validate_hole_electron_artifact",
]
