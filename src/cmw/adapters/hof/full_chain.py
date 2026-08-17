"""Prepare non-running, resumable OPT-to-IGMH HOF production queues."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shlex
import shutil
from typing import Any, Mapping, Sequence

from cmw.core.provenance import atomic_write_json, file_hash, stable_hash
from cmw.core.storage import check_storage_capacity
from cmw.molecular.multiwfn.runtime import inspect_runtime_compatibility
from cmw.molecular.orca.runtime import prepare_orca_runtime

from .command_queue import _git_head, _guarded_script, _quote, _write_script
from .config import load_hof_configuration
from .execution import materialize_hof_orca_node
from .planner import build_hof_workflow_plan
from .workflow import _node_token


FULL_CHAIN_SCHEMA_VERSION = 1


def _state_result(python_bin: Path, state: Path) -> str:
    return (
        f'$({_quote(python_bin)} -c '
        + shlex.quote(
            "import json,sys; print(json.load(open(sys.argv[1]))['result_path'])"
        )
        + f" {_quote(state)})"
    )


def _prepare_node_script(
    *,
    common: Sequence[str],
    node_id: str,
    state_path: Path,
    optimization_state: Path,
    frequency_state: Path | None = None,
) -> str:
    arguments = [*common, "--node", node_id]
    arguments.extend(("--optimization-result", "__OPT_RESULT__"))
    if frequency_state is not None:
        arguments.extend(("--frequency-result", "__FREQ_RESULT__"))
    command = shlex.join(arguments)
    command = command.replace("__OPT_RESULT__", '"$optimization_result"')
    command = command.replace("__FREQ_RESULT__", '"$frequency_result"')
    body = [
        f"optimization_result={_state_result(Path(common[0]), optimization_state)}",
    ]
    if frequency_state is not None:
        body.append(
            f"frequency_result={_state_result(Path(common[0]), frequency_state)}"
        )
    body.extend(
        (
            "set +e",
            f"payload=$({command})",
            "status=$?",
            "set -e",
            "if ((status != 0 && status != 10)); then printf '%s\\n' \"$payload\" >&2; exit \"$status\"; fi",
            f"printf '%s\\n' \"$payload\" > {_quote(state_path)}",
            "if ((status == 10)); then exit 0; fi",
            "command_path=$(printf '%s' \"$payload\" | "
            + _quote(common[0])
            + " -c 'import json,sys; print(json.load(sys.stdin)[\"command_path\"])')",
            '"$command_path"',
        )
    )
    return _guarded_script("\n".join(body))


def _prepare_initial_script(
    *, common: Sequence[str], state_path: Path
) -> str:
    command = shlex.join((*common, "--node", "geometry_optimization"))
    python_bin = Path(common[0])
    return _guarded_script(
        "\n".join(
            (
                "set +e",
                f"payload=$({command})",
                "status=$?",
                "set -e",
                "if ((status != 0 && status != 10)); then printf '%s\\n' \"$payload\" >&2; exit \"$status\"; fi",
                f"printf '%s\\n' \"$payload\" > {_quote(state_path)}",
                "if ((status == 10)); then exit 0; fi",
                "command_path=$(printf '%s' \"$payload\" | "
                + _quote(python_bin)
                + " -c 'import json,sys; print(json.load(sys.stdin)[\"command_path\"])')",
                '"$command_path"',
            )
        )
    )


def _promote_script(
    *,
    python_bin: Path,
    system_id: str,
    optimization_state: Path,
    frequency_state: Path,
    output_path: Path,
) -> str:
    command = shlex.join(
        (
            str(python_bin),
            "-m",
            "cmw.adapters.hof.execution_cli",
            "promote-geometry",
            "--optimization-result",
            "__OPT_RESULT__",
            "--frequency-result",
            "__FREQ_RESULT__",
            "--system",
            system_id,
            "--output",
            str(output_path),
        )
    ).replace("__OPT_RESULT__", '"$optimization_result"').replace(
        "__FREQ_RESULT__", '"$frequency_result"'
    )
    return _guarded_script(
        "\n".join(
            (
                f"optimization_result={_state_result(python_bin, optimization_state)}",
                f"frequency_result={_state_result(python_bin, frequency_state)}",
                command,
            )
        )
    )


def _relaxed_energy_script(
    *,
    common: Sequence[str],
    fragment_id: str,
    relaxation_state: Path,
    runtime_contract: Path,
    state_path: Path,
) -> str:
    python_bin = Path(common[0])
    relaxation_result = _state_result(python_bin, relaxation_state)
    base = list(common)
    prepare_index = base.index("prepare-node")
    base[prepare_index] = "prepare-relaxed-energy"
    node_index = base.index("--node") if "--node" in base else -1
    if node_index >= 0:
        del base[node_index : node_index + 2]
    arguments = [
        *base,
        "--fragment",
        fragment_id,
        "--relaxation-result",
        "__RELAX_RESULT__",
        "--runtime-contract",
        str(runtime_contract),
    ]
    # The common arguments already contain the runtime contract.
    first = arguments.index("--runtime-contract")
    second = arguments.index("--runtime-contract", first + 1)
    del arguments[first : first + 2]
    command = shlex.join(arguments).replace(
        "__RELAX_RESULT__", '"$relaxation_result"'
    )
    return _guarded_script(
        "\n".join(
            (
                f"relaxation_result={relaxation_result}",
                "set +e",
                f"payload=$({command})",
                "status=$?",
                "set -e",
                "if ((status != 0 && status != 10)); then printf '%s\\n' \"$payload\" >&2; exit \"$status\"; fi",
                f"printf '%s\\n' \"$payload\" > {_quote(state_path)}",
                "if ((status == 10)); then exit 0; fi",
                "command_path=$(printf '%s' \"$payload\" | "
                + _quote(python_bin)
                + " -c 'import json,sys; print(json.load(sys.stdin)[\"command_path\"])')",
                '"$command_path"',
            )
        )
    )


def _conversion_script(
    *,
    cmw_root: Path,
    python_bin: Path,
    density_state: Path,
    output: Path,
    converter: Path,
) -> str:
    density_result = _state_result(python_bin, density_state)
    command = shlex.join(
        (
            str(cmw_root / "scripts/orca/convert_orca_wavefunction.sh"),
            "--source",
            "__DENSITY_RESULT__",
            "--output",
            str(output),
            "--converter",
            str(converter),
            "--spin-mode",
            "restricted",
        )
    ).replace("__DENSITY_RESULT__", '"$density_result"')
    return _guarded_script(
        f"density_result={density_result}\nexport PYTHON_BIN={_quote(python_bin)}\n{command}"
    )


def _multiwfn_script(
    *,
    cmw_root: Path,
    python_bin: Path,
    density_state: Path,
    output: Path,
    converter: Path,
    multiwfn_executable: Path,
    settings: Path,
    fragments: Path,
    configuration: Path,
    threads: int,
) -> str:
    density_result = _state_result(python_bin, density_state)
    plan = shlex.join(
        (
            str(python_bin),
            "-m",
            "cmw.molecular.orca.conversion_cli",
            "plan",
            "--source",
            "__DENSITY_RESULT__",
            "--output",
            str(output),
            "--converter",
            str(converter),
            "--spin-mode",
            "restricted",
        )
    ).replace("__DENSITY_RESULT__", '"$density_result"')
    run = shlex.join(
        (
            str(cmw_root / "scripts/workflows/generate_igmh_cubes.sh"),
            "--source",
            "__SOURCE_RESULT__",
            "--output",
            str(output),
            "--fragments",
            str(fragments),
            "--config",
            str(configuration),
            "--multiwfn-exe",
            str(multiwfn_executable),
            "--settings-source",
            str(settings),
            "--threads",
            str(threads),
        )
    ).replace("__SOURCE_RESULT__", '"$source_result"')
    return _guarded_script(
        "\n".join(
            (
                f"density_result={density_result}",
                "set +e",
                f"conversion_plan=$({plan})",
                "conversion_status=$?",
                "set -e",
                "if ((conversion_status == 0)); then",
                "  printf 'ORCA-to-Molden result is not reusable; run the conversion step before IGMH\\n' >&2",
                "  printf '%s\\n' \"$conversion_plan\" >&2",
                "  exit 70",
                "fi",
                "if ((conversion_status != 10)); then",
                "  printf 'ORCA-to-Molden planning failed before IGMH\\n' >&2",
                "  printf '%s\\n' \"$conversion_plan\" >&2",
                "  exit \"$conversion_status\"",
                "fi",
                "source_result=$(printf '%s' \"$conversion_plan\" | "
                + _quote(python_bin)
                + " -c 'import json,sys; print(json.load(sys.stdin)[\"result_path\"])')",
                f"export PYTHON_BIN={_quote(python_bin)}",
                run,
            )
        )
    )


def _master_script(
    *,
    cmw_root: Path,
    python_bin: Path,
    queue_id: str,
    project_root: Path,
    minimum_free_gb: float,
    steps: Sequence[Mapping[str, Any]],
    python_runtime: Path,
) -> str:
    scripts = " ".join(_quote(str(step["script"])) for step in steps)
    labels = " ".join(_quote(str(step["id"])) for step in steps)
    return f"""#!/usr/bin/env bash
