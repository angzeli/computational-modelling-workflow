"""Actual benign Jobs worker receipts; no VASP/MPI or production-state access."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from cmw.core.provenance import stable_hash
from cmw.jobs import runtime
from cmw.jobs.store import Store
from cmw.periodic.vasp.result_execution import inspect_execution, inspect_execution_snapshot
from cmw.periodic.vasp.result_finalization import inspect_with_policy
from cmw.periodic.vasp.results import inspect_result
from tests.jobs.isolated_runtime import install
from tests.jobs.test_jobs import wait_for
from .result_fixtures import native_texts, structure_text
from .test_result_policy import policy


def run_fixture(root, schema=4):
    root.mkdir()
    run = root / "run"
    run.mkdir()
    texts = native_texts()
    texts["CONTCAR"] = structure_text(texts.pop("endpoint"))
    texts["POSCAR"] = structure_text()
    payload = root / "writer-input.json"
    payload.write_text(json.dumps(texts))
    store = Store(root / "state")
    if schema == 4:
        store.enroll_queue("synthetic-review-node", "synthetic-review-host")
    code = ("import json; from pathlib import Path; data=json.loads(Path(" + repr(str(payload)) +
            ").read_text()); [Path(name).write_text(text) for name,text in data.items()]; "
            "print('Synthetic native-shaped output; no scientific engine')")
    release = root / "release-writer"
    if schema == 3:
        code += ("; import time; gate=Path(" + repr(str(release)) + "); deadline=time.monotonic()+10\n"
                 "while not gate.exists() and time.monotonic()<deadline: time.sleep(.02)\n")
    with install():
        command = [sys.executable, "-c", code]
        if schema == 3:
            command = [sys.executable, "-m", "cmw.jobs.suspension_protocol", "--execution-budget", "10", "--", *command]
        job = store.add(argv=command, cwd=run, name="benign static writer", engine="VASP",
                        **({"suspension_backend": "local-command-v1"} if schema == 3 else {}))
        try:
            runtime.start(store)
            if schema == 3:
                wait_for(lambda: store.snapshot()["jobs"][0].get("suspension", {}).get("capability"), timeout=10)
                release.touch()
            wait_for(lambda: store.snapshot()["jobs"][0]["status"] in {"Done", "Fail", "Unknown"}, timeout=20)
            assert store.snapshot()["jobs"][0]["status"] == "Done", store.snapshot()["jobs"][0]
        finally:
            release.touch()
            owner = store.snapshot()["controller"]["owner"]
            runtime.stop(store)
            wait_for(lambda: not store.snapshot()["controller"]["online"])
            for child in runtime._CHILDREN:
                if owner and child.pid == owner["pid"]:
                    child.wait(timeout=5)
            runtime.reap_detached()
    snapshot = store.snapshot()
    assert snapshot["schema"] == schema
    receipt = store.root / "attempts" / job["attempt_id"] / "payload-exit.json"
    return store, snapshot, receipt, run


class NativeEnrolledExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cmw-native-review-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.store, cls.snapshot, cls.receipt, cls.run_directory = run_fixture(cls.root / "schema4")
        cls.job = cls.snapshot["jobs"][0]
        enrollment = cls.snapshot["queue_identity"]
        cls.target = {key: enrollment[key] for key in ("node_id", "queue_id", "host_id")}
        cls.target.update(job_id=cls.job["id"], attempt_id=cls.job["attempt_id"])

    def inspect(self, snapshot=None, **options):
        return inspect_execution_snapshot(snapshot or self.snapshot, options.pop("receipt_path", self.receipt),
            job_id=self.job["id"], attempt_id=self.job["attempt_id"], run_directory=self.run_directory,
            expected_target=options.pop("expected_target", self.target), **options)

    def test_actual_enrolled_completion_and_in_memory_read_only_identity(self):
        before = self.store.path.read_bytes()
        names = set(self.store.root.rglob("*"))
        observed = self.inspect()
        self.assertEqual(observed["status"], "eligible", observed)
        self.assertEqual(observed["binding"]["queue_identity"], self.snapshot["queue_identity"])
        self.assertEqual({item["role"] for item in observed["sources"]}, {"jobs_receipt"})
        self.assertEqual(len(observed["snapshot_identity"]["sha256"]), 64)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(set(self.store.root.rglob("*")), names)

    def test_legacy_schema2_and_schema3_actual_receipts_remain_eligible(self):
        for schema in (2, 3):
            with self.subTest(schema=schema):
                store, snapshot, receipt, run = run_fixture(self.root / f"schema{schema}", schema)
                job = snapshot["jobs"][0]
                result = inspect_execution_snapshot(snapshot, receipt, job_id=job["id"],
                                                     attempt_id=job["attempt_id"], run_directory=run)
                self.assertEqual(result["status"], "eligible", result)
                if schema == 3:
                    self.assertEqual(result["job"]["suspension"], job["suspension"])

    def test_current_export_binds_supported_policy_without_finalization(self):
        export = self.root / "snapshot.json"
        export.write_text(json.dumps(self.snapshot))
        evidence = inspect_result(self.run_directory)
        selected = policy()
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps(selected))
        spec = {"schema_version": 1, "intent": "retrospective", "producing_calculation": "benign static writer",
                "snapshot_id": evidence["snapshot_id"], "segment_id": evidence["selected_segment"]["segment_id"],
                "policy_id": stable_hash(selected), "source_identities": {s["role"]: s["sha256"] for s in evidence["sources"]},
                "artifact_roles": selected["artifact_roles"],
                "execution": {"snapshot": str(export), "receipt": str(self.receipt), "job_id": self.job["id"],
                              "attempt_id": self.job["attempt_id"], "source_identities": {
                                  "jobs_snapshot": hashlib.sha256(export.read_bytes()).hexdigest(),
                                  "jobs_receipt": hashlib.sha256(self.receipt.read_bytes()).hexdigest()}}}
        spec_path = self.root / "spec.json"
        spec_path.write_text(json.dumps(spec))
        result = inspect_with_policy(self.run_directory, policy_path=policy_path, spec_path=spec_path)
        self.assertEqual(result["policy_assessment"]["status"], "PASS", result["policy_assessment"])
        self.assertEqual(result["binding"]["execution"]["snapshot_schema"], 4)

    def test_downgrade_cannot_strip_enrollment_or_controller_identity(self):
        for schema in (2, 3):
            for strip in (False, True):
                with self.subTest(schema=schema, strip=strip):
                    snapshot = deepcopy(self.snapshot)
                    snapshot["schema"] = schema
                    if strip:
                        snapshot.pop("queue_identity")
                        snapshot["controller"]["schema"] = schema
                    self.assertEqual(self.inspect(snapshot)["status"], "invalid")
        old = deepcopy(self.snapshot)
        old.pop("queue_identity")
        self.assertEqual(self.inspect(old)["status"], "unknown")

    def test_wrong_owner_attempt_and_ambiguous_selection_never_eligible(self):
        for key in ("node_id", "queue_id", "host_id", "attempt_id", "job_id"):
            target = self.target | {key: 2 if key == "job_id" else "wrong"}
            self.assertEqual(self.inspect(expected_target=target)["status"], "invalid", key)
        for key in ("node_id", "queue_id", "host_id"):
            snapshot = deepcopy(self.snapshot)
            snapshot["queue_identity"][key] = "wrong"
            self.assertEqual(self.inspect(snapshot)["status"], "invalid", key)
        snapshot = deepcopy(self.snapshot)
        snapshot["jobs"].append(deepcopy(self.job))
        self.assertEqual(self.inspect(snapshot)["status"], "invalid")
        snapshot["schema"] = 5
        self.assertEqual(self.inspect(snapshot)["status"], "invalid")

    def test_receipt_and_queue_copies_cannot_grant_original_location(self):
        copied = self.root / "copied-receipt.json"
        shutil.copyfile(self.receipt, copied)
        self.assertEqual(self.inspect(receipt_path=copied)["status"], "invalid")
        clone = self.root / "copied-state"
        shutil.copytree(self.store.root, clone)
        snapshot = deepcopy(self.snapshot)
        snapshot["state_directory"] = str(clone)
        snapshot["queue_identity"]["state_root"] = str(clone)
        attempt = clone / "attempts" / self.job["attempt_id"]
        snapshot["jobs"][0]["logs"] = {name: str(attempt / f"{name}.log") for name in ("stdout", "stderr")}
        self.assertEqual(self.inspect(snapshot, receipt_path=attempt / "payload-exit.json")["status"], "invalid")

    def test_conflicting_terminal_and_suspension_facts_do_not_pass(self):
        changes = ({"cancel_requested": True}, {"termination_requested": "deadline"}, {"signal": 15},
                   {"exit_code": 1}, {"finished_at": 0}, {"attempt_id": "b" * 32},
                   {"suspension": {"protocol": 1, "backend": "local-command-v1", "phase": "Suspended",
                                   "desired": "Run", "inhibited": False, "intervals": []}})
        for change in changes:
            snapshot = deepcopy(self.snapshot)
            snapshot["jobs"][0].update(change)
            self.assertEqual(self.inspect(snapshot)["status"], "invalid", change)

    def test_unknown_submission_runtime_is_not_invented(self):
        self.assertEqual(self.inspect(expected_target=self.target | {"runtime_id": "c" * 64})["status"], "unknown")
        snapshot = deepcopy(self.snapshot)
        snapshot["jobs"][0]["node_submission"] = {"schema": 1, "node_id": self.target["node_id"],
            "queue_id": self.target["queue_id"], "request_id": "a" * 32, "digest": "b" * 64, "runtime_id": "c" * 64}
        self.assertEqual(self.inspect(snapshot)["status"], "invalid")
