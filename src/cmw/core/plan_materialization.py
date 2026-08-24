"""Materialize scientific workflow plans into non-running execution attempts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .artifacts import (
    ARTIFACT_TYPES,
    Artifact,
    ExcitedStateArtifact,
    FrequencyArtifact,
    OptimizationArtifact,
    SinglePointArtifact,
    artifact_from_dict,
    artifact_matches_type,
)
from .artifact_finalization import (
    ArtifactFinalizationEvidence,
    FinalizedArtifactBundle,
    finalize_artifact_bundle,
)
from .execution_contract import (
    ComputationalTask,
    ExecutionContractError,
    ExecutionIntent,
)
from .execution_layout import (
    ExecutionLayout,
    ExecutionLayoutError,
    execution_target_directory,
    next_attempt_identifier,
)
from .job import ExecutionAttempt
from .provenance import atomic_write_json, file_hash, read_json, stable_hash
from .workflow_graph import NodeKind, WorkflowGraph


EXECUTION_PLAN_SCHEMA_VERSION = 1
MATERIALIZED_EXECUTION_SCHEMA_VERSION = 1


class MaterializationStatus(str, Enum):
    PREPARED = "PREPARED"
    REUSED = "REUSED"


class WorkflowPlanMaterializationError(ValueError):
    """Raised when a workflow plan cannot become an execution attempt safely."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


@runtime_checkable
class ExecutionTarget(Protocol):
    """Small target identity required by the generic materializer."""

    @property
    def target_id(self) -> str: ...

    def to_dict(self) -> Mapping[str, object]: ...


