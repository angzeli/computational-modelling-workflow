"""Fail-closed selectable OPT / FREQ / SP molecular workflow."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from cmw.core.job import GeometryLineage
from cmw.core.provenance import atomic_write_json, read_json, stable_hash
from cmw.molecular.orca.input import (
    OrcaResources,
    OrcaStageSpec,
    make_target,
    render_orca_input,
)
from cmw.molecular.orca.job import check_reuse, write_target
from cmw.molecular.orca.status import FrequencyPolicy, StageType
from cmw.structure.xyz import XYZGeometry, geometry_hash, read_xyz, write_xyz


WORKFLOW_SCHEMA_VERSION = 1
CANONICAL_MODES: dict[str, tuple[StageType, ...]] = {
    "opt": (StageType.OPT,),
    "opt+sp": (StageType.OPT, StageType.SP),
    "opt+freq": (StageType.OPT, StageType.FREQ),
    "opt+freq+sp": (StageType.OPT, StageType.FREQ, StageType.SP),
}
MODE_ALIASES = {
    "opt-sp": "opt+sp",
    "opt-freq": "opt+freq",
    "opt-freq-sp": "opt+freq+sp",
}
ALL_STAGES = (StageType.OPT, StageType.FREQ, StageType.SP)


@dataclass(frozen=True)
class WorkflowConfig:
    charge: int
    multiplicity: int
    orca_exe: str
    resources: OrcaResources
    validation: FrequencyPolicy
    stages: Mapping[StageType, OrcaStageSpec]
    output_root: str | None = None


def canonical_mode(value: str) -> str:
    mode = MODE_ALIASES.get(value, value)
    if mode not in CANONICAL_MODES:
        allowed = ", ".join(CANONICAL_MODES)
        raise ValueError(f"unsupported workflow mode {value!r}; expected one of: {allowed}")
    return mode


def _bool(value: object, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"0", "1", "false", "true", "no", "yes"}:
        return value.lower() in {"1", "true", "yes"}
    raise ValueError(f"{name} must be a boolean")


def _optional_int(value: str | int | None, fallback: int, *, name: str) -> int:
    try:
        return fallback if value in (None, "") else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _optional_float(value: str | float | None, fallback: float, *, name: str) -> float:
    try:
        return fallback if value in (None, "") else float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc


def load_workflow_config(
    path: Path,
    *,
    env: Mapping[str, str] = os.environ,
    orca_exe: str | None = None,
    nprocs: int | None = None,
    maxcore_mb: int | None = None,
    require_minimum: bool | None = None,
    imaginary_tolerance_cm1: float | None = None,
) -> WorkflowConfig:
    """Load dependency-free JSON with CLI > environment > config > defaults."""

    data = read_json(path)
    if data.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
        raise ValueError("unsupported workflow configuration schema")
    resources = data.get("resources", {})
    validation = data.get("validation", {})
    stages_data = data.get("stages")
    if not isinstance(stages_data, dict):
        raise ValueError("configuration requires a stages object")
    configured_exe = str(data.get("orca_exe", "orca"))
    effective_exe = orca_exe or env.get("ORCA_EXE") or configured_exe
    effective_nprocs = _optional_int(
        nprocs if nprocs is not None else env.get("NPROCS"),
        int(resources.get("nprocs", 1)),
        name="nprocs",
    )
    effective_maxcore = _optional_int(
        maxcore_mb if maxcore_mb is not None else env.get("MAXCORE_MB"),
        int(resources.get("maxcore_mb_per_process", 1000)),
        name="maxcore",
    )
    configured_minimum = _bool(validation.get("require_minimum", False), name="require_minimum")
    env_minimum = env.get("CMW_REQUIRE_MINIMUM")
    effective_minimum = (
        require_minimum
        if require_minimum is not None
        else _bool(env_minimum, name="CMW_REQUIRE_MINIMUM")
        if env_minimum is not None
        else configured_minimum
    )
    effective_tolerance = _optional_float(
        imaginary_tolerance_cm1
        if imaginary_tolerance_cm1 is not None
        else env.get("CMW_IMAGINARY_TOLERANCE_CM1"),
        float(validation.get("imaginary_tolerance_cm1", 0.0)),
        name="imaginary frequency tolerance",
    )

    stage_specs: dict[StageType, OrcaStageSpec] = {}
    for stage in ALL_STAGES:
        raw = stages_data.get(stage.value.lower())
        if not isinstance(raw, dict):
            raise ValueError(f"configuration requires stages.{stage.value.lower()}")
        keywords = raw.get("keywords")
        blocks = raw.get("blocks", [])
        if not isinstance(keywords, str) or not isinstance(blocks, list) or not all(
            isinstance(block, str) for block in blocks
        ):
            raise ValueError(f"invalid settings for stages.{stage.value.lower()}")
        stage_specs[stage] = OrcaStageSpec(stage, keywords, tuple(blocks))

    charge = int(data.get("charge", 0))
    multiplicity = int(data.get("multiplicity", 1))
    if multiplicity < 1:
        raise ValueError("multiplicity must be positive")
    return WorkflowConfig(
        charge=charge,
        multiplicity=multiplicity,
        orca_exe=effective_exe,
        resources=OrcaResources(effective_nprocs, effective_maxcore),
        validation=FrequencyPolicy(effective_minimum, effective_tolerance),
        stages=stage_specs,
        output_root=str(data["output_root"]) if data.get("output_root") else None,
    )


def _stage_settings(config: WorkflowConfig, stage: StageType) -> dict[str, object]:
    settings = config.stages[stage].scientific_identity()
    if stage is StageType.FREQ:
        settings["validation_policy"] = asdict(config.validation)
    return settings


def _make_stage_target(
    config: WorkflowConfig, stage: StageType, geometry: XYZGeometry
):
    validation_policy = asdict(config.validation) if stage is StageType.FREQ else None
    return make_target(
        geometry,
        charge=config.charge,
        multiplicity=config.multiplicity,
        spec=config.stages[stage],
        validation_policy=validation_policy,
    )


def _workflow_target_id(mode: str, geometry: XYZGeometry, config: WorkflowConfig) -> str:
    return stable_hash(
        {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "mode": mode,
            "input_geometry_sha256": geometry_hash(geometry),
            "charge": config.charge,
            "multiplicity": config.multiplicity,
            "stages": {
                stage.value: _stage_settings(config, stage)
                for stage in CANONICAL_MODES[mode]
            },
            "validation": asdict(config.validation),
        }
    )


def _resolved_output(config: WorkflowConfig, output_override: Path | None) -> Path:
    selected = output_override or (Path(config.output_root) if config.output_root else None)
    if selected is None:
        raise ValueError("output root is required by --output or configuration")
    return selected.expanduser().resolve()


def build_state(
    *,
    mode: str,
    structure_path: Path,
    config_path: Path,
    output_override: Path | None = None,
    env: Mapping[str, str] = os.environ,
    orca_exe: str | None = None,
    nprocs: int | None = None,
    maxcore_mb: int | None = None,
    require_minimum: bool | None = None,
    imaginary_tolerance_cm1: float | None = None,
) -> dict[str, Any]:
    selected_mode = canonical_mode(mode)
    structure = read_xyz(structure_path.resolve())
    config = load_workflow_config(
        config_path.resolve(),
        env=env,
        orca_exe=orca_exe,
        nprocs=nprocs,
        maxcore_mb=maxcore_mb,
        require_minimum=require_minimum,
        imaginary_tolerance_cm1=imaginary_tolerance_cm1,
    )
    output = _resolved_output(config, output_override)
    selected = CANONICAL_MODES[selected_mode]
    workflow_target_id = _workflow_target_id(selected_mode, structure, config)
    stage_records: dict[str, dict[str, Any]] = {}
    for stage in ALL_STAGES:
        chosen = stage in selected
        depends_on: list[str] = []
        if stage is StageType.FREQ:
            depends_on = [StageType.OPT.value]
        elif stage is StageType.SP:
            depends_on = [StageType.OPT.value]
            if StageType.FREQ in selected:
                depends_on.append(StageType.FREQ.value)
        stage_records[stage.value] = {
            "selected": chosen,
            "status": "PENDING" if chosen else "SKIPPED",
            "depends_on": depends_on,
            "geometry_source": "input_structure" if stage is StageType.OPT else "validated_OPT_geometry",
            "settings": _stage_settings(config, stage),
            "target_id": None,
            "attempt_id": None,
            "disposition": "pending" if chosen else "skipped",
            "artifacts": {},
            "lineage": None,
            "reason": "",
        }
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "workflow": "opt_freq_sp",
        "mode": selected_mode,
        "workflow_target_id": workflow_target_id,
        "input_structure": {
            "path": str(structure_path.resolve()),
            "geometry_sha256": geometry_hash(structure),
            "atom_count": structure.atom_count,
        },
        "config_path": str(config_path.resolve()),
        "output_root": str(output),
        "electronic_state": {"charge": config.charge, "multiplicity": config.multiplicity},
        "resources": config.resources.to_dict(),
        "orca_exe": config.orca_exe,
        "validation": asdict(config.validation),
        "stage_order": [stage.value for stage in selected],
        "stages": stage_records,
        "reused_stages": [],
        "executed_stages": [],
        "skipped_stages": [stage.value for stage in ALL_STAGES if stage not in selected],
        "scientifically_valid": False,
        "status": "PLANNED",
        "failure_point": None,
        "result_path": str(output / "workflows" / workflow_target_id / "workflow_result.json"),
    }


def _metadata_artifact_path(metadata_path: Path, artifact: Mapping[str, Any]) -> Path:
    path = Path(str(artifact["path"]))
    return path if path.is_absolute() else metadata_path.parent / path


def _find_reusable(target_path: Path) -> tuple[Path, dict[str, Any]] | None:
    attempt_root = target_path.parent / "attempts"
    if not attempt_root.is_dir():
        return None
    for metadata in sorted(attempt_root.glob("*/job.json"), reverse=True):
        decision = check_reuse(target_path, metadata)
        if decision["reuse"]:
            return metadata, read_json(metadata)
    return None


def _resolve_stage_geometry(
    state: Mapping[str, Any], stage: StageType
) -> tuple[Path, GeometryLineage]:
    if stage is StageType.OPT:
        path = Path(state["input_structure"]["path"])
        geometry_sha = str(state["input_structure"]["geometry_sha256"])
        return path, GeometryLineage("input_structure", geometry_sha)

    opt = state["stages"][StageType.OPT.value]
    if opt["status"] != "VALID":
        raise RuntimeError("validated OPT result is required for downstream geometry")
    geometry_artifact = opt["artifacts"].get("final_geometry")
    if not geometry_artifact:
        raise RuntimeError("validated OPT final geometry is missing")
    path = Path(geometry_artifact["path"])
    geometry_sha = str(geometry_artifact["geometry_sha256"])
    return path, GeometryLineage(
        "validated_OPT_geometry",
        geometry_sha,
        StageType.OPT.value,
        str(opt["target_id"]),
        geometry_sha,
    )


def _config_from_state(state: Mapping[str, Any]) -> WorkflowConfig:
    stages = {
        StageType(name): OrcaStageSpec(
            StageType(name),
            str(record["settings"]["keywords"]),
            tuple(record["settings"].get("blocks", ())),
        )
        for name, record in state["stages"].items()
    }
    return WorkflowConfig(
        charge=int(state["electronic_state"]["charge"]),
        multiplicity=int(state["electronic_state"]["multiplicity"]),
        orca_exe=str(state["orca_exe"]),
        resources=OrcaResources(
            int(state["resources"]["nprocs"]),
            int(state["resources"]["maxcore_mb_per_process"]),
        ),
        validation=FrequencyPolicy(
            bool(state["validation"]["require_minimum"]),
            float(state["validation"]["imaginary_tolerance_cm1"]),
        ),
        stages=stages,
        output_root=str(state["output_root"]),
    )


def _artifact_locations(metadata_path: Path, metadata: Mapping[str, Any]) -> dict[str, Any]:
    locations = {
        role: {
            **artifact,
            "path": str(_metadata_artifact_path(metadata_path, artifact).resolve()),
        }
        for role, artifact in metadata["artifacts"].items()
    }
    final_geometry = locations.get("final_geometry")
    if final_geometry:
        final_geometry["geometry_sha256"] = geometry_hash(
            read_xyz(Path(final_geometry["path"]))
        )
    return locations


def _record_valid_stage(
    state: dict[str, Any],
    stage: StageType,
    *,
    target_id: str,
    metadata_path: Path,
    metadata: Mapping[str, Any],
    disposition: str,
) -> None:
    record = state["stages"][stage.value]
    record.update(
        {
            "status": "VALID",
            "target_id": target_id,
            "attempt_id": metadata["attempt"]["attempt_id"],
            "disposition": disposition,
            "metadata": str(metadata_path.resolve()),
            "artifacts": _artifact_locations(metadata_path, metadata),
            "lineage": metadata["lineage"],
            "execution": metadata["execution"],
            "scientific": metadata["scientific"],
            "reason": "",
        }
    )
    collection = state["reused_stages"] if disposition == "reused" else state["executed_stages"]
    if stage.value not in collection:
        collection.append(stage.value)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_json(path, state)
    atomic_write_json(Path(state["result_path"]), state)


def initialize_state(
    path: Path, state: dict[str, Any], *, restart_incomplete: bool = False
) -> None:
    """Create workflow state without replacing a different workflow target."""

    if path.exists():
        existing = read_json(path)
        if existing.get("workflow_target_id") == state["workflow_target_id"]:
            if restart_incomplete:
                for stage_name in state["stage_order"]:
                    prior = existing["stages"][stage_name]
                    if prior["status"] == "VALID":
                        state["stages"][stage_name] = prior
                state["reused_stages"] = list(existing.get("reused_stages", ()))
                state["executed_stages"] = list(existing.get("executed_stages", ()))
                state.update(status="PLANNED", failure_point=None, scientifically_valid=False)
                _write_state(path, state)
            return
        raise FileExistsError(
            "output already contains a different workflow target; choose another output root"
        )
    Path(state["output_root"]).mkdir(parents=True, exist_ok=True)
    _write_state(path, state)


def next_action(state_path: Path) -> tuple[str, dict[str, Any]]:
    """Reuse valid work or prepare exactly one next executable stage."""

    state = read_json(state_path)
    config = _config_from_state(state)
    for stage_name in state["stage_order"]:
        stage = StageType(stage_name)
        stage_record = state["stages"][stage.value]
        if stage_record["status"] == "VALID":
            continue
        if stage_record["status"] == "FAILED":
            return "FAILED", {"failure_point": stage.value, "reason": stage_record["reason"]}
        for dependency in stage_record["depends_on"]:
            if state["stages"][dependency]["status"] != "VALID":
                return "FAILED", {"failure_point": dependency, "reason": "required dependency is invalid"}

        geometry_path, lineage = _resolve_stage_geometry(state, stage)
        geometry = read_xyz(geometry_path)
        if geometry_hash(geometry) != lineage.geometry_sha256:
            state.update(status="FAILED", failure_point=stage.value, scientifically_valid=False)
            stage_record.update(status="FAILED", reason="geometry lineage hash mismatch")
            _write_state(state_path, state)
            return "FAILED", {"failure_point": stage.value, "reason": stage_record["reason"]}
        target = _make_stage_target(config, stage, geometry)
        target_dir = Path(state["output_root"]) / "stages" / stage.value.lower() / target.target_id
        target_path = target_dir / "target.json"
        if not target_path.exists():
            write_target(target_path, target, lineage)
        reusable = _find_reusable(target_path)
        if reusable is not None:
            metadata_path, metadata = reusable
            _record_valid_stage(
                state,
                stage,
                target_id=target.target_id,
                metadata_path=metadata_path,
                metadata=metadata,
                disposition="reused",
            )
            _write_state(state_path, state)
            continue

        if stage_record["status"] == "READY":
            return "RUN", dict(stage_record["runner"])
        attempt_id = uuid4().hex
        attempt_dir = target_dir / "attempts" / attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=False)
        input_geometry = attempt_dir / "input.xyz"
        input_path = attempt_dir / "stage.inp"
        output_path = attempt_dir / "stage.out"
        stderr_path = attempt_dir / "stage.err"
        metadata_path = attempt_dir / "job.json"
        final_geometry = attempt_dir / "stage.xyz" if stage is StageType.OPT else None
        write_xyz(input_geometry, geometry, comment=f"CMW {stage.value} attempt input")
        input_path.write_text(
            render_orca_input(
                geometry_path=Path(input_geometry.name),
                charge=config.charge,
                multiplicity=config.multiplicity,
                spec=config.stages[stage],
                resources=config.resources,
            ),
            encoding="utf-8",
        )
        write_target(target_path, target, lineage)
        runner = {
            "stage": stage.value,
            "target_id": target.target_id,
            "attempt_id": attempt_id,
            "input": str(input_path.resolve()),
            "target": str(target_path.resolve()),
            "metadata": str(metadata_path.resolve()),
            "output": str(output_path.resolve()),
            "stderr": str(stderr_path.resolve()),
            "final_geometry": str(final_geometry.resolve()) if final_geometry else "",
            "orca_exe": config.orca_exe,
            "nprocs": config.resources.nprocs,
            "maxcore_mb_per_process": config.resources.maxcore_mb_per_process,
            "require_minimum": config.validation.require_minimum,
            "imaginary_tolerance_cm1": config.validation.imaginary_tolerance_cm1,
        }
        stage_record.update(
            status="READY",
            target_id=target.target_id,
            attempt_id=attempt_id,
            disposition="new_attempt",
            lineage=asdict(lineage),
            runner=runner,
        )
        state["status"] = "RUNNING"
        _write_state(state_path, state)
        return "RUN", runner

    state["status"] = "COMPLETE"
    state["scientifically_valid"] = True
    state["failure_point"] = None
    _write_state(state_path, state)
    return "COMPLETE", {"result_path": state["result_path"]}


def advance_state(state_path: Path, *, stage: StageType) -> tuple[bool, dict[str, Any]]:
    """Record one runner outcome and stop the workflow on any invalid stage."""

    state = read_json(state_path)
    record = state["stages"][stage.value]
    if record["status"] != "READY":
        raise ValueError(f"stage {stage.value} is not awaiting an execution result")
    runner = record["runner"]
    target_path = Path(runner["target"])
    metadata_path = Path(runner["metadata"])
    decision = check_reuse(target_path, metadata_path)
    if not decision["reuse"]:
        record.update(status="FAILED", reason=decision["reason"])
        state.update(status="FAILED", failure_point=stage.value, scientifically_valid=False)
        _write_state(state_path, state)
        return False, decision
    metadata = read_json(metadata_path)
    _record_valid_stage(
        state,
        stage,
        target_id=str(runner["target_id"]),
        metadata_path=metadata_path,
        metadata=metadata,
        disposition="executed",
    )
    record.pop("runner", None)
    _write_state(state_path, state)
    return True, decision


def plan_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Inspect safely determinable reuse without preparing or launching attempts."""

    planned = json.loads(json.dumps(state))
    config = _config_from_state(planned)
    opt_geometry = read_xyz(Path(planned["input_structure"]["path"]))
    resolved_opt: tuple[Path, dict[str, Any], str] | None = None
    prerequisite_ready = True
    for stage_name in planned["stage_order"]:
        stage = StageType(stage_name)
        record = planned["stages"][stage.value]
        if stage is StageType.OPT:
            geometry = opt_geometry
            lineage = GeometryLineage("input_structure", geometry_hash(geometry))
        elif resolved_opt is not None:
            metadata_path, opt_metadata, opt_target_id = resolved_opt
            artifact = opt_metadata["artifacts"]["final_geometry"]
            geometry_path = _metadata_artifact_path(metadata_path, artifact)
            geometry = read_xyz(geometry_path)
            geometry_sha = geometry_hash(geometry)
            lineage = GeometryLineage(
                "validated_OPT_geometry", geometry_sha, "OPT", opt_target_id, geometry_sha
            )
        else:
            record.update(
                disposition="pending",
                reason="target awaits a validated OPT geometry",
                target_id=None,
            )
            prerequisite_ready = False
            continue
        target = _make_stage_target(config, stage, geometry)
        target_path = (
            Path(planned["output_root"])
            / "stages"
            / stage.value.lower()
            / target.target_id
            / "target.json"
        )
        record["target_id"] = target.target_id
        reusable = _find_reusable(target_path) if target_path.exists() else None
        if not prerequisite_ready:
            record.update(disposition="pending", reason="required prior stage is not yet valid")
        elif reusable:
            metadata_path, metadata = reusable
            record.update(disposition="reuse", reason="exact validated result is reusable")
            if stage is StageType.OPT:
                resolved_opt = (metadata_path, metadata, target.target_id)
        else:
            record.update(disposition="run", reason="no exact validated reusable result")
            prerequisite_ready = False
        record["lineage"] = asdict(lineage)
    return planned


