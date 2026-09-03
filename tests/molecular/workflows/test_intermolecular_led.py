from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import tempfile
import unittest
from pathlib import Path

from cmw.core.artifacts import (
    ArtifactValidation,
    DimerEnergyArtifact,
    LEDArtifact,
    LEDFragmentReferenceArtifact,
    ValidationStatus,
    artifact_from_dict,
)
from cmw.core.execution_profiles import (
    ExecutionProfile,
    MultiwfnResourcePolicy,
    OrcaResourcePolicy,
)
from cmw.core.plan_materialization import WorkflowPlanMaterializer
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.orca.renderer import ORCA_RENDERER_ID, OrcaExecutionRenderer
from cmw.molecular.orca.status import StageType
from cmw.molecular.stacking.hole_electron import FragmentDefinition
from cmw.molecular.workflows.intermolecular_led import (
    IntermolecularLEDWorkflowError,
    LEDFragmentElectronicState,
    build_intermolecular_led_plan,
    check_intermolecular_led_reuse,
    finalize_intermolecular_led_artifact,
)
from cmw.molecular.workflows.intermolecular_led_cli import build_parser


FIXTURES = Path(__file__).parents[2] / "fixtures" / "orca" / "led"
PASSED = ArtifactValidation(
    ValidationStatus.PASSED,
    {"execution": True, "scientific": True},
    "VALID_ORCA_RESULT",
    "synthetic finalized ORCA fixture",
)