@runtime_checkable
class ExecutionResourceProfile(Protocol):
    """Operational resource identity understood by a selected renderer."""

    name: str

    @property
    def execution_profile_hash(self) -> str: ...

    def to_dict(self) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class ExecutionPlanNode:
    """Adapter binding between one scientific node and one renderer."""

    node_id: str
    renderer_id: str
    execution_intent: ExecutionIntent
    renderer_configuration: Mapping[str, object] = field(default_factory=dict)
    input_artifacts: tuple[Artifact, ...] = ()
    planned_artifacts: tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("execution-plan node id is required")
        if not self.renderer_id:
            raise ValueError("execution-plan renderer id is required")
        object.__setattr__(
            self, "renderer_configuration", dict(self.renderer_configuration)
        )
        object.__setattr__(self, "input_artifacts", tuple(self.input_artifacts))
        object.__setattr__(self, "planned_artifacts", tuple(self.planned_artifacts))
        for artifact in (*self.input_artifacts, *self.planned_artifacts):
            if not isinstance(artifact, Artifact):
                raise TypeError("execution plans require typed Artifact instances")

    def identity(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "renderer_id": self.renderer_id,
            "execution_intent": self.execution_intent.to_dict(),
            "renderer_configuration": dict(self.renderer_configuration),
            "input_artifact_ids": [
                artifact.artifact_id for artifact in self.input_artifacts
            ],
            "planned_artifact_ids": [
                artifact.artifact_id for artifact in self.planned_artifacts
            ],
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.identity(),
            "input_artifacts": [
                artifact.to_dict() for artifact in self.input_artifacts
            ],
            "planned_artifacts": [
                artifact.to_dict() for artifact in self.planned_artifacts
            ],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutionPlanNode":
        return cls(
            node_id=str(value["node_id"]),
            renderer_id=str(value["renderer_id"]),
            execution_intent=ExecutionIntent.from_mapping(
                dict(value["execution_intent"])
            ),
            renderer_configuration=dict(value.get("renderer_configuration", {})),
            input_artifacts=tuple(
                artifact_from_dict(dict(item))
                for item in value.get("input_artifacts", ())
            ),
            planned_artifacts=tuple(
                artifact_from_dict(dict(item))
                for item in value.get("planned_artifacts", ())
            ),
        )


_TASK_BOUND_ARTIFACTS: tuple[tuple[type[Artifact], ComputationalTask], ...] = (
    (OptimizationArtifact, ComputationalTask.OPTIMIZATION),
    (FrequencyArtifact, ComputationalTask.FREQUENCY),
    (SinglePointArtifact, ComputationalTask.SINGLE_POINT),
    (ExcitedStateArtifact, ComputationalTask.EXCITED_STATE),
)


def _task_for_artifact_type(artifact_type: str) -> ComputationalTask | None:
    selected = ARTIFACT_TYPES.get(artifact_type)
    if selected is None:
        return None
    for base, task in _TASK_BOUND_ARTIFACTS:
        if issubclass(selected, base):
            return task
    return None


def _validate_task_artifact_alignment(
    node: ExecutionPlanNode, declared_outputs: Sequence[str]
) -> None:
    candidates = [
        (artifact_type, _task_for_artifact_type(artifact_type))
        for artifact_type in declared_outputs
    ]
    candidates.extend(
        (artifact.artifact_type, _task_for_artifact_type(artifact.artifact_type))
        for artifact in node.planned_artifacts
    )
    mismatches = [
        f"{artifact_type} requires {task.value}"
        for artifact_type, task in candidates
        if task is not None and task is not node.execution_intent.task
    ]
    if mismatches:
        raise ExecutionContractError(
            f"{ExecutionContractError.code}: execution task "
            f"{node.execution_intent.task.value} conflicts with "
            + ", ".join(mismatches)
        )


@dataclass(frozen=True)
class ExecutionPlan:
    """Scientific graph plus adapter-supplied executable node bindings."""

    workflow_graph: WorkflowGraph
    nodes: tuple[ExecutionPlanNode, ...]
    available_artifacts: Mapping[str, tuple[Artifact, ...]] = field(
        default_factory=dict
    )
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        available = {
            str(node_id): tuple(artifacts)
            for node_id, artifacts in self.available_artifacts.items()
        }
        object.__setattr__(self, "available_artifacts", available)
        object.__setattr__(self, "provenance", dict(self.provenance))
        self.validate()

    @property
    def node_map(self) -> dict[str, ExecutionPlanNode]:
        return {node.node_id: node for node in self.nodes}

    @property
    def execution_plan_id(self) -> str:
        return stable_hash(
            {
                "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
                "workflow_graph_id": self.workflow_graph.graph_id,
                "nodes": [node.identity() for node in self.nodes],
                "available_artifacts": {
                    node_id: [artifact.artifact_id for artifact in artifacts]
                    for node_id, artifacts in sorted(
                        self.available_artifacts.items()
                    )
                },
            }
        )

    def validate(self) -> None:
        identifiers = [node.node_id for node in self.nodes]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("execution-plan node ids must be unique")
        graph_nodes = self.workflow_graph.node_map
        for node in self.nodes:
            workflow_node = graph_nodes.get(node.node_id)
            if workflow_node is None:
                raise ValueError(
                    f"execution-plan node is absent from workflow graph: {node.node_id}"
                )
            if workflow_node.kind is not NodeKind.CALCULATION:
                raise ValueError(
                    f"only calculation nodes can be executable: {node.node_id}"
                )
            configured_intent = workflow_node.configuration.get("execution_intent")
            if configured_intent is not None:
                if not isinstance(configured_intent, Mapping):
                    raise ExecutionContractError(
                        f"{ExecutionContractError.code}: workflow execution intent "
                        "must be a mapping"
                    )
                if ExecutionIntent.from_mapping(configured_intent) != node.execution_intent:
                    raise ExecutionContractError(
                        f"{ExecutionContractError.code}: workflow and execution-plan "
                        f"intent differ for {node.node_id}"
                    )
            _validate_task_artifact_alignment(node, workflow_node.produces)
            declared = set(workflow_node.produces)
            for artifact in node.planned_artifacts:
                if artifact.producing_calculation != node.node_id:
                    raise ValueError(
                        f"planned artifact producer differs from node {node.node_id}"
                    )
                if declared and not any(
                    artifact_matches_type(artifact, declared_type)
                    for declared_type in declared
                ):
                    raise ValueError(
                        f"planned {artifact.artifact_type} is not declared by "
                        f"workflow node {node.node_id}"
                    )
            for artifact in node.input_artifacts:
                if not artifact.validation.passed:
                    raise ValueError(
                        f"execution-plan input artifact has not passed validation: "
                        f"{artifact.artifact_id}"
                    )
        for node_id, artifacts in self.available_artifacts.items():
            if node_id not in graph_nodes:
                raise ValueError(
                    f"available artifacts reference unknown workflow node: {node_id}"
                )
            if not artifacts:
                raise ValueError(
                    f"available artifact set for {node_id} must not be empty"
                )
            if any(not artifact.validation.passed for artifact in artifacts):
                raise ValueError(
                    f"available artifacts for {node_id} have not passed validation"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
            "execution_plan_id": self.execution_plan_id,
            "workflow_graph": self.workflow_graph.to_dict(),
            "nodes": [node.to_dict() for node in self.nodes],
            "available_artifacts": {
                node_id: [artifact.to_dict() for artifact in artifacts]
                for node_id, artifacts in sorted(self.available_artifacts.items())
            },
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutionPlan":
        if value.get("schema_version") != EXECUTION_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported execution-plan schema")
        raw_graph = value.get("workflow_graph")
        if not isinstance(raw_graph, Mapping):
            raise ValueError("execution plan requires a workflow_graph mapping")
        plan = cls(
            workflow_graph=WorkflowGraph.from_mapping(raw_graph),
            nodes=tuple(
                ExecutionPlanNode.from_mapping(dict(item))
                for item in value.get("nodes", ())
            ),
            available_artifacts={
                str(node_id): tuple(
                    artifact_from_dict(dict(item)) for item in artifacts
                )
                for node_id, artifacts in dict(
                    value.get("available_artifacts", {})
                ).items()
            },
            provenance=dict(value.get("provenance", {})),
        )
        if value.get("execution_plan_id") not in (None, plan.execution_plan_id):
            raise ValueError("stored execution-plan identity does not match content")
        return plan


@dataclass(frozen=True)
class RenderedExecutionInput:
    """Renderer-owned paths and operational metadata for one attempt."""

    primary_input: Path
    target_path: Path
    input_files: Mapping[str, Path]
    output_files: Mapping[str, Path]
    log_files: Mapping[str, Path]
    provenance_files: Mapping[str, Path]
    resources: Mapping[str, object]
    executable: Mapping[str, object]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "primary_input", Path(self.primary_input))
        object.__setattr__(self, "target_path", Path(self.target_path))
        for field_name in (
            "input_files",
            "output_files",
            "log_files",
            "provenance_files",
        ):
            object.__setattr__(
                self,
                field_name,
                {
                    str(role): Path(path)
                    for role, path in getattr(self, field_name).items()
                },
            )
        object.__setattr__(self, "resources", dict(self.resources))
        object.__setattr__(self, "executable", dict(self.executable))
        object.__setattr__(self, "metadata", dict(self.metadata))


@runtime_checkable
class ExecutionRenderer(Protocol):
    """Program adapter used by the core without embedding program semantics."""

    renderer_id: str
    supported_tasks: frozenset[ComputationalTask]

    def validate(
        self, node: ExecutionPlanNode, input_artifacts: Sequence[Artifact]
    ) -> None: ...

    def build_target(
        self, node: ExecutionPlanNode, input_artifacts: Sequence[Artifact]
    ) -> ExecutionTarget: ...

    def render(
        self,
        node: ExecutionPlanNode,
        input_artifacts: Sequence[Artifact],
        *,
        target: ExecutionTarget,
        layout: ExecutionLayout,
        resource_profile: ExecutionResourceProfile,
        runtime_identity: Mapping[str, object],
    ) -> RenderedExecutionInput: ...

    def is_reusable(self, target_path: Path, metadata_path: Path) -> bool: ...


@dataclass(frozen=True)
class MaterializedExecutionNode:
    node_id: str
    renderer_id: str
    execution_intent: ExecutionIntent
    target: Mapping[str, object]
    attempt: ExecutionAttempt
    layout: ExecutionLayout
    status: MaterializationStatus
    input_files: Mapping[str, str]
    output_files: Mapping[str, str]
    log_files: Mapping[str, str]
    provenance_files: Mapping[str, str]
    planned_artifact_ids: tuple[str, ...]
    execution_plan_id: str
    resource_profile_hash: str
    runtime_identity_hash: str
    renderer_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", dict(self.target))
        object.__setattr__(self, "status", MaterializationStatus(self.status))
        for field_name in (
            "input_files",
            "output_files",
            "log_files",
            "provenance_files",
        ):
            object.__setattr__(
                self,
                field_name,
                {
                    str(role): str(path)
                    for role, path in getattr(self, field_name).items()
                },
            )
        object.__setattr__(
            self, "planned_artifact_ids", tuple(self.planned_artifact_ids)
        )
        object.__setattr__(self, "renderer_metadata", dict(self.renderer_metadata))
        if not self.input_files.get("primary"):
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: materialized primary input is missing"
            )
        if self.target_id != self.attempt.target_id:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: attempt target identity conflicts with target"
            )
        if self.target_id != self.layout.target_identifier:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: target identity conflicts with layout"
            )
        self.layout.validate_attempt_identity(self.attempt.attempt_id)
        self.layout.validate_paths(
            input_paths=tuple(Path(path) for path in self.input_files.values()),
            output_paths=tuple(Path(path) for path in self.output_files.values()),
            metadata_paths=tuple(
                Path(path) for path in self.provenance_files.values()
            ),
            log_paths=tuple(Path(path) for path in self.log_files.values()),
        )
        if len(set(self.planned_artifact_ids)) != len(self.planned_artifact_ids):
            raise ValueError("materialized planned artifact identities must be unique")
        if not all(
            (self.execution_plan_id, self.resource_profile_hash, self.runtime_identity_hash)
        ):
            raise ValueError("materialized execution identities must be non-empty")

    @property
    def target_id(self) -> str:
        return str(self.target["target_id"])

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": MATERIALIZED_EXECUTION_SCHEMA_VERSION,
            "node_id": self.node_id,
            "renderer_id": self.renderer_id,
            "execution_intent": self.execution_intent.to_dict(),
            "target": dict(self.target),
            "attempt": self.attempt.to_dict(),
            "execution_layout": self.layout.to_dict(),
            "status": self.status.value,
            "input_files": dict(self.input_files),
            "output_files": dict(self.output_files),
            "log_files": dict(self.log_files),
            "provenance_files": dict(self.provenance_files),
            "planned_artifact_ids": list(self.planned_artifact_ids),
            "execution_plan_id": self.execution_plan_id,
            "resource_profile_hash": self.resource_profile_hash,
            "runtime_identity_hash": self.runtime_identity_hash,
            "renderer_metadata": dict(self.renderer_metadata),
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, status: MaterializationStatus | None = None
    ) -> "MaterializedExecutionNode":
        if value.get("schema_version") != MATERIALIZED_EXECUTION_SCHEMA_VERSION:
            raise ValueError("unsupported materialized-execution schema")
        raw_attempt = dict(value["attempt"])
        attempt = ExecutionAttempt(
            attempt_id=str(raw_attempt["attempt_id"]),
            target_id=str(raw_attempt["target_id"]),
            created_at=str(raw_attempt["created_at"]),
            resources=dict(raw_attempt["resources"]),
            executable=dict(raw_attempt["executable"]),
            generated_input_sha256=str(raw_attempt["generated_input_sha256"]),
            parent_attempt_id=(
                str(raw_attempt["parent_attempt_id"])
                if raw_attempt.get("parent_attempt_id") is not None
                else None
            ),
        )
        return cls(
            node_id=str(value["node_id"]),
            renderer_id=str(value["renderer_id"]),
            execution_intent=ExecutionIntent.from_mapping(
                dict(value["execution_intent"])
            ),
            target=dict(value["target"]),
            attempt=attempt,
            layout=ExecutionLayout.from_mapping(dict(value["execution_layout"])),
            status=status or MaterializationStatus(str(value["status"])),
            input_files={
                str(role): str(path)
                for role, path in dict(value["input_files"]).items()
            },
            output_files={
                str(role): str(path)
                for role, path in dict(value["output_files"]).items()
            },
            log_files={
                str(role): str(path)
                for role, path in dict(value["log_files"]).items()
            },
            provenance_files={
                str(role): str(path)
                for role, path in dict(value["provenance_files"]).items()
            },
            planned_artifact_ids=tuple(
                str(item) for item in value.get("planned_artifact_ids", ())
            ),
            execution_plan_id=str(value["execution_plan_id"]),
            resource_profile_hash=str(value["resource_profile_hash"]),
            runtime_identity_hash=str(value["runtime_identity_hash"]),
            renderer_metadata=dict(value.get("renderer_metadata", {})),
        )


