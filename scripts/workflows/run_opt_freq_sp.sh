#!/usr/bin/env bash
set -euo pipefail

# User-facing workflow orchestrator. Python owns stage semantics and returns one
# structured next action; shell owns sequencing and process-launch boundaries.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd -P)
PYTHON_BIN=${PYTHON_BIN:-python3}
ORCA_RUNNER=${CMW_ORCA_RUNNER:-$REPO_ROOT/scripts/orca/run_orca.sh}
source "$REPO_ROOT/scripts/runtime_python.sh"

forwarded=()
plan=0
json_plan=0
new_attempt=0
controlled_suspension=0
selected_mode=""
output=""
state_path=""
runtime_contract=${CMW_ORCA_RUNTIME_CONTRACT:-}

usage() {
  printf '%s\n' \
    "Usage: run_opt_freq_sp.sh --mode MODE --structure FILE --config FILE --output DIR" \
    "       [--orca-exe FILE] [--nprocs N] [--maxcore MB]" \
    "       [--require-minimum|--no-require-minimum] [--imaginary-tolerance CM-1]" \
    "       [--new-attempt] [--plan [--json]] [--controlled-suspension (opt only)]" \
    "       or: run_opt_freq_sp.sh --state EXACT_PREPARED_WORKFLOW_STATE" >&2
}

while (($#)); do
  case "$1" in
    --state) state_path=${2:?missing state path}; shift 2 ;;
    --controlled-suspension) controlled_suspension=1; shift ;;
    --mode|--structure|--config|--orca-exe|--nprocs|--maxcore|--imaginary-tolerance)
      [[ "$1" != --mode ]] || selected_mode=${2:?missing mode}
      forwarded+=("$1" "${2:?missing value for $1}")
      shift 2
      ;;
    --output)
      output=${2:?missing value for --output}
      forwarded+=("$1" "$output")
      shift 2
      ;;
    --require-minimum|--no-require-minimum)
      forwarded+=("$1")
      shift
      ;;
    --new-attempt)
      new_attempt=1
      shift
      ;;
    --plan) plan=1; shift ;;
    --json) json_plan=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage; exit 64 ;;
  esac
done

if ((controlled_suspension == 1)) && [[ "$selected_mode" != opt ]]; then
  printf 'Controlled suspension supports the single OPT stage only; multi-stage/frequency workflows are unqualified\n' >&2
  exit 64
fi

if ((plan)); then
  if ((new_attempt)); then
    printf '%s\n' '--new-attempt cannot be combined with --plan' >&2
    exit 64
  fi
  plan_format=human
  ((json_plan == 0)) || plan_format=json
  exec "$PYTHON_BIN" -m cmw.molecular.workflows.cli plan \
    "${forwarded[@]}" --format "$plan_format"
fi
if ((json_plan)); then
  printf '%s\n' '--json is valid only with --plan' >&2
  exit 64
