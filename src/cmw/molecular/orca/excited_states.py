"""Version-aware, fail-closed parsing of ORCA 6.1 TDA excited states."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Mapping, Sequence

from cmw.molecular.excited_states import (
    ExcitedStateRecord,
    OrbitalTransitionRecord,
    TransitionFilterSummary,
)

from .status import parse_orca_input_echo, parse_orca_output


ORCA_EXCITED_STATE_PARSER_VERSION = "1.0.0"
ORCA_EXCITED_STATE_GRAMMAR = "orca_6_1_tda_v1"
DEFAULT_ENERGY_JOIN_TOLERANCE_EV = 0.002

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
STATE_SECTION_RE = re.compile(
    r"^TD-DFT/TDA EXCITED STATES \((SINGLETS|TRIPLETS)\)\s*$", re.M
)
STATE_RE = re.compile(
    rf"^STATE\s+(\d+):\s+E=\s+({NUMBER})\s+au\s+({NUMBER})\s+eV\s+"
    rf"({NUMBER})\s+cm\*\*-1\s+<S\*\*2>\s+=\s+({NUMBER})\s+Mult\s+(\d+)\s*$",
    re.M,
)
TRANSITION_RE = re.compile(
    rf"^\s*(\d+[aAbB]?)\s*->\s*(\d+[aAbB]?)\s*:\s*({NUMBER})\s*"
    rf"\(c=\s*({NUMBER})\)\s*$",
    re.M,
)
SPECTRUM_ROW_RE = re.compile(
    rf"^\s*\d+-\S+\s+->\s+(\d+)-([13])A\s+({NUMBER})\s+({NUMBER})\s+"
    rf"({NUMBER})\s+({NUMBER})(?:\s+{NUMBER}){{4}}\s*$",
    re.M,
)
ROOT_COUNT_RE = re.compile(
    r"^Number of roots to be determined\s+\.\.\.\s+(\d+)\s*$", re.M
)
TRIPLET_GENERATION_RE = re.compile(
    r"^Generation of triplets\s+\.\.\.\s+(on|off)\s*$", re.I | re.M
)
TDA_OPERATIVE_RE = re.compile(
    r"^Tamm-Dancoff approximation\s+\.\.\.\s+operative\s*$", re.I | re.M
)
BASIS_RE = re.compile(
    r"^Your calculation utilizes the basis:\s*(\S.*?)\s*$", re.I | re.M
)
EXCHANGE_FUNCTIONAL_RE = re.compile(
    r"^Exchange functional \(TD-DFT\)\s+\.\.\.\s+(\S.*?)\s*$", re.I | re.M
)
CORRELATION_FUNCTIONAL_RE = re.compile(
    r"^Correlation functional \(TD-DFT\)\s+\.\.\.\s+(\S.*?)\s*$", re.I | re.M
)
INPUT_TDA_RE = re.compile(
    r"^\s*\|\s*\d+\s*>\s*tda\s+(true|false)\s*$", re.I | re.M
)
INPUT_TRIPLETS_RE = re.compile(
    r"^\s*\|\s*\d+\s*>\s*triplets\s+(true|false)\s*$", re.I | re.M
)
INPUT_TPRINT_RE = re.compile(
    rf"^\s*\|\s*\d+\s*>\s*TPrint\s+({NUMBER})\s*$", re.I | re.M
)
INPUT_DOSOC_RE = re.compile(
    r"^\s*\|\s*\d+\s*>\s*DoSOC\s+true\s*$", re.I | re.M
)

ELECTRIC_HEADING = "ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS"
VELOCITY_HEADING = "ABSORPTION SPECTRUM VIA TRANSITION VELOCITY DIPOLE MOMENTS"


class OrcaExcitedStateError(ValueError):
    """Base error carrying a stable machine-readable failure code."""

    code = "FAILED_EXCITED_STATE_PARSE"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class UnsupportedExcitedStateFormatError(OrcaExcitedStateError):
    code = "UNSUPPORTED_EXCITED_STATE_FORMAT"


class ExcitedStateParseError(OrcaExcitedStateError):
    code = "FAILED_EXCITED_STATE_PARSE"


class ExcitedStateJoinError(OrcaExcitedStateError):
    code = "FAILED_EXCITED_STATE_JOIN"


@dataclass(frozen=True)
class OrcaTDAProtocolMetadata:
    orca_version: str
    theory: str
    tda: bool
    functional: str
    basis: str
    requested_singlet_roots: int
    requested_triplet_roots: int
    triplets_enabled: bool
    solvent_model: str | None
    solvent_name: str | None
    tprint_threshold: float | None
    input_keyword_line: str

    def to_dict(self) -> dict[str, object]:
        return {
            "orca_version": self.orca_version,
            "theory": self.theory,
            "tda": self.tda,
            "functional": self.functional,
            "basis": self.basis,
            "requested_singlet_roots": self.requested_singlet_roots,
            "requested_triplet_roots": self.requested_triplet_roots,
            "triplets_enabled": self.triplets_enabled,
            "solvent_model": self.solvent_model,
            "solvent_name": self.solvent_name,
            "tprint_threshold": self.tprint_threshold,
            "input_keyword_line": self.input_keyword_line,
        }


@dataclass(frozen=True)
class ParsedOrcaExcitedStates:
    states: tuple[ExcitedStateRecord, ...]
    protocol: OrcaTDAProtocolMetadata
    source_provenance: Mapping[str, object]
    parser_version: str = ORCA_EXCITED_STATE_PARSER_VERSION
    grammar_version: str = ORCA_EXCITED_STATE_GRAMMAR
    energy_join_tolerance_ev: float = DEFAULT_ENERGY_JOIN_TOLERANCE_EV
    normal_termination: bool = True
    excited_state_completion: bool = True

    def __post_init__(self) -> None:
        identities = [item.canonical_key for item in self.states]
        global_indices = [item.orca_global_state_index for item in self.states]
        if not self.states or len(identities) != len(set(identities)):
            raise ExcitedStateParseError("parsed state identities are empty or duplicated")
        if None in global_indices or len(global_indices) != len(set(global_indices)):
            raise ExcitedStateParseError("ORCA global state indices are missing or duplicated")
        object.__setattr__(self, "source_provenance", dict(self.source_provenance))

    def state(self, spin_manifold: str, local_state_index: int) -> ExcitedStateRecord:
        key = (str(spin_manifold).casefold(), int(local_state_index))
        matches = [item for item in self.states if item.canonical_key == key]
        if len(matches) != 1:
            raise KeyError(f"excited state {key!r} is absent or ambiguous")
        return matches[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "parser_version": self.parser_version,
            "grammar_version": self.grammar_version,
            "energy_join_tolerance_ev": self.energy_join_tolerance_ev,
            "normal_termination": self.normal_termination,
            "excited_state_completion": self.excited_state_completion,
            "protocol": self.protocol.to_dict(),
            "source_provenance": dict(self.source_provenance),
            "states": [item.to_dict() for item in self.states],
        }


@dataclass(frozen=True)
class _DetailedState:
    spin_manifold: str
    local_state_index: int
    orca_global_state_index: int
    multiplicity: int
    s_squared: float
    energy_hartree: float
    energy_ev: float
    wavenumber_cm1: float
    transitions: tuple[OrbitalTransitionRecord, ...]


@dataclass(frozen=True)
class _SpectrumRow:
    spin_manifold: str
    local_state_index: int
    energy_ev: float
    wavenumber_cm1: float
    wavelength_nm: float
    oscillator_strength: float

    @property
    def key(self) -> tuple[str, int]:
        return self.spin_manifold, self.local_state_index


def parse_orca_tda_excited_states(
    text: str,
    *,
    source_path: str | Path | None = None,
    source_sha256: str | None = None,
    source_size_bytes: int | None = None,
    energy_join_tolerance_ev: float = DEFAULT_ENERGY_JOIN_TOLERANCE_EV,
    transition_weight_cutoff: float | None = None,
    expected_protocol: Mapping[str, object] | None = None,
) -> ParsedOrcaExcitedStates:
    """Parse one complete ORCA 6.1.x non-SOC TDA output."""

    if not isinstance(text, str) or not text.strip():
        raise ExcitedStateParseError("ORCA output text is empty")
    tolerance = float(energy_join_tolerance_ev)
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ExcitedStateParseError("energy join tolerance must be positive and finite")
    cutoff = None if transition_weight_cutoff is None else float(transition_weight_cutoff)
    if cutoff is not None and (not math.isfinite(cutoff) or cutoff < 0.0):
        raise ExcitedStateParseError("transition weight cutoff must be non-negative")
    _reject_unsupported_format(text)
    evidence = parse_orca_output(text)
    version = evidence.orca_version
    if version is None or not re.fullmatch(r"6\.1(?:\.\d+)?", version):
        raise UnsupportedExcitedStateFormatError(
            f"only fixture-tested ORCA 6.1.x grammar is supported, found {version!r}"
        )
    if not evidence.normal_termination:
        raise ExcitedStateParseError("ORCA output lacks normal termination")
    if (
        not evidence.excited_state_analysis_completed
        or "*** ORCA-CIS/TD-DFT FINISHED WITHOUT ERROR ***" not in text
    ):
        raise ExcitedStateParseError("ORCA excited-state calculation is incomplete")
    protocol = _parse_protocol(text, version)
    if expected_protocol is not None:
        _validate_declared_protocol(protocol, expected_protocol)
    detailed = _parse_detailed_states(text)
    _validate_root_counts(detailed, protocol)
    electric = _parse_spectrum(text, ELECTRIC_HEADING, required=True)
    velocity = _parse_spectrum(text, VELOCITY_HEADING, required=False)
    provenance = _source_provenance(
        text,
        source_path=source_path,
        source_sha256=source_sha256,
        source_size_bytes=source_size_bytes,
    )
    provenance.update(
        {
            "normal_termination": True,
            "excited_state_completion": True,
            "energy_join_tolerance_ev": tolerance,
        }
    )
    records = _join_states(
        detailed,
        electric,
        velocity,
        protocol,
        provenance,
        tolerance,
        cutoff,
    )
    return ParsedOrcaExcitedStates(
        records,
        protocol,
        provenance,
        energy_join_tolerance_ev=tolerance,
    )


def parse_orca_tda_excited_states_file(
    path: str | Path,
    *,
    energy_join_tolerance_ev: float = DEFAULT_ENERGY_JOIN_TOLERANCE_EV,
    transition_weight_cutoff: float | None = None,
    expected_protocol: Mapping[str, object] | None = None,
) -> ParsedOrcaExcitedStates:
    source = Path(path)
    payload = source.read_bytes()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExcitedStateParseError("ORCA output is not UTF-8 text") from exc
    return parse_orca_tda_excited_states(
        text,
        source_path=source,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        source_size_bytes=len(payload),
        energy_join_tolerance_ev=energy_join_tolerance_ev,
        transition_weight_cutoff=transition_weight_cutoff,
        expected_protocol=expected_protocol,
    )


def _reject_unsupported_format(text: str) -> None:
    if re.search(r"SOC CORRECTED|SPIN.ORBIT COUPLING", text, re.I) or INPUT_DOSOC_RE.search(text):
        raise UnsupportedExcitedStateFormatError("SOC-corrected excited states are unsupported")
    input_tda = INPUT_TDA_RE.findall(text)
    if input_tda and any(value.casefold() != "true" for value in input_tda):
        raise UnsupportedExcitedStateFormatError("full TDDFT is unsupported")
    if not TDA_OPERATIVE_RE.search(text):
        raise UnsupportedExcitedStateFormatError(
            "output does not prove that the Tamm-Dancoff approximation was operative"
        )
    if re.search(r"^TD-DFT EXCITED STATES", text, re.M):
        raise UnsupportedExcitedStateFormatError("full TDDFT state grammar is unsupported")


def _parse_protocol(text: str, version: str) -> OrcaTDAProtocolMetadata:
    keyword_lines = parse_orca_input_echo(text)
    if len(keyword_lines) != 1:
        raise ExcitedStateParseError("output lacks one unambiguous simple-input keyword line")
    keyword_line = keyword_lines[0]
    bases = tuple(dict.fromkeys(match.strip() for match in BASIS_RE.findall(text)))
    exchanges = tuple(
        dict.fromkeys(match.strip() for match in EXCHANGE_FUNCTIONAL_RE.findall(text))
    )
    correlations = tuple(
        dict.fromkeys(match.strip() for match in CORRELATION_FUNCTIONAL_RE.findall(text))
    )
    if len(bases) != 1:
        raise ExcitedStateParseError("output lacks one unambiguous orbital basis")
    if len(exchanges) != 1 or len(correlations) != 1 or exchanges != correlations:
        raise ExcitedStateParseError("output lacks one consistent TD-DFT functional")
    triplet_modes = tuple(value.casefold() for value in TRIPLET_GENERATION_RE.findall(text))
    if len(set(triplet_modes)) != 1 or not triplet_modes:
        raise ExcitedStateParseError("triplet-generation mode is missing or ambiguous")
    triplets_enabled = triplet_modes[0] == "on"
    input_triplets = tuple(value.casefold() == "true" for value in INPUT_TRIPLETS_RE.findall(text))
    if len(input_triplets) != 1 or input_triplets[0] != triplets_enabled:
        raise ExcitedStateParseError("input and output triplet intent do not agree")
    roots = tuple(int(value) for value in ROOT_COUNT_RE.findall(text))
    expected_root_sections = 2 if triplets_enabled else 1
    if len(roots) != expected_root_sections or any(value < 1 for value in roots):
        raise ExcitedStateParseError("requested root counts are missing or ambiguous")
    tprint_values = tuple(_number(value) for value in INPUT_TPRINT_RE.findall(text))
    if len(tprint_values) > 1:
        raise ExcitedStateParseError("TPrint threshold is ambiguous")
    solvent = re.search(r"\b(SMD)\(([^)]+)\)", keyword_line, re.I)
    return OrcaTDAProtocolMetadata(
        orca_version=version,
        theory="TDA",
        tda=True,
        functional=exchanges[0],
        basis=bases[0],
        requested_singlet_roots=roots[0],
        requested_triplet_roots=roots[1] if triplets_enabled else 0,
        triplets_enabled=triplets_enabled,
        solvent_model=solvent.group(1).upper() if solvent else None,
        solvent_name=solvent.group(2) if solvent else None,
        tprint_threshold=tprint_values[0] if tprint_values else None,
        input_keyword_line=keyword_line,
    )


def _parse_detailed_states(text: str) -> tuple[_DetailedState, ...]:
    headers = list(STATE_SECTION_RE.finditer(text))
    if not headers or len({item.group(1) for item in headers}) != len(headers):
        raise ExcitedStateParseError("excited-state manifold sections are missing or duplicated")
    states: list[_DetailedState] = []
    for header_index, header in enumerate(headers):
        manifold = "singlet" if header.group(1) == "SINGLETS" else "triplet"
        end = headers[header_index + 1].start() if header_index + 1 < len(headers) else text.find(
            "TD-DFT/TDA-EXCITATION SPECTRA", header.end()
        )
        if end < 0:
            raise ExcitedStateParseError(f"{manifold} detailed-state section is not bounded")
        section = text[header.end() : end]
        state_matches = list(STATE_RE.finditer(section))
        if not state_matches:
            raise ExcitedStateParseError(f"{manifold} section contains no detailed states")
        expected_multiplicity = 1 if manifold == "singlet" else 3
        for local_index, match in enumerate(state_matches, 1):
            transition_end = (
                state_matches[local_index].start()
                if local_index < len(state_matches)
                else len(section)
            )
            transitions = tuple(
                OrbitalTransitionRecord(
                    transition.group(1),
                    transition.group(2),
                    _number(transition.group(3)),
                    _number(transition.group(4)),
                )
                for transition in TRANSITION_RE.finditer(
                    section, match.end(), transition_end
                )
            )
            multiplicity = int(match.group(6))
            if multiplicity != expected_multiplicity:
                raise ExcitedStateParseError(
                    f"{manifold} state {local_index} has multiplicity {multiplicity}"
                )
            states.append(
                _DetailedState(
                    manifold,
                    local_index,
                    int(match.group(1)),
                    multiplicity,
                    _number(match.group(5)),
                    _number(match.group(2)),
                    _number(match.group(3)),
                    _number(match.group(4)),
                    transitions,
                )
            )
    global_indices = [item.orca_global_state_index for item in states]
    if len(global_indices) != len(set(global_indices)):
        raise ExcitedStateParseError("detailed-state global indices are duplicated")
    return tuple(states)


def _parse_spectrum(
    text: str, heading: str, *, required: bool
) -> dict[tuple[str, int], _SpectrumRow]:
    heading_matches = list(re.finditer(re.escape(heading), text))
    if not heading_matches:
        if required:
            raise ExcitedStateParseError(f"required spectrum is missing: {heading}")
        return {}
    if len(heading_matches) != 1:
        raise ExcitedStateParseError(f"spectrum heading is duplicated: {heading}")
    start = heading_matches[0].end()
    boundary_candidates = [
        position
        for marker in (
            ELECTRIC_HEADING,
            VELOCITY_HEADING,
            "CD SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS",
            "*** ORCA-CIS/TD-DFT FINISHED WITHOUT ERROR ***",
        )
        if (position := text.find(marker, start)) >= 0
    ]
    end = min(boundary_candidates) if boundary_candidates else len(text)
    rows: dict[tuple[str, int], _SpectrumRow] = {}
    for match in SPECTRUM_ROW_RE.finditer(text, start, end):
        local_index = int(match.group(1))
        manifold = "singlet" if match.group(2) == "1" else "triplet"
        row = _SpectrumRow(
            manifold,
            local_index,
            _number(match.group(3)),
            _number(match.group(4)),
            _number(match.group(5)),
            _number(match.group(6)),
        )
        if row.key in rows:
            raise ExcitedStateJoinError(
                f"duplicate {heading.casefold()} row for {row.key}"
            )
        rows[row.key] = row
    if not rows:
        raise ExcitedStateParseError(f"spectrum contains no parseable rows: {heading}")
    return rows


def _validate_root_counts(
    states: Sequence[_DetailedState], protocol: OrcaTDAProtocolMetadata
) -> None:
    counts = {
        manifold: sum(item.spin_manifold == manifold for item in states)
        for manifold in ("singlet", "triplet")
    }
    expected = {
        "singlet": protocol.requested_singlet_roots,
        "triplet": protocol.requested_triplet_roots,
    }
    if counts != expected:
        raise ExcitedStateParseError(
            f"detailed-state counts {counts} do not match requested roots {expected}"
        )


def _join_states(
    detailed: Sequence[_DetailedState],
    electric: Mapping[tuple[str, int], _SpectrumRow],
    velocity: Mapping[tuple[str, int], _SpectrumRow],
    protocol: OrcaTDAProtocolMetadata,
    source_provenance: Mapping[str, object],
    tolerance: float,
    transition_weight_cutoff: float | None,
) -> tuple[ExcitedStateRecord, ...]:
    detailed_keys = {(item.spin_manifold, item.local_state_index) for item in detailed}
    if set(electric) != detailed_keys:
        missing = sorted(detailed_keys - set(electric))
        extra = sorted(set(electric) - detailed_keys)
        raise ExcitedStateJoinError(
            "electric-dipole identities do not match detailed states; "
            f"missing={missing}, extra={extra}"
        )
    if velocity and set(velocity) != detailed_keys:
        missing = sorted(detailed_keys - set(velocity))
        extra = sorted(set(velocity) - detailed_keys)
        raise ExcitedStateJoinError(
            "velocity-dipole identities do not match detailed states; "
            f"missing={missing}, extra={extra}"
        )
    records: list[ExcitedStateRecord] = []
    for item in detailed:
        key = (item.spin_manifold, item.local_state_index)
        length_row = electric[key]
        velocity_row = velocity.get(key)
        if abs(item.energy_ev - length_row.energy_ev) > tolerance:
            raise ExcitedStateJoinError(
                f"detailed and electric-dipole energies disagree for {key}: "
                f"{item.energy_ev} versus {length_row.energy_ev} eV"
            )
        if velocity_row is not None and (
            abs(length_row.energy_ev - velocity_row.energy_ev) > tolerance
            or abs(length_row.wavelength_nm - velocity_row.wavelength_nm) > 0.05
        ):
            raise ExcitedStateJoinError(
                f"length- and velocity-gauge identities disagree for {key}"
            )
        transition_filter = None
        if transition_weight_cutoff is not None:
            retained = sum(
                transition.contribution_weight >= transition_weight_cutoff
                for transition in item.transitions
            )
            transition_filter = TransitionFilterSummary(
                transition_weight_cutoff,
                len(item.transitions),
                retained,
                len(item.transitions) - retained,
            )
        records.append(
            ExcitedStateRecord(
                state_index=item.local_state_index,
                local_state_index=item.local_state_index,
                orca_global_state_index=item.orca_global_state_index,
                excitation_energy_ev=length_row.energy_ev,
                oscillator_strength=length_row.oscillator_strength,
                spin_manifold=item.spin_manifold,
                multiplicity=item.multiplicity,
                s_squared=item.s_squared,
                detailed_energy_hartree=item.energy_hartree,
                detailed_energy_ev=item.energy_ev,
                wavenumber_cm1=item.wavenumber_cm1,
                spectrum_energy_ev=length_row.energy_ev,
                wavelength_nm=length_row.wavelength_nm,
                length_gauge_oscillator_strength=length_row.oscillator_strength,
                velocity_gauge_oscillator_strength=(
                    velocity_row.oscillator_strength if velocity_row else None
                ),
                primary_oscillator_strength_gauge="length",
                orbital_transitions=item.transitions,
                transition_filter=transition_filter,
                source_provenance=source_provenance,
                orca_version=protocol.orca_version,
                protocol_metadata=protocol.to_dict(),
            )
        )
    return tuple(records)


def _validate_declared_protocol(
    parsed: OrcaTDAProtocolMetadata, expected: Mapping[str, object]
) -> None:
    aliases = {
        "number_of_roots": "requested_singlet_roots",
        "functional": "functional",
        "method": "functional",
        "basis": "basis",
        "tda": "tda",
        "requested_singlet_roots": "requested_singlet_roots",
        "requested_triplet_roots": "requested_triplet_roots",
        "triplets_enabled": "triplets_enabled",
        "solvent_model": "solvent_model",
        "solvent_name": "solvent_name",
        "tprint_threshold": "tprint_threshold",
    }
    observed = parsed.to_dict()
    mismatches: list[str] = []
    for supplied_key, value in expected.items():
        key = aliases.get(str(supplied_key))
        if key is None or value is None:
            continue
        actual = observed[key]
        if isinstance(value, float) and actual is not None:
            matches = math.isclose(float(actual), value, rel_tol=1.0e-12, abs_tol=1.0e-15)
        elif isinstance(value, str) and isinstance(actual, str):
            matches = value.casefold() == actual.casefold()
        else:
            matches = value == actual
        if not matches:
            mismatches.append(f"{supplied_key}={value!r} (output {actual!r})")
    if mismatches:
        raise ExcitedStateParseError(
            "declared protocol does not match ORCA output: " + ", ".join(mismatches)
        )


def _source_provenance(
    text: str,
    *,
    source_path: str | Path | None,
    source_sha256: str | None,
    source_size_bytes: int | None,
) -> dict[str, object]:
    payload = text.encode("utf-8")
    return {
        "source_path": str(source_path) if source_path is not None else None,
        "source_sha256": source_sha256 or hashlib.sha256(payload).hexdigest(),
        "source_size_bytes": source_size_bytes if source_size_bytes is not None else len(payload),
        "parser_version": ORCA_EXCITED_STATE_PARSER_VERSION,
        "fixture_tested_grammar_version": ORCA_EXCITED_STATE_GRAMMAR,
    }


def _number(value: str) -> float:
    parsed = float(value.replace("D", "E").replace("d", "e"))
    if not math.isfinite(parsed):
        raise ExcitedStateParseError("ORCA output contains a non-finite number")
    return parsed


__all__ = [
    "DEFAULT_ENERGY_JOIN_TOLERANCE_EV",
    "ExcitedStateJoinError",
    "ExcitedStateParseError",
    "ORCA_EXCITED_STATE_GRAMMAR",
    "ORCA_EXCITED_STATE_PARSER_VERSION",
    "OrcaExcitedStateError",
    "OrcaTDAProtocolMetadata",
    "ParsedOrcaExcitedStates",
    "UnsupportedExcitedStateFormatError",
    "parse_orca_tda_excited_states",
    "parse_orca_tda_excited_states_file",
]