@dataclass(frozen=True)
class MaterializedExecutionGraph:
    execution_plan_id: str
    workflow_graph_id: str
    nodes: tuple[MaterializedExecutionNode, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": MATERIALIZED_EXECUTION_SCHEMA_VERSION,
            "execution_plan_id": self.execution_plan_id,
            "workflow_graph_id": self.workflow_graph_id,
            "nodes": [node.to_dict() for node in self.nodes],
        }


def _merge_artifacts(
    base: Mapping[str, tuple[Artifact, ...]],
    supplied: Mapping[str, Artifact | Sequence[Artifact]],
) -> dict[str, tuple[Artifact, ...]]:
    merged = {node_id: tuple(artifacts) for node_id, artifacts in base.items()}
    for node_id, raw in supplied.items():
        values = (raw,) if isinstance(raw, Artifact) else tuple(raw)
        if any(not isinstance(artifact, Artifact) for artifact in values):
            raise TypeError("materialization dependencies require typed artifacts")
        existing = {artifact.artifact_id: artifact for artifact in merged.get(node_id, ())}
        for artifact in values:
            # Artifact identity excludes mutable validation and file evidence.
            # A caller may therefore promote the plan's copy to a finalized copy
            # without changing the scientific identity used by the graph.
            existing[artifact.artifact_id] = artifact
        merged[str(node_id)] = tuple(existing.values())
    return merged


