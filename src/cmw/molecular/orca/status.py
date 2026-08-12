"""Factual ORCA output evidence and separate stage validation policy."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
FINAL_ENERGY_RE = re.compile(rf"^\s*FINAL SINGLE POINT ENERGY\s+({NUMBER})\s*$", re.M)
SCF_CONVERGED_RE = re.compile(r"SCF\s+CONVERGED\s+AFTER\s+(\d+)\s+CYCLES?", re.I)
SCF_FAILURE_RE = re.compile(
    r"SCF\s+(?:NOT|DID\s+NOT)\s+CONVERGE|SCF\s+CONVERGENCE\s+FAIL|SCF\s+FAILED",
    re.I,
)
VERSION_RE = re.compile(r"Program Version\s+([0-9]+(?:\.[0-9]+)+)", re.I)
RUNTIME_RE = re.compile(
    rf"TOTAL RUN TIME:\s+(\d+)\s+days\s+(\d+)\s+hours\s+"
    rf"(\d+)\s+minutes\s+({NUMBER})\s+seconds",
    re.I,
)
FREQUENCY_RE = re.compile(rf"^\s*\d+:\s+({NUMBER})\s+cm\*\*-1", re.M)
CHARGE_RE = re.compile(r"^\s*(?:Total Charge|Charge).*?\.\.+\s*(-?\d+)\s*$", re.I | re.M)
MULTIPLICITY_RE = re.compile(r"^\s*Multiplicity.*?\.\.+\s*(\d+)\s*$", re.I | re.M)
MEMORY_RE = re.compile(rf"Memory available(?:\s+for [^:\n]+)?:\s*({NUMBER})\s*MB", re.I)
FATAL_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"ORCA finished by error termination",
        r"ORCA TERMINATED ABNORMALLY",
        r"ABORTING THE RUN",
        r"FATAL ERROR",
        r"std::bad_alloc",
        r"cannot allocate memory",
        r"out of memory",
        r"MPI_ABORT",
    )
)


class StageType(str, Enum):
    OPT = "OPT"
    FREQ = "FREQ"
    SP = "SP"


class ExecutionStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"


class ScientificStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    NOT_CONVERGED = "NOT_CONVERGED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OrcaEvidence:
    """Facts recoverable from text, without a stage-specific verdict."""

    normal_termination: bool
    final_energies_hartree: tuple[float, ...]
    scf_converged: bool
    scf_cycles: int | None
    scf_failure_evidence: bool
    optimization_converged: bool
    frequency_analysis_completed: bool
    frequencies_cm1: tuple[float, ...]
    charge: int | None
    multiplicity: int | None
    orca_version: str | None
    runtime_seconds: float | None
    memory_available_mb: float | None
    warnings: tuple[str, ...]
    fatal_evidence: tuple[str, ...]

    @property
    def final_energy_hartree(self) -> float | None:
        return self.final_energies_hartree[-1] if self.final_energies_hartree else None

    @property
    def imaginary_frequencies_cm1(self) -> tuple[float, ...]:
        return tuple(value for value in self.frequencies_cm1 if value < 0.0)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["final_energy_hartree"] = self.final_energy_hartree
        result["imaginary_frequencies_cm1"] = self.imaginary_frequencies_cm1
        return result


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    reason: str
    normal_termination: bool
    process_running: bool
    process_exit_code: int | None


@dataclass(frozen=True)
class FrequencyPolicy:
    """Explicit minimum policy; negative-mode tolerance is in cm⁻¹."""

    require_minimum: bool = False
    imaginary_tolerance_cm1: float = 0.0

    def __post_init__(self) -> None:
        if self.imaginary_tolerance_cm1 < 0 or not math.isfinite(self.imaginary_tolerance_cm1):
            raise ValueError("imaginary-frequency tolerance must be finite and non-negative")


@dataclass(frozen=True)
class ScientificResult:
    status: ScientificStatus
    stage_type: StageType
    reason: str
    imaginary_frequencies_cm1: tuple[float, ...] = ()
    significant_imaginary_frequencies_cm1: tuple[float, ...] = ()

    @property
    def valid(self) -> bool:
        return self.status is ScientificStatus.VALID


def _number(value: str) -> float:
    return float(value.replace("D", "E").replace("d", "e"))


def parse_orca_output(text: str, *, stderr_text: str = "") -> OrcaEvidence:
    """Extract factual evidence from possibly partial or redacted ORCA text."""

    combined = "\n".join(part for part in (text, stderr_text) if part)
    energies = tuple(_number(token) for token in FINAL_ENERGY_RE.findall(text))
    frequencies = tuple(_number(token) for token in FREQUENCY_RE.findall(text))
    if not all(math.isfinite(value) for value in (*energies, *frequencies)):
        raise ValueError("ORCA output contains non-finite numeric evidence")
    scf_cycles = tuple(int(value) for value in SCF_CONVERGED_RE.findall(text))
    runtime_matches = list(RUNTIME_RE.finditer(text))
    runtime_seconds = None
    if runtime_matches:
        days, hours, minutes, seconds = runtime_matches[-1].groups()
        runtime_seconds = (
            int(days) * 86_400 + int(hours) * 3_600 + int(minutes) * 60 + _number(seconds)
        )
    fatal = tuple(
        line.strip()
        for line in combined.splitlines()
        if line.strip() and any(pattern.search(line) for pattern in FATAL_PATTERNS)
    )
    warning_lines = tuple(
        line.strip()
        for line in combined.splitlines()
        if re.search(r"(^|[\s*])WARNING([\s:!*]|$)", line, re.I)
    )
    version = VERSION_RE.search(text)
    charge = CHARGE_RE.findall(text)
    multiplicity = MULTIPLICITY_RE.findall(text)
    memory = MEMORY_RE.findall(combined)
    return OrcaEvidence(
        normal_termination="ORCA TERMINATED NORMALLY" in text,
        final_energies_hartree=energies,
        scf_converged=bool(scf_cycles),
        scf_cycles=scf_cycles[-1] if scf_cycles else None,
        scf_failure_evidence=bool(SCF_FAILURE_RE.search(text)),
        optimization_converged="THE OPTIMIZATION HAS CONVERGED" in text,
        frequency_analysis_completed=bool(
            re.search(r"VIBRATIONAL FREQUENCIES|THERMOCHEMISTRY AT", text, re.I)
            and frequencies
        ),
        frequencies_cm1=frequencies,
        charge=int(charge[-1]) if charge else None,
        multiplicity=int(multiplicity[-1]) if multiplicity else None,
        orca_version=version.group(1) if version else None,
        runtime_seconds=runtime_seconds,
        memory_available_mb=_number(memory[-1]) if memory else None,
        warnings=warning_lines,
        fatal_evidence=fatal,
    )


def read_orca_output(path: Path, *, stderr_path: Path | None = None) -> OrcaEvidence:
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    stderr = (
        stderr_path.read_text(encoding="utf-8", errors="replace")
        if stderr_path and stderr_path.is_file()
        else ""
    )
    return parse_orca_output(text, stderr_text=stderr)


def classify_execution(
    evidence: OrcaEvidence,
    *,
    process_running: bool = False,
    process_exit_code: int | None = None,
) -> ExecutionResult:
    """Classify process/program completion without asserting scientific validity."""

    if process_running:
        status, reason = ExecutionStatus.RUNNING, "ORCA process is still running"
    elif evidence.normal_termination and process_exit_code in (None, 0):
        status, reason = ExecutionStatus.SUCCESS, "ORCA terminated normally"
    elif evidence.fatal_evidence:
        status, reason = ExecutionStatus.FAILED, f"fatal evidence: {evidence.fatal_evidence[0]}"
    elif process_exit_code not in (None, 0):
        status, reason = ExecutionStatus.FAILED, f"ORCA exited with status {process_exit_code}"
    else:
        status, reason = ExecutionStatus.INCOMPLETE, "normal termination evidence is absent"
    return ExecutionResult(status, reason, evidence.normal_termination, process_running, process_exit_code)


def validate_stage(
    evidence: OrcaEvidence,
    stage_type: StageType,
    *,
    execution: ExecutionResult | None = None,
    frequency_policy: FrequencyPolicy = FrequencyPolicy(),
) -> ScientificResult:
    """Apply explicit OPT/FREQ/SP policy after factual evidence parsing."""

    if execution is not None and execution.status is not ExecutionStatus.SUCCESS:
        return ScientificResult(
            ScientificStatus.UNKNOWN,
            stage_type,
            "scientific validity requires successful ORCA execution",
        )
    if not evidence.normal_termination:
        return ScientificResult(
            ScientificStatus.UNKNOWN,
            stage_type,
            "scientific validity requires normal ORCA termination",
        )
    if evidence.fatal_evidence:
        return ScientificResult(ScientificStatus.INVALID, stage_type, "fatal evidence is present")
    if not evidence.scf_converged or evidence.scf_failure_evidence:
        return ScientificResult(
            ScientificStatus.NOT_CONVERGED, stage_type, "explicit SCF convergence is absent or contradicted"
        )
    if evidence.final_energy_hartree is None:
        return ScientificResult(ScientificStatus.INVALID, stage_type, "final electronic energy is absent")

    if stage_type is StageType.OPT:
        if not evidence.optimization_converged:
            return ScientificResult(
                ScientificStatus.NOT_CONVERGED,
                stage_type,
                "optimization convergence evidence is absent",
            )
        return ScientificResult(ScientificStatus.VALID, stage_type, "OPT evidence is complete")

    if stage_type is StageType.FREQ:
        if not evidence.frequency_analysis_completed:
            return ScientificResult(
                ScientificStatus.INVALID, stage_type, "frequency analysis evidence is incomplete"
            )
        imaginary = evidence.imaginary_frequencies_cm1
        significant = tuple(
            value for value in imaginary if abs(value) > frequency_policy.imaginary_tolerance_cm1
        )
        if frequency_policy.require_minimum and significant:
            return ScientificResult(
                ScientificStatus.INVALID,
                stage_type,
                "significant imaginary frequencies violate the configured minimum policy",
                imaginary,
                significant,
            )
        return ScientificResult(
            ScientificStatus.VALID,
            stage_type,
            "FREQ evidence satisfies the configured policy",
            imaginary,
            significant,
        )

    return ScientificResult(ScientificStatus.VALID, stage_type, "SP evidence is complete")
