#!/usr/bin/env bash
# Run the deterministic offline suite as a few concurrent unittest processes on
# one machine. Together the groups cover exactly `unittest discover -s tests`;
# each unit owns its TMPDIR, Jobs state and lifecycle evidence, and nothing else is shared.
set -uo pipefail
source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
work_root=${1:?usage: run_tests.sh NEW_EXTERNAL_DIRECTORY}
python_exe=${PYTHON_BIN:-python3}
mkdir -m 700 "$work_root"
work_root=$(cd "$work_root" && pwd -P)
case "$work_root/" in "$source_root/"*) printf 'Test state requires a directory outside the checkout\n' >&2; exit 64;; esac
unset PYTHONPATH

# Each group runs its units in order; units are DIRECTORY[:PATTERN] under tests/,
# or "top-level" for tests/test_*.py. Balanced from measured CI runtimes, so
# every directory and every tests/jobs initial letter must appear exactly once.
groups=(
  'jobs-a-l=jobs:test_[a-l]*.py'
  'jobs-m-q=jobs:test_[m-q]*.py'
  'jobs-r-z=jobs:test_[r-z]*.py'
  'nodes=nodes'
  'workflows=workflows campaign doctor structure core adapters top-level'
  'molecular=molecular periodic runtime progress'
)

unit() {
  local spec=$1 label=${1//[^A-Za-z0-9]/-} path
  local -a arguments
  if [[ $spec == top-level ]]; then
    arguments=()
    for path in "$source_root"/tests/test_*.py; do
      path=${path##*/}
      arguments+=("tests.${path%.py}")
    done
  else
    arguments=(discover -s "tests/${spec%%:*}" -t tests)
    [[ $spec == *:* ]] && arguments+=(-p "${spec#*:}")
  fi
  mkdir -m 700 "$work_root/$label" "$work_root/$label/tmp"
  (cd "$source_root" && TMPDIR="$work_root/$label/tmp" CMW_JOBS_STATE="$work_root/$label/unused-state" \
    CMW_TEST_EVIDENCE_DIR="$work_root/$label/evidence" "$python_exe" -m unittest "${arguments[@]}")
}

group() {
  local status=0 spec
  local -a specs
  read -r -a specs <<< "$1"
  for spec in "${specs[@]}"; do
    unit "$spec" || status=1
  done
  return "$status"
}

# Refuse to start unless every test module is claimed by exactly one unit.
all_specs=()
for entry in "${groups[@]}"; do
  read -r -a specs <<< "${entry#*=}"
  all_specs+=("${specs[@]}")
done
uncovered=0
while IFS= read -r path; do
  relative=${path#"$source_root/tests/"} base=${path##*/} claims=0
  [[ $relative == */* ]] && directory=${relative%%/*} || directory=top-level
  for spec in "${all_specs[@]}"; do
    [[ ${spec%%:*} == "$directory" ]] || continue
    if [[ $spec != *:* ]] || [[ $base == ${spec#*:} ]]; then claims=$((claims + 1)); fi
  done
  if (( claims != 1 )); then printf 'tests/%s is claimed by %d groups\n' "$relative" "$claims" >&2; uncovered=1; fi
done < <(find "$source_root/tests" -name 'test_*.py' -type f)
(( uncovered == 0 )) || exit 64

names=() pids=()
for entry in "${groups[@]}"; do
  names+=("${entry%%=*}")
  group "${entry#*=}" > "$work_root/${entry%%=*}.log" 2>&1 &
  pids+=($!)
done

status=0
for index in "${!pids[@]}"; do
  if wait "${pids[$index]}"; then result=PASS; else result=FAIL; status=1; fi
  printf '::group::%s %s\n' "$result" "${names[$index]}"
  cat "$work_root/${names[$index]}.log"
  printf '::endgroup::\n'
done
grep -h '^Ran [0-9]* test' "$work_root"/*.log | awk '{tests += $2} END {print "Total tests run:", tests}'
exit "$status"
