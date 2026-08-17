"""Generate and validate a fail-closed HOF scientific command queue."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess
from typing import Any, Mapping, Sequence

from cmw.core.provenance import atomic_write_json, file_hash, read_json, stable_hash


QUEUE_SCHEMA_VERSION = 1


def _git_head(repository: Path) -> str:
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _quote(value: Path | str) -> str:
    return shlex.quote(str(value))


def _guarded_script(body: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ ${CMW_HOF_QUEUE_RUN_TOKEN:-} != ${CMW_HOF_EXPECTED_TOKEN:-} "
        "|| -z ${CMW_HOF_QUEUE_RUN_TOKEN:-} ]]; then\n"
        "  printf 'This step must be launched by run_command_queue.sh\\n' >&2\n"
        "  exit 77\n"
        "fi\n"
        + body.rstrip()
        + "\n"
    )


def _write_script(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o750)


def _prepared_targets(package: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    values = package.get("prepared_targets")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError("downstream package lacks prepared_targets")
    result = {
        str(item["node_id"]): dict(item)
        for item in values
        if isinstance(item, Mapping) and item.get("node_id")
    }
    if len(result) != len(values):
        raise ValueError("downstream package has duplicate or invalid prepared targets")
    return result


def _validate_package(preflight: Path) -> dict[str, Any]:
    manifest = read_json(preflight / "package-manifest.json")
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("downstream package manifest is invalid")
    for relative, evidence in files.items():
        if not isinstance(evidence, Mapping):
            raise ValueError(f"invalid package evidence: {relative}")
        path = preflight / str(relative)
        if (
            not path.is_file()
            or path.stat().st_size != evidence.get("size_bytes")
            or file_hash(path) != evidence.get("sha256")
        ):
            raise ValueError(f"downstream package file changed: {relative}")
    return manifest


def _static_step_script(preview: Path) -> str:
    command = preview.read_text(encoding="utf-8").strip()
    if not command or "run_orca.sh" not in command:
        raise ValueError(f"invalid ORCA command preview: {preview}")
    return _guarded_script(command)


def _relaxed_energy_script(
    *,
    cmw_root: Path,
    python_bin: Path,
    orca_executable: Path,
    config_directory: Path,
    hof_root: Path,
    fragment_id: str,
    relaxation_result: Path,
    runtime_contract: Path,
) -> str:
    arguments = shlex.join(
        [
            str(python_bin),
            "-m",
            "cmw.adapters.hof.execution_cli",
            "prepare-relaxed-energy",
            "--systems",
            str(config_directory / "systems.yaml"),
            "--methods",
            str(config_directory / "methods.yaml"),
            "--protocol",
            str(config_directory / "protocol.yaml"),
            "--execution",
            str(config_directory / "execution.yaml"),
            "--system",
            "hof_h4tbapy",
            "--project-root",
            str(hof_root),
            "--fragment",
            fragment_id,
            "--relaxation-result",
            str(relaxation_result),
            "--runtime-contract",
            str(runtime_contract),
            "--cmw-root",
            str(cmw_root),
            "--python",
            str(python_bin),
            "--orca-exe",
            str(orca_executable),
        ]
    )
    body = f"""
export PYTHONPATH={_quote(cmw_root / 'src')}${{PYTHONPATH:+:$PYTHONPATH}}
set +e
payload=$({arguments})
status=$?
set -e
if ((status == 10)); then
  printf '[REUSED] relaxed fragment {fragment_id} energy\\n'
  exit 0
fi
if ((status != 0)); then
  printf '%s\\n' "$payload" >&2
  exit "$status"
fi
command_path=$(printf '%s' "$payload" | {_quote(python_bin)} -c \
  'import json,sys; print(json.load(sys.stdin)["command_path"])')
