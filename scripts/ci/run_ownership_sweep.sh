#!/usr/bin/env bash
# One dedicated, unsharded matrix per qualified platform. No row selection.
set -euo pipefail
source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
work_root=${1:?usage: run_ownership_sweep.sh NEW_EXTERNAL_DIRECTORY}
python_exe=${PYTHON_BIN:-python3}
mkdir -m 700 "$work_root"
work_root=$(cd "$work_root" && pwd -P)
case "$work_root/" in "$source_root/"*) printf 'Test state requires a directory outside the checkout\n' >&2; exit 64;; esac
mkdir -m 700 "$work_root/tmp"
unset PYTHONPATH
export TMPDIR="$work_root/tmp" CMW_JOBS_STATE="$work_root/unused-state"
export CMW_TEST_EVIDENCE_DIR="$work_root/evidence" CMW_FAULT_SWEEP=full
cd "$source_root"
"$python_exe" scripts/ci/run_module.py "$work_root/log" jobs.test_ownership_fault_sweep 2>&1 | tee "$work_root/output"
"$python_exe" scripts/ci/check_ownership_sweep.py "$work_root/evidence/ownership-fault-sweep.json" | tee "$work_root/coverage-union.txt"
