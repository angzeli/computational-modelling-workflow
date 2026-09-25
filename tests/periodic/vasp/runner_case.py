"""Portable synthetic wrapper/runner-record case; no real potential payloads."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json


DIALECT = "vasp-run-metadata-text-v1"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def add_runner(root, run, execution):
    """Turn invented Jobs evidence into the observed wrapper/payload pattern."""
    inputs = root / "inputs"
    inputs.mkdir(exist_ok=True)
    for name in ("POTCAR", "KPOINTS"):
        (run / name).write_text("synthetic " + name + "\n")
    snapshot_path = root / execution["snapshot"]
    snapshot = json.loads(snapshot_path.read_text())
    job = snapshot["jobs"][0]
    job["cwd"] = str(inputs)
    job["argv"] = [str(root / "runner" / "run-vasp.sh"), "--input", str(inputs),
                   "--output", str(run), "--binary", "std", "--ranks", "8", "--ncore", "4",
                   "--kpar", "1", "--mpi-mode", "synthetic", "--restart", "none",
                   "--timeout", "60", "--stop-before", "0", "--managed-foreground"]
    snapshot_path.write_text(json.dumps(snapshot))
    execution["source_identities"]["jobs_snapshot"] = sha(snapshot_path)
    lines = ["state: STAGING", "child_status: NOT STARTED",
             "staging_start_utc: " + timestamp(100.1), "source: " + str(inputs),
             "output: " + str(run), "managed_foreground: True", "restart_requested: none",
             "requested_version: 6.6.1", "original_incar_sha256: " + sha(run / "INCAR")]
    for name in ("INCAR", "POSCAR", "POTCAR", "KPOINTS"):
        size = (run / name).stat().st_size
        lines.append(f"staged_file: {name}; bytes={size}; source_stat=(1, 2, {size}, 100); SHA256={sha(run / name)}")
    lines += ["effective_incar_sha256: " + sha(run / "INCAR"), "state: RUNNING", "status: 0",
              "child_status: 0", "reason: completed", "start_utc: " + timestamp(100.2),
              "end_utc: " + timestamp(101.8), "advance_stop: not reached", "launcher_status: 0",
              "state: FINISHED"]
    path = run / "RUN_METADATA.txt"
    path.write_text("\n".join(lines) + "\n")
    execution["runner_record"] = {"dialect": DIALECT, "path": str(path), "sha256": sha(path)}
    return path
