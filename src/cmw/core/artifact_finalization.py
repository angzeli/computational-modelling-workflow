"""Fail-closed batch finalization for planned scientific artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from .artifacts import (
    Artifact,
    ArtifactCompatibilityError,
    ArtifactValidation,
    validate_artifact_compatibility,
)


@dataclass(frozen=True)
class ArtifactFinalizationEvidence:
    """Execution-supplied evidence used to promote one planned artifact."""

    validation: ArtifactValidation
    files: Mapping[str, str] = field(default_factory=dict)
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", dict(self.files))
        object.__setattr__(self, "provenance", dict(self.provenance))


@dataclass(frozen=True)
class ArtifactFinalizationFailure:
    artifact_id: str
    code: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "code": self.code,
            "reason": self.reason,
        }


class ArtifactBundleFinalizationError(ValueError):
    """Raised when a planned artifact set cannot be finalized as one bundle."""

    def __init__(
        self,
        failures: Sequence[ArtifactFinalizationFailure],
        partial_artifacts: Sequence[Artifact] = (),
    ) -> None:
        self.failures = tuple(failures)
        self.partial_artifacts = tuple(partial_artifacts)
        summary = "; ".join(
            f"{failure.artifact_id}: {failure.code}: {failure.reason}"
            for failure in self.failures
        )
        super().__init__(summary or "artifact bundle finalization failed")


@dataclass(frozen=True)
class FinalizedArtifactBundle:
    """A dependency-ordered, compatibility-validated artifact collection."""

    artifacts: tuple[Artifact, ...]

    @property
    def by_id(self) -> dict[str, Artifact]:
        return {artifact.artifact_id: artifact for artifact in self.artifacts}

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "FINALIZED",
            "artifact_count": len(self.artifacts),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


def _duplicates(artifacts: Sequence[Artifact]) -> set[str]:
    seen: set[str] = set()
    duplicate: set[str] = set()
    for artifact in artifacts:
        if artifact.artifact_id in seen:
            duplicate.add(artifact.artifact_id)
        seen.add(artifact.artifact_id)
    return duplicate


def finalize_artifact_bundle(
    planned_artifacts: Sequence[Artifact],
    evidence: Mapping[str, ArtifactFinalizationEvidence],
    *,
    external_artifacts: Sequence[Artifact] = (),
) -> FinalizedArtifactBundle:
    """Finalize a planned artifact set without executing or inventing results.

    Validation decisions, output files, and runtime provenance must be supplied
    explicitly as evidence. Scientific identity fields and parent declarations
    are retained from the planned artifacts.
    """

    planned = tuple(planned_artifacts)
    external = tuple(external_artifacts)
    failures: list[ArtifactFinalizationFailure] = []
    duplicate_ids = _duplicates((*planned, *external))
    failures.extend(
        ArtifactFinalizationFailure(
            artifact_id, "DUPLICATE_ARTIFACT", "artifact identity is not unique"
        )
        for artifact_id in sorted(duplicate_ids)
    )

    planned_by_id = {artifact.artifact_id: artifact for artifact in planned}
    external_by_id = {artifact.artifact_id: artifact for artifact in external}
    known_ids = set(planned_by_id) | set(external_by_id)
    unknown_evidence = sorted(set(evidence) - set(planned_by_id))
    failures.extend(
        ArtifactFinalizationFailure(
            artifact_id,
            "UNKNOWN_ARTIFACT",
            "finalization evidence does not match a planned artifact",
        )
        for artifact_id in unknown_evidence
    )

    for artifact in planned:
        artifact_evidence = evidence.get(artifact.artifact_id)
        if artifact_evidence is None:
            failures.append(
                ArtifactFinalizationFailure(
                    artifact.artifact_id,
                    "MISSING_FINALIZATION_EVIDENCE",
                    "no validation evidence was supplied",
                )
            )
        elif not artifact_evidence.validation.passed:
            failures.append(
                ArtifactFinalizationFailure(
                    artifact.artifact_id,
                    artifact_evidence.validation.code or "FAILED_VALIDATION",
                    artifact_evidence.validation.reason,
                )
            )
        for parent_id in artifact.parent_artifacts:
            if parent_id not in known_ids:
                failures.append(
                    ArtifactFinalizationFailure(
                        artifact.artifact_id,
                        "MISSING_PARENT",
                        f"declared parent artifact is unavailable: {parent_id}",
                    )
                )

    for artifact in external:
        if not artifact.validation.passed:
            failures.append(
                ArtifactFinalizationFailure(
                    artifact.artifact_id,
                    "INVALID_EXTERNAL_PARENT",
                    "external parent artifact has not passed validation",
                )
            )

    if failures:
        raise ArtifactBundleFinalizationError(failures)

    available = dict(external_by_id)
    remaining = dict(planned_by_id)
    finalized: list[Artifact] = []
    while remaining:
        progressed = False
        for artifact_id, artifact in tuple(remaining.items()):
            if not all(
                parent_id in available for parent_id in artifact.parent_artifacts
            ):
                continue
            supplied = evidence[artifact_id]
            candidate = replace(
                artifact,
                files={**dict(artifact.files), **dict(supplied.files)},
                validation=supplied.validation,
                provenance={
                    **dict(artifact.provenance),
                    **dict(supplied.provenance),
                },
            )
            try:
                validate_artifact_compatibility(
                    candidate,
                    tuple(available[parent] for parent in candidate.parent_artifacts),
                )
            except ArtifactCompatibilityError as exc:
                failures.append(
                    ArtifactFinalizationFailure(
                        artifact_id,
                        ArtifactCompatibilityError.code,
                        str(exc),
                    )
                )
                del remaining[artifact_id]
                progressed = True
                continue
            available[artifact_id] = candidate
            finalized.append(candidate)
            del remaining[artifact_id]
            progressed = True
        if failures:
            break
        if not progressed:
            failures.extend(
                ArtifactFinalizationFailure(
                    artifact_id,
                    "UNRESOLVED_DEPENDENCY",
                    "artifact dependencies could not be resolved",
                )
                for artifact_id in remaining
            )
            break

    if failures:
        raise ArtifactBundleFinalizationError(failures, finalized)
    return FinalizedArtifactBundle(tuple(finalized))


__all__ = [
    "ArtifactBundleFinalizationError",
    "ArtifactFinalizationEvidence",
    "ArtifactFinalizationFailure",
    "FinalizedArtifactBundle",
    "finalize_artifact_bundle",
]
