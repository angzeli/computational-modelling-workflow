"""Generate, verify, or authorize one sequential HOF batch queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .batch_queue import (
    authorize_hof_batch_queue,
    generate_hof_batch_queue,
    verify_hof_batch_queue,
)


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--queue", action="append", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--python", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--batch", type=Path, required=True)
    verify.add_argument("--authorization", type=Path, required=True)
    verify.add_argument("--require-authorized", action="store_true")
    authorize = sub.add_parser("authorize")
    authorize.add_argument("--batch", type=Path, required=True)
    authorize.add_argument("--authorization", type=Path, required=True)
    authorize.add_argument("--concurrent-scientific-jobs", required=True)
    authorize.add_argument("--conflicting-locks", required=True)
    authorize.add_argument("--package-validation", required=True)
    authorize.add_argument("--storage-validation", required=True)
    authorize.add_argument("--command-validation", required=True)
    args = parser.parse_args()
    try:
        if args.command == "generate":
            result = generate_hof_batch_queue(
                queue_directories=args.queue,
                output_directory=args.output,
                python_bin=args.python,
            )
        elif args.command == "verify":
            result = verify_hof_batch_queue(
                batch_path=args.batch,
                authorization_path=args.authorization,
                require_authorized=args.require_authorized,
            )
        else:
            result = authorize_hof_batch_queue(
                batch_path=args.batch,
                authorization_path=args.authorization,
                concurrent_scientific_jobs=args.concurrent_scientific_jobs,
                conflicting_locks=args.conflicting_locks,
                package_validation=args.package_validation,
                storage_validation=args.storage_validation,
                command_validation=args.command_validation,
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _print({"status": "FAILED", "reason": str(exc)})
        return 64
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
