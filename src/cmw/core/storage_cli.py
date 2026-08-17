"""Validate immediately allocatable filesystem capacity before a launch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .storage import StorageCapacityError, check_storage_capacity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--minimum-free-gb", type=float, required=True)
    args = parser.parse_args()
    try:
        result = check_storage_capacity(
            args.path, minimum_free_gb=args.minimum_free_gb
        )
    except (OSError, ValueError, StorageCapacityError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, sort_keys=True))
        return 74
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
