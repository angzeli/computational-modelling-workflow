"""Native ORCA 6.1 closed-shell two-fragment HFLD/LED energies (Hartree).

Single-fragment references are RHF, in their own basis at complex geometry.
The intermediate CCSD summary is not the HFLD energy. Only the final native
inter-fragment dispersion is added to HF; Singles is a quality diagnostic.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Sequence

CONTRACT = "native_hfld_led_rhf_own_basis_v1"
REFERENCE_SEMANTICS = "RHF_OWN_BASIS_AT_COMPLEX_GEOMETRY"
NUMBER = r"([-+]?\d+\.\d+(?:[EeDd][-+]?\d+)?)"


class HFLDError(ValueError):
    """Native evidence is missing, ambiguous or inconsistent."""


def _one(pattern: str, text: str) -> float:
    matches = re.findall(pattern, text, re.M)
    if len(matches) != 1:
        raise HFLDError(f"expected one native field: {pattern}; found {len(matches)}")
    value = float(matches[0].replace("D", "E").replace("d", "e"))
    if not math.isfinite(value):
        raise HFLDError("nonfinite native energy")
    return value


@dataclass(frozen=True)
class HFLDEnergies:
    total: float
    hf: float
    dispersion: float
    intra_hf: tuple[float, float]
    electrostatics: float
    exchange: float
    singles: float
    led_hf: float
    hf_led_numerical_difference: float
    closure_residual: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_hfld(text: str) -> HFLDEnergies:
    """Require native HFLD runtime evidence and independently check closure."""
    if text.count("ORCA TERMINATED NORMALLY") != 1:
        raise HFLDError("one normally terminated calculation is required")
    for marker in ("Intrafragment pairs 1 <-> 1 neglected for CCSD",
                   "Intrafragment pairs 2 <-> 2 neglected for CCSD",
                   "Entering RIJCOSX section of LED"):
        if marker not in text:
            raise HFLDError(f"missing native HFLD evidence: {marker}")
    if not re.search(r"\|\s*\d+>\s*!\s+HFLD\b", text, re.I):
        raise HFLDError("missing HFLD input echo")
    summary = text.split("FINAL SUMMARY DLPNO-CCSD ENERGY DECOMPOSITION (Eh)")
    if len(summary) != 2:
        raise HFLDError("expected one native LED summary")
    summary = summary[1].split("#Time for LED analysis")[0]
    indices = re.findall(r"^Intra fragment\s+(\d+) \(REF\.\)", summary, re.M)
    if indices != ["1", "2"]:
        raise HFLDError("exactly fragments 1 and 2 are required")
    intra = tuple(_one(rf"^Intra fragment\s+{i} \(REF\.\)\s+{NUMBER}\s*$", summary) for i in (1, 2))
    electro = _one(r"^Electrostatics \(REF\.\)\s+" + NUMBER + r"\s*$", summary)
    exchange = _one(r"^Exchange \(REF\.\)\s+" + NUMBER + r"\s*$", summary)
    dispersion = _one(r"^Inter-fragment dispersion\s+" + NUMBER + r"\s*$", text)
    pair_dispersion = sum(_one(rf"^Dispersion \({kind} pairs\)\s+{NUMBER}\s*$", summary) for kind in ("strong", "weak"))
    singles = _one(r"^Singles energy\s+" + NUMBER + r"\s*$", text)
    hf = _one(r"^Total Energy\s*:\s*" + NUMBER + r"\s+Eh", text)
    total = _one(r"^FINAL SINGLE POINT ENERGY\s+" + NUMBER + r"\s*$", text)
    reference = text.split("REFERENCE ENERGY E(0) DECOMPOSITION (Eh)")
    if len(reference) != 2:
        raise HFLDError("missing HF LED decomposition")
    led_hf = _one(r"^Total energy\s*=\s*" + NUMBER + r"\s*$", reference[1].split("Consistency check")[0])
    residual = total - hf - dispersion
    if abs(singles) >= 1e-6:
        raise HFLDError("Singles diagnostic exceeds 1e-6 Hartree")
    if abs(residual) > 5e-9 or abs(pair_dispersion - dispersion) > 5e-9:
        raise HFLDError("native HFLD dispersion/total closure failed")
    if abs(sum(intra) + electro + exchange - led_hf) > 5e-9:
        raise HFLDError("HF LED fragment decomposition closure failed")
    difference = hf - led_hf
    # COSX recomputation can differ from the final SCF quadrature. Report it
    # separately, never reinterpret it as correlation or electronic preparation.
    if abs(difference) > 1e-5:
        raise HFLDError("HF versus LED numerical difference exceeds 1e-5 Hartree")
    return HFLDEnergies(total, hf, dispersion, intra, electro, exchange,
                        singles, led_hf, difference, residual)


def parse_rhf_reference(text: str) -> float:
    if text.count("ORCA TERMINATED NORMALLY") != 1 or not re.search(r"\|\s*\d+>\s*!\s+RHF\b", text, re.I):
        raise HFLDError("normally terminated explicit RHF reference is required")
    if "Inter-fragment dispersion" in text:
        raise HFLDError("HFLD supersystem cannot be a single-fragment RHF reference")
    total = _one(r"^FINAL SINGLE POINT ENERGY\s+" + NUMBER + r"\s*$", text)
    hf = _one(r"^Total Energy\s*:\s*" + NUMBER + r"\s+Eh", text)
    if abs(total - hf) > 5e-9:
        raise HFLDError("RHF reference energy closure failed")
    return total


def assemble_hfld(dimer: HFLDEnergies, references: Sequence[float]) -> dict[str, float]:
    if len(references) != 2 or not all(math.isfinite(x) for x in references):
        raise HFLDError("two finite RHF reference energies are required")
    reference_sum = sum(references)
    result = {
        "interaction": dimer.total - reference_sum,
        "hf_interaction": dimer.hf - reference_sum,
        "electronic_preparation": sum(dimer.intra_hf) - reference_sum,
        "electrostatics": dimer.electrostatics,
        "exchange": dimer.exchange,
        "dispersion": dimer.dispersion,
        "hf_led_numerical_difference": dimer.hf_led_numerical_difference,
        "singles_diagnostic": dimer.singles,
    }
    reconstructed = sum(result[k] for k in ("electronic_preparation", "electrostatics", "exchange", "dispersion", "hf_led_numerical_difference"))
    result["closure_residual"] = result["interaction"] - reconstructed
    if abs(result["closure_residual"]) > 1e-8:
        raise HFLDError("HFLD interaction decomposition closure failed")
    return result
