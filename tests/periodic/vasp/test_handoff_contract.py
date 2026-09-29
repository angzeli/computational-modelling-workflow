"""Prepare → real fixture-owned Jobs receipt → runner binding → core artifacts.

Only a benign protocol fixture runs. This does not qualify any external port,
real VASP, MPI topology, potential, or scientific method.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cmw.core import artifact_finalization as core
from cmw.periodic.vasp.inputs import parse_incar
from cmw.periodic.vasp.result_finalization import (
    ResultFinalizationError, finalize_result, inspect_with_policy, verify_finalization,
)
from tests.jobs import test_jobs as lifecycle
from .handoff_case import bind_completed_case, prepare_case


class HandoffContractTests(unittest.TestCase):
    setUp = lifecycle.QueueTests.setUp
    tearDown = lifecycle.QueueTests.tearDown
    jobs = lifecycle.QueueTests.jobs
    run_queue = lifecycle.QueueTests.run_queue
    completed = lifecycle.QueueTests.completed

    def execute(self, **options):
        case = prepare_case(self.root / "case", **options)
        before = {p.name: p.read_bytes() for p in case["inputs"].iterdir()}
        self.assertEqual(set(before), {"INCAR", "POSCAR", "KPOINTS", "POTCAR"})
        self.assertEqual({p.name for p in (case["scratch"] / "preparation").iterdir()}, {"preparation.json"})
        self.assertEqual(case["prepared"]["effective_inputs"]["status"], "unobserved")
        self.store.add(argv=case["argv"], cwd=case["inputs"], name="benign handoff", env={"OMP_NUM_THREADS": "1"})
        self.run_queue()
        self.completed(1)
        bind_completed_case(case, self.store.snapshot())
        self.assertEqual({p.name: p.read_bytes() for p in case["inputs"].iterdir()}, before)
        for name in ("POSCAR", "KPOINTS", "POTCAR"):
            self.assertEqual((case["run"] / name).read_bytes(), before[name])
        prepared = parse_incar(before["INCAR"].decode())["settings"]
        effective = parse_incar((case["run"] / "INCAR").read_text())["settings"]
        self.assertEqual(effective, {**prepared, "NCORE": 2, "KPAR": 1})
        return case

    def finalize(self, case):
        return finalize_result(case["run"], policy_path=case["policy_path"], spec_path=case["spec_path"],
                               scratch_root=case["scratch"], record_directory="accepted")

    def assert_accepted(self, case, count):
        self.assertEqual(self.jobs()[0]["status"], "Done")
        self.assertEqual(json.loads(case["receipt"].read_text())["exit_code"], 0)
        before = {p: p.read_bytes() for p in case["run"].iterdir()}
        with patch.object(core, "finalize_artifact_bundle", wraps=core.finalize_artifact_bundle) as finalizer:
            result = self.finalize(case)
        finalizer.assert_called_once()
        path = Path(result["publication"]["record_path"])
        self.assertEqual({p.name for p in path.parent.iterdir()}, {"finalization.json"})
        checked = verify_finalization(path, policy_path=case["policy_path"])
        self.assertTrue(checked["valid"], checked["findings"])
        self.assertEqual(len(checked["artifacts"]), count)
        self.assertEqual({p: p.read_bytes() for p in case["run"].iterdir()}, before)
        roles = {s["role"] for s in result["required_sources"]}
        self.assertTrue({"preparation_record", "jobs_receipt", "runner_record", "runner_input:POTCAR"} <= roles)

    def test_static_preparation_native_receipt_and_finalized_energy(self):
        self.assert_accepted(self.execute(), 1)

    def test_fixed_cell_preparation_native_receipt_and_finalized_endpoint(self):
        self.assert_accepted(self.execute(relaxation=True), 2)

    def test_nonzero_owned_execution_cannot_finalize_converged_text(self):
        case = self.execute(exit_code=7)
        self.assertEqual(self.jobs()[0]["status"], "Fail")
        self.assertEqual(json.loads(case["receipt"].read_text())["exit_code"], 7)
        evidence = inspect_with_policy(case["run"], policy_path=case["policy_path"], spec_path=case["spec_path"])
        self.assertNotEqual(evidence["binding"]["execution"]["status"], "eligible")
        with self.assertRaises(ResultFinalizationError):
            self.finalize(case)
        self.assertFalse((case["scratch"] / "accepted").exists())

    def test_exit_zero_does_not_override_native_scientific_failure(self):
        case = self.execute(converged=False)
        self.assertEqual(self.jobs()[0]["status"], "Done")
        evidence = inspect_with_policy(case["run"], policy_path=case["policy_path"], spec_path=case["spec_path"])
        self.assertEqual(evidence["binding"]["execution"]["status"], "eligible")
        self.assertEqual(evidence["policy_assessment"]["status"], "FAIL")
        with self.assertRaises(ResultFinalizationError):
            self.finalize(case)
        self.assertFalse((case["scratch"] / "accepted").exists())
