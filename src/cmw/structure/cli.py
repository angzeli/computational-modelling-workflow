"""Convert a molecular XYZ file to a validated VASP POSCAR vacuum cell."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .conversion import CELL_MODES, inspect_xyz_to_poscar, xyz_to_poscar


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_xyz", type=Path)
    parser.add_argument("--output", type=Path, default=Path("POSCAR"))
    parser.add_argument("--vacuum", type=float, default=10.0, metavar="ANGSTROM")
    parser.add_argument("--cell-mode", choices=CELL_MODES, default="auto")
    parser.add_argument(
        "--net-charge",
        type=int,
        default=0,
        help="formal total molecular charge supplied by the caller (default: 0)",
    )
    parser.add_argument("--species-order", nargs="+", metavar="ELEMENT")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and display the proposed conversion without writing files",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.dry_run:
            result = inspect_xyz_to_poscar(
                args.input_xyz,
                vacuum=args.vacuum,
                cell_mode=args.cell_mode,
                net_charge=args.net_charge,
                species_order=args.species_order,
            )
        else:
            result = xyz_to_poscar(
                args.input_xyz,
                args.output,
                vacuum=args.vacuum,
                cell_mode=args.cell_mode,
                net_charge=args.net_charge,
                species_order=args.species_order,
            )
        _print(result)
        return 0
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError) as exc:
        _print({"error": type(exc).__name__, "reason": str(exc)})
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
