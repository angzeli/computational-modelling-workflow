from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cmw.core.job import ExecutionAttempt, GeometryLineage, JobTarget
from cmw.core.provenance import atomic_write_json, git_state, read_json, stable_hash
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
    def _attempt(self, directory: Path, stage: StageType = StageType.OPT):
        geometry = read_xyz(FIXTURE)
        spec = OrcaStageSpec(stage, "HF STO-3G")
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
        output.write_text(_valid_output(stage), encoding="utf-8")
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
