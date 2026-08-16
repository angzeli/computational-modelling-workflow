from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cmw.core.job import JobTarget
from cmw.core.provenance import ArtifactRecord, file_hash
from cmw.structure.xyz import geometry_hash, read_xyz


ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "tests" / "fixtures" / "fake_multiwfn.py"
STRUCTURE = ROOT / "tests" / "fixtures" / "xyz" / "synthetic.xyz"
FMO = ROOT / "scripts" / "workflows" / "generate_homo_lumo_cubes.sh"
ESP = ROOT / "scripts" / "workflows" / "generate_esp_cubes.sh"


class MultiwfnCubeHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw cubes space ")
        self.root = Path(self.temporary.name)
        self.source_dir = self.root / "validated source"
        self.source_dir.mkdir()
        self.geometry = self.source_dir / "source geometry.xyz"
        shutil.copyfile(STRUCTURE, self.geometry)
        self.wavefunction = self.source_dir / "source wavefunction.molden.input"
        self.wavefunction.write_text("[Molden Format]\nsynthetic only\n", encoding="utf-8")
        self.settings = self.root / "settings source.ini"
        self.settings.write_text("nthreads= 2\n", encoding="utf-8")
        self.source = self.source_dir / "orca result.json"
        self.output = self.root / "analysis output"
        self.log = self.root / "fake launches.log"
        self.write_source()
        self.env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT / "src"),
            "FAKE_MULTIWFN_LOG": str(self.log),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_source(
        self,
        *,
        spin_mode: str = "restricted",
        multiplicity: int = 1,
        geometry_identity: str | None = None,
    ) -> None:
        identity = geometry_identity or geometry_hash(read_xyz(self.geometry))
        target = JobTarget(
            stage_type="SP",
            geometry_sha256=identity,
            charge=0,
            multiplicity=multiplicity,
            calculation={"keywords": "synthetic"},
        )
        artifacts = {
            role: ArtifactRecord.from_path(path, role=role, relative_to=self.source_dir).__dict__
            for role, path in {"wavefunction": self.wavefunction, "geometry": self.geometry}.items()
        }
        self.source.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": target.to_dict(),
                    "lineage": {
                        "source": "synthetic_public_fixture",
                        "geometry_sha256": identity,
                        "parent_stage": None,
                        "parent_target_id": None,
                        "parent_artifact_sha256": None,
                    },
                    "execution": {"status": "SUCCESS"},
                    "scientific": {"status": "VALID"},
                    "artifacts": artifacts,
                    "wavefunction_semantics": {
                        "spin_mode": spin_mode,
                        "format": "molden",
                        "homo_index": 5,
                        "lumo_index": 6,
                        "orbital_indexing": "one_based",
                    },
                    "reusable": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def run_workflow(
        self,
        script: Path,
        *,
        spacing: float = 0.2,
        extra: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (
                str(script),
                "--source",
                str(self.source),
                "--output",
                str(self.output),
                "--grid-spacing-bohr",
                str(spacing),
                "--multiwfn-exe",
                str(FAKE),
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

    def results(self, operation: str) -> list[dict[str, object]]:
        return [
            json.loads(path.read_text())
            for path in sorted(
                (self.output / "calculation").glob(f"*/{operation}/*/result.json")
            )
        ]


class FmoWorkflowTests(MultiwfnCubeHarness):
    def test_validated_source_produces_homo_lumo_and_reuses(self) -> None:
        first = self.run_workflow(FMO)
        result = json.loads(first.stdout)
        self.assertEqual(set(result["required_artifact_roles"]), {"homo_cube", "lumo_cube"})
        self.assertEqual(
            result["scientific_artifact"]["artifact_type"], "AnalysisArtifact"
        )
        self.assertEqual(
            result["scientific_artifact"]["validation"]["status"], "PASSED"
        )
        self.assertEqual(result["target"]["calculation"]["homo_index"], 5)
        self.assertEqual(result["target"]["calculation"]["lumo_index"], 6)
        self.run_workflow(FMO)
        self.assertEqual(self.log.read_text().splitlines().count("FMO"), 1)

    def test_changed_wavefunction_creates_new_target(self) -> None:
        self.run_workflow(FMO)
        self.wavefunction.write_text("[Molden Format]\nchanged synthetic\n", encoding="utf-8")
        self.write_source()
        self.run_workflow(FMO)
        self.assertEqual(len(self.results("fmo")), 2)

    def test_open_shell_fails_without_guessing(self) -> None:
        self.write_source(spin_mode="unrestricted", multiplicity=2)
        completed = self.run_workflow(FMO, extra=("--plan",), check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("open-shell/unrestricted", completed.stdout)
        self.assertFalse(self.output.exists())

    def test_wrong_lineage_and_missing_cube_fail_closed(self) -> None:
        self.write_source(geometry_identity="0" * 64)
        wrong = self.run_workflow(FMO, extra=("--plan",), check=False)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("lineage", wrong.stdout)
        self.write_source()
        for role in ("homo", "lumo"):
            with self.subTest(missing=role):
                env = {**self.env, "FAKE_MULTIWFN_MISSING": role}
                missing = self.run_workflow(FMO, env=env, check=False)
                self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(self.results("fmo"), [])

    def test_malformed_cube_fails(self) -> None:
        completed = self.run_workflow(
            FMO, env={**self.env, "FAKE_MULTIWFN_MALFORMED": "lumo"}, check=False
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("truncated cube", completed.stdout)

    def test_changed_cube_hash_prevents_reuse(self) -> None:
        self.run_workflow(FMO)
        result_path = next(
            (self.output / "calculation").glob("*/fmo/*/result.json")
        )
        record = json.loads(result_path.read_text())
        cube = Path(record["scientific_artifact"]["files"]["homo_cube"])
        cube.write_text(cube.read_text() + " 0.8\n", encoding="utf-8")
        self.run_workflow(FMO)
        self.assertEqual(self.log.read_text().splitlines().count("FMO"), 2)
        self.assertEqual(len(list((result_path.parent / "attempts").iterdir())), 2)


class EspWorkflowTests(MultiwfnCubeHarness):
    def test_density_esp_pair_and_four_thread_override(self) -> None:
        result = json.loads(
            self.run_workflow(ESP, env={**self.env, "MULTIWFN_NTHREADS": "4"}).stdout
        )
        self.assertEqual(set(result["required_artifact_roles"]), {"density_cube", "esp_cube"})
        self.assertEqual(result["runtime"]["requested_nthreads"], 4)
        self.assertIn("threads=4", self.log.read_text())

    def test_mismatching_grid_and_missing_partner_fail(self) -> None:
        mismatch = self.run_workflow(
            ESP,
            env={**self.env, "FAKE_MULTIWFN_MISMATCH_GRID": "1"},
            check=False,
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("origins differ", mismatch.stdout)
        second_output = self.root / "second output"
        self.output = second_output
        missing = self.run_workflow(
            ESP, env={**self.env, "FAKE_MULTIWFN_MISSING": "esp"}, check=False
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_grid_change_creates_sibling_target_and_unchanged_reuses(self) -> None:
        self.run_workflow(ESP, spacing=0.2)
        self.run_workflow(ESP, spacing=0.2)
        self.run_workflow(ESP, spacing=0.3)
        self.assertEqual(len(self.results("esp")), 2)
        self.assertEqual(self.log.read_text().splitlines().count("ESP"), 2)

    def test_plan_is_nonexecuting_and_machine_readable(self) -> None:
        plan = json.loads(self.run_workflow(ESP, extra=("--plan",)).stdout)
        self.assertEqual(plan["operation"], "ESP")
        self.assertEqual(plan["runtime"]["requested_nthreads"], 8)
        self.assertEqual(plan["expected_artifacts"], ["density.cube", "esp.cube"])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
