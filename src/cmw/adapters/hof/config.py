"""Load the HOF YAML schema and normalize it into adapter models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from cmw.structure.xyz import XYZGeometry, read_xyz

from .models import (
    HOF_ADAPTER_SCHEMA_VERSION,
    HofAdapterConfiguration,
    HofFragment,
    HofHydrogenBond,
    HofInteractionProtocol,
    HofSystem,
)
from .validation import validate_hof_system


def load_yaml_document(path: Path) -> dict[str, Any]:
    """Load one mapping with PyYAML's safe loader."""

    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError("HOF YAML support requires the PyYAML package") from exc
    value = yaml.safe_load(path.read_text(encoding="utf-8", errors="strict"))
    if not isinstance(value, Mapping):
        raise ValueError(f"HOF configuration must be a mapping: {path}")
    return dict(value)


def _schema(document: Mapping[str, Any], *, name: str) -> None:
    version = document.get("schema_version")
    if isinstance(version, bool) or version != HOF_ADAPTER_SCHEMA_VERSION:
        raise ValueError(f"unsupported {name} schema_version")


def _mapping(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return dict(value)


def _integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _boolean(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _required_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _strings(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{name} must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    result = tuple(item.strip() for item in value)
    if not all(result) or len(set(result)) != len(result):
        raise ValueError(f"{name} must contain unique non-empty strings")
    return result


def _resolve_method(
    methods: Mapping[str, Any], method_ref: str, *, stack: tuple[str, ...] = ()
) -> dict[str, Any]:
    if method_ref in stack:
        raise ValueError(
            "cyclic HOF method_ref chain: " + " -> ".join((*stack, method_ref))
        )
    raw = methods.get(method_ref)
    if not isinstance(raw, Mapping):
        raise ValueError(f"HOF method_ref is not defined: {method_ref}")
    selected = dict(raw)
    parent = selected.pop("method_ref", None)
    if parent is None:
        return selected
    if not isinstance(parent, str) or not parent:
        raise ValueError(f"method {method_ref!r} has an invalid method_ref")
    return {
        **_resolve_method(methods, parent, stack=(*stack, method_ref)),
        **selected,
    }


def _parse_fragments(
    raw: Mapping[str, Any], *, atom_index_base: int
) -> tuple[HofFragment, ...]:
    fragments: list[HofFragment] = []
    for fragment_id, value in raw.items():
        fragment = _mapping(value, name=f"fragment {fragment_id}")
        indices = fragment.get("atom_indices")
        if (
            not isinstance(indices, Sequence)
            or isinstance(indices, (str, bytes, bytearray))
            or not indices
        ):
            raise ValueError(f"fragment {fragment_id!r} atom_indices must be a list")
        parsed_indices = tuple(
            _integer(index, name=f"fragment {fragment_id!r} atom index")
            - atom_index_base
            for index in indices
        )
        fragments.append(
            HofFragment(
                str(fragment_id),
                parsed_indices,
                _integer(
                    fragment.get("charge"), name=f"fragment {fragment_id!r} charge"
                ),
                _integer(
                    fragment.get("multiplicity"),
                    name=f"fragment {fragment_id!r} multiplicity",
                ),
            )
        )
    return tuple(fragments)


def _parse_hydrogen_bonds(
    raw: Mapping[str, Any], *, atom_index_base: int
) -> tuple[int, bool, tuple[HofHydrogenBond, ...]]:
    expected_count = _integer(
        raw.get("expected_count"), name="hydrogen_bonds.expected_count"
    )
    symmetry_equivalent = _boolean(
        raw.get("symmetry_equivalent"),
        name="hydrogen_bonds.symmetry_equivalent",
    )
    values = raw.get("bonds")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError("hydrogen_bonds.bonds must be a list")
    bonds: list[HofHydrogenBond] = []
    reserved = {"id", "donor_atom", "hydrogen_atom", "acceptor_atom"}
    for index, value in enumerate(values):
        bond = _mapping(value, name=f"hydrogen_bonds.bonds[{index}]")
        bond_id = bond.get("id")
        if not isinstance(bond_id, str) or not bond_id:
            raise ValueError(f"hydrogen_bonds.bonds[{index}].id is required")
        bonds.append(
            HofHydrogenBond(
                bond_id,
                _integer(bond.get("donor_atom"), name=f"{bond_id}.donor_atom")
                - atom_index_base,
                _integer(bond.get("hydrogen_atom"), name=f"{bond_id}.hydrogen_atom")
                - atom_index_base,
                _integer(bond.get("acceptor_atom"), name=f"{bond_id}.acceptor_atom")
                - atom_index_base,
                {key: item for key, item in bond.items() if key not in reserved},
            )
        )
    return expected_count, symmetry_equivalent, tuple(bonds)


def _parse_system(
    systems_document: Mapping[str, Any],
    *,
    system_id: str,
    project_root: Path,
    structure_override: Path | None,
    geometry: XYZGeometry | None,
) -> HofSystem:
    _schema(systems_document, name="HOF systems")
    systems = _mapping(systems_document.get("systems"), name="systems")
    selected = _mapping(systems.get(system_id), name=f"systems.{system_id}")
    structure = _mapping(
        selected.get("structure"), name=f"systems.{system_id}.structure"
    )
    structure_input = structure.get("input")
    if not isinstance(structure_input, str) or not structure_input:
        raise ValueError(f"systems.{system_id}.structure.input is required")
    structure_path = Path(structure_override or structure_input).expanduser()
    if not structure_path.is_absolute():
        structure_path = project_root / structure_path
    structure_path = structure_path.resolve()
    selected_geometry = geometry if geometry is not None else read_xyz(structure_path)

    atom_index_base = _integer(
        selected.get("atom_index_base"), name=f"systems.{system_id}.atom_index_base"
    )
    fragments = _parse_fragments(
        _mapping(selected.get("fragments"), name=f"systems.{system_id}.fragments"),
        atom_index_base=atom_index_base,
    )
    expected_count, symmetry_equivalent, bonds = _parse_hydrogen_bonds(
        _mapping(
            selected.get("hydrogen_bonds"),
            name=f"systems.{system_id}.hydrogen_bonds",
        ),
        atom_index_base=atom_index_base,
    )
    system = HofSystem(
        system_id=system_id,
        label=str(selected.get("label", system_id)),
        structure_path=structure_path,
        geometry=selected_geometry,
        atom_index_base=atom_index_base,
        charge=_integer(selected.get("charge"), name=f"systems.{system_id}.charge"),
        multiplicity=_integer(
            selected.get("multiplicity"), name=f"systems.{system_id}.multiplicity"
        ),
        fragments=fragments,
        hydrogen_bonds=bonds,
        expected_hydrogen_bonds=expected_count,
        symmetry_equivalent=symmetry_equivalent,
    )
    validate_hof_system(system)
    return system


def _parse_interaction_protocol(
    methods_document: Mapping[str, Any], protocol_document: Mapping[str, Any]
) -> HofInteractionProtocol:
    _schema(methods_document, name="HOF methods")
    _schema(protocol_document, name="HOF protocol")
    workflow = _mapping(protocol_document.get("workflow"), name="workflow")
    interaction = _mapping(
        workflow.get("interaction_energy"), name="workflow.interaction_energy"
    )
    if (
        _boolean(interaction.get("enabled"), name="workflow.interaction_energy.enabled")
        is not True
    ):
        raise ValueError("HOF interaction_energy workflow must be enabled")
    method_ref = interaction.get("method_ref")
    if not isinstance(method_ref, str) or not method_ref:
        raise ValueError("workflow.interaction_energy.method_ref is required")
    resolved = _resolve_method(methods_document, method_ref)

    program = _required_string(
        resolved.get("program"), name=f"methods.{method_ref}.program"
    )
    method = _required_string(
        resolved.get("method"), name=f"methods.{method_ref}.method"
    )
    basis = _required_string(resolved.get("basis"), name=f"methods.{method_ref}.basis")
    pno = _required_string(
        resolved.get("pno", resolved.get("pno_setting")),
        name=f"methods.{method_ref}.pno",
    )
    tight_scf = _boolean(
        resolved.get("tight_scf"), name=f"methods.{method_ref}.tight_scf"
    )
    counterpoise = _boolean(
        resolved.get("counterpoise"), name=f"methods.{method_ref}.counterpoise"
    )
    led = _boolean(resolved.get("led"), name=f"methods.{method_ref}.led")
    method_frozen = _boolean(
        resolved.get("frozen_fragments"),
        name=f"methods.{method_ref}.frozen_fragments",
    )
    workflow_frozen = _boolean(
        interaction.get("frozen_fragments"),
        name="workflow.interaction_energy.frozen_fragments",
    )
    if method_frozen != workflow_frozen:
        raise ValueError("method and workflow frozen_fragments settings conflict")
    if program.casefold() != "orca":
        raise ValueError("HOF interaction protocol currently requires program: orca")
    if not counterpoise:
        raise ValueError("HOF interaction protocol requires counterpoise: true")
    if not led:
        raise ValueError("HOF interaction protocol requires led: true")
    if "dlpno" not in "".join(method.casefold().split()):
        raise ValueError("LED-enabled HOF interaction protocol requires a DLPNO method")
    if not basis or not pno:
        raise ValueError(
            "DLPNO HOF interaction protocol requires basis and PNO settings"
        )

    outputs = _strings(
        interaction.get("outputs"), name="workflow.interaction_energy.outputs"
    )
    required_outputs = {"interaction_energy", "led"}
    calculate_deformation = _boolean(
        interaction.get("calculate_deformation_energy", False),
        name="workflow.interaction_energy.calculate_deformation_energy",
    )
    if calculate_deformation:
        required_outputs.add("deformation_energy")
    missing_outputs = sorted(required_outputs - set(outputs))
    if missing_outputs:
        raise ValueError(
            "workflow.interaction_energy.outputs is missing: "
            + ", ".join(missing_outputs)
        )
    geometry_source = interaction.get("geometry_source")
    if not isinstance(geometry_source, str) or not geometry_source:
        raise ValueError("workflow.interaction_energy.geometry_source is required")
    consumed = {
        "program",
        "method",
        "basis",
        "pno",
        "pno_setting",
        "tight_scf",
        "counterpoise",
        "led",
        "frozen_fragments",
    }
    return HofInteractionProtocol(
        method_ref=method_ref,
        program=program,
        method=method,
        basis=basis,
        pno=pno,
        tight_scf=tight_scf,
        counterpoise=counterpoise,
        led=led,
        frozen_fragments=workflow_frozen,
        geometry_source=geometry_source,
        calculate_deformation_energy=calculate_deformation,
        outputs=outputs,
        metadata={key: item for key, item in resolved.items() if key not in consumed},
    )


def configuration_from_documents(
    *,
    systems: Mapping[str, Any],
    methods: Mapping[str, Any],
    protocol: Mapping[str, Any],
    system_id: str,
    project_root: Path,
    structure_override: Path | None = None,
    geometry: XYZGeometry | None = None,
    source_files: Mapping[str, str] | None = None,
) -> HofAdapterConfiguration:
    """Translate already-loaded HOF documents into validated adapter models."""

    return HofAdapterConfiguration(
        system=_parse_system(
            systems,
            system_id=system_id,
            project_root=project_root.resolve(),
            structure_override=structure_override,
            geometry=geometry,
        ),
        interaction=_parse_interaction_protocol(methods, protocol),
        source_files=dict(source_files or {}),
    )


def load_hof_configuration(
    *,
    systems_path: Path,
    methods_path: Path,
    protocol_path: Path,
    system_id: str,
    project_root: Path | None = None,
    structure_override: Path | None = None,
) -> HofAdapterConfiguration:
    """Load the three public HOF YAML files without changing their schema."""

    systems_path = systems_path.expanduser().resolve()
    methods_path = methods_path.expanduser().resolve()
    protocol_path = protocol_path.expanduser().resolve()
    selected_root = (
        project_root.expanduser().resolve()
        if project_root is not None
        else systems_path.parent.parent
    )
    return configuration_from_documents(
        systems=load_yaml_document(systems_path),
        methods=load_yaml_document(methods_path),
        protocol=load_yaml_document(protocol_path),
        system_id=system_id,
        project_root=selected_root,
        structure_override=structure_override,
        source_files={
            "systems": str(systems_path),
            "methods": str(methods_path),
            "protocol": str(protocol_path),
        },
    )


__all__ = [
    "configuration_from_documents",
    "load_hof_configuration",
    "load_yaml_document",
]
