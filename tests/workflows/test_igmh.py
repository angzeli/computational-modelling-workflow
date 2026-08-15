from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.workflows.test_multiwfn_cubes import FAKE, MultiwfnCubeHarness, ROOT


IGMH = ROOT / "scripts" / "workflows" / "generate_igmh_cubes.sh"


class IgmhWorkflowTests(MultiwfnCubeHarness):
    def setUp(self) -> None:
        super().setUp()
        self.fragments = self.root / "fragment definition.json"
        self.config = self.root / "igmh config.json"
        self.write_fragments([1], [2, 3])
        self.write_config(0.2)

    def write_fragments(
        self,
        fragment_a: list[int],
        fragment_b: list[int],
        *,
        overlap: bool = False,
        complete: bool = True,
        indexing: str = "one_based",
    ) -> None:
        self.fragments.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "indexing": indexing,
                    "allow_overlap": overlap,
                    "require_complete_partition": complete,
                    "fragments": {"A": fragment_a, "B": fragment_b},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def write_config(
        self, spacing: float, *, outputs: list[dict[str, object]] | None = None
    ) -> None:
        record: dict[str, object] = {
            "schema_version": 1,
            "profile": "interfragment",
            "grid_spacing_bohr": spacing,
            "cube_generation": True,
            "visualization": {
                "format": "cube",
                "recommended_isovalue": 0.01,
            },
        }
        if outputs is not None:
            record["outputs"] = outputs
        self.config.write_text(
            json.dumps(record, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_igmh(
        self,
        *,
        extra: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        executable: Path = FAKE,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (
                str(IGMH),
                "--source",
                str(self.source),
                "--fragments",
                str(self.fragments),
                "--config",
                str(self.config),
                "--output",
                str(self.output),
                "--multiwfn-exe",
                str(executable),
                "--settings-source",
                str(self.settings),
                *extra,
            ),
            cwd=ROOT,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=check,
        )

    def test_valid_fragments_generate_provenance_rich_cube_pair(self) -> None:
        result = json.loads(self.run_igmh().stdout)
        self.assertEqual(
            set(result["required_artifact_roles"]),
            {"delta_g_inter_cube", "sign_lambda2_rho_cube"},
        )
        calculation = result["target"]["calculation"]
        self.assertEqual(calculation["fragment_a"], [1])
        self.assertEqual(calculation["fragment_b"], [2, 3])
        self.assertEqual(calculation["indexing"], "one_based")
        self.assertEqual(calculation["grid_spacing_bohr"], 0.2)
        self.assertEqual(result["runtime"]["requested_nthreads"], 8)
        self.assertEqual(result["scientific_artifact"]["artifact_type"], "IGMHArtifact")
        self.assertEqual(
            result["source_density_artifact"]["artifact_type"], "DensityArtifact"
        )
        self.assertEqual(
            result["scientific_artifact"]["parent_artifacts"],
            [result["source_density_artifact"]["artifact_id"]],
        )
        provenance = result["scientific_artifact"]["provenance"]
        self.assertEqual(provenance["generating_program"], "Multiwfn")
        self.assertEqual(provenance["multiwfn_version"], "3.8")
        self.assertIn("source_wavefunction", provenance["input_files"])
        self.assertEqual(
            set(provenance["output_files"]),
            {"delta_g_inter_cube", "sign_lambda2_rho_cube", "metadata"},
        )
        self.assertEqual(provenance["visualization"]["recommended_isovalue"], 0.01)
        self.assertEqual(
            result["scientific_artifact"]["validation"]["status"], "PASSED"
        )
        self.assertEqual(
            result["workflow_graph"]["nodes"][1]["produces"], ["IGMHArtifact"]
        )

    def test_configured_output_paths_are_discovered_without_public_name_assumptions(
        self,
    ) -> None:
        self.write_config(
            0.2,
            outputs=[
                {
                    "role": "delta_g_inter_cube",
                    "raw_path": "dg_inter.cub",
                    "output_path": "cubes/interaction-field.cube",
                },
                {
                    "role": "sign_lambda2_rho_cube",
                    "raw_path": "sl2r.cub",
                    "output_path": "cubes/signed-density.cube",
                },
                {
                    "role": "delta_g_intra_cube",
                    "raw_path": "dg_intra.cub",
                    "output_path": "cubes/intrafragment-field.cube",
                    "required": False,
                },
            ],
        )

        result = json.loads(self.run_igmh().stdout)
        attempt = next((self.output / "igmh").glob("*/attempts/*"))
        self.assertTrue((attempt / "cubes/interaction-field.cube").is_file())
        self.assertTrue((attempt / "cubes/signed-density.cube").is_file())
        self.assertFalse((attempt / "cubes/intrafragment-field.cube").exists())
        self.assertEqual(
            set(result["required_artifact_roles"]),
            {"delta_g_inter_cube", "sign_lambda2_rho_cube"},
        )
        command_outputs = {item["role"]: item for item in result["command"]["outputs"]}
        self.assertEqual(
            command_outputs["delta_g_inter_cube"]["output_path"],
            "cubes/interaction-field.cube",
        )

    def test_invalid_index_overlap_and_incomplete_partition_fail(self) -> None:
        cases = (
            ("invalid atom index", [1], [2, 4], False, True),
            ("overlap", [1, 2], [2, 3], False, True),
            ("incomplete partition", [1], [2], False, True),
        )
        for expected, first, second, overlap, complete in cases:
            with self.subTest(expected=expected):
                self.write_fragments(first, second, overlap=overlap, complete=complete)
                completed = self.run_igmh(extra=("--plan",), check=False)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(expected, completed.stdout)
        self.assertFalse(self.output.exists())

    def test_fragment_and_grid_changes_create_new_targets(self) -> None:
        self.run_igmh()
        self.write_fragments([1, 2], [3])
        self.run_igmh()
        self.write_config(0.3)
        self.run_igmh()
        self.assertEqual(len(self.results("igmh")), 3)
        self.assertEqual(self.log.read_text().splitlines().count("IGMH"), 3)

    def test_unchanged_target_reuses_both_outputs(self) -> None:
        self.run_igmh()
        self.run_igmh()
        self.assertEqual(self.log.read_text().splitlines().count("IGMH"), 1)

    def test_missing_malformed_and_incompatible_cubes_fail(self) -> None:
        cases = (
            ("missing", {"FAKE_MULTIWFN_MISSING": "dg_inter"}),
            ("malformed", {"FAKE_MULTIWFN_MALFORMED": "sl2r"}),
            ("incompatible", {"FAKE_MULTIWFN_MISMATCH_GRID": "1"}),
        )
        for label, extra_env in cases:
            with self.subTest(label=label):
                self.output = self.root / f"{label} output"
                completed = self.run_igmh(env={**self.env, **extra_env}, check=False)
                self.assertNotEqual(completed.returncode, 0)
                if label == "missing":
                    self.assertIn("MISSING_REQUIRED_OUTPUT", completed.stdout)
                self.assertEqual(self.results("igmh"), [])

    def test_changed_source_wavefunction_fails_closed_until_manifest_updates(
        self,
    ) -> None:
        self.wavefunction.write_text("changed without provenance\n", encoding="utf-8")
        completed = self.run_igmh(extra=("--plan",), check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("integrity mismatch", completed.stdout)

    def test_four_thread_override_and_long_child_health_monitor(self) -> None:
        env = {
            **self.env,
            "MULTIWFN_NTHREADS": "4",
            "FAKE_MULTIWFN_CHILD_SECONDS": "0.7",
            "CMW_HEALTH_INTERVAL": "0.1",
        }
        result = json.loads(self.run_igmh(env=env).stdout)
        self.assertEqual(result["runtime"]["requested_nthreads"], 4)
        attempt = next((self.output / "igmh").glob("*/attempts/*"))
        health = json.loads((attempt / "health-latest.json").read_text())
        self.assertTrue(health["diagnostic_only"])
        history = [
            json.loads(line)
            for line in (attempt / "health-history.jsonl").read_text().splitlines()
        ]
        self.assertGreaterEqual(len(history), 2)
        if all("sampling unavailable" not in sample["reason"] for sample in history):
            self.assertTrue(any(sample["process_count"] >= 1 for sample in history))
        else:
            self.assertTrue(all(sample["state"] == "UNKNOWN" for sample in history))
        self.assertIn(
            health["state"], {"ACTIVE", "WAITING", "UNKNOWN", "POSSIBLY_STALLED"}
        )
        self.assertEqual(result["scientific"]["status"], "VALID")

    def test_plan_shows_fragments_indexing_outputs_and_no_menu_numbers(self) -> None:
        plan = json.loads(self.run_igmh(extra=("--plan",)).stdout)
        self.assertEqual(plan["configuration"]["fragment_a"], [1])
        self.assertEqual(plan["configuration"]["indexing"], "one_based")
        self.assertEqual(plan["expected_artifacts"], ["dg_inter.cub", "sl2r.cub"])
        self.assertNotIn("menu", json.dumps(plan).lower())
        self.assertFalse(self.output.exists())

    def test_unsupported_version_fails_before_operation(self) -> None:
        unsupported = self.root / "unsupported Multiwfn"
        unsupported.write_text(
            FAKE.read_text(encoding="utf-8").replace(
                'print("Version 3.8")', 'print("Version 3.9")'
            ),
            encoding="utf-8",
        )
        unsupported.chmod(0o755)
        completed = self.run_igmh(executable=unsupported, check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("supported series is 3.8", completed.stdout)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
