#!/usr/bin/env bash
set -euo pipefail

# Shared operational runner for public FMO/ESP entrypoints. This script owns
# locks, process launch, descriptors, signals, and runtime environment only.

CMW_WORKFLOW_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
CMW_ROOT=$(cd "$CMW_WORKFLOW_DIR/../.." && pwd -P)
PYTHON_BIN=${PYTHON_BIN:-python3}
source "$CMW_ROOT/scripts/runtime_python.sh"

# shellcheck source=scripts/multiwfn/multiwfn_runtime.sh
source "$CMW_ROOT/scripts/multiwfn/multiwfn_runtime.sh"

multiwfn_cube_usage() {
  printf '%s\n' \
    "Usage: $0 --source RESULT.json --output DIRECTORY" \
    "          (--grid-spacing-bohr VALUE | --fragments FILE --config FILE)" \
    "          [--multiwfn-exe FILE]" \
    "          [--settings-source FILE] [--threads N] [--plan]" >&2
}

multiwfn_cube_main() {
  local operation=$1
  shift
  local source_result="" output_root="" grid_spacing="" settings_source=""
  local fragment_path="" igmh_config=""
  local cli_threads="" plan_only=0 plan_json prepare_json prepared_plan="" prepared_json=""
  while (($#)); do
    case "$1" in
      --prepared-plan) prepared_plan=${2:?}; shift 2 ;;
      --source) source_result=${2:?}; shift 2 ;;
      --output) output_root=${2:?}; shift 2 ;;
      --grid-spacing-bohr) grid_spacing=${2:?}; shift 2 ;;
      --fragments) fragment_path=${2:?}; shift 2 ;;
      --config) igmh_config=${2:?}; shift 2 ;;
      --multiwfn-exe) MULTIWFN_EXE=${2:?}; shift 2 ;;
      --settings-source) settings_source=${2:?}; shift 2 ;;
      --threads) cli_threads=${2:?}; shift 2 ;;
      --plan) plan_only=1; shift ;;
      --json) shift ;;
      --help|-h) multiwfn_cube_usage; return 0 ;;
      *) printf 'Unknown argument: %s\n' "$1" >&2; multiwfn_cube_usage; return 64 ;;
    esac
  done
  if [[ -n "$prepared_plan" ]]; then
    prepared_json=$("$PYTHON_BIN" -m cmw.molecular.multiwfn.jobs verify-prepared "$prepared_plan") || return 64
    source_result=$(printf '%s' "$prepared_json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["source_result"])')
    output_root=$(printf '%s' "$prepared_json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["scientific_plan"]["output_root"])')
    [[ $(printf '%s' "$prepared_json" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["profile"])') == "$operation" ]] || return 64
  fi
  if [[ -z "$source_result" || -z "$output_root" ]]; then
    multiwfn_cube_usage
    return 64
  fi
  if [[ -n "$prepared_plan" ]]; then
    : # The versioned prepared plan already checked its operation-specific inputs.
  elif [[ "$operation" == IGMH ]]; then
    if [[ -z "$fragment_path" || -z "$igmh_config" ]]; then
      multiwfn_cube_usage
      return 64
    fi
  elif [[ -z "$grid_spacing" ]]; then
    multiwfn_cube_usage
    return 64
  fi

  local plan_command=(
    "$PYTHON_BIN" -m cmw.molecular.workflows.downstream_cli plan
    --operation "$operation" --source "$source_result" --output "$output_root"
  )
  if [[ "$operation" == IGMH ]]; then
    plan_command+=(--fragments "$fragment_path" --config "$igmh_config")
  else
    plan_command+=(--grid-spacing-bohr "$grid_spacing")
  fi
  [[ -z "$cli_threads" ]] || plan_command+=(--threads "$cli_threads")
  if [[ -n "$prepared_plan" ]]; then
    plan_json=$(printf '%s' "$prepared_json" | "$PYTHON_BIN" -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["scientific_plan"]))')
  elif ! plan_json=$("${plan_command[@]}"); then
    printf '%s\n' "$plan_json"
    return 64
  fi
  if ((plan_only)); then
    printf '%s\n' "$plan_json"
    return 0
  fi

  local target_path result_path lock_path lock_json lock_token="" child_pid="" monitor_pid=""
  target_path=$(printf '%s' "$plan_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["target_path"])')
  result_path=$(printf '%s' "$plan_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["result_path"])')
  if printf '%s' "$plan_json" | "$PYTHON_BIN" -c \
    'import json,sys; raise SystemExit(not json.load(sys.stdin)["reuse"]["reuse"])'
  then
    printf '[REUSED] %s\n' "$result_path" >&2
    "$PYTHON_BIN" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2, sort_keys=True))' "$result_path"
    return 0
  fi

  lock_path=${target_path}.lock
  cleanup_multiwfn_workflow() {
    local status=$?
    trap - EXIT INT TERM
    if [[ -n "${child_pid:-}" ]] && kill -0 "$child_pid" 2>/dev/null; then
      kill -TERM "$child_pid" 2>/dev/null || true
      wait "$child_pid" 2>/dev/null || true
    fi
    if [[ -n "${monitor_pid:-}" ]] && kill -0 "$monitor_pid" 2>/dev/null; then
      kill -TERM "$monitor_pid" 2>/dev/null || true
      wait "$monitor_pid" 2>/dev/null || true
    fi
    if [[ -n "${lock_token:-}" ]]; then
      "$PYTHON_BIN" -m cmw.molecular.workflows.downstream_cli lock release \
        --lock "$lock_path" --token "$lock_token" >/dev/null 2>&1 || true
    fi
    return "$status"
  }
  trap cleanup_multiwfn_workflow EXIT
  trap 'return 130' INT
  trap 'return 143' TERM

  lock_json=$("$PYTHON_BIN" -m cmw.molecular.workflows.downstream_cli lock acquire \
    --lock "$lock_path" --job-id "$target_path" --owner-pid "$$") || return 73
  lock_token=$(printf '%s' "$lock_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["owner"]["token"])')

  local prepare_command=(
    "$PYTHON_BIN" -m cmw.molecular.workflows.downstream_cli prepare
    --operation "$operation" --source "$source_result" --output "$output_root"
  )
  if [[ "$operation" == IGMH ]]; then
    prepare_command+=(--fragments "$fragment_path" --config "$igmh_config")
  else
    prepare_command+=(--grid-spacing-bohr "$grid_spacing")
  fi
  [[ -z "$cli_threads" ]] || prepare_command+=(--threads "$cli_threads")
  if [[ -n "$prepared_plan" ]]; then
    prepare_json=$plan_json
  elif ! prepare_json=$("${prepare_command[@]}"); then
    printf '%s\n' "$prepare_json"
    return 64
  fi
  local attempt_directory runtime_path menu_path source_wavefunction source_geometry
  attempt_directory=$(printf '%s' "$prepare_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["attempt_directory"])')
  source_wavefunction=$(printf '%s' "$prepare_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["source"]["wavefunction_path"])')
  source_geometry=$(printf '%s' "$prepare_json" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["source"]["geometry_path"])')
  runtime_path=$attempt_directory/runtime.json
  menu_path=$attempt_directory/menu.in
  if [[ -n "$cli_threads" ]]; then
    MULTIWFN_NTHREADS=$cli_threads
  fi
  if [[ -n "$prepared_plan" ]]; then
    multiwfn_runtime_load "$runtime_path"
  else
    multiwfn_runtime_prepare "$attempt_directory" "$runtime_path" "$settings_source"
    "$PYTHON_BIN" -m cmw.molecular.workflows.downstream_cli menu \
      --target "$target_path" --runtime "$runtime_path" --destination "$menu_path" >/dev/null
  fi

  export CMW_MULTIWFN_OPERATION=$operation CMW_SOURCE_GEOMETRY=$source_geometry
  multiwfn_health_monitor() {
    local owned_pid=$1 attempt=$2 interval=${CMW_HEALTH_INTERVAL:-30}
    local sleep_pid=""
    trap '
      if [[ -n "${sleep_pid:-}" ]]; then kill "$sleep_pid" 2>/dev/null || true; fi
      exit 0
    ' INT TERM
    while kill -0 "$owned_pid" 2>/dev/null; do
      "$PYTHON_BIN" -m cmw.molecular.multiwfn.cli health \
        --pid "$owned_pid" --state "$attempt/health-state.json" \
        --activity "$attempt/multiwfn.log" --threads "$MULTIWFN_NTHREADS" \
        --history "$attempt/health-history.jsonl" \
        >"$attempt/health-latest.json" 2>/dev/null || true
      sleep "$interval" &
      sleep_pid=$!
      wait "$sleep_pid" || return 0
      sleep_pid=""
    done
  }
  multiwfn_jobs_event "$attempt_directory" launch_requested
  set +e
  (
    # The parent owns the stage lock; runtime must not restore its cleanup trap.
    trap - EXIT INT TERM
    cd "$attempt_directory"
    multiwfn_runtime_launch "$source_wavefunction" <menu.in >multiwfn.log 2>multiwfn.stderr
  ) &
  child_pid=$!
  multiwfn_health_monitor "$child_pid" "$attempt_directory" &
  monitor_pid=$!
  wait "$child_pid"
  local process_status=$?
  printf '%s\n' "$process_status" > "$attempt_directory/process.exitcode"
  multiwfn_jobs_event "$attempt_directory" process_exited "$process_status"
  child_pid=""
  kill -TERM "$monitor_pid" 2>/dev/null || true
  wait "$monitor_pid" 2>/dev/null || true
  monitor_pid=""
  set -e
  multiwfn_jobs_event "$attempt_directory" validation_started
  if [[ -n "$prepared_plan" ]]; then
    "$PYTHON_BIN" -m cmw.molecular.multiwfn.jobs verify-finalize "$prepared_plan" >/dev/null || {
      multiwfn_jobs_event "$attempt_directory" validation_failed
      return 70
    }
  fi
  if ((process_status != 0)); then
    local failure_json
    failure_json=$("$PYTHON_BIN" -m cmw.molecular.workflows.downstream_cli finalize \
      --target "$target_path" --runtime "$runtime_path" --menu "$menu_path" \
      --attempt-directory "$attempt_directory" --process-exit-code "$process_status" \
      --repository "$CMW_ROOT" 2>/dev/null || true)
    printf 'Multiwfn %s attempt failed with status %d; diagnostics retained in %s\n' \
      "$operation" "$process_status" "$attempt_directory" >&2
    [[ -z "$failure_json" ]] || printf '%s\n' "$failure_json" >&2
    multiwfn_jobs_event "$attempt_directory" validation_failed
    return "$process_status"
  fi
  "$PYTHON_BIN" -m cmw.molecular.workflows.downstream_cli normalize \
    --target "$target_path" --attempt-directory "$attempt_directory" >/dev/null
  local final_json
  final_json=$("$PYTHON_BIN" -m cmw.molecular.workflows.downstream_cli finalize \
    --target "$target_path" --runtime "$runtime_path" --menu "$menu_path" \
    --attempt-directory "$attempt_directory" --process-exit-code "$process_status" \
    --repository "$CMW_ROOT") || {
      printf '%s\n' "$final_json"
      multiwfn_jobs_event "$attempt_directory" validation_failed
      return 70
    }
  multiwfn_jobs_event "$attempt_directory" validation_passed
  "$PYTHON_BIN" -m cmw.molecular.workflows.downstream_cli lock release \
    --lock "$lock_path" --token "$lock_token" >/dev/null
  lock_token=""
  trap - EXIT INT TERM
  printf '%s\n' "$final_json"
}
