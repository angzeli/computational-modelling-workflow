"""Scientific target, execution-attempt, artifact, and lineage identities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from .execution_contract import canonical_stage_type
from .provenance import stable_hash


JOB_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class JobTarget:
    """Scientific intent; operational resource choices are deliberately excluded."""

    stage_type: str
    geometry_sha256: str
    charge: int
    multiplicity: int
    calculation: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage_type", canonical_stage_type(self.stage_type))
        object.__setattr__(self, "calculation", dict(self.calculation))

    @property
    def target_id(self) -> str:
        return stable_hash({"schema_version": JOB_SCHEMA_VERSION, **asdict(self)})

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "target_id": self.target_id}


@dataclass(frozen=True)
class GeometryLineage:
    """Exact relationship between this target geometry and its validated parent."""

    source: str
    geometry_sha256: str
    parent_stage: str | None = None
    parent_target_id: str | None = None
    parent_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        parent_values = (self.parent_stage, self.parent_target_id, self.parent_artifact_sha256)
        if any(value is not None for value in parent_values) and not all(
            value is not None for value in parent_values
        ):
            raise ValueError("parent lineage requires stage, target identity, and artifact hash")
        if self.parent_artifact_sha256 and self.parent_artifact_sha256 != self.geometry_sha256:
            raise ValueError("child geometry identity must match the declared parent artifact")


@dataclass(frozen=True)
class ExecutionAttempt:
    """One operational realization of a scientific target."""

    attempt_id: str
    target_id: str
    created_at: str
    resources: Mapping[str, object]
    executable: Mapping[str, object]
    generated_input_sha256: str
    parent_attempt_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        target_id: str,
        resources: Mapping[str, object],
        executable: Mapping[str, object],
        generated_input_sha256: str,
        parent_attempt_id: str | None = None,
        attempt_id: str | None = None,
    ) -> "ExecutionAttempt":
        return cls(
            attempt_id=attempt_id or uuid4().hex,
            target_id=target_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            resources=dict(resources),
            executable=dict(executable),
            generated_input_sha256=generated_input_sha256,
            parent_attempt_id=parent_attempt_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
