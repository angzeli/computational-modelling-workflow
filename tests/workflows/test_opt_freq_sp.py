from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cmw.structure.xyz import geometry_hash, read_xyz


ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "scripts" / "workflows" / "run_opt_freq_sp.sh"
FAKE_ORCA = ROOT / "tests" / "fixtures" / "fake_orca.py"
STRUCTURE = ROOT / "tests" / "fixtures" / "xyz" / "synthetic.xyz"


def _config(
    path: Path,
    *,
    sp_keywords: str = "PBE0 def2-SVP",
    opt_keywords: str = "HF STO-3G",
    imaginary_tolerance_cm1: float = 5.0,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "charge": 0,
                "multiplicity": 1,
                "orca_exe": str(FAKE_ORCA),
                "resources": {"nprocs": 1, "maxcore_mb_per_process": 128},
                "validation": {
                    "require_minimum": True,
                    "imaginary_tolerance_cm1": imaginary_tolerance_cm1,
                },
                "stages": {
                    "opt": {"keywords": opt_keywords, "blocks": []},
                    "freq": {"keywords": opt_keywords, "blocks": []},
                    "sp": {"keywords": sp_keywords, "blocks": []},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class WorkflowHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw workflow space ")
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "workflow config.json"
        self.structure = self.directory / "starting structure.xyz"
        self.output = self.directory / "calculation runs"
        self.log = self.directory / "fake executions.log"
        _config(self.config)
        shutil.copyfile(STRUCTURE, self.structure)
        self.env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT / "src"),
            "FAKE_ORCA_LOG": str(self.log),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_workflow(
        self,
        mode: str,
        *,
        config: Path | None = None,
        extra: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (
                str(RUNNER),
                "--mode",
                mode,
                "--structure",
                str(self.structure),
                "--config",
                str(config or self.config),
                "--output",
                str(self.output),
                *extra,
            ),
            cwd=ROOT,
            env=env or self.env,
            check=check,
            capture_output=True,
            text=True,
        )

    def executions(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def results(self) -> list[dict[str, object]]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.output / "workflows").glob("*/workflow_result.json"))
        ]


class ModeSelectionTests(WorkflowHarness):
    def test_all_four_modes_execute_only_selected_stages_in_order(self) -> None:
        expected = {
            "opt": ["OPT"],
            "opt+sp": ["OPT", "SP"],
            "opt+freq": ["OPT", "FREQ"],
            "opt+freq+sp": ["OPT", "FREQ", "SP"],
        }
        for mode, stages in expected.items():
            with self.subTest(mode=mode):
                case = self.directory / mode.replace("+", "_")
                case.mkdir()
                original_output = self.output
                original_log = self.log
                self.output = case / "runs"
                self.log = case / "log"
                self.env["FAKE_ORCA_LOG"] = str(self.log)
                self.run_workflow(mode)
                self.assertEqual(self.executions(), stages)
                result = self.results()[0]
                self.assertTrue(result["scientifically_valid"])
                self.assertEqual(result["stage_order"], stages)
                self.output = original_output
                self.log = original_log
                self.env["FAKE_ORCA_LOG"] = str(self.log)

    def test_canonical_alias_is_accepted_but_result_uses_canonical_mode(self) -> None:
        self.run_workflow("opt-freq-sp")
        self.assertEqual(self.results()[0]["mode"], "opt+freq+sp")


