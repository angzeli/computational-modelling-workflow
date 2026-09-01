from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from cmw.core.attempt_cleanup import (
    CleanupError,
    CleanupExitCode,
    CleanupMode,
    InvalidCleanupPath,
    TransactionState,
    _safe_remove_tree,
    apply_cleanup_plan,
    build_cleanup_plan,
    cleanup_exit_code,
    enumerate_campaign_attempts,
    load_cleanup_registry,
    recover_cleanup_transactions,
    resolve_campaign,
    resolve_canonical_attempts,
)
from cmw.core.execution_layout import ExecutionLayout, next_attempt_identifier
from cmw.core.locks import acquire_lock, release_lock
from cmw.core.provenance import atomic_write_json, file_hash, read_json


def _no_writer(_: Path) -> tuple[bool | None, str]:
    return False, "synthetic fixture has no writer"


class SyntheticCampaign:
    def __init__(self, base: Path, *, name: str = "campaign") -> None:
        self.root = (base / name).resolve()
        self.root.mkdir(parents=True)
        self.campaign_id = "synthetic-campaign"
        self.target_id = "a" * 64
        self.target = self.root / "calculation" / "system-a" / "node-a" / self.target_id
        self.target.mkdir(parents=True)
        atomic_write_json(
            self.target / "target.json",
            {"schema_version": 1, "target_id": self.target_id, "canonical_attempt_id": "attempt_002"},
        )
        self._write_campaign(canonical="attempt_002")
        self.add_attempt("attempt_001", reusable=False, execution="FAILED", scientific="INVALID")
        self.add_attempt("attempt_002", reusable=True, execution="SUCCESS", scientific="VALID")

    def _write_campaign(
        self,
        *,
        canonical: str | None,
        watcher: dict[str, object] | None = None,
    ) -> None:
        targets = {self.target_id: {"canonical_attempt_id": canonical}} if canonical else {}
        value: dict[str, object] = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "targets": targets,
        }
        if watcher is not None:
            value["watcher"] = watcher
        atomic_write_json(self.root / ".cmw" / "campaign.json", value)

    def add_attempt(
        self,
        attempt_id: str,
        *,
        reusable: bool,
        execution: str,
        scientific: str,
        pid: int | None = None,
        superseded_by: str | None = None,
        parent_attempt_id: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> Path:
        layout = ExecutionLayout(
            self.root,
            "system-a",
            "node-a",
            self.target_id,
            attempt_id,
        )
        if not layout.working_directory.exists():
            layout.create_working_directory()
        atomic_write_json(layout.layout_path, layout.to_dict())
        input_path = layout.input_path("stage.inp")
        output_path = layout.output_path("stage.out")
        stderr_path = layout.log_path("stage.err")
        input_path.write_text(f"input for {attempt_id}\n", encoding="utf-8")
        output_path.write_text(f"output for {attempt_id}\n", encoding="utf-8")
        stderr_path.write_text("synthetic failure\n" if not reusable else "", encoding="utf-8")
        artifacts = {
            role: {"path": path.name, "size_bytes": path.stat().st_size, "sha256": file_hash(path)}
            for role, path in {"input": input_path, "output": output_path, "stderr": stderr_path}.items()
        }
        record: dict[str, object] = {
            "schema_version": 1,
            "target": {"target_id": self.target_id, "calculation": {"keywords": "synthetic"}},
            "execution_layout": layout.to_dict(),
            "attempt": {
                "attempt_id": attempt_id,
                "target_id": self.target_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "resources": {"nprocs": 1},
                "executable": {"program": "synthetic", "version": "1"},
                "generated_input_sha256": artifacts["input"]["sha256"],
                "parent_attempt_id": parent_attempt_id,
            },
            "execution": {"status": execution, "process_exit_code": 0 if execution == "SUCCESS" else 1},
            "scientific": {"status": scientific, "stage_type": "SP", "reason": scientific.lower()},
            "artifacts": artifacts,
            "required_artifact_roles": ["input", "output"],
            "scientific_artifact": {
                "artifact_id": f"artifact-{attempt_id}",
                "validation": {"status": "PASSED" if reusable else "FAILED"},
            },
            "reusable": reusable,
            "geometry_input": {"geometry_sha256": "g" * 64},
        }
        if pid is not None:
            record["pid"] = pid
        if superseded_by is not None:
            record["superseded_by_attempt_id"] = superseded_by
        if extra:
            record.update(extra)
        atomic_write_json(layout.metadata_path("job.json"), record)
        return layout.working_directory

    @property
    def old(self) -> Path:
        return self.target / "attempts" / "attempt_001"

    @property
    def canonical(self) -> Path:
        return self.target / "attempts" / "attempt_002"

    def plan(self, mode: CleanupMode = CleanupMode.MINIMAL_PROVENANCE, **kwargs: object):
        return build_cleanup_plan(self.root, mode=mode, open_writer_probe=_no_writer, **kwargs)


class EligibilityTests(unittest.TestCase):
    def test_01_failed_attempt_with_later_finalized_attempt_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = fixture.plan()
            self.assertEqual([item.attempt_id for item in plan.candidates], ["attempt_001"])

    def test_02_lower_attempt_number_can_be_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            fixture._write_campaign(canonical="attempt_001")
            target = read_json(fixture.target / "target.json")
            target["canonical_attempt_id"] = "attempt_001"
            atomic_write_json(fixture.target / "target.json", target)
            old = read_json(fixture.old / "job.json")
            old["reusable"] = True
            old["execution"]["status"] = "SUCCESS"
            old["scientific"]["status"] = "VALID"
            atomic_write_json(fixture.old / "job.json", old)
            failed = read_json(fixture.canonical / "job.json")
            failed["reusable"] = False
            failed["execution"]["status"] = "FAILED"
            failed["scientific"]["status"] = "INVALID"
            atomic_write_json(fixture.canonical / "job.json", failed)
            plan = fixture.plan()
            self.assertEqual([item.attempt_id for item in plan.candidates], ["attempt_002"])
            self.assertIn("attempt is canonical", next(item for item in plan.ineligible if item.attempt_id == "attempt_001").reasons)

    def test_03_highest_failed_attempt_does_not_become_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            fixture.add_attempt("attempt_003", reusable=False, execution="FAILED", scientific="INVALID")
            plan = fixture.plan()
            self.assertEqual({item.attempt_id for item in plan.candidates}, {"attempt_001", "attempt_003"})

    def test_04_explicit_supersession_relation_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            old = read_json(fixture.old / "job.json")
            old["superseded_by_attempt_id"] = "attempt_002"
            atomic_write_json(fixture.old / "job.json", old)
            plan = fixture.plan()
            self.assertEqual(plan.candidates[0].canonical_attempt_id, "attempt_002")

    def test_05_active_attempt_blocks_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            old = read_json(fixture.old / "job.json")
            old["execution"]["status"] = "RUNNING"
            atomic_write_json(fixture.old / "job.json", old)
            plan = fixture.plan()
            self.assertTrue(plan.global_blockers)
            self.assertFalse(plan.candidates)

    def test_06_active_watcher_blocks_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            fixture._write_campaign(canonical="attempt_002", watcher={"active": True, "pid": os.getpid()})
            plan = fixture.plan()
            self.assertTrue(any("ACTIVE_WATCHER" in item for item in plan.global_blockers))

    def test_07_stale_dead_lock_is_diagnostic_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            lock = fixture.root / "campaign.lock"
            atomic_write_json(
                lock,
                {
                    "schema_version": 1,
                    "token": "stale",
                    "hostname": socket.gethostname(),
                    "pid": 999_999_999,
                    "job_id": "old",
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            )
            plan = fixture.plan()
            self.assertTrue(plan.candidates)
            self.assertTrue(any(item.get("state") == "STALE_LOCAL" for item in plan.process_lock_evidence))

    def test_08_ambiguous_pid_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            old = read_json(fixture.old / "job.json")
            old["pid"] = 42
            atomic_write_json(fixture.old / "job.json", old)
            with patch("cmw.core.attempt_cleanup._pid_state", return_value="UNCERTAIN"):
                plan = fixture.plan()
            self.assertEqual(cleanup_exit_code(plan), CleanupExitCode.ACTIVE_CAMPAIGN)

    def test_09_finalization_failed_recoverable_attempt_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            old = read_json(fixture.old / "job.json")
            old["execution"]["status"] = "SUCCESS"
            atomic_write_json(fixture.old / "job.json", old)
            atomic_write_json(fixture.old / "finalization-failure.json", {"status": "FAILED"})
            plan = fixture.plan()
            self.assertTrue(any("recovery" in reason for reason in plan.ineligible[0].reasons))

    def test_10_refinalized_old_attempt_can_be_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            old = read_json(fixture.old / "job.json")
            old["reusable"] = True
            old["execution"]["status"] = "SUCCESS"
            old["scientific"]["status"] = "VALID"
            atomic_write_json(fixture.old / "job.json", old)
            self.assertEqual([item.attempt_id for item in fixture.plan().candidates], ["attempt_001"])

    def test_11_current_artifact_payload_reference_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            atomic_write_json(
                fixture.root / "result.json",
                {"artifacts": {"old_output": {"path": str(fixture.old / "stage.out")}}},
            )
            plan = fixture.plan()
            self.assertTrue(any("authoritative references" in reason for reason in plan.ineligible[0].reasons))

    def test_12_parent_artifact_lineage_reference_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            atomic_write_json(
                fixture.root / "lineage.json",
                {"parent_artifacts": ["artifact-attempt_001"]},
            )
            plan = fixture.plan()
            self.assertFalse(plan.candidates)

    def test_13_multiple_reusable_attempts_without_canonical_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            fixture._write_campaign(canonical=None)
            target = read_json(fixture.target / "target.json")
            target.pop("canonical_attempt_id", None)
            atomic_write_json(fixture.target / "target.json", target)
            old = read_json(fixture.old / "job.json")
            old["reusable"] = True
            old["execution"]["status"] = "SUCCESS"
            old["scientific"]["status"] = "VALID"
            atomic_write_json(fixture.old / "job.json", old)
            plan = fixture.plan()
            self.assertFalse(plan.candidates)
            self.assertTrue(any("multiple reusable" in reason for item in plan.ineligible for reason in item.reasons))

    def test_14_only_finalized_attempt_is_not_cleanup_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            _safe_remove_tree(fixture.old, root=fixture.root)
            plan = fixture.plan()
            self.assertFalse(plan.candidates)
            self.assertIn("attempt is canonical", plan.ineligible[0].reasons)

    def test_15_no_finalized_replacement_blocks_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            canonical = read_json(fixture.canonical / "job.json")
            canonical["reusable"] = False
            canonical["execution"]["status"] = "FAILED"
            canonical["scientific"]["status"] = "INVALID"
            atomic_write_json(fixture.canonical / "job.json", canonical)
            plan = fixture.plan()
            self.assertFalse(plan.candidates)


class PathAndPlanTests(unittest.TestCase):
    def test_16_shared_hardlink_is_accounted_without_deleting_shared_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            shared = fixture.root / "shared.bin"
            shared.write_bytes(b"shared payload")
            os.link(shared, fixture.old / "shared.bin")
            plan = fixture.plan()
            self.assertGreater(plan.candidates[0].inventory.shared_hardlink_bytes, 0)
            apply_cleanup_plan(plan, open_writer_probe=_no_writer)
            self.assertEqual(shared.read_bytes(), b"shared payload")

    def test_17_symlink_escape_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            outside = fixture.root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (fixture.old / "escape").symlink_to(outside)
            plan = fixture.plan()
            self.assertFalse(plan.candidates)
            self.assertTrue(any("symlink escapes" in reason for reason in plan.ineligible[0].reasons))

    def test_18_broken_symlink_is_safely_unlinked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            (fixture.old / "broken").symlink_to(fixture.old / "missing")
            plan = fixture.plan()
            self.assertTrue(plan.candidates)
            apply_cleanup_plan(plan, open_writer_probe=_no_writer)
            self.assertFalse((fixture.old / "broken").exists())

    def test_19_attempt_symlink_outside_campaign_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = SyntheticCampaign(base)
            _safe_remove_tree(fixture.old, root=fixture.root)
            outside = base / "outside-attempt"
            outside.mkdir()
            (fixture.target / "attempts" / "attempt_001").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(InvalidCleanupPath):
                fixture.plan()

    def test_20_campaign_root_deletion_is_impossible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            with self.assertRaises(InvalidCleanupPath):
                _safe_remove_tree(fixture.root, root=fixture.root)

    def test_21_dry_run_makes_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            before = sorted(str(path.relative_to(fixture.root)) for path in fixture.root.rglob("*"))
            fixture.plan()
            after = sorted(str(path.relative_to(fixture.root)) for path in fixture.root.rglob("*"))
            self.assertEqual(before, after)

    def test_22_plan_hash_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            self.assertEqual(fixture.plan().plan_sha256, fixture.plan().plan_sha256)

    def test_23_stale_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = fixture.plan()
            target = read_json(fixture.target / "target.json")
            target["changed"] = True
            atomic_write_json(fixture.target / "target.json", target)
            with self.assertRaisesRegex(CleanupError, "campaign state changed") as caught:
                apply_cleanup_plan(plan, open_writer_probe=_no_writer)
            self.assertEqual(caught.exception.exit_code, CleanupExitCode.STALE_PLAN)

    def test_24_large_byte_accounting_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            payload = fixture.old / "large.gbw"
            payload.write_bytes(b"x" * 1_000_001)
            plan = fixture.plan()
            self.assertGreaterEqual(plan.candidates[0].inventory.total_bytes, 1_000_001)
            self.assertEqual(plan.estimated_reclaimed_bytes, plan.candidates[0].inventory.estimated_reclaimed_bytes)

    def test_25_unicode_and_space_campaign_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary), name="campaign space α")
            self.assertTrue(fixture.plan().candidates)

    def test_26_campaign_id_resolves_from_search_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = SyntheticCampaign(base)
            self.assertEqual(resolve_campaign(fixture.campaign_id, search_roots=(base,)), fixture.root)

    def test_27_home_or_root_campaign_path_is_rejected(self) -> None:
        for path in (Path.home(), Path("/")):
            with self.assertRaises(InvalidCleanupPath):
                resolve_campaign(path)

    def test_28_open_writer_blocks_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = build_cleanup_plan(
                fixture.root,
                open_writer_probe=lambda _: (True, "synthetic writer"),
            )
            self.assertFalse(plan.candidates)

    def test_29_unavailable_optional_writer_probe_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = build_cleanup_plan(
                fixture.root,
                open_writer_probe=lambda _: (None, "unavailable"),
            )
            self.assertTrue(plan.candidates)
            self.assertTrue(any(item.get("state") == "UNCERTAIN" for item in plan.process_lock_evidence if item.get("kind") == "open_writer"))


