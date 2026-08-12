#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
exec "$ROOT/scripts/workflows/run_opt_freq_sp.sh" \
  --mode opt \
  --structure "$ROOT/examples/molecular/orca/shared/molecule.xyz" \
  --config "$ROOT/examples/molecular/orca/shared/workflow.json" \
  --output "$ROOT/runs/example-opt" "$@"
