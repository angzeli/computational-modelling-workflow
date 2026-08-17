from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from cmw.adapters.hof.batch_queue import (
    authorize_hof_batch_queue,
    generate_hof_batch_queue,
    verify_hof_batch_queue,
)
from cmw.core.provenance import file_hash, stable_hash


ROOT = Path(__file__).parents[3]


class HofBatchQueueTests(unittest.TestCase):
    def _child(self, root: Path, system_id: str) -> Path:
        directory = root / system_id
        preflight = root / f"{system_id}-preflight"
        steps = directory / "steps"
        steps.mkdir(parents=True)
        preflight.mkdir()
        script = steps / "01_step.sh"
        script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        script.chmod(0o750)
        source = root / f"{system_id}.source"
        source.write_text(system_id, encoding="utf-8")
        package_manifest = preflight / "package-manifest.json"
        package_manifest.write_text(
            json.dumps(
                {
                    "storage_capacity": {
                        "check_path": str(root),
                        "minimum_free_gib": 1.0,
                    }
                }
            ),
            encoding="utf-8",
        )
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        step = {
            "id": "step",
            "kind": "ORCA",
            "script": "steps/01_step.sh",
            "script_size_bytes": script.stat().st_size,
            "script_sha256": file_hash(script),
        }
        identity = {
            "schema_version": 1,
            "system_id": system_id,
            "steps": [step],
            "source_files": {
                str(source): {
                    "size_bytes": source.stat().st_size,
                    "sha256": file_hash(source),
                }
            },
            "repositories": {
                "cmw": {"path": str(ROOT), "head": head},
                "hof": {"path": str(ROOT), "head": head},
            },
            "preflight_directory": str(preflight),
            "preflight_manifest_sha256": file_hash(package_manifest),
        }
        queue = {
            **identity,
            "queue_id": stable_hash(identity),
            "status": "PREPARED_NOT_AUTHORIZED",
        }
        (directory / "command-queue.json").write_text(
            json.dumps(queue), encoding="utf-8"
        )
        (directory / "queue-authorization.json").write_text(
            json.dumps(
                {
                    "queue_id": queue["queue_id"],
                    "status": "NOT_AUTHORIZED",
                }
            ),
            encoding="utf-8",
        )
        return directory

    def test_two_child_queues_become_one_sequential_authorization_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._child(root, "first")
            second = self._child(root, "second")
            output = root / "batch"

            generated = generate_hof_batch_queue(
                queue_directories=(first, second),
                output_directory=output,
                python_bin=Path("/usr/bin/python3"),
            )
            verification = verify_hof_batch_queue(
                batch_path=output / "batch-queue.json",
                authorization_path=output / "batch-authorization.json",
                require_authorized=False,
            )

            self.assertEqual(
                [step["system_id"] for step in generated["steps"]],
                ["first", "second"],
            )
            self.assertFalse(verification["authorized"])
            subprocess.run(
                ("bash", "-n", str(output / "run_both_command_queues.sh")),
                check=True,
            )
            authorized = authorize_hof_batch_queue(
                batch_path=output / "batch-queue.json",
                authorization_path=output / "batch-authorization.json",
                concurrent_scientific_jobs="none",
                conflicting_locks="none",
                package_validation="passed",
                storage_validation="passed",
                command_validation="passed",
            )
            self.assertTrue(authorized["authorized"])

    def test_duplicate_system_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = self._child(root, "same")
            with self.assertRaisesRegex(ValueError, "repeats system identity"):
                generate_hof_batch_queue(
                    queue_directories=(child, child),
                    output_directory=root / "batch",
                    python_bin=Path("/usr/bin/python3"),
                )


if __name__ == "__main__":
    unittest.main()
