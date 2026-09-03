from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cmw.core.execution_layout import ExecutionLayout, TARGET_MANIFEST_FILENAME
from cmw.core.execution_layout_migration import (
    LayoutMigrationError,
    MigrationState,
    StaleMigrationPlan,
    apply_migration_plan,
    audit_migration,
    build_migration_plan,
    inventory_tree,
    resolve_attempt,
    resolve_target,
    resume_migration,
    rollback_migration,
)
from cmw.core.provenance import file_hash


class ExecutionLayoutMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw layout ü ")
        self.root = Path(self.temporary.name).resolve()
        self.campaign = self.root / "campaign with spaces"
        self.leaf = self.campaign / "calculation" / "ground_state" / "sp" / "aa"
        self.target_id = "a1b2c3d4e5f6" + "0" * 52
        self.target = self._create_target(self.target_id, attempts=("attempt_001", "attempt_002"))
        self.external = self.root / "external.dat"
        self.external.write_text("must remain external\n", encoding="utf-8")
        attempt = self.target / "attempts" / "attempt_001"
        os.symlink(str(self.external), attempt / "external-link")
        os.symlink("missing-relative-target", attempt / "broken-link")
        payload = attempt / "hardlink-a.dat"
        payload.write_text("one physical extent\n", encoding="utf-8")
        os.link(payload, attempt / "hardlink-b.dat")
        self.current = self.campaign / "current.json"
        self.current.write_text(
            json.dumps({"attempt_path": str(attempt)}, sort_keys=True),
            encoding="utf-8",
        )
        self.evidence = self.root / "evidence"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_target(
        self,
        target_id: str,
        *,
        attempts: tuple[str, ...] = ("attempt_001",),
        node: str = "sp",
        leaf: Path | None = None,
    ) -> Path:
        selected_leaf = leaf or self.leaf
        for attempt_id in attempts:
            layout = ExecutionLayout(
                selected_leaf,
                "AA",
                node,
                target_id,
                attempt_id,
            )
            layout.create_working_directory()
            (layout.working_directory / "execution-layout.json").write_text(
                json.dumps(layout.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (layout.working_directory / "stage.out").write_bytes(
                f"scientific output {attempt_id}\n".encode()
            )
            (layout.working_directory / "geometry.xyz").write_text(
                "1\nfixture\nH 0 0 0\n", encoding="utf-8"
            )
        target = layout.target_directory
        (target / "target.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "target": {
                        "target_id": target_id,
                        "stage_type": node.upper(),
                        "geometry_sha256": "9" * 64,
                        "calculation": {"method_hash": "8" * 64},
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return target

    def test_dry_run_is_deterministic_and_does_not_mutate(self) -> None:
        before = tuple(sorted(str(path) for path in self.campaign.rglob("*")))
        first = build_migration_plan(self.campaign)
        second = build_migration_plan(self.campaign)
        after = tuple(sorted(str(path) for path in self.campaign.rglob("*")))
        self.assertEqual(first.plan_sha256, second.plan_sha256)
        self.assertEqual(before, after)
        self.assertEqual(len(first.targets), 1)
        self.assertEqual(first.attempt_count, 2)
        self.assertEqual(first.symlink_count, 2)
        self.assertEqual(first.targets[0].hardlink_entry_count, 2)
        self.assertEqual(first.blockers, ())

    def test_apply_preserves_science_links_ids_and_updates_mutable_pointer(self) -> None:
        plan = build_migration_plan(self.campaign)
        old_layout = self.target / "attempts" / "attempt_001" / "execution-layout.json"
        old_layout_hash = file_hash(old_layout)
        output_hash = file_hash(self.target / "attempts" / "attempt_001" / "stage.out")
        result = apply_migration_plan(
            plan,
            confirm_plan=plan.plan_sha256,
            evidence_directory=self.evidence,
        )
        destination = Path(plan.targets[0].destination_path)
        attempt = destination / "attempts" / "attempt_001"
        self.assertEqual(result["status"], "COMMITTED")
        self.assertFalse(self.target.exists())
        self.assertEqual(file_hash(attempt / "execution-layout.json"), old_layout_hash)
        self.assertEqual(file_hash(attempt / "stage.out"), output_hash)
        self.assertTrue((attempt / "external-link").is_symlink())
        self.assertEqual(os.readlink(attempt / "external-link"), str(self.external))
        self.assertTrue((attempt / "broken-link").is_symlink())
        self.assertEqual(os.readlink(attempt / "broken-link"), "missing-relative-target")
        self.assertTrue(self.external.is_file())
        self.assertEqual(
            os.lstat(attempt / "hardlink-a.dat").st_ino,
            os.lstat(attempt / "hardlink-b.dat").st_ino,
        )
        manifest = json.loads((destination / TARGET_MANIFEST_FILENAME).read_text())
        self.assertEqual(manifest["full_target_id"], self.target_id)
        self.assertEqual(manifest["geometry_sha256"], "9" * 64)
        self.assertEqual(
            json.loads(self.current.read_text())["attempt_path"],
            str(destination / "attempts" / "attempt_001"),
        )
        self.assertEqual(
            resolve_target(
                self.campaign,
                full_target_id=self.target_id,
                legacy_path=self.target,
            ),
            destination,
        )
        self.assertEqual(
            resolve_attempt(
                self.campaign,
                full_target_id=self.target_id,
                attempt_id="attempt_002",
                legacy_path=self.target,
            ),
            destination / "attempts" / "attempt_002",
        )
        audit = audit_migration(
            self.campaign, transaction_id=plan.plan_sha256[:20]
        )
        self.assertEqual(audit["status"], "VALIDATED")
        self.assertEqual(audit["registry_entry_count"], 3)

    def test_stale_plan_and_prefix_collision_fail_closed(self) -> None:
        plan = build_migration_plan(self.campaign)
        (self.target / "attempts" / "attempt_001" / "stage.out").write_text(
            "changed\n", encoding="utf-8"
        )
        with self.assertRaises(StaleMigrationPlan):
            apply_migration_plan(
                plan,
                confirm_plan=plan.plan_sha256,
                evidence_directory=self.evidence,
            )
        colliding = "a1b2c3d4e5f6" + "f" * 52
        self._create_target(colliding, leaf=self.leaf)
        collision_plan = build_migration_plan(self.campaign)
        self.assertTrue(
            any("SHORT_TARGET_DISPLAY_ID_COLLISION" in item for item in collision_plan.blockers)
        )

    def test_interrupted_move_can_resume(self) -> None:
        plan = build_migration_plan(self.campaign)

        def fail_after_first_move(state: MigrationState, target_id: str | None) -> None:
            if state is MigrationState.MOVING and target_id is not None:
                raise RuntimeError("synthetic crash")

        with self.assertRaisesRegex(LayoutMigrationError, "synthetic crash"):
            apply_migration_plan(
                plan,
                confirm_plan=plan.plan_sha256,
                evidence_directory=self.evidence,
                _fault_hook=fail_after_first_move,
            )
        result = resume_migration(
            self.campaign,
            transaction_id=plan.plan_sha256[:20],
            evidence_directory=self.evidence,
        )
        self.assertEqual(result["status"], "COMMITTED")

    def test_interrupted_metadata_can_rollback(self) -> None:
        plan = build_migration_plan(self.campaign)

        def fail_before_metadata(state: MigrationState, target_id: str | None) -> None:
            if state is MigrationState.METADATA_UPDATING:
                raise RuntimeError("metadata crash")

        with self.assertRaises(LayoutMigrationError):
            apply_migration_plan(
                plan,
                confirm_plan=plan.plan_sha256,
                evidence_directory=self.evidence,
                _fault_hook=fail_before_metadata,
            )
        result = rollback_migration(
            self.campaign,
            transaction_id=plan.plan_sha256[:20],
            evidence_directory=self.evidence,
        )
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.assertTrue(self.target.is_dir())
        self.assertEqual(
            json.loads(self.current.read_text())["attempt_path"],
            str(self.target / "attempts" / "attempt_001"),
        )

    def test_inventory_never_dereferences_links(self) -> None:
        entries = inventory_tree(self.target)
        link = next(item for item in entries if item.path.endswith("external-link"))
        broken = next(item for item in entries if item.path.endswith("broken-link"))
        self.assertEqual(link.entry_type, "symlink")
        self.assertEqual(link.link_target, str(self.external))
        self.assertIsNone(link.scientific_sha256)
        self.assertEqual(broken.link_target, "missing-relative-target")

    def test_cross_filesystem_plan_is_blocked(self) -> None:
        real_lstat = os.lstat

        def different_destination_device(path: os.PathLike[str] | str):
            result = real_lstat(path)
            if Path(path) == self.leaf:
                values = list(result)
                values[2] = result.st_dev + 1
                return os.stat_result(values)
            return result

        with mock.patch(
            "cmw.core.execution_layout_migration.os.lstat",
            side_effect=different_destination_device,
        ):
            plan = build_migration_plan(self.campaign)
        self.assertTrue(
            any("CROSS_FILESYSTEM_MIGRATION_UNSUPPORTED" in item for item in plan.blockers)
        )

    def test_large_mutable_metadata_uses_streaming_atomic_update(self) -> None:
        with mock.patch(
            "cmw.core.execution_layout_migration.STREAMING_METADATA_THRESHOLD_BYTES",
            1,
        ):
            plan = build_migration_plan(self.campaign)
            reference = next(
                item for item in plan.mutable_references if item.path == str(self.current)
            )
            self.assertEqual(reference.update_mode, "stream_text")
            apply_migration_plan(
                plan,
                confirm_plan=plan.plan_sha256,
                evidence_directory=self.evidence,
            )
        destination = Path(plan.targets[0].destination_path)
        self.assertEqual(
            json.loads(self.current.read_text())["attempt_path"],
            str(destination / "attempts" / "attempt_001"),
        )


if __name__ == "__main__":
    unittest.main()
