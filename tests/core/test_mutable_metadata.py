from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cmw.core.mutable_metadata import (
    MutableBackupPolicy,
    MutableRecordError,
    apply_structured_json_patch,
    plan_structured_json_patch,
)
from cmw.core.provenance import file_hash


class MutableMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw mutable metadata ")
        self.root = Path(self.temporary.name)
        self.old = "/legacy/calculation/target"
        self.new = "/campaign/target-deadbeef"
        self.policy = MutableBackupPolicy(full_copy_max_bytes=1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _record(self, value: object, *, name: str = "record.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def test_nested_multiple_fields_apply_and_inverse_restore_exact_bytes(self) -> None:
        path = self._record(
            {
                "schema_version": 1,
                "control": {"current": self.old, "attempt": self.old + "/attempts/attempt_001"},
                "payload": ["unrelated-" + ("x" * 1024)] * 2048,
            }
        )
        original = path.read_bytes()
        plan = plan_structured_json_patch(
            path, {self.old: self.new}, policy=self.policy
        )
        self.assertEqual(
            {item.pointer for item in plan.patches},
            {"/control/current", "/control/attempt"},
        )
        self.assertLess(plan.projected_backup_bytes, len(original) // 100)
        apply_structured_json_patch(
            path,
            plan.patches,
            expected_source_sha256=plan.preimage_sha256,
            expected_result_sha256=plan.postimage_sha256,
        )
        self.assertEqual(json.loads(path.read_text())["payload"][0], "unrelated-" + ("x" * 1024))
        apply_structured_json_patch(
            path,
            plan.patches,
            reverse=True,
            expected_source_sha256=plan.postimage_sha256,
            expected_result_sha256=plan.preimage_sha256,
        )
        self.assertEqual(path.read_bytes(), original)

    def test_preimage_and_postimage_hash_mismatches_fail_closed(self) -> None:
        path = self._record(
            {"schema_version": 1, "path": self.old, "payload": "stable"}
        )
        plan = plan_structured_json_patch(path, {self.old: self.new}, policy=self.policy)
        path.write_text(path.read_text() + " ", encoding="utf-8")
        with self.assertRaisesRegex(MutableRecordError, "unexpected mutable record hash") as caught:
            apply_structured_json_patch(
                path,
                plan.patches,
                expected_source_sha256=plan.preimage_sha256,
                expected_result_sha256=plan.postimage_sha256,
            )
        self.assertEqual(caught.exception.code, "MUTABLE_RECORD_PREIMAGE_MISMATCH")
        path.write_bytes(
            json.dumps(
                {"schema_version": 1, "path": self.new, "payload": "changed"},
                indent=2,
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        with self.assertRaises(MutableRecordError) as rollback:
            apply_structured_json_patch(
                path,
                plan.patches,
                reverse=True,
                expected_source_sha256=plan.postimage_sha256,
                expected_result_sha256=plan.preimage_sha256,
            )
        self.assertEqual(rollback.exception.code, "MUTABLE_RECORD_POSTIMAGE_MISMATCH")

    def test_absent_expected_pointer_and_schema_mismatch_fail_closed(self) -> None:
        path = self._record({"schema_version": 1, "path": self.old})
        with self.assertRaises(MutableRecordError) as absent:
            plan_structured_json_patch(
                path,
                {self.old: self.new},
                policy=self.policy,
                expected_pointers=("/missing",),
            )
        self.assertEqual(absent.exception.code, "MUTABLE_RECORD_EXPECTED_FIELD_ABSENT")
        path = self._record({"schema_version": 99, "path": self.old}, name="schema.json")
        with self.assertRaises(MutableRecordError) as schema:
            plan_structured_json_patch(path, {self.old: self.new}, policy=self.policy)
        self.assertEqual(schema.exception.code, "MUTABLE_RECORD_SCHEMA_MISMATCH")

    def test_duplicate_key_and_unbounded_line_are_unsafe(self) -> None:
        duplicate = self.root / "duplicate.json"
        duplicate.write_text(
            '{"schema_version":1,"path":"%s","path":"%s"}\n'
            % (self.old, self.old),
            encoding="utf-8",
        )
        with self.assertRaises(MutableRecordError) as ambiguous:
            plan_structured_json_patch(
                duplicate, {self.old: self.new}, policy=self.policy
            )
        self.assertEqual(ambiguous.exception.code, "AMBIGUOUS_STRUCTURED_TARGET")
        long_line = self.root / "long.json"
        long_line.write_text(
            json.dumps({"schema_version": 1, "path": self.old, "payload": "x" * 4096}),
            encoding="utf-8",
        )
        bounded = MutableBackupPolicy(
            full_copy_max_bytes=1, structured_patch_max_line_bytes=1024
        )
        with self.assertRaises(MutableRecordError) as unsafe:
            plan_structured_json_patch(
                long_line, {self.old: self.new}, policy=bounded
            )
        self.assertEqual(unsafe.exception.code, "UNSAFE_LARGE_RECORD")

    def test_changed_field_limit_is_enforced(self) -> None:
        path = self._record(
            {"schema_version": 1, "paths": [self.old, self.old, self.old]}
        )
        policy = MutableBackupPolicy(
            full_copy_max_bytes=1, structured_patch_max_changed_fields=2
        )
        with self.assertRaises(MutableRecordError) as caught:
            plan_structured_json_patch(path, {self.old: self.new}, policy=policy)
        self.assertEqual(caught.exception.code, "UNSAFE_LARGE_RECORD")

    def test_patch_backup_byte_limit_is_enforced(self) -> None:
        path = self._record({"schema_version": 1, "path": self.old + "/" + ("x" * 2048)})
        policy = MutableBackupPolicy(
            full_copy_max_bytes=1, structured_patch_max_backup_bytes=1024
        )
        with self.assertRaises(MutableRecordError) as caught:
            plan_structured_json_patch(path, {self.old: self.new}, policy=policy)
        self.assertEqual(caught.exception.code, "UNSAFE_LARGE_RECORD")

    def test_forward_result_hash_is_deterministic(self) -> None:
        path = self._record({"schema_version": 1, "path": self.old})
        first = plan_structured_json_patch(path, {self.old: self.new}, policy=self.policy)
        second = plan_structured_json_patch(path, {self.old: self.new}, policy=self.policy)
        self.assertEqual(first.patch_sha256, second.patch_sha256)
        apply_structured_json_patch(
            path,
            first.patches,
            expected_source_sha256=first.preimage_sha256,
            expected_result_sha256=first.postimage_sha256,
        )
        self.assertEqual(file_hash(path), first.postimage_sha256)

    def test_large_indented_scanner_uses_exact_offset_locators(self) -> None:
        path = self._record(
            {
                "schema_version": 1,
                "systems": [
                    {"layout": {"working_directory": self.old + "/attempts/attempt_001"}},
                    {"layout": {"working_directory": self.old + "/attempts/attempt_002"}},
                ],
                "payload": [1, 2, 3],
            }
        )
        original = path.read_bytes()
        with mock.patch(
            "cmw.core.mutable_metadata.LARGE_STRUCTURED_SCANNER_THRESHOLD_BYTES", 1
        ):
            plan = plan_structured_json_patch(
                path, {self.old: self.new}, policy=self.policy
            )
        self.assertEqual(len(plan.patches), 2)
        self.assertTrue(all("/@" in item.pointer for item in plan.patches))
        apply_structured_json_patch(
            path,
            plan.patches,
            expected_source_sha256=plan.preimage_sha256,
            expected_result_sha256=plan.postimage_sha256,
        )
        apply_structured_json_patch(
            path,
            plan.patches,
            reverse=True,
            expected_source_sha256=plan.postimage_sha256,
            expected_result_sha256=plan.preimage_sha256,
        )
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
