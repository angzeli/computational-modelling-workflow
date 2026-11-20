#!/bin/bash
# Run one read-only PBS client for a scheduler-backed node (#81, AGENTS §11).
# Shell owns the client's launch, descriptors and output bounds. Python passes
# the enrolled absolute client path and fixed arguments, validates job IDs
# between calls, and interprets the result.
# Usage: pbs_client.sh LIMIT CLIENT [ARG...]
set -u
[[ $# -ge 2 ]] || exit 125
limit=$1
client=$2
shift 2
[[ $limit =~ ^[1-9][0-9]{0,9}$ ]] || exit 125
[[ $client == /* && -f $client && -x $client ]] || exit 125
case ${client##*/} in
    qselect|qstat) ;;
    *) exit 125 ;;
esac
# head stops reading one byte past the limit, so the client gets SIGPIPE and
# an oversized reply is refused by length without ever being buffered whole.
"$client" "$@" </dev/null 2> >(head -c 4096 >&2) | head -c "$((limit + 1))"
exit "${PIPESTATUS[0]}"
