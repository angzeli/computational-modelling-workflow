#!/usr/bin/env bash
set -euo pipefail

# Operational ORCA runner. Scientific decisions are delegated to the structured
# Python contract; this script owns process, descriptor, signal, lock, and
# temporary-directory lifecycle.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd -P)
PYTHON_BIN=${PYTHON_BIN:-python3}
ORCA_EXE=${ORCA_EXE:-orca}
NPROCS=${NPROCS:-1}
MAXCORE_MB=${MAXCORE_MB:-1000}
CMW_REQUIRE_MINIMUM=${CMW_REQUIRE_MINIMUM:-0}
CMW_IMAGINARY_TOLERANCE_CM1=${CMW_IMAGINARY_TOLERANCE_CM1:-0.0}
external_scratch_root=${CMW_EXTERNAL_SCRATCH_ROOT:-}
external_scratch_mount=${CMW_EXTERNAL_SCRATCH_MOUNT:-}
external_scratch_minimum_free_gib=${CMW_EXTERNAL_SCRATCH_MINIMUM_FREE_GIB:-}

input=""
target=""
metadata=""
output=""
stderr_path=""
layout=""
geometry_contract=""
runtime_contract=""
runtime_launch_manifest=""
require_runtime_contract=0
minimum_free_disk_gb=""
disk_check_path=""
replace_stale=0
artifacts=()
execution_inputs=()

usage() {
  printf '%s\n' \
    "Usage: run_orca.sh --input FILE --target FILE --metadata FILE" \
    "                   --output FILE --stderr FILE [--artifact ROLE=FILE]" \
    "                   [--layout FILE]" \
    "                   [--geometry-contract FILE]" \
    "                   [--runtime-contract FILE]" \
    "                   [--require-runtime-contract]" \
    "                   [--minimum-free-disk-gb NUMBER --disk-check-path DIR]" \
    "                   [--external-scratch-root DIR --external-scratch-mount DIR]" \
    "                   [--external-scratch-minimum-free-gib NUMBER]" \
    "                   [--execution-input ROLE=FILE]" \
    "                   [--replace-stale-lock]" >&2
}

while (($#)); do
  case "$1" in
    --input) input=${2:?}; shift 2 ;;
    --target) target=${2:?}; shift 2 ;;
    --metadata) metadata=${2:?}; shift 2 ;;
    --output) output=${2:?}; shift 2 ;;
    --stderr) stderr_path=${2:?}; shift 2 ;;
    --layout) layout=${2:?}; shift 2 ;;
    --geometry-contract) geometry_contract=${2:?}; shift 2 ;;
    --runtime-contract) runtime_contract=${2:?}; shift 2 ;;
    --require-runtime-contract) require_runtime_contract=1; shift ;;
    --minimum-free-disk-gb) minimum_free_disk_gb=${2:?}; shift 2 ;;
    --disk-check-path) disk_check_path=${2:?}; shift 2 ;;
    --external-scratch-root) external_scratch_root=${2:?}; shift 2 ;;
    --external-scratch-mount) external_scratch_mount=${2:?}; shift 2 ;;
    --external-scratch-minimum-free-gib) external_scratch_minimum_free_gib=${2:?}; shift 2 ;;
    --execution-input) execution_inputs+=("${2:?}"); shift 2 ;;
    --artifact) artifacts+=("${2:?}"); shift 2 ;;
    --replace-stale-lock) replace_stale=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage; exit 64 ;;
  esac
done

if [[ -z "$input" || -z "$target" || -z "$metadata" || -z "$output" || -z "$stderr_path" ]]; then
  usage
  exit 64
fi
case "$CMW_REQUIRE_MINIMUM" in
  1|true|TRUE|True|yes|YES|Yes) require_minimum=1 ;;
  0|false|FALSE|False|no|NO|No) require_minimum=0 ;;
  *)
    printf 'CMW_REQUIRE_MINIMUM must be true or false\n' >&2
    exit 64
    ;;
esac

absolute_path() {
  local path=$1 directory name
  directory=$(dirname "$path")
  name=$(basename "$path")
  mkdir -p "$directory"
  directory=$(cd "$directory" && pwd -P)
  printf '%s/%s\n' "$directory" "$name"
}

input=$(absolute_path "$input")
input_directory=$(dirname "$input")
target=$(absolute_path "$target")
metadata=$(absolute_path "$metadata")
output=$(absolute_path "$output")
stderr_path=$(absolute_path "$stderr_path")
if [[ -n "$layout" ]]; then
  layout=$(absolute_path "$layout")
