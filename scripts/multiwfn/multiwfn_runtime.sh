#!/usr/bin/env bash

# Shell-owned Multiwfn launch adapter. Python resolves semantics and provenance;
# this helper owns environment, short-path alias, stdin, and child execution.

MULTIWFN_RUNTIME_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
MULTIWFN_RUNTIME_ROOT=$(cd "$MULTIWFN_RUNTIME_DIR/../.." && pwd -P)
MULTIWFN_RUNTIME_PYTHON=${PYTHON_BIN:-python3}
MULTIWFN_NTHREADS=${MULTIWFN_NTHREADS-8}
MULTIWFN_EXE=${MULTIWFN_EXE:-}
MULTIWFN_VERSION=""
MULTIWFN_RUN_SETTINGS_PATH=""
MULTIWFN_RUN_SETTINGS_DIRECTORY=""

multiwfn_runtime_python() {
  PYTHONPATH="$MULTIWFN_RUNTIME_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$MULTIWFN_RUNTIME_PYTHON" -m cmw.molecular.multiwfn.cli "$@"
}

multiwfn_runtime_json_value() {
  local key=$1
  "$MULTIWFN_RUNTIME_PYTHON" -c \
    'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$key"
}

multiwfn_runtime_prepare() {
  local attempt_directory=$1 metadata=$2 settings_source=${3:-} prepared
  local command=(prepare --attempt-directory "$attempt_directory" --metadata "$metadata")
  [[ -z "$MULTIWFN_EXE" ]] || command+=(--multiwfn-exe "$MULTIWFN_EXE")
  [[ -z "$settings_source" ]] || command+=(--settings-source "$settings_source")
  [[ -z "${MULTIWFN_NTHREADS+x}" ]] || command+=(--threads "$MULTIWFN_NTHREADS")
  prepared=$(multiwfn_runtime_python "${command[@]}") || return
  MULTIWFN_EXE=$(printf '%s' "$prepared" | multiwfn_runtime_json_value executable)
  MULTIWFN_VERSION=$(printf '%s' "$prepared" | multiwfn_runtime_json_value version)
  MULTIWFN_NTHREADS=$(printf '%s' "$prepared" | multiwfn_runtime_json_value requested_nthreads)
  MULTIWFN_RUN_SETTINGS_PATH=$(printf '%s' "$prepared" | multiwfn_runtime_json_value settings_path)
  MULTIWFN_RUN_SETTINGS_DIRECTORY=${MULTIWFN_RUN_SETTINGS_PATH%/settings.ini}
  export MULTIWFN_EXE MULTIWFN_NTHREADS
}

multiwfn_runtime_launch() {
  local alias_root alias_path
  if [[ -z "$MULTIWFN_RUN_SETTINGS_DIRECTORY" || ! -s "$MULTIWFN_RUN_SETTINGS_PATH" ]]; then
    printf 'Multiwfn run-local settings were not prepared\n' >&2
    return 64
  fi
  if [[ -e "$PWD/settings.ini" || -L "$PWD/settings.ini" ]]; then
    printf 'Working-directory settings.ini would override Multiwfnpath\n' >&2
    return 64
  fi
  (
    alias_root=$(mktemp -d "${TMPDIR:-/tmp}/cmw-multiwfn.XXXXXXXX") || exit 70
    alias_path=$alias_root/runtime
    cleanup_multiwfn_alias() {
      rm -f -- "$alias_path"
      rmdir "$alias_root" 2>/dev/null || true
    }
    trap cleanup_multiwfn_alias EXIT
    ln -s "$MULTIWFN_RUN_SETTINGS_DIRECTORY" "$alias_path"
    Multiwfnpath="$alias_path" OMP_NUM_THREADS="$MULTIWFN_NTHREADS" \
      "$MULTIWFN_EXE" "$@"
  )
}
