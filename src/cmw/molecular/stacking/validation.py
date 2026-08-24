"""Cross-branch validation for planned vertical stacking workflows."""

from __future__ import annotations

from typing import Mapping, Sequence

from cmw.core.artifacts import Artifact, artifact_matches_type
from cmw.core.workflow_graph import WorkflowGraph


class StackingWorkflowValidationError(ValueError):
    """Raised when a stacking plan has an orphan or incompatible contract."""

    code = "FAILED_STACKING_WORKFLOW_VALIDATION"


def validate_stacking_artifact_lineage(
    graph: WorkflowGraph,
    artifacts_by_node: Mapping[str, Sequence[Artifact]],
) -> None:
    """Validate declared output types and parent identities without executing nodes."""

    unknown_nodes = set(artifacts_by_node) - set(graph.node_map)
    if unknown_nodes:
        raise StackingWorkflowValidationError(
            "artifact templates reference unknown nodes: "
            + ", ".join(sorted(unknown_nodes))
        )
    by_id: dict[str, tuple[str, Artifact]] = {}
    for node_id, artifacts in artifacts_by_node.items():
        node = graph.node_map[node_id]
        for artifact in artifacts:
            if artifact.artifact_id in by_id:
                raise StackingWorkflowValidationError(
                    f"duplicate planned artifact identity: {artifact.artifact_id}"
                )
            if not any(
                artifact_matches_type(artifact, expected)
                for expected in node.produces
            ):
                raise StackingWorkflowValidationError(
                    f"node {node_id!r} does not declare {artifact.artifact_type}"
                )
            by_id[artifact.artifact_id] = (node_id, artifact)

    children: dict[str, set[str]] = {node_id: set() for node_id in graph.node_map}
    ancestors: dict[str, set[str]] = {node_id: set() for node_id in graph.node_map}
    for node in graph.nodes:
        for dependency in node.dependencies:
            children[dependency].add(node.node_id)
    for node_id in graph.topological_order():
        node = graph.node_map[node_id]
        for dependency in node.dependencies:
            ancestors[node_id].add(dependency)
            ancestors[node_id].update(ancestors[dependency])

    for artifact_id, (node_id, artifact) in by_id.items():
        for parent_id in artifact.parent_artifacts:
            parent = by_id.get(parent_id)
            if parent is None:
                raise StackingWorkflowValidationError(
                    f"artifact {artifact_id} has missing parent {parent_id}"
                )
            parent_node = parent[0]
            if parent_node == node_id:
                continue
            if parent_node not in ancestors[node_id]:
                raise StackingWorkflowValidationError(
                    f"artifact parent from {parent_node!r} is not upstream of {node_id!r}"
                )

    referenced = {
        parent_id
        for _, artifact in by_id.values()
        for parent_id in artifact.parent_artifacts
    }
    terminal_types = {
        "OptimizationArtifact",
        "FrequencyArtifact",
        "SinglePointArtifact",
        "WavefunctionArtifact",
        "ExcitedStateArtifact",
        "NTOArtifact",
        "HoleElectronArtifact",
    }
    orphans = sorted(
        artifact_id
        for artifact_id, (node_id, artifact) in by_id.items()
        if artifact_id not in referenced
        and artifact.artifact_type not in terminal_types
        and children[node_id]
    )
    if orphans:
        raise StackingWorkflowValidationError(
            "planned workflow contains orphan artifacts: " + ", ".join(orphans)
        )


__all__ = [
    "StackingWorkflowValidationError",
    "validate_stacking_artifact_lineage",
]
