"""Argparse presentation for local VASP POTCAR preparation."""

from __future__ import annotations

import argparse
from pathlib import Path

from .potentials import build_potcar, check_potcar, list_potentials


def _build(args: argparse.Namespace) -> int:
    result = build_potcar(args.poscar, args.output, root=args.potcar_root, pots=args.pot, force=args.force)
    print("POTCAR resolution")
    print("POSCAR species: " + " ".join(result["species"]))
    for element, label in zip(result["species"], result["potentials"]):
        marker = "  [CLI override]" if element in result["overrides"] else ""
        print(f"  {element} -> {label}{marker}")
    print(f"POTPAW root: {result['root']}\nOutput: {result['output']}")
    print(f"Validation: species order PASS; datasets {result['datasets']}; POTCAR written PASS")
    return 0


def _list(args: argparse.Namespace) -> int:
    labels = list_potentials(args.element, args.potcar_root)
    print(f"Available potentials for {args.element}:")
    print("\n".join(f"  {label}" for label in labels) if labels else "  (none)")
    return 0


def _check(args: argparse.Namespace) -> int:
    datasets = check_potcar(args.poscar, args.potcar)
    print(f"POSCAR: {args.poscar}\nPOTCAR: {args.potcar}")
    for dataset in datasets:
        label = f"  [{dataset.label}]" if dataset.label else ""
        print(f"  {dataset.element}  PASS{label}")
    print(f"Datasets: {len(datasets)}; species order: PASS; overall: PASS")
    return 0


def register(subcommands: argparse._SubParsersAction) -> None:
    vasp = subcommands.add_parser("vasp", help="prepare local VASP inputs")
    commands = vasp.add_subparsers(dest="vasp_command", required=True)
    description = (
        "Use local licensed POTCAR files; CMW does not provide potentials. "
        "Build defaults to the suffix-free directory matching each POSCAR species. "
        "Repeat --pot ELEMENT=VARIANT for explicit one-off overrides. "
        "Supply --potcar-root PATH or set CMW_VASP_POTCAR_ROOT; CLI takes precedence."
    )
    potcar = commands.add_parser("potcar", help="build, list or check local POTCAR datasets", description=description)
    operations = potcar.add_subparsers(dest="potcar_command", required=True)
    build = operations.add_parser("build", help="assemble in POSCAR species order", description=description)
    build.add_argument("poscar", nargs="?", type=Path, default=Path("POSCAR"), help="VASP 5/6 POSCAR (default: ./POSCAR)")
    build.add_argument("--output", type=Path, default=Path("POTCAR"), help="destination (default: ./POTCAR)")
    build.add_argument("--pot", action="append", default=[], metavar="ELEMENT=VARIANT", help="override one species; may be repeated")
    build.add_argument("--force", action="store_true", help="explicitly replace an existing output")
    build.set_defaults(handler=_build)
    listing = operations.add_parser("list", help="discover actual local variants", description=description)
    listing.add_argument("element", help="case-sensitive element symbol")
    listing.set_defaults(handler=_list)
    for parser in (build, listing):
        parser.add_argument("--potcar-root", type=Path, help="local POTPAW library; overrides CMW_VASP_POTCAR_ROOT")
    check = operations.add_parser("check", help="validate dataset count and chemical species order")
    check.add_argument("poscar", nargs="?", type=Path, default=Path("POSCAR"), help="POSCAR (default: ./POSCAR)")
    check.add_argument("--potcar", type=Path, default=Path("POTCAR"), help="POTCAR to check (default: ./POTCAR)")
    check.set_defaults(handler=_check)
