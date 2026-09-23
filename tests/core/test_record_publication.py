from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cmw.core.preparation_publication import (
    PublicationError, publish_record, record_publication_plan,
)


class RecordPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.source = self.root / "run"
        self.source.mkdir()
        (self.source / "OUTCAR").write_text("synthetic source only\n")

    def plan(self, record="result", **kwargs):
        return record_publication_plan(self.scratch, record, sources=[self.source], **kwargs)

    def read_record(self):
        return json.loads((self.scratch / "result/finalization.json").read_text())

    def test_plan_writes_nothing_and_publication_writes_record_only(self):
        plan = self.plan()
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertNotIn("input_path", plan)
        original = (self.source / "OUTCAR").read_bytes()
        result = publish_record({"record_kind": "synthetic_finalization", "evidence_id": "e1"}, plan)
        self.assertEqual(result["evidence_id"], "e1")
        self.assertEqual(result["publication"]["state"], "complete")
        self.assertEqual(result["publication"]["kind"], "record")
        self.assertEqual(self.read_record(), result)
        self.assertEqual({p.name for p in (self.scratch / "result").iterdir()}, {"finalization.json"})
        self.assertEqual({p.name for p in self.source.iterdir()}, {"OUTCAR"})
        self.assertEqual((self.source / "OUTCAR").read_bytes(), original)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"scratch", "run"})
        self.assertNotIn("prepared_inputs", result)

    def test_validator_observes_intent_and_is_called_once(self):
        calls = []

        def validate():
            calls.append(self.read_record()["publication"]["state"])
            return {"valid": True, "sources": [{"role": "OUTCAR", "sha256": "synthetic"}]}

        result = publish_record({}, self.plan(), validate)
        self.assertEqual(calls, ["intent"])
        self.assertTrue(result["publication"]["source_revalidation"]["valid"])

    def test_record_data_is_frozen_before_validation(self):
        caller = {"nested": {"identity": "original"}}

        def validate():
            caller["nested"]["identity"] = "changed"

        result = publish_record(caller, self.plan(), validate)
        self.assertEqual(result["nested"]["identity"], "original")

    def test_rejected_validator_persists_failed_record(self):
        for index, outcome in enumerate((False, {"valid": False}, {"valid": 1}, "unknown")):
            with self.subTest(outcome=outcome):
                plan = self.plan(record=f"result-{index}")
                with self.assertRaises(PublicationError) as caught:
                    publish_record({}, plan, lambda: outcome)
                self.assertEqual(caught.exception.code, "PUBLICATION_FAILED")
                self.assertFalse(caught.exception.incomplete)
                value = json.loads(Path(plan["record_path"]).read_text())
                self.assertEqual(value["publication"]["state"], "failed")
                self.assertNotIn("completed_at", value["publication"])

    def test_raised_validator_exception_persists_failure(self):
        def validate():
            raise RuntimeError("source replaced")

        with self.assertRaisesRegex(PublicationError, "source replaced"):
            publish_record({}, self.plan(), validate)
        self.assertEqual(self.read_record()["publication"]["state"], "failed")

    def test_existing_record_directory_refused_without_rewrite(self):
        plan = self.plan()
        result = publish_record({}, plan)
        before = Path(plan["record_path"]).read_bytes()
        with self.assertRaises(PublicationError):
            publish_record({"replacement": True}, plan)
        with self.assertRaises(PublicationError):
            self.plan()
        self.assertEqual(Path(plan["record_path"]).read_bytes(), before)
        self.assertEqual(self.read_record()["publication_id"], result["publication_id"])

    def test_empty_existing_directory_also_refused(self):
        (self.scratch / "result").mkdir()
        with self.assertRaises(PublicationError):
            self.plan()
        self.assertEqual(list((self.scratch / "result").iterdir()), [])

    def test_missing_scratch_parent_invalid_mount_and_unsafe_names(self):
        with self.assertRaises(PublicationError):
            record_publication_plan(self.root / "missing", "case")
        with self.assertRaises(PublicationError):
            self.plan(record="missing/case")
        with self.assertRaises(PublicationError):
            self.plan(scratch_mount=self.scratch)
        for name in ("", ".", "..", "../finalization.json", "a/b.json", "a\\b.json"):
            with self.subTest(name=name), self.assertRaises(PublicationError):
                self.plan(filename=name)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_whole_source_directory_and_outside_scratch_refused(self):
        nested_scratch = self.source / "scratch"
        nested_scratch.mkdir()
        with self.assertRaises(PublicationError):
            record_publication_plan(nested_scratch, "result", sources=[self.source])
        for name in (".", "../outside"):
            with self.subTest(name=name), self.assertRaises(PublicationError):
                self.plan(record=name)
        self.assertEqual(list(nested_scratch.iterdir()), [])

    def test_symlink_scratch_source_and_record_parent_refused(self):
        alias = self.root / "alias"
        alias.symlink_to(self.scratch, target_is_directory=True)
        with self.assertRaises(PublicationError):
            record_publication_plan(alias, "case")
        source_alias = self.root / "source-alias"
        source_alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(PublicationError):
            record_publication_plan(self.scratch, "case", sources=[source_alias])
        (self.scratch / "alias").symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(PublicationError):
            self.plan(record="alias/result")

    def test_plan_parent_replacement_detected_before_write(self):
        parent = self.scratch / "parent"
        parent.mkdir()
        plan = self.plan(record="parent/case")
        parent.rename(self.scratch / "old-parent")
        parent.mkdir()
        with self.assertRaises(PublicationError):
            publish_record({}, plan)
        self.assertEqual(list(parent.iterdir()), [])

    def test_nonfinite_or_nonserializable_record_has_no_writes(self):
        for record in ({"invalid": float("nan")}, {"invalid": object()}):
            with self.subTest(record=record), self.assertRaises((TypeError, ValueError)):
                publish_record(record, self.plan())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_intent_write_failure_persists_failed_if_possible(self):
        with patch("cmw.core.preparation_publication._write_candidate", side_effect=OSError("intent failed")):
            with self.assertRaises(PublicationError) as caught:
                publish_record({}, self.plan())
        self.assertFalse(caught.exception.incomplete)
        self.assertEqual(self.read_record()["publication"]["state"], "failed")

    def test_completion_failure_persists_incomplete(self):
        from cmw.core.preparation_publication import _write_record

        def fail_complete(path, record):
            if record["publication"]["state"] == "complete":
                raise OSError("completion failed")
            _write_record(path, record)

        with patch("cmw.core.preparation_publication._write_record", side_effect=fail_complete):
            with self.assertRaises(PublicationError) as caught:
                publish_record({}, self.plan())
        self.assertTrue(caught.exception.incomplete)
        self.assertEqual(caught.exception.code, "PUBLICATION_INCOMPLETE")
        self.assertEqual(self.read_record()["publication"]["state"], "incomplete")

    def test_post_replacement_sync_failure_cannot_leave_false_complete(self):
        from cmw.core.preparation_publication import _sync_directory
        record_path = self.scratch / "result/finalization.json"
        failures = []

        def fail_sync(path):
            if path == record_path.parent and record_path.exists():
                state = json.loads(record_path.read_text())["publication"]["state"]
                if state in {"complete", "incomplete"}:
                    failures.append(state)
                    raise OSError("directory sync failed")
            _sync_directory(path)

        with patch("cmw.core.preparation_publication._sync_directory", side_effect=fail_sync):
            with self.assertRaises(PublicationError):
                publish_record({}, self.plan())
        self.assertEqual(failures, ["complete", "incomplete"])
        self.assertEqual(self.read_record()["publication"]["state"], "incomplete")
        self.assertNotIn("completed_at", self.read_record()["publication"])

    def test_directory_ownership_change_does_not_write_to_new_owner(self):
        def validate():
            owned = self.scratch / "result"
            owned.rename(self.scratch / "original")
            owned.mkdir()
            (owned / "foreign").write_text("untouched")

        with self.assertRaises(PublicationError):
            publish_record({}, self.plan(), validate)
        self.assertEqual({p.name for p in (self.scratch / "result").iterdir()}, {"foreign"})
        self.assertEqual((self.scratch / "result/foreign").read_text(), "untouched")
        original = json.loads((self.scratch / "original/finalization.json").read_text())
        self.assertEqual(original["publication"]["state"], "intent")

    def test_foreign_record_change_is_not_overwritten(self):
        def validate():
            (self.scratch / "result/finalization.json").write_text('{"foreign":true}\n')

        with self.assertRaises(PublicationError):
            publish_record({}, self.plan(), validate)
        self.assertEqual(self.read_record(), {"foreign": True})

    def test_concurrent_publishers_have_one_winner(self):
        code = '''
import sys,time
from pathlib import Path
from cmw.core.preparation_publication import record_publication_plan,publish_record,PublicationError
root=Path(sys.argv[1]); label=sys.argv[2]
try:
 plan=record_publication_plan(root/'scratch','shared',sources=[root/'run'])
 (root/('ready-'+label)).touch()
 deadline=time.monotonic()+10
 while not (root/'release').exists():
  if time.monotonic()>deadline:raise RuntimeError('barrier timeout')
  time.sleep(.01)
 publish_record({'winner':label},plan)
except PublicationError:
 sys.exit(3)
'''
        environment = os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.root), label],
                                     env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for label in ("first", "second")]
        try:
            deadline = time.monotonic() + 10
            while not all((self.root / ("ready-" + label)).exists() for label in ("first", "second")):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            (self.root / "release").touch()
            responses = [process.communicate(timeout=15) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 3], responses)
            value = json.loads((self.scratch / "shared/finalization.json").read_text())
            self.assertIn(value["winner"], {"first", "second"})
            self.assertEqual(value["publication"]["state"], "complete")
            self.assertEqual({p.name for p in (self.scratch / "shared").iterdir()}, {"finalization.json"})
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_abrupt_exit_during_validation_retains_intent(self):
        code = '''
import os,sys
from pathlib import Path
from cmw.core.preparation_publication import record_publication_plan,publish_record
root=Path(sys.argv[1])
plan=record_publication_plan(root/'scratch','crash')
publish_record({},plan,lambda:os._exit(17))
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}, timeout=15)
        self.assertEqual(result.returncode, 17)
        value = json.loads((self.scratch / "crash/finalization.json").read_text())
        self.assertEqual(value["publication"]["state"], "intent")
        self.assertEqual({p.name for p in (self.scratch / "crash").iterdir()}, {"finalization.json"})


if __name__ == "__main__":
    unittest.main()