def _write_dimer(path: Path, atom_count: int = 36, *, offset: float = 0.0) -> Path:
    lines = [str(atom_count), "synthetic fixed dimer"]
    lines.extend(
        f"H {index * 0.7 + offset:.6f} 0.000000 0.000000"
        for index in range(atom_count)
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _spec(*, basis: str = "def2-TZVPP") -> OrcaStageSpec:
    return OrcaStageSpec(
        StageType.SP,
        f"DLPNO-CCSD(T) {basis} {basis}/C def2/JK RIJK TightPNO TightSCF LED",
        protocol={
            "method": "DLPNO-CCSD(T)",
            "basis": basis,
            "auxiliary_basis": {
                "correlation": f"{basis}/C",
                "coulomb_exchange": "def2/JK",
            },
            "reference_approximation": "RIJK",
            "pno": "TightPNO",
            "led": True,
            "fragments_required": True,
            "expected_fragments": 2,
        },
    )


def _fragments(atom_count: int = 36) -> tuple[FragmentDefinition, FragmentDefinition]:
    split = atom_count // 2
    return (
        FragmentDefinition("A", tuple(range(split))),
        FragmentDefinition("B", tuple(range(split, atom_count))),
    )


def _states() -> tuple[LEDFragmentElectronicState, LEDFragmentElectronicState]:
    return (
        LEDFragmentElectronicState("A", 0, 1),
        LEDFragmentElectronicState("B", 0, 1),
    )


def _finalized_parent(artifact, output: Path, attempt_id: str):
    return replace(
        artifact,
        files={"output": str(output)},
        validation=PASSED,
        provenance={
            "attempt": {
                "attempt_id": attempt_id,
                "target_id": artifact.producing_calculation,
                "executable": {
                    "name": "orca",
                    "version": "6.1.1",
                    "path": "/synthetic/orca",
                },
            }
        },
    )


class IntermolecularLEDPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.structure = structure_artifact_from_file(
            _write_dimer(self.root / "dimer.xyz"),
            source="synthetic fixed dimer",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_interaction_only_dag_has_three_sp_nodes_and_no_cp_or_other_work(self) -> None:
        plan = build_intermolecular_led_plan(
            self.structure, _fragments(), _states(), _spec()
        )

        self.assertEqual(
            [node.node_id for node in plan.execution_plan.nodes],
            [
                "led_dimer",
                "led_fragment_reference_1",
                "led_fragment_reference_2",
            ],
        )
        self.assertEqual(plan.graph.node_map["led_assembler"].kind.value, "aggregation")
        self.assertEqual(
            plan.graph.node_map["led_assembler"].dependencies,
            (
                "led_dimer",
                "led_fragment_reference_1",
                "led_fragment_reference_2",
            ),
        )
        serialized = str(plan.to_dict()).casefold()
        for excluded in (
            "cpinteractionartifact",
            "counterpoise",
            "optimizationartifact",
            "igmhartifact",
            "deformationenergyartifact",
            "association",
        ):
            self.assertNotIn(excluded, serialized)

    def test_plan_is_deterministic_and_fragment_references_have_exact_identities(
        self,
    ) -> None:
        first = build_intermolecular_led_plan(
            self.structure, _fragments(), _states(), _spec()
        )
        second = build_intermolecular_led_plan(
            self.structure, _fragments(), _states(), _spec()
        )

        self.assertEqual(
            first.execution_plan.execution_plan_id,
            second.execution_plan.execution_plan_id,
        )
        references = [
            first.artifact_templates[node_id][0]
            for node_id in (
                "led_fragment_reference_1",
                "led_fragment_reference_2",
            )
        ]
        self.assertTrue(
            all(isinstance(item, LEDFragmentReferenceArtifact) for item in references)
        )
        self.assertNotEqual(
            references[0].protocol["fragment_identity_hash"],
            references[1].protocol["fragment_identity_hash"],
        )
        self.assertEqual(references[0].protocol["active_atom_indices"], list(range(18)))
        self.assertEqual(references[0].protocol["ghost_atom_indices"], list(range(18, 36)))
        self.assertEqual(
            references[0].protocol["full_dimer_geometry_hash"],
            self.structure.geometry_hash,
        )

    def test_reference_reuse_identity_changes_with_every_scientific_input(self) -> None:
        base = build_intermolecular_led_plan(
            self.structure, _fragments(), _states(), _spec()
        )
        base_node = base.execution_plan.node_map["led_fragment_reference_1"]
        renderer = OrcaExecutionRenderer()
        base_target = renderer.build_target(base_node, (self.structure,))

        changed_structure = structure_artifact_from_file(
            _write_dimer(self.root / "changed.xyz", offset=0.01),
            source="same chemical label, changed exact geometry",
        )
        geometry_plan = build_intermolecular_led_plan(
            changed_structure, _fragments(), _states(), _spec()
        )
        geometry_target = renderer.build_target(
            geometry_plan.execution_plan.node_map["led_fragment_reference_1"],
            (changed_structure,),
        )
        charged_states = (
            LEDFragmentElectronicState("A", 1, 2),
            LEDFragmentElectronicState("B", -1, 2),
        )
        state_plan = build_intermolecular_led_plan(
            self.structure, _fragments(), charged_states, _spec()
        )
        state_target = renderer.build_target(
            state_plan.execution_plan.node_map["led_fragment_reference_1"],
            (self.structure,),
        )
        method_plan = build_intermolecular_led_plan(
            self.structure, _fragments(), _states(), _spec(basis="def2-SVP")
        )
        method_target = renderer.build_target(
            method_plan.execution_plan.node_map["led_fragment_reference_1"],
            (self.structure,),
        )

        self.assertEqual(
            len(
                {
                    base_target.target_id,
                    geometry_target.target_id,
                    state_target.target_id,
                    method_target.target_id,
                }
            ),
            4,
        )

    def test_non_contiguous_partition_renders_dimer_labels_and_ghost_basis(self) -> None:
        small = structure_artifact_from_file(
            _write_dimer(self.root / "noncontiguous.xyz", atom_count=6),
            source="synthetic non-contiguous dimer",
        )
        fragments = (
            FragmentDefinition("A", (0, 2, 4)),
            FragmentDefinition("B", (1, 3, 5)),
        )
        plan = build_intermolecular_led_plan(small, fragments, _states(), _spec())
        profile = ExecutionProfile(
            "local-test",
            OrcaResourcePolicy(2, 4.0),
            MultiwfnResourcePolicy(1, 1.0),
        )
        materializer = WorkflowPlanMaterializer(
            {ORCA_RENDERER_ID: OrcaExecutionRenderer()}
        )

        dimer = materializer.materialize_node(
            plan.execution_plan,
            "led_dimer",
            project_root=self.root,
            system_identifier="pair",
            resource_profile=profile,
            runtime_identity={"orca": {"path": "/synthetic/orca", "version": "6.1.1"}},
        )
        reference = materializer.materialize_node(
            plan.execution_plan,
            "led_fragment_reference_1",
            project_root=self.root,
            system_identifier="pair",
            resource_profile=profile,
            runtime_identity={"orca": {"path": "/synthetic/orca", "version": "6.1.1"}},
        )
        dimer_xyz = Path(dimer.input_files["geometry"]).read_text(encoding="utf-8")
        reference_xyz = Path(reference.input_files["geometry"]).read_text(
            encoding="utf-8"
        )

        self.assertEqual(dimer_xyz.count("H(1)"), 3)
        self.assertEqual(dimer_xyz.count("H(2)"), 3)
        self.assertEqual(reference_xyz.count("H:"), 3)
        expected_target = OrcaExecutionRenderer().build_target(
            plan.execution_plan.node_map["led_dimer"], (small,)
        )
        self.assertEqual(dimer.target_id, expected_target.target_id)
        self.assertEqual(dimer.layout.version.value, "v2")
        self.assertTrue(dimer.layout.target_directory.name.startswith("target-"))
        self.assertNotIn("calculation", dimer.layout.target_directory.parts)
        self.assertEqual(reference.layout.version.value, "v2")
        reference_manifest = json.loads(
            (reference.layout.target_directory / "target-manifest.json").read_text()
        )
        self.assertEqual(reference_manifest["full_target_id"], reference.target_id)
        self.assertIn(
            "! DLPNO-CCSD(T) def2-TZVPP",
            Path(dimer.input_files["primary"]).read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            " LED ",
            Path(reference.input_files["primary"]).read_text(encoding="utf-8"),
        )
        self.assertEqual(dimer.attempt.resources["nprocs"], 2)
        self.assertEqual(
            dimer.renderer_metadata["resolved_resources"][
                "derived_maxcore_mb_per_process"
            ],
            1638,
        )

    def test_assembler_remains_gated_until_all_three_results_are_valid(self) -> None:
        plan = build_intermolecular_led_plan(
            self.structure, _fragments(), _states(), _spec()
        )
        templates = plan.artifact_templates
        dimer = replace(templates["led_dimer"][0], validation=PASSED)
        reference_a = replace(
            templates["led_fragment_reference_1"][0], validation=PASSED
        )
        reference_b = replace(
            templates["led_fragment_reference_2"][0], validation=PASSED
        )

        with self.assertRaises(ValueError):
            plan.graph.validate_artifacts(
                "led_assembler",
                {
                    "led_dimer": dimer,
                    "led_fragment_reference_1": reference_a,
                },
            )
        resolved = plan.graph.validate_artifacts(
            "led_assembler",
            {
                "led_dimer": dimer,
                "led_fragment_reference_1": reference_a,
                "led_fragment_reference_2": reference_b,
            },
        )
        self.assertEqual(len(resolved), 3)

    def test_partition_must_be_disjoint_complete_and_two_fragment(self) -> None:
        invalid_partitions = (
            (FragmentDefinition("A", (0, 1)), FragmentDefinition("B", (1, 2))),
            (
                FragmentDefinition("A", tuple(range(17))),
                FragmentDefinition("B", tuple(range(18, 36))),
            ),
            (FragmentDefinition("A", tuple(range(36))),),
        )
        for fragments in invalid_partitions:
            with self.subTest(fragments=fragments):
                with self.assertRaises(IntermolecularLEDWorkflowError):
                    build_intermolecular_led_plan(
                        self.structure, fragments, _states(), _spec()
                    )

        cp_protocol = dict(_spec().protocol)
        cp_protocol["counterpoise"] = True
        with self.assertRaisesRegex(IntermolecularLEDWorkflowError, "counterpoise"):
            build_intermolecular_led_plan(
                self.structure,
                _fragments(),
                _states(),
                OrcaStageSpec(
                    StageType.SP,
                    _spec().keywords,
                    protocol=cp_protocol,
                ),
            )

    def test_cli_writes_a_generic_execution_plan_from_artifact_inputs(self) -> None:
        structure_path = self.root / "structure.json"
        fragments_path = self.root / "fragments.json"
        states_path = self.root / "states.json"
        spec_path = self.root / "orca-spec.json"
        output_path = self.root / "execution-plan.json"
        manifest_path = self.root / "led-plan.json"
        structure_path.write_text(
            json.dumps(self.structure.to_dict()), encoding="utf-8"
        )
        fragments_path.write_text(
            json.dumps([item.to_dict() for item in _fragments()]), encoding="utf-8"
        )
        states_path.write_text(
            json.dumps([item.to_dict() for item in _states()]), encoding="utf-8"
        )
        spec_path.write_text(
            json.dumps(_spec().scientific_identity()), encoding="utf-8"
        )
        parsed = build_parser().parse_args(
            [
                "plan",
                "--structure-artifact",
                str(structure_path),
                "--fragments",
                str(fragments_path),
                "--fragment-states",
                str(states_path),
                "--orca-spec",
                str(spec_path),
                "--output",
                str(output_path),
                "--manifest",
                str(manifest_path),
            ]
        )

        with redirect_stdout(StringIO()):
            self.assertEqual(parsed.handler(parsed), 0)
        plan_record = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(len(plan_record["nodes"]), 3)
        self.assertTrue(manifest_path.is_file())


class IntermolecularLEDFinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.structure = structure_artifact_from_file(
            _write_dimer(self.root / "dimer.xyz"),
            source="synthetic fixed dimer",
        )
        self.fragments = _fragments()
        self.plan = build_intermolecular_led_plan(
            self.structure, self.fragments, _states(), _spec()
        )
        outputs = {}
        for name in ("dimer", "fragment_a", "fragment_b"):
            output = self.root / f"{name}.out"
            output.write_text(
                (FIXTURES / f"h4tbapy_{name}.out").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            outputs[name] = output
        self.dimer = _finalized_parent(
            self.plan.artifact_templates["led_dimer"][0],
            outputs["dimer"],
            "attempt-dimer",
        )
        self.references = (
            _finalized_parent(
                self.plan.artifact_templates["led_fragment_reference_1"][0],
                outputs["fragment_a"],
                "attempt-fragment-a",
            ),
            _finalized_parent(
                self.plan.artifact_templates["led_fragment_reference_2"][0],
                outputs["fragment_b"],
                "attempt-fragment-b",
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _finalize(self) -> LEDArtifact:
        return finalize_intermolecular_led_artifact(
            self.structure,
            self.fragments,
            self.dimer,
            self.references,
        )

    def test_numerical_finalization_serialization_and_lineage(self) -> None:
        artifact = self._finalize()

        self.assertTrue(artifact.validation.passed)
        self.assertAlmostEqual(
            artifact.led_result["components"]["reconstructed_total_kj_mol"],
            -84.4445854506,
        )
        self.assertEqual(len(artifact.parent_artifacts), 3)
        self.assertEqual(
            artifact.metadata["full_dimer_geometry_hash"], self.structure.geometry_hash
        )
        self.assertEqual(
            set(artifact.provenance["fragment_reference_contracts"]), {"A", "B"}
        )
        restored = artifact_from_dict(artifact.to_dict())
        self.assertIsInstance(restored, LEDArtifact)
        self.assertEqual(restored.artifact_id, artifact.artifact_id)
        self.assertEqual(restored.led_result, artifact.led_result)

    def test_artifact_identity_ignores_paths_but_includes_numerical_results(self) -> None:
        artifact = self._finalize()
        relocated_result = deepcopy(artifact.led_result)
        relocated_result["dimer_raw"]["source_output_identity"][
            "source_path"
        ] = "/elsewhere/stage.out"
        relocated = replace(
            artifact,
            files={key: f"/elsewhere/{Path(value).name}" for key, value in artifact.files.items()},
            led_result=relocated_result,
        )
        changed_result = deepcopy(artifact.led_result)
        changed_result["components"]["hartree"]["exchange"] += 0.001
        changed = replace(artifact, led_result=changed_result)

        self.assertEqual(relocated.artifact_id, artifact.artifact_id)
        self.assertNotEqual(changed.artifact_id, artifact.artifact_id)

    def test_refinalization_reads_existing_outputs_without_rerun(self) -> None:
        before = {path: path.stat().st_mtime_ns for path in map(Path, self.dimer.files.values())}
        before.update(
            {
                path: path.stat().st_mtime_ns
                for reference in self.references
                for path in map(Path, reference.files.values())
            }
        )

        first = self._finalize()
        second = self._finalize()

        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in before})

    def test_exact_reuse_rejects_changed_output_structure_method_and_partition(self) -> None:
        artifact = self._finalize()
        self.assertTrue(
            check_intermolecular_led_reuse(
                artifact,
                structure=self.structure,
                fragments=self.fragments,
                parents=(self.dimer, *self.references),
            )["reuse"]
        )

        Path(self.references[1].files["output"]).write_text(
            "changed", encoding="utf-8"
        )
        self.assertFalse(
            check_intermolecular_led_reuse(
                artifact,
                structure=self.structure,
                fragments=self.fragments,
                parents=(self.dimer, *self.references),
            )["reuse"]
        )
        wrong_method = replace(self.references[0], method="DLPNO-CCSD")
        self.assertFalse(
            check_intermolecular_led_reuse(
                artifact,
                structure=self.structure,
                fragments=self.fragments,
                parents=(self.dimer, wrong_method, self.references[1]),
            )["reuse"]
        )
        wrong_structure = structure_artifact_from_file(
            _write_dimer(self.root / "wrong.xyz", offset=0.02),
            source="different structure",
        )
        self.assertFalse(
            check_intermolecular_led_reuse(
                artifact,
                structure=wrong_structure,
                fragments=self.fragments,
                parents=(self.dimer, *self.references),
            )["reuse"]
        )
        wrong_partition = (
            FragmentDefinition("A", tuple(range(0, 36, 2))),
            FragmentDefinition("B", tuple(range(1, 36, 2))),
        )
        self.assertFalse(
            check_intermolecular_led_reuse(
                artifact,
                structure=self.structure,
                fragments=wrong_partition,
                parents=(self.dimer, *self.references),
            )["reuse"]
        )

    def test_failed_parse_or_incomplete_parents_never_finalize(self) -> None:
        marker = self.root / "marker.out"
        marker.write_text(
            "Program Version 6.1.1\nLOCAL ENERGY DECOMPOSITION\n"
            "ORCA TERMINATED NORMALLY\n",
            encoding="utf-8",
        )
        bad_dimer = replace(self.dimer, files={"output": str(marker)})
        with self.assertRaises(IntermolecularLEDWorkflowError):
            finalize_intermolecular_led_artifact(
                self.structure,
                self.fragments,
                bad_dimer,
                self.references,
            )
        with self.assertRaises(IntermolecularLEDWorkflowError):
            finalize_intermolecular_led_artifact(
                self.structure,
                self.fragments,
                self.dimer,
                self.references[:1],
            )

    def test_mismatched_fragment_identity_and_attempt_provenance_fail_closed(self) -> None:
        bad_protocol = dict(self.references[0].protocol)
        bad_protocol["fragment_identity_hash"] = "0" * 64
        mismatched = replace(self.references[0], protocol=bad_protocol)
        with self.assertRaisesRegex(IntermolecularLEDWorkflowError, "identity"):
            finalize_intermolecular_led_artifact(
                self.structure,
                self.fragments,
                self.dimer,
                (mismatched, self.references[1]),
            )
        missing_attempt = replace(self.references[0], provenance={})
        with self.assertRaisesRegex(IntermolecularLEDWorkflowError, "attempt"):
            finalize_intermolecular_led_artifact(
                self.structure,
                self.fragments,
                self.dimer,
                (missing_attempt, self.references[1]),
            )

    def test_embedded_contract_records_cannot_hide_behind_matching_hashes(self) -> None:
        cases = []

        protocol = deepcopy(self.references[0].protocol)
        protocol["led_method_contract"] = {
            **protocol["led_method_contract"],
            "keywords_without_led": "DLPNO-CCSD(T) def2-SVP",
        }
        cases.append(
            ("method contracts differ", replace(self.references[0], protocol=protocol))
        )

        protocol = deepcopy(self.references[0].protocol)
        protocol["fragment_partition"] = list(reversed(protocol["fragment_partition"]))
        cases.append(
            ("partition record", replace(self.references[0], protocol=protocol))
        )

        protocol = deepcopy(self.references[0].protocol)
        protocol["ghost_atom_indices"] = list(reversed(protocol["ghost_atom_indices"]))
        cases.append(
            ("active/ghost mapping", replace(self.references[0], protocol=protocol))
        )

        protocol = deepcopy(self.references[0].protocol)
        protocol["charge"] = True
        cases.append(
            ("electronic state", replace(self.references[0], protocol=protocol))
        )

        for message, reference in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(IntermolecularLEDWorkflowError, message):
                    finalize_intermolecular_led_artifact(
                        self.structure,
                        self.fragments,
                        self.dimer,
                        (reference, self.references[1]),
                    )

    def test_cli_finalization_and_exact_reuse_use_existing_results(self) -> None:
        manifests = {
            "structure": self.structure,
            "dimer": self.dimer,
            "fragment_a": self.references[0],
            "fragment_b": self.references[1],
        }
        paths = {}
        for name, artifact in manifests.items():
            path = self.root / f"{name}.json"
            path.write_text(json.dumps(artifact.to_dict()), encoding="utf-8")
            paths[name] = path
        fragments_path = self.root / "fragments.json"
        fragments_path.write_text(
            json.dumps([item.to_dict() for item in self.fragments]), encoding="utf-8"
        )
        led_path = self.root / "led-artifact.json"
        common = [
            "--structure-artifact",
            str(paths["structure"]),
            "--fragments",
            str(fragments_path),
            "--dimer-result",
            str(paths["dimer"]),
            "--fragment-result",
            str(paths["fragment_a"]),
            "--fragment-result",
            str(paths["fragment_b"]),
        ]
        finalize = build_parser().parse_args(
            ["finalize", *common, "--output", str(led_path)]
        )
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(finalize.handler(finalize), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "FINALIZED")

        reuse = build_parser().parse_args(
            ["reuse", *common, "--led-artifact", str(led_path)]
        )
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(reuse.handler(reuse), 0)
        self.assertTrue(json.loads(output.getvalue())["reuse"])


if __name__ == "__main__":
    unittest.main()
