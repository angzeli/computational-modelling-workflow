"""Compose multiple prepared HOF queues into one sequential authorization unit."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shlex
from typing import Any, Mapping, Sequence

from cmw.core.provenance import atomic_write_json, file_hash, read_json, stable_hash

from .command_queue import verify_hof_command_queue


BATCH_QUEUE_SCHEMA_VERSION = 1


def _batch_identity(value: Mapping[str, Any]) -> dict[str, object]:
    return {
        key: value[key]
        for key in ("schema_version", "subqueues", "steps", "storage_policy")
    }


def _master_script(
    *,
    python_bin: Path,
    cmw_root: Path,
    batch_id: str,
    project_root: Path,
    minimum_free_gb: float,
    steps: Sequence[Mapping[str, Any]],
) -> str:
    encoded_steps = "\n".join(
        "|".join(
            (
                str(step["system_id"]),
                str(step["subqueue_id"]),
                str(step["preflight_directory"]),
                str(step["step_id"]),
                str(step["script"]),
            )
        )
        for step in steps
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail
BATCH_DIR=$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd -P)
PYTHON_BIN={shlex.quote(str(python_bin))}
CMW_ROOT={shlex.quote(str(cmw_root))}
if [[ ${{1:---status}} != --run || ${{2:-}} != --confirm-expensive ]]; then
  PYTHONPATH="$CMW_ROOT/src${{PYTHONPATH:+:$PYTHONPATH}}" \
    "$PYTHON_BIN" -m cmw.adapters.hof.batch_queue_cli verify \
      --batch "$BATCH_DIR/batch-queue.json" \
      --authorization "$BATCH_DIR/batch-authorization.json"
  printf 'Use --run --confirm-expensive only after this exact batch is authorized.\n'
  exit 0
fi
PYTHONPATH="$CMW_ROOT/src${{PYTHONPATH:+:$PYTHONPATH}}" \
  "$PYTHON_BIN" -m cmw.adapters.hof.batch_queue_cli verify \
    --batch "$BATCH_DIR/batch-queue.json" \
    --authorization "$BATCH_DIR/batch-authorization.json" --require-authorized

scientific_processes() {{
  ps -axo pid=,comm= | while read -r pid command; do
    name=${{command##*/}}
    case "$name" in
      orca|orca_*|Multiwfn|mpirun) printf '%s %s\n' "$pid" "$command" ;;
    esac
  done
}}

step_count={len(steps)}
index=0
while IFS='|' read -r system_id subqueue_id preflight step_id script; do
  index=$((index + 1))
  conflicts=$(scientific_processes)
  if [[ -n "$conflicts" ]]; then
    printf 'Conflicting scientific process before %s/%s:\n%s\n' \
      "$system_id" "$step_id" "$conflicts" >&2
    exit 73
  fi
  export PYTHONPATH="$CMW_ROOT/src:$preflight/python-runtime${{PYTHONPATH:+:$PYTHONPATH}}"
  "$PYTHON_BIN" -m cmw.core.storage_cli --path {shlex.quote(str(project_root))} \
    --minimum-free-gb {minimum_free_gb}
  export CMW_HOF_EXPECTED_TOKEN="$subqueue_id"
  export CMW_HOF_QUEUE_RUN_TOKEN="$subqueue_id"
  printf '[BATCH %02d/%02d] %s/%s\n' "$index" "$step_count" "$system_id" "$step_id"
  "$script"
done <<'CMW_BATCH_STEPS'
{encoded_steps}
CMW_BATCH_STEPS
printf '[BATCH COMPLETE] %s\n' {shlex.quote(batch_id)}
"""


def generate_hof_batch_queue(
    *,
    queue_directories: Sequence[Path],
    output_directory: Path,
    python_bin: Path,
) -> dict[str, object]:
    """Create one non-authorized sequential queue from immutable child queues."""

    if len(queue_directories) < 2:
        raise ValueError("a batch queue requires at least two child queues")
    output = output_directory.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"batch queue output already exists: {output}")
    python_bin = python_bin.expanduser().resolve(strict=True)
    subqueues: list[dict[str, object]] = []
    steps: list[dict[str, object]] = []
    systems: set[str] = set()
    cmw_roots: set[str] = set()
    project_roots: set[str] = set()
    storage_floors: set[float] = set()
    for raw_directory in queue_directories:
        directory = raw_directory.expanduser().resolve(strict=True)
        queue_path = directory / "command-queue.json"
        authorization_path = directory / "queue-authorization.json"
        verification = verify_hof_command_queue(
            queue_path=queue_path,
            authorization_path=authorization_path,
            require_authorized=False,
        )
        queue = read_json(queue_path)
        system_id = str(queue["system_id"])
        if system_id in systems:
            raise ValueError(f"batch queue repeats system identity: {system_id}")
        systems.add(system_id)
        cmw_roots.add(str(queue["repositories"]["cmw"]["path"]))
        preflight = Path(str(queue["preflight_directory"]))
        manifest = read_json(preflight / "package-manifest.json")
        storage = manifest.get("storage_capacity")
        if not isinstance(storage, Mapping):
            raise ValueError(f"child queue lacks storage evidence: {system_id}")
        project_roots.add(str(storage["check_path"]))
        storage_floors.add(float(storage["minimum_free_gib"]))
        subqueues.append(
            {
                "system_id": system_id,
                "directory": str(directory),
                "queue_path": str(queue_path),
                "queue_id": str(queue["queue_id"]),
                "queue_sha256": file_hash(queue_path),
                "preflight_directory": str(preflight),
                "verification": verification["validation"],
            }
        )
        for step in queue["steps"]:
            script = directory / str(step["script"])
            steps.append(
                {
                    "system_id": system_id,
                    "subqueue_id": str(queue["queue_id"]),
                    "preflight_directory": str(preflight),
                    "step_id": str(step["id"]),
                    "kind": str(step["kind"]),
                    "script": str(script),
                    "script_size_bytes": script.stat().st_size,
                    "script_sha256": file_hash(script),
                }
            )
    if len(cmw_roots) != 1 or len(project_roots) != 1 or len(storage_floors) != 1:
        raise ValueError("child queues use incompatible runtime or storage policies")
    identity = {
        "schema_version": BATCH_QUEUE_SCHEMA_VERSION,
        "subqueues": subqueues,
        "steps": steps,
        "storage_policy": {
            "check_path": next(iter(project_roots)),
            "minimum_free_gb": next(iter(storage_floors)),
            "recheck_before_every_step": True,
        },
    }
    batch_id = stable_hash(identity)
    record = {
        **identity,
        "batch_id": batch_id,
        "status": "PREPARED_NOT_AUTHORIZED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "one_scientific_process_at_a_time": True,
            "one_authorization_unit": True,
            "fail_closed": True,
        },
    }
    output.mkdir(parents=True)
    atomic_write_json(output / "batch-queue.json", record)
    atomic_write_json(
        output / "batch-authorization.json",
        {
            "schema_version": BATCH_QUEUE_SCHEMA_VERSION,
            "batch_id": batch_id,
            "status": "NOT_AUTHORIZED",
        },
    )
    script = output / "run_both_command_queues.sh"
    script.write_text(
        _master_script(
            python_bin=python_bin,
            cmw_root=Path(next(iter(cmw_roots))),
            batch_id=batch_id,
            project_root=Path(next(iter(project_roots))),
            minimum_free_gb=next(iter(storage_floors)),
            steps=steps,
        ),
        encoding="utf-8",
    )
    script.chmod(0o750)
    return record


