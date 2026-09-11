#!/usr/bin/env bash
# Foreground CMW Jobs payload; runtime helper owns the scientific child/signals.
set -euo pipefail
CMW_IFCT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
CMW_ROOT=$(cd "$CMW_IFCT_DIR/../.." && pwd -P)
PYTHON_BIN=${PYTHON_BIN:-python3}
export PYTHONPATH="$CMW_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
source "$CMW_ROOT/scripts/multiwfn/multiwfn_runtime.sh"
if (($# != 2)); then
    printf 'Usage: %s SPEC.json NEW_ATTEMPT_DIRECTORY\n' "$0" >&2
    exit 64
fi
spec=$1
attempt=$2
# Exclusive attempt creation prevents replay from overwriting scientific logs.
mkdir "$attempt"
attempt=$(cd "$attempt" && pwd -P)
multiwfn_runtime_prepare "$attempt" "$attempt/runtime.json"
"$PYTHON_BIN" -m cmw.molecular.multiwfn.ifct_job prepare --spec "$spec" --attempt "$attempt" > "$attempt/prepare.json"
wavefunction=$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["wavefunction"])' "$spec")
cd "$attempt"
code=0
multiwfn_runtime_launch "$wavefunction" < menu.in > multiwfn.session.log 2>&1 || code=$?
printf '%s\n' "$code" > process.exitcode
"$PYTHON_BIN" -m cmw.molecular.multiwfn.ifct_job finalize --attempt "$attempt" --exit-code "$code"
