"""Closed-shell native HFLD supersystem plus two own-basis RHF references."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from cmw.core.artifacts import CalculationArtifact, StructureArtifact
from cmw.core.plan_materialization import ExecutionPlan
from cmw.core.provenance import stable_hash
from cmw.core.structure_artifacts import structure_artifact_path, validate_structure_artifact
from cmw.core.workflow_graph import CalculationNode, ArtifactRequirement, WorkflowGraph
from cmw.molecular.orca.hfld import CONTRACT, REFERENCE_SEMANTICS, HFLDError
from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.orca.renderer import orca_execution_plan_node
from cmw.molecular.orca.status import StageType
from cmw.molecular.stacking.hole_electron import FragmentDefinition
from cmw.structure.xyz import read_xyz


@dataclass(frozen=True)
class HFLDFragmentState:
    fragment_id: str
    charge: int
    multiplicity: int

    def to_dict(self):
        return {"fragment_id": self.fragment_id, "charge": self.charge, "multiplicity": self.multiplicity}


@dataclass(frozen=True)
class HFLDPlan:
    execution_plan: ExecutionPlan
    partition_hash: str

    def to_dict(self):
        return {"contract": CONTRACT, "reference_semantics": REFERENCE_SEMANTICS,
                "partition_hash": self.partition_hash, "execution_plan": self.execution_plan.to_dict()}


def hfld_spec() -> OrcaStageSpec:
    return OrcaStageSpec(StageType.SP,
        "HFLD def2-TZVP(-f) def2-TZVP/C def2/J RIJCOSX DefGrid3 VeryTightSCF",
        blocks=("%mdci\n  DoLEDHF true\n  TCutPNO 3.33e-7\n  TCutPairs 1e-5\nend",),
        protocol={"method": "HFLD", "basis": "def2-TZVP(-f)",
                  "auxiliary_basis": {"correlation": "def2-TZVP/C", "coulomb": "def2/J"},
                  "reference_approximation": "RIJCOSX", "scf": "VeryTightSCF",
                  "tcut_pno": 3.33e-7, "tcut_pairs": 1e-5, "do_led_hf": True,
                  "led": True, "fragments_required": True, "expected_fragments": 2})


def build_hfld_plan(structure: StructureArtifact, fragments: Sequence[FragmentDefinition],
                    states: Sequence[HFLDFragmentState], spec: OrcaStageSpec, *, graph_id="hfld_led") -> HFLDPlan:
    if not structure.validation.passed or not validate_structure_artifact(structure).passed:
        raise HFLDError("validated source structure required")
    if spec.scientific_identity() != hfld_spec().scientific_identity():
        raise HFLDError("unsupported HFLD* protocol")
    if structure.multiplicity != 1:
        raise HFLDError("this native HFLD contract is closed shell")
    if len(fragments) != 2 or len({f.fragment_id for f in fragments}) != 2:
        raise HFLDError("two distinct physical fragments required")
    flat = [i for f in fragments for i in f.atom_indices]
    if len(flat) != len(set(flat)) or set(flat) != set(range(structure.atom_count)):
        raise HFLDError("fragments must cover every atom exactly once")
    if len(states) != 2 or {s.fragment_id for s in states} != {f.fragment_id for f in fragments}:
        raise HFLDError("fragment electronic states do not match partition")
    if any(type(s.charge) is not int or s.multiplicity != 1 for s in states) or sum(s.charge for s in states) != structure.charge:
        raise HFLDError("closed-shell fragment charges must sum to supersystem charge")
    geometry = read_xyz(structure_artifact_path(structure))
    partition = [f.to_dict() for f in fragments]
    partition_hash = stable_hash(partition)
    common = {"hfld_contract": CONTRACT, "reference_semantics": REFERENCE_SEMANTICS,
              "fragment_partition": partition, "fragment_partition_hash": partition_hash,
              "fragment_states": [s.to_dict() for s in states],
              "source_structure_artifact_id": structure.artifact_id,
              "full_dimer_geometry_hash": structure.geometry_hash,
              "hfld_method_contract": spec.scientific_identity()}
    owners = {i: n for n, f in enumerate(fragments, 1) for i in f.atom_indices}
    specifications = [("led_dimer", OrcaStageSpec(StageType.SP, spec.keywords, spec.blocks,
                       protocol={**spec.protocol, **common, "hfld_role": "supersystem"}),
                       {"atom_indices": list(range(geometry.atom_count)),
                        "atom_labels": [f"{a.element}({owners[i]})" for i,a in enumerate(geometry.atoms)]})]
    for n, f in enumerate(fragments, 1):
        state = next(s for s in states if s.fragment_id == f.fragment_id)
        protocol = {**common, "method": "RHF", "basis": "def2-TZVP(-f)",
                    "auxiliary_basis": {"coulomb": "def2/J"}, "reference_approximation": "RIJCOSX",
                    "scf": "VeryTightSCF", "hfld_role": "reference", "fragment_id": f.fragment_id,
                    "active_atom_indices": list(f.atom_indices), "charge": state.charge, "multiplicity": 1}
        reference = OrcaStageSpec(StageType.SP,
                    "RHF def2-TZVP(-f) def2/J RIJCOSX DefGrid3 VeryTightSCF", protocol=protocol)
        specifications.append((f"led_fragment_reference_{n}", reference,
                    {"atom_indices": list(f.atom_indices), "charge": state.charge,
                     "multiplicity": 1, "allow_electronic_state_override": True}))
    graph_nodes = [CalculationNode("structure", produces=("StructureArtifact",))]
    nodes = []
    for node_id, selected, selection in specifications:
        artifact = CalculationArtifact(producing_calculation=node_id, method=selected.protocol["method"],
                    basis=selected.protocol["basis"], protocol=selected.protocol,
                    parent_artifacts=(structure.artifact_id,))
        graph_nodes.append(CalculationNode(node_id, dependencies=("structure",), operation="orca_single_point",
                    requires=(ArtifactRequirement("StructureArtifact", from_nodes=("structure",)),),
                    produces=("CalculationArtifact",), configuration={"execution_intent": selected.execution_intent.to_dict()}))
        nodes.append(orca_execution_plan_node(node_id, selected, planned_artifacts=(artifact,),
                    geometry_artifact_id=structure.artifact_id, geometry_selection=selection))
    graph = WorkflowGraph(graph_id, tuple(graph_nodes), external_inputs=("StructureArtifact",))
    return HFLDPlan(ExecutionPlan(graph, tuple(nodes), available_artifacts={"structure": (structure,)}), partition_hash)


def finalize_hfld_records(target_paths, metadata_paths):
    """Revalidate three canonical parents and enforce their native reference lineage."""
    from pathlib import Path
    from cmw.core.provenance import read_json
    from cmw.molecular.orca.job import check_reuse
    from cmw.molecular.orca.hfld import parse_hfld, parse_rhf_reference, assemble_hfld

    if len(target_paths) != 3 or len(metadata_paths) != 3:
        raise HFLDError("one supersystem and two reference parents required")
    records = []
    for target, metadata in zip(target_paths, metadata_paths, strict=True):
        verdict = check_reuse(Path(target), Path(metadata))
        if not verdict["reuse"]:
            raise HFLDError(f"canonical parent is not reusable: {verdict['reason']}")
        record = read_json(Path(metadata))
        if not record.get("hfld_validation", {}).get("valid"):
            raise HFLDError("parent lacks native HFLD validation")
        records.append(record)
    protocols = [r["target"]["calculation"]["protocol"] for r in records]
    dimer = protocols[0]
    if dimer.get("hfld_role") != "supersystem" or dimer.get("hfld_contract") != CONTRACT:
        raise HFLDError("first parent must be a native HFLD supersystem")
    for n, reference in enumerate(protocols[1:]):
        for key in ("hfld_contract", "reference_semantics", "fragment_partition_hash",
                    "source_structure_artifact_id", "full_dimer_geometry_hash", "hfld_method_contract", "fragment_states"):
            if reference.get(key) != dimer.get(key):
                raise HFLDError(f"reference lineage differs: {key}")
        fragment = dimer["fragment_partition"][n]
        if (reference.get("hfld_role") != "reference" or reference.get("method") != "RHF"
            or reference.get("fragment_id") != fragment["fragment_id"]
            or reference.get("active_atom_indices") != fragment["atom_indices"]):
            raise HFLDError("wrong RHF fragment reference identity or ordering")
    from cmw.core.execution_layout import resolve_internal_path, resolve_recorded_layout
    def output(index):
        record = records[index]
        layout = resolve_recorded_layout(record["execution_layout"], metadata_path=Path(metadata_paths[index]))
        path = resolve_internal_path(Path(record["artifacts"]["output"]["path"]),
                    layout_record=record["execution_layout"], resolved_layout=layout)
        return path.read_text(encoding="utf-8")
    native = parse_hfld(output(0))
    result = assemble_hfld(native, [parse_rhf_reference(output(i)) for i in (1, 2)])
    return {"contract": CONTRACT, "reference_semantics": REFERENCE_SEMANTICS,
            "units": "Hartree", "values": result,
            "parent_target_ids": [r["target"]["target_id"] for r in records],
            "parent_attempt_ids": [r["attempt"]["attempt_id"] for r in records]}
