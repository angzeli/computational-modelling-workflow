#!/usr/bin/env python3
"""Synthetic executable for deterministic ORCA lifecycle tests only."""

from __future__ import annotations

import os
import sys
from pathlib import Path


if sys.argv[1:] == ["--version"]:
    print("synthetic ORCA 1.0")
    raise SystemExit(0)
if len(sys.argv) != 2:
    raise SystemExit(64)

input_path = Path(sys.argv[1])
first_line = input_path.read_text(encoding="utf-8").splitlines()[0]
stage_token = first_line.split()[-1].lower()
stage = {"opt": "OPT", "freq": "FREQ", "sp": "SP"}.get(stage_token)
if stage is None:
    raise SystemExit("unknown synthetic stage")

log_path = os.environ.get("FAKE_ORCA_LOG")
if log_path:
    with Path(log_path).open("a", encoding="utf-8") as handle:
        handle.write(stage + "\n")

print("STDIN_BYTES=" + str(len(sys.stdin.buffer.read())), file=sys.stderr)
print("Program Version 1.0.0")
print("Total Charge       .... 0")
print("Multiplicity       .... 1")
fail_stage = os.environ.get("FAKE_ORCA_FAIL_STAGE", "").upper()

if fail_stage == stage:
    if stage == "OPT":
        print("SCF CONVERGED AFTER 4 CYCLES")
        print("FINAL SINGLE POINT ENERGY -10.000000000")
    elif stage == "FREQ":
        print("SCF CONVERGED AFTER 4 CYCLES")
        print("FINAL SINGLE POINT ENERGY -10.100000000")
    else:
        print("SCF DID NOT CONVERGE")
        print("FINAL SINGLE POINT ENERGY -10.200000000")
    print("ORCA TERMINATED NORMALLY")
    raise SystemExit(0)

print("SCF CONVERGED AFTER 4 CYCLES")
print(
    "FINAL SINGLE POINT ENERGY "
    + {"OPT": "-10.000000000", "FREQ": "-10.100000000", "SP": "-10.200000000"}[stage]
)
if stage == "OPT":
    print("THE OPTIMIZATION HAS CONVERGED")
    source = input_path.parent / "input.xyz"
    lines = source.read_text(encoding="utf-8").splitlines()
    fields = lines[2].split()
    fields[1] = f"{float(fields[1]) + 0.25:.12f}"
    lines[2] = " ".join(fields)
    input_path.with_suffix(".xyz").write_text("\n".join(lines) + "\n", encoding="utf-8")
elif stage == "FREQ":
    print("VIBRATIONAL FREQUENCIES")
    print("  0:       0.00 cm**-1")
    print("  1:     100.00 cm**-1")
print("TOTAL RUN TIME: 0 days 0 hours 0 minutes 0.1 seconds")
print("ORCA TERMINATED NORMALLY")
