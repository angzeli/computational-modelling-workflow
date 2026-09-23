"""Publish an ordered CIF import with its provenance kept only in Scratch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cmw.core.preparation_publication import (
    content_identity, publication_plan, publish_preparation,
)
from cmw.core.provenance import stable_hash


def import_cif(
    input_cif: str | Path,
    output_poscar: str | Path,
    *,
    scratch_root: str | Path,
    record_directory: str | Path,
    scratch_mount: str | Path | None = None,
    block: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Import one ordered periodic structure without changing its cell basis.

    The source is read once; the same validated POSCAR bytes and source identity
    are used for preview and publication. Relative record directories use the
    explicit Scratch root. Both destinations must be new and their parents
    must already exist.
    """
    from .cif import read_ordered_cif

    source = Path(input_cif).expanduser()
    output = Path(output_poscar).expanduser()
    plan = publication_plan(
        output=output, scratch_root=scratch_root,
        record_directory=record_directory, sources=(source,), kind="file",
        scratch_mount=scratch_mount,
    )
    raw = source.read_bytes()
    result = read_ordered_cif(raw, block=block)
    source_identity = content_identity(raw)
    output_identity = content_identity(result.poscar)
    structure = result.structure.to_dict()
    record: dict[str, object] = {
        "schema_version": 1,
        "record_kind": "periodic-structure-import",
        "downstream_use": "periodic_structure_import",
        "preparation_content_id": stable_hash({
            "record_kind": "periodic-structure-import", "renderer_version": 1,
            "source_sha256": source_identity["sha256"],
            "selected_block": result.structure.selected_block,
            "structure_id": result.structure.structure_id,
            "poscar_sha256": output_identity["sha256"],
        }),
        "scientific_target_id": None,
        "sources": [{
            "role": "ordered_cif", "recorded_path": str(input_cif),
            "resolved_path": str(source.resolve()), **source_identity,
        }],
        "requested_settings": {"block": block},
        "resolved_settings": {"block": result.structure.selected_block},
        "structure": structure,
        "conversion": result.metadata,
        "prepared_inputs": {output.name: output_identity},
        "validation": {
            "status": "valid",
            "scope": "ordered fully occupied periodic CIF import and POSCAR read-back",
            "unassessed": ["electronic settings", "physical model adequacy", "scientific convergence"],
        },
        "effective_inputs": {"status": "unobserved"},
        "renderer": {"name": "cmw.structure.cif", "version": 1},
    }
    if dry_run:
        return {**record, "dry_run": True, "publication_plan": plan}
    return publish_preparation(files={output.name: result.poscar}, record=record, plan=plan)


def _handle(args: argparse.Namespace) -> int:
    try:
        result = import_cif(
            args.input, args.output, scratch_root=args.scratch_root,
            record_directory=args.record_directory, scratch_mount=args.scratch_mount,
            block=args.block, dry_run=args.dry_run,
        )
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError) as exc:
        result = {"code": getattr(exc, "code", "CIF_IMPORT_INVALID"),
                  "reason": str(exc), "incomplete": getattr(exc, "incomplete", False)}
        if getattr(exc, "details", None) is not None:
            result["details"] = exc.details
        if getattr(exc, "record_path", None) is not None:
            result["record_path"] = str(exc.record_path)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"{result['code']}: {result['reason']}")
            blocks = result.get("details", {}).get("available_blocks")
            if blocks:
                print("Available blocks: " + ", ".join(item["name"] for item in blocks))
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        mode = "Preview" if args.dry_run else "Prepared"
        print(f"{mode}: ordered periodic CIF block {result['resolved_settings']['block']} imported.")
        print(f"POSCAR: {Path(args.output).expanduser().resolve()}")
        record = (result.get("publication", {}).get("record_path")
                  or result.get("publication_plan", {}).get("record_path"))
        print(f"Scratch preparation record: {record}")
    return 0


def register(operations: argparse._SubParsersAction) -> None:
    parser = operations.add_parser(
        "import-cif", help="import an ordered fully occupied periodic CIF structure",
        description="Import one explicit ordered periodic CIF block without changing its cell basis. "
                    "Creates only a POSCAR; source, symmetry and atom provenance go to Scratch.",
    )
    parser.add_argument("--input", type=Path, required=True, help="source ordered periodic CIF")
    parser.add_argument("--output", type=Path, required=True, help="new POSCAR file; parent must exist")
    parser.add_argument("--scratch-root", type=Path, required=True, help="existing explicit Scratch root")
    parser.add_argument("--record-directory", type=Path, required=True,
                        help="new record directory; relative paths use Scratch root; parent must exist")
    parser.add_argument("--scratch-mount", type=Path, help="optional expected mounted volume for Scratch")
    parser.add_argument("--block", help="exact CIF block name; required when multiple structural blocks are present")
    parser.add_argument("--dry-run", action="store_true", help="parse, expand and validate in memory; write no files")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_handle)
