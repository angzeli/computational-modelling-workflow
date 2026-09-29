"""Portable prepared fixture and binding builder; completion receipts stay native.

The caller runs the returned argv through its isolated Jobs runtime. This helper
never creates or changes a receipt or a snapshot's execution fields.
"""
from pathlib import Path
import shlex
import sys
import time

from cmw.core.provenance import stable_hash
from cmw.periodic.vasp.preparation import prepare
from cmw.periodic.vasp.results import inspect_result
from .result_case import digest, write_json
from .result_fixtures import native_texts, structure_text


def wait_for_fixture_sources(paths, *, quiet_seconds=.25, timeout=3):
    """Require a bounded quiet window before planning from generated fixtures.

    Desktop files can acquire metadata shortly after creation. This test-only
    wait precedes binding; it never retries a refused finalization or relaxes
    the production byte/inode/ctime verification contract.
    """
    paths = tuple(paths)

    def snapshot():
        values = []
        for path in paths:
            observed = path.stat()
            values.append((observed.st_dev, observed.st_ino, observed.st_size,
                           observed.st_mtime_ns, observed.st_ctime_ns))
        return values

    before = snapshot()
    unchanged_since = time.monotonic()
    deadline = unchanged_since + timeout
    while time.monotonic() < deadline:
        time.sleep(.05)
        current = snapshot()
        now = time.monotonic()
        if current != before:
            before, unchanged_since = current, now
        elif now - unchanged_since >= quiet_seconds:
            return
    raise AssertionError("Generated fixture sources did not settle before binding")


def prepare_case(root, *, relaxation=False, exit_code=0, converged=True):
    root = Path(root).resolve()
    root.mkdir()
    scratch = root / "scratch"
    scratch.mkdir()
    flags = [[True] * 3, [False] * 3] if relaxation else None
    (root / "source.POSCAR").write_text(structure_text(flags=flags))
    for element in ("H", "He"):
        directory = root / "library" / element
        directory.mkdir(parents=True)
        (directory / "POTCAR").write_text(
            f"TITEL = INVENTED {element} 01Jan2001\nVRHFIN = {element}: invented metadata only\nEnd of Dataset\n")
    incar = {"GGA": "PE", "ENCUT": 300, "PREC": "Normal", "ISPIN": 1,
             "ISMEAR": 0, "SIGMA": .05, "EDIFF": 1e-6, "NELM": 3,
             "ALGO": "Normal", "ISTART": 0, "ICHARG": 2}
    if relaxation:
        incar.update(IBRION=2, NSW=10, POTIM=.5, EDIFFG=-.02)
    preparation_spec = {
        "schema_version": 1, "calculation": "fixed-cell-relaxation" if relaxation else "static",
        "structure": "source.POSCAR", "profile": {
            "name": "invented composed handoff", "incar": incar,
            "kpoints": {"mode": "Gamma", "mesh": [1, 1, 1], "shift": [0, 0, 0]},
            "potentials": {"root": "library", "variants": {"H": "H", "He": "He"},
                           "requirements": {"family": "INVENTED"}}},
        "runtime": {"mpi_ranks": 2, "threads_per_rank": 1, "cpus": 2,
                    "incar": {"NCORE": 2, "KPAR": 1}},
    }
    spec_path = root / "prepare.json"
    write_json(spec_path, preparation_spec)
    inputs, output = root / "inputs", root / "output"
    prepared = prepare(spec_path, output=inputs, scratch_root=scratch, record_directory="preparation")
    fixture = Path(__file__).with_name("fake_handoff_runner.py")
    runner = root / "run-vasp.sh"
    payload_script = root / "fake_payload.py"
    payload_script.write_text(fixture.read_text())
    runner.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(payload_script))} "$@"\n')
    runner.chmod(0o700)
    texts = native_texts(relaxation=relaxation, evaluations=2 if relaxation else 1,
                         native=converged, settings={**incar, "NCORE": 2, "KPAR": 1})
    endpoint = texts.pop("endpoint")
    texts.pop("INCAR")
    if relaxation:
        texts["CONTCAR"] = structure_text(endpoint, flags=flags)
    write_json(root / "payload.json", {"outputs": texts, "exit_code": exit_code})
    selected_policy = {
        "schema_version": 1, "name": "invented composed handoff policy",
        "calculation": preparation_spec["calculation"],
        "electronic": {"history": "all", "basis": "native-and-numeric", "criterion": "run-ediff"},
        "force": {"criterion": "run-ediffg", "scope": "free"} if relaxation else None,
        "endpoint": {"position_tolerance_angstrom": 2e-5, "cell_tolerance_angstrom": 2e-6},
        "require_execution": True, "require_input_binding": True,
        "artifact_roles": (["periodic-structure"] if relaxation else []) + ["energy:sigma_to_zero"],
    }
    policy_path = root / "policy.json"
    write_json(policy_path, selected_policy)
    argv = [str(runner), "--input", str(inputs), "--output", str(output), "--binary", "std",
            "--ranks", "2", "--ncore", "2", "--kpar", "1", "--mpi-mode", "native",
            "--restart", "none", "--timeout", "60", "--stop-before", "0", "--managed-foreground"]
    return {"root": root, "scratch": scratch, "inputs": inputs, "run": output, "argv": argv,
            "prepared": prepared, "policy": selected_policy, "policy_path": policy_path}


def bind_completed_case(case, snapshot):
    """Save an unmodified native snapshot and reference its original receipt."""
    job, = snapshot["jobs"]
    snapshot_path = case["root"] / "jobs-snapshot.json"
    write_json(snapshot_path, snapshot)
    receipt = Path(snapshot["state_directory"]) / "attempts" / job["attempt_id"] / "payload-exit.json"
    record = case["scratch"] / "preparation" / "preparation.json"
    metadata = case["run"] / "RUN_METADATA.txt"
    wait_for_fixture_sources([*case["run"].iterdir(), case["policy_path"], snapshot_path, receipt, record])
    evidence = inspect_result(case["run"])
    spec = {
        "schema_version": 1, "intent": "retrospective", "producing_calculation": "invented-composed-handoff",
        "snapshot_id": evidence["snapshot_id"], "segment_id": evidence["selected_segment"]["segment_id"],
        "policy_id": stable_hash(case["policy"]),
        "source_identities": {s["role"]: s["sha256"] for s in evidence["sources"]},
        "execution": {"snapshot": str(snapshot_path), "receipt": str(receipt),
                      "job_id": job["id"], "attempt_id": job["attempt_id"],
                      "source_identities": {"jobs_snapshot": digest(snapshot_path), "jobs_receipt": digest(receipt)},
                      "runner_record": {"dialect": "vasp-run-metadata-text-v1",
                                        "path": str(metadata), "sha256": digest(metadata)}},
        "artifact_roles": case["policy"]["artifact_roles"], "expected_runtime_overlay": {"NCORE": 2, "KPAR": 1},
        "preparation_record": {"path": str(record), "sha256": digest(record)},
    }
    path = case["root"] / "finalize.json"
    write_json(path, spec)
    wait_for_fixture_sources([path])
    case.update(spec=spec, spec_path=path, receipt=receipt, snapshot=snapshot_path)
    return case
