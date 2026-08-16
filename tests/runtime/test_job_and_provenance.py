from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cmw.core.execution_layout import ExecutionLayout
from cmw.core.job import ExecutionAttempt, GeometryLineage, JobTarget
from cmw.core.provenance import atomic_write_json, git_state, read_json, stable_hash
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.molecular.orca.geometry import prepare_orca_geometry_input
from cmw.molecular.orca.input import OrcaResources, OrcaStageSpec, make_target, render_orca_input
from cmw.molecular.orca.job import check_reuse, finalize_attempt, write_target
from cmw.molecular.orca.status import StageType
from cmw.structure.xyz import geometry_hash, read_xyz, write_xyz


FIXTURE = Path(__file__).parents[1] / "fixtures" / "xyz" / "synthetic.xyz"


def _valid_output(stage: StageType) -> str:
    rows = [
        "Program Version 6.0.1",
        "SCF CONVERGED AFTER 6 CYCLES",
        "FINAL SINGLE POINT ENERGY -3.000000000",
    ]
    if stage is StageType.OPT:
        rows.append("THE OPTIMIZATION HAS CONVERGED")
    if stage is StageType.FREQ:
        rows.extend(("VIBRATIONAL FREQUENCIES", "  0: 0.00 cm**-1", "  1: 100.00 cm**-1"))
    rows.append("ORCA TERMINATED NORMALLY")
    return "\n".join(rows) + "\n"


class IdentityTests(unittest.TestCase):
    def test_stage_spec_rejects_duplicate_control_directives(self) -> None:
        with self.assertRaisesRegex(ValueError, "stage-control"):
            OrcaStageSpec(StageType.OPT, "HF Opt")
        with self.assertRaisesRegex(ValueError, "cannot be duplicated"):
            OrcaStageSpec(StageType.SP, "HF", ("%scf\n  MaxIter 100\nend\n%pal 8 end",))

    def test_stable_hash_ignores_mapping_order(self) -> None:
        self.assertEqual(stable_hash({"a": 1, "b": 2}), stable_hash({"b": 2, "a": 1}))

    def test_target_excludes_attempt_resources(self) -> None:
        target = JobTarget("SP", "a" * 64, 0, 1, {"keywords": "synthetic"})
        first = ExecutionAttempt.create(
            target_id=target.target_id,
            resources={"nprocs": 1, "maxcore_mb_per_process": 1000},
            executable={"version": "1"},
            generated_input_sha256="b" * 64,
        )
        second = ExecutionAttempt.create(
            target_id=target.target_id,
            resources={"nprocs": 8},
            executable={"version": "2"},
            generated_input_sha256="b" * 64,
        )
        self.assertEqual(first.target_id, second.target_id)
        self.assertNotEqual(first.attempt_id, second.attempt_id)

    def test_geometry_lineage_fails_on_partial_or_mismatched_parent(self) -> None:
        with self.assertRaises(ValueError):
            GeometryLineage("optimized", "a" * 64, parent_stage="OPT")
        with self.assertRaises(ValueError):
            GeometryLineage(
                "optimized", "a" * 64, "OPT", "b" * 64, "c" * 64
            )

    def test_atomic_metadata_and_optional_non_git_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "record.json"
            atomic_write_json(path, {"schema_version": 1, "value": "ok"})
            self.assertEqual(read_json(path)["value"], "ok")
            state = git_state(Path(temporary))
        self.assertIsNotNone(state)
        self.assertFalse(state["available"])


