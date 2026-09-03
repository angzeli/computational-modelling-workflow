"""Canonical normalization and deterministic CSV export for excited-state data."""

from __future__ import annotations

from dataclasses import dataclass
import csv
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Mapping, Sequence

from cmw.core.artifacts import (
    Artifact,
    HoleElectronArtifact,
    NTOArtifact,
    ValidationStatus,
    artifact_from_dict,
)
from cmw.core.execution_layout_migration import (
    load_migration_registry,
    resolve_attempt,
)
from cmw.core.provenance import read_json
from cmw.molecular.excited_states import (
    ExcitedStateIdentity,
    ExcitedStateRecord,
    StateSelectionRequest,
    StateSelectionStatus,
    resolve_excited_state,
)
from cmw.molecular.multiwfn.excited_states import (
    MULTIWFN2026_HEA_GRAMMAR,
    MULTIWFN2026_NTO_GRAMMAR,
    MULTIWFN38_HEA_GRAMMAR,
    MULTIWFN38_NTO_GRAMMAR,
    ParsedMultiwfnHoleElectron,
    ParsedMultiwfnNto,
)
from cmw.molecular.orca.excited_states import ParsedOrcaExcitedStates


CanonicalStateKey = tuple[str, str, int]

TDA_MANIFOLD_COLUMNS = (
    "system",
    "spin_manifold",
    "state_index",
    "state_label",
    "orca_global_state_index",
    "excitation_energy_ev",
    "wavelength_nm",
    "oscillator_strength_length",
    "oscillator_strength_velocity",
    "oscillator_strength_gauge_abs_delta",
    "transition_count_parsed",
    "leading_transitions",
    "leading_transition_weight_sum",
    "is_s1",
    "is_lowest_bright",
    "is_selected",
    "selection_roles",
)

TDA_MATRIX_COLUMNS = (
    "system",
    "spin_manifold",
    "state_index",
    "state_label",
    "selection_roles",
    "excitation_energy_ev",
    "wavelength_nm",
    "oscillator_strength_length",
    "oscillator_strength_velocity",
    "bright_threshold_length",
    "transition_count_parsed",
    "leading_transitions",
    "leading_transition_weight_sum",
)

NTO_COLUMNS = (
    "system",
    "spin_manifold",
    "state_index",
    "state_label",
    "selection_roles",
    "nto_pairs_parsed",
    *(f"nto_pair_weight_{index}" for index in range(1, 11)),
    "nto_top2_weight_sum",
    "nto_top5_weight_sum",
    "nto_top10_weight_sum",
    "nto_all_parsed_weight_sum",
)

HEA_COLUMNS = (
    "system",
    "spin_manifold",
    "state_index",
    "state_label",
    "selection_roles",
    "D_angstrom",
    "Sr",
    "t_angstrom",
    "hole_extent_angstrom",
    "electron_extent_angstrom",
    "grid_nx",
    "grid_ny",
    "grid_nz",
    "grid_points",
    "cfgcrossthres",
)

TABLE_NAMES = (
    "tda_state_manifold_qc",
    "tda_matrix",
    "nto_state",
    "hea_state",
)

_SUPPORTED_NTO_GRAMMARS = frozenset(
    {MULTIWFN38_NTO_GRAMMAR, MULTIWFN2026_NTO_GRAMMAR}
)
_SUPPORTED_HEA_GRAMMARS = frozenset(
    {MULTIWFN38_HEA_GRAMMAR, MULTIWFN2026_HEA_GRAMMAR}
)
_ORBITAL_LABEL = re.compile(r"^(\d+)([ab])$", re.I)


class ExcitedStateTableError(ValueError):
    """Raised when tabular excited-state evidence is incomplete or inconsistent."""

    code = "FAILED_EXCITED_STATE_TABLES"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True)
class NormalizedTdaManifold:
    """One system's full manifold plus its deduplicated selected-state rows."""

    system: str
    rows: tuple[Mapping[str, object], ...]
    selected_rows: tuple[Mapping[str, object], ...]
    bright_threshold_length: float

    @property
    def selected_keys(self) -> tuple[CanonicalStateKey, ...]:
        return tuple(_row_key(row) for row in self.selected_rows)


@dataclass(frozen=True)
class ResolvedAnalysisArtifact:
    """A validation-passed analysis artifact tied to its exact execution attempt."""

    artifact: Artifact
    record_path: Path
    target_directory: Path
    attempt_directory: Path
    resolved_files: Mapping[str, Path]
    system_identifier: str | None
    stored_artifact_id: str
    identity_resolution: str


