#!/bin/bash
# Own the payload's descriptors, foreground wait, signals and pinned group leader.
# The guardian gives GO only after persisting group ownership, and RELEASE only
# after all payload members have ended. Never exec away this group leader.
set -u
python_bin=$1
cmw_import_path=$2
receipt=$3
stdout_path=$4
stderr_path=$5
shift 5
trap '' HUP
trap ':' TERM INT
IFS= read -r instruction || exit 125
[[ "$instruction" == GO ]] || exit 125
"$@" </dev/null >>"$stdout_path" 2>>"$stderr_path" &
payload_pid=$!
wait "$payload_pid"
code=$?
# TERM may interrupt wait while the payload is still alive.
while kill -0 "$payload_pid" 2>/dev/null; do
    wait "$payload_pid"
    code=$?
done
/usr/bin/env -u PYTHONHOME PYTHONPATH="$cmw_import_path" "$python_bin" -m cmw.jobs.runtime receipt "$receipt" "$code" || exit 125
# Remain the live group leader until the guardian has drained descendants.
while ! IFS= read -r instruction; do
    # A dead guardian is not permission to release this ownership anchor.
    /bin/sleep 1
done
[[ "$instruction" == RELEASE ]] || exit 125
exit 0
