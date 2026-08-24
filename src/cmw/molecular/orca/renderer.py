"""ORCA implementation of the generic workflow-plan renderer contract."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from cmw.core.artifacts import Artifact, StructureArtifact
from cmw.core.execution_contract import ComputationalTask, ExecutionContractError
from cmw.core.execution_layout import ExecutionLayout
from cmw.core.execution_profiles import ExecutionProfile
from cmw.core.job import GeometryLineage, JobTarget
from cmw.core.plan_materialization import (
    ExecutionPlanNode,
    ExecutionResourceProfile,
    ExecutionTarget,
    RenderedExecutionInput,
)
from cmw.core.provenance import atomic_write_json
from cmw.core.structure_artifacts import (
    structure_artifact_path,
    validate_structure_artifact,
)
from cmw.structure.xyz import Atom, XYZGeometry, geometry_hash, read_xyz

from .geometry import prepare_orca_geometry_input
from .input import (
    OrcaStageSpec,
    make_target_from_geometry_input,
    render_orca_input,
    resolve_orca_resources,
)
from .job import check_reuse, load_target, write_target
from .status import StageType


ORCA_RENDERER_ID = "orca"


def _base_orca_element(label: str) -> str:
    selected = label[:-1] if label.endswith(":") else label
    if "(" in selected and selected.endswith(")"):
        selected = selected.split("(", 1)[0]
    return selected


def orca_execution_plan_node(
    node_id: str,
    spec: OrcaStageSpec,
    *,
    input_artifacts: Sequence[Artifact] = (),
    planned_artifacts: Sequence[Artifact] = (),
    geometry_artifact_id: str | None = None,
    geometry_selection: Mapping[str, object] | None = None,
    outputs: Mapping[str, str] | None = None,
    lineage: Mapping[str, object] | None = None,
) -> ExecutionPlanNode:
    """Bind an adapter's ORCA plan to the generic materialization contract."""

    configuration: dict[str, object] = {
        "spec": spec.scientific_identity(),
        "outputs": dict(outputs or {}),
    }
    if geometry_artifact_id is not None:
        configuration["geometry_artifact_id"] = geometry_artifact_id
    if geometry_selection is not None:
        configuration["geometry_selection"] = dict(geometry_selection)
    if lineage is not None:
        configuration["lineage"] = dict(lineage)
    return ExecutionPlanNode(
        node_id=node_id,
        renderer_id=ORCA_RENDERER_ID,
        execution_intent=spec.execution_intent,
        renderer_configuration=configuration,
        input_artifacts=tuple(input_artifacts),
        planned_artifacts=tuple(planned_artifacts),
    )