fi
if [[ -n "$geometry_contract" ]]; then
  geometry_contract=$(absolute_path "$geometry_contract")
fi
if [[ -n "$runtime_contract" ]]; then
  runtime_contract=$(absolute_path "$runtime_contract")
fi
resolved_artifacts=()
if ((${#artifacts[@]} > 0)); then
  for item in "${artifacts[@]}"; do
    role=${item%%=*}
    path=${item#*=}
    if [[ "$role" == "$item" || -z "$role" || -z "$path" ]]; then
      printf 'Invalid artifact declaration: %s\n' "$item" >&2
      exit 64
    fi
    resolved_artifacts+=("$role=$(absolute_path "$path")")
  done
fi
execution_outputs=()
if ((${#resolved_artifacts[@]} > 0)); then
  execution_outputs=("${resolved_artifacts[@]}")
fi

external_scratch_record="$input_directory/external-scratch.json"
external_configured=0
if [[ -n "$external_scratch_root" || -n "$external_scratch_mount" || -n "$external_scratch_minimum_free_gib" ]]; then
  if [[ -z "$external_scratch_root" || -z "$external_scratch_mount" || -z "$external_scratch_minimum_free_gib" ]]; then
    printf 'External scratch requires root, mounted volume, and minimum free GiB\n' >&2
    exit 64
  fi
  if [[ -z "$layout" || -z "$geometry_contract" ]]; then
    printf 'External scratch requires execution-layout and geometry contracts\n' >&2
    exit 64
  fi
  external_configured=1
fi

export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if [[ -n "$minimum_free_disk_gb" || -n "$disk_check_path" ]]; then
  if [[ -z "$minimum_free_disk_gb" || -z "$disk_check_path" ]]; then
    printf 'Disk-capacity protection requires both a threshold and check path\n' >&2
    exit 64
  fi
  if ! storage_json=$(
    "$PYTHON_BIN" -m cmw.core.storage_cli \
      --path "$disk_check_path" --minimum-free-gb "$minimum_free_disk_gb"
  ); then
    printf 'Storage-capacity preflight failed: %s\n' "$storage_json" >&2
    exit 74
  fi
fi

reuse_if_available() {
  if "$PYTHON_BIN" -m cmw.molecular.orca.cli reuse \
    --target "$target" --metadata "$metadata" >/dev/null 2>&1
  then
    if [[ -f "$external_scratch_record" ]]; then
      if ! "$PYTHON_BIN" -m cmw.core.external_scratch_cli cleanup \
        --record "$external_scratch_record" \
        --finalization-metadata "$metadata" >/dev/null
      then
        printf 'Reusable result has external scratch requiring safe cleanup: %s\n' \
          "$external_scratch_record" >&2
        exit 75
      fi
    fi
    printf '[REUSED] %s\n' "$metadata"
    exit 0
  fi
}

reuse_if_available

if [[ ! -f "$input" || ! -f "$target" ]]; then
  printf 'ORCA input or target metadata is missing\n' >&2
  exit 64
fi
if ! command -v "$ORCA_EXE" >/dev/null 2>&1 && [[ ! -x "$ORCA_EXE" ]]; then
  printf 'ORCA executable was not found: %s\n' "$ORCA_EXE" >&2
  exit 69
fi
if [[ -x "$ORCA_EXE" ]]; then
  orca_directory=$(cd "$(dirname "$ORCA_EXE")" && pwd -P)
  orca_command="$orca_directory/$(basename "$ORCA_EXE")"
else
  orca_command=$(command -v "$ORCA_EXE")
fi
if ((require_runtime_contract == 1)) && [[ -z "$runtime_contract" ]]; then
  printf 'Parallel production execution requires an ORCA runtime contract\n' >&2
  exit 69
fi
if [[ -n "$runtime_contract" ]]; then
  if ! runtime_json=$(
    "$PYTHON_BIN" -m cmw.molecular.orca.cli runtime-validate \
      --runtime "$runtime_contract" --orca-exe "$orca_command"
  ); then
    printf 'ORCA runtime contract validation failed: %s\n' "$runtime_json" >&2
    exit 69
  fi
  runtime_path_prefix=$(printf '%s' "$runtime_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(":".join(json.load(sys.stdin)["environment"]["path_prepend"]))')
  runtime_library_variable=$(printf '%s' "$runtime_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["environment"]["library_path_variable"])')
  runtime_library_prefix=$(printf '%s' "$runtime_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(":".join(json.load(sys.stdin)["environment"]["library_path_prepend"]))')
  while IFS= read -r runtime_assignment; do
    [[ -z "$runtime_assignment" ]] || export "$runtime_assignment"
  done < <(printf '%s' "$runtime_json" | "$PYTHON_BIN" -c \
    'import json,sys; value=json.load(sys.stdin)["environment"].get("variables", {}); [print(f"{name}={value[name]}") for name in sorted(value)]')
  export PATH="$runtime_path_prefix${PATH:+:$PATH}"
  case "$runtime_library_variable" in
    DYLD_LIBRARY_PATH)
      export DYLD_LIBRARY_PATH="$runtime_library_prefix${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
      ;;
    LD_LIBRARY_PATH)
      export LD_LIBRARY_PATH="$runtime_library_prefix${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
      ;;
    *)
      printf 'Unsupported runtime library-path variable: %s\n' "$runtime_library_variable" >&2
      exit 69
      ;;
  esac
  runtime_launch_manifest="$input_directory/orca-runtime-launch.json"
  resolved_artifacts+=("runtime_contract=$runtime_contract")
fi

lock_path="${target}.lock"
lock_json=""
lock_token=""
child_pid=""
local_scratch_dir=""
external_execution_directory=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  if [[ -n "$lock_token" ]]; then
    "$PYTHON_BIN" -m cmw.molecular.orca.cli lock release \
      --lock "$lock_path" --token "$lock_token" >/dev/null 2>&1 || true
  fi
  if [[ -n "$local_scratch_dir" && -d "$local_scratch_dir" && $(basename "$local_scratch_dir") == cmw-orca.* ]]; then
    rm -r -- "$local_scratch_dir"
  fi
  if ((status != 0)) && [[ -f "$external_scratch_record" ]]; then
    "$PYTHON_BIN" -m cmw.core.external_scratch_cli fail \
      --record "$external_scratch_record" \
      --reason "ORCA runner exited with status $status" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

lock_command=(
  "$PYTHON_BIN" -m cmw.molecular.orca.cli lock acquire
  --lock "$lock_path" --job-id "$target" --owner-pid "$$"
)
((replace_stale == 0)) || lock_command+=(--replace-stale)
if ! lock_json=$("${lock_command[@]}"); then
  printf 'Unable to acquire ORCA attempt lock: %s\n' "$lock_json" >&2
  exit 73
fi
lock_token=$(printf '%s' "$lock_json" | "$PYTHON_BIN" -c \
  'import json,sys; print(json.load(sys.stdin)["owner"]["token"])')

# Close the race between the first reuse check and lock acquisition.
reuse_if_available

if [[ -e "$metadata" || -e "$output" || -e "$stderr_path" ]]; then
  printf 'Non-reusable attempt artifacts already exist; choose a new attempt directory\n' >&2
  exit 73
fi

run_directory="$input_directory"
run_input="$input"
run_output="$output"
run_stderr="$stderr_path"
if ((external_configured == 1)); then
  scratch_prepare=(
    "$PYTHON_BIN" -m cmw.core.external_scratch_cli prepare
    --scratch-root "$external_scratch_root"
    --mount "$external_scratch_mount"
    --minimum-free-gib "$external_scratch_minimum_free_gib"
    --layout "$layout"
    --target "$target"
    --input "$input"
    --geometry-contract "$geometry_contract"
    --output "$output"
    --stderr "$stderr_path"
    --process-environment OMPI_MCA_osc=pt2pt
  )
  if ((${#execution_inputs[@]} > 0)); then
    for item in "${execution_inputs[@]}"; do
      scratch_prepare+=(--execution-input "$item")
    done
  fi
  if ((${#execution_outputs[@]} > 0)); then
    for item in "${execution_outputs[@]}"; do
      scratch_prepare+=(--artifact "$item")
    done
  fi
  if ! scratch_prepare_json=$("${scratch_prepare[@]}"); then
    printf 'External scratch preflight or preparation failed: %s\n' \
      "$scratch_prepare_json" >&2
    exit 74
  fi
  run_directory=$("$PYTHON_BIN" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["execution_directory"])' \
    "$external_scratch_record")
  run_input=$("$PYTHON_BIN" -c \
    'import json,sys; d=json.load(open(sys.argv[1])); print(next(x["execution_path"] for x in d["inputs"] if x["role"] == "primary"))' \
    "$external_scratch_record")
  run_output=$("$PYTHON_BIN" -c \
    'import json,sys; d=json.load(open(sys.argv[1])); print(next(x["execution_path"] for x in d["outputs"] if x["role"] == "output"))' \
    "$external_scratch_record")
  run_stderr=$("$PYTHON_BIN" -c \
    'import json,sys; d=json.load(open(sys.argv[1])); print(next(x["execution_path"] for x in d["outputs"] if x["role"] == "stderr"))' \
    "$external_scratch_record")
  external_execution_directory="$run_directory"
  while IFS= read -r scratch_assignment; do
    [[ -z "$scratch_assignment" ]] || export "$scratch_assignment"
  done < <("$PYTHON_BIN" -c \
    'import json,sys; value=json.load(open(sys.argv[1])).get("process_environment", {}); [print(f"{name}={value[name]}") for name in sorted(value)]' \
    "$external_scratch_record")
fi

if [[ -n "$runtime_contract" ]]; then
  runtime_materialize=(
    "$PYTHON_BIN" -m cmw.molecular.orca.cli runtime-materialize
    --runtime "$runtime_contract" --orca-exe "$orca_command"
    --working-directory "$run_directory"
    --output "$runtime_launch_manifest"
  )
  ((external_configured == 0)) || runtime_materialize+=(--copy-working-directory-overlay)
  if ! runtime_launch_json=$("${runtime_materialize[@]}"); then
    printf 'ORCA launch-time runtime validation failed: %s\n' \
      "$runtime_launch_json" >&2
    exit 69
  fi
  resolved_artifacts+=("runtime_launch_contract=$runtime_launch_manifest")
fi

if ((external_configured == 1)); then
  "$PYTHON_BIN" -m cmw.core.external_scratch_cli running \
    --record "$external_scratch_record" >/dev/null
fi

# ORCA file scratch follows cwd onto external storage. Keep TMPDIR on the local
# POSIX filesystem because OpenMPI's shared-memory transport is not exFAT-safe.
scratch_parent=${TMPDIR:-/tmp}
mkdir -p "$scratch_parent"
local_scratch_dir=$(mktemp -d "$scratch_parent/cmw-orca.XXXXXXXX")
export TMPDIR="$local_scratch_dir"

orca_version=$(
  "$orca_command" --version 2>/dev/null | head -n 1 || true
)

input_name=$(basename "$run_input")
launch=("$orca_command" "$input_name")
if [[ ${CMW_CAFFEINATE:-0} == 1 ]] && command -v caffeinate >/dev/null 2>&1; then
  launch=(caffeinate -i "${launch[@]}")
fi

set +e
(
  cd "$run_directory"
  exec "${launch[@]}"
) </dev/null >"$run_output" 2>"$run_stderr" &
child_pid=$!
wait "$child_pid"
process_status=$?
child_pid=""
set -e

if ((external_configured == 1)); then
  if ! "$PYTHON_BIN" -m cmw.core.external_scratch_cli copy-back \
    --record "$external_scratch_record" \
    --process-exit-code "$process_status" >/dev/null
  then
    printf 'External scratch copy-back failed; scratch was preserved: %s\n' \
      "$external_execution_directory" >&2
    exit 75
  fi
fi

finalize=(
  "$PYTHON_BIN" -m cmw.molecular.orca.cli finalize
  --target "$target" --metadata "$metadata" --input "$input"
  --output "$output" --stderr "$stderr_path" --process-exit-code "$process_status"
  --orca-exe "$orca_command" --orca-version "$orca_version"
  --nprocs "$NPROCS" --maxcore "$MAXCORE_MB" --repository "$REPO_ROOT"
  --imaginary-tolerance "$CMW_IMAGINARY_TOLERANCE_CM1"
)
((require_minimum == 0)) || finalize+=(--require-minimum)
[[ -z "$layout" ]] || finalize+=(--layout "$layout")
[[ -z "$geometry_contract" ]] || finalize+=(--geometry-contract "$geometry_contract")
if ((${#resolved_artifacts[@]} > 0)); then
  for item in "${resolved_artifacts[@]}"; do
    finalize+=(--artifact "$item")
  done
fi
if ! "${finalize[@]}" >/dev/null; then
  printf 'ORCA attempt did not satisfy execution, scientific, or artifact validation\n' >&2
  exit 70
fi
if ((external_configured == 1)); then
  if ! "$PYTHON_BIN" -m cmw.core.external_scratch_cli cleanup \
    --record "$external_scratch_record" \
    --finalization-metadata "$metadata" >/dev/null
  then
    printf 'Scientific result finalized, but owned scratch cleanup failed: %s\n' \
      "$external_execution_directory" >&2
    exit 75
  fi
fi
printf '[COMPLETE] %s\n' "$metadata"
