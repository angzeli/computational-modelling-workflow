"""System-agnostic workflow graph validation and artifact readiness checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .artifacts import ARTIFACT_TYPES, Artifact, artifact_matches_type


WORKFLOW_GRAPH_SCHEMA_VERSION = 1


def _declared_artifact_matches(candidate: str, expected: str) -> bool:
    candidate_type = ARTIFACT_TYPES.get(candidate)
    expected_type = ARTIFACT_TYPES.get(expected)
    if candidate_type is not None and expected_type is not None:
        return issubclass(candidate_type, expected_type)
    return candidate == expected


class NodeKind(str, Enum):
    CALCULATION = "calculation"
    AGGREGATION = "aggregation"
    DERIVED = "derived"


@dataclass(frozen=True)
class ArtifactRequirement:
    artifact_type: str
    count: int = 1
    from_nodes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.artifact_type:
            raise ValueError("artifact requirement type is required")
        if self.count < 1:
            raise ValueError("artifact requirement count must be positive")
        if len(set(self.from_nodes)) != len(self.from_nodes):
            raise ValueError("artifact requirement from_nodes must be unique")
        object.__setattr__(self, "from_nodes", tuple(self.from_nodes))

    @classmethod
    def from_value(cls, value: object) -> "ArtifactRequirement":
        if isinstance(value, str):
            return cls(value)
        if not isinstance(value, Mapping):
            raise ValueError("artifact requirements must be strings or objects")
        artifact_type = value.get("artifact_type", value.get("type"))
        if not isinstance(artifact_type, str):
            raise ValueError("artifact requirement requires artifact_type")
        nodes = value.get("from_nodes", value.get("from", ()))
        if isinstance(nodes, str):
            nodes = (nodes,)
        if not isinstance(nodes, Sequence) or isinstance(nodes, (bytes, bytearray)):
            raise ValueError("artifact requirement from_nodes must be a list")
        return cls(artifact_type, int(value.get("count", 1)), tuple(str(item) for item in nodes))

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_type": self.artifact_type,
            "count": self.count,
            "from_nodes": list(self.from_nodes),
        }


def _string_tuple(value: object, *, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise ValueError(f"{name} must be a string or list of strings")
    parsed = tuple(str(item) for item in value)
    if not all(parsed) or len(set(parsed)) != len(parsed):
        raise ValueError(f"{name} must contain unique non-empty strings")
    return parsed


@dataclass(frozen=True)
class WorkflowNode:
    """One calculation, aggregation, or derived-result node in a workflow DAG."""

    node_id: str
    kind: NodeKind = NodeKind.CALCULATION
    dependencies: tuple[str, ...] = ()
    operation: str | None = None
    role: str | None = None
    requires: tuple[ArtifactRequirement, ...] = ()
    produces: tuple[str, ...] = ()
    configuration: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("workflow node id is required")
        if self.node_id in self.dependencies:
            raise ValueError(f"workflow node {self.node_id!r} cannot depend on itself")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError(f"workflow node {self.node_id!r} has duplicate dependencies")
        if len(set(self.produces)) != len(self.produces):
            raise ValueError(f"workflow node {self.node_id!r} has duplicate outputs")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "requires", tuple(self.requires))
        object.__setattr__(self, "produces", tuple(self.produces))
        object.__setattr__(self, "configuration", dict(self.configuration))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkflowNode":
        node_id = value.get("id", value.get("node_id"))
        if not isinstance(node_id, str):
            raise ValueError("workflow node requires a string id")
        kind = NodeKind(str(value.get("kind", value.get("node_type", "calculation"))).lower())
        dependencies = _string_tuple(
            value.get("depends_on", value.get("dependencies", value.get("parents"))),
            name=f"dependencies for {node_id}",
        )
        raw_requires = value.get(
            "requires", value.get("required_artifacts", value.get("inputs", ()))
        )
        if isinstance(raw_requires, (str, Mapping)):
            raw_requires = (raw_requires,)
        if not isinstance(raw_requires, Sequence) or isinstance(raw_requires, (bytes, bytearray)):
            raise ValueError(f"requirements for {node_id} must be a list")
        requires = tuple(ArtifactRequirement.from_value(item) for item in raw_requires)
        produces = _string_tuple(
            value.get("produces", value.get("outputs", value.get("artifacts"))),
            name=f"outputs for {node_id}",
        )
        reserved = {
            "id",
            "node_id",
            "kind",
            "node_type",
            "depends_on",
            "dependencies",
            "parents",
            "requires",
            "required_artifacts",
            "inputs",
            "produces",
            "outputs",
            "artifacts",
            "operation",
            "type",
            "method",
            "role",
        }
        configuration = {key: item for key, item in value.items() if key not in reserved}
        operation = value.get("operation", value.get("type", value.get("method")))
        return cls(
            node_id,
            kind,
            dependencies,
            str(operation) if operation is not None else None,
            str(value["role"]) if value.get("role") is not None else None,
            requires,
            produces,
            configuration,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "kind": self.kind.value,
            "depends_on": list(self.dependencies),
            "operation": self.operation,
            "role": self.role,
            "requires": [item.to_dict() for item in self.requires],
            "produces": list(self.produces),
            "configuration": dict(self.configuration),
        }


@dataclass(frozen=True)
class WorkflowGraph:
    """Validated directed acyclic graph of system-agnostic workflow nodes."""

    graph_id: str
    nodes: tuple[WorkflowNode, ...]
    external_inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.graph_id:
            raise ValueError("workflow graph id is required")
        if not self.nodes:
            raise ValueError("workflow graph requires at least one node")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "external_inputs", tuple(self.external_inputs))
        self.validate()

    @property
    def node_map(self) -> dict[str, WorkflowNode]:
        return {node.node_id: node for node in self.nodes}

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, workflow_id: str | None = None
    ) -> "WorkflowGraph":
        selected: Mapping[str, Any] = value
        graph_id = workflow_id or str(value.get("id", "workflow"))
        workflow = value.get("workflow")
        if isinstance(workflow, Mapping):
            if "nodes" in workflow:
                selected = workflow
            else:
                if workflow_id is None:
                    if len(workflow) != 1:
                        raise ValueError("workflow_id is required when multiple workflows are defined")
                    graph_id, selected_value = next(iter(workflow.items()))
                else:
                    if workflow_id not in workflow:
                        raise ValueError(f"workflow is not defined: {workflow_id}")
                    selected_value = workflow[workflow_id]
                    graph_id = workflow_id
                if not isinstance(selected_value, Mapping):
                    raise ValueError("workflow definition must be an object")
                selected = selected_value

        raw_nodes = selected.get("nodes")
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes, bytearray)):
            raise ValueError("workflow requires a nodes list")
        nodes = [WorkflowNode.from_mapping(item) for item in raw_nodes if isinstance(item, Mapping)]
        if len(nodes) != len(raw_nodes):
            raise ValueError("every workflow node must be an object")

        def append_special(raw: object, kind: NodeKind, default_id: str) -> None:
            if raw is None:
                return
            values = raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, Mapping)) else (raw,)
            for index, item in enumerate(values):
                if not isinstance(item, Mapping):
                    raise ValueError(f"{kind.value} definition must be an object")
                data = dict(item)
                data.setdefault("id", default_id if len(values) == 1 else f"{default_id}_{index + 1}")
                data["kind"] = kind.value
                data.setdefault("depends_on", [node.node_id for node in nodes])
                nodes.append(WorkflowNode.from_mapping(data))

        append_special(selected.get("aggregation"), NodeKind.AGGREGATION, "aggregation")
        append_special(
            selected.get("derived_results", selected.get("derived")),
            NodeKind.DERIVED,
            "derived_result",
        )
        external = _string_tuple(selected.get("inputs"), name="workflow inputs")
        return cls(graph_id, tuple(nodes), external)

    @classmethod
    def linear(
        cls,
        graph_id: str,
        node_ids: Iterable[str],
        *,
        produces: Mapping[str, Sequence[str]] | None = None,
    ) -> "WorkflowGraph":
        nodes: list[WorkflowNode] = []
        previous: str | None = None
        for node_id in node_ids:
            nodes.append(
                WorkflowNode(
                    str(node_id),
                    dependencies=(previous,) if previous is not None else (),
                    produces=tuple((produces or {}).get(str(node_id), ())),
                )
            )
            previous = str(node_id)
        return cls(graph_id, tuple(nodes))

    def validate(self) -> None:
        identifiers = [node.node_id for node in self.nodes]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("workflow node ids must be unique")
        node_map = self.node_map
        for node in self.nodes:
            missing = [item for item in node.dependencies if item not in node_map]
            if missing:
                raise ValueError(
                    f"workflow node {node.node_id!r} has missing dependencies: {', '.join(missing)}"
                )
            if node.kind in {NodeKind.AGGREGATION, NodeKind.DERIVED} and not node.dependencies:
                raise ValueError(f"{node.kind.value} node {node.node_id!r} requires dependencies")
            for requirement in node.requires:
                sources = requirement.from_nodes or node.dependencies
                invalid_sources = [item for item in sources if item not in node.dependencies]
                if invalid_sources:
                    raise ValueError(
                        f"artifact requirement for {node.node_id!r} references non-dependencies: "
                        + ", ".join(invalid_sources)
                    )
                declared = sum(
                    _declared_artifact_matches(candidate, requirement.artifact_type)
                    for source in sources
                    for candidate in node_map[source].produces
                )
                declared += sum(
                    _declared_artifact_matches(item, requirement.artifact_type)
                    for item in self.external_inputs
                )
                any_declared = any(node_map[source].produces for source in sources)
                if any_declared and declared < requirement.count:
                    raise ValueError(
                        f"workflow node {node.node_id!r} requires {requirement.count} "
                        f"{requirement.artifact_type} artifact(s), but dependencies declare {declared}"
                    )
                if not sources and declared < requirement.count:
                    raise ValueError(
                        f"workflow node {node.node_id!r} lacks required input {requirement.artifact_type}"
                    )
        self.topological_order()

    def topological_order(self) -> tuple[str, ...]:
        node_map = self.node_map
        indegree = {node_id: 0 for node_id in node_map}
        children = {node_id: [] for node_id in node_map}
        for node in self.nodes:
            indegree[node.node_id] = len(node.dependencies)
            for dependency in node.dependencies:
                children[dependency].append(node.node_id)
        queue = [node.node_id for node in self.nodes if indegree[node.node_id] == 0]
        ordered: list[str] = []
        while queue:
            current = queue.pop(0)
            ordered.append(current)
            for child in children[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(ordered) != len(node_map):
            cyclic = [node_id for node_id, degree in indegree.items() if degree]
            raise ValueError(f"workflow graph contains a dependency cycle: {', '.join(cyclic)}")
        return tuple(ordered)

    def ready_nodes(self, completed: Iterable[str]) -> tuple[str, ...]:
        completed_set = set(completed)
        unknown = completed_set - set(self.node_map)
        if unknown:
            raise ValueError(f"completed set contains unknown nodes: {', '.join(sorted(unknown))}")
        return tuple(
            node.node_id
            for node in self.nodes
            if node.node_id not in completed_set
            and all(dependency in completed_set for dependency in node.dependencies)
        )

    def validate_artifacts(
        self,
        node_id: str,
        artifacts_by_node: Mapping[str, Artifact | Sequence[Artifact]],
    ) -> tuple[Artifact, ...]:
        """Validate actual dependency products before running one downstream node."""

        node = self.node_map.get(node_id)
        if node is None:
            raise ValueError(f"workflow node is not defined: {node_id}")
        collected: list[tuple[str, Artifact]] = []
        for dependency in node.dependencies:
            raw = artifacts_by_node.get(dependency)
            values = (raw,) if isinstance(raw, Artifact) else tuple(raw or ())
            if not values:
                raise ValueError(
                    f"workflow node {node_id!r} lacks artifacts from dependency {dependency!r}"
                )
            for artifact in values:
                if not isinstance(artifact, Artifact):
                    raise TypeError("workflow artifacts must use the typed Artifact model")
                if not artifact.validation.passed:
                    raise ValueError(
                        f"workflow node {node_id!r} received invalid artifact {artifact.artifact_id}"
                    )
                collected.append((dependency, artifact))

        for requirement in node.requires:
            allowed = set(requirement.from_nodes or node.dependencies)
            matches = [
                artifact
                for source, artifact in collected
                if source in allowed and artifact_matches_type(artifact, requirement.artifact_type)
            ]
            if len(matches) < requirement.count:
                raise ValueError(
                    f"workflow node {node_id!r} requires {requirement.count} valid "
                    f"{requirement.artifact_type} artifact(s), received {len(matches)}"
                )
        return tuple(artifact for _, artifact in collected)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": WORKFLOW_GRAPH_SCHEMA_VERSION,
            "graph_id": self.graph_id,
            "external_inputs": list(self.external_inputs),
            "topological_order": list(self.topological_order()),
            "nodes": [node.to_dict() for node in self.nodes],
        }


__all__ = [
    "WORKFLOW_GRAPH_SCHEMA_VERSION",
    "ArtifactRequirement",
    "NodeKind",
    "WorkflowGraph",
    "WorkflowNode",
]
