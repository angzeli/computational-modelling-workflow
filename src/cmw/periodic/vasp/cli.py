"""Argparse presentation for local VASP POTCAR preparation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .potentials import build_potcar, inspect_potcar, list_potentials, read_poscar_blocks


def _check_inputs(args: argparse.Namespace) -> int:
    from .inputs import check_inputs

    result = check_inputs(args.directory, record_path=args.preparation_record)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Input check: {result['status']} (bounded preparation scope)")
        for item in result["findings"]:
            print(f"{item['severity']}: {item['code']}: {item['message']}")
    return result["exit_code"]


def _identity_options(args: argparse.Namespace) -> dict[str, object]:
    requirements = None
    if args.requirements is not None:
        requirements = json.loads(args.requirements.read_text(encoding="utf-8"))
    return {"requirements": requirements, "library_release": args.library_release,
            "strict": args.strict_identity}


def _build(args: argparse.Namespace) -> int:
    result = build_potcar(args.poscar, args.output, root=args.potcar_root, pots=args.pot,
                          force=args.force, dry_run=args.dry_run, **_identity_options(args))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("POTCAR resolution")
    print("POSCAR species: " + " ".join(result["species"]))
    for element, label in zip(result["species"], result["potentials"]):
        marker = "  [CLI override]" if element in result["overrides"] else ""
        print(f"  {element} -> {label}{marker}")
    print(f"POTPAW root: {result['root']}\nOutput: {result['output']}")
    print(f"Validation: species order PASS; datasets {result['datasets']}; "
          + ("preview only (no files written)" if args.dry_run else "POTCAR written PASS"))
    print(f"Selection identity: {result['selection']['selection_status']}; scientific suitability: unassessed")
    return 0


def _list(args: argparse.Namespace) -> int:
    labels = list_potentials(args.element, args.potcar_root)
    if args.json:
        print(json.dumps({"element": args.element, "potentials": labels}, indent=2))
    else:
        print(f"Available potentials for {args.element}:")
        print("\n".join(f"  {label}" for label in labels) if labels else "  (none)")
    return 0


def _check(args: argparse.Namespace) -> int:
    species, counts = read_poscar_blocks(args.poscar)
    record = inspect_potcar(args.potcar.read_bytes(), species=species, counts=counts,
                             **_identity_options(args))
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0 if record["identity_valid"] else 2
    print(f"POSCAR: {args.poscar}\nPOTCAR: {args.potcar}")
    for dataset in record["datasets"]:
        label = f"  [{dataset['label']}]" if dataset["label"] else ""
        print(f"  {dataset['element']}  PASS{label}")
    identity = "PASS" if record["identity_valid"] else "FAILED"
    print(f"Datasets: {record['dataset_count']}; species order: PASS; requested identity checks: {identity}")
    print("Selection and scientific suitability: unassessed; unestablished fields: "
          + (", ".join(record["unestablished"]) or "none"))
    return 0 if record["identity_valid"] else 2


def register(subcommands: argparse._SubParsersAction) -> None:
    vasp = subcommands.add_parser("vasp", help="prepare local VASP inputs")
    commands = vasp.add_subparsers(dest="vasp_command", required=True)
    checking = commands.add_parser("check-inputs", help="read-only checks of a four-file input bundle")
    checking.add_argument("directory", type=Path)
    checking.add_argument("--preparation-record", "--record", type=Path, help="optional external Scratch preparation.json")
    checking.add_argument("--json", action="store_true")
    checking.set_defaults(handler=_check_inputs)
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
    build.add_argument("--dry-run", action="store_true", help="resolve the exact selected bytes without writing")
    build.set_defaults(handler=_build)
    listing = operations.add_parser("list", help="discover actual local variants", description=description)
    listing.add_argument("element", help="case-sensitive element symbol")
    listing.add_argument("--json", action="store_true", help="print structured output")
    listing.set_defaults(handler=_list)
    for parser in (build, listing):
        parser.add_argument("--potcar-root", type=Path, help="local POTPAW library; overrides CMW_VASP_POTCAR_ROOT")
    check = operations.add_parser("check", help="validate dataset count and chemical species order")
    check.add_argument("poscar", nargs="?", type=Path, default=Path("POSCAR"), help="POSCAR (default: ./POSCAR)")
    check.add_argument("--potcar", type=Path, default=Path("POTCAR"), help="POTCAR to check (default: ./POTCAR)")
    for parser in (build, check):
        parser.add_argument("--json", action="store_true", help="print structured non-payload identity output")
        parser.add_argument("--requirements", type=Path, help="JSON comparability requirements")
        parser.add_argument("--library-release", help="separately caller-declared library release; never inferred from dates")
        parser.add_argument("--strict-identity", action="store_true", help="require parsed variant and family metadata")
    check.set_defaults(handler=_check)
