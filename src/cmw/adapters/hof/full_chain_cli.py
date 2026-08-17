"""Generate a non-running HOF OPT-to-IGMH preflight and queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .full_chain import generate_hof_full_chain


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True)
    parser.add_argument("--hof-root", type=Path, required=True)
    parser.add_argument("--cmw-root", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--orca-exe", type=Path, required=True)
    parser.add_argument("--converter", type=Path, required=True)
    parser.add_argument("--multiwfn-exe", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--python-library-source", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = generate_hof_full_chain(
            system_id=args.system, hof_root=args.hof_root, cmw_root=args.cmw_root,
            preflight_directory=args.preflight, queue_directory=args.queue,
            python_bin=args.python, orca_executable=args.orca_exe,
            converter=args.converter, multiwfn_executable=args.multiwfn_exe,
            multiwfn_settings=args.settings,
            python_library_source=args.python_library_source,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "FAILED", "reason": str(exc)}, sort_keys=True))
        return 64
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
