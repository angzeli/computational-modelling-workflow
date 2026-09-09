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
    migration_plan_from_mapping,
    resolve_attempt,
    resolve_target,
    resume_migration,
    rollback_migration,
)
from cmw.core.mutable_metadata import MutableBackupPolicy, MutableBackupStrategy
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
        self.assertFalse((self.leaf / "calculation").exists())
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
        self.assertTrue((self.leaf / "calculation").is_dir())
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

    def test_large_mutable_metadata_uses_structured_inverse_patch(self) -> None:
        current = json.loads(self.current.read_text())
        current["schema_version"] = 1
        self.current.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
        plan = build_migration_plan(
            self.campaign,
            mutable_backup_policy=MutableBackupPolicy(full_copy_max_bytes=1),
        )
        reference = next(
            item for item in plan.mutable_references if item.path == str(self.current)
        )
        self.assertEqual(
            reference.backup_strategy,
            MutableBackupStrategy.STRUCTURED_INVERSE_PATCH.value,
        )
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

    def test_full_copy_forward_and_rollback_restore_exact_bytes(self) -> None:
        original = self.current.read_bytes()
        plan = build_migration_plan(self.campaign)
        reference = next(item for item in plan.mutable_references if item.path == str(self.current))
        self.assertEqual(reference.backup_strategy, MutableBackupStrategy.FULL_COPY.value)
        apply_migration_plan(
            plan, confirm_plan=plan.plan_sha256, evidence_directory=self.evidence
        )
        rollback_migration(
            self.campaign,
            transaction_id=plan.plan_sha256[:20],
            evidence_directory=self.evidence,
        )
        self.assertEqual(self.current.read_bytes(), original)

    def test_overlay_only_record_stays_byte_identical_and_rolls_back_registry(self) -> None:
        original = self.current.read_bytes()
        plan = build_migration_plan(
            self.campaign,
            mutable_backup_policy=MutableBackupPolicy(
                overlay_only_records=(str(self.current),)
            ),
        )
        reference = next(item for item in plan.mutable_references if item.path == str(self.current))
        self.assertEqual(
            reference.backup_strategy,
            MutableBackupStrategy.MIGRATION_OVERLAY_ONLY.value,
        )
        apply_migration_plan(
            plan, confirm_plan=plan.plan_sha256, evidence_directory=self.evidence
        )
        self.assertEqual(self.current.read_bytes(), original)
        self.assertEqual(
            resolve_target(self.campaign, full_target_id=self.target_id, legacy_path=self.target),
            Path(plan.targets[0].destination_path),
        )
        rollback_migration(
            self.campaign,
            transaction_id=plan.plan_sha256[:20],
            evidence_directory=self.evidence,
        )
        self.assertEqual(self.current.read_bytes(), original)
        self.assertEqual(
            resolve_target(self.campaign, full_target_id=self.target_id, legacy_path=self.target),
            self.target,
        )

    def test_unsafe_large_record_blocks_before_mutation(self) -> None:
        before = self.current.read_bytes()
        plan = build_migration_plan(
            self.campaign,
            mutable_backup_policy=MutableBackupPolicy(full_copy_max_bytes=1),
        )
        self.assertTrue(any("BLOCK_UNSAFE_LARGE_RECORD" in item for item in plan.blockers))
        reference = next(item for item in plan.mutable_references if item.path == str(self.current))
        self.assertEqual(
            reference.backup_strategy,
            MutableBackupStrategy.BLOCK_UNSAFE_LARGE_RECORD.value,
        )
        with self.assertRaises(LayoutMigrationError):
            apply_migration_plan(
                plan, confirm_plan=plan.plan_sha256, evidence_directory=self.evidence
            )
        self.assertEqual(self.current.read_bytes(), before)
        self.assertTrue(self.target.is_dir())

    def test_unsafe_large_unstructured_record_blocks_before_mutation(self) -> None:
        unsafe = self.campaign / "unsafe.json"
        unsafe.write_text(
            "not-json " + str(self.target / "attempts" / "attempt_001"),
            encoding="utf-8",
        )
        plan = build_migration_plan(
            self.campaign,
            mutable_backup_policy=MutableBackupPolicy(full_copy_max_bytes=1),
        )
        self.assertTrue(
            any(str(unsafe) in item and "BLOCK_UNSAFE_LARGE_RECORD" in item for item in plan.blockers)
        )
        self.assertTrue(self.target.is_dir())

    def test_journal_serializes_strategy_and_patch_resume_is_idempotent(self) -> None:
        current = json.loads(self.current.read_text())
        current["schema_version"] = 1
        self.current.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
        original = self.current.read_bytes()
        plan = build_migration_plan(
            self.campaign,
            mutable_backup_policy=MutableBackupPolicy(full_copy_max_bytes=1),
        )

        def crash_after_patch(state: MigrationState, selected: str | None) -> None:
            if state is MigrationState.METADATA_UPDATING and selected == str(self.current):
                raise RuntimeError("crash after structured patch")

        with self.assertRaises(LayoutMigrationError):
            apply_migration_plan(
                plan,
                confirm_plan=plan.plan_sha256,
                evidence_directory=self.evidence,
                _fault_hook=crash_after_patch,
            )
        journal = json.loads(
            (
                self.campaign
                / ".cmw"
                / "layout-migrations"
                / "transactions"
                / f"{plan.plan_sha256[:20]}.json"
            ).read_text()
        )
        mutable = journal["mutable_records"][0]
        self.assertEqual(
            mutable["backup_strategy"],
            MutableBackupStrategy.STRUCTURED_INVERSE_PATCH.value,
        )
        self.assertEqual(mutable["apply_status"], "APPLIED")
        self.assertTrue(mutable["forward_mutation"])
        self.assertTrue(mutable["inverse_mutation"])
        result = resume_migration(
            self.campaign,
            transaction_id=plan.plan_sha256[:20],
            evidence_directory=self.evidence,
        )
        self.assertEqual(result["status"], "COMMITTED")
        rollback_migration(
            self.campaign,
            transaction_id=plan.plan_sha256[:20],
            evidence_directory=self.evidence,
        )
        self.assertEqual(self.current.read_bytes(), original)

    def test_mixed_full_copy_and_patch_rollback(self) -> None:
        original_current = self.current.read_bytes()
        large = self.campaign / "large-state.json"
        large.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "attempt_path": str(self.target / "attempts" / "attempt_002"),
                    "payload": ["x" * 100] * 1000,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_large = large.read_bytes()
        threshold = self.current.stat().st_size + 16
        plan = build_migration_plan(
            self.campaign,
            mutable_backup_policy=MutableBackupPolicy(full_copy_max_bytes=threshold),
        )
        self.assertEqual(
            {item.backup_strategy for item in plan.mutable_references},
            {
                MutableBackupStrategy.FULL_COPY.value,
                MutableBackupStrategy.STRUCTURED_INVERSE_PATCH.value,
            },
        )
        apply_migration_plan(
            plan, confirm_plan=plan.plan_sha256, evidence_directory=self.evidence
        )
        rollback_migration(
            self.campaign,
            transaction_id=plan.plan_sha256[:20],
            evidence_directory=self.evidence,
        )
        self.assertEqual(self.current.read_bytes(), original_current)
        self.assertEqual(large.read_bytes(), original_large)

    def test_rollback_after_partial_structured_metadata_update(self) -> None:
        current = json.loads(self.current.read_text())
        current["schema_version"] = 1
        self.current.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        originals = {self.current: self.current.read_bytes()}
        records: list[Path] = []
        for name in ("large-a.json", "large-b.json"):
            path = self.campaign / name
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "attempt_path": str(self.target / "attempts" / "attempt_001"),
                        "payload": ["x" * 100] * 100,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            records.append(path)
            originals[path] = path.read_bytes()
        plan = build_migration_plan(
            self.campaign,
            mutable_backup_policy=MutableBackupPolicy(full_copy_max_bytes=1),
        )

        def crash_after_first_large(state: MigrationState, selected: str | None) -> None:
            if state is MigrationState.METADATA_UPDATING and selected == str(records[0]):
                raise RuntimeError("partial metadata crash")

        with self.assertRaises(LayoutMigrationError):
            apply_migration_plan(
                plan,
                confirm_plan=plan.plan_sha256,
                evidence_directory=self.evidence,
                _fault_hook=crash_after_first_large,
            )
        rollback_migration(
            self.campaign,
            transaction_id=plan.plan_sha256[:20],
            evidence_directory=self.evidence,
        )
        for path, expected in originals.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_backup_policy_is_in_plan_identity_and_legacy_plan_deserializes(self) -> None:
        default = build_migration_plan(self.campaign)
        overlay = build_migration_plan(
            self.campaign,
            mutable_backup_policy=MutableBackupPolicy(
                overlay_only_records=(str(self.current),)
            ),
        )
        self.assertNotEqual(default.plan_sha256, overlay.plan_sha256)
        value = default.to_dict()
        value.pop("plan_sha256")
        value.pop("mutable_backup_policy")
        value["schema_version"] = 1
        value["tool_version"] = "cmw-execution-layout-migration/1"
        value["mutable_references"] = [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "replacements": item["replacements"],
                "update_mode": item["update_mode"],
            }
            for item in value["mutable_references"]
        ]
        restored = migration_plan_from_mapping(value)
        self.assertEqual(restored.schema_version, 1)
        self.assertEqual(restored.mutable_references[0].contract_version, 1)


if __name__ == "__main__":
    unittest.main()
