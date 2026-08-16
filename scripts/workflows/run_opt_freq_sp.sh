#!/usr/bin/env bash
set -euo pipefail

# User-facing workflow orchestrator. Python owns stage semantics and returns one
# structured next action; shell owns sequencing and process-launch boundaries.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd -P)
PYTHON_BIN=${PYTHON_BIN:-python3}
ORCA_RUNNER=${CMW_ORCA_RUNNER:-$REPO_ROOT/scripts/orca/run_orca.sh}
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

forwarded=()
plan=0
json_plan=0
new_attempt=0
output=""

usage() {
  printf '%s\n' \
    "Usage: run_opt_freq_sp.sh --mode MODE --structure FILE --config FILE --output DIR" \
    "       [--orca-exe FILE] [--nprocs N] [--maxcore MB]" \
    "       [--require-minimum|--no-require-minimum] [--imaginary-tolerance CM-1]" \
    "       [--new-attempt] [--plan [--json]]" >&2
}

while (($#)); do
  case "$1" in
    --mode|--structure|--config|--orca-exe|--nprocs|--maxcore|--imaginary-tolerance)
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
init_command=("$PYTHON_BIN" -m cmw.molecular.workflows.cli init "${forwarded[@]}")
((new_attempt == 0)) || init_command+=(--new-attempt)
init_json=$("${init_command[@]}")
state_path=$(printf '%s' "$init_json" | "$PYTHON_BIN" -c \
  'import json,sys; print(json.load(sys.stdin)["state_path"])')

json_field() {
  local document=$1 field=$2
  printf '%s' "$document" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$field"
}

while true; do
  set +e
  action_json=$("$PYTHON_BIN" -m cmw.molecular.workflows.cli next --state "$state_path")
  action_status=$?
  set -e
  action=$(json_field "$action_json" action)
  if [[ "$action" == COMPLETE && $action_status -eq 10 ]]; then
    result_path=$(json_field "$action_json" result_path)
    printf '[WORKFLOW COMPLETE] %s\n' "$result_path"
    exit 0
  fi
  if [[ "$action" == FAILED || $action_status -eq 20 ]]; then
    printf 'Workflow stopped at %s: %s\n' \
      "$(json_field "$action_json" failure_point)" \
      "$(json_field "$action_json" reason)" >&2
    exit 70
  fi
  if [[ "$action" != RUN || $action_status -ne 0 ]]; then
    printf 'Unable to determine the next workflow action: %s\n' "$action_json" >&2
    exit 64
  fi

  stage=$(json_field "$action_json" stage)
  input=$(json_field "$action_json" input)
  target=$(json_field "$action_json" target)
  metadata=$(json_field "$action_json" metadata)
  stage_output=$(json_field "$action_json" output)
  stage_stderr=$(json_field "$action_json" stderr)
  final_geometry=$(json_field "$action_json" final_geometry)
  layout=$(json_field "$action_json" layout)
  geometry_contract=$(json_field "$action_json" geometry_contract)
  stage_orca_exe=$(json_field "$action_json" orca_exe)
  stage_nprocs=$(json_field "$action_json" nprocs)
  stage_maxcore=$(json_field "$action_json" maxcore_mb_per_process)
  require_minimum=$(json_field "$action_json" require_minimum)
  imaginary_tolerance=$(json_field "$action_json" imaginary_tolerance_cm1)

  runner=(
    "$ORCA_RUNNER" --input "$input" --target "$target" --metadata "$metadata"
    --output "$stage_output" --stderr "$stage_stderr"
  )
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