class WorkflowPlanMaterializer:
    """Resolve dependencies and prepare attempts without launching executables."""

    def __init__(self, renderers: Mapping[str, ExecutionRenderer]) -> None:
        selected = dict(renderers)
        if not selected:
            raise ValueError("workflow materializer requires at least one renderer")
        for renderer_id, renderer in selected.items():
            if renderer_id != renderer.renderer_id:
                raise ValueError("renderer registry key conflicts with renderer identity")
        self._renderers = selected

    @staticmethod
    def _validate_rendered_paths(
        rendered: RenderedExecutionInput,
        *,
        target: ExecutionTarget,
        layout: ExecutionLayout,
    ) -> None:
        layout.validate_paths(
            input_paths=tuple(rendered.input_files.values()),
            output_paths=tuple(rendered.output_files.values()),
            metadata_paths=tuple(rendered.provenance_files.values()),
            log_paths=tuple(rendered.log_files.values()),
        )
        if rendered.primary_input.resolve() not in {
            path.resolve() for path in rendered.input_files.values()
        }:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: primary input is not a declared input file"
            )
        expected_target = layout.target_directory / "target.json"
        if rendered.target_path.resolve() != expected_target:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: renderer target path is non-deterministic"
            )
        if not rendered.target_path.is_file():
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: renderer did not persist target metadata"
            )
        if target.target_id != layout.target_identifier:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: target identity conflicts with layout"
            )

    @staticmethod
    def _prepared_record(
        target_directory: Path,
        *,
        node: ExecutionPlanNode,
        renderer: ExecutionRenderer,
        execution_plan_id: str,
        resource_profile_hash: str,
        runtime_identity_hash: str,
    ) -> MaterializedExecutionNode | None:
        attempts = target_directory / "attempts"
        if not attempts.is_dir():
            return None
        target_path = target_directory / "target.json"
        for directory in sorted(attempts.iterdir(), reverse=True):
            manifest_path = directory / "materialization.json"
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            try:
                record = MaterializedExecutionNode.from_mapping(
                    read_json(manifest_path)
                )
            except (OSError, UnicodeError, ValueError, KeyError, TypeError):
                continue
            if (
                record.node_id != node.node_id
                or record.renderer_id != node.renderer_id
                or record.execution_plan_id != execution_plan_id
                or record.resource_profile_hash != resource_profile_hash
                or record.runtime_identity_hash != runtime_identity_hash
            ):
                continue
            metadata_path = directory / "job.json"
            if metadata_path.is_file() and renderer.is_reusable(
                target_path, metadata_path
            ):
                return replace(record, status=MaterializationStatus.REUSED)
            spent = any(
                (directory / filename).exists()
                for filename in ("job.json", "stage.out", "stage.err")
            )
            if spent:
                continue
            input_paths = [Path(path) for path in record.input_files.values()]
            provenance_paths = [
                Path(path) for path in record.provenance_files.values()
            ]
            if (
                all(path.is_file() for path in (*input_paths, *provenance_paths))
                and record.input_files.get("primary") is not None
                and file_hash(Path(record.input_files["primary"]))
                == record.attempt.generated_input_sha256
            ):
                return record
        return None

    def materialize_node(
        self,
        plan: ExecutionPlan,
        node_id: str,
        *,
        project_root: Path,
        system_identifier: str,
        resource_profile: ExecutionResourceProfile | None,
        runtime_identity: Mapping[str, object],
        artifacts_by_node: Mapping[
            str, Artifact | Sequence[Artifact]
        ] | None = None,
    ) -> MaterializedExecutionNode:
        if resource_profile is None:
            raise WorkflowPlanMaterializationError(
                "MISSING_RESOURCE_PROFILE",
                "workflow materialization requires a resolved resource profile",
            )
        if not runtime_identity:
            raise WorkflowPlanMaterializationError(
                "MISSING_RUNTIME_IDENTITY",
                "workflow materialization requires runtime provenance",
            )
        node = plan.node_map.get(node_id)
        if node is None:
            raise WorkflowPlanMaterializationError(
                "MISSING_EXECUTION_NODE", f"execution-plan node is undefined: {node_id}"
            )
        renderer = self._renderers.get(node.renderer_id)
        if renderer is None:
            raise WorkflowPlanMaterializationError(
                "INCOMPATIBLE_RENDERER",
                f"renderer is not registered: {node.renderer_id}",
            )
        if node.execution_intent.task not in renderer.supported_tasks:
            raise ExecutionContractError(
                f"{ExecutionContractError.code}: renderer {node.renderer_id} does "
                f"not support {node.execution_intent.task.value}"
            )

        available = _merge_artifacts(
            plan.available_artifacts, artifacts_by_node or {}
        )
        try:
            dependency_artifacts = plan.workflow_graph.validate_artifacts(
                node_id, available
            )
        except (TypeError, ValueError) as exc:
            raise WorkflowPlanMaterializationError(
                "MISSING_ARTIFACT_DEPENDENCY", str(exc)
            ) from exc
        input_artifacts = tuple(
            {
                artifact.artifact_id: artifact
                for artifact in (*node.input_artifacts, *dependency_artifacts)
            }.values()
        )
        renderer.validate(node, input_artifacts)
        target = renderer.build_target(node, input_artifacts)
        if not isinstance(target, ExecutionTarget):
            raise TypeError("renderer target does not provide target_id and to_dict")
        target_record = dict(target.to_dict())
        stored_target_id = target_record.get("target_id")
        if stored_target_id not in (None, target.target_id):
            raise ValueError("renderer target record conflicts with target identity")
        target_record["target_id"] = target.target_id

        project_root = Path(project_root).expanduser().resolve()
        target_directory = execution_target_directory(
            project_root,
            system_identifier=system_identifier,
            workflow_node_identifier=node_id,
            target_identifier=target.target_id,
        )
        resource_profile_hash = resource_profile.execution_profile_hash
        runtime_identity_hash = stable_hash(dict(runtime_identity))
        previous = self._prepared_record(
            target_directory,
            node=node,
            renderer=renderer,
            execution_plan_id=plan.execution_plan_id,
            resource_profile_hash=resource_profile_hash,
            runtime_identity_hash=runtime_identity_hash,
        )
        if previous is not None:
            return previous

        attempt_identifier = next_attempt_identifier(target_directory)
        attempt_index = int(attempt_identifier.rsplit("_", 1)[1])
        parent_attempt_id = (
            f"attempt_{attempt_index - 1:03d}" if attempt_index > 1 else None
        )
        layout = ExecutionLayout(
            project_root=project_root,
            system_identifier=system_identifier,
            workflow_node_identifier=node_id,
            target_identifier=target.target_id,
            attempt_identifier=attempt_identifier,
        )
        layout.create_working_directory()
        atomic_write_json(layout.layout_path, layout.to_dict())
        try:
            rendered = renderer.render(
                node,
                input_artifacts,
                target=target,
                layout=layout,
                resource_profile=resource_profile,
                runtime_identity=runtime_identity,
            )
            self._validate_rendered_paths(rendered, target=target, layout=layout)
            input_files = {
                role: str(path.resolve())
                for role, path in rendered.input_files.items()
            }
            primary_roles = [
                role
                for role, path in rendered.input_files.items()
                if path.resolve() == rendered.primary_input.resolve()
            ]
            if len(primary_roles) != 1:
                raise ExecutionLayoutError(
                    f"{ExecutionLayoutError.code}: primary input role is ambiguous"
                )
            primary_role = primary_roles[0]
            if primary_role != "primary":
                input_files = {
                    "primary": input_files.pop(primary_role), **input_files
                }
            attempt = ExecutionAttempt.create(
                target_id=target.target_id,
                resources=rendered.resources,
                executable=rendered.executable,
                generated_input_sha256=file_hash(rendered.primary_input),
                parent_attempt_id=parent_attempt_id,
                attempt_id=layout.attempt_identifier,
            )
            materialized = MaterializedExecutionNode(
                node_id=node_id,
                renderer_id=node.renderer_id,
                execution_intent=node.execution_intent,
                target=target_record,
                attempt=attempt,
                layout=layout,
                status=MaterializationStatus.PREPARED,
                input_files=input_files,
                output_files={
                    role: str(path.resolve())
                    for role, path in rendered.output_files.items()
                },
                log_files={
                    role: str(path.resolve())
                    for role, path in rendered.log_files.items()
                },
                provenance_files={
                    role: str(path.resolve())
                    for role, path in rendered.provenance_files.items()
                },
                planned_artifact_ids=tuple(
                    artifact.artifact_id for artifact in node.planned_artifacts
                ),
                execution_plan_id=plan.execution_plan_id,
                resource_profile_hash=resource_profile_hash,
                runtime_identity_hash=runtime_identity_hash,
                renderer_metadata=rendered.metadata,
            )
            atomic_write_json(
                layout.metadata_path("execution-attempt.json"), attempt.to_dict()
            )
            atomic_write_json(
                layout.metadata_path("materialization.json"), materialized.to_dict()
            )
            return materialized
        except Exception as exc:
            code = getattr(exc, "code", "FAILED_PLAN_MATERIALIZATION")
            atomic_write_json(
                layout.metadata_path("materialization-failure.json"),
                {
                    "schema_version": MATERIALIZED_EXECUTION_SCHEMA_VERSION,
                    "status": "FAILED",
                    "code": str(code),
                    "reason": str(exc),
                    "execution_plan_id": plan.execution_plan_id,
                    "node_id": node_id,
                    "renderer_id": node.renderer_id,
                    "target_id": target.target_id,
                    "execution_layout": layout.to_dict(),
                    "resource_profile_hash": resource_profile_hash,
                    "runtime_identity_hash": runtime_identity_hash,
                },
            )
            raise

    def materialize_ready(
        self,
        plan: ExecutionPlan,
        *,
        project_root: Path,
        system_identifier: str,
        resource_profile: ExecutionResourceProfile | None,
        runtime_identity: Mapping[str, object],
        artifacts_by_node: Mapping[
            str, Artifact | Sequence[Artifact]
        ] | None = None,
    ) -> MaterializedExecutionGraph:
        available = _merge_artifacts(
            plan.available_artifacts, artifacts_by_node or {}
        )
        materialized: list[MaterializedExecutionNode] = []
        for node_id in plan.workflow_graph.topological_order():
            if node_id not in plan.node_map:
                continue
            workflow_node = plan.workflow_graph.node_map[node_id]
            if not all(dependency in available for dependency in workflow_node.dependencies):
                continue
            materialized.append(
                self.materialize_node(
                    plan,
                    node_id,
                    project_root=project_root,
                    system_identifier=system_identifier,
                    resource_profile=resource_profile,
                    runtime_identity=runtime_identity,
                    artifacts_by_node=available,
                )
            )
        return MaterializedExecutionGraph(
            plan.execution_plan_id, plan.workflow_graph.graph_id, tuple(materialized)
        )