fi
if [[ -n "$state_path" ]]; then
  if ((${#forwarded[@]} || plan || json_plan || new_attempt || controlled_suspension)); then
    printf 'An exact prepared state cannot be combined with workflow initialization options\n' >&2
    exit 64
  fi
  [[ -f "$state_path" ]] || { printf 'Prepared workflow state is unavailable\n' >&2; exit 64; }
else
  init_command=("$PYTHON_BIN" -m cmw.molecular.workflows.cli init "${forwarded[@]}")
  ((new_attempt == 0)) || init_command+=(--new-attempt)
  init_json=$("${init_command[@]}")
  state_path=$(printf '%s' "$init_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["state_path"])')
fi

json_fields() {
  local document=$1
  shift
  printf '%s' "$document" | "$PYTHON_BIN" -c '
import json,sys
document = json.load(sys.stdin)
if not isinstance(document, dict):
    raise ValueError("Workflow action must be an object")
values = [str(document.get(field, "")) for field in sys.argv[1:]]
if any("\0" in value for value in values):
    raise ValueError("Workflow action contains NUL")
sys.stdout.write("\0".join(values) + "\0")' "$@"
}

# Decode once per response; a checked private file preserves both literal fields
# and producer failures on Bash 3.2 without eval or process-substitution status loss.
action_file=$(mktemp "${TMPDIR:-/tmp}/cmw-workflow-action.XXXXXX")
trap 'rm -f -- "$action_file"' EXIT
action_keys=(action result_path failure_point reason stage input target metadata
  output stderr final_geometry layout geometry_contract orca_exe nprocs
  maxcore_mb_per_process require_minimum imaginary_tolerance_cm1)

while true; do
  set +e
  action_json=$("$PYTHON_BIN" -m cmw.molecular.workflows.cli next --state "$state_path")
  action_status=$?
  set -e
  if ! json_fields "$action_json" "${action_keys[@]}" > "$action_file"; then
    printf 'Unable to decode the next workflow action\n' >&2
    exit 64
  fi
  action_fields=()
  while IFS= read -r -d '' field; do
    action_fields+=("$field")
  done < "$action_file"
  if ((${#action_fields[@]} != ${#action_keys[@]})); then
    printf 'Incomplete workflow action fields\n' >&2
    exit 64
  fi
  action=${action_fields[0]}
  if [[ "$action" == COMPLETE && $action_status -eq 10 ]]; then
    result_path=${action_fields[1]}
    printf '[WORKFLOW COMPLETE] %s\n' "$result_path"
    exit 0
  fi
  if [[ "$action" == FAILED || $action_status -eq 20 ]]; then
    printf 'Workflow stopped at %s: %s\n' \
      "${action_fields[2]}" "${action_fields[3]}" >&2
    exit 70
  fi
  if [[ "$action" != RUN || $action_status -ne 0 ]]; then
    printf 'Unable to determine the next workflow action: %s\n' "$action_json" >&2
    exit 64
  fi

  stage=${action_fields[4]}
  input=${action_fields[5]}
  target=${action_fields[6]}
  metadata=${action_fields[7]}
  stage_output=${action_fields[8]}
  stage_stderr=${action_fields[9]}
  final_geometry=${action_fields[10]}
  layout=${action_fields[11]}
  geometry_contract=${action_fields[12]}
  stage_orca_exe=${action_fields[13]}
  stage_nprocs=${action_fields[14]}
  stage_maxcore=${action_fields[15]}
  require_minimum=${action_fields[16]}
  imaginary_tolerance=${action_fields[17]}

  runner=(
    "$ORCA_RUNNER" --input "$input" --target "$target" --metadata "$metadata"
    --output "$stage_output" --stderr "$stage_stderr"
  )
  ((controlled_suspension == 0)) || runner+=(--controlled-suspension)
  [[ -z "$runtime_contract" ]] || runner+=(--runtime-contract "$runtime_contract" --require-runtime-contract)
  [[ -z "$layout" ]] || runner+=(--layout "$layout")
  [[ -z "$geometry_contract" ]] || runner+=(--geometry-contract "$geometry_contract")
  [[ -z "$final_geometry" ]] || runner+=(--artifact "final_geometry=$final_geometry")

  set +e
  ORCA_EXE="$stage_orca_exe" NPROCS="$stage_nprocs" MAXCORE_MB="$stage_maxcore" \
    CMW_REQUIRE_MINIMUM=$([[ "$require_minimum" == True ]] && printf 1 || printf 0) \
    CMW_IMAGINARY_TOLERANCE_CM1="$imaginary_tolerance" \
    "${runner[@]}"
  runner_status=$?
  set -e

  set +e
  advance_json=$("$PYTHON_BIN" -m cmw.molecular.workflows.cli advance \
    --state "$state_path" --stage "$stage")
  advance_status=$?
  set -e
  if ((advance_status != 0)); then
    printf 'Workflow stage %s failed validation: %s\n' "$stage" "$advance_json" >&2
    ((runner_status == 0)) && exit 70
    exit "$runner_status"
  fi
done
