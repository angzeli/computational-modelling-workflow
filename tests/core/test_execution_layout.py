from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cmw.core.execution_layout import (
    ExecutionLayout,
    ExecutionLayoutError,
    next_attempt_identifier,
)


class ExecutionLayoutTests(unittest.TestCase):
    def test_same_identities_produce_the_same_resolved_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = ExecutionLayout(
                root, "system-a", "optimization", "target-a", "attempt_001"
            )
            second = ExecutionLayout(
                root, "system-a", "optimization", "target-a", "attempt_001"
            )

            self.assertEqual(first.to_dict(), second.to_dict())
            self.assertEqual(
                first.working_directory,
                root
                / "calculation"
                / "system-a"
                / "optimization"
                / "target-a"
                / "attempts"
                / "attempt_001",
            )
            self.assertTrue(first.output_path("stage.out").is_absolute())
            self.assertTrue(first.log_path("stage.err").is_absolute())

    def test_sequential_attempts_have_separate_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = ExecutionLayout(root, "system-a", "sp", "target-a", "attempt_001")
            first.create_working_directory()
            self.assertEqual(next_attempt_identifier(first.target_directory), "attempt_002")

            second = ExecutionLayout(root, "system-a", "sp", "target-a", "attempt_002")
            second.create_working_directory()
            self.assertNotEqual(first.working_directory, second.working_directory)
            self.assertTrue(first.working_directory.is_dir())
            self.assertTrue(second.working_directory.is_dir())

    def test_layout_validation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            layout = ExecutionLayout(root, "system-a", "sp", "target-a", "attempt_001")
            with self.assertRaisesRegex(ExecutionLayoutError, "working directory is missing"):
                layout.validate(require_existing=True)

            layout.create_working_directory()
            with self.assertRaisesRegex(ExecutionLayoutError, "output path is unresolved"):
                layout.validate_paths(output_paths=(Path("stage.out"),))
            with self.assertRaisesRegex(ExecutionLayoutError, "conflicting attempt identity"):
                layout.validate_attempt_identity("attempt_002")

            stored = layout.to_dict()
            stored["working_directory"] = str(root / "different")
            with self.assertRaisesRegex(ExecutionLayoutError, "conflicts with identities"):
                ExecutionLayout.from_mapping(stored)

    def test_relative_or_templated_project_root_is_rejected(self) -> None:
        for root in (Path("relative"), Path("/tmp/{project}")):
            with self.subTest(root=root):
                with self.assertRaisesRegex(ExecutionLayoutError, "absolute resolved path"):
                    ExecutionLayout(root, "system-a", "sp", "target-a", "attempt_001")


if __name__ == "__main__":
    unittest.main()
