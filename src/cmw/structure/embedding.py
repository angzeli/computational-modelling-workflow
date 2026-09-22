"""Canonical molecular embedding with a POSCAR and a separate Scratch record."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from cmw.core.preparation_publication import publication_plan, publish_preparation
from cmw.core.provenance import stable_hash

from .conversion import CELL_MODES, DEFAULT_VACUUM_ANGSTROM, render_xyz_to_poscar


def embed_molecule(
    input_xyz: str | Path,
    output_poscar: str | Path,
    *,
    scratch_root: str | Path,
    record_directory: str | Path,
    scratch_mount: str | Path | None = None,
    vacuum: float | None = None,
    cell_mode: str | None = None,
    net_charge: int | None = None,
    species_order: Iterable[str] | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Embed an ordinary molecular XYZ using the existing conversion algorithm.

    Relative input/output/root/mount paths use the caller's working directory;
    a relative record directory uses the explicit Scratch root. Output and
    record parents must exist. Neither destination may already exist. Preview
    renders and validates in memory and writes nothing.
    """

    source = Path(input_xyz).expanduser()
    output = Path(output_poscar).expanduser()
    plan = publication_plan(
        output=output, scratch_root=Path(scratch_root),
        record_directory=Path(record_directory), sources=(source,), kind="file",
        scratch_mount=Path(scratch_mount) if scratch_mount is not None else None,
    )
    requested = {
        "vacuum_angstrom": DEFAULT_VACUUM_ANGSTROM if vacuum is None else vacuum,
        "cell_mode": "auto" if cell_mode is None else cell_mode,
        "net_charge": 0 if net_charge is None else net_charge,
        "species_order": list(species_order) if species_order is not None else None,
    }
    content, metadata, mapping = render_xyz_to_poscar(
        source, vacuum=requested["vacuum_angstrom"],
        cell_mode=requested["cell_mode"], net_charge=requested["net_charge"],
        species_order=requested["species_order"], ordinary_xyz_only=True,
    )
    resolved = {
        **requested, "cell_mode": metadata["resolved_cell_mode"],
        "species_order": metadata["species_order"],
    }
    origins = {
        "vacuum_angstrom": "caller" if vacuum is not None else "documented molecular embedding default",
        "cell_mode": "caller" if cell_mode is not None else "documented molecular embedding default",
        "net_charge": "caller" if net_charge is not None else "documented molecular embedding default",
        "species_order": "caller" if species_order is not None else "first occurrence in source XYZ",
    }
    prepared_inputs = {output.name: {
        "sha256": metadata["output_sha256"], "size_bytes": len(content),
    }}
    record: dict[str, object] = {
        "record_kind": "molecular-embedding",
        "preparation_content_id": stable_hash({
            "record_kind": "molecular-embedding", "renderer_version": 1,
            "source_sha256": metadata["input_sha256"],
            "requested_settings": requested, "poscar_sha256": metadata["output_sha256"],
        }),
        "scientific_target_id": None,
        "sources": [{
            "role": "molecular_xyz", "recorded_path": str(input_xyz),
            "resolved_path": str(source.resolve()),
            "sha256": metadata["input_sha256"], "size_bytes": metadata["input_size_bytes"],
        }],
        "requested_settings": requested,
        "resolved_settings": resolved,
        "settings_origins": origins,
        "conversion": metadata,
        "mapping": mapping,
        "prepared_inputs": prepared_inputs,
        "validation": {
            **metadata["validation"],
            "scope": "ordinary molecular XYZ embedding and POSCAR read-back",
            "unassessed": ["isolated-model adequacy", "electronic settings", "scientific convergence"],
        },
        "effective_inputs": {"status": "unobserved"},
        "renderer": {"name": "cmw.structure.conversion", "version": 1,
                     "ase_version": metadata["ase_version"]},
    }
    if dry_run:
        return {**record, "dry_run": True, "publication_plan": plan}
    return publish_preparation(files={output.name: content}, record=record, plan=plan)


def _handle(args: argparse.Namespace) -> int:
    try:
        result = embed_molecule(
            args.input, args.output, scratch_root=args.scratch_root,
            record_directory=args.record_directory, scratch_mount=args.scratch_mount,
            vacuum=args.vacuum, cell_mode=args.cell_mode, net_charge=args.net_charge,
            species_order=args.species_order, dry_run=args.dry_run,
        )
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError) as exc:
        result = {"code": getattr(exc, "code", "MOLECULAR_EMBEDDING_INVALID"),
                  "reason": str(exc), "incomplete": getattr(exc, "incomplete", False)}
        if getattr(exc, "record_path", None) is not None:
            result["record_path"] = str(exc.record_path)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"{result['code']}: {result['reason']}")
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        mode = "Preview" if args.dry_run else "Prepared"
        print(f"{mode}: molecular XYZ embedded in a centered periodic cell.")
        print(f"POSCAR: {Path(args.output).expanduser().resolve()}")
        record = (result.get("publication", {}).get("record_path")
                  or result.get("publication_plan", {}).get("record_path"))
        print(f"Scratch preparation record: {record}")
        for warning in result["conversion"]["warnings"]:
            print(f"Warning: {warning}")
    return 0


def register(subcommands: argparse._SubParsersAction) -> None:
    structure = subcommands.add_parser("structure", help="explicit structure preparation")
    operations = structure.add_subparsers(dest="structure_operation", required=True)
    parser = operations.add_parser(
        "embed-molecule", help="center and embed ordinary molecular XYZ in a periodic vacuum cell",
        description="Molecular embedding: creates a centered periodic vacuum cell and groups species. "
                    "This is not CIF or periodic/extxyz import. Metadata and mapping go only to Scratch.",
    )
    parser.add_argument("--input", type=Path, required=True, help="ordinary molecular XYZ; Cartesian ångström")
    parser.add_argument("--output", type=Path, required=True, help="new POSCAR file; parent must exist")
    parser.add_argument("--scratch-root", type=Path, required=True, help="existing explicit Scratch root")
    parser.add_argument("--record-directory", type=Path, required=True,
                        help="new record directory; relative paths use Scratch root; parent must exist")
    parser.add_argument("--scratch-mount", type=Path, help="optional expected mounted volume for Scratch")
    parser.add_argument("--vacuum", type=float, metavar="ANGSTROM", help="minimum per-face clearance (default: 10)")
    parser.add_argument("--cell-mode", choices=CELL_MODES, help="auto: neutral orthorhombic, charged cubic (default: auto)")
    parser.add_argument("--net-charge", type=int, help="formal molecular charge supplied by caller (default: 0)")
    parser.add_argument("--species-order", nargs="+", metavar="ELEMENT")
    parser.add_argument("--dry-run", action="store_true", help="render/read back in memory; write no files")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_handle)