"$command_path"
"""
    return _guarded_script(body)


def _conversion_script(
    *,
    cmw_root: Path,
    python_bin: Path,
    density_result: Path,
    hof_root: Path,
    converter: Path,
) -> str:
    command = shlex.join(
        [
            str(cmw_root / "scripts/orca/convert_orca_wavefunction.sh"),
            "--source",
            str(density_result),
            "--output",
            str(hof_root),
            "--converter",
            str(converter),
            "--spin-mode",
            "restricted",
        ]
    )
    return _guarded_script(
        f"export PYTHON_BIN={_quote(python_bin)}\n{command}\n"
    )


def _multiwfn_script(
    *,
    cmw_root: Path,
    python_bin: Path,
    density_result: Path,
    hof_root: Path,
    converter: Path,
    multiwfn_executable: Path,
    settings_source: Path,
    fragments_path: Path,
    configuration_path: Path,
    threads: int,
) -> str:
    plan = shlex.join(
        [
            str(python_bin),
            "-m",
            "cmw.molecular.orca.conversion_cli",
            "plan",
            "--source",
            str(density_result),
            "--output",
            str(hof_root),
            "--converter",
            str(converter),
            "--spin-mode",
            "restricted",
        ]
    )
    command = shlex.join(
        [
            str(cmw_root / "scripts/workflows/generate_igmh_cubes.sh"),
            "--source",
            "$source_result",
            "--output",
            str(hof_root),
            "--fragments",
            str(fragments_path),
            "--config",
            str(configuration_path),
            "--multiwfn-exe",
            str(multiwfn_executable),
            "--settings-source",
            str(settings_source),
            "--threads",
            str(threads),
        ]
    ).replace("'$source_result'", '"$source_result"')
    body = f"""
export PYTHONPATH={_quote(cmw_root / 'src')}${{PYTHONPATH:+:$PYTHONPATH}}
export PYTHON_BIN={_quote(python_bin)}
set +e
conversion_plan=$({plan})
status=$?
set -e
if ((status != 10)); then
  printf 'Validated reusable ORCA-to-Molden result is required before IGMH\\n' >&2
  printf '%s\\n' "$conversion_plan" >&2
  exit 70
fi
source_result=$(printf '%s' "$conversion_plan" | "$PYTHON_BIN" -c \
  'import json,sys; print(json.load(sys.stdin)["result_path"])')
{command}
"""
    return _guarded_script(body)


def _master_script(
    *, cmw_root: Path, python_bin: Path, queue_id: str, steps: Sequence[Mapping[str, Any]]
) -> str:
    script_names = " ".join(_quote(str(item["script"])) for item in steps)
    labels = " ".join(_quote(str(item["id"])) for item in steps)
    return f"""#!/usr/bin/env bash
set -euo pipefail

QUEUE_DIR=$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd -P)
PYTHON_BIN={_quote(python_bin)}
export PYTHONPATH={_quote(cmw_root / 'src')}${{PYTHONPATH:+:$PYTHONPATH}}
mode=${{1:---status}}

case "$mode" in
  --status|--plan)
    "$PYTHON_BIN" -m cmw.adapters.hof.command_queue_cli verify \
      --queue "$QUEUE_DIR/command-queue.json" \
      --authorization "$QUEUE_DIR/queue-authorization.json"
    exit $?
    ;;
  --run)
    if [[ ${{2:-}} != --confirm-expensive ]]; then
      printf 'Use --run --confirm-expensive after queue authorization\\n' >&2
      exit 64
    fi
    ;;
  *)
    printf 'Usage: %s [--status|--plan|--run --confirm-expensive]\\n' "$0" >&2
    exit 64
    ;;
esac

"$PYTHON_BIN" -m cmw.adapters.hof.command_queue_cli verify \
  --queue "$QUEUE_DIR/command-queue.json" \
  --authorization "$QUEUE_DIR/queue-authorization.json" --require-authorized

