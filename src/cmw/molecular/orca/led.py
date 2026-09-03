"""ORCA 6.1.1 intermolecular LED parsing and numerical reconstruction."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from cmw.molecular.stacking.ground_state import HARTREE_TO_KJ_MOL

from .status import parse_orca_output


ORCA_INTERMOLECULAR_LED_PARSER_VERSION = "1.0.0"
ORCA_INTERMOLECULAR_LED_GRAMMAR = "orca_6_1_1_interfragment_led_v1"
ORCA_LED_FRAGMENT_REFERENCE_GRAMMAR = (
    "orca_6_1_1_dlpno_fragment_reference_v1"
)
DEFAULT_LED_RECONSTRUCTION_TOLERANCE_HARTREE = 1.0e-6

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
LED_HEADER_RE = re.compile(
    r"^\s*LOCAL ENERGY DECOMPOSITION FOR DLPNO-CC METHODS\s*$", re.M
)
LED_END_RE = re.compile(r"^\s*#Time for LED analysis\s*\.+", re.M)


class LEDReferenceSemantics(str, Enum):
    """Versioned energy-reference convention used by the LED assembler."""

    FROZEN_FRAGMENT_IN_DIMER_BASIS = "FROZEN_FRAGMENT_IN_DIMER_BASIS"


class OrcaLEDError(ValueError):
    """Base error carrying a stable machine-readable LED failure code."""

    code = "FAILED_LED_PARSE"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class UnsupportedOrcaLEDFormatError(OrcaLEDError):
    code = "UNSUPPORTED_LED_FORMAT"


class OrcaLEDParseError(OrcaLEDError):
    code = "FAILED_LED_PARSE"


class LEDReconstructionError(OrcaLEDError):
    code = "FAILED_LED_RECONSTRUCTION"


def _finite(value: object, *, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise OrcaLEDParseError(f"{name} is not numeric") from exc
    if not math.isfinite(parsed):
        raise OrcaLEDParseError(f"{name} is not finite")
    return parsed


def _number(value: str, *, name: str) -> float:
    return _finite(value.replace("D", "E").replace("d", "e"), name=name)


def _single_number(
    pattern: str | re.Pattern[str], text: str, *, name: str
) -> float:
    compiled = (
        pattern
        if isinstance(pattern, re.Pattern)
        else re.compile(pattern, re.M | re.S)
    )
    matches = compiled.findall(text)
    if len(matches) != 1:
        qualifier = "missing" if not matches else "ambiguous"
        raise OrcaLEDParseError(f"{name} is {qualifier}")
    value = matches[0]
    if isinstance(value, tuple):
        if len(value) != 1:
            raise OrcaLEDParseError(f"{name} parser returned an invalid row")
        value = value[0]
    return _number(str(value), name=name)


def _unique_number(pattern: str, text: str, *, name: str) -> float:
    matches = re.findall(pattern, text, re.M)
    if not matches:
        raise OrcaLEDParseError(f"{name} is missing")
    values = tuple(_number(item, name=name) for item in matches)
    if len(set(values)) != 1:
        raise OrcaLEDParseError(f"{name} is ambiguous")
    return values[0]


def _source_identity(
    payload: bytes,
    *,
    source_path: str | Path | None,
    source_sha256: str | None,
    source_size_bytes: int | None,
    grammar: str,
) -> dict[str, object]:
    digest = hashlib.sha256(payload).hexdigest()
    size = len(payload)
    if source_sha256 is not None and source_sha256 != digest:
        raise OrcaLEDParseError("declared source-output hash does not match content")
    if source_size_bytes is not None and source_size_bytes != size:
        raise OrcaLEDParseError("declared source-output size does not match content")
    return {
        "source_path": str(source_path) if source_path is not None else None,
        "source_sha256": digest,
        "source_size_bytes": size,
        "parser_version": ORCA_INTERMOLECULAR_LED_PARSER_VERSION,
        "fixture_tested_grammar_version": grammar,
    }


def _validated_orca_611(text: str) -> str:
    evidence = parse_orca_output(text)
    if evidence.orca_version != "6.1.1":
        raise UnsupportedOrcaLEDFormatError(
            "validated intermolecular LED grammar requires ORCA 6.1.1"
        )
    if not evidence.normal_termination:
        raise OrcaLEDParseError("ORCA output lacks normal termination")
    return evidence.orca_version


@dataclass(frozen=True)
class OrcaLEDFragmentPartition:
    orca_fragment_index: int
    atom_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        indices = tuple(int(item) for item in self.atom_indices)
        if self.orca_fragment_index < 1 or not indices:
            raise OrcaLEDParseError("ORCA LED fragment partition is invalid")
        if len(set(indices)) != len(indices) or min(indices) < 0:
            raise OrcaLEDParseError(
                "ORCA LED fragment atoms must be unique zero-based indices"
            )
        object.__setattr__(self, "atom_indices", indices)

    def to_dict(self) -> dict[str, object]:
        return {
            "orca_fragment_index": self.orca_fragment_index,
            "atom_indices": list(self.atom_indices),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OrcaLEDFragmentPartition":
        return cls(
            int(value["orca_fragment_index"]),
            tuple(int(item) for item in value["atom_indices"]),
        )


@dataclass(frozen=True)
class OrcaDimerLEDQuantities:
    """Raw quantities printed by the dimer ORCA LED calculation."""

    orca_version: str
    fragment_partitions: tuple[OrcaLEDFragmentPartition, ...]
    intra_fragment_reference_energies_hartree: tuple[float, ...]
    electrostatics_reference_hartree: float
    exchange_reference_hartree: float
    dispersion_strong_pairs_hartree: float
    dispersion_weak_pairs_hartree: float
    non_dispersion_strong_pairs_hartree: float
    non_dispersion_weak_pairs_hartree: float
    dimer_triples_correction_hartree: float
    interfragment_strong_pairs_hartree: float
    interfragment_triples_hartree: float
    interfragment_weak_pairs_hartree: float
    orca_dimer_direct_reference_interaction_hartree: float
    orca_dimer_direct_correlation_interaction_hartree: float
    orca_dimer_direct_total_interaction_hartree: float
    final_ccsd_t_energy_hartree: float
    source_output_identity: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.orca_version != "6.1.1":
            raise UnsupportedOrcaLEDFormatError(
                "dimer LED quantities require the validated ORCA 6.1.1 grammar"
            )
        if len(self.fragment_partitions) != 2:
            raise OrcaLEDParseError("standard intermolecular LED requires two fragments")
        if len(self.intra_fragment_reference_energies_hartree) != 2:
            raise OrcaLEDParseError("two intra-fragment reference energies are required")
        numeric = (
            *self.intra_fragment_reference_energies_hartree,
            self.electrostatics_reference_hartree,
            self.exchange_reference_hartree,
            self.dispersion_strong_pairs_hartree,
            self.dispersion_weak_pairs_hartree,
            self.non_dispersion_strong_pairs_hartree,
            self.non_dispersion_weak_pairs_hartree,
            self.dimer_triples_correction_hartree,
            self.interfragment_strong_pairs_hartree,
            self.interfragment_triples_hartree,
            self.interfragment_weak_pairs_hartree,
            self.orca_dimer_direct_reference_interaction_hartree,
            self.orca_dimer_direct_correlation_interaction_hartree,
            self.orca_dimer_direct_total_interaction_hartree,
            self.final_ccsd_t_energy_hartree,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise OrcaLEDParseError("dimer LED quantities must be finite")
        if set(self.fragment_partitions[0].atom_indices).intersection(
            self.fragment_partitions[1].atom_indices
        ):
            raise OrcaLEDParseError("ORCA LED fragments overlap")
        object.__setattr__(
            self, "source_output_identity", dict(self.source_output_identity)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "orca_version": self.orca_version,
            "parser_grammar": ORCA_INTERMOLECULAR_LED_GRAMMAR,
            "fragment_partitions": [item.to_dict() for item in self.fragment_partitions],
            "intra_fragment_reference_energies_hartree": list(
                self.intra_fragment_reference_energies_hartree
            ),
            "electrostatics_reference_hartree": self.electrostatics_reference_hartree,
            "exchange_reference_hartree": self.exchange_reference_hartree,
            "dispersion_strong_pairs_hartree": self.dispersion_strong_pairs_hartree,
            "dispersion_weak_pairs_hartree": self.dispersion_weak_pairs_hartree,
            "non_dispersion_strong_pairs_hartree": self.non_dispersion_strong_pairs_hartree,
            "non_dispersion_weak_pairs_hartree": self.non_dispersion_weak_pairs_hartree,
            "dimer_triples_correction_hartree": self.dimer_triples_correction_hartree,
            "interfragment_strong_pairs_hartree": self.interfragment_strong_pairs_hartree,
            "interfragment_triples_hartree": self.interfragment_triples_hartree,
            "interfragment_weak_pairs_hartree": self.interfragment_weak_pairs_hartree,
            "orca_dimer_direct_reference_interaction_hartree": (
                self.orca_dimer_direct_reference_interaction_hartree
            ),
            "orca_dimer_direct_correlation_interaction_hartree": (
                self.orca_dimer_direct_correlation_interaction_hartree
            ),
            "orca_dimer_direct_total_interaction_hartree": (
                self.orca_dimer_direct_total_interaction_hartree
            ),
            "final_ccsd_t_energy_hartree": self.final_ccsd_t_energy_hartree,
            "source_output_identity": dict(self.source_output_identity),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OrcaDimerLEDQuantities":
        if value.get("parser_grammar") != ORCA_INTERMOLECULAR_LED_GRAMMAR:
            raise UnsupportedOrcaLEDFormatError("unknown stored dimer LED grammar")
        return cls(
            orca_version=str(value["orca_version"]),
            fragment_partitions=tuple(
                OrcaLEDFragmentPartition.from_mapping(item)
                for item in value["fragment_partitions"]
            ),
            intra_fragment_reference_energies_hartree=tuple(
                _finite(item, name="intra-fragment reference energy")
                for item in value["intra_fragment_reference_energies_hartree"]
            ),
            electrostatics_reference_hartree=_finite(
                value["electrostatics_reference_hartree"], name="electrostatics"
            ),
            exchange_reference_hartree=_finite(
                value["exchange_reference_hartree"], name="exchange"
            ),
            dispersion_strong_pairs_hartree=_finite(
                value["dispersion_strong_pairs_hartree"],
                name="strong-pair dispersion",
            ),
            dispersion_weak_pairs_hartree=_finite(
                value["dispersion_weak_pairs_hartree"],
                name="weak-pair dispersion",
            ),
            non_dispersion_strong_pairs_hartree=_finite(
                value["non_dispersion_strong_pairs_hartree"],
                name="strong-pair non-dispersion",
            ),
            non_dispersion_weak_pairs_hartree=_finite(
                value["non_dispersion_weak_pairs_hartree"],
                name="weak-pair non-dispersion",
            ),
            dimer_triples_correction_hartree=_finite(
                value["dimer_triples_correction_hartree"], name="dimer triples"
            ),
            interfragment_strong_pairs_hartree=_finite(
                value["interfragment_strong_pairs_hartree"],
                name="direct interfragment strong pairs",
            ),
            interfragment_triples_hartree=_finite(
                value["interfragment_triples_hartree"],
                name="direct interfragment triples",
            ),
            interfragment_weak_pairs_hartree=_finite(
                value["interfragment_weak_pairs_hartree"],
                name="direct interfragment weak pairs",
            ),
            orca_dimer_direct_reference_interaction_hartree=_finite(
                value["orca_dimer_direct_reference_interaction_hartree"],
                name="ORCA dimer direct reference interaction",
            ),
            orca_dimer_direct_correlation_interaction_hartree=_finite(
                value["orca_dimer_direct_correlation_interaction_hartree"],
                name="ORCA dimer direct correlation interaction",
            ),
            orca_dimer_direct_total_interaction_hartree=_finite(
                value["orca_dimer_direct_total_interaction_hartree"],
                name="ORCA dimer direct total interaction",
            ),
            final_ccsd_t_energy_hartree=_finite(
                value["final_ccsd_t_energy_hartree"],
                name="dimer final CCSD(T) energy",
            ),
            source_output_identity=dict(value.get("source_output_identity", {})),
        )


@dataclass(frozen=True)
class OrcaLEDFragmentReferenceQuantities:
    """Raw quantities from one frozen fragment in the complete dimer basis."""

    fragment_id: str
    reference_energy_e0_hartree: float
    corrected_correlation_energy_hartree: float
    triples_correction_hartree: float
    final_ccsd_t_energy_hartree: float
    orca_version: str = "6.1.1"
    reference_semantics: LEDReferenceSemantics = (
        LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS
    )
    source_output_identity: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        fragment_id = str(self.fragment_id).strip()
        if not fragment_id:
            raise OrcaLEDParseError("fragment reference requires an identifier")
        if self.orca_version != "6.1.1":
            raise UnsupportedOrcaLEDFormatError(
                "fragment reference requires the validated ORCA 6.1.1 grammar"
            )
        object.__setattr__(
            self, "reference_semantics", LEDReferenceSemantics(self.reference_semantics)
        )
        for name, value in (
            ("reference E(0)", self.reference_energy_e0_hartree),
            ("corrected correlation", self.corrected_correlation_energy_hartree),
            ("triples correction", self.triples_correction_hartree),
            ("final CCSD(T)", self.final_ccsd_t_energy_hartree),
        ):
            if not math.isfinite(value):
                raise OrcaLEDParseError(f"fragment {name} must be finite")
        object.__setattr__(self, "fragment_id", fragment_id)
        object.__setattr__(
            self, "source_output_identity", dict(self.source_output_identity)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "fragment_id": self.fragment_id,
            "orca_version": self.orca_version,
            "parser_grammar": ORCA_LED_FRAGMENT_REFERENCE_GRAMMAR,
            "reference_semantics": self.reference_semantics.value,
            "reference_energy_e0_hartree": self.reference_energy_e0_hartree,
            "corrected_correlation_energy_hartree": self.corrected_correlation_energy_hartree,
            "triples_correction_hartree": self.triples_correction_hartree,
            "final_ccsd_t_energy_hartree": self.final_ccsd_t_energy_hartree,
            "source_output_identity": dict(self.source_output_identity),
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any]
    ) -> "OrcaLEDFragmentReferenceQuantities":
        if value.get("parser_grammar") != ORCA_LED_FRAGMENT_REFERENCE_GRAMMAR:
            raise UnsupportedOrcaLEDFormatError("unknown stored fragment-reference grammar")
        return cls(
            fragment_id=str(value["fragment_id"]),
            reference_energy_e0_hartree=_finite(
                value["reference_energy_e0_hartree"], name="fragment reference E(0)"
            ),
            corrected_correlation_energy_hartree=_finite(
                value["corrected_correlation_energy_hartree"],
                name="fragment corrected correlation",
            ),
            triples_correction_hartree=_finite(
                value["triples_correction_hartree"], name="fragment triples"
            ),
            final_ccsd_t_energy_hartree=_finite(
                value["final_ccsd_t_energy_hartree"],
                name="fragment final CCSD(T) energy",
            ),
            orca_version=str(value["orca_version"]),
            reference_semantics=LEDReferenceSemantics(str(value["reference_semantics"])),
            source_output_identity=dict(value.get("source_output_identity", {})),
        )


@dataclass(frozen=True)
class IntermolecularLEDComponents:
    electronic_preparation_hartree: float
    electrostatics_hartree: float
    exchange_hartree: float
    non_dispersive_ccsd_correlation_hartree: float
    dispersion_hartree: float
    triples_hartree: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in self.values_hartree.values()):
            raise LEDReconstructionError("six-component LED result must be finite")

    @property
    def values_hartree(self) -> dict[str, float]:
        return {
            "electronic_preparation": self.electronic_preparation_hartree,
            "electrostatics": self.electrostatics_hartree,
            "exchange": self.exchange_hartree,
            "non_dispersive_ccsd_correlation": self.non_dispersive_ccsd_correlation_hartree,
            "dispersion": self.dispersion_hartree,
            "triples": self.triples_hartree,
        }

    @property
    def values_kj_mol(self) -> dict[str, float]:
        return {
            key: value * HARTREE_TO_KJ_MOL
            for key, value in self.values_hartree.items()
        }

    @property
    def reconstructed_total_hartree(self) -> float:
        return sum(self.values_hartree.values())

    @property
    def reconstructed_total_kj_mol(self) -> float:
        return self.reconstructed_total_hartree * HARTREE_TO_KJ_MOL

    def to_dict(self) -> dict[str, object]:
        return {
            "hartree": self.values_hartree,
            "kj_mol": self.values_kj_mol,
            "reconstructed_total_hartree": self.reconstructed_total_hartree,
            "reconstructed_total_kj_mol": self.reconstructed_total_kj_mol,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "IntermolecularLEDComponents":
        raw = value.get("hartree")
        if not isinstance(raw, Mapping):
            raise LEDReconstructionError("stored LED components lack Hartree values")
        return cls(
            electronic_preparation_hartree=_finite(
                raw["electronic_preparation"], name="electronic preparation"
            ),
            electrostatics_hartree=_finite(raw["electrostatics"], name="electrostatics"),
            exchange_hartree=_finite(raw["exchange"], name="exchange"),
            non_dispersive_ccsd_correlation_hartree=_finite(
                raw["non_dispersive_ccsd_correlation"],
                name="non-dispersive CCSD correlation",
            ),
            dispersion_hartree=_finite(raw["dispersion"], name="dispersion"),
            triples_hartree=_finite(raw["triples"], name="triples"),
        )


@dataclass(frozen=True)
class IntermolecularLEDResult:
    dimer: OrcaDimerLEDQuantities
    fragment_references: tuple[OrcaLEDFragmentReferenceQuantities, ...]
    components: IntermolecularLEDComponents
    independently_reconstructed_total_hartree: float
    reconstruction_residual_hartree: float
    reconstruction_tolerance_hartree: float
    reference_semantics: LEDReferenceSemantics = (
        LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS
    )

    def __post_init__(self) -> None:
        if len(self.fragment_references) != 2:
            raise LEDReconstructionError("six-component LED requires two references")
        if len({item.fragment_id for item in self.fragment_references}) != 2:
            raise LEDReconstructionError("fragment reference identities must be unique")
        object.__setattr__(
            self, "reference_semantics", LEDReferenceSemantics(self.reference_semantics)
        )
        tolerance = _finite(
            self.reconstruction_tolerance_hartree,
            name="LED reconstruction tolerance",
        )
        if tolerance <= 0:
            raise LEDReconstructionError("LED reconstruction tolerance must be positive")
        if abs(self.reconstruction_residual_hartree) > tolerance:
            raise LEDReconstructionError(
                "six-component reconstruction residual exceeds tolerance: "
                f"{self.reconstruction_residual_hartree:.12g} Eh > {tolerance:.12g} Eh"
            )

    @property
    def reconstructed_total_hartree(self) -> float:
        return self.components.reconstructed_total_hartree

    @property
    def reconstructed_total_kj_mol(self) -> float:
        return self.components.reconstructed_total_kj_mol

    @property
    def reconstruction_residual_kj_mol(self) -> float:
        return self.reconstruction_residual_hartree * HARTREE_TO_KJ_MOL

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": "intermolecular_six_component_v1",
            "reference_semantics": self.reference_semantics.value,
            "dimer_raw": self.dimer.to_dict(),
            "fragment_references_raw": [
                item.to_dict() for item in self.fragment_references
            ],
            "components": self.components.to_dict(),
            "independently_reconstructed_total_hartree": (
                self.independently_reconstructed_total_hartree
            ),
            "independently_reconstructed_total_kj_mol": (
                self.independently_reconstructed_total_hartree * HARTREE_TO_KJ_MOL
            ),
            "reconstruction_residual_hartree": self.reconstruction_residual_hartree,
            "reconstruction_residual_kj_mol": self.reconstruction_residual_kj_mol,
            "reconstruction_tolerance_hartree": self.reconstruction_tolerance_hartree,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "IntermolecularLEDResult":
        if value.get("contract") != "intermolecular_six_component_v1":
            raise LEDReconstructionError("unknown stored intermolecular LED contract")
        return cls(
            dimer=OrcaDimerLEDQuantities.from_mapping(dict(value["dimer_raw"])),
            fragment_references=tuple(
                OrcaLEDFragmentReferenceQuantities.from_mapping(item)
                for item in value["fragment_references_raw"]
            ),
            components=IntermolecularLEDComponents.from_mapping(
                dict(value["components"])
            ),
            independently_reconstructed_total_hartree=_finite(
                value["independently_reconstructed_total_hartree"],
                name="independent LED total",
            ),
            reconstruction_residual_hartree=_finite(
                value["reconstruction_residual_hartree"],
                name="LED reconstruction residual",
            ),
            reconstruction_tolerance_hartree=_finite(
                value["reconstruction_tolerance_hartree"],
                name="LED reconstruction tolerance",
            ),
            reference_semantics=LEDReferenceSemantics(
                str(value["reference_semantics"])
            ),
        )


def _extract_led_section(text: str) -> str:
    headers = list(LED_HEADER_RE.finditer(text))
    if len(headers) != 1:
        qualifier = "missing" if not headers else "ambiguous"
        raise OrcaLEDParseError(f"ORCA LED section is {qualifier}")
    end = LED_END_RE.search(text, headers[0].end())
    if end is None:
        raise OrcaLEDParseError("ORCA LED section is truncated")
    return text[headers[0].start() : end.end()]


def parse_orca_interfragment_led(
    text: str,
    *,
    source_path: str | Path | None = None,
    source_sha256: str | None = None,
    source_size_bytes: int | None = None,
) -> OrcaDimerLEDQuantities:
    """Parse the validated ORCA 6.1.1 two-fragment dimer LED grammar."""

    version = _validated_orca_611(text)
    section = _extract_led_section(text)
    fragment_rows = re.findall(r"^\s*Fragment\s+(\d+):\s+([0-9 ]+)\s*$", section, re.M)
    if len(fragment_rows) != 2 or {int(item[0]) for item in fragment_rows} != {1, 2}:
        raise OrcaLEDParseError("ORCA LED fragment partition is missing or ambiguous")
    partitions = tuple(
        OrcaLEDFragmentPartition(
            int(fragment_index), tuple(int(item) for item in atoms.split())
        )
        for fragment_index, atoms in sorted(fragment_rows, key=lambda item: int(item[0]))
    )
    intra_rows = re.findall(
        rf"INTRA-FRAGMENT REF\. ENERGY FOR FRAGMENT\s+(\d+)\s*\n-+.*?"
        rf"^Total energy\s*=\s*({NUMBER})\s*$",
        section,
        re.M | re.S,
    )
    if len(intra_rows) != 2 or {int(item[0]) for item in intra_rows} != {1, 2}:
        raise OrcaLEDParseError("intra-fragment reference energies are missing or ambiguous")
    intra = tuple(
        _number(value, name=f"fragment {index} intra-reference energy")
        for index, value in sorted(intra_rows, key=lambda item: int(item[0]))
    )

    correlation = re.search(
        r"INTER- vs INTRA-FRAGMENT CORRELATION ENERGIES \(Eh\)(.*?)"
        r"Sum of INTRA-fragment correlation energies",
        section,
        re.S,
    )
    total_interaction = re.search(
        r"INTER- vs INTRA-FRAGMENT TOTAL ENERGIES \(Eh\)(.*?)"
        r"Sum of INTRA-fragment total energies",
        section,
        re.S,
    )
    summary = re.search(
        r"FINAL SUMMARY DLPNO-CCSD ENERGY DECOMPOSITION \(Eh\)(.*?)"
        r"#Time for LED analysis",
        section,
        re.S,
    )
    if correlation is None or total_interaction is None or summary is None:
        raise OrcaLEDParseError("required ORCA LED numerical subsection is missing")
    corr = correlation.group(1)
    total = total_interaction.group(1)
    final = summary.group(1)
    source = _source_identity(
        text.encode("utf-8"),
        source_path=source_path,
        source_sha256=source_sha256,
        source_size_bytes=source_size_bytes,
        grammar=ORCA_INTERMOLECULAR_LED_GRAMMAR,
    )
    return OrcaDimerLEDQuantities(
        orca_version=version,
        fragment_partitions=partitions,
        intra_fragment_reference_energies_hartree=intra,
        electrostatics_reference_hartree=_single_number(
            rf"^\s*Electrostatics \(REF\.\)\s+({NUMBER})\s*$",
            final,
            name="electrostatics",
        ),
        exchange_reference_hartree=_single_number(
            rf"^\s*Exchange \(REF\.\)\s+({NUMBER})\s*$",
            final,
            name="exchange",
        ),
        dispersion_strong_pairs_hartree=_single_number(
            rf"^\s*Dispersion \(strong pairs\)\s+({NUMBER})\s*$",
            final,
            name="strong-pair dispersion",
        ),
        dispersion_weak_pairs_hartree=_single_number(
            rf"^\s*Dispersion \(weak pairs\)\s+({NUMBER})\s*$",
            final,
            name="weak-pair dispersion",
        ),
        non_dispersion_strong_pairs_hartree=_single_number(
            rf"^\s*Non dispersion \(strong pairs\)\s+({NUMBER})\s*$",
            final,
            name="strong-pair non-dispersion",
        ),
        non_dispersion_weak_pairs_hartree=_single_number(
            rf"^\s*Non dispersion \(weak pairs\)\s+({NUMBER})\s*$",
            final,
            name="weak-pair non-dispersion",
        ),
        dimer_triples_correction_hartree=_single_number(
            rf"^Triples Correction \(T\)\s+\.\.\.\s+({NUMBER})\s*$",
            text,
            name="dimer triples correction",
        ),
        interfragment_strong_pairs_hartree=_single_number(
            rf"^\s*Inter strong pairs\s+({NUMBER})\s+\(",
            corr,
            name="direct interfragment strong pairs",
        ),
        interfragment_triples_hartree=_single_number(
            rf"^\s*Inter triples\s+({NUMBER})\s+\(",
            corr,
            name="direct interfragment triples",
        ),
        interfragment_weak_pairs_hartree=_single_number(
            rf"^\s*Inter weak pairs\s+({NUMBER})\s+\(",
            corr,
            name="direct interfragment weak pairs",
        ),
        orca_dimer_direct_reference_interaction_hartree=_single_number(
            rf"^\s*Interfragment reference\s+({NUMBER})\s+\(",
            total,
            name="ORCA dimer direct reference interaction",
        ),
        orca_dimer_direct_correlation_interaction_hartree=_single_number(
            rf"^\s*Interfragment correlation\s+({NUMBER})\s+\(",
            total,
            name="ORCA dimer direct correlation interaction",
        ),
        orca_dimer_direct_total_interaction_hartree=_single_number(
            rf"^\s*Total interaction\s+({NUMBER})\s+\(",
            total,
            name="ORCA dimer direct total interaction",
        ),
        final_ccsd_t_energy_hartree=_single_number(
            rf"^\s*FINAL SINGLE POINT ENERGY\s+({NUMBER})\s*$",
            text,
            name="dimer final CCSD(T) energy",
        ),
        source_output_identity=source,
    )


def parse_orca_interfragment_led_file(path: str | Path) -> OrcaDimerLEDQuantities:
    selected = Path(path)
    payload = selected.read_bytes()
    return parse_orca_interfragment_led(
        payload.decode("utf-8"),
        source_path=selected,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        source_size_bytes=len(payload),
    )


def parse_orca_led_fragment_reference(
    text: str,
    *,
    fragment_id: str,
    source_path: str | Path | None = None,
    source_sha256: str | None = None,
    source_size_bytes: int | None = None,
) -> OrcaLEDFragmentReferenceQuantities:
    """Parse one ORCA 6.1.1 frozen-fragment-in-dimer-basis result."""

    version = _validated_orca_611(text)
    if LED_HEADER_RE.search(text):
        raise OrcaLEDParseError("LED fragment reference unexpectedly contains a LED section")
    source = _source_identity(
        text.encode("utf-8"),
        source_path=source_path,
        source_sha256=source_sha256,
        source_size_bytes=source_size_bytes,
        grammar=ORCA_LED_FRAGMENT_REFERENCE_GRAMMAR,
    )
    return OrcaLEDFragmentReferenceQuantities(
        fragment_id=fragment_id,
        reference_energy_e0_hartree=_unique_number(
            rf"^E\(0\)\s+\.\.\.\s+({NUMBER})\s*$",
            text,
            name="fragment reference E(0)",
        ),
        corrected_correlation_energy_hartree=_single_number(
            rf"^E\(CORR\)\(corrected\)\s+\.\.\.\s+({NUMBER})\s*$",
            text,
            name="fragment corrected correlation",
        ),
        triples_correction_hartree=_single_number(
            rf"^Triples Correction \(T\)\s+\.\.\.\s+({NUMBER})\s*$",
            text,
            name="fragment triples correction",
        ),
        final_ccsd_t_energy_hartree=_single_number(
            rf"^\s*FINAL SINGLE POINT ENERGY\s+({NUMBER})\s*$",
            text,
            name="fragment final CCSD(T) energy",
        ),
        orca_version=version,
        source_output_identity=source,
    )


def parse_orca_led_fragment_reference_file(
    path: str | Path, *, fragment_id: str
) -> OrcaLEDFragmentReferenceQuantities:
    selected = Path(path)
    payload = selected.read_bytes()
    return parse_orca_led_fragment_reference(
        payload.decode("utf-8"),
        fragment_id=fragment_id,
        source_path=selected,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        source_size_bytes=len(payload),
    )


def assemble_interfragment_led(
    dimer: OrcaDimerLEDQuantities,
    fragment_references: Sequence[OrcaLEDFragmentReferenceQuantities],
    *,
    reconstruction_tolerance_hartree: float = (
        DEFAULT_LED_RECONSTRUCTION_TOLERANCE_HARTREE
    ),
) -> IntermolecularLEDResult:
    """Assemble the historical six-component frozen-geometry LED convention."""

    references = tuple(fragment_references)
    if len(references) != 2:
        raise LEDReconstructionError("six-component LED requires exactly two references")
    if len({item.fragment_id for item in references}) != 2:
        raise LEDReconstructionError("fragment reference identities must be unique")
    if any(
        item.reference_semantics
        is not LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS
        for item in references
    ):
        raise LEDReconstructionError("fragment reference semantics are incompatible")
    components = IntermolecularLEDComponents(
        electronic_preparation_hartree=(
            sum(dimer.intra_fragment_reference_energies_hartree)
            - sum(item.reference_energy_e0_hartree for item in references)
        ),
        electrostatics_hartree=dimer.electrostatics_reference_hartree,
        exchange_hartree=dimer.exchange_reference_hartree,
        non_dispersive_ccsd_correlation_hartree=(
            dimer.non_dispersion_strong_pairs_hartree
            + dimer.non_dispersion_weak_pairs_hartree
            - sum(item.corrected_correlation_energy_hartree for item in references)
        ),
        dispersion_hartree=(
            dimer.dispersion_strong_pairs_hartree
            + dimer.dispersion_weak_pairs_hartree
        ),
        triples_hartree=(
            dimer.dimer_triples_correction_hartree
            - sum(item.triples_correction_hartree for item in references)
        ),
    )
    independent = dimer.final_ccsd_t_energy_hartree - sum(
        item.final_ccsd_t_energy_hartree for item in references
    )
    residual = components.reconstructed_total_hartree - independent
    return IntermolecularLEDResult(
        dimer=dimer,
        fragment_references=references,
        components=components,
        independently_reconstructed_total_hartree=independent,
        reconstruction_residual_hartree=residual,
        reconstruction_tolerance_hartree=reconstruction_tolerance_hartree,
    )


__all__ = [
    "DEFAULT_LED_RECONSTRUCTION_TOLERANCE_HARTREE",
    "IntermolecularLEDComponents",
    "IntermolecularLEDResult",
    "LEDReconstructionError",
    "LEDReferenceSemantics",
    "ORCA_INTERMOLECULAR_LED_GRAMMAR",
    "ORCA_INTERMOLECULAR_LED_PARSER_VERSION",
    "ORCA_LED_FRAGMENT_REFERENCE_GRAMMAR",
    "OrcaDimerLEDQuantities",
    "OrcaLEDError",
    "OrcaLEDFragmentPartition",
    "OrcaLEDFragmentReferenceQuantities",
    "OrcaLEDParseError",
    "UnsupportedOrcaLEDFormatError",
    "assemble_interfragment_led",
    "parse_orca_interfragment_led",
    "parse_orca_interfragment_led_file",
    "parse_orca_led_fragment_reference",
    "parse_orca_led_fragment_reference_file",
]
