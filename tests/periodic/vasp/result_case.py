"""Synthetic saved-receipt cases shared by offline and installed acceptance."""
import hashlib
import json
from pathlib import Path

from cmw.core.provenance import stable_hash
from cmw.periodic.vasp.results import inspect_result
from .result_fixtures import write_run


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def make_case(root, *, relaxation=False, constrained=False, attempt="a" * 32):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run = write_run(root / "run", relaxation=relaxation, evaluations=2 if relaxation else 1,
                    flags=[[True] * 3, [False] * 3] if constrained else None)
    scratch = root / "scratch"
    scratch.mkdir()
    inputs = root / "project-inputs"
    inputs.mkdir()
    for name in ("INCAR", "POSCAR"):
        (inputs / name).write_bytes((run / name).read_bytes())
    (inputs / "KPOINTS").write_text("Invented fixture\n0\nGamma\n1 1 1\n0 0 0\n")
    (inputs / "POTCAR").write_text("INVENTED NON-POTENTIAL PLACEHOLDER\n")
    state = root / "synthetic-state"
    attempt_dir = state / "attempts" / attempt
    attempt_dir.mkdir(parents=True)
    receipt = attempt_dir / "payload-exit.json"
    snapshot = root / "saved-jobs-snapshot.json"
    write_json(receipt, {"exit_code": 0, "recorded_at": 102.0})
    write_json(snapshot, {"schema": 2, "state_directory": str(state), "observed_at": 104.0,
                         "jobs": [{"id": 7, "display_id": "J7.1", "attempt_id": attempt,
                                   "engine": "Command", "argv": ["/bin/sh", "never-executed-synthetic-runner"],
                                   "cwd": str(run), "status": "Done", "started_at": 100.0, "finished_at": 103.0,
                                   "exit_code": 0, "signal": None, "cancel_requested": False,
                                   "layout": None, "logs": {s: str(attempt_dir / (s + ".log")) for s in ("stdout", "stderr")}}]})
    roles = ["periodic-structure"] if relaxation else ["energy:sigma_to_zero"]
    policy = {"schema_version": 1, "name": "invented-relaxation" if relaxation else "invented-static",
              "calculation": "fixed-cell-relaxation" if relaxation else "static",
              "electronic": {"history": "all", "basis": "native-and-numeric", "criterion": "run-ediff"},
              "force": {"criterion": "run-ediffg", "scope": "free"} if relaxation else None,
              "endpoint": {"position_tolerance_angstrom": 2e-5, "cell_tolerance_angstrom": 2e-6},
              "require_execution": True, "require_input_binding": True, "artifact_roles": roles}
    policy_path = root / "policy.json"
    write_json(policy_path, policy)
    evidence = inspect_result(run)
    spec = {"schema_version": 1, "intent": "retrospective", "producing_calculation": "invented-calculation",
            "snapshot_id": evidence["snapshot_id"], "segment_id": evidence["selected_segment"]["segment_id"],
            "policy_id": stable_hash(policy), "source_identities": {s["role"]: s["sha256"] for s in evidence["sources"]},
            "execution": {"snapshot": str(snapshot), "receipt": str(receipt), "job_id": 7, "attempt_id": attempt,
                          "source_identities": {"jobs_snapshot": digest(snapshot), "jobs_receipt": digest(receipt)}},
            "artifact_roles": roles, "parents": []}
    spec_path = root / "spec.json"
    write_json(spec_path, spec)
    return {"root": root, "run": run, "scratch": scratch, "inputs": inputs, "state": state,
            "receipt": receipt, "snapshot": snapshot, "policy": policy, "policy_path": policy_path,
            "spec": spec, "spec_path": spec_path}
