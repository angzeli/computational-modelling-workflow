"""Program-independent execution intent shared by planners and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ComputationalTask(str, Enum):
    """Canonical computational tasks whose behavior must be rendered explicitly."""

    OPTIMIZATION = "optimization"
    FREQUENCY = "frequency"
    SINGLE_POINT = "single_point"
    EXCITED_STATE = "excited_state"


class ExecutionContractError(ValueError):
    """Raised when planned task, rendered behavior, and result type disagree."""

    code = "FAILED_EXECUTION_CONTRACT_MISMATCH"


def canonical_stage_type(value: object) -> str:
    """Return one stable token for the canonical CMW molecular stages.

    Enum members, their qualified string representation, and case variants are
    accepted at API boundaries. Program-specific stages outside this small
    canonical set are preserved verbatim.
    """

    raw = value.value if isinstance(value, Enum) else str(value)
    token = str(raw).strip()
    if token.casefold().startswith("stagetype."):
        token = token.split(".", 1)[1]
    aliases = {
        "opt": "OPT",
        "freq": "FREQ",
        "sp": "SP",
        "tddft": "TDDFT",
    }
    return aliases.get(token.casefold(), token)


@dataclass(frozen=True)
class ExecutionIntent:
    """Explicit bridge from a scientific stage to program execution behavior."""

    stage_type: str
    task: ComputationalTask
    required_behavior: str

    def __post_init__(self) -> None:
        stage_type = canonical_stage_type(self.stage_type)
        if not stage_type:
            raise ValueError("execution intent requires a scientific stage type")
        if not self.required_behavior.strip():
            raise ValueError("execution intent requires an execution behavior")
        object.__setattr__(self, "task", ComputationalTask(self.task))
        object.__setattr__(self, "stage_type", stage_type)

    def to_dict(self) -> dict[str, str]:
        return {
            "stage_type": self.stage_type,
            "task": self.task.value,
            "required_behavior": self.required_behavior,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutionIntent":
        return cls(
            stage_type=str(value["stage_type"]),
            task=ComputationalTask(str(value["task"])),
            required_behavior=str(value["required_behavior"]),
        )


__all__ = [
    "ComputationalTask",
    "ExecutionContractError",
    "ExecutionIntent",
    "canonical_stage_type",
]
