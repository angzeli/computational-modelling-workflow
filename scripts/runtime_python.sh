#!/usr/bin/env bash
# The caller pins the interpreter. Preserve a virtual environment's symlink path;
# resolving it to the underlying binary would lose the selected environment.
case ${PYTHON_BIN-} in
  /*) ;;
  *) printf 'PYTHON_BIN must name an explicit absolute Python executable; PATH lookup is not allowed\n' >&2; exit 64;;
esac
if [[ ! -f "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  printf 'PYTHON_BIN is not an executable file: %s\n' "$PYTHON_BIN" >&2
  exit 64
fi
export PYTHON_BIN
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONHOME
# Source checkouts bind only their own package. Inherited import paths may belong
# to another Python minor version; installed assets use their pinned environment.
cmw_python_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -f "$cmw_python_root/src/cmw/__init__.py" ]]; then
  export PYTHONPATH="$cmw_python_root/src"
else
  unset PYTHONPATH
fi
unset cmw_python_root
