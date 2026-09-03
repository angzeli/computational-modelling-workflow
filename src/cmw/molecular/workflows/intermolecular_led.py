"""Generic fixed-geometry intermolecular ORCA LED workflow and artifact finalizer."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Sequence

from cmw.core.artifacts import (
    Artifact,
    ArtifactValidation,
    CalculationArtifact,
    DimerEnergyArtifact,
    LEDArtifact,
    LEDFragmentReferenceArtifact,
    StructureArtifact,
    ValidationStatus,
    validate_artifact_compatibility,
)
from cmw.core.plan_materialization import ExecutionPlan
from cmw.core.provenance import file_hash, stable_hash
from cmw.core.structure_artifacts import (
    structure_artifact_path,
    validate_structure_artifact,
)
from cmw.core.workflow_graph import (
    AggregationNode,
    ArtifactRequirement,
    CalculationNode,
    WorkflowGraph,
)
from cmw.molecular.orca.input import OrcaStageSpec
from cmw.molecular.orca.led import (
    DEFAULT_LED_RECONSTRUCTION_TOLERANCE_HARTREE,
    LEDReferenceSemantics,
    IntermolecularLEDResult,
    OrcaLEDError,
    assemble_interfragment_led,
    parse_orca_interfragment_led_file,
    parse_orca_led_fragment_reference_file,
)
from cmw.molecular.orca.renderer import orca_execution_plan_node
from cmw.molecular.orca.status import StageType
from cmw.molecular.stacking.hole_electron import FragmentDefinition
from cmw.structure.xyz import read_xyz


INTERMOLECULAR_LED_CONTRACT = "intermolecular_six_component_v1"


class IntermolecularLEDWorkflowError(ValueError):
    """Raised when a three-calculation LED contract is incomplete."""

    code = "FAILED_INTERMOLECULAR_LED_WORKFLOW"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True)
class LEDFragmentElectronicState:
    fragment_id: str
    charge: int
    multiplicity: int

    def __post_init__(self) -> None:
        fragment_id = str(self.fragment_id).strip()
        if not fragment_id:
            raise IntermolecularLEDWorkflowError("fragment state requires an identifier")
        if isinstance(self.charge, bool) or not isinstance(self.charge, int):
            raise IntermolecularLEDWorkflowError("fragment charge must be an integer")
        if (
            isinstance(self.multiplicity, bool)
            or not isinstance(self.multiplicity, int)
            or self.multiplicity < 1
        ):
            raise IntermolecularLEDWorkflowError(
                "fragment multiplicity must be a positive integer"
            )
        object.__setattr__(self, "fragment_id", fragment_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "fragment_id": self.fragment_id,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
        }


def _validated_partition(
    structure: StructureArtifact,
    fragments: Sequence[FragmentDefinition],
) -> tuple[FragmentDefinition, FragmentDefinition]:
    selected = tuple(fragments)
    if len(selected) != 2:
        raise IntermolecularLEDWorkflowError(
            "standard intermolecular LED requires exactly two fragments"
        )
    if len({item.fragment_id for item in selected}) != 2:
        raise IntermolecularLEDWorkflowError("fragment identifiers must be unique")
    if structure.atom_count is None:
        raise IntermolecularLEDWorkflowError("structure atom count is required")
    atom_sets = [set(item.atom_indices) for item in selected]
    if atom_sets[0].intersection(atom_sets[1]):
        raise IntermolecularLEDWorkflowError("fragment definitions overlap")
    if atom_sets[0].union(atom_sets[1]) != set(range(structure.atom_count)):
        raise IntermolecularLEDWorkflowError(
            "fragment definitions must cover every structure atom"
        )
    return selected[0], selected[1]


def fragment_partition_hash(fragments: Sequence[FragmentDefinition]) -> str:
    return stable_hash([item.to_dict() for item in fragments])


def _base_keywords(spec: OrcaStageSpec) -> str:
    tokens = [token for token in spec.keywords.split() if token.casefold() != "led"]
    return " ".join(tokens)


def _base_protocol(protocol: Mapping[str, object]) -> dict[str, object]:
    excluded = {
        "active_atom_indices",
        "expected_fragments",
        "fragment_id",
        "fragment_identity_hash",
        "fragment_partition",
        "fragment_partition_hash",
        "fragments_required",
        "full_dimer_geometry_hash",
        "ghost_atom_indices",
        "led",
        "led_contract",
        "led_method_contract",
        "led_method_contract_hash",
        "led_role",
        "reference_semantics",
        "source_structure_artifact_id",
    }
    return {key: value for key, value in protocol.items() if key not in excluded}


def _method_contract(spec: OrcaStageSpec) -> dict[str, object]:
    return {
        "adapter": "orca",
        "stage_type": "SP",
        "keywords_without_led": _base_keywords(spec),
        "blocks": [
            line.rstrip()
            for block in spec.blocks
            for line in block.strip().splitlines()
            if line.strip()
        ],
        "protocol": _base_protocol(spec.protocol),
    }


def led_fragment_reference_identity(
    *,
    structure: StructureArtifact,
    fragment: FragmentDefinition,
    state: LEDFragmentElectronicState,
    ghost_atom_indices: Sequence[int],
    method_contract: Mapping[str, object],
    partition_hash: str,
) -> str:
    """Hash every scientific input controlling one LED fragment reference."""

    return stable_hash(
        {
            "reference_semantics": (
                LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
            ),
            "source_structure_artifact_id": structure.artifact_id,
            "full_dimer_geometry_hash": structure.geometry_hash,
            "fragment_partition_hash": partition_hash,
            "fragment": fragment.to_dict(),
            "fragment_state": state.to_dict(),
            "ghost_atom_indices": list(ghost_atom_indices),
            "method_contract": dict(method_contract),
        }
    )


@dataclass(frozen=True)
class IntermolecularLEDPlan:
    """Three ORCA calculations plus a non-executing numerical aggregation node."""

    structure_artifact: StructureArtifact
    fragments: tuple[FragmentDefinition, FragmentDefinition]
    fragment_states: tuple[LEDFragmentElectronicState, LEDFragmentElectronicState]
    graph: WorkflowGraph
    execution_plan: ExecutionPlan
    artifact_templates: Mapping[str, tuple[Artifact, ...]]
    partition_hash: str
    method_contract_hash: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_templates", dict(self.artifact_templates))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "workflow": "intermolecular_led",
            "reference_semantics": (
                LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
            ),
            "structure_artifact": self.structure_artifact.to_dict(),
            "fragments": [item.to_dict() for item in self.fragments],
            "fragment_states": [item.to_dict() for item in self.fragment_states],
            "fragment_partition_hash": self.partition_hash,
            "led_method_contract_hash": self.method_contract_hash,
            "workflow_graph": self.graph.to_dict(),
            "execution_plan": self.execution_plan.to_dict(),
            "artifact_templates": {
                node_id: [item.to_dict() for item in artifacts]
                for node_id, artifacts in self.artifact_templates.items()
            },
            "metadata": dict(self.metadata),
        }


def build_intermolecular_led_plan(
    structure: StructureArtifact,
    fragments: Sequence[FragmentDefinition],
    fragment_states: Sequence[LEDFragmentElectronicState],
    dimer_spec: OrcaStageSpec,
    *,
    graph_id: str = "intermolecular_led",
) -> IntermolecularLEDPlan:
    """Plan a fixed-geometry three-calculation ORCA LED workflow."""

    structure_validation = validate_structure_artifact(structure)
    if not structure.validation.passed or not structure_validation.passed:
        raise IntermolecularLEDWorkflowError(
            "source StructureArtifact has not passed validation"
        )
    if not structure.geometry_hash or structure.charge is None or structure.multiplicity is None:
        raise IntermolecularLEDWorkflowError(
            "source structure requires geometry hash, charge, and multiplicity"
        )
    selected_fragments = _validated_partition(structure, fragments)
    states = tuple(fragment_states)
    if len(states) != 2 or {item.fragment_id for item in states} != {
        item.fragment_id for item in selected_fragments
    }:
        raise IntermolecularLEDWorkflowError(
            "fragment electronic states must match both fragment definitions"
        )
    states_by_id = {item.fragment_id: item for item in states}
    ordered_states = tuple(states_by_id[item.fragment_id] for item in selected_fragments)
    if dimer_spec.stage_type is not StageType.SP:
        raise IntermolecularLEDWorkflowError("intermolecular LED requires an SP stage")
    if "dlpno" not in dimer_spec.keywords.casefold() or "led" not in {
        token.casefold() for token in dimer_spec.keywords.split()
    }:
        raise IntermolecularLEDWorkflowError(
            "dimer specification requires DLPNO and LED keywords"
        )
    if dimer_spec.protocol.get("counterpoise") is True:
        raise IntermolecularLEDWorkflowError(
            "intermolecular LED planning does not accept a counterpoise workflow flag"
        )
    method = dimer_spec.protocol.get("method")
    basis = dimer_spec.protocol.get("basis")
    if not isinstance(method, str) or not method or not isinstance(basis, str) or not basis:
        raise IntermolecularLEDWorkflowError(
            "dimer specification requires explicit method and basis protocol metadata"
        )
    source_path = structure_artifact_path(structure)
    if source_path is None:
        raise IntermolecularLEDWorkflowError("source structure file is unavailable")
    geometry = read_xyz(source_path)
    if geometry.atom_count != structure.atom_count:
        raise IntermolecularLEDWorkflowError("structure atom count contradicts its XYZ file")

    partition_hash = fragment_partition_hash(selected_fragments)
    partition_record = [item.to_dict() for item in selected_fragments]
    method_contract = _method_contract(dimer_spec)
    method_contract_hash = stable_hash(method_contract)
    common_protocol = {
        **_base_protocol(dimer_spec.protocol),
        "led_contract": INTERMOLECULAR_LED_CONTRACT,
        "reference_semantics": (
            LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
        ),
        "source_structure_artifact_id": structure.artifact_id,
        "full_dimer_geometry_hash": structure.geometry_hash,
        "fragment_partition": partition_record,
        "fragment_partition_hash": partition_hash,
        "led_method_contract": method_contract,
        "led_method_contract_hash": method_contract_hash,
    }
    dimer_protocol = {
        **common_protocol,
        "led": True,
        "led_role": "dimer",
        "fragments_required": True,
        "expected_fragments": 2,
    }
    resolved_dimer_spec = OrcaStageSpec(
        StageType.SP,
        dimer_spec.keywords,
        dimer_spec.blocks,
        protocol=dimer_protocol,
    )
    all_indices = tuple(range(geometry.atom_count))
    atom_to_fragment = {
        atom_index: number
        for number, fragment in enumerate(selected_fragments, start=1)
        for atom_index in fragment.atom_indices
    }
    dimer_labels = tuple(
        f"{atom.element}({atom_to_fragment[index]})"
        for index, atom in enumerate(geometry.atoms)
    )

    dimer_id = "led_dimer"
    fragment_node_ids = tuple(
        f"led_fragment_reference_{index}"
        for index in range(1, 3)
    )
    assembler_id = "led_assembler"
    structure_id = "structure"
    dimer_artifact = DimerEnergyArtifact(
        producing_calculation=dimer_id,
        method=method,
        basis=basis,
        protocol=dimer_protocol,
        parent_artifacts=(structure.artifact_id,),
        metadata={
            "energy_role": "led_dimer",
            "source_structure_artifact_id": structure.artifact_id,
        },
    )
    fragment_artifacts: list[LEDFragmentReferenceArtifact] = []
    fragment_specs: list[OrcaStageSpec] = []
    fragment_selections: list[dict[str, object]] = []
    for fragment, state in zip(selected_fragments, ordered_states, strict=True):
        active = tuple(fragment.atom_indices)
        active_set = set(active)
        ghosts = tuple(index for index in all_indices if index not in active_set)
        reference_identity = led_fragment_reference_identity(
            structure=structure,
            fragment=fragment,
            state=state,
            ghost_atom_indices=ghosts,
            method_contract=method_contract,
            partition_hash=partition_hash,
        )
        protocol = {
            **common_protocol,
            "led": False,
            "led_role": "fragment_reference",
            "fragment_id": fragment.fragment_id,
            "fragment_identity_hash": reference_identity,
            "active_atom_indices": list(active),
            "ghost_atom_indices": list(ghosts),
            "charge": state.charge,
            "multiplicity": state.multiplicity,
        }
        fragment_specs.append(
            OrcaStageSpec(
                StageType.SP,
                _base_keywords(dimer_spec),
                dimer_spec.blocks,
                protocol=protocol,
            )
        )
        fragment_selections.append(
            {
                "atom_indices": list(all_indices),
                "atom_labels": [
                    f"{atom.element}:" if index in set(ghosts) else atom.element
                    for index, atom in enumerate(geometry.atoms)
                ],
                "charge": state.charge,
                "multiplicity": state.multiplicity,
                "allow_electronic_state_override": True,
            }
        )
        fragment_artifacts.append(
            LEDFragmentReferenceArtifact(
                producing_calculation=fragment_node_ids[len(fragment_artifacts)],
                method=method,
                basis=basis,
                protocol=protocol,
                parent_artifacts=(structure.artifact_id,),
                metadata={
                    "energy_role": "led_fragment_reference",
                    "fragment_id": fragment.fragment_id,
                    "fragment_identity_hash": reference_identity,
                    "source_structure_artifact_id": structure.artifact_id,
                },
            )
        )

    nodes = [
        CalculationNode(structure_id, produces=("StructureArtifact",)),
        CalculationNode(
            dimer_id,
            dependencies=(structure_id,),
            operation="orca_single_point",
            role="led_dimer",
            requires=(ArtifactRequirement("StructureArtifact", from_nodes=(structure_id,)),),
            produces=("DimerEnergyArtifact",),
            configuration={"execution_intent": resolved_dimer_spec.execution_intent.to_dict()},
        ),
    ]
    for node_id, fragment, spec in zip(
        fragment_node_ids, selected_fragments, fragment_specs, strict=True
    ):
        nodes.append(
            CalculationNode(
                node_id,
                dependencies=(structure_id,),
                operation="orca_single_point",
                role="led_fragment_reference",
                requires=(ArtifactRequirement("StructureArtifact", from_nodes=(structure_id,)),),
                produces=("LEDFragmentReferenceArtifact",),
                configuration={
                    "fragment_id": fragment.fragment_id,
                    "reference_semantics": (
                        LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
                    ),
                    "execution_intent": spec.execution_intent.to_dict(),
                },
            )
        )
    nodes.append(
        AggregationNode(
            assembler_id,
            dependencies=(dimer_id, *fragment_node_ids),
            operation="assemble_interfragment_led",
            requires=(
                ArtifactRequirement("DimerEnergyArtifact", 1, (dimer_id,)),
                ArtifactRequirement(
                    "LEDFragmentReferenceArtifact", 2, fragment_node_ids
                ),
            ),
            produces=("LEDArtifact",),
            configuration={
                "led_contract": INTERMOLECULAR_LED_CONTRACT,
                "reference_semantics": (
                    LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
                ),
                "fragment_partition_hash": partition_hash,
            },
        )
    )
    graph = WorkflowGraph(
        graph_id,
        tuple(nodes),
        external_inputs=("StructureArtifact",),
        provenance={"workflow": "cmw.molecular.workflows.intermolecular_led"},
    )
    execution_nodes = [
        orca_execution_plan_node(
            dimer_id,
            resolved_dimer_spec,
            planned_artifacts=(dimer_artifact,),
            geometry_artifact_id=structure.artifact_id,
            geometry_selection={
                "atom_indices": list(all_indices),
                "atom_labels": list(dimer_labels),
            },
        )
    ]
    for node_id, spec, selection, artifact in zip(
        fragment_node_ids,
        fragment_specs,
        fragment_selections,
        fragment_artifacts,
        strict=True,
    ):
        execution_nodes.append(
            orca_execution_plan_node(
                node_id,
                spec,
                planned_artifacts=(artifact,),
                geometry_artifact_id=structure.artifact_id,
                geometry_selection=selection,
            )
        )
    plan = ExecutionPlan(
        graph,
        tuple(execution_nodes),
        available_artifacts={structure_id: (structure,)},
        provenance={
            "led_contract": INTERMOLECULAR_LED_CONTRACT,
            "reference_semantics": (
                LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
            ),
            "fragment_partition_hash": partition_hash,
            "led_method_contract_hash": method_contract_hash,
        },
    )
    return IntermolecularLEDPlan(
        structure,
        selected_fragments,
        ordered_states,
        graph,
        plan,
        {
            dimer_id: (dimer_artifact,),
            **{
                node_id: (artifact,)
                for node_id, artifact in zip(
                    fragment_node_ids, fragment_artifacts, strict=True
                )
            },
        },
        partition_hash,
        method_contract_hash,
        metadata={
            "calculation_nodes": [dimer_id, *fragment_node_ids],
            "aggregation_node": assembler_id,
        },
    )


def _output_path(artifact: CalculationArtifact) -> Path:
    raw = artifact.files.get("output")
    if not raw:
        raise IntermolecularLEDWorkflowError(
            f"calculation artifact lacks output: {artifact.artifact_id}"
        )
    path = Path(raw)
    if not path.is_file() or path.stat().st_size < 1:
        raise IntermolecularLEDWorkflowError(f"calculation output is unavailable: {path}")
    return path


def _expected_parent_contract(
    structure: StructureArtifact,
    fragments: tuple[FragmentDefinition, FragmentDefinition],
    dimer: CalculationArtifact,
    references: tuple[CalculationArtifact, CalculationArtifact],
) -> tuple[str, str]:
    partition_hash = fragment_partition_hash(fragments)
    partition_record = [item.to_dict() for item in fragments]
    all_parents = (dimer, *references)
    if not isinstance(dimer, DimerEnergyArtifact) or any(
        not isinstance(item, LEDFragmentReferenceArtifact) for item in references
    ):
        raise IntermolecularLEDWorkflowError(
            "LED parents require one DimerEnergyArtifact and two "
            "LEDFragmentReferenceArtifact instances"
        )
    if any(not item.validation.passed for item in all_parents):
        raise IntermolecularLEDWorkflowError(
            "all three LED calculation artifacts must pass validation"
        )
    if dimer.protocol.get("led_role") != "dimer" or dimer.protocol.get("led") is not True:
        raise IntermolecularLEDWorkflowError("dimer parent is not a validated LED SP")
    if any(item.protocol.get("led_role") != "fragment_reference" for item in references):
        raise IntermolecularLEDWorkflowError("fragment parents are not LED references")
    if any(
        item.protocol.get("reference_semantics")
        != LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
        for item in all_parents
    ):
        raise IntermolecularLEDWorkflowError("LED reference semantics mismatch")
    contract_hashes = {item.protocol.get("led_method_contract_hash") for item in all_parents}
    if None in contract_hashes or len(contract_hashes) != 1:
        raise IntermolecularLEDWorkflowError("LED scientific input contracts differ")
    method_contract = dimer.protocol.get("led_method_contract")
    if not isinstance(method_contract, Mapping):
        raise IntermolecularLEDWorkflowError("LED method contract is missing")
    method_contract_record = dict(method_contract)
    if stable_hash(method_contract_record) != next(iter(contract_hashes)):
        raise IntermolecularLEDWorkflowError("LED method-contract hash mismatch")
    for item in all_parents:
        parent_contract = item.protocol.get("led_method_contract")
        if (
            not isinstance(parent_contract, Mapping)
            or dict(parent_contract) != method_contract_record
        ):
            raise IntermolecularLEDWorkflowError("LED method contracts differ")
        if item.protocol.get("source_structure_artifact_id") != structure.artifact_id:
            raise IntermolecularLEDWorkflowError("LED parent structure identity mismatch")
        if item.protocol.get("full_dimer_geometry_hash") != structure.geometry_hash:
            raise IntermolecularLEDWorkflowError("LED parent geometry hash mismatch")
        if item.protocol.get("fragment_partition") != partition_record:
            raise IntermolecularLEDWorkflowError("LED fragment partition record mismatch")
        if item.protocol.get("fragment_partition_hash") != partition_hash:
            raise IntermolecularLEDWorkflowError("LED fragment partition hash mismatch")
    by_fragment = {
        str(item.protocol.get("fragment_id")): item for item in references
    }
    if set(by_fragment) != {item.fragment_id for item in fragments}:
        raise IntermolecularLEDWorkflowError("LED fragment reference identities mismatch")
    all_atoms = tuple(range(structure.atom_count or 0))
    for fragment in fragments:
        reference = by_fragment[fragment.fragment_id]
        active = tuple(reference.protocol.get("active_atom_indices", ()))
        ghosts = tuple(reference.protocol.get("ghost_atom_indices", ()))
        active_set = set(active)
        expected_ghosts = tuple(index for index in all_atoms if index not in active_set)
        if active != fragment.atom_indices or ghosts != expected_ghosts:
            raise IntermolecularLEDWorkflowError(
                f"fragment {fragment.fragment_id} active/ghost mapping mismatch"
            )
        charge = reference.protocol.get("charge")
        multiplicity = reference.protocol.get("multiplicity")
        if (
            isinstance(charge, bool)
            or not isinstance(charge, int)
            or isinstance(multiplicity, bool)
            or not isinstance(multiplicity, int)
        ):
            raise IntermolecularLEDWorkflowError(
                f"fragment {fragment.fragment_id} electronic state is invalid"
            )
        state = LEDFragmentElectronicState(
            fragment.fragment_id,
            charge,
            multiplicity,
        )
        expected_identity = led_fragment_reference_identity(
            structure=structure,
            fragment=fragment,
            state=state,
            ghost_atom_indices=ghosts,
            method_contract=method_contract_record,
            partition_hash=partition_hash,
        )
        if reference.protocol.get("fragment_identity_hash") != expected_identity:
            raise IntermolecularLEDWorkflowError(
                f"fragment {fragment.fragment_id} scientific identity mismatch"
            )
    method_basis = {(item.method, item.basis) for item in all_parents}
    if len(method_basis) != 1:
        raise IntermolecularLEDWorkflowError("LED parent methods or bases differ")
    return str(next(iter(contract_hashes))), partition_hash


def _source_execution_identity(
    artifact: CalculationArtifact,
) -> tuple[dict[str, object], dict[str, object]]:
    attempt = artifact.provenance.get("attempt")
    if not isinstance(attempt, Mapping) or not str(attempt.get("attempt_id", "")):
        raise IntermolecularLEDWorkflowError(
            f"LED parent lacks source execution attempt: {artifact.artifact_id}"
        )
    executable = attempt.get("executable")
    if not isinstance(executable, Mapping) or not executable:
        nested = artifact.provenance.get("provenance")
        executable = nested.get("executable") if isinstance(nested, Mapping) else None
    if not isinstance(executable, Mapping) or not executable:
        raise IntermolecularLEDWorkflowError(
            f"LED parent lacks executable provenance: {artifact.artifact_id}"
        )
    version = executable.get("version")
    if version is not None and str(version) != "6.1.1":
        raise IntermolecularLEDWorkflowError(
            f"LED parent executable is not ORCA 6.1.1: {artifact.artifact_id}"
        )
    return dict(attempt), dict(executable)


def finalize_intermolecular_led_artifact(
    structure: StructureArtifact,
    fragments: Sequence[FragmentDefinition],
    dimer_calculation: CalculationArtifact,
    fragment_reference_calculations: Sequence[CalculationArtifact],
    *,
    producing_calculation: str = "intermolecular_led_assembler",
    reconstruction_tolerance_hartree: float = (
        DEFAULT_LED_RECONSTRUCTION_TOLERANCE_HARTREE
    ),
) -> LEDArtifact:
    """Parse three finalized outputs and create one validated numerical artifact."""

    if not structure.validation.passed or not validate_structure_artifact(structure).passed:
        raise IntermolecularLEDWorkflowError("source StructureArtifact is invalid")
    selected_fragments = _validated_partition(structure, fragments)
    references = tuple(fragment_reference_calculations)
    if len(references) != 2:
        raise IntermolecularLEDWorkflowError("exactly two fragment references are required")
    method_contract_hash, partition_hash = _expected_parent_contract(
        structure, selected_fragments, dimer_calculation, references
    )
    dimer_path = _output_path(dimer_calculation)
    try:
        dimer = parse_orca_interfragment_led_file(dimer_path)
    except OrcaLEDError as exc:
        raise IntermolecularLEDWorkflowError(str(exc)) from exc
    expected_partitions = tuple(item.atom_indices for item in selected_fragments)
    parsed_partitions = tuple(item.atom_indices for item in dimer.fragment_partitions)
    if parsed_partitions != expected_partitions:
        raise IntermolecularLEDWorkflowError(
            "ORCA LED fragment mapping contradicts the declared partition"
        )
    by_fragment = {
        str(item.protocol["fragment_id"]): item for item in references
    }
    try:
        parsed_references = tuple(
            parse_orca_led_fragment_reference_file(
                _output_path(by_fragment[fragment.fragment_id]),
                fragment_id=fragment.fragment_id,
            )
            for fragment in selected_fragments
        )
        result = assemble_interfragment_led(
            dimer,
            parsed_references,
            reconstruction_tolerance_hartree=reconstruction_tolerance_hartree,
        )
    except OrcaLEDError as exc:
        raise IntermolecularLEDWorkflowError(str(exc)) from exc
    parents = (dimer_calculation, *references)
    execution_identities = {
        parent.artifact_id: _source_execution_identity(parent) for parent in parents
    }
    protocol = {
        "led_contract": INTERMOLECULAR_LED_CONTRACT,
        "reference_semantics": (
            LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
        ),
        "led_method_contract_hash": method_contract_hash,
        "fragment_partition_hash": partition_hash,
        "reconstruction_tolerance_hartree": reconstruction_tolerance_hartree,
    }
    files = {
        "dimer_output": str(dimer_path),
        **{
            f"fragment_{fragment.fragment_id}_reference_output": str(
                _output_path(by_fragment[fragment.fragment_id])
            )
            for fragment in selected_fragments
        },
    }
    source_outputs = {
        "dimer": dict(dimer.source_output_identity),
        **{
            f"fragment_{item.fragment_id}": dict(item.source_output_identity)
            for item in parsed_references
        },
    }
    artifact = LEDArtifact(
        producing_calculation=producing_calculation,
        method=dimer_calculation.method,
        basis=dimer_calculation.basis,
        protocol=protocol,
        parent_artifacts=tuple(item.artifact_id for item in parents),
        files=files,
        validation=ArtifactValidation(
            ValidationStatus.PASSED,
            {
                "three_calculation_parents": True,
                "source_structure": True,
                "geometry_hash": True,
                "fragment_partition": True,
                "reference_semantics": True,
                "orca_6_1_1_grammar": True,
                "numerical_reconstruction": True,
                "residual_within_tolerance": True,
            },
            "VALID_INTERMOLECULAR_LED",
            "three ORCA results support the validated six-component LED decomposition",
        ),
        provenance={
            "source_outputs": source_outputs,
            "source_target_ids": {
                "dimer": dimer_calculation.producing_calculation,
                **{
                    f"fragment_{item.protocol['fragment_id']}": item.producing_calculation
                    for item in references
                },
            },
            "source_execution_attempts": {
                "dimer": execution_identities[dimer_calculation.artifact_id][0],
                **{
                    f"fragment_{item.protocol['fragment_id']}": execution_identities[
                        item.artifact_id
                    ][0]
                    for item in references
                },
            },
            "source_executables": {
                "dimer": execution_identities[dimer_calculation.artifact_id][1],
                **{
                    f"fragment_{item.protocol['fragment_id']}": execution_identities[
                        item.artifact_id
                    ][1]
                    for item in references
                },
            },
            "fragment_reference_contracts": {
                fragment.fragment_id: {
                    "fragment_identity_hash": by_fragment[
                        fragment.fragment_id
                    ].protocol["fragment_identity_hash"],
                    "active_atom_indices": list(fragment.atom_indices),
                    "ghost_atom_indices": list(
                        by_fragment[fragment.fragment_id].protocol[
                            "ghost_atom_indices"
                        ]
                    ),
                    "charge": by_fragment[fragment.fragment_id].protocol["charge"],
                    "multiplicity": by_fragment[
                        fragment.fragment_id
                    ].protocol["multiplicity"],
                }
                for fragment in selected_fragments
            },
        },
        metadata={
            "source_structure_artifact_id": structure.artifact_id,
            "full_dimer_geometry_hash": structure.geometry_hash,
            "fragment_partition_hash": partition_hash,
            "fragments": [item.to_dict() for item in selected_fragments],
            "reference_semantics": (
                LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
            ),
            "orca_dimer_direct_total_is_raw_only": True,
        },
        led_result=result.to_dict(),
    )
    validate_artifact_compatibility(artifact, parents)
    validation = validate_intermolecular_led_artifact(
        artifact,
        structure=structure,
        fragments=selected_fragments,
        parents=parents,
    )
    if not validation.passed:
        raise IntermolecularLEDWorkflowError(validation.reason)
    return replace(artifact, validation=validation)


def validate_intermolecular_led_artifact(
    artifact: LEDArtifact,
    *,
    structure: StructureArtifact,
    fragments: Sequence[FragmentDefinition],
    parents: Sequence[CalculationArtifact],
) -> ArtifactValidation:
    """Revalidate numerical content, files, lineage, and exact source hashes."""

    checks: dict[str, bool | None] = {
        "artifact_validation": False,
        "contract": False,
        "parents": False,
        "structure": False,
        "fragment_partition": False,
        "source_target_ids": False,
        "source_execution_attempts": False,
        "source_executables": False,
        "source_output_hashes": False,
        "numerical_result": False,
    }
    try:
        selected_fragments = _validated_partition(structure, fragments)
        selected_parents = tuple(parents)
        checks["artifact_validation"] = artifact.validation.passed
        dimer = [item for item in selected_parents if item.protocol.get("led_role") == "dimer"]
        references = [
            item
            for item in selected_parents
            if item.protocol.get("led_role") == "fragment_reference"
        ]
        checks["contract"] = (
            artifact.protocol.get("led_contract") == INTERMOLECULAR_LED_CONTRACT
            and artifact.protocol.get("reference_semantics")
            == LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS.value
        )
        if len(dimer) != 1 or len(references) != 2:
            raise IntermolecularLEDWorkflowError("LED artifact parents are incomplete")
        _expected_parent_contract(
            structure,
            selected_fragments,
            dimer[0],
            (references[0], references[1]),
        )
        checks["parents"] = artifact.parent_artifacts == tuple(
            item.artifact_id for item in selected_parents
        )
        checks["structure"] = (
            artifact.metadata.get("source_structure_artifact_id")
            == structure.artifact_id
            and artifact.metadata.get("full_dimer_geometry_hash")
            == structure.geometry_hash
        )
        expected_partition_hash = fragment_partition_hash(selected_fragments)
        checks["fragment_partition"] = (
            artifact.metadata.get("fragment_partition_hash")
            == expected_partition_hash
            and artifact.protocol.get("fragment_partition_hash")
            == expected_partition_hash
        )
        result = IntermolecularLEDResult.from_mapping(artifact.led_result)
        checks["numerical_result"] = (
            result.reference_semantics
            is LEDReferenceSemantics.FROZEN_FRAGMENT_IN_DIMER_BASIS
            and abs(result.reconstruction_residual_hartree)
            <= result.reconstruction_tolerance_hartree
        )
        target_ids = artifact.provenance.get("source_target_ids")
        attempts = artifact.provenance.get("source_execution_attempts")
        executables = artifact.provenance.get("source_executables")
        if not all(
            isinstance(item, Mapping) for item in (target_ids, attempts, executables)
        ):
            raise IntermolecularLEDWorkflowError(
                "source target, attempt, or executable provenance is missing"
            )
        expected_roles = {
            "dimer": dimer[0],
            **{
                f"fragment_{item.protocol['fragment_id']}": item
                for item in references
            },
        }
        expected_execution = {
            role: _source_execution_identity(parent)
            for role, parent in expected_roles.items()
        }
        checks["source_target_ids"] = all(
            target_ids.get(role) == parent.producing_calculation
            for role, parent in expected_roles.items()
        )
        checks["source_execution_attempts"] = all(
            isinstance(attempts.get(role), Mapping)
            and attempts[role].get("attempt_id")
            == expected_execution[role][0].get("attempt_id")
            for role in expected_roles
        )
        checks["source_executables"] = all(
            isinstance(executables.get(role), Mapping)
            and dict(executables[role]) == expected_execution[role][1]
            for role in expected_roles
        )
        output_identities = artifact.provenance.get("source_outputs")
        if not isinstance(output_identities, Mapping):
            raise IntermolecularLEDWorkflowError("source-output provenance is missing")
        paths = {
            "dimer": artifact.files.get("dimer_output"),
            **{
                f"fragment_{item.fragment_id}": artifact.files.get(
                    f"fragment_{item.fragment_id}_reference_output"
                )
                for item in selected_fragments
            },
        }
        checks["source_output_hashes"] = all(
            path is not None
            and Path(path).is_file()
            and isinstance(output_identities.get(role), Mapping)
            and output_identities[role].get("source_sha256") == file_hash(Path(path))
            and output_identities[role].get("source_size_bytes") == Path(path).stat().st_size
            for role, path in paths.items()
        )
        if not all(value is True for value in checks.values()):
            raise IntermolecularLEDWorkflowError("one or more LED validation checks failed")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return ArtifactValidation(
            ValidationStatus.FAILED,
            checks,
            getattr(exc, "code", "FAILED_INTERMOLECULAR_LED_VALIDATION"),
            str(exc),
        )
    return ArtifactValidation(
        ValidationStatus.PASSED,
        checks,
        "VALID_INTERMOLECULAR_LED",
        "LED numerical content, lineage, and source outputs revalidated",
    )


def check_intermolecular_led_reuse(
    artifact: LEDArtifact,
    *,
    structure: StructureArtifact,
    fragments: Sequence[FragmentDefinition],
    parents: Sequence[CalculationArtifact],
) -> dict[str, object]:
    """Return an exact, non-mutating reuse verdict for a finalized LED artifact."""

    validation = validate_intermolecular_led_artifact(
        artifact,
        structure=structure,
        fragments=fragments,
        parents=parents,
    )
    return {
        "reuse": validation.passed,
        "code": validation.code,
        "reason": validation.reason,
        "artifact_id": artifact.artifact_id,
        "validation": validation.to_dict(),
    }


__all__ = [
    "INTERMOLECULAR_LED_CONTRACT",
    "IntermolecularLEDPlan",
    "IntermolecularLEDWorkflowError",
    "LEDFragmentElectronicState",
    "build_intermolecular_led_plan",
    "check_intermolecular_led_reuse",
    "finalize_intermolecular_led_artifact",
    "fragment_partition_hash",
    "led_fragment_reference_identity",
    "validate_intermolecular_led_artifact",
]
