#!/bin/bash
# Ask the user's own OpenSSH master whether it is alive (#81 S03, AGENTS §3).
# Shell owns the check's launch and descriptors. Python first decides whether
# the enrolled socket is eligible (a private directory and the user's own
# socket), bounds this helper's lifetime and maps the result to AUTH_REQUIRED.
# `ssh -O check` talks only to the local control socket: it never opens,
# authenticates or stops a master, and never connects to the node.
# Usage: session_check.sh SSH CONTROL_PATH ALIAS
# Exit: 0 master alive; 1 no live master; 125 arguments refused.
set -u
[[ $# -eq 3 ]] || exit 125
ssh=$1
socket=$2
alias=$3
[[ $ssh == /* && -f $ssh && -x $ssh ]] || exit 125
[[ $socket == /* ]] || exit 125
[[ $alias =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || exit 125
"$ssh" -o BatchMode=yes -o "ControlPath=$socket" -O check -- "$alias" </dev/null >/dev/null 2>&1 || exit 1
exit 0
