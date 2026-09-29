"""Benign protocol fixture, copied as run-vasp.sh by handoff_case.

This is not a VASP launcher: it only copies invented inputs and writes supplied
invented text. It never discovers or executes a binary, MPI, or a stop file.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    for option in ("input", "output", "binary", "ranks", "ncore", "kpar",
                   "mpi-mode", "restart", "timeout", "stop-before"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--managed-foreground", required=True, action="store_true")
    args = parser.parse_args()
    source, output = Path(args.input), Path(args.output)
    payload = json.loads(Path(__file__).with_name("payload.json").read_text())
    output.mkdir()
    lines = ["state: STAGING", "child_status: NOT STARTED",
             "staging_start_utc: " + now(), "source: " + str(source),
             "output: " + str(output), "managed_foreground: True",
             "restart_requested: none", "requested_version: 6.6.1",
             "original_incar_sha256: " + sha(source / "INCAR")]
    for name in ("INCAR", "POSCAR", "POTCAR", "KPOINTS"):
        path = source / name
        observed = path.stat()
        (output / name).write_bytes(path.read_bytes())
        stat = (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)
        lines.append(f"staged_file: {name}; bytes={observed.st_size}; source_stat={stat}; SHA256={sha(path)}")
    with (output / "INCAR").open("a") as handle:
        handle.write(f"NCORE = {args.ncore}\nKPAR = {args.kpar}\n")
    lines += ["effective_incar_sha256: " + sha(output / "INCAR"), "state: RUNNING"]
    started = now()
    for name, text in payload["outputs"].items():
        (output / name).write_text(text)
    code = payload["exit_code"]
    lines += [f"status: {code}", f"child_status: {code}",
              "reason: " + ("completed" if code == 0 else "child failure"),
              "start_utc: " + started, "end_utc: " + now(),
              "advance_stop: not reached", f"launcher_status: {code}", "state: FINISHED"]
    (output / "RUN_METADATA.txt").write_text("\n".join(lines) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
