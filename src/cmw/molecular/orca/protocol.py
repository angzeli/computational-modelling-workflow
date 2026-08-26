"""Method-aware ORCA protocol intent and fail-closed validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping

from .status import OrcaEvidence, StageType


class ProtocolValidationStatus(str, Enum):
    PASSED = "PASSED"
    FAILED_PROTOCOL_MISMATCH = "FAILED_PROTOCOL_MISMATCH"


def _boolean(value: object, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class ProtocolIntent:
    """Scientific method features that must be evidenced by ORCA output."""

    method: str | None = None
    basis: str | None = None
    auxiliary_basis: Mapping[str, str] = field(default_factory=dict)
    auto_auxiliary_basis: bool = False
    reference_approximation: str | None = None
    pno: str | None = None
    led: bool = False
    fragments_required: bool = False
    expected_fragments: int | None = None
    optimization_required: bool | None = None
    frequency_required: bool | None = None
    excited_state_required: bool | None = None
    minimum_excited_states: int | None = None

    def __post_init__(self) -> None:
        for name in ("method", "basis", "reference_approximation", "pno"):
            value = getattr(self, name)
            if isinstance(value, str):
                object.__setattr__(self, name, value.strip() or None)
        if self.expected_fragments is not None and self.expected_fragments < 1:
            raise ValueError("expected_fragments must be positive")
        if self.minimum_excited_states is not None and (
            isinstance(self.minimum_excited_states, bool)
            or not isinstance(self.minimum_excited_states, int)
            or self.minimum_excited_states < 1
        ):
            raise ValueError("minimum_excited_states must be a positive integer")
        object.__setattr__(self, "auxiliary_basis", dict(self.auxiliary_basis))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ProtocolIntent":
        raw = dict(value or {})
        pno = raw.get("pno", raw.get("pno_setting"))
        auxiliary = raw.get("auxiliary_basis", {})
        if not isinstance(auxiliary, Mapping):
            raise ValueError("auxiliary_basis must be a mapping")
        fragments_value = raw.get("fragments_required", raw.get("fragments", False))
        if isinstance(fragments_value, int) and not isinstance(fragments_value, bool):
            expected_fragments = fragments_value
            fragments_required = True
        else:
            fragments_required = _boolean(fragments_value, name="fragments_required")
            expected = raw.get("expected_fragments", raw.get("fragment_count"))
            expected_fragments = int(expected) if expected is not None else None
        minimum_raw = raw.get("minimum_excited_states")
        if isinstance(minimum_raw, bool):
            raise ValueError("minimum_excited_states must be a positive integer")
        minimum_excited_states = (
            int(minimum_raw) if minimum_raw is not None else None
        )
        return cls(
            method=str(raw["method"]) if raw.get("method") is not None else None,
            basis=str(raw["basis"]) if raw.get("basis") is not None else None,
            auxiliary_basis={str(key): str(item) for key, item in auxiliary.items()},
            auto_auxiliary_basis=_boolean(
                raw.get("auto_auxiliary_basis", False),
                name="auto_auxiliary_basis",
            ),
            reference_approximation=(
                str(raw["reference_approximation"])
                if raw.get("reference_approximation") is not None
                else None
            ),
            pno=str(pno) if pno is not None else None,
            led=_boolean(raw.get("led", False), name="led"),
            fragments_required=fragments_required,
            expected_fragments=expected_fragments,
            optimization_required=(
                _boolean(raw["optimization_required"], name="optimization_required")
                if raw.get("optimization_required") is not None
                else None
            ),
            frequency_required=(
                _boolean(raw["frequency_required"], name="frequency_required")
                if raw.get("frequency_required") is not None
                else None
            ),
            excited_state_required=(
                _boolean(raw["excited_state_required"], name="excited_state_required")
                if raw.get("excited_state_required") is not None
                else None
            ),
            minimum_excited_states=minimum_excited_states,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProtocolValidation:
    status: ProtocolValidationStatus
    checks: Mapping[str, bool]
    expected: Mapping[str, object]
    detected: Mapping[str, object]
    failures: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.status is ProtocolValidationStatus.PASSED

    @property
    def reason(self) -> str:
        return (
            "method-aware ORCA protocol validation passed"
            if self.valid
            else f"FAILED_PROTOCOL_MISMATCH: {', '.join(self.failures)}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "checks": dict(self.checks),
            "expected": dict(self.expected),
            "detected": dict(self.detected),
            "failures": list(self.failures),
            "reason": self.reason,
        }


def _normal(value: str) -> str:
    return "".join(value.casefold().split())


def _reported(expected: str, values: tuple[str, ...], keywords: tuple[str, ...]) -> bool:
    wanted = _normal(expected)
    candidates = {_normal(value) for value in (*values, *keywords)}
    return wanted in candidates


def validate_protocol(
    evidence: OrcaEvidence,
    intent: ProtocolIntent,
    stage_type: StageType,
) -> ProtocolValidation:
    """Compare declared scientific intent with factual ORCA output evidence."""

    checks: dict[str, bool] = {"scf_converged": evidence.scf_converged and not evidence.scf_failure_evidence}
    failures: list[str] = []

    if intent.method is not None:
        checks["method_match"] = _reported(
            intent.method, evidence.reported_methods, evidence.input_keyword_tokens
        )
    if intent.basis is not None:
        checks["basis_match"] = _reported(
            intent.basis, evidence.reported_basis_sets, evidence.input_keyword_tokens
        )
    for role, auxiliary_basis in intent.auxiliary_basis.items():
        checks[f"auxiliary_basis_{role}_match"] = _reported(
            auxiliary_basis,
            evidence.reported_basis_sets,
            evidence.input_keyword_tokens,
        )
    if intent.auto_auxiliary_basis:
        checks["auto_auxiliary_basis_match"] = _reported(
            "AutoAux", (), evidence.input_keyword_tokens
        )
    if intent.reference_approximation is not None:
        checks["reference_approximation_match"] = _reported(
            intent.reference_approximation, (), evidence.input_keyword_tokens
        )
    if intent.pno is not None:
        checks["pno_match"] = _reported(
            intent.pno, evidence.reported_pno_settings, evidence.input_keyword_tokens
        )

    require_optimization = (
        stage_type is StageType.OPT
        if intent.optimization_required is None
        else intent.optimization_required
    )
    require_frequency = (
        stage_type is StageType.FREQ
        if intent.frequency_required is None
        else intent.frequency_required
    )
    require_excited_states = (
        stage_type is StageType.TDDFT
        if intent.excited_state_required is None
        else intent.excited_state_required
    )
    if require_optimization:
        checks["optimization_converged"] = evidence.optimization_converged
    if require_frequency:
        checks["frequency_completed"] = evidence.frequency_analysis_completed
    if require_excited_states:
        checks["excited_state_completed"] = evidence.excited_state_analysis_completed
    if intent.minimum_excited_states is not None:
        checks["excited_state_count"] = (
            evidence.excited_state_count is not None
            and evidence.excited_state_count >= intent.minimum_excited_states
        )
    if intent.led:
        checks["led_present"] = evidence.led_present
    if intent.fragments_required or intent.expected_fragments is not None:
        checks["fragments_present"] = evidence.fragment_count is not None
        if intent.expected_fragments is not None:
            checks["fragment_count_match"] = evidence.fragment_count == intent.expected_fragments

    failures.extend(name for name, passed in checks.items() if not passed)
    status = (
        ProtocolValidationStatus.PASSED
        if not failures
        else ProtocolValidationStatus.FAILED_PROTOCOL_MISMATCH
    )
    return ProtocolValidation(
        status,
        checks,
        intent.to_dict(),
        {
            "methods": list(evidence.reported_methods),
            "basis_sets": list(evidence.reported_basis_sets),
            "pno_settings": list(evidence.reported_pno_settings),
            "input_keyword_tokens": list(evidence.input_keyword_tokens),
            "led_present": evidence.led_present,
            "fragment_count": evidence.fragment_count,
            "scf_converged": evidence.scf_converged,
            "optimization_converged": evidence.optimization_converged,
            "frequency_analysis_completed": evidence.frequency_analysis_completed,
            "excited_state_analysis_completed": (
                evidence.excited_state_analysis_completed
            ),
            "excited_state_count": evidence.excited_state_count,
        },
        tuple(failures),
    )


__all__ = [
    "ProtocolIntent",
    "ProtocolValidation",
    "ProtocolValidationStatus",
    "validate_protocol",
]