class ReuseTests(unittest.TestCase):
    def _attempt(
        self,
        directory: Path,
        stage: StageType = StageType.OPT,
        *,
        keywords: str = "HF STO-3G",
        protocol: dict[str, object] | None = None,
        output_text: str | None = None,
    ):
        geometry = read_xyz(FIXTURE)
        spec = OrcaStageSpec(stage, keywords, protocol=protocol or {})
        target = make_target(geometry, charge=0, multiplicity=1, spec=spec)
        input_path = directory / "stage.inp"
        input_geometry = directory / "input.xyz"
        write_xyz(input_geometry, geometry)
        input_path.write_text(
            render_orca_input(
                geometry_path=Path("input.xyz"),
                charge=0,
                multiplicity=1,
                spec=spec,
                resources=OrcaResources(),
            ),
            encoding="utf-8",
        )
        target_path = directory / "target.json"
        write_target(
            target_path, target, GeometryLineage("input_structure", geometry_hash(geometry))
        )
        output = directory / "stage.out"
        error = directory / "stage.err"
        output.write_text(output_text or _valid_output(stage), encoding="utf-8")
        error.write_text("", encoding="utf-8")
        artifacts = {}
        if stage is StageType.OPT:
            final_geometry = directory / "stage.xyz"
            write_xyz(final_geometry, geometry)
            artifacts["final_geometry"] = final_geometry
        metadata = directory / "job.json"
        record = finalize_attempt(
            target_path=target_path,
            metadata_path=metadata,
            input_path=input_path,
            output_path=output,
            stderr_path=error,
            process_exit_code=0,
            executable={"path": "synthetic-orca", "version": "test"},
            resources={"nprocs": 1, "maxcore_mb_per_process": 1000},
            artifacts=artifacts,
        )
        return target_path, metadata, output, record

    def test_valid_exact_attempt_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target, metadata, _, record = self._attempt(Path(temporary))
            result = check_reuse(target, metadata)
        self.assertTrue(record["reusable"])
        self.assertTrue(result["reuse"])
        self.assertEqual(
            record["scientific_artifact"]["artifact_type"], "OptimizationArtifact"
        )
        self.assertEqual(
            record["execution_intent"],
            {
                "stage_type": "OPT",
                "task": "optimization",
                "required_behavior": "Opt",
            },
        )
        self.assertEqual(
            record["scientific_artifact"]["validation"]["status"], "PASSED"
        )
        self.assertEqual(record["geometry_input"]["mode"], "legacy_xyzfile")

    def test_layout_identity_and_resolved_paths_reach_artifact_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            geometry = read_xyz(FIXTURE)
            spec = OrcaStageSpec(StageType.OPT, "HF STO-3G")
            target = make_target(geometry, charge=0, multiplicity=1, spec=spec)
            layout = ExecutionLayout(
                root,
                "system-a",
                "optimization",
                target.target_id,
                "attempt_001",
            )
            layout.create_working_directory()
            target_path = layout.target_directory / "target.json"
            write_target(
                target_path,
                target,
                GeometryLineage("input_structure", geometry_hash(geometry)),
            )
            input_geometry = layout.input_path("input.xyz")
            structure_artifact = structure_artifact_from_file(
                FIXTURE.resolve(),
                source="synthetic fixture",
                charge=0,
                multiplicity=1,
            )
            geometry_input = prepare_orca_geometry_input(
                structure_artifact,
                input_geometry,
                provenance={"workflow_node": "optimization"},
            )
            geometry_contract = layout.metadata_path("geometry-input.json")
            atomic_write_json(geometry_contract, geometry_input.to_dict())
            input_path = layout.input_path("stage.inp")
            input_path.write_text(
                render_orca_input(
                    geometry_input=geometry_input,
                    charge=0,
                    multiplicity=1,
                    spec=spec,
                    resources=OrcaResources(),
                ),
                encoding="utf-8",
            )
            output_path = layout.output_path("stage.out")
            output_path.write_text(_valid_output(StageType.OPT), encoding="utf-8")
            stderr_path = layout.log_path("stage.err")
            stderr_path.write_text("", encoding="utf-8")
            final_geometry = layout.output_path("stage.xyz")
            write_xyz(final_geometry, geometry)
            metadata_path = layout.metadata_path("job.json")

            record = finalize_attempt(
                target_path=target_path,
                metadata_path=metadata_path,
                input_path=input_path,
                output_path=output_path,
                stderr_path=stderr_path,
                process_exit_code=0,
                executable={"path": "synthetic-orca", "version": "test"},
                resources=OrcaResources().to_dict(),
                artifacts={"final_geometry": final_geometry},
                execution_layout=layout,
                geometry_input=geometry_input,
                geometry_contract_path=geometry_contract,
            )
            reuse = check_reuse(target_path, metadata_path)

        artifact = record["scientific_artifact"]
        self.assertEqual(record["attempt"]["attempt_id"], "attempt_001")
        self.assertEqual(artifact["files"]["output"], str(output_path))
        self.assertEqual(artifact["provenance"]["attempt_id"], "attempt_001")
        self.assertEqual(
            artifact["provenance"]["geometry_input"][
                "source_structure_artifact_id"
            ],
            structure_artifact.artifact_id,
        )
        self.assertEqual(
            artifact["parent_artifacts"], [structure_artifact.artifact_id]
        )
        self.assertEqual(
            artifact["files"]["input_geometry"], str(input_geometry)
        )
        self.assertEqual(
            artifact["files"]["geometry_contract"], str(geometry_contract)
        )
        self.assertEqual(
            artifact["provenance"]["producing_execution_node"], "optimization"
        )
        self.assertEqual(
            artifact["provenance"]["resolved_output_path"],
            str(layout.output_directory),
        )
        self.assertTrue(reuse["reuse"])

    def test_declared_protocol_mismatch_is_not_reusable(self) -> None:
        wrong = (Path(__file__).parents[1] / "fixtures" / "orca" / "protocol_wrong_method.out").read_text(
            encoding="utf-8"
        )
        with tempfile.TemporaryDirectory() as temporary:
            target, metadata, _, record = self._attempt(
                Path(temporary),
                StageType.SP,
                keywords="DLPNO-CCSD(T) def2-TZVPP TightPNO",
                protocol={
                    "method": "DLPNO-CCSD(T)",
                    "basis": "def2-TZVPP",
                    "pno": "TightPNO",
                    "led": True,
                },
                output_text=wrong,
            )
            result = check_reuse(target, metadata)
        self.assertFalse(record["reusable"])
        self.assertEqual(record["validation"]["status"], "FAILED_PROTOCOL_MISMATCH")
        self.assertEqual(record["scientific"]["status"], "INVALID")
        self.assertIn("FAILED_PROTOCOL_MISMATCH", record["scientific"]["reason"])
        typed = record["scientific_artifact"]
        self.assertEqual(typed["validation"]["status"], "FAILED")
        self.assertFalse(typed["validation"]["checks"]["method_match"])
        self.assertEqual(
            typed["provenance"]["validation"]["status"],
            "FAILED_PROTOCOL_MISMATCH",
        )
        self.assertEqual(result["code"], "SCIENTIFICALLY_INVALID")

    def test_target_mismatch_prevents_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _, metadata, _, _ = self._attempt(directory)
            geometry = read_xyz(FIXTURE)
            changed = make_target(
                geometry,
                charge=1,
                multiplicity=2,
                spec=OrcaStageSpec(StageType.OPT, "HF STO-3G"),
            )
            changed_target = directory / "changed-target.json"
            write_target(
                changed_target,
                changed,
                GeometryLineage("input_structure", geometry_hash(geometry)),
            )
            result = check_reuse(changed_target, metadata)
        self.assertEqual(result["code"], "TARGET_MISMATCH")

    def test_artifact_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target, metadata, output, _ = self._attempt(Path(temporary))
            output.write_text("tampered", encoding="utf-8")
            result = check_reuse(target, metadata)
        self.assertEqual(result["code"], "ARTIFACT_MISMATCH")

    def test_scientifically_invalid_attempt_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target, metadata, output, _ = self._attempt(directory)
            output.write_text("ORCA TERMINATED NORMALLY\n", encoding="utf-8")
            error = directory / "stage.err"
            record = finalize_attempt(
                target_path=target,
                metadata_path=metadata,
                input_path=directory / "stage.inp",
                output_path=output,
                stderr_path=error,
                process_exit_code=0,
                executable={"path": "synthetic-orca"},
                resources={"nprocs": 1, "maxcore_mb_per_process": 1000},
                artifacts={"final_geometry": directory / "stage.xyz"},
            )
            result = check_reuse(target, metadata)
        self.assertFalse(record["reusable"])
        self.assertEqual(result["code"], "SCIENTIFICALLY_INVALID")

    def test_generated_input_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target, metadata, _, _ = self._attempt(directory)
            (directory / "stage.inp").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "rendered-input contract"):
                finalize_attempt(
                    target_path=target,
                    metadata_path=metadata,
                    input_path=directory / "stage.inp",
                    output_path=directory / "stage.out",
                    stderr_path=directory / "stage.err",
                    process_exit_code=0,
                    executable={},
                    resources={},
                    artifacts={"final_geometry": directory / "stage.xyz"},
                )


if __name__ == "__main__":
    unittest.main()
