from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cmw.core.artifact_finalization import ArtifactFinalizationEvidence
from cmw.core.artifacts import (
    Artifact,
    ArtifactValidation,
    OptimizationArtifact,
    ValidationStatus,
)
from cmw.core.execution_contract import (
    ComputationalTask,
    ExecutionContractError,
    ExecutionIntent,
)
from cmw.core.execution_layout import ExecutionLayout, ExecutionLayoutError
from cmw.core.execution_profiles import (
    ExecutionProfile,
    MultiwfnResourcePolicy,
    OrcaResourcePolicy,
)
from cmw.core.job import JobTarget
from cmw.core.plan_materialization import (
    ExecutionPlan,
    ExecutionPlanNode,
    MaterializationStatus,
    RenderedExecutionInput,
    WorkflowPlanMaterializationError,
    WorkflowPlanMaterializer,
    finalize_materialized_artifacts,
)
from cmw.core.provenance import atomic_write_json
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.core.workflow_graph import (
    ArtifactRequirement,
    CalculationNode,
    WorkflowGraph,
)


PASSED = ArtifactValidation(
    ValidationStatus.PASSED,
    {"synthetic": True},
    "VALID",
    "synthetic test evidence",
)


class SyntheticRenderer:
    renderer_id = "synthetic"
    supported_tasks = frozenset({ComputationalTask.OPTIMIZATION})

    def __init__(self, *, unresolved_output: bool = False) -> None:
        self.unresolved_output = unresolved_output

    def validate(self, node, input_artifacts) -> None:
        if len(input_artifacts) != 1:
            raise ValueError("synthetic renderer requires one input artifact")

    def build_target(self, node, input_artifacts) -> JobTarget:
        structure = input_artifacts[0]
        return JobTarget(
            stage_type="OPT",
            geometry_sha256=structure.geometry_hash,
            charge=structure.charge,
            multiplicity=structure.multiplicity,
            calculation={"renderer": self.renderer_id, "node": node.node_id},
        )

    def render(
        self,
        node,
        input_artifacts,
        *,
        target,
        layout: ExecutionLayout,
        resource_profile,
        runtime_identity,
    ) -> RenderedExecutionInput:
        primary = layout.input_path("stage.inp")
        primary.write_text("synthetic optimization input\n", encoding="utf-8")
        target_path = layout.target_directory / "target.json"
        atomic_write_json(target_path, target.to_dict())
        provenance_path = layout.metadata_path("renderer.json")
        atomic_write_json(provenance_path, {"renderer": self.renderer_id})
        output = (
            Path("stage.out")
            if self.unresolved_output
            else layout.output_path("stage.out")
        )
        return RenderedExecutionInput(
            primary_input=primary,
            target_path=target_path,
            input_files={"primary": primary},
            output_files={
                "output": output,
                "geometry": layout.output_path("optimized.xyz"),
            },
            log_files={"stderr": layout.log_path("stage.err")},
            provenance_files={
                "layout": layout.layout_path,
                "renderer": provenance_path,
            },
            resources=resource_profile.to_dict(),
            executable=dict(runtime_identity),
        )

    def is_reusable(self, target_path: Path, metadata_path: Path) -> bool:
        return False


class WorkflowPlanMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        structure_path = self.root / "structure.xyz"
        structure_path.write_text(
            "2\nsynthetic hydrogen\nH 0 0 0\nH 0 0 0.74\n",
            encoding="utf-8",
        )
        self.structure = structure_artifact_from_file(
            structure_path,
            source="synthetic materialization test",
            charge=0,
            multiplicity=1,
        )
        self.intent = ExecutionIntent(
            "OPT", ComputationalTask.OPTIMIZATION, "Opt"
        )
        self.graph = WorkflowGraph(
            "synthetic-materialization",
            (
                CalculationNode(
                    "structure", produces=("StructureArtifact",)
                ),
                CalculationNode(
                    "optimization",
                    dependencies=("structure",),
                    requires=(
                        ArtifactRequirement(
                            "StructureArtifact", from_nodes=("structure",)
                        ),
                    ),
                    produces=("OptimizationArtifact",),
                    configuration={"execution_intent": self.intent.to_dict()},
                ),
            ),
        )
        self.optimization = OptimizationArtifact(
            producing_calculation="optimization",
            method="synthetic",
            basis="synthetic",
            parent_artifacts=(self.structure.artifact_id,),
        )
        self.profile = ExecutionProfile(
            "synthetic",
            OrcaResourcePolicy(1, 1.0),
            MultiwfnResourcePolicy(1, 1.0),
        )
        self.runtime = {"program": "synthetic", "version": "1"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def plan(self, *, renderer_id: str = "synthetic") -> ExecutionPlan:
        return ExecutionPlan(
            self.graph,
            (
                ExecutionPlanNode(
                    "optimization",
                    renderer_id,
                    self.intent,
                    planned_artifacts=(self.optimization,),
                ),
            ),
            available_artifacts={"structure": (self.structure,)},
        )

    def test_successful_materialization_resolves_attempt_and_layout(self) -> None:
        plan = self.plan()
        restored = ExecutionPlan.from_mapping(plan.to_dict())
        materializer = WorkflowPlanMaterializer(
            {"synthetic": SyntheticRenderer()}
        )

        result = materializer.materialize_node(
            restored,
            "optimization",
            project_root=self.root,
            system_identifier="system-a",
            resource_profile=self.profile,
            runtime_identity=self.runtime,
        )

        self.assertEqual(result.status, MaterializationStatus.PREPARED)
        self.assertEqual(result.attempt.attempt_id, "attempt_001")
        self.assertTrue(result.layout.working_directory.is_dir())
        self.assertTrue(all(Path(path).is_absolute() for path in result.input_files.values()))
        self.assertTrue(
            result.layout.metadata_path("materialization.json").is_file()
        )
        repeated = materializer.materialize_node(
            restored,
            "optimization",
            project_root=self.root,
            system_identifier="system-a",
            resource_profile=self.profile,
            runtime_identity=self.runtime,
        )
        self.assertEqual(repeated.attempt.attempt_id, "attempt_001")

        materialized_graph = materializer.materialize_ready(
            restored,
            project_root=self.root,
            system_identifier="system-b",
            resource_profile=self.profile,
            runtime_identity=self.runtime,
        )
        self.assertEqual(materialized_graph.execution_plan_id, restored.execution_plan_id)
        self.assertEqual(
            materialized_graph.workflow_graph_id, restored.workflow_graph.graph_id
        )
        self.assertEqual(
            tuple(node.node_id for node in materialized_graph.nodes),
            ("optimization",),
        )

    def test_missing_resource_profile_fails_closed(self) -> None:
        with self.assertRaises(WorkflowPlanMaterializationError) as caught:
            WorkflowPlanMaterializer(
                {"synthetic": SyntheticRenderer()}
            ).materialize_node(
                self.plan(),
                "optimization",
                project_root=self.root,
                system_identifier="system-a",
                resource_profile=None,
                runtime_identity=self.runtime,
            )
        self.assertEqual(caught.exception.code, "MISSING_RESOURCE_PROFILE")

    def test_spent_attempt_allocates_a_linked_restart(self) -> None:
        materializer = WorkflowPlanMaterializer(
            {"synthetic": SyntheticRenderer()}
        )
        first = materializer.materialize_node(
            self.plan(),
            "optimization",
            project_root=self.root,
            system_identifier="system-a",
            resource_profile=self.profile,
            runtime_identity=self.runtime,
        )
        first.layout.output_path("stage.out").write_text(
            "failed synthetic run\n", encoding="utf-8"
        )

        second = materializer.materialize_node(
            self.plan(),
            "optimization",
            project_root=self.root,
            system_identifier="system-a",
            resource_profile=self.profile,
            runtime_identity=self.runtime,
        )

        self.assertEqual(second.attempt.attempt_id, "attempt_002")
        self.assertEqual(second.attempt.parent_attempt_id, "attempt_001")

    def test_unresolved_output_path_fails_and_records_attempt_diagnostics(self) -> None:
        with self.assertRaises(ExecutionLayoutError):
            WorkflowPlanMaterializer(
                {"synthetic": SyntheticRenderer(unresolved_output=True)}
            ).materialize_node(
                self.plan(),
                "optimization",
                project_root=self.root,
                system_identifier="system-a",
                resource_profile=self.profile,
                runtime_identity=self.runtime,
            )
        failures = tuple(
            self.root.glob("calculation/**/materialization-failure.json")
        )
        self.assertEqual(len(failures), 1)

    def test_incompatible_renderer_fails_closed(self) -> None:
        with self.assertRaises(WorkflowPlanMaterializationError) as caught:
            WorkflowPlanMaterializer(
                {"synthetic": SyntheticRenderer()}
            ).materialize_node(
                self.plan(renderer_id="unregistered"),
                "optimization",
                project_root=self.root,
                system_identifier="system-a",
                resource_profile=self.profile,
                runtime_identity=self.runtime,
            )
        self.assertEqual(caught.exception.code, "INCOMPATIBLE_RENDERER")

    def test_missing_artifact_dependency_fails_closed(self) -> None:
        plan = ExecutionPlan(
            self.graph,
            self.plan().nodes,
            available_artifacts={},
        )
        with self.assertRaises(WorkflowPlanMaterializationError) as caught:
            WorkflowPlanMaterializer(
                {"synthetic": SyntheticRenderer()}
            ).materialize_node(
                plan,
                "optimization",
                project_root=self.root,
                system_identifier="system-a",
                resource_profile=self.profile,
                runtime_identity=self.runtime,
            )
        self.assertEqual(caught.exception.code, "MISSING_ARTIFACT_DEPENDENCY")

    def test_invalid_execution_intent_is_rejected_by_artifact_contract(self) -> None:
        invalid = ExecutionIntent(
            "SP", ComputationalTask.SINGLE_POINT, "SP"
        )
        with self.assertRaisesRegex(
            ExecutionContractError, "FAILED_EXECUTION_CONTRACT_MISMATCH"
        ):
            ExecutionPlan(
                self.graph,
                (
                    ExecutionPlanNode(
                        "optimization",
                        "synthetic",
                        invalid,
                        planned_artifacts=(self.optimization,),
                    ),
                ),
                available_artifacts={"structure": (self.structure,)},
            )

    def test_materialized_attempt_finalizes_only_declared_artifacts(self) -> None:
        materialized = WorkflowPlanMaterializer(
            {"synthetic": SyntheticRenderer()}
        ).materialize_node(
            self.plan(),
            "optimization",
            project_root=self.root,
            system_identifier="system-a",
            resource_profile=self.profile,
            runtime_identity=self.runtime,
        )
        geometry_output = Path(materialized.output_files["geometry"])
        geometry_output.write_text(
            "2\nsynthetic result\nH 0 0 0\nH 0 0 0.75\n",
            encoding="utf-8",
        )
        bundle = finalize_materialized_artifacts(
            materialized,
            (self.optimization,),
            {
                self.optimization.artifact_id: ArtifactFinalizationEvidence(
                    PASSED,
                    files={"geometry": str(geometry_output)},
                )
            },
            external_artifacts=(self.structure,),
        )

        finalized = bundle.artifacts[0]
        self.assertEqual(
            finalized.provenance["attempt_id"], "attempt_001"
        )
        self.assertEqual(
            finalized.provenance["target_id"], materialized.target_id
        )


if __name__ == "__main__":
    unittest.main()