@dataclass(frozen=True)
class ExcitedStateTableBundle:
    """Four cross-validated datasets ready for deterministic CSV serialization."""

    tda_state_manifold_qc: tuple[Mapping[str, object], ...]
    tda_matrix: tuple[Mapping[str, object], ...]
    nto_state: tuple[Mapping[str, object], ...]
    hea_state: tuple[Mapping[str, object], ...]

    def validate(self) -> None:
        _validate_columns(self.tda_state_manifold_qc, TDA_MANIFOLD_COLUMNS)
        _validate_columns(self.tda_matrix, TDA_MATRIX_COLUMNS)
        _validate_columns(self.nto_state, NTO_COLUMNS)
        _validate_columns(self.hea_state, HEA_COLUMNS)
        _require_unique_keys(self.tda_state_manifold_qc, "TDA manifold")
        _require_unique_keys(self.tda_matrix, "TDA matrix")
        _require_unique_keys(self.nto_state, "NTO")
        _require_unique_keys(self.hea_state, "HEA")

        selected = {
            _row_key(row)
            for row in self.tda_state_manifold_qc
            if bool(row["is_selected"])
        }
        matrix = {_row_key(row) for row in self.tda_matrix}
        nto = {_row_key(row) for row in self.nto_state}
        hea = {_row_key(row) for row in self.hea_state}
        if not selected or not (selected == matrix == nto == hea):
            raise ExcitedStateTableError(
                "canonical selected-state key sets differ across output tables"
            )
        roles: dict[CanonicalStateKey, str] = {}
        for table in (self.tda_matrix, self.nto_state, self.hea_state):
            for row in table:
                key = _row_key(row)
                value = str(row["selection_roles"])
                previous = roles.setdefault(key, value)
                if previous != value:
                    raise ExcitedStateTableError(
                        f"selection roles disagree for {key!r}"
                    )

    def tables(self) -> Mapping[str, tuple[Mapping[str, object], ...]]:
        return {
            "tda_state_manifold_qc": self.tda_state_manifold_qc,
            "tda_matrix": self.tda_matrix,
            "nto_state": self.nto_state,
            "hea_state": self.hea_state,
        }


def _row_key(row: Mapping[str, object]) -> CanonicalStateKey:
    return (
        str(row["system"]),
        str(row["spin_manifold"]).casefold(),
        int(row["state_index"]),
    )


