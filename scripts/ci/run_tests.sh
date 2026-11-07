#!/usr/bin/env bash
# Run the deterministic offline suite with one unittest process per test module,
# a few at a time. Every tests/**/test_*.py is discovered, so coverage matches
# `unittest discover -s tests`. Fresh processes matter: long-lived ones measured
# twice as slow on CI. Each module owns its TMPDIR, Jobs state and evidence.
set -uo pipefail
source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
work_root=${1:?usage: run_tests.sh NEW_EXTERNAL_DIRECTORY}
python_exe=${PYTHON_BIN:-python3}
parallel=${CMW_TEST_JOBS:-$(getconf _NPROCESSORS_ONLN)}
mkdir -m 700 "$work_root"
work_root=$(cd "$work_root" && pwd -P)
case "$work_root/" in "$source_root/"*) printf 'Test state requires a directory outside the checkout\n' >&2; exit 64;; esac
unset PYTHONPATH

run_module() {
  local relative=${1#"$source_root/"} module work started status tests line
  local started_at finished_at result pipeline_status
  # Same module names and sys.path as `discover -s tests -t tests`; a short
  # work directory keeps path-length-sensitive tests representative.
  module=${relative#tests/} module=${module%.py} module=${module//\//.}
  work=$(mktemp -d "$work_root/m.XXXXXX")
  mkdir -m 700 "$work/tmp"
  started=$SECONDS
  started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf 'CMW_TEST_MODULE_STARTED module=%q pid=%s started_at=%s log=%q\n' \
    "$relative" "$$" "$started_at" "$work/log"
  (cd "$source_root" && TMPDIR="$work/tmp" CMW_JOBS_STATE="$work/unused-state" \
    CMW_TEST_EVIDENCE_DIR="$work/evidence" "$python_exe" -c \
    'import sys, unittest; sys.path.insert(0, "tests"); unittest.main(module=None, argv=["python -m unittest", sys.argv[1]])' \
    "$module") 2>&1 | tee "$work/log" | while IFS= read -r line || [[ -n "$line" ]]; do
      # The sweep watchdog's stack frames have no marker. Keep the entire
      # sweep stream live; other modules expose only marked diagnostics.
      if [[ $module == jobs.test_ownership_fault_sweep ]]; then
        printf '%s\n' "$line"
      else
        # Unittest progress may precede a marker on the same line.
        case "$line" in *OWNERSHIP_FAULT_*) printf '%s\n' "$line";; esac
      fi
    done
  pipeline_status=("${PIPESTATUS[@]}")
  status=${pipeline_status[0]}
  if [[ $status == 0 && ( ${pipeline_status[1]} != 0 || ${pipeline_status[2]} != 0 ) ]]; then
    status=1
  fi
  tests=$(sed -n 's/^Ran \([0-9]*\) tests\{0,1\} in .*/\1/p' "$work/log")
  finished_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  result=$([[ $status == 0 ]] && echo PASS || echo FAIL)
  printf '%s %s tests=%s seconds=%s log=%s\n' "$result" \
    "$relative" "${tests:-0}" "$((SECONDS - started))" "$work/log" >> "$work_root/results.txt"
  printf 'CMW_TEST_MODULE_FINISHED module=%q result=%s exit_code=%s tests=%s elapsed_seconds=%s started_at=%s finished_at=%s log=%q\n' \
    "$relative" "$result" "$status" "${tests:-unknown}" "$((SECONDS - started))" \
    "$started_at" "$finished_at" "$work/log"
}
export -f run_module
export source_root work_root python_exe

# Largest modules first, so a slow one does not start last and set the tail
# (measured on CI: 512 s versus ~740 s in path order).
find "$source_root/tests" -name 'test_*.py' -type f -exec ls -1S {} + |
  xargs -P "$parallel" -I{} bash -c 'run_module "$1"' _ {}

status=0
while read -r result relative _ _ log; do
  [[ $result == PASS ]] && continue
  status=1
  printf '::group::FAIL %s\n' "$relative"
  cat "${log#log=}"
  printf '::endgroup::\n'
done < "$work_root/results.txt"
sort -t= -k3 -rn "$work_root/results.txt" | head -10
awk '{n++; split($3, t, "="); tests += t[2]; if ($1 != "PASS") failed++}
  END {printf "%d modules, %d tests, %d failed modules\n", n, tests, failed}' "$work_root/results.txt"
exit "$status"