set -euo pipefail
QUEUE_DIR=$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd -P)
PYTHON_BIN={_quote(python_bin)}
export PYTHONPATH={_quote(cmw_root / 'src')}:{_quote(python_runtime)}${{PYTHONPATH:+:$PYTHONPATH}}
if [[ ${{1:---status}} != --run || ${{2:-}} != --confirm-expensive ]]; then
  "$PYTHON_BIN" -m cmw.adapters.hof.command_queue_cli verify \
    --queue "$QUEUE_DIR/command-queue.json" \
    --authorization "$QUEUE_DIR/queue-authorization.json"
  printf 'Use --run --confirm-expensive only after this exact queue is authorized.\n'
  exit 0
fi
"$PYTHON_BIN" -m cmw.adapters.hof.command_queue_cli verify \
  --queue "$QUEUE_DIR/command-queue.json" \
    --authorization "$QUEUE_DIR/queue-authorization.json" --require-authorized
scientific_processes() {{
  ps -axo pid=,comm= | while read -r pid command; do
    name=${{command##*/}}
    case "$name" in
      orca|orca_*|Multiwfn|mpirun) printf '%s %s\n' "$pid" "$command" ;;
    esac
  done
}}
scripts=({scripts})
labels=({labels})
export CMW_HOF_EXPECTED_TOKEN={_quote(queue_id)}
export CMW_HOF_QUEUE_RUN_TOKEN={_quote(queue_id)}
for index in "${{!scripts[@]}}"; do
  conflicts=$(scientific_processes)
  if [[ -n "$conflicts" ]]; then
    printf 'Conflicting scientific process before %s:\n%s\n' \
      "${{labels[$index]}}" "$conflicts" >&2
    exit 73
  fi
  "$PYTHON_BIN" -m cmw.core.storage_cli --path {_quote(project_root)} \
    --minimum-free-gb {minimum_free_gb}
  printf '[QUEUE %02d/%02d] %s\n' "$((index + 1))" "${{#scripts[@]}}" "${{labels[$index]}}"
  "$QUEUE_DIR/${{scripts[$index]}}"
done
printf '[QUEUE COMPLETE] %s\n' {_quote(queue_id)}
"""


def generate_hof_full_chain(
    *,
    system_id: str,
    hof_root: Path,
    cmw_root: Path,
    preflight_directory: Path,
    queue_directory: Path,
    python_bin: Path,
    orca_executable: Path,
    converter: Path,
    multiwfn_executable: Path,
    multiwfn_settings: Path,
    python_library_source: Path,
) -> dict[str, object]:
    """Generate one immutable, non-authorized OPT-to-IGMH package and queue."""

    hof_root = hof_root.expanduser().resolve(strict=True)
    cmw_root = cmw_root.expanduser().resolve(strict=True)
    preflight = preflight_directory.expanduser().resolve()
    queue = queue_directory.expanduser().resolve()
    if preflight.exists() or queue.exists():
        raise FileExistsError("full-chain preflight or queue output already exists")
    for path in (python_bin, orca_executable, converter, multiwfn_executable, multiwfn_settings):
        if not path.expanduser().resolve().is_file():
            raise FileNotFoundError(f"required executable or settings file is missing: {path}")
    python_bin = python_bin.expanduser().resolve()
    orca_executable = orca_executable.expanduser().resolve()
    converter = converter.expanduser().resolve()
    multiwfn_executable = multiwfn_executable.expanduser().resolve()
    multiwfn_settings = multiwfn_settings.expanduser().resolve()
    python_library_source = python_library_source.expanduser().resolve(strict=True)
    if not (python_library_source / "yaml/__init__.py").is_file():
        raise ValueError("python library source must contain the yaml package")
    config = hof_root / "config"
    configuration = load_hof_configuration(
        systems_path=config / "systems.yaml",
        methods_path=config / "methods.yaml",
        protocol_path=config / "protocol.yaml",
        execution_path=config / "execution.yaml",
        system_id=system_id,
        project_root=hof_root,
    )
    profile = configuration.execution_profile
    if profile is None or profile.storage is None:
        raise ValueError("production full-chain planning requires a storage policy")
    storage = check_storage_capacity(
        hof_root, minimum_free_gb=profile.storage.minimum_free_gb
    )
    multiwfn_runtime = inspect_runtime_compatibility(
        executable=str(multiwfn_executable),
        settings_source=multiwfn_settings,
        threads=profile.multiwfn.nthreads,
        environment={},
    )
    plan = build_hof_workflow_plan(configuration)
    preflight.mkdir(parents=True)
    python_runtime = preflight / "python-runtime"
    shutil.copytree(
        python_library_source / "yaml",
        python_runtime / "yaml",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_yaml*.so"),
    )
    runtime = prepare_orca_runtime(profile, orca_executable=orca_executable)
    runtime_path = preflight / "orca-runtime.json"
    atomic_write_json(runtime_path, runtime)
    atomic_write_json(preflight / "multiwfn-runtime-preflight.json", multiwfn_runtime)
    initial = materialize_hof_orca_node(
        systems_path=config / "systems.yaml",
        methods_path=config / "methods.yaml",
        protocol_path=config / "protocol.yaml",
        execution_path=config / "execution.yaml",
        system_id=system_id,
        project_root=hof_root,
        node_id="geometry_optimization",
        runtime_contract_source=runtime_path,
        cmw_root=cmw_root,
        python_bin=python_bin,
        orca_executable=orca_executable,
    )
    atomic_write_json(preflight / "workflow-plan.json", plan.to_dict())
    atomic_write_json(preflight / "workflow-graph.json", plan.graph.to_dict())
    atomic_write_json(preflight / "storage-capacity.json", storage)
    atomic_write_json(preflight / "initial-attempt.json", initial)
    fragments = {
        "schema_version": 1,
        "indexing": "one_based",
        "fragments": {
            item.fragment_id: [index + 1 for index in item.atom_indices]
            for item in configuration.system.fragments
        },
        "require_complete_partition": True,
        "allow_overlap": False,
    }
    atomic_write_json(preflight / "igmh-fragments.json", fragments)
    igmh = dict(plan.multiwfn_plans["multiwfn_igmh"])
    igmh.pop("runtime", None)
    igmh.pop("execute", None)
    igmh.pop("fragments", None)
    igmh["schema_version"] = 1
    atomic_write_json(preflight / "igmh-configuration.json", igmh)
    command_preview = Path(str(initial["command_path"]))
    (preflight / "exact_terminal_command.sh").write_bytes(command_preview.read_bytes())
    (preflight / "exact_terminal_command.sh").chmod(0o750)
    package_files = tuple(
        path
        for path in preflight.rglob("*")
        if path.is_file() and path.name != "package-manifest.json"
    )
    manifest = {
        "schema_version": FULL_CHAIN_SCHEMA_VERSION,
        "system_id": system_id,
        "status": "PREPARED_NOT_AUTHORIZED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository_commits": {"cmw": _git_head(cmw_root), "hof": _git_head(hof_root)},
        "storage_capacity": storage,
        "multiwfn_runtime": multiwfn_runtime,
        "initial_attempt": initial,
        "files": {
            str(path.relative_to(preflight)): {
                "size_bytes": path.stat().st_size,
                "sha256": file_hash(path),
            }
            for path in package_files
        },
    }
    atomic_write_json(preflight / "package-manifest.json", manifest)

    queue.mkdir(parents=True)
    (queue / "steps").mkdir()
    (queue / "state").mkdir()
    for name in ("igmh-fragments.json", "igmh-configuration.json"):
        (queue / name).write_bytes((preflight / name).read_bytes())
    initial_state = queue / "state/geometry_optimization.json"
    atomic_write_json(initial_state, initial)
    common = (
        str(python_bin), "-m", "cmw.adapters.hof.execution_cli", "prepare-node",
        "--systems", str(config / "systems.yaml"), "--methods", str(config / "methods.yaml"),
        "--protocol", str(config / "protocol.yaml"), "--execution", str(config / "execution.yaml"),
        "--system", system_id, "--project-root", str(hof_root), "--runtime-contract", str(runtime_path),
        "--cmw-root", str(cmw_root), "--python", str(python_bin), "--orca-exe", str(orca_executable),
    )
    steps: list[dict[str, object]] = []

    def add(node: str, text: str, *, kind: str = "ORCA") -> Path:
        number = len(steps) + 1
        relative = Path("steps") / f"{number:02d}_{node}.sh"
        _write_script(queue / relative, text)
        steps.append({"id": node, "kind": kind, "script": str(relative)})
        return queue / "state" / f"{node}.json"

    add(
        "geometry_optimization",
        _prepare_initial_script(common=common, state_path=initial_state),
    )
    frequency_state = queue / "state/geometry_frequency.json"
    add(
        "geometry_frequency",
        _prepare_node_script(
            common=common, node_id="geometry_frequency", state_path=frequency_state,
            optimization_state=initial_state,
        ),
    )
    add(
        "validated_geometry",
        _promote_script(
            python_bin=python_bin, system_id=system_id, optimization_state=initial_state,
            frequency_state=frequency_state, output_path=queue / "state/validated_geometry.json",
        ),
        kind="VALIDATION",
    )
    dynamic_nodes = ["dimer", "fragment_a", "fragment_b"]
    for fragment in configuration.system.fragments:
        token = _node_token(fragment.fragment_id)
        dynamic_nodes.extend((f"distorted_fragment_{token}_energy", f"relax_fragment_{token}"))
    dynamic_nodes.append("igmh_density")
    state_paths: dict[str, Path] = {}
    for node_id in dynamic_nodes:
        state = queue / "state" / f"{node_id}.json"
        state_paths[node_id] = state
        add(
            node_id,
            _prepare_node_script(
                common=common, node_id=node_id, state_path=state,
                optimization_state=initial_state, frequency_state=frequency_state,
            ),
        )
        if node_id.startswith("relax_fragment_"):
            fragment_id = next(
                fragment.fragment_id
                for fragment in configuration.system.fragments
                if node_id == f"relax_fragment_{_node_token(fragment.fragment_id)}"
            )
            relaxed_id = f"relaxed_fragment_{_node_token(fragment_id)}_energy"
            relaxed_state = queue / "state" / f"{relaxed_id}.json"
            state_paths[relaxed_id] = relaxed_state
            add(
                relaxed_id,
                _relaxed_energy_script(
                    common=common, fragment_id=fragment_id, relaxation_state=state,
                    runtime_contract=runtime_path, state_path=relaxed_state,
                ),
            )
    analysis_root = hof_root / "calculation" / system_id / "igmh_analysis"
    density_state = state_paths["igmh_density"]
    add(
        "orca_to_molden",
        _conversion_script(
            cmw_root=cmw_root, python_bin=python_bin, density_state=density_state,
            output=analysis_root, converter=converter,
        ),
        kind="CONVERSION",
    )
    add(
        "multiwfn_igmh",
        _multiwfn_script(
            cmw_root=cmw_root, python_bin=python_bin, density_state=density_state,
            output=analysis_root, converter=converter, multiwfn_executable=multiwfn_executable,
            settings=multiwfn_settings, fragments=queue / "igmh-fragments.json",
            configuration=queue / "igmh-configuration.json", threads=profile.multiwfn.nthreads,
        ),
        kind="MULTIWFN",
    )
    for step in steps:
        script = queue / str(step["script"])
        step["script_size_bytes"] = script.stat().st_size
        step["script_sha256"] = file_hash(script)
    source_paths = (
        config / "systems.yaml", config / "methods.yaml", config / "protocol.yaml",
        config / "execution.yaml", configuration.system.structure_path,
        preflight / "package-manifest.json", runtime_path,
        cmw_root / "scripts/orca/run_orca.sh",
        cmw_root / "scripts/orca/convert_orca_wavefunction.sh",
        cmw_root / "scripts/workflows/generate_igmh_cubes.sh",
        cmw_root / "src/cmw/adapters/hof/full_chain.py",
        cmw_root / "src/cmw/adapters/hof/execution.py",
        cmw_root / "src/cmw/adapters/hof/execution_cli.py",
        cmw_root / "src/cmw/core/storage.py",
        python_bin, orca_executable, converter, multiwfn_executable, multiwfn_settings,
    )
    sources = {
        str(path): {"size_bytes": path.stat().st_size, "sha256": file_hash(path)}
        for path in source_paths
    }
    identity = {
        "schema_version": 1,
        "system_id": system_id,
        "steps": steps,
        "source_files": sources,
        "repositories": {
            "cmw": {"path": str(cmw_root), "head": _git_head(cmw_root)},
            "hof": {"path": str(hof_root), "head": _git_head(hof_root)},
        },
        "preflight_directory": str(preflight),
        "preflight_manifest_sha256": file_hash(preflight / "package-manifest.json"),
    }
    queue_id = stable_hash(identity)
    queue_record = {
        **identity,
        "queue_id": queue_id,
        "status": "PREPARED_NOT_AUTHORIZED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "storage_policy": profile.storage.to_dict(),
        "policy": {
            "one_scientific_process_at_a_time": True,
            "fail_closed": True,
            "requires_explicit_authorization": True,
            "recheck_storage_before_every_step": True,
        },
    }
    atomic_write_json(queue / "command-queue.json", queue_record)
    atomic_write_json(
        queue / "queue-authorization.json",
        {"schema_version": 1, "queue_id": queue_id, "status": "NOT_AUTHORIZED"},
    )
    _write_script(
        queue / "run_command_queue.sh",
        _master_script(
            cmw_root=cmw_root, python_bin=python_bin, queue_id=queue_id,
            project_root=hof_root, minimum_free_gb=profile.storage.minimum_free_gb,
            steps=steps, python_runtime=python_runtime,
        ),
    )
    (queue / "README.md").write_text(
        f"# {configuration.system.label} full CMW queue\n\n"
        "Status: **PREPARED_NOT_AUTHORIZED**\n\n"
        "This queue spans optimization, frequency validation, geometry promotion, "
        "interaction/deformation calculations, density conversion, and Multiwfn IGMH. "
        "It rechecks immediately allocatable storage before every step.\n",
        encoding="utf-8",
    )
    return {
        "system_id": system_id,
        "status": "PREPARED_NOT_AUTHORIZED",
        "preflight_directory": str(preflight),
        "queue_directory": str(queue),
        "queue_id": queue_id,
        "step_count": len(steps),
        "initial_attempt": initial,
        "storage_capacity": storage,
    }


__all__ = ["generate_hof_full_chain"]
