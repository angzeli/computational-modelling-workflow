#!/usr/bin/env bash
# Foreground CMW Jobs payload; runtime helper owns the scientific child/signals.
set -euo pipefail
CMW_IFCT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
CMW_ROOT=$(cd "$CMW_IFCT_DIR/../.." && pwd -P)
source "$CMW_ROOT/scripts/runtime_python.sh"
source "$CMW_ROOT/scripts/multiwfn/multiwfn_runtime.sh"
if (($# != 2)); then
    printf 'Usage: %s SPEC.json NEW_ATTEMPT_DIRECTORY\n' "$0" >&2
    exit 64
fi
if [[ $1 == --prepared-plan ]]; then
    prepared=$("$PYTHON_BIN" -m cmw.molecular.multiwfn.jobs verify-prepared "$2")
    attempt=$(printf '%s' "$prepared" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["attempt_directory"])')
    wavefunction=$(printf '%s' "$prepared" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["source_wavefunction"])')
    native_log=$(printf '%s' "$prepared" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["native_log"])')
    multiwfn_runtime_load "$attempt/runtime.json"
else
    spec=$1
    attempt=$2
    # Exclusive attempt creation prevents replay from overwriting scientific logs.
    mkdir "$attempt"
    attempt=$(cd "$attempt" && pwd -P)
    multiwfn_runtime_prepare "$attempt" "$attempt/runtime.json"
    "$PYTHON_BIN" -m cmw.molecular.multiwfn.ifct_job prepare --spec "$spec" --attempt "$attempt" > "$attempt/prepare.json"
    native_log=$attempt/multiwfn.session.log
    wavefunction=$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["wavefunction"])' "$spec")
fi
cd "$attempt"
code=0
multiwfn_jobs_event "$attempt" launch_requested
multiwfn_runtime_launch "$wavefunction" < menu.in > "$native_log" 2>&1 || code=$?
printf '%s\n' "$code" > process.exitcode
multiwfn_jobs_event "$attempt" process_exited "$code"
multiwfn_jobs_event "$attempt" validation_started
if [[ $1 == --prepared-plan ]]; then
    "$PYTHON_BIN" -m cmw.molecular.multiwfn.jobs verify-finalize "$2" >/dev/null || {
        multiwfn_jobs_event "$attempt" validation_failed
        exit 70
    }
fi
if "$PYTHON_BIN" -m cmw.molecular.multiwfn.ifct_job finalize --attempt "$attempt" --exit-code "$code"; then
    multiwfn_jobs_event "$attempt" validation_passed
else
    multiwfn_jobs_event "$attempt" validation_failed
    exit 70
fi