class ApplyAndRegistryTests(unittest.TestCase):
    def test_30_minimal_provenance_creates_bounded_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            result = apply_cleanup_plan(fixture.plan(reason="storage pressure"), reason="storage pressure", open_writer_probe=_no_writer)
            tombstone = read_json(fixture.old / "attempt-tombstone.json")
            self.assertEqual(result.status, "COMMITTED")
            self.assertEqual(tombstone["cleanup"]["reason"], "storage pressure")
            self.assertLess((fixture.old / "attempt-tombstone.json").stat().st_size, 256 * 1024)

    def test_31_tombstone_size_cap_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            (fixture.old / "stage.err").write_text("x" * 5000, encoding="utf-8")
            old = read_json(fixture.old / "job.json")
            old["artifacts"]["stderr"] = {
                "path": "stage.err",
                "size_bytes": 5000,
                "sha256": file_hash(fixture.old / "stage.err"),
            }
            atomic_write_json(fixture.old / "job.json", old)
            plan = fixture.plan(tombstone_max_bytes=1024, diagnostic_tail_bytes=5000)
            self.assertFalse(plan.candidates)
            self.assertTrue(any("exceeds" in reason for reason in plan.ineligible[0].reasons))

    def test_32_diagnostic_tail_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            (fixture.old / "stage.err").write_text("0123456789" * 20, encoding="utf-8")
            plan = fixture.plan(diagnostic_tail_bytes=12)
            diagnostic = plan.candidates[0].tombstone["diagnostic_tail"]
            self.assertLessEqual(diagnostic["retained_bytes"], 12)
            self.assertTrue(diagnostic["truncated"])

    def test_33_purge_leaves_no_attempt_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = fixture.plan(CleanupMode.PURGE)
            apply_cleanup_plan(plan, confirm_plan=plan.plan_sha256, open_writer_probe=_no_writer)
            self.assertFalse(fixture.old.exists())

    def test_34_purge_requires_full_exact_plan_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = fixture.plan(CleanupMode.PURGE)
            for confirmation in (None, plan.plan_sha256[:12], "0" * 64):
                with self.assertRaises(CleanupError) as caught:
                    apply_cleanup_plan(plan, confirm_plan=confirmation, open_writer_probe=_no_writer)
                self.assertEqual(caught.exception.exit_code, CleanupExitCode.INVALID_ARGUMENT)

    def test_35_purge_receipt_is_non_reconstructive_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = fixture.plan(CleanupMode.PURGE)
            result = apply_cleanup_plan(plan, confirm_plan=plan.plan_sha256, reason="test", open_writer_probe=_no_writer)
            transaction = fixture.root / ".cmw" / "cleanup" / "transactions" / result.transaction_id
            receipt = read_json(transaction / "receipt.json")
            self.assertEqual(
                set(receipt),
                {
                    "cleanup_transaction_id",
                    "cleanup_mode",
                    "tool_version",
                    "timestamp",
                    "reason",
                    "attempts_removed",
                    "total_bytes_removed",
                    "pre_cleanup_campaign_state_hash",
                    "post_cleanup_campaign_state_hash",
                    "cleanup_plan_hash",
                    "state",
                },
            )
            self.assertFalse((transaction / "manifest.json").exists())

    def test_36_cleanup_registry_records_compacted_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            result = apply_cleanup_plan(fixture.plan(), open_writer_probe=_no_writer)
            entry = load_cleanup_registry(fixture.root)["attempts"][f"{fixture.target_id}/attempt_001"]
            self.assertEqual(entry["status"], "COMPACTED")
            self.assertEqual(entry["transaction_id"], result.transaction_id)

    def test_37_canonical_artifact_and_reuse_evidence_remain_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            canonical_hash = file_hash(fixture.canonical / "stage.out")
            apply_cleanup_plan(fixture.plan(), open_writer_probe=_no_writer)
            self.assertEqual(file_hash(fixture.canonical / "stage.out"), canonical_hash)
            plan = fixture.plan()
            canonical = next(item for item in plan.ineligible if item.attempt_id == "attempt_002")
            self.assertTrue(canonical.reusable)

    def test_38_repeated_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            apply_cleanup_plan(fixture.plan(), open_writer_probe=_no_writer)
            repeat = fixture.plan()
            result = apply_cleanup_plan(repeat, open_writer_probe=_no_writer)
            self.assertEqual(result.status, "NO_ELIGIBLE_ATTEMPTS")
            self.assertEqual(len(load_cleanup_registry(fixture.root)["transactions"]), 1)

    def test_39_already_compacted_attempt_is_reported_as_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            apply_cleanup_plan(fixture.plan(), open_writer_probe=_no_writer)
            self.assertEqual(fixture.plan().already_cleaned[0].already_cleaned, "COMPACTED")

    def test_40_already_purged_attempt_is_reported_as_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = fixture.plan(CleanupMode.PURGE)
            apply_cleanup_plan(plan, confirm_plan=plan.plan_sha256, open_writer_probe=_no_writer)
            self.assertEqual(fixture.plan(CleanupMode.PURGE).already_cleaned[0].already_cleaned, "PURGED")

    def test_41_purged_attempt_number_is_not_reallocated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = fixture.plan(CleanupMode.PURGE)
            apply_cleanup_plan(plan, confirm_plan=plan.plan_sha256, open_writer_probe=_no_writer)
            self.assertEqual(next_attempt_identifier(fixture.target), "attempt_003")

    def test_42_legacy_campaign_acquires_registry_only_on_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            (fixture.root / ".cmw" / "campaign.json").unlink()
            plan = fixture.plan()
            self.assertFalse((fixture.root / ".cmw" / "cleanup" / "registry.json").exists())
            apply_cleanup_plan(plan, open_writer_probe=_no_writer)
            self.assertTrue((fixture.root / ".cmw" / "cleanup" / "registry.json").is_file())

    def test_generic_enumerator_and_resolver_understand_cleanup_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            apply_cleanup_plan(fixture.plan(), open_writer_probe=_no_writer)
            attempts = enumerate_campaign_attempts(fixture.root)
            compacted = next(item for item in attempts if item["attempt_id"] == "attempt_001")
            self.assertEqual(compacted["cleanup_status"], "COMPACTED")
            resolved = resolve_canonical_attempts(fixture.root)
            self.assertEqual(resolved["canonical_attempts"][fixture.target_id], "attempt_002")