def verify_hof_batch_queue(
    *, batch_path: Path, authorization_path: Path, require_authorized: bool
) -> dict[str, object]:
    """Reject drift in the batch identity or any child queue and script."""

    batch = read_json(batch_path)
    if batch.get("schema_version") != BATCH_QUEUE_SCHEMA_VERSION:
        raise ValueError("unsupported HOF batch-queue schema")
    if stable_hash(_batch_identity(batch)) != batch.get("batch_id"):
        raise ValueError("HOF batch queue identity is invalid")
    for child in batch["subqueues"]:
        queue_path = Path(str(child["queue_path"]))
        if file_hash(queue_path) != child["queue_sha256"]:
            raise ValueError(f"child queue changed: {child['system_id']}")
        verification = verify_hof_command_queue(
            queue_path=queue_path,
            authorization_path=Path(str(child["directory"]))
            / "queue-authorization.json",
            require_authorized=False,
        )
        if verification["queue_id"] != child["queue_id"]:
            raise ValueError(f"child queue identity changed: {child['system_id']}")
    for step in batch["steps"]:
        script = Path(str(step["script"]))
        if (
            not script.is_file()
            or script.stat().st_size != step["script_size_bytes"]
            or file_hash(script) != step["script_sha256"]
        ):
            raise ValueError(
                f"batch step is missing or changed: {step['system_id']}/{step['step_id']}"
            )
    authorization = read_json(authorization_path)
    authorized = (
        authorization.get("batch_id") == batch["batch_id"]
        and authorization.get("status") == "READY_TO_LAUNCH"
    )
    if require_authorized and not authorized:
        raise ValueError("this exact HOF batch queue is not authorized")
    return {
        "batch_id": batch["batch_id"],
        "status": batch["status"],
        "authorized": authorized,
        "systems": [item["system_id"] for item in batch["subqueues"]],
        "step_count": len(batch["steps"]),
        "validation": "PASSED",
    }


def authorize_hof_batch_queue(
    *,
    batch_path: Path,
    authorization_path: Path,
    concurrent_scientific_jobs: str,
    conflicting_locks: str,
    package_validation: str,
    storage_validation: str,
    command_validation: str,
) -> dict[str, object]:
    """Authorize one immutable two-system batch after fresh external checks."""

    checks = {
        "concurrent_scientific_jobs": concurrent_scientific_jobs,
        "conflicting_locks": conflicting_locks,
        "package_validation": package_validation,
        "storage_validation": storage_validation,
        "command_validation": command_validation,
    }
    expected = {
        "concurrent_scientific_jobs": "none",
        "conflicting_locks": "none",
        "package_validation": "passed",
        "storage_validation": "passed",
        "command_validation": "passed",
    }
    failed = [name for name, value in checks.items() if value != expected[name]]
    if failed:
        raise ValueError("batch authorization failed closed: " + ", ".join(failed))
    verification = verify_hof_batch_queue(
        batch_path=batch_path,
        authorization_path=authorization_path,
        require_authorized=False,
    )
    now = datetime.now(timezone.utc).isoformat()
    authorization = {
        "schema_version": BATCH_QUEUE_SCHEMA_VERSION,
        "batch_id": verification["batch_id"],
        "status": "READY_TO_LAUNCH",
        "authorized_at": now,
        "checks": checks,
    }
    atomic_write_json(authorization_path, authorization)
    batch = read_json(batch_path)
    batch["status"] = "READY_TO_LAUNCH"
    batch["authorized_at"] = now
    atomic_write_json(batch_path, batch)
    return verify_hof_batch_queue(
        batch_path=batch_path,
        authorization_path=authorization_path,
        require_authorized=True,
    )


__all__ = [
    "authorize_hof_batch_queue",
    "generate_hof_batch_queue",
    "verify_hof_batch_queue",
]
