from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.core.external_scratch import (
    ExternalScratchError,
    cleanup_external_scratch,
    copy_back_external_scratch,
    mark_external_scratch_failed,
    mark_external_scratch_running,
    prepare_external_scratch,
)
from cmw.core.job import JobTarget
from cmw.core.provenance import atomic_write_json
from cmw.core.storage import DiskUsage


GIB = 1024**3


class ExternalScratchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw external scratch ")
        self.root = Path(self.temporary.name).resolve()
        self.mount = self.root / "mounted volume"
        self.mount.mkdir()
        self.scratch = self.mount / "operational scratch"
        self.attempt = self.root / "canonical attempt"
        self.attempt.mkdir()
        (self.attempt / "stage.inp").write_text("! HF STO-3G\n", encoding="utf-8")
        (self.attempt / "input.xyz").write_text(
            "1\nfixture\nH 0 0 0\n", encoding="utf-8"
        )
        self.target = JobTarget("SP", "a" * 64, 0, 1, {"keywords": "HF STO-3G"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _prepare(
        self,
        *,
        attempt: Path | None = None,
        scratch: Path | None = None,
        mount_checker=None,
        free_gib: int = 1000,
    ) -> tuple[Path, dict[str, object]]:
        selected_attempt = attempt or self.attempt
        selected_scratch = scratch or self.scratch
        record = prepare_external_scratch(
            scratch_root=selected_scratch,
            mount_path=self.mount,
            canonical_attempt_directory=selected_attempt,
            target_id=self.target.target_id,
            attempt_id="attempt_002",
            input_files={
                "primary": selected_attempt / "stage.inp",
                "geometry": selected_attempt / "input.xyz",
            },
            output_files={
                "output": selected_attempt / "stage.out",
                "stderr": selected_attempt / "stage.err",
            },
            process_environment={
                "OMPI_MCA_osc": "pt2pt",
            },
            minimum_free_gib=800,
            mount_checker=mount_checker or (lambda path: path == self.mount),
            usage_provider=lambda _: DiskUsage(2000 * GIB, 1000 * GIB, free_gib * GIB),
        )
        return selected_attempt / "external-scratch.json", record

    @staticmethod
    def _output(record: dict[str, object], role: str) -> Path:
        outputs = record["outputs"]
        assert isinstance(outputs, list)
        return Path(
            next(
                str(item["execution_path"])
                for item in outputs
                if isinstance(item, dict) and item["role"] == role
            )
        )

    def _finish_copyback(self, record_path: Path, record: dict[str, object]) -> dict[str, object]:
        self._output(record, "output").write_text("ORCA TERMINATED NORMALLY\n", encoding="utf-8")
        self._output(record, "stderr").write_text("", encoding="utf-8")
        mark_external_scratch_running(record_path)
        return copy_back_external_scratch(record_path, process_exit_code=0)

    def _finalization(self, record: dict[str, object], *, valid: bool = True) -> Path:
        path = self.attempt / "job.json"
        atomic_write_json(
            path,
            {
                "target": {"target_id": record["target_id"]},
                "attempt": {"attempt_id": record["attempt_id"]},
                "execution": {"status": "SUCCESS" if valid else "FAILED"},
                "scientific": {"status": "VALID" if valid else "INVALID"},
                "reusable": valid,
            },
        )
        return path

    def test_preflight_fails_closed_for_missing_or_unmounted_volume(self) -> None:
        with self.assertRaisesRegex(ExternalScratchError, "mounted volume is missing"):
            prepare_external_scratch(
                scratch_root=self.scratch,
                mount_path=self.root / "absent",
                canonical_attempt_directory=self.attempt,
                target_id=self.target.target_id,
                attempt_id="attempt_002",
                input_files={"primary": self.attempt / "stage.inp"},
                output_files={"output": self.attempt / "stage.out"},
                minimum_free_gib=1,
            )
        with self.assertRaisesRegex(ExternalScratchError, "not mounted"):
            self._prepare(mount_checker=lambda _: False)
        self.assertFalse(self.scratch.exists())

    def test_preflight_fails_closed_below_free_space_threshold(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "FAILED_STORAGE_CAPACITY"):
            self._prepare(free_gib=799)
        self.assertFalse(any(self.scratch.glob("cmw-orca-*")))

    def test_preflight_fails_closed_when_unique_directory_cannot_be_created(self) -> None:
        with patch(
            "cmw.core.external_scratch.tempfile.mkdtemp",
            side_effect=PermissionError("synthetic read-only filesystem"),
        ):
            with self.assertRaisesRegex(PermissionError, "read-only filesystem"):
                self._prepare()
        self.assertFalse(any(self.scratch.glob("cmw-orca-*")))

    def test_scratch_disappearing_before_launch_never_falls_back(self) -> None:
        record_path, record = self._prepare()
        execution = Path(str(record["execution_directory"]))
        detached = execution.with_name(execution.name + "-disconnected")
        execution.rename(detached)

        with self.assertRaisesRegex(ExternalScratchError, "missing"):
            mark_external_scratch_running(record_path)
        self.assertFalse((self.attempt / "stage.out").exists())
        self.assertTrue(detached.is_dir())

    def test_preflight_rejects_scratch_overlapping_canonical_attempt(self) -> None:
        attempt = self.mount / "canonical attempt"
        attempt.mkdir()
        (attempt / "stage.inp").write_text("! HF STO-3G\n", encoding="utf-8")
        (attempt / "input.xyz").write_text("1\nx\nH 0 0 0\n", encoding="utf-8")

        with self.assertRaisesRegex(ExternalScratchError, "overlaps"):
            self._prepare(attempt=attempt, scratch=attempt / "scratch")

    def test_unique_directories_do_not_change_scientific_target_identity(self) -> None:
        first_path, first = self._prepare()
        second_attempt = self.root / "second canonical attempt"
        second_attempt.mkdir()
        (second_attempt / "stage.inp").write_text("! HF STO-3G\n", encoding="utf-8")
        (second_attempt / "input.xyz").write_text("1\nx\nH 0 0 0\n", encoding="utf-8")
        second_path, second = self._prepare(attempt=second_attempt)

        self.assertEqual(first["target_id"], self.target.target_id)
        self.assertEqual(second["target_id"], self.target.target_id)
        self.assertEqual(
            first["process_environment"],
            {"OMPI_MCA_osc": "pt2pt"},
        )
        self.assertNotEqual(first["execution_directory"], second["execution_directory"])
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())

    def test_allowlisted_copyback_excludes_runtime_scratch_and_cleanup_is_exact(self) -> None:
        record_path, record = self._prepare()
        execution = Path(str(record["execution_directory"]))
        disposable = execution / "stage.PAO_V12.tmp.proc0"
        disposable.write_bytes(b"large disposable fixture")
        copied = self._finish_copyback(record_path, record)

        self.assertEqual(copied["status"], "COPIED")
        self.assertTrue((self.attempt / "stage.out").is_file())
        self.assertTrue((self.attempt / "stage.err").is_file())
        self.assertFalse((self.attempt / disposable.name).exists())
        self.assertIn(disposable.name, copied["copyback"]["unlisted_entry_sample"])

        sibling = self.scratch / "unrelated"
        sibling.mkdir()
        finalized = self._finalization(record)
        cleaned = cleanup_external_scratch(
            record_path,
            finalization_metadata_path=finalized,
            mount_checker=lambda path: path == self.mount,
        )
        self.assertEqual(cleaned["status"], "CLEANED")
        self.assertFalse(execution.exists())
        self.assertTrue(sibling.is_dir())
        self.assertTrue((self.attempt / "stage.out").is_file())

    def test_failed_science_preserves_external_scratch(self) -> None:
        record_path, record = self._prepare()
        copied = self._finish_copyback(record_path, record)
        execution = Path(str(record["execution_directory"]))
        invalid = self._finalization(record, valid=False)

        with self.assertRaisesRegex(ExternalScratchError, "has not succeeded"):
            cleanup_external_scratch(
                record_path,
                finalization_metadata_path=invalid,
                mount_checker=lambda path: path == self.mount,
            )
        failed = mark_external_scratch_failed(record_path, reason="scientific invalid")
        self.assertEqual(copied["status"], "COPIED")
        self.assertEqual(failed["status"], "FAILED")
        self.assertTrue(failed["scratch_preserved"])
        self.assertTrue(execution.is_dir())

    def test_missing_or_empty_success_output_blocks_copyback(self) -> None:
        record_path, record = self._prepare()
        mark_external_scratch_running(record_path)
        self._output(record, "stderr").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ExternalScratchError, "missing"):
            copy_back_external_scratch(record_path, process_exit_code=0)
        self._output(record, "output").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ExternalScratchError, "empty"):
            copy_back_external_scratch(record_path, process_exit_code=0)
        self.assertTrue(Path(str(record["execution_directory"])).is_dir())

    def test_interrupted_copyback_is_not_success_and_can_resume(self) -> None:
        record_path, record = self._prepare()
        self._output(record, "output").write_text("output\n", encoding="utf-8")
        self._output(record, "stderr").write_text("", encoding="utf-8")
        mark_external_scratch_running(record_path)
        calls = 0

        def interrupted(source: Path, destination: Path) -> object:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic interruption")
            return source.replace(destination)

        with self.assertRaisesRegex(OSError, "synthetic interruption"):
            copy_back_external_scratch(
                record_path, process_exit_code=0, replace_provider=interrupted
            )
        failed = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "COPY_FAILED")
        self.assertFalse((self.attempt / "job.json").exists())

        recovered = copy_back_external_scratch(record_path, process_exit_code=0)
        self.assertEqual(recovered["status"], "COPIED")
        self.assertTrue((self.attempt / "stage.out").is_file())
        self.assertTrue((self.attempt / "stage.err").is_file())

    def test_unsafe_cleanup_path_is_rejected_without_touching_sibling(self) -> None:
        record_path, record = self._prepare()
        copied = self._finish_copyback(record_path, record)
        sibling = self.scratch / "unrelated"
        sibling.mkdir()
        copied["execution_directory"] = str(self.scratch)
        atomic_write_json(record_path, copied)
        finalized = self._finalization(record)

        with self.assertRaisesRegex(ExternalScratchError, "must not equal"):
            cleanup_external_scratch(
                record_path,
                finalization_metadata_path=finalized,
                mount_checker=lambda path: path == self.mount,
            )
        self.assertTrue(sibling.is_dir())


if __name__ == "__main__":
    unittest.main()
