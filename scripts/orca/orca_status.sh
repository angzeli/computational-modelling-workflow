#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd -P)
source "$REPO_ROOT/scripts/runtime_python.sh"

exec "$PYTHON_BIN" -m cmw.molecular.orca.cli status "$@"
