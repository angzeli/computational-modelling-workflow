#!/usr/bin/env bash
# Run the deterministic offline suite as a few concurrent unittest processes on
# one machine. Together the groups cover exactly `unittest discover -s tests`;
# each owns its TMPDIR, Jobs state and lifecycle evidence, and nothing else is shared.
set -uo pipefail
source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
work_root=${1:?usage: run_tests.sh NEW_EXTERNAL_DIRECTORY}
python_exe=${PYTHON_BIN:-python3}
mkdir -m 700 "$work_root"
work_root=$(cd "$work_root" && pwd -P)
case "$work_root/" in "$source_root/"*) printf 'Test state requires a directory outside the checkout\n' >&2; exit 64;; esac
unset PYTHONPATH

top_level=()
for path in "$source_root"/tests/test_*.py; do
  name=${path##*/}
  top_level+=("tests.${name%.py}")
done

suite() {
  local name=$1; shift
  mkdir -m 700 "$work_root/$name" "$work_root/$name/tmp"
  (cd "$source_root" && TMPDIR="$work_root/$name/tmp" CMW_JOBS_STATE="$work_root/$name/unused-state" \
    CMW_TEST_EVIDENCE_DIR="$work_root/$name/evidence" "$python_exe" -m unittest "$@")
}

# Groups are balanced from measured per-directory runtimes.
remaining() {
  local status=0 directory
  for directory in periodic molecular workflows runtime campaign doctor progress structure core adapters; do
    suite "rest-$directory" discover -s "tests/$directory" -t tests || status=1
  done
  suite rest-top-level "${top_level[@]}" || status=1
  return "$status"
}

names=(jobs-a-m jobs-n-z nodes remaining)
suite jobs-a-m discover -s tests/jobs -t tests -p 'test_[a-m]*.py' > "$work_root/jobs-a-m.log" 2>&1 &
pids=($!)
suite jobs-n-z discover -s tests/jobs -t tests -p 'test_[n-z]*.py' > "$work_root/jobs-n-z.log" 2>&1 &
pids+=($!)
suite nodes discover -s tests/nodes -t tests > "$work_root/nodes.log" 2>&1 &
pids+=($!)
remaining > "$work_root/remaining.log" 2>&1 &
pids+=($!)

status=0
for index in "${!pids[@]}"; do
  if wait "${pids[$index]}"; then result=PASS; else result=FAIL; status=1; fi
  printf '::group::%s %s\n' "$result" "${names[$index]}"
  cat "$work_root/${names[$index]}.log"
  printf '::endgroup::\n'
done
grep -h '^Ran [0-9]* test' "$work_root"/*.log
exit "$status"
