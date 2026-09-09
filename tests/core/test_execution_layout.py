from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cmw.core.execution_layout import (
    ExecutionLayout,
    ExecutionLayoutError,
    ExecutionLayoutVersion,
    TARGET_MANIFEST_FILENAME,
    build_target_manifest,
    format_layout_status,
    next_attempt_identifier,
    write_target_manifest,
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

    def test_v2_has_one_short_target_layer_and_relative_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            leaf = Path(temporary).resolve() / "ground_state" / "sp" / "aa"
            target_id = "a" * 64
            layout = ExecutionLayout(
                leaf,
                "AA",
                "SP",
                target_id,
                "attempt_001",
                version=ExecutionLayoutVersion.V2,
            )
            self.assertEqual(
                layout.working_directory,
                leaf / "target-aaaaaaaaaaaa" / "attempts" / "attempt_001",
            )
            self.assertNotIn("calculation", layout.target_directory.parts[-3:])
            self.assertNotIn(target_id, layout.target_directory.parts)
            stored = layout.to_dict()
            self.assertEqual(stored["layout_version"], "v2")
            self.assertFalse(Path(str(stored["working_path"])).is_absolute())
            relocated = Path(temporary).resolve() / "relocated" / "sp" / "aa"
            restored = ExecutionLayout.from_mapping(stored, root_override=relocated)
            self.assertEqual(restored.target_identifier, target_id)
            self.assertEqual(restored.target_directory.parent, relocated)

    def test_v2_operational_stage_and_configurable_width(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            layout = ExecutionLayout(
                root,
                "AA",
                "orca_to_molden",
                "abcdef1234567890" * 4,
                "attempt_001",
                version="v2",
                operational_stage="orca_to_molden",
                short_id_width=16,
            )
            self.assertEqual(
                layout.target_directory,
                root / "orca_to_molden" / "target-abcdef1234567890",
            )

    def test_v2_manifest_preserves_full_identity_and_fails_on_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first_id = "123456789abc" + "0" * 52
            second_id = "123456789abc" + "f" * 52
            first = ExecutionLayout(
                root, "AA", "sp", first_id, "attempt_001", version="v2"
            )
            manifest = build_target_manifest(
                first,
                target={
                    "target_id": first_id,
                    "geometry_sha256": "9" * 64,
                    "method_hash": "8" * 64,
                },
                source_artifact_ids=("artifact-a",),
            )
            write_target_manifest(
                first.target_directory / TARGET_MANIFEST_FILENAME, manifest
            )
            first.create_working_directory()
            same = ExecutionLayout(
                root, "AA", "sp", first_id, "attempt_002", version="v2"
            )
            same.create_working_directory()
            different = ExecutionLayout(
                root, "AA", "sp", second_id, "attempt_001", version="v2"
            )
            with self.assertRaisesRegex(
                ExecutionLayoutError, "SHORT_TARGET_DISPLAY_ID_COLLISION"
            ):
                different.create_working_directory()
            self.assertEqual(manifest["full_target_id"], first_id)
            self.assertEqual(manifest["geometry_sha256"], "9" * 64)
            self.assertEqual(manifest["protocol_method_hash"], "8" * 64)
            self.assertEqual(manifest["source_artifact_ids"], ["artifact-a"])

    def test_status_is_short_by_default_and_full_when_verbose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target_id = "abcdef1234567890" * 4
            layout = ExecutionLayout(
                Path(temporary).resolve(),
                "AA",
                "TDA",
                target_id,
                "attempt_001",
                version="v2",
            )
            normal = format_layout_status(layout)
            self.assertIn("Target: target-abcdef123456", normal)
            self.assertNotIn(target_id, normal)
            self.assertIn(target_id, format_layout_status(layout, verbose=True))

    def test_all_hof_scientific_leaf_families_share_the_v2_formula(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target_id = "fedcba9876543210" * 4
            families = (
                ("ground_state", "sp", "aa"),
                ("ground_state", "fmo", "ab"),
                ("ground_state", "igmh", "bb"),
                ("excited_state", "tda", "aa"),
                ("excited_state", "nto", "ab"),
                ("excited_state", "hea", "bb"),
            )
            for domain, stage, system in families:
                with self.subTest(stage=stage, system=system):
                    leaf = root / domain / stage / system
                    layout = ExecutionLayout(
                        leaf,
                        system.upper(),
                        stage.upper(),
                        target_id,
                        "attempt_001",
                        version="v2",
                    )
                    self.assertEqual(
                        layout.target_directory,
                        leaf / "target-fedcba987654",
                    )
                    self.assertEqual(
                        sum(part.startswith("target-") for part in layout.target_directory.parts),
                        1,
                    )


if __name__ == "__main__":
    unittest.main()