def _finite(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ExcitedStateTableError(f"{label} is not numeric") from exc
    if not math.isfinite(result):
        raise ExcitedStateTableError(f"{label} must be finite")
    return result


def _positive(value: object, label: str, *, allow_zero: bool = False) -> float:
    result = _finite(value, label)
    if result < 0.0 or (result == 0.0 and not allow_zero):
        relation = "non-negative" if allow_zero else "positive"
        raise ExcitedStateTableError(f"{label} must be {relation}")
    return result


def _state_identity(value: Mapping[str, object], label: str) -> ExcitedStateIdentity:
    try:
        return ExcitedStateIdentity.from_mapping(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ExcitedStateTableError(f"{label} has invalid canonical identity") from exc


def resolve_selection_roles(
    states: Sequence[ExcitedStateRecord],
    *,
    bright_threshold_length: float,
) -> Mapping[ExcitedStateIdentity, tuple[str, ...]]:
    """Resolve the S1 and lowest-bright roles using existing selection semantics."""

    threshold = _positive(
        bright_threshold_length, "length-gauge bright threshold", allow_zero=True
    )
    if not states:
        raise ExcitedStateTableError("state manifold is empty")
    requests = (
        (
            "S1",
            StateSelectionRequest(
                policy="lowest_singlet",
                spin_manifold="singlet",
                oscillator_strength_gauge="length",
                minimum_oscillator_strength=0.0,
            ),
        ),
        (
            "lowest_bright",
            StateSelectionRequest(
                policy="lowest_bright_singlet",
                spin_manifold="singlet",
                oscillator_strength_gauge="length",
                minimum_oscillator_strength=threshold,
            ),
        ),
    )
    grouped: dict[ExcitedStateIdentity, list[str]] = {}
    for role, request in requests:
        result = resolve_excited_state(states, request)
        if result.status is not StateSelectionStatus.SELECTED or result.selected_identity is None:
            raise ExcitedStateTableError(f"state-selection role {role!r} has no match")
        grouped.setdefault(result.selected_identity, []).append(role)
    return {identity: tuple(roles) for identity, roles in grouped.items()}


def _frontier_label(raw: str, homo_index_one_based: int | None) -> str:
    match = _ORBITAL_LABEL.fullmatch(raw)
    if match is None:
        return f"MO[{raw}]"
    index = int(match.group(1))
    spin = match.group(2).casefold()
    fallback = f"MO{index}{spin}"
    if homo_index_one_based is None or spin != "a":
        return fallback
    homo = int(homo_index_one_based)
    if homo < 1:
        raise ExcitedStateTableError("frontier HOMO index must be one-based and positive")
    if index <= homo:
        delta = homo - index
        return "HOMO" if delta == 0 else f"HOMO-{delta}"
    delta = index - (homo + 1)
    return "LUMO" if delta == 0 else f"LUMO+{delta}"


def _leading_transitions(
    state: ExcitedStateRecord,
    *,
    count: int,
    frontier_homo_index_one_based: int | None,
) -> tuple[str, float]:
    ordered = sorted(
        state.orbital_transitions,
        key=lambda item: (
            -item.contribution_weight,
            item.source_orbital,
            item.target_orbital,
            item.coefficient,
        ),
    )[:count]
    rendered = ";".join(
        f"{_frontier_label(item.source_orbital, frontier_homo_index_one_based)}"
        f"->{_frontier_label(item.target_orbital, frontier_homo_index_one_based)}"
        f":{_format_float(item.contribution_weight)}"
        for item in ordered
    )
    return rendered, sum(item.contribution_weight for item in ordered)


def normalize_tda_manifold(
    system: str,
    parsed: ParsedOrcaExcitedStates,
    *,
    finalized_selected_identities: Sequence[ExcitedStateIdentity | Mapping[str, object]],
    bright_threshold_length: float,
    expected_state_count: int | None = None,
    expected_spin_manifold: str | None = None,
    frontier_homo_index_one_based: int | None = None,
    leading_transition_count: int = 5,
) -> NormalizedTdaManifold:
    """Normalize a complete typed ORCA manifold and cross-check final selections."""

    name = str(system).strip()
    if not name:
        raise ExcitedStateTableError("system identifier is required")
    if not parsed.normal_termination or not parsed.excited_state_completion:
        raise ExcitedStateTableError("ORCA excited-state result is incomplete")
    states = tuple(parsed.states)
    if expected_spin_manifold is not None:
        manifold = str(expected_spin_manifold).casefold()
        states = tuple(item for item in states if item.spin_manifold == manifold)
    if expected_state_count is not None and len(states) != int(expected_state_count):
        raise ExcitedStateTableError(
            f"{name} has {len(states)} states; expected {expected_state_count}"
        )
    if leading_transition_count < 1:
        raise ExcitedStateTableError("leading transition count must be positive")
    identities = [item.identity for item in states]
    if len(identities) != len(set(identities)):
        raise ExcitedStateTableError(f"{name} contains duplicate canonical states")

    roles = resolve_selection_roles(
        states, bright_threshold_length=bright_threshold_length
    )
    finalized = tuple(
        item
        if isinstance(item, ExcitedStateIdentity)
        else _state_identity(item, "finalized selected state")
        for item in finalized_selected_identities
    )
    if len(finalized) != len(set(finalized)):
        raise ExcitedStateTableError("finalized selected identities are duplicated")
    if set(finalized) != set(roles):
        raise ExcitedStateTableError(
            "recomputed state selection disagrees with finalized selected identities"
        )

    rows: list[dict[str, object]] = []
    for state in sorted(
        states,
        key=lambda item: (item.spin_manifold, item.local_state_index),
    ):
        energy = _positive(state.excitation_energy_ev, "excitation energy")
        wavelength = _positive(state.wavelength_nm, "wavelength")
        length = _finite(
            state.length_gauge_oscillator_strength,
            "length-gauge oscillator strength",
        )
        velocity = _finite(
            state.velocity_gauge_oscillator_strength,
            "velocity-gauge oscillator strength",
        )
        if length < 0.0 or velocity < 0.0:
            raise ExcitedStateTableError("oscillator strengths must be non-negative")
        for transition in state.orbital_transitions:
            _positive(
                transition.contribution_weight,
                "transition weight",
                allow_zero=True,
            )
        leading, weight_sum = _leading_transitions(
            state,
            count=leading_transition_count,
            frontier_homo_index_one_based=frontier_homo_index_one_based,
        )
        state_roles = roles.get(state.identity, ())
        rows.append(
            {
                "system": name,
                "spin_manifold": state.spin_manifold,
                "state_index": state.local_state_index,
                "state_label": state.identity.label,
                "orca_global_state_index": state.orca_global_state_index,
                "excitation_energy_ev": energy,
                "wavelength_nm": wavelength,
                "oscillator_strength_length": length,
                "oscillator_strength_velocity": velocity,
                "oscillator_strength_gauge_abs_delta": abs(length - velocity),
                "transition_count_parsed": len(state.orbital_transitions),
                "leading_transitions": leading,
                "leading_transition_weight_sum": weight_sum,
                "is_s1": "S1" in state_roles,
                "is_lowest_bright": "lowest_bright" in state_roles,
                "is_selected": bool(state_roles),
                "selection_roles": ";".join(state_roles),
            }
        )
    selected_rows = tuple(
        {
            "system": row["system"],
            "spin_manifold": row["spin_manifold"],
            "state_index": row["state_index"],
            "state_label": row["state_label"],
            "selection_roles": row["selection_roles"],
            "excitation_energy_ev": row["excitation_energy_ev"],
            "wavelength_nm": row["wavelength_nm"],
            "oscillator_strength_length": row["oscillator_strength_length"],
            "oscillator_strength_velocity": row["oscillator_strength_velocity"],
            "bright_threshold_length": float(bright_threshold_length),
            "transition_count_parsed": row["transition_count_parsed"],
            "leading_transitions": row["leading_transitions"],
            "leading_transition_weight_sum": row["leading_transition_weight_sum"],
        }
        for row in rows
        if row["is_selected"]
    )
    return NormalizedTdaManifold(
        name,
        tuple(rows),
        selected_rows,
        float(bright_threshold_length),
    )


def _analysis_identity(artifact: Artifact) -> ExcitedStateIdentity:
    raw = artifact.metadata.get("selected_state_identity")
    if not isinstance(raw, Mapping):
        raise ExcitedStateTableError("analysis artifact lacks selected-state identity")
    return _state_identity(raw, "analysis artifact")


def _require_finalized_artifact(
    artifact: Artifact,
    expected_type: type[Artifact],
    supported_grammars: frozenset[str],
) -> None:
    if not isinstance(artifact, expected_type):
        raise ExcitedStateTableError(
            f"expected {expected_type.TYPE}, found {artifact.artifact_type}"
        )
    if artifact.validation.status is not ValidationStatus.PASSED:
        raise ExcitedStateTableError("analysis artifact validation did not pass")
    required_checks = {
        "parser_complete",
        "process_exit_code",
        "required_outputs",
        "source_identity",
        "state_identity",
    }
    if expected_type is HoleElectronArtifact:
        required_checks.add("D_rounding_consistency")
    if any(artifact.validation.checks.get(key) is not True for key in required_checks):
        raise ExcitedStateTableError(
            "analysis artifact has an incomplete required validation check"
        )
    execution = artifact.provenance.get("execution_attempt")
    exit_code = execution.get("process_exit_code") if isinstance(execution, Mapping) else None
    if (
        not isinstance(execution, Mapping)
        or isinstance(exit_code, bool)
        or exit_code != 0
    ):
        raise ExcitedStateTableError("analysis artifact lacks successful process evidence")
    grammar = artifact.metadata.get("parser_grammar", artifact.metadata.get("grammar_id"))
    if grammar not in supported_grammars:
        raise ExcitedStateTableError(f"unsupported finalized parser grammar {grammar!r}")
    if artifact.metadata.get("parser_complete") is False:
        raise ExcitedStateTableError("analysis artifact parser result is incomplete")
    if artifact.provenance.get("superseded_by_attempt_id"):
        raise ExcitedStateTableError("analysis artifact attempt is superseded")


def _compare_nto_parse(artifact: NTOArtifact, parsed: ParsedMultiwfnNto) -> None:
    if not parsed.parser_complete:
        raise ExcitedStateTableError("reparsed NTO result is incomplete")
    if parsed.grammar_id != artifact.metadata.get("parser_grammar"):
        raise ExcitedStateTableError("NTO parser grammar disagrees with artifact")
    if parsed.state_evidence.selected_identity != _analysis_identity(artifact):
        raise ExcitedStateTableError("reparsed NTO identity disagrees with artifact")
    _compare_session_identity(artifact, parsed.source_provenance)
    stored = artifact.metadata.get("orbital_pairs")
    if not isinstance(stored, Sequence) or isinstance(stored, (str, bytes)):
        raise ExcitedStateTableError("NTO artifact pair block is malformed")
    stored_weights = tuple(
        float(item["weight"]) for item in stored if isinstance(item, Mapping)
    )
    parsed_weights = tuple(item.weight for item in parsed.pairs)
    if stored_weights != parsed_weights:
        raise ExcitedStateTableError("reparsed NTO weights disagree with artifact")


def normalize_nto_state(
    system: str,
    artifact: NTOArtifact,
    *,
    selection_roles: Sequence[str],
    parsed: ParsedMultiwfnNto | None = None,
) -> Mapping[str, object]:
    """Normalize one finalized NTO artifact without changing pair semantics."""

    _require_finalized_artifact(artifact, NTOArtifact, _SUPPORTED_NTO_GRAMMARS)
    identity = _analysis_identity(artifact)
    if parsed is not None:
        _compare_nto_parse(artifact, parsed)
    raw_pairs = artifact.metadata.get("orbital_pairs")
    if not isinstance(raw_pairs, Sequence) or isinstance(raw_pairs, (str, bytes)):
        raise ExcitedStateTableError("NTO artifact lacks ordered pair records")
    pairs: list[tuple[int, float]] = []
    for raw in raw_pairs:
        if not isinstance(raw, Mapping):
            raise ExcitedStateTableError("NTO pair record is malformed")
        index = int(raw["pair_index"])
        weight = _positive(raw["weight"], "NTO pair weight", allow_zero=True)
        if weight > 1.0:
            raise ExcitedStateTableError("NTO pair weight exceeds one")
        pairs.append((index, weight))
    if [item[0] for item in pairs] != list(range(1, len(pairs) + 1)):
        raise ExcitedStateTableError("NTO pair indices are not contiguous and ordered")
    if int(artifact.metadata.get("printed_pair_count", len(pairs))) != len(pairs):
        raise ExcitedStateTableError("NTO printed pair count disagrees with records")
    weights = tuple(item[1] for item in pairs)
    if sum(weights) > 1.0 + len(weights) * 0.5e-6 + 0.5e-6:
        raise ExcitedStateTableError("NTO parsed weights have an invalid total")
    row: dict[str, object] = {
        "system": str(system),
        "spin_manifold": identity.spin_manifold,
        "state_index": identity.local_state_index,
        "state_label": identity.label,
        "selection_roles": ";".join(selection_roles),
        "nto_pairs_parsed": len(weights),
    }
    for index in range(1, 11):
        row[f"nto_pair_weight_{index}"] = (
            weights[index - 1] if len(weights) >= index else None
        )
    row.update(
        {
            "nto_top2_weight_sum": sum(weights[:2]),
            "nto_top5_weight_sum": sum(weights[:5]),
            "nto_top10_weight_sum": sum(weights[:10]),
            "nto_all_parsed_weight_sum": sum(weights),
        }
    )
    return row


def _compare_hea_parse(
    artifact: HoleElectronArtifact, parsed: ParsedMultiwfnHoleElectron
) -> None:
    if not parsed.parser_complete or not parsed.D_validation.consistent:
        raise ExcitedStateTableError("reparsed HEA result is incomplete or inconsistent")
    if parsed.grammar_id != artifact.metadata.get("parser_grammar"):
        raise ExcitedStateTableError("HEA parser grammar disagrees with artifact")
    if parsed.state_evidence.selected_identity != _analysis_identity(artifact):
        raise ExcitedStateTableError("reparsed HEA identity disagrees with artifact")
    _compare_session_identity(artifact, parsed.source_provenance)
    comparisons = {
        "reported_D_angstrom": parsed.reported_D_angstrom,
        "Sr": parsed.sr,
        "t_angstrom": parsed.t_angstrom,
        "hole_extent_angstrom": parsed.hole_extent_angstrom,
        "electron_extent_angstrom": parsed.electron_extent_angstrom,
        "total_grid_points": parsed.total_grid_points,
        "coefficient_cross_term_threshold": parsed.coefficient_cross_term_threshold,
    }
    for key, value in comparisons.items():
        if artifact.metadata.get(key) != value:
            raise ExcitedStateTableError(f"reparsed HEA {key} disagrees with artifact")


def _compare_session_identity(
    artifact: Artifact, parsed_provenance: Mapping[str, object]
) -> None:
    outputs = artifact.provenance.get("output_identities")
    session = outputs.get("session_log") if isinstance(outputs, Mapping) else None
    expected_hash = session.get("sha256") if isinstance(session, Mapping) else None
    parsed_hash = parsed_provenance.get("session_log_sha256")
    if not isinstance(expected_hash, str) or parsed_hash != expected_hash:
        raise ExcitedStateTableError(
            "reparsed session log identity disagrees with finalized artifact"
        )


def normalize_hea_state(
    system: str,
    artifact: HoleElectronArtifact,
    *,
    selection_roles: Sequence[str],
    parsed: ParsedMultiwfnHoleElectron | None = None,
) -> Mapping[str, object]:
    """Normalize one finalized non-fragment HEA artifact."""

    _require_finalized_artifact(artifact, HoleElectronArtifact, _SUPPORTED_HEA_GRAMMARS)
    if artifact.metadata.get("fragment_resolved") is not False:
        raise ExcitedStateTableError("only non-fragment HEA artifacts are supported")
    identity = _analysis_identity(artifact)
    if parsed is not None:
        _compare_hea_parse(artifact, parsed)
    distance = _positive(
        artifact.metadata.get("reported_D_angstrom"), "HEA D", allow_zero=True
    )
    overlap = _finite(artifact.metadata.get("Sr"), "HEA Sr")
    if not 0.0 <= overlap <= 1.0:
        raise ExcitedStateTableError("HEA Sr must lie within [0, 1]")
    separation = _finite(artifact.metadata.get("t_angstrom"), "HEA t")
    hole = _positive(
        artifact.metadata.get("hole_extent_angstrom"),
        "HEA hole extent",
        allow_zero=True,
    )
    electron = _positive(
        artifact.metadata.get("electron_extent_angstrom"),
        "HEA electron extent",
        allow_zero=True,
    )
    dimensions = artifact.metadata.get("grid_dimensions")
    grid: tuple[int, int, int] | None = None
    if dimensions is not None:
        if (
            not isinstance(dimensions, Sequence)
            or isinstance(dimensions, (str, bytes))
            or len(dimensions) != 3
        ):
            raise ExcitedStateTableError("HEA grid dimensions are malformed")
        grid = tuple(int(value) for value in dimensions)
        if any(value < 1 for value in grid):
            raise ExcitedStateTableError("HEA grid dimensions must be positive")
    total = artifact.metadata.get("total_grid_points")
    total_points = int(total) if total is not None else None
    if total_points is not None and total_points < 1:
        raise ExcitedStateTableError("HEA grid point count must be positive")
    if grid is not None and total_points is not None and math.prod(grid) != total_points:
        raise ExcitedStateTableError("HEA grid dimensions disagree with total points")
    threshold = artifact.metadata.get("coefficient_cross_term_threshold")
    threshold_value = (
        _positive(threshold, "HEA cross-term threshold", allow_zero=True)
        if threshold is not None
        else None
    )
    return {
        "system": str(system),
        "spin_manifold": identity.spin_manifold,
        "state_index": identity.local_state_index,
        "state_label": identity.label,
        "selection_roles": ";".join(selection_roles),
        "D_angstrom": distance,
        "Sr": overlap,
        "t_angstrom": separation,
        "hole_extent_angstrom": hole,
        "electron_extent_angstrom": electron,
        "grid_nx": grid[0] if grid else None,
        "grid_ny": grid[1] if grid else None,
        "grid_nz": grid[2] if grid else None,
        "grid_points": total_points,
        "cfgcrossthres": threshold_value,
    }


def join_excited_state_tables(
    manifolds: Sequence[NormalizedTdaManifold],
    *,
    nto_artifacts: Mapping[CanonicalStateKey, NTOArtifact],
    hea_artifacts: Mapping[CanonicalStateKey, HoleElectronArtifact],
    parsed_nto: Mapping[CanonicalStateKey, ParsedMultiwfnNto] | None = None,
    parsed_hea: Mapping[CanonicalStateKey, ParsedMultiwfnHoleElectron] | None = None,
) -> ExcitedStateTableBundle:
    """Join TDA, NTO, and HEA strictly by system plus canonical state identity."""

    if len({item.system for item in manifolds}) != len(manifolds):
        raise ExcitedStateTableError("normalized systems are duplicated")
    manifold_rows = tuple(row for item in manifolds for row in item.rows)
    matrix_rows = tuple(row for item in manifolds for row in item.selected_rows)
    selected_keys = tuple(_row_key(row) for row in matrix_rows)
    if len(selected_keys) != len(set(selected_keys)):
        raise ExcitedStateTableError("selected canonical state keys are duplicated")
    if set(nto_artifacts) != set(selected_keys):
        raise ExcitedStateTableError("NTO artifact keys do not match selected TDA states")
    if set(hea_artifacts) != set(selected_keys):
        raise ExcitedStateTableError("HEA artifact keys do not match selected TDA states")
    if parsed_nto is not None and set(parsed_nto) != set(selected_keys):
        raise ExcitedStateTableError("reparsed NTO keys do not match selected TDA states")
    if parsed_hea is not None and set(parsed_hea) != set(selected_keys):
        raise ExcitedStateTableError("reparsed HEA keys do not match selected TDA states")

    nto_rows: list[Mapping[str, object]] = []
    hea_rows: list[Mapping[str, object]] = []
    for matrix_row in matrix_rows:
        key = _row_key(matrix_row)
        roles = tuple(str(matrix_row["selection_roles"]).split(";"))
        nto = normalize_nto_state(
            key[0],
            nto_artifacts[key],
            selection_roles=roles,
            parsed=parsed_nto[key] if parsed_nto is not None else None,
        )
        hea = normalize_hea_state(
            key[0],
            hea_artifacts[key],
            selection_roles=roles,
            parsed=parsed_hea[key] if parsed_hea is not None else None,
        )
        if _row_key(nto) != key or _row_key(hea) != key:
            raise ExcitedStateTableError(
                f"analysis artifact canonical identity disagrees with TDA for {key!r}"
            )
        nto_rows.append(nto)
        hea_rows.append(hea)
    bundle = ExcitedStateTableBundle(
        manifold_rows,
        matrix_rows,
        tuple(nto_rows),
        tuple(hea_rows),
    )
    bundle.validate()
    return bundle


def resolve_finalized_analysis_artifact(
    record_path: str | Path,
    *,
    campaign_root: str | Path,
    expected_type: type[Artifact],
    canonical_attempt_id: str | None = None,
) -> ResolvedAnalysisArtifact:
    """Load an authoritative artifact record and resolve its exact v1/v2 attempt."""

    source = Path(record_path).expanduser().resolve(strict=True)
    raw = read_json(source)
    artifact, stored_artifact_id, identity_resolution = _load_migration_aware_artifact(
        raw, campaign_root=campaign_root, record_path=source
    )
    grammars = (
        _SUPPORTED_NTO_GRAMMARS
        if expected_type is NTOArtifact
        else _SUPPORTED_HEA_GRAMMARS
        if expected_type is HoleElectronArtifact
        else frozenset()
    )
    _require_finalized_artifact(artifact, expected_type, grammars)
    execution = artifact.provenance["execution_attempt"]
    assert isinstance(execution, Mapping)
    target_id = str(execution.get("target_id", ""))
    attempt_id = str(execution.get("attempt_id", ""))
    if not target_id or not attempt_id:
        raise ExcitedStateTableError("artifact lacks target/attempt identity")
    if canonical_attempt_id is not None and attempt_id != canonical_attempt_id:
        raise ExcitedStateTableError(
            f"artifact attempt {attempt_id} is not canonical {canonical_attempt_id}"
        )
    renderer = artifact.provenance.get("renderer")
    layout = renderer.get("execution_layout") if isinstance(renderer, Mapping) else None
    recorded_target = (
        Path(str(layout["target_directory"]))
        if isinstance(layout, Mapping) and layout.get("target_directory")
        else None
    )
    try:
        attempt = resolve_attempt(
            campaign_root,
            full_target_id=target_id,
            attempt_id=attempt_id,
            legacy_path=recorded_target,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ExcitedStateTableError(
            f"cannot resolve artifact execution attempt {target_id}/{attempt_id}"
        ) from exc
    target = attempt.parent.parent
    resolved_files: dict[str, Path] = {}
    recorded_attempt = recorded_target / "attempts" / attempt_id if recorded_target else None
    for role, value in artifact.files.items():
        path = Path(value)
        candidate = path
        if not candidate.is_file() and recorded_attempt is not None:
            try:
                relative = path.relative_to(recorded_attempt)
            except ValueError:
                relative = None
            if relative is not None:
                candidate = attempt / relative
        if not candidate.is_file():
            raise ExcitedStateTableError(f"required artifact file is missing: {role}")
        try:
            candidate.resolve().relative_to(attempt.resolve())
        except ValueError as exc:
            raise ExcitedStateTableError(
                f"artifact file {role!r} is outside its resolved attempt"
            ) from exc
        resolved_files[str(role)] = candidate.resolve()
    system_identifier = (
        str(layout["system_identifier"])
        if isinstance(layout, Mapping) and layout.get("system_identifier")
        else None
    )
    return ResolvedAnalysisArtifact(
        artifact,
        source,
        target,
        attempt,
        resolved_files,
        system_identifier,
        stored_artifact_id,
        identity_resolution,
    )


def _replace_registered_paths(
    value: object, replacements: Mapping[str, str]
) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _replace_registered_paths(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_registered_paths(item, replacements) for item in value]
    if isinstance(value, str):
        for source in sorted(replacements, key=len, reverse=True):
            if value == source or value.startswith(source + os.sep):
                return replacements[source] + value[len(source) :]
    return value


def _load_migration_aware_artifact(
    raw: Mapping[str, object],
    *,
    campaign_root: str | Path,
    record_path: Path,
) -> tuple[Artifact, str, str]:
    stored_artifact_id = str(raw.get("artifact_id", ""))
    if not stored_artifact_id:
        raise ExcitedStateTableError(f"artifact record lacks identity: {record_path}")
    try:
        return artifact_from_dict(raw), stored_artifact_id, "exact"
    except (KeyError, TypeError, ValueError) as original_error:
        try:
            registry = load_migration_registry(campaign_root)
            paths = registry.get("paths", {})
            reverse = {
                str(entry["new_path"]): str(old_path)
                for old_path, entry in paths.items()
                if isinstance(entry, Mapping) and entry.get("new_path")
            }
            historical_raw = _replace_registered_paths(raw, reverse)
            if not isinstance(historical_raw, Mapping):
                raise TypeError("artifact record replacement is not a mapping")
            historical_artifact = artifact_from_dict(historical_raw)
            current_raw = dict(raw)
            current_raw.pop("artifact_id", None)
            current_artifact = artifact_from_dict(current_raw)
        except (KeyError, OSError, TypeError, ValueError):
            raise ExcitedStateTableError(
                f"invalid artifact record {record_path}"
            ) from original_error
        if not reverse or historical_artifact.artifact_id != stored_artifact_id:
            raise ExcitedStateTableError(
                "artifact identity drift is not explained by the migration "
                f"registry: {record_path}"
            ) from original_error
        return current_artifact, stored_artifact_id, "registered_path_migration"


def _validate_columns(
    rows: Sequence[Mapping[str, object]], columns: Sequence[str]
) -> None:
    expected = set(columns)
    for row in rows:
        if set(row) != expected:
            raise ExcitedStateTableError("table row does not match its exact CSV schema")


def _require_unique_keys(
    rows: Sequence[Mapping[str, object]], label: str
) -> None:
    keys = [_row_key(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise ExcitedStateTableError(f"{label} contains duplicate canonical keys")


def _format_float(value: float) -> str:
    return format(float(value), ".12g")


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _format_float(value)
    return value


def serialize_csv_rows(
    rows: Sequence[Mapping[str, object]], columns: Sequence[str]
) -> str:
    """Serialize exact-schema rows with deterministic RFC-compatible quoting."""

    _validate_columns(rows, columns)
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=list(columns),
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_value(row[column]) for column in columns})
    return stream.getvalue()


def write_excited_state_csv_bundle(
    bundle: ExcitedStateTableBundle,
    output_paths: Mapping[str, str | Path],
    *,
    temporary_parent: str | Path = "/private/tmp",
) -> None:
    """Validate all datasets, stage all CSVs outside outputs, then replace all four."""

    bundle.validate()
    if set(output_paths) != set(TABLE_NAMES):
        raise ExcitedStateTableError("output mapping must name exactly the four tables")
    schemas = {
        "tda_state_manifold_qc": TDA_MANIFOLD_COLUMNS,
        "tda_matrix": TDA_MATRIX_COLUMNS,
        "nto_state": NTO_COLUMNS,
        "hea_state": HEA_COLUMNS,
    }
    staged: dict[str, Path] = {}
    temp_root = Path(temporary_parent).resolve()
    if str(temp_root) != "/private/tmp" and not temp_root.is_dir():
        raise ExcitedStateTableError("temporary parent does not exist")
    with tempfile.TemporaryDirectory(
        prefix="cmw-excited-state-tables-", dir=temp_root
    ) as temporary:
        temporary_directory = Path(temporary)
        for name, rows in bundle.tables().items():
            path = temporary_directory / f"{name}.csv"
            path.write_text(
                serialize_csv_rows(rows, schemas[name]),
                encoding="utf-8",
                newline="",
            )
            staged[name] = path
        destinations = {name: Path(value) for name, value in output_paths.items()}
        for path in destinations.values():
            if not path.parent.is_dir():
                raise ExcitedStateTableError(f"output directory is missing: {path.parent}")
        backups: dict[str, Path | None] = {}
        for name, destination in destinations.items():
            if destination.exists() and not destination.is_file():
                raise ExcitedStateTableError(
                    f"output destination is not a regular file: {destination}"
                )
            if destination.is_file():
                backup = temporary_directory / f"{name}.previous"
                shutil.copy2(destination, backup)
                backups[name] = backup
            else:
                backups[name] = None
        installed: list[str] = []
        try:
            for name in TABLE_NAMES:
                os.replace(staged[name], destinations[name])
                installed.append(name)
        except OSError as exc:
            rollback_errors: list[str] = []
            for name in reversed(installed):
                destination = destinations[name]
                backup = backups[name]
                try:
                    if backup is None:
                        destination.unlink(missing_ok=True)
                    else:
                        os.replace(backup, destination)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{name}: {rollback_exc}")
            detail = (
                "; rollback errors: " + "; ".join(rollback_errors)
                if rollback_errors
                else ""
            )
            raise ExcitedStateTableError(
                f"four-table installation failed and was rolled back{detail}"
            ) from exc


__all__ = [
    "CanonicalStateKey",
    "ExcitedStateTableBundle",
    "ExcitedStateTableError",
    "HEA_COLUMNS",
    "NTO_COLUMNS",
    "NormalizedTdaManifold",
    "ResolvedAnalysisArtifact",
    "TABLE_NAMES",
    "TDA_MANIFOLD_COLUMNS",
    "TDA_MATRIX_COLUMNS",
    "join_excited_state_tables",
    "normalize_hea_state",
    "normalize_nto_state",
    "normalize_tda_manifold",
    "resolve_finalized_analysis_artifact",
    "resolve_selection_roles",
    "serialize_csv_rows",
    "write_excited_state_csv_bundle",
]
