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

input=""
target=""
metadata=""
output=""
stderr_path=""
layout=""
replace_stale=0
artifacts=()

usage() {
  printf '%s\n' \
    "Usage: run_orca.sh --input FILE --target FILE --metadata FILE" \
    "                   --output FILE --stderr FILE [--artifact ROLE=FILE]" \
    "                   [--layout FILE]" \
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

absolute_path() {
  local path=$1 directory name
  directory=$(dirname "$path")
  name=$(basename "$path")
  mkdir -p "$directory"
  directory=$(cd "$directory" && pwd -P)
  printf '%s/%s\n' "$directory" "$name"
}

input=$(absolute_path "$input")
target=$(absolute_path "$target")
metadata=$(absolute_path "$metadata")
output=$(absolute_path "$output")
stderr_path=$(absolute_path "$stderr_path")
if [[ -n "$layout" ]]; then
  layout=$(absolute_path "$layout")
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

export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if "$PYTHON_BIN" -m cmw.molecular.orca.cli reuse \
  --target "$target" --metadata "$metadata" >/dev/null 2>&1
then
  printf '[REUSED] %s\n' "$metadata"
  exit 0
fi

if [[ ! -f "$input" || ! -f "$target" ]]; then
  printf 'ORCA input or target metadata is missing\n' >&2
  exit 64
fi
if ! command -v "$ORCA_EXE" >/dev/null 2>&1 && [[ ! -x "$ORCA_EXE" ]]; then
  printf 'ORCA executable was not found: %s\n' "$ORCA_EXE" >&2
  exit 69
fi

lock_path="${target}.lock"
lock_json=""
lock_token=""
child_pid=""
scratch_dir=""

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
  if [[ -n "$scratch_dir" && -d "$scratch_dir" && $(basename "$scratch_dir") == cmw-orca.* ]]; then
    rm -r -- "$scratch_dir"
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
if "$PYTHON_BIN" -m cmw.molecular.orca.cli reuse \
  --target "$target" --metadata "$metadata" >/dev/null 2>&1
then
  printf '[REUSED] %s\n' "$metadata"
  exit 0
fi

if [[ -e "$metadata" || -e "$output" || -e "$stderr_path" ]]; then
  printf 'Non-reusable attempt artifacts already exist; choose a new attempt directory\n' >&2
  exit 73
fi

scratch_parent=${TMPDIR:-/tmp}
mkdir -p "$scratch_parent"
scratch_dir=$(mktemp -d "$scratch_parent/cmw-orca.XXXXXXXX")
export TMPDIR="$scratch_dir"

orca_version=$(
  "$ORCA_EXE" --version 2>/dev/null | head -n 1 || true
)

launch=("$ORCA_EXE" "$input")
if [[ ${CMW_CAFFEINATE:-0} == 1 ]] && command -v caffeinate >/dev/null 2>&1; then
  launch=(caffeinate -i "${launch[@]}")
fi

set +e
"${launch[@]}" </dev/null >"$output" 2>"$stderr_path" &
child_pid=$!
wait "$child_pid"
process_status=$?
child_pid=""
set -e

finalize=(
  "$PYTHON_BIN" -m cmw.molecular.orca.cli finalize
  --target "$target" --metadata "$metadata" --input "$input"
  --output "$output" --stderr "$stderr_path" --process-exit-code "$process_status"
  --orca-exe "$ORCA_EXE" --orca-version "$orca_version"
  --nprocs "$NPROCS" --maxcore "$MAXCORE_MB" --repository "$REPO_ROOT"
)
[[ -z "$layout" ]] || finalize+=(--layout "$layout")
if ((${#resolved_artifacts[@]} > 0)); then
  for item in "${resolved_artifacts[@]}"; do
    finalize+=(--artifact "$item")
  done
fi
[[ ${CMW_REQUIRE_MINIMUM:-0} == 1 ]] && finalize+=(--require-minimum)
finalize+=(--imaginary-tolerance "${CMW_IMAGINARY_TOLERANCE_CM1:-0.0}")

if ! "${finalize[@]}" >/dev/null; then
  printf 'ORCA attempt did not satisfy execution, scientific, or artifact validation\n' >&2
  exit 70
fi
printf '[COMPLETE] %s\n' "$metadata"