def human_plan(state: Mapping[str, Any]) -> str:
    lines = [
        f"Mode: {state['mode']}",
        f"Structure: {state['input_structure']['path']}",
        f"Geometry SHA-256: {state['input_structure']['geometry_sha256']}",
        f"Charge / multiplicity: {state['electronic_state']['charge']} / {state['electronic_state']['multiplicity']}",
        f"ORCA executable: {state['orca_exe']}",
        f"Resources: {state['resources']['nprocs']} processes, {state['resources']['maxcore_mb_per_process']} MB MaxCore/process",
        f"Output: {state['output_root']}",
        "Stages:",
    ]
    for stage in ALL_STAGES:
        record = state["stages"][stage.value]
        settings = record["settings"]
        dependencies = ",".join(record["depends_on"]) or "none"
        lines.append(
            f"  {stage.value}: {record['disposition']} | dependencies={dependencies} | "
            f"geometry={record['geometry_source']} | keywords={settings['keywords']}"
        )
        if record.get("reason"):
            lines.append(f"    {record['reason']}")
    validation = state["validation"]
    lines.append(
        "Frequency policy: "
        f"require_minimum={str(validation['require_minimum']).lower()}, "
        f"imaginary_tolerance_cm-1={validation['imaginary_tolerance_cm1']}"
    )
    return "\n".join(lines)
