from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
import unittest
from pathlib import Path

from cmw.core.artifacts import OptimizationArtifact
from cmw.core.execution_profiles import (
    ExecutionProfile,
    MultiwfnResourcePolicy,
    OrcaResourcePolicy,
)
from cmw.core.plan_materialization import ExecutionPlan, WorkflowPlanMaterializer
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.core.workflow_graph import (
    ArtifactRequirement,
    CalculationNode,
    WorkflowGraph,
)
from cmw.molecular.orca.cli import build_parser
from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.orca.renderer import (
    ORCA_RENDERER_ID,
    OrcaExecutionRenderer,
    orca_execution_plan_node,
)
from cmw.molecular.orca.status import StageType


class OrcaPlanRendererTests(unittest.TestCase):
    def test_structure_artifact_materializes_xyzfile_optimization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source.xyz"
            source.write_text(
                "2\nsynthetic hydrogen\nH 0 0 0\nH 0 0 0.74\n",
                encoding="utf-8",
            )
            structure = structure_artifact_from_file(
                source,
                source="synthetic ORCA renderer test",
                charge=0,
                multiplicity=1,
            )
            spec = OrcaStageSpec(StageType.OPT, "r2SCAN-3c")
            graph = WorkflowGraph(
                "orca-materialization",
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
                        configuration={
                            "execution_intent": spec.execution_intent.to_dict()
                        },
                    ),
                ),
            )
            planned = OptimizationArtifact(
                producing_calculation="optimization",
                method="r2SCAN-3c",
                parent_artifacts=(structure.artifact_id,),
            )
            plan = ExecutionPlan(
                graph,
                (
                    orca_execution_plan_node(
                        "optimization",
                        spec,
                        planned_artifacts=(planned,),
                        geometry_artifact_id=structure.artifact_id,
                        outputs={"geometry": "stage.xyz"},
                    ),
                ),
                available_artifacts={"structure": (structure,)},
            )
            profile = ExecutionProfile(
                "test",
                OrcaResourcePolicy(2, 2.0),
                MultiwfnResourcePolicy(1, 1.0),
            )
            runtime = {
                "runtime_id": "synthetic-runtime",
                "orca": {"path": "/synthetic/orca", "version": "test"},
            }

            result = WorkflowPlanMaterializer(
                {ORCA_RENDERER_ID: OrcaExecutionRenderer()}
            ).materialize_node(
                plan,
                "optimization",
                project_root=root,
                system_identifier="system-a",
                resource_profile=profile,
                runtime_identity=runtime,
            )

            stage_input = Path(result.input_files["primary"])
            rendered = stage_input.read_text(encoding="utf-8")
            self.assertIn("! r2SCAN-3c Opt", rendered)
            self.assertIn("* xyzfile 0 1 input.xyz", rendered)
            self.assertTrue(Path(result.input_files["geometry"]).is_file())
            self.assertTrue(
                result.layout.metadata_path("geometry-input.json").is_file()
            )
            self.assertEqual(result.execution_intent, spec.execution_intent)
            self.assertEqual(
                result.planned_artifact_ids, (planned.artifact_id,)
            )

            plan_path = root / "execution-plan.json"
            plan_path.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
            runtime_path = root / "runtime.json"
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            execution_path = root / "execution.yaml"
            execution_path.write_text(
                "schema_version: 1\n"
                "active_profile: test\n"
                "profiles:\n"
                "  test:\n"
                "    orca:\n"
                "      nprocs: 2\n"
                "      total_memory_gb: 2.0\n"
                "    multiwfn:\n"
                "      nthreads: 1\n"
                "      total_memory_gb: 1.0\n",
                encoding="utf-8",
            )
            parsed = build_parser().parse_args(
                [
                    "materialize-plan",
                    "--plan",
                    str(plan_path),
                    "--node",
                    "optimization",
                    "--project-root",
                    str(root),
                    "--system",
                    "system-b",
                    "--execution-config",
                    str(execution_path),
                    "--runtime-contract",
                    str(runtime_path),
                ]
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = parsed.handler(parsed)
            cli_record = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(cli_record["status"], "PREPARED")
            self.assertTrue(Path(cli_record["input_files"]["primary"]).is_file())

    def test_existing_orca_cli_exposes_prepare_only_materialization(self) -> None:
        parsed = build_parser().parse_args(
            [
                "materialize-plan",
                "--plan",
                "plan.json",
                "--node",
                "optimization",
                "--project-root",
                "/tmp/project",
                "--system",
                "system-a",
                "--execution-config",
                "execution.yaml",
                "--runtime-contract",
                "runtime.json",
            ]
        )

        self.assertEqual(parsed.command, "materialize-plan")
        self.assertEqual(parsed.handler.__name__, "_materialize_plan")


if __name__ == "__main__":
    unittest.main()
