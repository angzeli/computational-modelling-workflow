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
  local started_at finished_at result pipeline_status stream_timeout=0 counts completed=0 active=0
  local active_test_record='{"test": null}'
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
    CMW_TEST_EVIDENCE_DIR="$work/evidence" "$python_exe" scripts/ci/run_module.py \
    "$work/log" "$module") 2>&1 | while IFS= read -r line || [[ -n "$line" ]]; do
      # The sweep watchdog's stack frames have no marker. Keep the entire
      # sweep stream live; other modules expose only marked diagnostics.
      case "$line" in *CMW_TEST_CASE_STARTED*) active_test_record=${line#*CMW_TEST_CASE_STARTED };; esac
      if [[ $line == *'Timeout ('*')!'* ]]; then
        stream_timeout=1
        printf 'CMW_TEST_TIMEOUT module=%q active=%s\n' "$relative" "$active_test_record"
        "$python_exe" "$source_root/scripts/ci/unittest_guard.py" --report-processes "$work/evidence"
      fi
      if [[ $module == jobs.test_ownership_fault_sweep || $stream_timeout == 1 ]]; then
        printf '%s\n' "$line"
      else
        # Unittest progress may precede a marker on the same line.
        case "$line" in *OWNERSHIP_FAULT_*|*CMW_TEST_CASE_*) printf '%s\n' "$line";; esac
      fi
    done
  pipeline_status=("${PIPESTATUS[@]}")
  status=${pipeline_status[0]}
  "$python_exe" "$source_root/scripts/ci/unittest_guard.py" --report-processes "$work/evidence"
  if [[ $status == 0 && ${pipeline_status[1]} != 0 ]]; then
    status=1
  fi
  tests=$(sed -n 's/^Ran \([0-9]*\) tests\{0,1\} in .*/\1/p' "$work/log")
  if [[ -z $tests && $status == 0 ]]; then
    status=1  # An early zero exit is not a completed unittest module.
  fi
  counts=$(sed -n 's/.*"tests_started": \([0-9]*\), "tests_completed": \([0-9]*\).*/\1 \2/p' "$work/log" | tail -1)
  if [[ -n $counts ]]; then
    read -r active completed <<< "$counts"
  fi
  finished_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  result=$([[ $status == 0 ]] && echo PASS || echo FAIL)
  printf '%s %s tests=%s seconds=%s log=%s completed=%s started=%s\n' "$result" \
    "$relative" "${tests:-incomplete}" "$((SECONDS - started))" "$work/log" \
    "$completed" "$active" >> "$work_root/results.txt"
  printf 'CMW_TEST_MODULE_FINISHED module=%q result=%s exit_code=%s tests=%s elapsed_seconds=%s started_at=%s finished_at=%s log=%q completed=%s started=%s\n' \
    "$relative" "$result" "$status" "${tests:-incomplete}" "$((SECONDS - started))" \
    "$started_at" "$finished_at" "$work/log" "$completed" "$active"
  if [[ $status != 0 ]]; then
    printf '::group::FAIL %s\n' "$relative"
    if [[ $module == jobs.test_ownership_fault_sweep ]]; then
      printf 'Sweep output and watchdog stacks were streamed above; log=%s\n' "$work/log"
    else
      cat "$work/log"
    fi
    printf '::endgroup::\n'
  fi
}
export -f run_module
export source_root work_root python_exe

# Largest modules first, so a slow one does not start last and set the tail
# (measured on CI: 512 s versus ~740 s in path order).
find "$source_root/tests" -name 'test_*.py' -type f -exec ls -1S {} + |
  xargs -P "$parallel" -I{} bash -c 'run_module "$1"' _ {}

status=0
while read -r result _; do
  [[ $result == PASS ]] && continue
  status=1
done < "$work_root/results.txt"
sort -t= -k3 -rn "$work_root/results.txt" | head -10
awk '{n++; split($3, t, "="); if (t[2] == "incomplete") {
    incomplete++; split($(NF-1), c, "="); split($NF, s, "="); completed += c[2]; started += s[2]
  } else tests += t[2]; if ($1 != "PASS") failed++}
  END {if (incomplete) printf "%d modules, %d tests in completed modules, %d failed modules, %d incomplete modules (%d completed methods of %d started in incomplete modules)\n", n, tests, failed, incomplete, completed, started;
       else printf "%d modules, %d tests, %d failed modules\n", n, tests, failed}' "$work_root/results.txt"
exit "$status"
