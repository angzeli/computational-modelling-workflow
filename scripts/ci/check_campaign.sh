#!/usr/bin/env bash
# Reuse the artifact gate; this script owns only campaign sequencing/isolation.
set -euo pipefail
source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
mode=${1:?usage: check_campaign.sh suite|artifacts|deterministic|soak NEW_EXTERNAL_DIRECTORY}
campaign_root=${2:?A new external evidence directory is required}
python_exe=${PYTHON_BIN:-python3}
case "$mode" in suite|artifacts|deterministic|soak) ;; *) exit 64;; esac
mkdir -m 700 "$campaign_root"
campaign_root=$(cd "$campaign_root" && pwd -P)
case "$campaign_root/" in "$source_root/"*) printf 'Campaign evidence must be outside the checkout\n' >&2; exit 64;; esac
mkdir -m 700 "$campaign_root/tmp"
export TMPDIR="$campaign_root/tmp" PYTHONDONTWRITEBYTECODE=1
export CMW_JOBS_STATE="$campaign_root/unused-default-state"
export CMW_TEST_EVIDENCE_DIR="$campaign_root/lifecycle-evidence"
unset PYTHONHOME PYTHONPATH
case "$mode" in
  suite)
    "$python_exe" -I -B -c 'import cmw,sys;from pathlib import Path;origin=Path(cmw.__file__).resolve();expected=Path(sys.argv[1])/"src";assert origin.is_relative_to(expected),(origin,expected);print("Verified editable suite origin:",origin)' "$source_root"
    (cd "$source_root" && "$python_exe" -B -m unittest discover -s tests)
    ;;
  artifacts)
    CMW_ARTIFACT_OFFLINE=1 CMW_ARTIFACT_QUIET=1 PYTHON_BIN="$python_exe" bash "$source_root/scripts/ci/check_artifacts.sh" "$campaign_root/installed"
    mkdir -p "$campaign_root/focused-driver"
    cp -R "$source_root/tests" "$campaign_root/focused-driver/tests"
    for kind in core jobs; do
      (cd "$campaign_root/focused-driver" && "$campaign_root/installed/env-$kind/bin/python" -I -B -c \
        'import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("tests.campaign.installed_focus",run_name="__main__")' \
        "$campaign_root/focused-driver" "$kind" "$campaign_root/installed-focus-$kind.json")
    done
    ;;
  deterministic|soak)
    artifact_report=${CMW_CAMPAIGN_ARTIFACT_REPORT:?Set the exact final installed-focus-jobs.json report}
    # PYTHON_BIN must select the exact final installed Jobs wheel interpreter.
    # The copied directory contains tests only; there is no production src tree.
    mkdir -p "$campaign_root/driver"
    cp -R "$source_root/tests" "$campaign_root/driver/tests"
    (cd "$campaign_root/driver" && "$python_exe" -I -B -c \
      'import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("tests.campaign.acceptance",run_name="__main__")' \
      "$campaign_root/driver" --mode "$mode" --output "$campaign_root/$mode" --artifact-report "$artifact_report")
    ;;
esac
test ! -e "$CMW_JOBS_STATE"