scientific_processes() {{
  ps -axo pid=,comm= | while read -r pid command; do
    name=${{command##*/}}
    case "$name" in
      orca|orca_*|Multiwfn|orca_2mkl) printf '%s %s\\n' "$pid" "$command" ;;
    esac
  done
}}

scripts=({script_names})
labels=({labels})
export CMW_HOF_EXPECTED_TOKEN={_quote(queue_id)}
export CMW_HOF_QUEUE_RUN_TOKEN={_quote(queue_id)}
for index in "${{!scripts[@]}}"; do
  conflicts=$(scientific_processes)
  if [[ -n "$conflicts" ]]; then
    printf 'Conflicting scientific process detected before %s:\\n%s\\n' \
      "${{labels[$index]}}" "$conflicts" >&2
    exit 73
  fi
  printf '[QUEUE %02d/%02d] %s\\n' "$((index + 1))" "${{#scripts[@]}}" "${{labels[$index]}}"
  "$QUEUE_DIR/${{scripts[$index]}}"
done
printf '[QUEUE COMPLETE] all ORCA/conversion/Multiwfn jobs validated\\n'
"""


def generate_hof_command_queue(
    *,
    preflight_directory: Path,
    output_directory: Path,
    cmw_root: Path,
    hof_root: Path,
    python_bin: Path,
    orca_executable: Path,
    converter: Path,
    multiwfn_executable: Path,
    multiwfn_settings: Path,
) -> dict[str, object]:
    """Create a non-authorized, resumable queue for the prepared HOF plan."""

    preflight = preflight_directory.expanduser().resolve(strict=True)
    output = output_directory.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"command queue output already exists: {output}")
    package = _validate_package(preflight)
    targets = _prepared_targets(package)
    required = {
        "dimer",
        "fragment_a",
        "fragment_b",
        "distorted_fragment_a_energy",
        "relax_fragment_a",
        "distorted_fragment_b_energy",
        "relax_fragment_b",
        "igmh_density",
    }
    if not required.issubset(targets):
        raise ValueError("downstream package lacks required materialized targets")
    resolved_files = [
        path.expanduser().resolve()
        for path in (
            python_bin,
            orca_executable,
            converter,
            multiwfn_executable,
            multiwfn_settings,
        )
    ]
    if not all(path.is_file() for path in resolved_files):
        raise FileNotFoundError("one or more queue executables/settings files are missing")
    python_bin, orca_executable, converter, multiwfn_executable, multiwfn_settings = resolved_files
    cmw_root = cmw_root.expanduser().resolve(strict=True)
    hof_root = hof_root.expanduser().resolve(strict=True)
    output.mkdir(parents=True)
    (output / "steps").mkdir()

    static_order = (
        "dimer",
        "fragment_a",
        "fragment_b",
        "distorted_fragment_a_energy",
        "relax_fragment_a",
    )
    steps: list[dict[str, object]] = []
    for number, node_id in enumerate(static_order, start=1):
        script = f"steps/{number:02d}_{node_id}.sh"
        _write_script(
            output / script,
            _static_step_script(preflight / "command-previews" / f"{node_id}.txt"),
        )
        steps.append({"id": node_id, "kind": "ORCA", "script": script})

    config = hof_root / "config"
    runtime_a = Path(str(targets["relax_fragment_a"]["attempt_directory"])) / "orca-runtime.json"
    relax_a_result = Path(str(targets["relax_fragment_a"]["attempt_directory"])) / "job.json"
    script = "steps/06_relaxed_fragment_a_energy.sh"
    _write_script(
        output / script,
        _relaxed_energy_script(
            cmw_root=cmw_root,
            python_bin=python_bin,
            orca_executable=orca_executable,
            config_directory=config,
            hof_root=hof_root,
            fragment_id="A",
            relaxation_result=relax_a_result,
            runtime_contract=runtime_a,
        ),
    )
    steps.append({"id": "relaxed_fragment_a_energy", "kind": "ORCA", "script": script})

    for number, node_id in ((7, "distorted_fragment_b_energy"), (8, "relax_fragment_b")):
        script = f"steps/{number:02d}_{node_id}.sh"
        _write_script(
            output / script,
            _static_step_script(preflight / "command-previews" / f"{node_id}.txt"),
        )
        steps.append({"id": node_id, "kind": "ORCA", "script": script})

    runtime_b = Path(str(targets["relax_fragment_b"]["attempt_directory"])) / "orca-runtime.json"
    relax_b_result = Path(str(targets["relax_fragment_b"]["attempt_directory"])) / "job.json"
    script = "steps/09_relaxed_fragment_b_energy.sh"
    _write_script(
        output / script,
        _relaxed_energy_script(
            cmw_root=cmw_root,
            python_bin=python_bin,
            orca_executable=orca_executable,
            config_directory=config,
            hof_root=hof_root,
            fragment_id="B",
            relaxation_result=relax_b_result,
            runtime_contract=runtime_b,
        ),
    )
    steps.append({"id": "relaxed_fragment_b_energy", "kind": "ORCA", "script": script})

    script = "steps/10_igmh_density.sh"
    _write_script(
        output / script,
        _static_step_script(preflight / "command-previews/igmh_density.txt"),
    )
    steps.append({"id": "igmh_density", "kind": "ORCA", "script": script})
    density_result = Path(str(targets["igmh_density"]["attempt_directory"])) / "job.json"

    script = "steps/11_orca_to_molden.sh"
    _write_script(
        output / script,
        _conversion_script(
            cmw_root=cmw_root,
            python_bin=python_bin,
            density_result=density_result,
            hof_root=hof_root,
            converter=converter,
        ),
    )
    steps.append({"id": "orca_to_molden", "kind": "CONVERSION", "script": script})

    threads = int(
        read_json(preflight / "workflow-plan.json")["multiwfn_plans"]["multiwfn_igmh"]
        ["runtime"]["nthreads"]
    )
    script = "steps/12_multiwfn_igmh.sh"
    _write_script(
        output / script,
        _multiwfn_script(
            cmw_root=cmw_root,
            python_bin=python_bin,
            density_result=density_result,
            hof_root=hof_root,
            converter=converter,
            multiwfn_executable=multiwfn_executable,
            settings_source=multiwfn_settings,
            fragments_path=output / "igmh-fragments.json",
            configuration_path=output / "igmh-configuration.json",
            threads=threads,
        ),
    )
    steps.append({"id": "multiwfn_igmh", "kind": "MULTIWFN", "script": script})

    for step in steps:
        step_path = output / str(step["script"])
        step["script_size_bytes"] = step_path.stat().st_size
        step["script_sha256"] = file_hash(step_path)

    (output / "igmh-fragments.json").write_bytes(
        (preflight / "igmh-fragments.json").read_bytes()
    )
    (output / "igmh-configuration.json").write_bytes(
        (preflight / "igmh-configuration.json").read_bytes()
    )
    prepared_files: list[Path] = []
    for node_id in required:
        item = targets[node_id]
        attempt = Path(str(item["attempt_directory"]))
        target_directory = Path(str(item["target_directory"]))
        prepared_files.extend(
            (
                target_directory / "target.json",
                attempt / "stage.inp",
                attempt / "input.xyz",
                attempt / "geometry-input.json",
                attempt / "execution-layout.json",
                attempt / "orca-runtime.json",
            )
        )
    geometry_source = read_json(preflight / "validated-geometry-source.json")
    validated_parent_files: list[Path] = []
    for key in ("optimization_job", "frequency_job"):
        result_path = Path(str(geometry_source[key])).resolve(strict=True)
        validated_parent_files.append(result_path)
        result = read_json(result_path)
        for artifact in result.get("artifacts", {}).values():
            if not isinstance(artifact, Mapping):
                continue
            artifact_path = Path(str(artifact.get("path", "")))
            if not artifact_path.is_absolute():
                artifact_path = result_path.parent / artifact_path
            if artifact_path.is_file():
                validated_parent_files.append(artifact_path)
    source_paths = [
        config / "systems.yaml",
        config / "methods.yaml",
        config / "protocol.yaml",
        config / "execution.yaml",
        cmw_root / "scripts/orca/run_orca.sh",
        cmw_root / "scripts/orca/convert_orca_wavefunction.sh",
        cmw_root / "scripts/workflows/generate_igmh_cubes.sh",
        cmw_root / "scripts/workflows/multiwfn_cube_workflow.sh",
        cmw_root / "scripts/multiwfn/multiwfn_runtime.sh",
        cmw_root / "src/cmw/adapters/hof/execution.py",
        cmw_root / "src/cmw/adapters/hof/execution_cli.py",
        cmw_root / "src/cmw/adapters/hof/command_queue.py",
        cmw_root / "src/cmw/adapters/hof/command_queue_cli.py",
        cmw_root / "src/cmw/adapters/hof/config.py",
        cmw_root / "src/cmw/adapters/hof/models.py",
        cmw_root / "src/cmw/adapters/hof/orca.py",
        cmw_root / "src/cmw/adapters/hof/planner.py",
        cmw_root / "src/cmw/adapters/hof/validation.py",
        cmw_root / "src/cmw/adapters/hof/workflow.py",
        cmw_root / "src/cmw/molecular/orca/conversion.py",
        cmw_root / "src/cmw/molecular/orca/conversion_cli.py",
        cmw_root / "src/cmw/molecular/orca/cli.py",
        cmw_root / "src/cmw/molecular/orca/geometry.py",
        cmw_root / "src/cmw/molecular/orca/input.py",
        cmw_root / "src/cmw/molecular/orca/job.py",
        cmw_root / "src/cmw/molecular/orca/protocol.py",
        cmw_root / "src/cmw/molecular/orca/runtime.py",
        cmw_root / "src/cmw/molecular/orca/status.py",
        cmw_root / "src/cmw/molecular/multiwfn/adapter.py",
        cmw_root / "src/cmw/molecular/multiwfn/runtime.py",
        cmw_root / "src/cmw/molecular/workflows/downstream_cli.py",
        cmw_root / "src/cmw/molecular/workflows/igmh.py",
        cmw_root / "src/cmw/molecular/workflows/multiwfn_analysis.py",
        cmw_root / "src/cmw/molecular/workflows/source.py",
        cmw_root / "src/cmw/core/artifacts.py",
        cmw_root / "src/cmw/core/execution_layout.py",
        cmw_root / "src/cmw/core/execution_profiles.py",
        cmw_root / "src/cmw/core/job.py",
        cmw_root / "src/cmw/core/locks.py",
        cmw_root / "src/cmw/core/provenance.py",
        cmw_root / "src/cmw/core/structure_artifacts.py",
        converter,
        multiwfn_executable,
        multiwfn_settings,
        *prepared_files,
        *validated_parent_files,
    ]
    source_files = {
        str(path.resolve()): {
            "size_bytes": path.stat().st_size,
            "sha256": file_hash(path),
        }
        for path in source_paths
    }
    repositories = {
        "cmw": {"path": str(cmw_root), "head": _git_head(cmw_root)},
        "hof": {"path": str(hof_root), "head": _git_head(hof_root)},
    }
    queue_identity = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "system_id": "hof_h4tbapy",
        "steps": steps,
        "source_files": source_files,
        "repositories": repositories,
        "preflight_directory": str(preflight),
        "preflight_manifest_sha256": file_hash(preflight / "package-manifest.json"),
    }
    queue_id = stable_hash(queue_identity)
    queue = {
        **queue_identity,
        "queue_id": queue_id,
        "status": "PREPARED_NOT_AUTHORIZED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "one_scientific_process_at_a_time": True,
            "fail_closed": True,
            "resume_by_validated_reuse": True,
            "requires_explicit_authorization": True,
        },
    }
    atomic_write_json(output / "command-queue.json", queue)
    atomic_write_json(
        output / "queue-authorization.json",
        {
            "schema_version": QUEUE_SCHEMA_VERSION,
            "queue_id": queue_id,
            "status": "NOT_AUTHORIZED",
            "reason": "queue generation does not authorize expensive execution",
        },
    )
    _write_script(
        output / "run_command_queue.sh",
        _master_script(
            cmw_root=cmw_root, python_bin=python_bin, queue_id=queue_id, steps=steps
        ),
    )
    (output / "README.md").write_text(
        f"""# HOF command queue

Status: **PREPARED_NOT_AUTHORIZED**

Queue identity: `{queue_id}`

The queue contains {len(steps)} sequential external-program steps: 10 ORCA jobs,
one provenance-preserving ORCA-to-Molden conversion, and one Multiwfn IGMH job.
Every completed step is revalidated and reused on restart; no two scientific
processes are launched concurrently. Any failed or mismatched parent stops the queue.

Derived CP interaction, LED, and deformation artifacts are finalized from the
validated job results after this external-program queue; they are not additional
ORCA or Multiwfn launches.

Do not run `run_command_queue.sh --run --confirm-expensive` until
`queue-authorization.json` has been regenerated by a fresh launch-authorization
check with status `READY_TO_LAUNCH` for this exact queue identity.
""",
        encoding="utf-8",
    )
    return queue


def verify_hof_command_queue(
    *, queue_path: Path, authorization_path: Path, require_authorized: bool
) -> dict[str, object]:
    """Fail closed on repository, source, script, or authorization drift."""

    queue = read_json(queue_path)
    if queue.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise ValueError("unsupported command-queue schema")
    identity = {
        key: queue[key]
        for key in (
            "schema_version",
            "system_id",
            "steps",
            "source_files",
            "repositories",
            "preflight_directory",
            "preflight_manifest_sha256",
        )
    }
    if stable_hash(identity) != queue.get("queue_id"):
        raise ValueError("command queue identity is invalid")
    for name, repository in queue["repositories"].items():
        path = Path(str(repository["path"]))
        if _git_head(path) != repository["head"]:
            raise ValueError(f"{name} repository HEAD changed")
    for raw_path, evidence in queue["source_files"].items():
        path = Path(raw_path)
        if (
            not path.is_file()
            or path.stat().st_size != evidence["size_bytes"]
            or file_hash(path) != evidence["sha256"]
        ):
            raise ValueError(f"queue source changed: {path}")
    queue_directory = queue_path.resolve().parent
    for step in queue["steps"]:
        script = queue_directory / str(step["script"])
        if (
            not script.is_file()
            or script.stat().st_size != step["script_size_bytes"]
            or file_hash(script) != step["script_sha256"]
        ):
            raise ValueError(f"queue step is missing or changed: {script}")
    preflight_manifest = Path(str(queue["preflight_directory"])) / "package-manifest.json"
    if (
        not preflight_manifest.is_file()
        or file_hash(preflight_manifest) != queue["preflight_manifest_sha256"]
    ):
        raise ValueError("downstream preflight manifest changed")
    authorization = read_json(authorization_path)
    authorized = (
        authorization.get("queue_id") == queue["queue_id"]
        and authorization.get("status") == "READY_TO_LAUNCH"
    )
    if require_authorized and not authorized:
        raise ValueError("this exact command queue is not authorized to launch")
    return {
        "queue_id": queue["queue_id"],
        "queue_status": queue["status"],
        "authorized": authorized,
        "step_count": len(queue["steps"]),
        "steps": [step["id"] for step in queue["steps"]],
        "validation": "PASSED",
    }


def authorize_hof_command_queue(
    *,
    queue_path: Path,
    authorization_path: Path,
    report_path: Path,
    concurrent_scientific_jobs: str,
    conflicting_locks: str,
    package_validation: str,
    resource_validation: str,
    command_validation: str,
) -> dict[str, object]:
    """Authorize one immutable queue after externally observed launch gates pass."""

    checks = {
        "concurrent_scientific_jobs": concurrent_scientific_jobs,
        "conflicting_locks": conflicting_locks,
        "package_validation": package_validation,
        "resource_validation": resource_validation,
        "command_validation": command_validation,
    }
    expected = {
        "concurrent_scientific_jobs": "none",
        "conflicting_locks": "none",
        "package_validation": "passed",
        "resource_validation": "passed",
        "command_validation": "passed",
    }
    failures = [name for name, value in checks.items() if value != expected[name]]
    if failures:
        raise ValueError(
            "queue authorization failed closed: " + ", ".join(sorted(failures))
        )

    verification = verify_hof_command_queue(
        queue_path=queue_path,
        authorization_path=authorization_path,
        require_authorized=False,
    )
    queue = read_json(queue_path)
    authorized_at = datetime.now(timezone.utc).isoformat()
    exact_command = str(queue_path.resolve().parent / "run_command_queue.sh")
    authorization = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "queue_id": queue["queue_id"],
        "status": "READY_TO_LAUNCH",
        "authorized_at": authorized_at,
        "checks": checks,
        "repositories": queue["repositories"],
        "exact_next_human_action": exact_command,
    }
    atomic_write_json(authorization_path, authorization)
    queue["status"] = "READY_TO_LAUNCH"
    queue["authorized_at"] = authorized_at
    atomic_write_json(queue_path, queue)
    final_verification = verify_hof_command_queue(
        queue_path=queue_path,
        authorization_path=authorization_path,
        require_authorized=True,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "\n".join(
            (
                "# HOF command queue final authorization",
                "",
                "## Verdict",
                "",
                "**READY_TO_LAUNCH**",
                "",
                f"- Queue identity: `{queue['queue_id']}`",
                f"- Authorized at: `{authorized_at}`",
                f"- CMW commit: `{queue['repositories']['cmw']['head']}`",
                f"- HOF commit: `{queue['repositories']['hof']['head']}`",
                f"- Queue validation: `{verification['validation']}`",
                "- Concurrent scientific jobs: `none`",
                "- Conflicting execution locks: `none`",
                "- Prepared package validation: `passed`",
                "- Execution resource validation: `passed`",
                "- Exact command validation: `passed`",
                "",
                "## Exact next human action",
                "",
                "Run the authorized queue entrypoint in Terminal:",
                "",
                "```bash",
                f"{exact_command} --run --confirm-expensive",
                "```",
                "",
                "The authorization does not itself launch ORCA or Multiwfn.",
                "",
            )
        ),
        encoding="utf-8",
    )
    return {**final_verification, "authorization": authorization}


__all__ = [
    "QUEUE_SCHEMA_VERSION",
    "authorize_hof_command_queue",
    "generate_hof_command_queue",
    "verify_hof_command_queue",
]
