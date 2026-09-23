#!/usr/bin/env bash
# Build/install orchestration; Python performs installed behavior validation.
set -euo pipefail
source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
check_root=${1:?usage: check_artifacts.sh NEW_EXTERNAL_DIRECTORY}
python_exe=${PYTHON_BIN:-python3}
mkdir -m 700 "$check_root"
check_root=$(cd "$check_root" && pwd -P)
case "$check_root/" in "$source_root/"*) printf 'Artifact checks require a directory outside the checkout\n' >&2; exit 64;; esac
mkdir -p "$check_root/dist" "$check_root/driver/tests/jobs" "$check_root/driver/tests/periodic/vasp" "$check_root/tmp"
export TMPDIR="$check_root/tmp" PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH
"$python_exe" -m build --outdir "$check_root/dist" "$source_root"
"$python_exe" "$source_root/tests/distribution_hygiene.py" --source-root "$source_root" \
  --artifact "$check_root"/dist/*.whl --artifact "$check_root"/dist/*.tar.gz
cp "$source_root/tests/installed_smoke.py" "$check_root/driver/smoke.py"
: > "$check_root/driver/tests/__init__.py"
: > "$check_root/driver/tests/jobs/__init__.py"
cp "$source_root/tests/jobs/"{isolated_runtime,lifecycle_evidence}.py "$check_root/driver/tests/jobs/"
: > "$check_root/driver/tests/periodic/__init__.py"
: > "$check_root/driver/tests/periodic/vasp/__init__.py"
cp "$source_root/tests/periodic/vasp/"{result_fixtures,result_case}.py "$check_root/driver/tests/periodic/vasp/"
for kind in core jobs sdist editable; do
  env_root="$check_root/env-$kind"
  "$python_exe" -m venv "$env_root"
  wheel=("$check_root"/dist/*.whl)
  sdist=("$check_root"/dist/*.tar.gz)
  case "$kind" in
    core) install_args=("${wheel[0]}");;
    jobs) install_args=("${wheel[0]}[jobs]");;
    sdist) install_args=("${sdist[0]}[jobs]");;
    editable) install_args=(-e "$source_root[jobs]");;
  esac
  "$env_root/bin/python" -m pip install "${install_args[@]}"
  "$env_root/bin/python" -m pip check
  (
    cd "$check_root/driver"
    "$env_root/bin/python" smoke.py --kind "$kind" --output "$check_root/result-$kind" --source-root "$source_root"
  )
done
