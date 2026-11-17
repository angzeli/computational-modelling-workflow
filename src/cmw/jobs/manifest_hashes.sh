#!/bin/bash
# Detached launch of the restart-output hasher: this script owns its environment,
# log descriptors and backgrounding. Python (cmw.jobs.manifest_hashing) owns the
# digests, stability rule and the state.json write. Prints one JSON line.
set -u
python_bin=$1
cmw_import_path=$2
state_root=$3
job_id=$4
log=$5
(umask 077; : >>"$log") || { printf '{"status":"REFUSED","reason":"hasher log is not writable"}\n'; exit 73; }
trap '' HUP
PYTHONPATH="$cmw_import_path" PYTHONDONTWRITEBYTECODE=1 \
    "$python_bin" -B -m cmw.jobs.manifest_hashing "$state_root" "$job_id" </dev/null >>"$log" 2>&1 &
printf '{"status":"LAUNCHED","pid":%d}\n' "$!"
