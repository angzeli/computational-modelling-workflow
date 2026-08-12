#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=scripts/workflows/multiwfn_cube_workflow.sh
source "$SCRIPT_DIR/multiwfn_cube_workflow.sh"
multiwfn_cube_main ESP "$@"