class RecoveryTests(unittest.TestCase):
    class SimulatedCrash(BaseException):
        pass

    def test_43_crash_before_staging_is_discoverable_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            with self.assertRaises(self.SimulatedCrash):
                apply_cleanup_plan(
                    fixture.plan(),
                    open_writer_probe=_no_writer,
                    fault_hook=lambda point: (_ for _ in ()).throw(self.SimulatedCrash()) if point == "before_staging" else None,
                )
            actions = recover_cleanup_transactions(fixture.root)
            self.assertEqual(actions[0]["action"], "ROLLED_BACK")
            self.assertTrue((fixture.old / "job.json").is_file())

    def test_44_crash_after_staging_rolls_back_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            with self.assertRaises(self.SimulatedCrash):
                apply_cleanup_plan(
                    fixture.plan(),
                    open_writer_probe=_no_writer,
                    fault_hook=lambda point: (_ for _ in ()).throw(self.SimulatedCrash()) if point == "after_staging" else None,
                )
            actions = recover_cleanup_transactions(fixture.root)
            self.assertEqual(actions[0]["action"], "ROLLED_BACK")
            self.assertTrue((fixture.old / "stage.out").is_file())

    def test_45_crash_during_metadata_update_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            with self.assertRaises(self.SimulatedCrash):
                apply_cleanup_plan(
                    fixture.plan(),
                    open_writer_probe=_no_writer,
                    fault_hook=lambda point: (_ for _ in ()).throw(self.SimulatedCrash()) if point == "during_metadata_update" else None,
                )
            actions = recover_cleanup_transactions(fixture.root)
            self.assertEqual(actions[0]["action"], "RESUMED")
            self.assertTrue((fixture.old / "attempt-tombstone.json").is_file())

    def test_46_ordinary_failure_before_irreversible_delete_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            with self.assertRaises(CleanupError):
                apply_cleanup_plan(
                    fixture.plan(),
                    open_writer_probe=_no_writer,
                    fault_hook=lambda point: (_ for _ in ()).throw(RuntimeError("fault")) if point == "after_staging" else None,
                )
            self.assertTrue((fixture.old / "job.json").is_file())
            states = [read_json(path)["state"] for path in fixture.root.glob(".cmw/cleanup/transactions/*/state.json")]
            self.assertEqual(states, [TransactionState.ROLLED_BACK.value])

    def test_47_permission_error_after_metadata_commit_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            with self.assertRaises(CleanupError) as caught:
                apply_cleanup_plan(
                    fixture.plan(),
                    open_writer_probe=_no_writer,
                    remove_tree=lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
                )
            self.assertEqual(caught.exception.exit_code, CleanupExitCode.ROLLBACK_REQUIRED)
            actions = recover_cleanup_transactions(fixture.root)
            self.assertEqual(actions[0]["action"], "RESUMED")

    def test_48_partial_filesystem_deletion_remains_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))

            def partial(path: Path, *, root: Path) -> int:
                first = next(item for item in path.rglob("*") if item.is_file())
                first.unlink()
                raise OSError("interrupted delete")

            with self.assertRaises(CleanupError):
                apply_cleanup_plan(fixture.plan(), open_writer_probe=_no_writer, remove_tree=partial)
            actions = recover_cleanup_transactions(fixture.root)
            self.assertEqual(actions[0]["action"], "RESUMED")

    def test_49_cleanup_lock_prevents_duplicate_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            lock = fixture.root / ".cmw" / "cleanup.lock"
            owner = acquire_lock(lock, job_id="other", owner_pid=os.getpid())
            try:
                with self.assertRaises(CleanupError) as caught:
                    apply_cleanup_plan(fixture.plan(), open_writer_probe=_no_writer)
                self.assertEqual(caught.exception.exit_code, CleanupExitCode.ACTIVE_CAMPAIGN)
            finally:
                release_lock(lock, token=owner.token)

    def test_50_implementation_never_invokes_shell_rm_rf(self) -> None:
        source = Path(__import__("cmw.core.attempt_cleanup", fromlist=["x"]).__file__).read_text(encoding="utf-8")
        self.assertNotIn("rm -rf", source)
        self.assertNotIn("shell=True", source)

    def test_apply_invocation_resumes_incomplete_metadata_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            with self.assertRaises(self.SimulatedCrash):
                apply_cleanup_plan(
                    fixture.plan(),
                    open_writer_probe=_no_writer,
                    fault_hook=lambda point: (_ for _ in ()).throw(self.SimulatedCrash()) if point == "during_metadata_update" else None,
                )
            interrupted = fixture.plan()
            result = apply_cleanup_plan(interrupted, open_writer_probe=_no_writer)
            self.assertEqual(result.status, "NO_ELIGIBLE_ATTEMPTS")
            self.assertEqual(result.recovery_actions[0]["action"], "RESUMED")

    def test_purge_recovery_writes_only_bounded_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            plan = fixture.plan(CleanupMode.PURGE)
            with self.assertRaises(self.SimulatedCrash):
                apply_cleanup_plan(
                    plan,
                    confirm_plan=plan.plan_sha256,
                    reason="recovery test",
                    open_writer_probe=_no_writer,
                    fault_hook=lambda point: (_ for _ in ()).throw(self.SimulatedCrash()) if point == "during_metadata_update" else None,
                )
            result = apply_cleanup_plan(fixture.plan(CleanupMode.PURGE), open_writer_probe=_no_writer)
            self.assertEqual(result.recovery_actions[0]["action"], "RESUMED")
            transaction = fixture.root / ".cmw" / "cleanup" / "transactions" / result.recovery_actions[0]["transaction_id"]
            self.assertTrue((transaction / "receipt.json").is_file())
            self.assertFalse((transaction / "manifest.json").exists())
            self.assertFalse((transaction / "plan.json").exists())


if __name__ == "__main__":
    unittest.main()