def finalize_materialized_artifacts(
    materialized: MaterializedExecutionNode,
    planned_artifacts: Sequence[Artifact],
    evidence: Mapping[str, ArtifactFinalizationEvidence],
    *,
    external_artifacts: Sequence[Artifact] = (),
) -> FinalizedArtifactBundle:
    """Finalize exactly the artifacts declared by one materialized attempt.

    The function delegates scientific compatibility decisions to the existing
    bundle finalizer.  It adds only execution provenance, and never executes a
    program or creates scientific evidence.
    """

    planned = tuple(planned_artifacts)
    declared_ids = tuple(artifact.artifact_id for artifact in planned)
    if declared_ids != materialized.planned_artifact_ids:
        raise WorkflowPlanMaterializationError(
            "ARTIFACT_PLAN_MISMATCH",
            "finalization artifacts differ from the materialized execution plan",
        )
    declared_outputs = {
        Path(path).expanduser().resolve()
        for path in materialized.output_files.values()
    }
    for artifact_id, supplied in evidence.items():
        for role, raw_path in supplied.files.items():
            output_path = Path(raw_path).expanduser()
            if not output_path.is_absolute():
                raise WorkflowPlanMaterializationError(
                    "ARTIFACT_OUTPUT_MISMATCH",
                    f"artifact {artifact_id} has unresolved output {role}: {raw_path}",
                )
            resolved = output_path.resolve()
            if resolved not in declared_outputs or not resolved.is_file():
                raise WorkflowPlanMaterializationError(
                    "ARTIFACT_OUTPUT_MISMATCH",
                    f"artifact {artifact_id} output {role} is not a materialized file",
                )
    enriched = {
        artifact_id: replace(
            supplied,
            provenance={
                **dict(supplied.provenance),
                "producing_execution_node": materialized.node_id,
                "target_id": materialized.target_id,
                "attempt_id": materialized.attempt.attempt_id,
                "execution_plan_id": materialized.execution_plan_id,
            },
        )
        for artifact_id, supplied in evidence.items()
    }
    return finalize_artifact_bundle(
        planned,
        enriched,
        external_artifacts=external_artifacts,
    )


__all__ = [
    "EXECUTION_PLAN_SCHEMA_VERSION",
    "MATERIALIZED_EXECUTION_SCHEMA_VERSION",
    "ExecutionPlan",
    "ExecutionPlanNode",
    "ExecutionRenderer",
    "ExecutionResourceProfile",
    "ExecutionTarget",
    "MaterializationStatus",
    "MaterializedExecutionGraph",
    "MaterializedExecutionNode",
    "RenderedExecutionInput",
    "WorkflowPlanMaterializationError",
    "WorkflowPlanMaterializer",
    "finalize_materialized_artifacts",
]
