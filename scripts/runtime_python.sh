#!/usr/bin/env bash
# Source checkouts bind their own package. Wheel assets use the selected installed
# interpreter and must never discover a neighbouring checkout through PYTHONPATH.
export PYTHONDONTWRITEBYTECODE=1
cmw_python_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -f "$cmw_python_root/src/cmw/__init__.py" ]]; then
  export PYTHONPATH="$cmw_python_root/src${PYTHONPATH:+:$PYTHONPATH}"
else
  unset PYTHONPATH
fi
unset cmw_python_root
