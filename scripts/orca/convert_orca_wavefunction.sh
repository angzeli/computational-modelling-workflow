#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd -P)
PYTHON_BIN=${PYTHON_BIN:-python3}
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

source_result=""
output_root=""
converter=""
spin_mode=""

usage() {
  printf '%s\n' \
    "Usage: $0 --source RESULT.json --output DIRECTORY" \
    "          --converter ORCA_2MKL --spin-mode restricted|unrestricted"
}

while (($#)); do
  case "$1" in
    --source) source_result=${2:?}; shift 2 ;;
    --output) output_root=${2:?}; shift 2 ;;
    --converter) converter=${2:?}; shift 2 ;;
    --spin-mode) spin_mode=${2:?}; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ -z "$source_result" || -z "$output_root" || -z "$converter" || -z "$spin_mode" ]]; then
  usage >&2
  exit 64
fi

set +e
plan_json=$("$PYTHON_BIN" -m cmw.molecular.orca.conversion_cli plan \
  --source "$source_result" --output "$output_root" \
  --converter "$converter" --spin-mode "$spin_mode")
plan_status=$?
set -e
if ((plan_status == 10)); then
  printf '[REUSED] %s\n' "$(printf '%s' "$plan_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["result_path"])')"
  exit 0
fi
if ((plan_status != 0)); then
  printf '%s\n' "$plan_json" >&2
  exit "$plan_status"
fi
target_path=$(printf '%s' "$plan_json" | "$PYTHON_BIN" -c \
  'import json,sys; print(json.load(sys.stdin)["target_path"])')
mkdir -p "$(dirname "$target_path")"
lock_path=${target_path}.lock
lock_json=""
lock_token=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$lock_token" ]]; then
    "$PYTHON_BIN" -m cmw.molecular.orca.cli lock release \
      --lock "$lock_path" --token "$lock_token" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

lock_json=$("$PYTHON_BIN" -m cmw.molecular.orca.cli lock acquire \
  --lock "$lock_path" --job-id "$target_path" --owner-pid "$$")
lock_token=$(printf '%s' "$lock_json" | "$PYTHON_BIN" -c \
  'import json,sys; print(json.load(sys.stdin)["owner"]["token"])')

set +e
prepare_json=$("$PYTHON_BIN" -m cmw.molecular.orca.conversion_cli prepare \
  --source "$source_result" --output "$output_root" \
  --converter "$converter" --spin-mode "$spin_mode")
prepare_status=$?
set -e
if ((prepare_status == 10)); then
  printf '[REUSED] %s\n' "$(printf '%s' "$prepare_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["result_path"])')"
  exit 0
fi
if ((prepare_status != 0)); then
  printf '%s\n' "$prepare_json" >&2
  exit "$prepare_status"
fi

value() {
  local key=$1
  printf '%s' "$prepare_json" | "$PYTHON_BIN" -c \
    'import json,sys; value=json.load(sys.stdin); print(value[sys.argv[1]])' "$key"
}

plan_path=$(value plan_path)
working_directory=$(printf '%s' "$prepare_json" | "$PYTHON_BIN" -c \
  'import json,sys; print(json.load(sys.stdin)["command"]["working_directory"])')
stdout_path=$(value stdout)
stderr_path=$(value stderr)

set +e
(
  cd "$working_directory"
  "$converter" wavefunction -molden
) </dev/null >"$stdout_path" 2>"$stderr_path"
process_status=$?
set -e

final_json=$("$PYTHON_BIN" -m cmw.molecular.orca.conversion_cli finalize \
  --plan "$plan_path" --process-exit-code "$process_status") || {
    printf '%s\n' "$final_json" >&2
    exit 70
  }
"$PYTHON_BIN" -m cmw.molecular.orca.cli lock release \
  --lock "$lock_path" --token "$lock_token" >/dev/null
lock_token=""
trap - EXIT
printf '[COMPLETE] %s\n' "$(printf '%s' "$final_json" | "$PYTHON_BIN" -c \
  'import json,sys; print(json.load(sys.stdin)["provenance"]["conversion_plan_sha256"])')"
