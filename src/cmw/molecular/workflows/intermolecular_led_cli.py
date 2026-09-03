"""Plan, finalize, and revalidate generic three-calculation intermolecular LED."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence

from cmw.core.artifacts import (
    Artifact,
    CalculationArtifact,
    DimerEnergyArtifact,
    LEDArtifact,
    LEDFragmentReferenceArtifact,
    StructureArtifact,
    artifact_from_dict,
)
from cmw.core.provenance import atomic_write_json, read_json
from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.stacking.hole_electron import FragmentDefinition

from .intermolecular_led import (
    LEDFragmentElectronicState,
    build_intermolecular_led_plan,
    check_intermolecular_led_reuse,
    finalize_intermolecular_led_artifact,
)


def _artifact(path: str | Path) -> Artifact:
    value = read_json(Path(path))
    if isinstance(value.get("scientific_artifact"), Mapping):
        value = dict(value["scientific_artifact"])
    return artifact_from_dict(value)


def _list(path: str | Path, *, name: str) -> list[Mapping[str, object]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"{name} must contain a JSON list of objects")
    return [dict(item) for item in value]


def _fragments(path: str | Path) -> tuple[FragmentDefinition, ...]:
    return tuple(
        FragmentDefinition(
            str(item["fragment_id"]),
            tuple(int(index) for index in item["atom_indices"]),
        )
        for item in _list(path, name="fragments")
    )


def _fragment_states(path: str | Path) -> tuple[LEDFragmentElectronicState, ...]:
    return tuple(
        LEDFragmentElectronicState(
            str(item["fragment_id"]),
            int(item["charge"]),
            int(item["multiplicity"]),
        )
        for item in _list(path, name="fragment states")
    )


def _orca_spec(path: str | Path) -> OrcaStageSpec:
    value = read_json(Path(path))
    return OrcaStageSpec(
        value.get("stage_type", "SP"),
        str(value["keywords"]),
        tuple(str(item) for item in value.get("blocks", ())),
        protocol=dict(value.get("protocol", {})),
    )


def _structure(path: str | Path) -> StructureArtifact:
    artifact = _artifact(path)
    if not isinstance(artifact, StructureArtifact):
        raise TypeError("structure manifest must contain a StructureArtifact")
    return artifact


def _calculation(path: str | Path) -> CalculationArtifact:
    artifact = _artifact(path)
    if not isinstance(artifact, CalculationArtifact):
        raise TypeError("result manifest must contain a CalculationArtifact")
    return artifact


def _plan(args: argparse.Namespace) -> int:
    plan = build_intermolecular_led_plan(
        _structure(args.structure_artifact),
        _fragments(args.fragments),
        _fragment_states(args.fragment_states),
        _orca_spec(args.orca_spec),
        graph_id=args.graph_id,
    )
    atomic_write_json(Path(args.output), plan.execution_plan.to_dict())
    if args.manifest is not None:
        atomic_write_json(Path(args.manifest), plan.to_dict())
    print(json.dumps({
        "status": "PLANNED",
        "execution_plan_id": plan.execution_plan.execution_plan_id,
        "output": str(Path(args.output)),
        "manifest": str(Path(args.manifest)) if args.manifest is not None else None,
        "calculation_nodes": list(plan.metadata["calculation_nodes"]),
        "aggregation_node": plan.metadata["aggregation_node"],
    }, sort_keys=True))
    return 0


def _parents(args: argparse.Namespace) -> tuple[
    DimerEnergyArtifact,
    tuple[LEDFragmentReferenceArtifact, LEDFragmentReferenceArtifact],
]:
    dimer = _calculation(args.dimer_result)
    references = tuple(_calculation(path) for path in args.fragment_result)
    if not isinstance(dimer, DimerEnergyArtifact):
        raise TypeError("dimer result must contain a DimerEnergyArtifact")
    if len(references) != 2 or any(
        not isinstance(item, LEDFragmentReferenceArtifact) for item in references
    ):
        raise TypeError(
            "exactly two fragment results containing LEDFragmentReferenceArtifact are required"
        )
    return dimer, (references[0], references[1])


def _finalize(args: argparse.Namespace) -> int:
    dimer, references = _parents(args)
    artifact = finalize_intermolecular_led_artifact(
        _structure(args.structure_artifact),
        _fragments(args.fragments),
        dimer,
        references,
        producing_calculation=args.producing_calculation,
    )
    atomic_write_json(Path(args.output), artifact.to_dict())
    print(json.dumps({
        "status": "FINALIZED",
        "artifact_id": artifact.artifact_id,
        "output": str(Path(args.output)),
        "reconstructed_total_kj_mol": artifact.led_result["components"][
            "reconstructed_total_kj_mol"
        ],
        "reconstruction_residual_hartree": artifact.led_result[
            "reconstruction_residual_hartree"
        ],
    }, sort_keys=True))
    return 0


def _reuse(args: argparse.Namespace) -> int:
    artifact = _artifact(args.led_artifact)
    if not isinstance(artifact, LEDArtifact):
        raise TypeError("LED manifest must contain a LEDArtifact")
    dimer, references = _parents(args)
    result = check_intermolecular_led_reuse(
        artifact,
        structure=_structure(args.structure_artifact),
        fragments=_fragments(args.fragments),
        parents=(dimer, *references),
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["reuse"] else 1


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--structure-artifact", required=True)
    parser.add_argument("--fragments", required=True)


def _result_arguments(parser: argparse.ArgumentParser) -> None:
    _source_arguments(parser)
    parser.add_argument("--dimer-result", required=True)
    parser.add_argument("--fragment-result", action="append", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    _source_arguments(plan)
    plan.add_argument("--fragment-states", required=True)
    plan.add_argument("--orca-spec", required=True)
    plan.add_argument("--graph-id", default="intermolecular_led")
    plan.add_argument("--output", required=True)
    plan.add_argument("--manifest")
    plan.set_defaults(handler=_plan)

    finalize = sub.add_parser("finalize")
    _result_arguments(finalize)
    finalize.add_argument("--producing-calculation", default="intermolecular_led_assembler")
    finalize.add_argument("--output", required=True)
    finalize.set_defaults(handler=_finalize)

    reuse = sub.add_parser("reuse")
    _result_arguments(reuse)
    reuse.add_argument("--led-artifact", required=True)
    reuse.set_defaults(handler=_reuse)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