class OrcaExecutionRenderer:
    """Render an ORCA attempt while leaving process launch to the shell layer."""

    renderer_id = ORCA_RENDERER_ID
    supported_tasks = frozenset(
        {
            ComputationalTask.OPTIMIZATION,
            ComputationalTask.FREQUENCY,
            ComputationalTask.SINGLE_POINT,
            ComputationalTask.EXCITED_STATE,
        }
    )

    @staticmethod
    def _spec(node: ExecutionPlanNode) -> OrcaStageSpec:
        raw = node.renderer_configuration.get("spec")
        if not isinstance(raw, Mapping):
            raise ExecutionContractError(
                f"{ExecutionContractError.code}: ORCA renderer requires a spec mapping"
            )
        try:
            spec = OrcaStageSpec(
                StageType(str(raw["stage_type"])),
                str(raw["keywords"]),
                tuple(str(item) for item in raw.get("blocks", ())),
                protocol=dict(raw.get("protocol", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionContractError(
                f"{ExecutionContractError.code}: invalid ORCA renderer specification: {exc}"
            ) from exc
        if spec.execution_intent != node.execution_intent:
            raise ExecutionContractError(
                f"{ExecutionContractError.code}: ORCA stage specification conflicts "
                "with execution-plan intent"
            )
        return spec

    @staticmethod
    def _geometry(
        node: ExecutionPlanNode, input_artifacts: Sequence[Artifact]
    ) -> StructureArtifact:
        structures = [
            artifact
            for artifact in input_artifacts
            if isinstance(artifact, StructureArtifact)
        ]
        selected_id = node.renderer_configuration.get("geometry_artifact_id")
        if selected_id is not None:
            structures = [
                artifact
                for artifact in structures
                if artifact.artifact_id == str(selected_id)
            ]
        if len(structures) != 1:
            raise ValueError(
                "ORCA materialization requires exactly one resolved StructureArtifact"
            )
        structure = structures[0]
        validation = validate_structure_artifact(structure)
        if not structure.validation.passed or not validation.passed:
            raise ValueError(
                "ORCA geometry StructureArtifact has not passed validation: "
                f"{validation.code}: {validation.reason}"
            )
        if (
            structure.charge is None
            or structure.multiplicity is None
            or not structure.geometry_hash
        ):
            raise ValueError(
                "ORCA geometry requires charge, multiplicity, and geometry hash"
            )
        return structure

    @staticmethod
    def _output_names(node: ExecutionPlanNode) -> dict[str, str]:
        raw = node.renderer_configuration.get("outputs", {})
        if not isinstance(raw, Mapping):
            raise ValueError("ORCA outputs must be a mapping")
        outputs = {str(role): str(filename) for role, filename in raw.items()}
        if any(
            not role
            or not filename
            or Path(filename).name != filename
            for role, filename in outputs.items()
        ):
            raise ValueError(
                "ORCA output names must be non-empty attempt-local basenames"
            )
        return outputs

    @staticmethod
    def _geometry_selection(
        node: ExecutionPlanNode, structure: StructureArtifact
    ) -> tuple[tuple[int, ...], tuple[str, ...], int, int, bool]:
        raw = node.renderer_configuration.get("geometry_selection", {})
        if not isinstance(raw, Mapping):
            raise ValueError("ORCA geometry selection must be a mapping")
        path = structure_artifact_path(structure)
        if path is None:
            raise ValueError("ORCA geometry StructureArtifact lacks a structure file")
        geometry = read_xyz(path)
        raw_indices = raw.get("atom_indices")
        indices = (
            tuple(range(geometry.atom_count))
            if raw_indices is None
            else tuple(int(item) for item in raw_indices)  # type: ignore[arg-type]
        )
        if (
            not indices
            or len(set(indices)) != len(indices)
            or any(index < 0 or index >= geometry.atom_count for index in indices)
        ):
            raise ValueError("ORCA geometry atom selection is invalid")
        raw_labels = raw.get("atom_labels")
        labels = (
            tuple(geometry.atoms[index].element for index in indices)
            if raw_labels is None
            else tuple(str(item) for item in raw_labels)  # type: ignore[arg-type]
        )
        if len(labels) != len(indices):
            raise ValueError("ORCA geometry labels do not match selected atoms")
        for label, index in zip(labels, indices, strict=True):
            atom = geometry.atoms[index]
            if _base_orca_element(label) != atom.element:
                raise ValueError("ORCA geometry label contradicts the source element")
        charge = int(raw.get("charge", structure.charge))
        multiplicity = int(raw.get("multiplicity", structure.multiplicity))
        allow_override = bool(raw.get("allow_electronic_state_override", False))
        if multiplicity < 1:
            raise ValueError("ORCA geometry multiplicity must be positive")
        if not allow_override and (charge, multiplicity) != (
            structure.charge,
            structure.multiplicity,
        ):
            raise ValueError(
                "ORCA selected electronic state conflicts with its StructureArtifact"
            )
        return indices, labels, charge, multiplicity, allow_override

    @classmethod
    def _selected_geometry(
        cls, node: ExecutionPlanNode, structure: StructureArtifact
    ) -> tuple[XYZGeometry, int, int]:
        indices, labels, charge, multiplicity, _ = cls._geometry_selection(
            node, structure
        )
        path = structure_artifact_path(structure)
        assert path is not None
        source = read_xyz(path)
        selected = XYZGeometry(
            tuple(
                Atom(_base_orca_element(label), *source.atoms[index].coordinates)
                for index, label in zip(indices, labels, strict=True)
            ),
            source.comment,
        )
        return selected, charge, multiplicity

    def validate(
        self, node: ExecutionPlanNode, input_artifacts: Sequence[Artifact]
    ) -> None:
        self._spec(node)
        structure = self._geometry(node, input_artifacts)
        self._geometry_selection(node, structure)
        self._output_names(node)

    def build_target(
        self, node: ExecutionPlanNode, input_artifacts: Sequence[Artifact]
    ) -> JobTarget:
        spec = self._spec(node)
        structure = self._geometry(node, input_artifacts)
        selected, charge, multiplicity = self._selected_geometry(node, structure)
        return JobTarget(
            stage_type=spec.stage_type.value,
            geometry_sha256=geometry_hash(selected),
            charge=charge,
            multiplicity=multiplicity,
            calculation=spec.scientific_identity(),
        )

    def render(
        self,
        node: ExecutionPlanNode,
        input_artifacts: Sequence[Artifact],
        *,
        target: ExecutionTarget,
        layout: ExecutionLayout,
        resource_profile: ExecutionResourceProfile,
        runtime_identity: Mapping[str, object],
    ) -> RenderedExecutionInput:
        if not isinstance(resource_profile, ExecutionProfile):
            raise TypeError("ORCA renderer requires a CMW ExecutionProfile")
        if not isinstance(target, JobTarget):
            raise TypeError("ORCA renderer requires a JobTarget")
        spec = self._spec(node)
        structure = self._geometry(node, input_artifacts)
        indices, labels, charge, multiplicity, allow_override = (
            self._geometry_selection(node, structure)
        )
        resources = resolve_orca_resources(resource_profile)
        geometry_input = prepare_orca_geometry_input(
            structure,
            layout.input_path("input.xyz"),
            atom_indices=indices,
            atom_labels=labels,
            charge=charge,
            multiplicity=multiplicity,
            allow_electronic_state_override=allow_override,
            provenance={
                "materializer": "cmw.core.WorkflowPlanMaterializer",
                "workflow_node": node.node_id,
            },
        )
        rendered_target = make_target_from_geometry_input(
            geometry_input, spec=spec
        )
        if rendered_target.target_id != target.target_id:
            raise ValueError("materialized ORCA geometry changed target identity")

        target_path = layout.target_directory / "target.json"
        raw_lineage = node.renderer_configuration.get("lineage", {})
        if not isinstance(raw_lineage, Mapping):
            raise ValueError("ORCA lineage must be a mapping")
        lineage = GeometryLineage(
            source=str(raw_lineage.get("source", structure.source or node.node_id)),
            geometry_sha256=target.geometry_sha256,
            parent_stage=(
                str(raw_lineage["parent_stage"])
                if raw_lineage.get("parent_stage") is not None
                else None
            ),
            parent_target_id=(
                str(raw_lineage["parent_target_id"])
                if raw_lineage.get("parent_target_id") is not None
                else None
            ),
            parent_artifact_sha256=(
                str(raw_lineage["parent_artifact_sha256"])
                if raw_lineage.get("parent_artifact_sha256") is not None
                else None
            ),
        )
        if target_path.exists():
            stored, _ = load_target(target_path)
            if stored.target_id != target.target_id:
                raise FileExistsError("stored ORCA target conflicts with plan")
        else:
            write_target(target_path, target, lineage)

        geometry_contract_path = layout.metadata_path("geometry-input.json")
        atomic_write_json(geometry_contract_path, geometry_input.to_dict())
        runtime_path = layout.metadata_path("orca-runtime.json")
        atomic_write_json(runtime_path, dict(runtime_identity))
        stage_input = layout.input_path("stage.inp")
        stage_input.write_text(
            render_orca_input(
                geometry_input=geometry_input,
                charge=target.charge,
                multiplicity=target.multiplicity,
                spec=spec,
                resources=resources.resources,
            ),
            encoding="utf-8",
        )

        output_files = {"output": layout.output_path("stage.out")}
        for role, filename in self._output_names(node).items():
            if role in output_files and output_files[role].name != filename:
                raise ValueError(f"ORCA output role conflicts: {role}")
            output_files[role] = layout.output_path(filename)
        executable = runtime_identity.get("orca")
        if not isinstance(executable, Mapping):
            executable = {
                "renderer": self.renderer_id,
                "runtime_id": runtime_identity.get("runtime_id"),
            }
        return RenderedExecutionInput(
            primary_input=stage_input,
            target_path=target_path,
            input_files={"primary": stage_input, "geometry": geometry_input.input_path},
            output_files=output_files,
            log_files={"stderr": layout.log_path("stage.err")},
            provenance_files={
                "layout": layout.layout_path,
                "geometry_contract": geometry_contract_path,
                "runtime_contract": runtime_path,
            },
            resources=resources.resources.to_dict(),
            executable=dict(executable),
            metadata={
                "renderer": self.renderer_id,
                "resolved_resources": resources.to_dict(),
                "launch_contract": {
                    "working_directory": str(layout.working_directory),
                    "input": str(stage_input),
                    "target": str(target_path),
                    "metadata": str(layout.metadata_path("job.json")),
                    "output": str(layout.output_path("stage.out")),
                    "stderr": str(layout.log_path("stage.err")),
                    "layout": str(layout.layout_path),
                    "geometry_contract": str(geometry_contract_path),
                    "runtime_contract": str(runtime_path),
                    "artifacts": {
                        role: str(path)
                        for role, path in output_files.items()
                        if role != "output"
                    },
                },
            },
        )

    def is_reusable(self, target_path: Path, metadata_path: Path) -> bool:
        try:
            return bool(check_reuse(target_path, metadata_path)["reuse"])
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            return False


__all__ = [
    "ORCA_RENDERER_ID",
    "OrcaExecutionRenderer",
    "orca_execution_plan_node",
]
