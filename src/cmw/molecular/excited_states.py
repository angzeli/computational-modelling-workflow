"""Generic quantitative excited-state records and deterministic selection policies."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Mapping, Sequence


SPIN_MANIFOLDS = frozenset({"singlet", "triplet"})
OSCILLATOR_STRENGTH_GAUGES = frozenset({"length", "velocity"})


class ExcitedStateDataError(ValueError):
    """Raised when quantitative excited-state evidence is internally inconsistent."""

    code = "FAILED_EXCITED_STATE_DATA"


@dataclass(frozen=True, order=True)
class ExcitedStateIdentity:
    """Stable cross-tool identity within one spin manifold."""

    spin_manifold: str
    local_state_index: int

    def __post_init__(self) -> None:
        manifold = str(self.spin_manifold).strip().casefold()
        if manifold not in SPIN_MANIFOLDS:
            raise ExcitedStateDataError("spin manifold must be singlet or triplet")
        if (
            isinstance(self.local_state_index, bool)
            or not isinstance(self.local_state_index, int)
            or self.local_state_index < 1
        ):
            raise ExcitedStateDataError("local state index must be a positive integer")
        object.__setattr__(self, "spin_manifold", manifold)

    @property
    def label(self) -> str:
        prefix = "S" if self.spin_manifold == "singlet" else "T"
        return f"{prefix}{self.local_state_index}"

    def to_dict(self) -> dict[str, object]:
        return {
            "spin_manifold": self.spin_manifold,
            "local_state_index": self.local_state_index,
            "label": self.label,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ExcitedStateIdentity":
        return cls(
            str(value["spin_manifold"]),
            int(value["local_state_index"]),
        )


@dataclass(frozen=True)
class OrbitalTransitionRecord:
    """One printed occupied-to-virtual contribution in an excited state."""

    source_orbital: str
    target_orbital: str
    contribution_weight: float
    coefficient: float

    def __post_init__(self) -> None:
        source = str(self.source_orbital).strip()
        target = str(self.target_orbital).strip()
        weight = float(self.contribution_weight)
        coefficient = float(self.coefficient)
        if not source or not target:
            raise ExcitedStateDataError("orbital transition labels are required")
        if not math.isfinite(weight) or weight < 0.0:
            raise ExcitedStateDataError(
                "orbital transition weight must be finite and non-negative"
            )
        if not math.isfinite(coefficient):
            raise ExcitedStateDataError("orbital transition coefficient must be finite")
        object.__setattr__(self, "source_orbital", source)
        object.__setattr__(self, "target_orbital", target)
        object.__setattr__(self, "contribution_weight", weight)
        object.__setattr__(self, "coefficient", coefficient)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_orbital": self.source_orbital,
            "target_orbital": self.target_orbital,
            "contribution_weight": self.contribution_weight,
            "coefficient": self.coefficient,
            "coefficient_sign_is_phase_dependent": True,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "OrbitalTransitionRecord":
        return cls(
            str(value["source_orbital"]),
            str(value["target_orbital"]),
            float(value["contribution_weight"]),
            float(value["coefficient"]),
        )


@dataclass(frozen=True)
class TransitionFilterSummary:
    """Recorded, non-destructive transition-summary policy."""

    minimum_weight: float
    total_parsed_count: int
    retained_count: int
    omitted_count: int

    def __post_init__(self) -> None:
        cutoff = float(self.minimum_weight)
        counts = (self.total_parsed_count, self.retained_count, self.omitted_count)
        if not math.isfinite(cutoff) or cutoff < 0.0:
            raise ExcitedStateDataError("transition cutoff must be non-negative")
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in counts):
            raise ExcitedStateDataError("transition summary counts must be non-negative integers")
        if self.retained_count + self.omitted_count != self.total_parsed_count:
            raise ExcitedStateDataError("transition summary counts are inconsistent")
        object.__setattr__(self, "minimum_weight", cutoff)

    def to_dict(self) -> dict[str, object]:
        return {
            "minimum_weight": self.minimum_weight,
            "total_parsed_count": self.total_parsed_count,
            "retained_count": self.retained_count,
            "omitted_count": self.omitted_count,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TransitionFilterSummary":
        return cls(
            float(value["minimum_weight"]),
            int(value["total_parsed_count"]),
            int(value["retained_count"]),
            int(value["omitted_count"]),
        )


@dataclass(frozen=True)
class ExcitedStateRecord:
    """One quantitative state with backward-compatible legacy fields first."""

    state_index: int
    excitation_energy_ev: float
    oscillator_strength: float
    spin_manifold: str
    selection_labels: tuple[str, ...] = ()
    selection_rationale: str | None = None
    local_state_index: int | None = None
    orca_global_state_index: int | None = None
    multiplicity: int | None = None
    s_squared: float | None = None
    detailed_energy_hartree: float | None = None
    detailed_energy_ev: float | None = None
    wavenumber_cm1: float | None = None
    spectrum_energy_ev: float | None = None
    wavelength_nm: float | None = None
    length_gauge_oscillator_strength: float | None = None
    velocity_gauge_oscillator_strength: float | None = None
    primary_oscillator_strength_gauge: str | None = None
    orbital_transitions: tuple[OrbitalTransitionRecord, ...] = ()
    transition_filter: TransitionFilterSummary | None = None
    source_provenance: Mapping[str, object] = field(default_factory=dict)
    orca_version: str | None = None
    protocol_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        local_index = (
            self.state_index
            if self.local_state_index is None
            else self.local_state_index
        )
        for value, label in (
            (self.state_index, "state index"),
            (local_index, "local state index"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ExcitedStateDataError(f"{label} must be a positive integer")
        if local_index != self.state_index:
            raise ExcitedStateDataError(
                "legacy state_index must equal canonical local_state_index"
            )
        global_index = self.orca_global_state_index
        if global_index is not None and (
            isinstance(global_index, bool)
            or not isinstance(global_index, int)
            or global_index < 1
        ):
            raise ExcitedStateDataError("ORCA global state index must be positive")
        manifold = str(self.spin_manifold).strip().casefold()
        if manifold not in SPIN_MANIFOLDS:
            raise ExcitedStateDataError("spin manifold must be singlet or triplet")
        energy = _positive_number(self.excitation_energy_ev, "excitation energy")
        oscillator = _non_negative_number(self.oscillator_strength, "oscillator strength")
        expected_multiplicity = 1 if manifold == "singlet" else 3
        if self.multiplicity is not None and self.multiplicity != expected_multiplicity:
            raise ExcitedStateDataError("multiplicity and spin manifold are inconsistent")
        labels = tuple(str(item).strip() for item in self.selection_labels)
        if any(not item for item in labels) or len(set(labels)) != len(labels):
            raise ExcitedStateDataError(
                "state-selection labels must be unique non-empty strings"
            )
        rationale = (
            str(self.selection_rationale).strip()
            if self.selection_rationale is not None
            else None
        )
        if labels and not rationale:
            raise ExcitedStateDataError("selected states require a rationale")
        for value, label in (
            (self.s_squared, "S-squared"),
            (self.detailed_energy_hartree, "detailed Hartree energy"),
            (self.detailed_energy_ev, "detailed eV energy"),
            (self.wavenumber_cm1, "wavenumber"),
            (self.spectrum_energy_ev, "spectrum energy"),
            (self.wavelength_nm, "wavelength"),
        ):
            if value is not None:
                _positive_number(value, label, allow_zero=label == "S-squared")
        length = _optional_non_negative(
            self.length_gauge_oscillator_strength, "length-gauge oscillator strength"
        )
        velocity = _optional_non_negative(
            self.velocity_gauge_oscillator_strength, "velocity-gauge oscillator strength"
        )
        gauge = (
            str(self.primary_oscillator_strength_gauge).strip().casefold()
            if self.primary_oscillator_strength_gauge is not None
            else None
        )
        if gauge is not None and gauge not in OSCILLATOR_STRENGTH_GAUGES:
            raise ExcitedStateDataError("primary oscillator-strength gauge is unsupported")
        authoritative = length if gauge == "length" else velocity if gauge == "velocity" else None
        if authoritative is not None and not math.isclose(
            oscillator, authoritative, rel_tol=0.0, abs_tol=1.0e-12
        ):
            raise ExcitedStateDataError(
                "primary oscillator strength does not match its declared gauge"
            )
        transitions = tuple(
            item
            if isinstance(item, OrbitalTransitionRecord)
            else OrbitalTransitionRecord.from_mapping(item)
            for item in self.orbital_transitions
        )
        transition_filter = self.transition_filter
        if transition_filter is not None:
            if transition_filter.total_parsed_count != len(transitions):
                raise ExcitedStateDataError(
                    "transition filter must describe every parsed transition"
                )
            observed_retained = sum(
                item.contribution_weight >= transition_filter.minimum_weight
                for item in transitions
            )
            if observed_retained != transition_filter.retained_count:
                raise ExcitedStateDataError(
                    "transition filter counts do not match parsed transition weights"
                )
        object.__setattr__(self, "local_state_index", local_index)
        object.__setattr__(self, "spin_manifold", manifold)
        object.__setattr__(self, "excitation_energy_ev", energy)
        object.__setattr__(self, "oscillator_strength", oscillator)
        object.__setattr__(self, "selection_labels", labels)
        object.__setattr__(self, "selection_rationale", rationale)
        object.__setattr__(self, "length_gauge_oscillator_strength", length)
        object.__setattr__(self, "velocity_gauge_oscillator_strength", velocity)
        object.__setattr__(self, "primary_oscillator_strength_gauge", gauge)
        object.__setattr__(self, "orbital_transitions", transitions)
        object.__setattr__(self, "source_provenance", dict(self.source_provenance))
        object.__setattr__(self, "protocol_metadata", dict(self.protocol_metadata))

    @property
    def identity(self) -> ExcitedStateIdentity:
        return ExcitedStateIdentity(self.spin_manifold, int(self.local_state_index))

    @property
    def canonical_key(self) -> tuple[str, int]:
        return self.identity.spin_manifold, self.identity.local_state_index

    def oscillator_strength_for(self, gauge: str) -> float | None:
        normalized = str(gauge).strip().casefold()
        if normalized == "length":
            return self.length_gauge_oscillator_strength
        if normalized == "velocity":
            return self.velocity_gauge_oscillator_strength
        raise ExcitedStateDataError("oscillator-strength gauge must be length or velocity")

    def retained_transitions(self) -> tuple[OrbitalTransitionRecord, ...]:
        if self.transition_filter is None:
            return self.orbital_transitions
        return tuple(
            item
            for item in self.orbital_transitions
            if item.contribution_weight >= self.transition_filter.minimum_weight
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "state_index": self.state_index,
            "local_state_index": self.local_state_index,
            "orca_global_state_index": self.orca_global_state_index,
            "canonical_identity": self.identity.to_dict(),
            "excitation_energy_ev": self.excitation_energy_ev,
            "oscillator_strength": self.oscillator_strength,
            "spin_manifold": self.spin_manifold,
            "multiplicity": self.multiplicity,
            "s_squared": self.s_squared,
            "detailed_energy_hartree": self.detailed_energy_hartree,
            "detailed_energy_ev": self.detailed_energy_ev,
            "wavenumber_cm1": self.wavenumber_cm1,
            "spectrum_energy_ev": self.spectrum_energy_ev,
            "wavelength_nm": self.wavelength_nm,
            "length_gauge_oscillator_strength": self.length_gauge_oscillator_strength,
            "velocity_gauge_oscillator_strength": self.velocity_gauge_oscillator_strength,
            "primary_oscillator_strength_gauge": self.primary_oscillator_strength_gauge,
            "orbital_transitions": [item.to_dict() for item in self.orbital_transitions],
            "transition_filter": (
                self.transition_filter.to_dict() if self.transition_filter else None
            ),
            "selection_labels": list(self.selection_labels),
            "selection_rationale": self.selection_rationale,
            "source_provenance": dict(self.source_provenance),
            "orca_version": self.orca_version,
            "protocol_metadata": dict(self.protocol_metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ExcitedStateRecord":
        labels = value.get("selection_labels", ())
        transitions = value.get("orbital_transitions", ())
        if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes)):
            raise ExcitedStateDataError("selection_labels must be a sequence")
        if not isinstance(transitions, Sequence) or isinstance(transitions, (str, bytes)):
            raise ExcitedStateDataError("orbital_transitions must be a sequence")
        if not all(
            isinstance(item, (OrbitalTransitionRecord, Mapping))
            for item in transitions
        ):
            raise ExcitedStateDataError(
                "orbital_transitions contain an unsupported record"
            )
        transition_filter = value.get("transition_filter")
        if transition_filter is not None and not isinstance(transition_filter, Mapping):
            raise ExcitedStateDataError("transition_filter must be a mapping")
        local_index = value.get("local_state_index", value["state_index"])
        return cls(
            state_index=int(value["state_index"]),
            excitation_energy_ev=float(value["excitation_energy_ev"]),
            oscillator_strength=float(value["oscillator_strength"]),
            spin_manifold=str(value["spin_manifold"]),
            selection_labels=tuple(str(item) for item in labels),
            selection_rationale=(
                str(value["selection_rationale"])
                if value.get("selection_rationale") is not None
                else None
            ),
            local_state_index=int(local_index),
            orca_global_state_index=_optional_int(value.get("orca_global_state_index")),
            multiplicity=_optional_int(value.get("multiplicity")),
            s_squared=_optional_float(value.get("s_squared")),
            detailed_energy_hartree=_optional_float(value.get("detailed_energy_hartree")),
            detailed_energy_ev=_optional_float(value.get("detailed_energy_ev")),
            wavenumber_cm1=_optional_float(value.get("wavenumber_cm1")),
            spectrum_energy_ev=_optional_float(value.get("spectrum_energy_ev")),
            wavelength_nm=_optional_float(value.get("wavelength_nm")),
            length_gauge_oscillator_strength=_optional_float(
                value.get("length_gauge_oscillator_strength")
            ),
            velocity_gauge_oscillator_strength=_optional_float(
                value.get("velocity_gauge_oscillator_strength")
            ),
            primary_oscillator_strength_gauge=(
                str(value["primary_oscillator_strength_gauge"])
                if value.get("primary_oscillator_strength_gauge") is not None
                else None
            ),
            orbital_transitions=tuple(
                item
                if isinstance(item, OrbitalTransitionRecord)
                else OrbitalTransitionRecord.from_mapping(item)
                for item in transitions
            ),
            transition_filter=(
                TransitionFilterSummary.from_mapping(transition_filter)
                if isinstance(transition_filter, Mapping)
                else None
            ),
            source_provenance=(
                dict(value.get("source_provenance", {}))
                if isinstance(value.get("source_provenance", {}), Mapping)
                else {}
            ),
            orca_version=(
                str(value["orca_version"])
                if value.get("orca_version") is not None
                else None
            ),
            protocol_metadata=(
                dict(value.get("protocol_metadata", {}))
                if isinstance(value.get("protocol_metadata", {}), Mapping)
                else {}
            ),
        )


class StateSelectionStatus(str, Enum):
    SELECTED = "SELECTED"
    NO_MATCH = "NO_MATCH"
    DEFERRED = "DEFERRED"


@dataclass(frozen=True)
class StateSelectionRequest:
    """Configuration for one deterministic, non-CT state selection."""

    policy: str
    spin_manifold: str = "singlet"
    oscillator_strength_gauge: str = "length"
    minimum_oscillator_strength: float = 0.01
    maximum_energy_above_reference_ev: float | None = None
    reference_state: ExcitedStateIdentity | None = None
    energy_interval_ev: tuple[float, float] | None = None
    wavelength_interval_nm: tuple[float, float] | None = None
    window_preference: str = "brightest"

    def __post_init__(self) -> None:
        policy = str(self.policy).strip().casefold()
        manifold = str(self.spin_manifold).strip().casefold()
        if policy == "lowest_triplet":
            manifold = "triplet"
        elif policy == "lowest_singlet":
            manifold = "singlet"
        gauge = str(self.oscillator_strength_gauge).strip().casefold()
        preference = str(self.window_preference).strip().casefold()
        if not policy:
            raise ExcitedStateDataError("state-selection policy is required")
        if manifold not in SPIN_MANIFOLDS:
            raise ExcitedStateDataError("selection spin manifold is unsupported")
        if gauge not in OSCILLATOR_STRENGTH_GAUGES:
            raise ExcitedStateDataError("selection oscillator gauge is unsupported")
        if preference not in {"brightest", "lowest_energy"}:
            raise ExcitedStateDataError("window preference must be brightest or lowest_energy")
        _non_negative_number(
            self.minimum_oscillator_strength, "minimum oscillator strength"
        )
        if self.maximum_energy_above_reference_ev is not None:
            _non_negative_number(
                self.maximum_energy_above_reference_ev,
                "maximum energy above reference",
            )
        _validate_interval(self.energy_interval_ev, "energy")
        _validate_interval(self.wavelength_interval_nm, "wavelength")
        object.__setattr__(self, "policy", policy)
        object.__setattr__(self, "spin_manifold", manifold)
        object.__setattr__(self, "oscillator_strength_gauge", gauge)
        object.__setattr__(self, "window_preference", preference)

    def to_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "spin_manifold": self.spin_manifold,
            "oscillator_strength_gauge": self.oscillator_strength_gauge,
            "minimum_oscillator_strength": self.minimum_oscillator_strength,
            "maximum_energy_above_reference_ev": self.maximum_energy_above_reference_ev,
            "reference_state": self.reference_state.to_dict() if self.reference_state else None,
            "energy_interval_ev": (
                list(self.energy_interval_ev) if self.energy_interval_ev else None
            ),
            "wavelength_interval_nm": (
                list(self.wavelength_interval_nm) if self.wavelength_interval_nm else None
            ),
            "window_preference": self.window_preference,
        }


@dataclass(frozen=True)
class StateSelectionResult:
    status: StateSelectionStatus
    selected_identity: ExcitedStateIdentity | None
    policy: str
    configuration: Mapping[str, object]
    rationale: str
    candidate_count: int
    tie_break: str

    def __post_init__(self) -> None:
        if self.status is StateSelectionStatus.SELECTED and self.selected_identity is None:
            raise ExcitedStateDataError("selected result requires a state identity")
        if self.status is not StateSelectionStatus.SELECTED and self.selected_identity is not None:
            raise ExcitedStateDataError("unresolved selection cannot carry an identity")
        if self.candidate_count < 0:
            raise ExcitedStateDataError("candidate count cannot be negative")
        if not str(self.rationale).strip() or not str(self.tie_break).strip():
            raise ExcitedStateDataError("selection rationale and tie-break are required")
        object.__setattr__(self, "configuration", dict(self.configuration))

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "selected_identity": (
                self.selected_identity.to_dict() if self.selected_identity else None
            ),
            "policy": self.policy,
            "configuration": dict(self.configuration),
            "rationale": self.rationale,
            "candidate_count": self.candidate_count,
            "tie_break": self.tie_break,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "StateSelectionResult":
        identity = value.get("selected_identity")
        return cls(
            StateSelectionStatus(str(value["status"])),
            ExcitedStateIdentity.from_mapping(identity) if isinstance(identity, Mapping) else None,
            str(value["policy"]),
            dict(value.get("configuration", {})),
            str(value["rationale"]),
            int(value["candidate_count"]),
            str(value["tie_break"]),
        )


def resolve_excited_state(
    states: Sequence[ExcitedStateRecord],
    request: StateSelectionRequest,
) -> StateSelectionResult:
    """Resolve one state without inferring charge-transfer character."""

    policy = _canonical_policy(request.policy)
    configuration = request.to_dict()
    if policy == "lowest_ct_like_state":
        return StateSelectionResult(
            StateSelectionStatus.DEFERRED,
            None,
            policy,
            configuration,
            "CT-like state selection requires fragment-resolved hole/electron evidence",
            0,
            "no tie-break: CT classification is deferred",
        )
    candidates = [item for item in states if item.spin_manifold == request.spin_manifold]
    candidates = _apply_intervals(candidates, request)
    if policy in {"lowest_bright_singlet", "brightest_singlet_in_window"}:
        if request.spin_manifold != "singlet":
            raise ExcitedStateDataError(f"{policy} requires the singlet manifold")
        candidates = [
            item
            for item in candidates
            if (value := item.oscillator_strength_for(request.oscillator_strength_gauge))
            is not None
            and value >= request.minimum_oscillator_strength
        ]
    if policy == "brightest_singlet_in_window":
        if request.maximum_energy_above_reference_ev is None:
            raise ExcitedStateDataError(
                "brightest-in-window selection requires a maximum energy above reference"
            )
        reference = _reference_state(states, request)
        upper = reference.excitation_energy_ev + request.maximum_energy_above_reference_ev
        candidates = [
            item
            for item in candidates
            if reference.excitation_energy_ev <= item.excitation_energy_ev <= upper
        ]
    if policy == "experimental_window" and not (
        request.energy_interval_ev or request.wavelength_interval_nm
    ):
        raise ExcitedStateDataError(
            "experimental-window selection requires an energy or wavelength interval"
        )
    if policy == "experimental_window" and request.window_preference == "brightest":
        candidates = [
            item
            for item in candidates
            if item.oscillator_strength_for(request.oscillator_strength_gauge)
            is not None
        ]
    if not candidates:
        return StateSelectionResult(
            StateSelectionStatus.NO_MATCH,
            None,
            policy,
            configuration,
            "no parsed state satisfied the configured manifold and thresholds",
            0,
            "no tie-break: candidate set is empty",
        )
    if policy in {"lowest_state", "lowest_bright_singlet"}:
        ordered = sorted(candidates, key=_lowest_key)
        tie_break = "energy ascending, then local state index, then ORCA global index"
    elif policy == "brightest_singlet_in_window":
        ordered = sorted(
            candidates,
            key=lambda item: _brightest_key(item, request.oscillator_strength_gauge),
        )
        tie_break = (
            f"{request.oscillator_strength_gauge}-gauge oscillator strength descending, "
            "then energy and canonical index ascending"
        )
    elif policy == "experimental_window" and request.window_preference == "lowest_energy":
        ordered = sorted(candidates, key=_lowest_key)
        tie_break = "configured window preference: lowest energy, then canonical index"
    elif policy == "experimental_window":
        ordered = sorted(
            candidates,
            key=lambda item: _brightest_key(item, request.oscillator_strength_gauge),
        )
        tie_break = (
            f"configured window preference: brightest by {request.oscillator_strength_gauge} "
            "gauge, then energy and canonical index"
        )
    else:
        raise ExcitedStateDataError(f"unsupported state-selection policy: {request.policy}")
    selected = ordered[0]
    return StateSelectionResult(
        StateSelectionStatus.SELECTED,
        selected.identity,
        policy,
        configuration,
        f"selected {selected.identity.label} from {len(candidates)} eligible states "
        f"using {policy}",
        len(candidates),
        tie_break,
    )


def resolve_excited_states(
    states: Sequence[ExcitedStateRecord],
    requests: Sequence[StateSelectionRequest],
) -> tuple[StateSelectionResult, ...]:
    return tuple(resolve_excited_state(states, request) for request in requests)


def _canonical_policy(value: str) -> str:
    aliases = {
        "lowest_singlet": "lowest_state",
        "lowest_triplet": "lowest_state",
        "lowest_bright": "lowest_bright_singlet",
        "lowest_bright_state": "lowest_bright_singlet",
        "brightest_in_window": "brightest_singlet_in_window",
        "experimentally_relevant": "experimental_window",
        "lowest_ct_like": "lowest_ct_like_state",
    }
    return aliases.get(value, value)


def _apply_intervals(
    states: Sequence[ExcitedStateRecord], request: StateSelectionRequest
) -> list[ExcitedStateRecord]:
    selected = list(states)
    if request.energy_interval_ev is not None:
        lower, upper = request.energy_interval_ev
        selected = [item for item in selected if lower <= item.excitation_energy_ev <= upper]
    if request.wavelength_interval_nm is not None:
        lower, upper = request.wavelength_interval_nm
        selected = [
            item
            for item in selected
            if item.wavelength_nm is not None and lower <= item.wavelength_nm <= upper
        ]
    return selected


def _reference_state(
    states: Sequence[ExcitedStateRecord], request: StateSelectionRequest
) -> ExcitedStateRecord:
    if request.reference_state is not None:
        matches = [item for item in states if item.identity == request.reference_state]
        if len(matches) != 1:
            raise ExcitedStateDataError("configured reference state is absent or ambiguous")
        return matches[0]
    manifold = [item for item in states if item.spin_manifold == request.spin_manifold]
    if not manifold:
        raise ExcitedStateDataError("reference manifold has no parsed states")
    return min(manifold, key=_lowest_key)


def _lowest_key(item: ExcitedStateRecord) -> tuple[float, int, int]:
    return (
        item.excitation_energy_ev,
        int(item.local_state_index),
        item.orca_global_state_index or int(item.local_state_index),
    )


def _brightest_key(item: ExcitedStateRecord, gauge: str) -> tuple[float, float, int, int]:
    oscillator = item.oscillator_strength_for(gauge)
    if oscillator is None:
        oscillator = -math.inf
    return (
        -oscillator,
        item.excitation_energy_ev,
        int(item.local_state_index),
        item.orca_global_state_index or int(item.local_state_index),
    )


def _validate_interval(value: tuple[float, float] | None, label: str) -> None:
    if value is None:
        return
    if len(value) != 2:
        raise ExcitedStateDataError(f"{label} interval requires lower and upper bounds")
    lower, upper = (float(item) for item in value)
    if not all(math.isfinite(item) and item > 0.0 for item in (lower, upper)):
        raise ExcitedStateDataError(f"{label} interval bounds must be positive and finite")
    if lower > upper:
        raise ExcitedStateDataError(f"{label} interval lower bound exceeds upper bound")


def _positive_number(value: object, label: str, *, allow_zero: bool = False) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0 or (parsed == 0.0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ExcitedStateDataError(f"{label} must be finite and {qualifier}")
    return parsed


def _non_negative_number(value: object, label: str) -> float:
    return _positive_number(value, label, allow_zero=True)


def _optional_non_negative(value: object | None, label: str) -> float | None:
    return None if value is None else _non_negative_number(value, label)


def _optional_float(value: object | None) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: object | None) -> int | None:
    return None if value is None else int(value)


__all__ = [
    "ExcitedStateDataError",
    "ExcitedStateIdentity",
    "ExcitedStateRecord",
    "OrbitalTransitionRecord",
    "StateSelectionRequest",
    "StateSelectionResult",
    "StateSelectionStatus",
    "TransitionFilterSummary",
    "resolve_excited_state",
    "resolve_excited_states",
]
