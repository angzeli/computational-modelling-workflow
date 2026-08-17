from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cmw.core.artifacts import (
    FrequencyArtifact,
    OptimizationArtifact,
    SinglePointArtifact,
    artifact_from_result,
)
from cmw.core.execution_contract import (
    ComputationalTask,
    ExecutionContractError,
)
from cmw.molecular.orca.input import (
    OrcaResources,
    OrcaStageSpec,
    make_target,
    parse_rendered_orca_input,
    render_orca_input,
)
from cmw.molecular.orca.status import StageType
from cmw.molecular.workflows.opt_freq_sp import legacy_workflow_graph
from cmw.structure.xyz import read_xyz


FIXTURE = Path(__file__).parents[2] / "fixtures" / "xyz" / "synthetic.xyz"


class OrcaExecutionContractTests(unittest.TestCase):
    def render(self, spec: OrcaStageSpec) -> str:
        return render_orca_input(
            geometry_path=Path("input.xyz"),
            charge=0,
            multiplicity=1,
            spec=spec,
            resources=OrcaResources(),
        )

    def test_stage_types_render_their_required_orca_operations(self) -> None:
        expectations = {
            StageType.OPT: (ComputationalTask.OPTIMIZATION, "Opt"),
            StageType.FREQ: (ComputationalTask.FREQUENCY, "Freq"),
            StageType.SP: (ComputationalTask.SINGLE_POINT, "SP"),
        }

        for stage, (task, operation) in expectations.items():
            with self.subTest(stage=stage.value):
                spec = OrcaStageSpec(stage, "HF STO-3G")
                rendered = self.render(spec)
                self.assertEqual(spec.execution_intent.task, task)
                self.assertEqual(rendered.splitlines()[0].split()[-1], operation)

    def test_intentional_stage_task_mismatch_fails_closed(self) -> None:
        mismatched = OrcaStageSpec(
            StageType.OPT,
            "HF STO-3G",
            task=ComputationalTask.SINGLE_POINT,
        )

        with self.assertRaisesRegex(
            ExecutionContractError, "FAILED_EXECUTION_CONTRACT_MISMATCH"
        ):
            self.render(mismatched)

    def test_dlpno_requires_an_explicit_correlation_auxiliary_basis(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "FAILED_PROTOCOL_MISMATCH.*auxiliary_basis.correlation"
        ):
            OrcaStageSpec(
                StageType.SP,
                "DLPNO-CCSD(T) def2-TZVPP TightPNO",
                protocol={"method": "DLPNO-CCSD(T)", "basis": "def2-TZVPP"},
            )

        valid = OrcaStageSpec(
            StageType.SP,
            "DLPNO-CCSD(T) def2-TZVPP def2-TZVPP/C TightPNO",
            protocol={
                "method": "DLPNO-CCSD(T)",
                "basis": "def2-TZVPP",
                "auxiliary_basis": {"correlation": "def2-TZVPP/C"},
            },
        )
        self.assertIn("def2-TZVPP/C", self.render(valid).splitlines()[0])

    def test_led_requires_explicit_reference_and_matching_auxiliary_basis(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "FAILED_PROTOCOL_MISMATCH.*explicit RIJK or RIJCOSX"
        ):
            OrcaStageSpec(
                StageType.SP,
                "DLPNO-CCSD(T) def2-TZVPP def2-TZVPP/C TightPNO LED",
                protocol={
                    "method": "DLPNO-CCSD(T)",
                    "auxiliary_basis": {"correlation": "def2-TZVPP/C"},
                    "led": True,
                },
            )

    def test_scientific_contract_rejects_ambiguous_protocol_types(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "FAILED_PROTOCOL_MISMATCH.*led must be a boolean"
        ):
            OrcaStageSpec(
                StageType.SP,
                "HF STO-3G",
                protocol={"led": "false"},
            )

        with self.assertRaisesRegex(
            ValueError, "FAILED_PROTOCOL_MISMATCH.*duplicate auxiliary_basis role"
        ):
            OrcaStageSpec(
                StageType.SP,
                "HF STO-3G def2-TZVPP/C",
                protocol={
                    "auxiliary_basis": {
                        "correlation": "def2-TZVPP/C",
                        "Correlation": "def2-TZVPP/C",
                    }
                },
            )

        with self.assertRaisesRegex(
            ValueError, "FAILED_PROTOCOL_MISMATCH.*coulomb_exchange"
        ):
            OrcaStageSpec(
                StageType.SP,
                "DLPNO-CCSD(T) def2-TZVPP def2-TZVPP/C RIJK TightPNO LED",
                protocol={
                    "method": "DLPNO-CCSD(T)",
                    "auxiliary_basis": {"correlation": "def2-TZVPP/C"},
                    "reference_approximation": "RIJK",
                    "led": True,
                },
            )

    def test_rendered_operation_mismatch_fails_closed_during_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stage.inp"
            path.write_text(
                self.render(OrcaStageSpec(StageType.SP, "HF STO-3G")),
                encoding="utf-8",
            )
            (path.parent / "input.xyz").write_text(
                FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                ExecutionContractError, "FAILED_EXECUTION_CONTRACT_MISMATCH"
            ):
                parse_rendered_orca_input(path, StageType.OPT)

    def test_execution_task_selects_the_matching_artifact_type(self) -> None:
        expected_types = {
            StageType.OPT: OptimizationArtifact,
            StageType.FREQ: FrequencyArtifact,
            StageType.SP: SinglePointArtifact,
        }
        geometry = read_xyz(FIXTURE)
        for stage, expected_type in expected_types.items():
            with self.subTest(stage=stage.value):
                spec = OrcaStageSpec(stage, "HF STO-3G")
                target = make_target(geometry, charge=0, multiplicity=1, spec=spec)
                artifact = artifact_from_result(
                    {
                        "target": target.to_dict(),
                        "execution_intent": spec.execution_intent.to_dict(),
                        "execution": {"status": "SUCCESS"},
                        "scientific": {"status": "VALID", "reason": "valid"},
                        "reusable": True,
                    }
                )
                self.assertIsInstance(artifact, expected_type)

    def test_artifact_mapping_rejects_mismatched_execution_intent(self) -> None:
        geometry = read_xyz(FIXTURE)
        spec = OrcaStageSpec(StageType.SP, "HF STO-3G")
        target = make_target(geometry, charge=0, multiplicity=1, spec=spec)

        with self.assertRaisesRegex(
            ExecutionContractError, "FAILED_EXECUTION_CONTRACT_MISMATCH"
        ):
            artifact_from_result(
                {
                    "target": target.to_dict(),
                    "execution_intent": {
                        "stage_type": "SP",
                        "task": "optimization",
                        "required_behavior": "Opt",
                    },
                    "execution": {"status": "SUCCESS"},
                    "scientific": {"status": "VALID", "reason": "valid"},
                    "reusable": True,
                }
            )

    def test_legacy_stage_only_contract_retains_identity_and_graph_order(self) -> None:
        legacy = OrcaStageSpec(StageType.OPT, "HF STO-3G")
        explicit = OrcaStageSpec(
            StageType.OPT,
            "HF STO-3G",
            task=ComputationalTask.OPTIMIZATION,
        )

        self.assertEqual(legacy.scientific_identity(), explicit.scientific_identity())
        graph = legacy_workflow_graph("opt+freq+sp")
        self.assertEqual(graph.topological_order(), ("OPT", "FREQ", "SP"))
        self.assertEqual(
            graph.node_map["OPT"].configuration["execution_intent"]["task"],
            "optimization",
        )


if __name__ == "__main__":
    unittest.main()
