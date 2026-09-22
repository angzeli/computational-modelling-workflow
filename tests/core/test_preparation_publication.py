from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cmw.core.preparation_publication import (
    PublicationError, inspect_publication, publication_plan, publish_preparation,
)


class PreparationPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.source = self.root / "source"
        self.source.write_text("unchanged")
        self.files = {name: (name + " synthetic only\n").encode()
                      for name in ("INCAR", "KPOINTS", "POSCAR", "POTCAR")}

    def plan(self, output="inputs", record="case", **kwargs):
        return publication_plan(output=self.root / output, scratch_root=self.scratch,
                                record_directory=record, sources=[self.source], **kwargs)

    def test_preview_and_four_file_publication(self):
        plan = self.plan()
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse((self.root / "inputs").exists())
        result = publish_preparation(self.files, {"record_kind": "synthetic"}, plan)
        self.assertEqual(set(p.name for p in (self.root / "inputs").iterdir()), set(self.files))
        self.assertEqual(set(p.name for p in (self.scratch / "case").iterdir()), {"preparation.json"})
        self.assertEqual(result["publication"]["state"], "complete")
        self.assertEqual(inspect_publication(self.scratch / "case/preparation.json")["exit_code"], 0)
        self.assertEqual(self.source.read_text(), "unchanged")
        self.assertFalse(list(self.root.glob(".cmw-preparation-*")))

    def test_file_publication_has_no_sidecars(self):
        plan = self.plan(output="POSCAR", kind="file")
        publish_preparation({"POSCAR": b"synthetic"}, {}, plan)
        self.assertEqual((self.root / "POSCAR").read_bytes(), b"synthetic")
        self.assertFalse(list(self.root.glob("*.json")))
        self.assertEqual(inspect_publication(self.scratch / "case/preparation.json")["exit_code"], 0)

    def test_malformed_record_shapes_return_structured_findings(self):
        valid = publish_preparation(self.files, {}, self.plan())
        path = self.scratch / "malformed.json"
        malformed = [[], 1, "record", None, True]
        for field, values in {
            "schema_version": [True, 1.0, None, [], {"version": 1}],
            "publication": [[], "complete", 1, None],
            "prepared_inputs": [[], "INCAR", 1, None],
        }.items():
            malformed.extend({**valid, field: value} for value in values)
        for value in malformed:
            with self.subTest(value=value):
                payload = json.dumps(value).encode()
                path.write_bytes(payload)
                result = inspect_publication(path)
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["exit_code"], 1)
                self.assertIn("PUBLICATION_RECORD_INVALID", [item["code"] for item in result["findings"]])
                self.assertEqual(path.read_bytes(), payload)

    def test_unavailable_aliases_overlap_and_existing_destinations(self):
        with self.assertRaises(PublicationError):
            publication_plan(output=self.root / "inputs", scratch_root=self.root / "missing",
                             record_directory="case")
        self.assertFalse((self.root / "inputs").exists())
        with self.assertRaises(PublicationError):
            self.plan(output="scratch", record="case")
        with self.assertRaises(PublicationError):
            self.plan(output="source")
        with self.assertRaises(PublicationError):
            self.plan(record="../outside")
        (self.root / "inputs").mkdir()
        with self.assertRaises(PublicationError):
            self.plan()
        self.assertEqual(list((self.root / "inputs").iterdir()), [])
        (self.scratch / "case").mkdir()
        with self.assertRaises(PublicationError):
            self.plan(output="another")
        (self.root / "alias").symlink_to(self.scratch, target_is_directory=True)
        with self.assertRaises(PublicationError):
            self.plan(output="alias/inputs", record="different")

    def test_staging_failure_retains_record_but_no_inputs_or_temporary(self):
        with patch("cmw.core.preparation_publication._write_candidate", side_effect=OSError("injected staging error")):
            with self.assertRaises(PublicationError) as raised:
                publish_preparation(self.files, {}, self.plan())
        self.assertFalse(raised.exception.incomplete)
        self.assertFalse((self.root / "inputs").exists())
        self.assertFalse(list(self.root.glob(".cmw-preparation-*")))
        record = json.loads((self.scratch / "case/preparation.json").read_text())
        self.assertEqual(record["publication"]["state"], "failed")
        self.assertEqual(inspect_publication(self.scratch / "case/preparation.json")["exit_code"], 1)

    def test_finalization_failure_never_deletes_published_inputs(self):
        from cmw.core.preparation_publication import _write_record

        def fail_final(path, record):
            if record["publication"]["state"] == "complete":
                raise OSError("injected record finalization error")
            _write_record(path, record)

        with patch("cmw.core.preparation_publication._write_record", side_effect=fail_final):
            with self.assertRaises(PublicationError) as raised:
                publish_preparation(self.files, {}, self.plan())
        self.assertTrue(raised.exception.incomplete)
        self.assertEqual(set(p.name for p in (self.root / "inputs").iterdir()), set(self.files))
        self.assertEqual(json.loads((self.scratch / "case/preparation.json").read_text())["publication"]["state"], "incomplete")
        self.assertEqual(inspect_publication(self.scratch / "case/preparation.json")["exit_code"], 1)
        with self.assertRaises(PublicationError):
            self.plan()

    def test_file_cleanup_failure_is_incomplete_and_does_not_override_error(self):
        with patch("cmw.core.preparation_publication.shutil.rmtree", side_effect=OSError("injected cleanup failure")):
            with self.assertRaises(PublicationError) as raised:
                publish_preparation({"POSCAR": b"synthetic"}, {}, self.plan(output="POSCAR", kind="file"))
        self.assertTrue(raised.exception.incomplete)
        self.assertEqual(raised.exception.code, "PUBLICATION_INCOMPLETE")
        self.assertEqual((self.root / "POSCAR").read_bytes(), b"synthetic")
        record_path = self.scratch / "case/preparation.json"
        state = json.loads(record_path.read_text())["publication"]
        self.assertEqual(state["state"], "incomplete")
        self.assertIn("injected cleanup failure", state["cleanup_error"])
        self.assertTrue(Path(state["staging_path"]).is_dir())
        self.assertEqual(inspect_publication(record_path)["exit_code"], 1)

    def test_final_record_sync_failure_stays_incomplete_when_secondary_sync_fails(self):
        from cmw.core.preparation_publication import _sync_directory
        record_path = self.scratch / "case/preparation.json"
        failed_states = []

        def fail_after_record_replacement(path):
            if path == record_path.parent and record_path.exists():
                state = json.loads(record_path.read_text())["publication"]["state"]
                if state in {"complete", "incomplete"}:
                    failed_states.append(state)
                    raise OSError("injected post-replacement directory sync failure")
            _sync_directory(path)

        with patch("cmw.core.preparation_publication._sync_directory", side_effect=fail_after_record_replacement):
            with self.assertRaises(PublicationError) as raised:
                publish_preparation(self.files, {}, self.plan())
        self.assertEqual(failed_states, ["complete", "incomplete"])
        self.assertTrue(raised.exception.incomplete)
        state = json.loads(record_path.read_text())["publication"]
        self.assertEqual(state["state"], "incomplete")
        self.assertNotIn("completed_at", state)
        self.assertEqual(inspect_publication(record_path)["exit_code"], 1)
        self.assertEqual(set(p.name for p in (self.root / "inputs").iterdir()), set(self.files))
        self.assertFalse(list(self.root.glob(".cmw-preparation-*")))

    def test_no_replace_if_empty_directory_appears_after_plan(self):
        from cmw.core.preparation_publication import _rename_exclusive

        def competitor(source, destination):
            destination.mkdir()
            _rename_exclusive(source, destination)

        with patch("cmw.core.preparation_publication._rename_exclusive", side_effect=competitor):
            with self.assertRaises(PublicationError):
                publish_preparation(self.files, {}, self.plan())
        self.assertEqual(list((self.root / "inputs").iterdir()), [])
        self.assertFalse(list(self.root.glob(".cmw-preparation-*")))

    def test_record_detects_changed_bytes_and_unexpected_sidecar(self):
        publish_preparation(self.files, {}, self.plan())
        (self.root / "inputs/POSCAR").write_text("changed")
        (self.root / "inputs/metadata.json").write_text("{}")
        codes = {f["code"] for f in inspect_publication(self.scratch / "case/preparation.json")["findings"]}
        self.assertIn("PUBLICATION_CONTENT_MISMATCH", codes)
        self.assertIn("PUBLICATION_NAMES_MISMATCH", codes)

    def test_real_subprocess_race_does_not_merge_or_replace(self):
        # Each candidate has different bytes and a different Scratch identity.
        code = '''
import sys,time
from pathlib import Path
from cmw.core.preparation_publication import publication_plan,publish_preparation,PublicationError
root=Path(sys.argv[1]); label=sys.argv[2]
try:
 plan=publication_plan(output=root/'inputs',scratch_root=root/'scratch',record_directory=label)
 (root/('ready-'+label)).touch()
 deadline=time.monotonic()+10
 while not (root/'release').exists():
  if time.monotonic()>deadline:raise RuntimeError('barrier timeout')
  time.sleep(.01)
 publish_preparation({n:label.encode() for n in ('INCAR','KPOINTS','POSCAR','POTCAR')},{},plan)
except PublicationError:
 sys.exit(3)
'''
        environment = os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.root), label],
                                     env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for label in ("first", "second")]
        try:
            import time
            deadline = time.monotonic() + 10
            while not all((self.root / ("ready-" + label)).exists() for label in ("first", "second")):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            (self.root / "release").touch()
            responses = [p.communicate(timeout=15) for p in processes]
            self.assertEqual(sorted(p.returncode for p in processes), [0, 3], responses)
            contents = {p.read_bytes() for p in (self.root / "inputs").iterdir()}
            self.assertEqual(len(contents), 1)
            self.assertIn(contents.pop(), {b"first", b"second"})
            self.assertFalse(list(self.root.glob(".cmw-preparation-*")))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_abrupt_interruption_leaves_identifiable_intent(self):
        code = '''
import os,sys
from pathlib import Path
from unittest.mock import patch
from cmw.core.preparation_publication import publication_plan,publish_preparation
root=Path(sys.argv[1])
plan=publication_plan(output=root/'inputs',scratch_root=root/'scratch',record_directory='crash')
with patch('cmw.core.preparation_publication._rename_exclusive',side_effect=lambda *a:os._exit(17)):
 publish_preparation({'POSCAR':b'synthetic'}, {}, plan)
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}, timeout=15)
        self.assertEqual(result.returncode, 17)
        record = json.loads((self.scratch / "crash/preparation.json").read_text())
        self.assertEqual(record["publication"]["state"], "intent")
        self.assertTrue(Path(record["publication"]["staging_path"]).is_dir())
        self.assertFalse((self.root / "inputs").exists())
        self.assertEqual(inspect_publication(self.scratch / "crash/preparation.json")["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