class FailureAndLineageTests(WorkflowHarness):
    def test_invalid_opt_stops_all_downstream_stages(self) -> None:
        env = {**self.env, "FAKE_ORCA_FAIL_STAGE": "OPT"}
        result = self.run_workflow("opt+freq+sp", env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.executions(), ["OPT"])
        manifest = self.results()[0]
        self.assertEqual(manifest["failure_point"], "OPT")
        self.assertFalse(manifest["scientifically_valid"])

    def test_invalid_required_freq_prevents_new_sp(self) -> None:
        env = {**self.env, "FAKE_ORCA_FAIL_STAGE": "FREQ"}
        result = self.run_workflow("opt+freq+sp", env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.executions(), ["OPT", "FREQ"])
        self.assertEqual(self.results()[0]["failure_point"], "FREQ")

    def test_invalid_sp_marks_workflow_incomplete_without_corrupting_opt(self) -> None:
        env = {**self.env, "FAKE_ORCA_FAIL_STAGE": "SP"}
        result = self.run_workflow("opt+sp", env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        manifest = self.results()[0]
        self.assertEqual(manifest["stages"]["OPT"]["status"], "VALID")
        self.assertEqual(manifest["stages"]["SP"]["status"], "FAILED")

    def test_freq_and_sp_both_use_validated_opt_geometry(self) -> None:
        self.run_workflow("opt+freq+sp")
        manifest = self.results()[0]
        initial_hash = manifest["input_structure"]["geometry_sha256"]
        opt_hash = manifest["stages"]["OPT"]["artifacts"]["final_geometry"]["geometry_sha256"]
        self.assertNotEqual(initial_hash, opt_hash)
        for stage in ("FREQ", "SP"):
            record = manifest["stages"][stage]
            self.assertEqual(record["lineage"]["parent_stage"], "OPT")
            self.assertEqual(record["lineage"]["parent_artifact_sha256"], opt_hash)
            input_geometry = Path(record["artifacts"]["input"]["path"]).parent / "input.xyz"
            self.assertEqual(geometry_hash(read_xyz(input_geometry)), opt_hash)
            self.assertEqual(
                record["scientific_artifact"]["provenance"]["geometry_input"][
                    "mode"
                ],
                "xyzfile",
            )
            self.assertEqual(
                Path(record["scientific_artifact"]["files"]["input_geometry"]),
                input_geometry,
            )


class ResumeTests(WorkflowHarness):
    def test_explicit_new_attempt_retries_failed_stage(self) -> None:
        failed_env = {**self.env, "FAKE_ORCA_FAIL_STAGE": "OPT"}
        self.run_workflow("opt", env=failed_env, check=False)
        self.run_workflow("opt", extra=("--new-attempt",))
        self.assertEqual(self.executions(), ["OPT", "OPT"])
        self.assertTrue(self.results()[0]["scientifically_valid"])

    def test_valid_opt_is_reused_when_workflow_is_extended(self) -> None:
        self.run_workflow("opt")
        self.run_workflow("opt+freq")
        self.assertEqual(self.executions(), ["OPT", "FREQ"])
        extended = next(result for result in self.results() if result["mode"] == "opt+freq")
        self.assertEqual(extended["reused_stages"], ["OPT"])
        self.assertEqual(extended["executed_stages"], ["FREQ"])

    def test_valid_opt_freq_can_be_extended_to_sp(self) -> None:
        self.run_workflow("opt+freq")
        self.run_workflow("opt+freq+sp")
        self.assertEqual(self.executions(), ["OPT", "FREQ", "SP"])
        extended = next(result for result in self.results() if result["mode"] == "opt+freq+sp")
        self.assertEqual(extended["reused_stages"], ["OPT", "FREQ"])
        self.assertEqual(extended["executed_stages"], ["SP"])

    def test_changing_only_sp_settings_reuses_opt_and_freq(self) -> None:
        self.run_workflow("opt+freq+sp")
        changed = self.directory / "changed sp.json"
        _config(changed, sp_keywords="B3LYP def2-SVP")
        self.run_workflow("opt+freq+sp", config=changed)
        self.assertEqual(self.executions(), ["OPT", "FREQ", "SP", "SP"])
        changed_result = next(
            result
            for result in self.results()
            if result["stages"]["SP"]["settings"]["keywords"] == "B3LYP def2-SVP"
        )
        self.assertEqual(changed_result["reused_stages"], ["OPT", "FREQ"])

    def test_changing_frequency_policy_prevents_frequency_reuse(self) -> None:
        self.run_workflow("opt+freq")
        changed = self.directory / "changed frequency policy.json"
        _config(changed, imaginary_tolerance_cm1=10.0)
        self.run_workflow("opt+freq", config=changed)
        self.assertEqual(self.executions(), ["OPT", "FREQ", "FREQ"])
        changed_result = next(
            result
            for result in self.results()
            if result["validation"]["imaginary_tolerance_cm1"] == 10.0
        )
        self.assertEqual(changed_result["reused_stages"], ["OPT"])

    def test_changed_opt_target_is_not_reused(self) -> None:
        self.run_workflow("opt")
        changed = self.directory / "changed opt.json"
        _config(changed, opt_keywords="B3LYP STO-3G")
        self.run_workflow("opt", config=changed)
        self.assertEqual(self.executions(), ["OPT", "OPT"])

    def test_artifact_mismatch_fails_closed_and_creates_new_attempt(self) -> None:
        self.run_workflow("opt")
        first = self.results()[0]
        output = Path(first["stages"]["OPT"]["artifacts"]["output"]["path"])
        output.write_text("tampered\n", encoding="utf-8")
        self.run_workflow("opt+freq")
        self.assertEqual(self.executions(), ["OPT", "OPT", "FREQ"])
        target_dir = output.parents[2]
        self.assertEqual(len(list((target_dir / "attempts").iterdir())), 2)


class PlanningTests(WorkflowHarness):
    def test_plan_is_non_executing_and_shows_skips_dependencies_and_overrides(self) -> None:
        env = {**self.env, "NPROCS": "3"}
        completed = self.run_workflow(
            "opt+sp",
            extra=("--plan", "--json", "--nprocs", "4"),
            env=env,
        )
        plan = json.loads(completed.stdout)
        self.assertEqual(plan["resources"]["nprocs"], 4)
        self.assertEqual(plan["stages"]["FREQ"]["disposition"], "skipped")
        self.assertEqual(plan["stages"]["SP"]["depends_on"], ["OPT"])
        self.assertEqual(self.executions(), [])
        self.assertFalse(self.output.exists())

    def test_plan_reports_existing_reuse_when_determinable(self) -> None:
        self.run_workflow("opt")
        completed = self.run_workflow("opt+freq", extra=("--plan", "--json"))
        plan = json.loads(completed.stdout)
        self.assertEqual(plan["stages"]["OPT"]["disposition"], "reuse")
        self.assertEqual(plan["stages"]["FREQ"]["disposition"], "run")


if __name__ == "__main__":
    unittest.main()
