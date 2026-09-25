"""Invented separate-wrapper/output records through the real core finalizer."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.core import artifact_finalization as core
from cmw.periodic.vasp.result_finalization import (
    _record_identity, finalize_result, verify_finalization,
)
from .result_case import make_case, write_json, digest
from .runner_case import add_runner


class RunnerFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.case = make_case(self.root / "case", relaxation=True, constrained=True)
        self.metadata = add_runner(self.case["root"], self.case["run"], self.case["spec"]["execution"])
        self.case["policy"]["artifact_roles"].append("energy:sigma_to_zero")
        from cmw.core.provenance import stable_hash
        self.case["spec"]["policy_id"] = stable_hash(self.case["policy"])
        write_json(self.case["policy_path"], self.case["policy"])
        write_json(self.case["spec_path"], self.case["spec"])

    def finalize(self, **options):
        c = self.case
        return finalize_result(c["run"], policy_path=c["policy_path"], spec_path=c["spec_path"],
                               scratch_root=c["scratch"], record_directory=options.pop("record_directory", "accepted"), **options)

    def test_actual_core_bundle_and_verification_retain_runner_source_closure(self):
        before = {p: p.read_bytes() for p in self.case["run"].iterdir()}
        with patch.object(core, "finalize_artifact_bundle", wraps=core.finalize_artifact_bundle) as finalizer:
            preview = self.finalize(dry_run=True)
        finalizer.assert_called_once()
        self.assertEqual(list(self.case["scratch"].iterdir()), [])
        self.assertEqual(preview["artifact_bundle"]["artifact_count"], 2)
        record = self.finalize()
        self.assertEqual(before, {p: p.read_bytes() for p in self.case["run"].iterdir()})
        checked = verify_finalization(record["publication"]["record_path"])
        self.assertTrue(checked["valid"], checked["findings"])
        self.assertEqual(len(checked["artifacts"]), 2)
        self.assertIn("runner_identity", checked["artifacts"][0].protocol)
        sources = record["required_sources"]
        self.assertEqual(len({s["resolved_path"] for s in sources}), len(sources))
        self.assertTrue({"runner_record", "runner_input:KPOINTS", "runner_input:POTCAR"}
                        <= {s["role"] for s in sources})
        self.assertEqual({p.name for p in Path(record["publication"]["record_path"]).parent.iterdir()},
                         {"finalization.json"})

    def test_changed_runtime_inputs_or_runner_record_prevent_reuse(self):
        for name in ("INCAR", "POSCAR", "KPOINTS", "POTCAR", "RUN_METADATA.txt"):
            record = self.finalize(record_directory="before-changing-" + name)
            self.assertTrue(verify_finalization(record["publication"]["record_path"])["valid"])
            path = self.case["run"] / name
            original = path.read_bytes()
            with self.subTest(name=name):
                path.write_bytes(original + b"changed\n")
                self.assertFalse(verify_finalization(record["publication"]["record_path"])["valid"])
            path.write_bytes(original)

    def test_stored_runner_facts_and_source_closure_cannot_be_relabelled(self):
        original = self.finalize()
        path = Path(original["publication"]["record_path"])
        for mutation in (
            lambda r: r["evidence"]["binding"]["execution"]["runner"].update(payload_cwd="/invented/other"),
            lambda r: r.update(required_sources=[s for s in r["required_sources"] if s["role"] != "runner_input:POTCAR"]),
            lambda r: r["evidence"]["binding"]["execution"].update(sources=[
                s for s in r["evidence"]["binding"]["execution"]["sources"] if s["role"] != "runner_input:POTCAR"]),
            lambda r: r["evidence"]["binding"]["execution"]["sources"][-1].update(role="unrelated"),
        ):
            record = deepcopy(original)
            mutation(record)
            record["record_id"] = _record_identity(record)
            write_json(path, record)
            self.assertFalse(verify_finalization(path)["valid"])

    def test_contextual_input_identity_changes_artifact_identity(self):
        original = self.finalize(dry_run=True)
        path = self.case["run"] / "KPOINTS"
        old_hash, size = digest(path), path.stat().st_size
        path.write_bytes(path.read_bytes().replace(b"synthetic", b"different"))
        self.metadata.write_text(self.metadata.read_text().replace(old_hash, digest(path)))
        self.assertEqual(path.stat().st_size, size)
        self.case["spec"]["execution"]["runner_record"]["sha256"] = digest(self.metadata)
        write_json(self.case["spec_path"], self.case["spec"])
        changed = self.finalize(dry_run=True)
        self.assertEqual(original["evidence"]["snapshot_id"], changed["evidence"]["snapshot_id"])
        self.assertNotEqual(original["artifact_bundle"]["artifacts"][0]["artifact_id"],
                            changed["artifact_bundle"]["artifacts"][0]["artifact_id"])


if __name__ == "__main__":
    unittest.main()
