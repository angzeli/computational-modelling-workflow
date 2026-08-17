from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from cmw.adapters.hof.execution import (
    _reused_attempt_state,
    materialize_hof_orca_node,
)


ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests/fixtures/hof"


class HofExecutionMaterializationTests(unittest.TestCase):
    def test_reused_attempt_preserves_operational_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "target" / "attempts" / "attempt_004"
            attempt.mkdir(parents=True)
            metadata = attempt / "job.json"
            metadata.write_text("{}\n", encoding="utf-8")
            command = attempt / "exact_terminal_command.sh"
            command.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (attempt / "execution-layout.json").write_text(
                json.dumps(
                    {
                        "attempt_identifier": "attempt_004",
                        "target_identifier": "target-id",
                        "workflow_node_identifier": "geometry_optimization",
                    }
                ),
                encoding="utf-8",
            )

            result = _reused_attempt_state(
                metadata,
                node_id="geometry_optimization",
                target_id="target-id",
            )

            self.assertEqual(result["status"], "REUSED")
            self.assertEqual(result["attempt_id"], "attempt_004")
            self.assertEqual(result["command_path"], str(command.resolve()))
            self.assertEqual(result["result_path"], str(metadata.resolve()))

    def test_initial_optimization_is_materialized_with_storage_protection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "hof"
            shutil.copytree(FIXTURE / "config", project / "config")
            shutil.copytree(FIXTURE / "structure", project / "structure")
            runtime = Path(temporary) / "orca-runtime.json"
            runtime.write_text("{}\n", encoding="utf-8")

            result = materialize_hof_orca_node(
                systems_path=project / "config/systems.yaml",
                methods_path=project / "config/methods.yaml",
                protocol_path=project / "config/protocol.yaml",
                execution_path=project / "config/execution.yaml",
                system_id="synthetic_hof",
                project_root=project,
                node_id="geometry_optimization",
                runtime_contract_source=runtime,
                cmw_root=ROOT,
                python_bin=Path("/usr/bin/python3"),
                orca_executable=Path("/usr/bin/true"),
            )

            attempt = Path(str(result["attempt_directory"]))
            self.assertEqual(result["status"], "PREPARED")
            self.assertIn("r2SCAN-3c Opt", (attempt / "stage.inp").read_text())
            command = (attempt / "exact_terminal_command.sh").read_text()
            self.assertIn("--minimum-free-disk-gb 100.0", command)
            self.assertIn(f"--disk-check-path {project}", command)
            self.assertIn("--artifact final_geometry=", command)
            self.assertEqual(
                json.loads((attempt / "execution-layout.json").read_text())[
                    "attempt_identifier"
                ],
                "attempt_001",
            )

            repeated = materialize_hof_orca_node(
                systems_path=project / "config/systems.yaml",
                methods_path=project / "config/methods.yaml",
                protocol_path=project / "config/protocol.yaml",
                execution_path=project / "config/execution.yaml",
                system_id="synthetic_hof",
                project_root=project,
                node_id="geometry_optimization",
                runtime_contract_source=runtime,
                cmw_root=ROOT,
                python_bin=Path("/usr/bin/python3"),
                orca_executable=Path("/usr/bin/true"),
            )
            self.assertEqual(repeated["attempt_id"], "attempt_001")
            self.assertEqual(repeated["command_path"], result["command_path"])

    def test_non_initial_node_requires_validated_parent_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "hof"
            shutil.copytree(FIXTURE / "config", project / "config")
            shutil.copytree(FIXTURE / "structure", project / "structure")
            runtime = Path(temporary) / "orca-runtime.json"
            runtime.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "validated geometry result"):
                materialize_hof_orca_node(
                    systems_path=project / "config/systems.yaml",
                    methods_path=project / "config/methods.yaml",
                    protocol_path=project / "config/protocol.yaml",
                    execution_path=project / "config/execution.yaml",
                    system_id="synthetic_hof",
                    project_root=project,
                    node_id="dimer",
                    runtime_contract_source=runtime,
                    cmw_root=ROOT,
                    python_bin=Path("/usr/bin/python3"),
                    orca_executable=Path("/usr/bin/true"),
                )


if __name__ == "__main__":
    unittest.main()
