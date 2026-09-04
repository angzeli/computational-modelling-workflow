"""Fail-closed external working storage for one executable attempt.

The scratch location is operational metadata only.  Scientific targets and
canonical attempt paths are created before this module is invoked.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from .provenance import atomic_write_json, file_hash, read_json
from .storage import BYTES_PER_GIB, DiskUsage, check_storage_capacity


EXTERNAL_SCRATCH_SCHEMA_VERSION = 1
EXTERNAL_SCRATCH_RECORD = "external-scratch.json"
OWNER_RECORD = ".cmw-external-scratch-owner.json"
COPYBACK_OWNER_RECORD = ".cmw-copyback-owner.json"
ROLE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ExternalScratchError(RuntimeError):
    """Raised when external scratch cannot satisfy its safety contract."""

    code = "FAILED_EXTERNAL_SCRATCH"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__(f"{self.code}: {message}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _role(value: object) -> str:
    selected = str(value)
    if ROLE_PATTERN.fullmatch(selected) is None:
        raise ExternalScratchError("file roles must be path-safe identifiers")
    return selected


def _absolute(path: Path | str, *, name: str) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise ExternalScratchError(f"{name} must be absolute")
    return Path(os.path.abspath(selected))


def _direct_existing_path(path: Path | str, *, name: str) -> Path:
    """Resolve an existing path while rejecting symlink traversal."""

    selected = _absolute(path, name=name)
    try:
        resolved = selected.resolve(strict=True)
    except OSError as exc:
        raise ExternalScratchError(f"{name} is missing or unreadable") from exc
    if selected.is_symlink() or resolved != selected:
        raise ExternalScratchError(f"{name} must not traverse a symlink")
    return selected


def _strict_child(path: Path, parent: Path, *, name: str) -> None:
    try:
        relative = path.relative_to(parent)
    except ValueError as exc:
        raise ExternalScratchError(f"{name} is outside its allowed root") from exc
    if not relative.parts:
        raise ExternalScratchError(f"{name} must not equal its allowed root")


def _disjoint(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
    except ValueError:
        pass
    else:
        return False
    try:
        second.relative_to(first)
    except ValueError:
        return True
    return False


def _identity(path: Path) -> dict[str, object]:
    return {
        "size_bytes": path.stat().st_size,
        "sha256": file_hash(path),
    }


def _verified_copy(source: Path, destination: Path) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.copying"
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        expected = _identity(source)
        if _identity(temporary) != expected:
            raise ExternalScratchError(
                f"copy integrity failed for {source.name}", code="FAILED_COPY_INTEGRITY"
            )
        os.replace(temporary, destination)
        return expected
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _owned_marker(directory: Path, token: str, record_path: Path) -> dict[str, object]:
    marker_path = directory / OWNER_RECORD
    try:
        marker = read_json(marker_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ExternalScratchError(
            "external execution ownership marker is missing or invalid",
            code="UNSAFE_SCRATCH_OWNERSHIP",
        ) from exc
    if (
        marker.get("owner_token") != token
        or marker.get("execution_directory") != str(directory)
        or marker.get("canonical_record") != str(record_path)
    ):
        raise ExternalScratchError(
            "external execution ownership marker does not match the session",
            code="UNSAFE_SCRATCH_OWNERSHIP",
        )
    return marker


def _write_session(path: Path, value: Mapping[str, object]) -> dict[str, object]:
    selected = dict(value)
    selected["updated_at"] = _now()
    atomic_write_json(path, selected)
    return selected


def _load_session(path: Path | str) -> tuple[Path, dict[str, object]]:
    record_path = _absolute(path, name="scratch record")
    try:
        record = read_json(record_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ExternalScratchError("scratch record is missing or invalid") from exc
    if record.get("schema_version") != EXTERNAL_SCRATCH_SCHEMA_VERSION:
        raise ExternalScratchError("unsupported external scratch record schema")
    return record_path, record


def prepare_external_scratch(
    *,
    scratch_root: Path | str,
    mount_path: Path | str,
    canonical_attempt_directory: Path | str,
    target_id: str,
    attempt_id: str,
    input_files: Mapping[str, Path | str],
    output_files: Mapping[str, Path | str],
    process_environment: Mapping[str, str] | None = None,
    allow_empty_output_roles: Sequence[str] = ("stderr",),
    minimum_free_gib: float,
    mount_checker: Callable[[Path], bool] = os.path.ismount,
    usage_provider: Callable[[Path], DiskUsage] = shutil.disk_usage,
) -> dict[str, object]:
    """Create one unique execution directory after mounted-volume preflight."""

    mount = _absolute(mount_path, name="mount path")
    if not mount.is_dir() or mount.resolve(strict=True) != mount:
        raise ExternalScratchError("expected mounted volume is missing or indirect")
    if not mount_checker(mount):
        raise ExternalScratchError(
            f"expected volume is not mounted: {mount}", code="MISSING_SCRATCH_VOLUME"
        )

    root = _absolute(scratch_root, name="scratch root")
    parent = root.parent.resolve(strict=True)
    if parent != mount:
        _strict_child(parent, mount, name="scratch-root parent")
    if root.exists() and (root.is_symlink() or root.resolve(strict=True) != root):
        raise ExternalScratchError("scratch root must not traverse a symlink")
    root.mkdir(exist_ok=True)
    root = root.resolve(strict=True)
    _strict_child(root, mount, name="scratch root")
    if os.stat(root).st_dev != os.stat(mount).st_dev:
        raise ExternalScratchError("scratch root is not on the expected mounted volume")

    attempt = _absolute(
        canonical_attempt_directory, name="canonical attempt directory"
    )
    if not attempt.is_dir() or attempt.is_symlink() or attempt.resolve(strict=True) != attempt:
        raise ExternalScratchError("canonical attempt directory is missing or indirect")
    if not _disjoint(root, attempt):
        raise ExternalScratchError("scratch root overlaps the canonical attempt")
    capacity = check_storage_capacity(
        root, minimum_free_gb=minimum_free_gib, usage_provider=usage_provider
    )

    record_path = attempt / EXTERNAL_SCRATCH_RECORD
    if record_path.exists():
        raise ExternalScratchError("external scratch record already exists for attempt")
    token = uuid4().hex
    prefix_target = re.sub(r"[^A-Za-z0-9]", "", str(target_id))[:12] or "target"
    prefix_attempt = re.sub(r"[^A-Za-z0-9_-]", "", str(attempt_id)) or "attempt"
    execution_stem = f"cmw-{prefix_target}-{prefix_attempt}"
    execution = Path(
        tempfile.mkdtemp(
            prefix=f"cmw-orca-{prefix_target}-{prefix_attempt}-", dir=root
        )
    ).resolve(strict=True)
    try:
        _strict_child(execution, root, name="execution directory")
        if not _disjoint(execution, attempt):
            raise ExternalScratchError("execution directory overlaps canonical data")
        marker = {
            "schema_version": EXTERNAL_SCRATCH_SCHEMA_VERSION,
            "owner_token": token,
            "execution_directory": str(execution),
            "scratch_root": str(root),
            "canonical_attempt_directory": str(attempt),
            "canonical_record": str(record_path),
            "created_at": _now(),
        }
        atomic_write_json(execution / OWNER_RECORD, marker)

        inputs: list[dict[str, object]] = []
        outputs: list[dict[str, object]] = []
        used_names: set[str] = {OWNER_RECORD}
        primary_stem = ""
        for raw_role, raw_path in input_files.items():
            role = _role(raw_role)
            source = _direct_existing_path(raw_path, name=f"input {role}")
            _strict_child(source, attempt, name=f"input {role}")
            if not source.is_file():
                raise ExternalScratchError(f"input {role} is missing or indirect")
            execution_name = source.name
            if role == "primary":
                primary_stem = source.stem
                execution_name = f"{execution_stem}{source.suffix}"
            if execution_name in used_names:
                raise ExternalScratchError("execution filenames must be unique")
            used_names.add(execution_name)
            destination = execution / execution_name
            identity = _verified_copy(source, destination)
            inputs.append(
                {
                    "role": role,
                    "canonical_path": str(source),
                    "execution_path": str(destination),
                    **identity,
                }
            )
        if not primary_stem:
            raise ExternalScratchError("scratch execution requires a primary input")
        empty_roles = {_role(item) for item in allow_empty_output_roles}
        for raw_role, raw_path in output_files.items():
            role = _role(raw_role)
            destination = _absolute(raw_path, name=f"output {role}")
            _strict_child(destination, attempt, name=f"output {role}")
            execution_name = destination.name
            if execution_name == primary_stem or execution_name.startswith(
                f"{primary_stem}."
            ):
                execution_name = f"{execution_stem}{execution_name[len(primary_stem):]}"
            if execution_name in used_names:
                raise ExternalScratchError("execution filenames must be unique")
            used_names.add(execution_name)
            outputs.append(
                {
                    "role": role,
                    "canonical_path": str(destination),
                    "execution_path": str(execution / execution_name),
                    "allow_empty": role in empty_roles,
                }
            )
        if not inputs or not outputs:
            raise ExternalScratchError("scratch execution requires inputs and outputs")
        unknown_empty = empty_roles - {str(item["role"]) for item in outputs}
        if unknown_empty:
            raise ExternalScratchError(
                "allow-empty roles are not declared outputs: "
                + ", ".join(sorted(unknown_empty))
            )

        environment: dict[str, str] = {}
        for raw_name, raw_value in (process_environment or {}).items():
            name = str(raw_name)
            value = str(raw_value)
            if (
                ENVIRONMENT_NAME_PATTERN.fullmatch(name) is None
                or "\x00" in value
                or "\n" in value
                or "\r" in value
            ):
                raise ExternalScratchError("process environment is malformed")
            environment[name] = value

        record: dict[str, object] = {
            "schema_version": EXTERNAL_SCRATCH_SCHEMA_VERSION,
            "status": "PREPARED",
            "created_at": _now(),
            "target_id": str(target_id),
            "attempt_id": str(attempt_id),
            "owner_token": token,
            "mount_path": str(mount),
            "scratch_root": str(root),
            "execution_directory": str(execution),
            "execution_stem": execution_stem,
            "canonical_attempt_directory": str(attempt),
            "inputs": inputs,
            "outputs": outputs,
            "process_environment": dict(sorted(environment.items())),
            "preflight": {
                **capacity,
                "mounted_volume": True,
                "same_device_as_mount": True,
                "writable_unique_directory": True,
            },
        }
        return _write_session(record_path, record)
    except Exception:
        if execution.is_dir():
            try:
                _owned_marker(execution, token, record_path)
            except ExternalScratchError:
                pass
            else:
                shutil.rmtree(execution)
        raise


def mark_external_scratch_running(record_path: Path | str) -> dict[str, object]:
    path, record = _load_session(record_path)
    if record.get("status") != "PREPARED":
        raise ExternalScratchError("only a prepared scratch session may start")
    execution = _direct_existing_path(
        str(record["execution_directory"]), name="execution directory"
    )
    _owned_marker(execution, str(record["owner_token"]), path)
    record["status"] = "RUNNING"
    record["started_at"] = _now()
    return _write_session(path, record)


def mark_external_scratch_failed(
    record_path: Path | str, *, reason: str
) -> dict[str, object]:
    path, record = _load_session(record_path)
    if record.get("status") == "CLEANED":
        return record
    record["status"] = "FAILED"
    record["failure_reason"] = str(reason).strip() or "external execution failed"
    record["scratch_preserved"] = Path(
        str(record["execution_directory"])
    ).is_dir()
    return _write_session(path, record)


def copy_back_external_scratch(
    record_path: Path | str,
    *,
    process_exit_code: int,
    replace_provider: Callable[[Path, Path], object] = os.replace,
) -> dict[str, object]:
    """Copy only declared outputs through verified canonical staging."""

    path, record = _load_session(record_path)
    if record.get("status") not in {"RUNNING", "COPYING", "COPY_FAILED"}:
        raise ExternalScratchError("scratch session is not ready for copy-back")
    execution = _direct_existing_path(
        str(record["execution_directory"]), name="execution directory"
    )
    attempt = _direct_existing_path(
        str(record["canonical_attempt_directory"]),
        name="canonical attempt directory",
    )
    token = str(record["owner_token"])
    _owned_marker(execution, token, path)
    if not attempt.is_dir():
        raise ExternalScratchError("canonical attempt disappeared before copy-back")

    output_records = tuple(record.get("outputs", ()))
    if not output_records or any(not isinstance(item, Mapping) for item in output_records):
        raise ExternalScratchError("declared output contract is invalid")
    expected: dict[str, dict[str, object]] = {}
    for raw in output_records:
        item = dict(raw)  # type: ignore[arg-type]
        role = _role(item.get("role"))
        source = _absolute(
            str(item.get("execution_path", "")), name=f"scratch output {role}"
        )
        destination = _absolute(
            str(item.get("canonical_path", "")), name=f"canonical output {role}"
        )
        _strict_child(source, execution, name=f"scratch output {role}")
        _strict_child(destination, attempt, name=f"canonical output {role}")
        if (
            not source.is_file()
            or source.is_symlink()
            or source.resolve(strict=True) != source
        ):
            record["status"] = "COPY_FAILED"
            record["copyback_error"] = f"required scratch output is missing: {role}"
            _write_session(path, record)
            raise ExternalScratchError(record["copyback_error"], code="MISSING_COPYBACK_OUTPUT")
        identity = _identity(source)
        if (
            process_exit_code == 0
            and not bool(item.get("allow_empty"))
            and identity["size_bytes"] == 0
        ):
            raise ExternalScratchError(
                f"successful execution produced an empty required output: {role}",
                code="EMPTY_COPYBACK_OUTPUT",
            )
        expected[role] = {
            **item,
            **identity,
        }

    staging = attempt / f".cmw-external-copyback-{token[:12]}"
    staging_marker = staging / COPYBACK_OWNER_RECORD
    try:
        record["status"] = "COPYING"
        record["process_exit_code"] = process_exit_code
        _write_session(path, record)
        if staging.exists():
            marker = read_json(staging_marker)
            if marker.get("owner_token") != token:
                raise ExternalScratchError(
                    "copy-back staging ownership does not match",
                    code="UNSAFE_COPYBACK_STAGING",
                )
        else:
            staging.mkdir()
            atomic_write_json(staging_marker, {"owner_token": token})

        for role, item in expected.items():
            source = Path(str(item["execution_path"]))
            staged = staging / Path(str(item["canonical_path"])).name
            if staged.is_file() and _identity(staged) == {
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
            }:
                continue
            _verified_copy(source, staged)

        for role, item in expected.items():
            staged = staging / Path(str(item["canonical_path"])).name
            destination = Path(str(item["canonical_path"]))
            identity = {
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
            }
            if destination.exists():
                if not destination.is_file() or _identity(destination) != identity:
                    raise ExternalScratchError(
                        f"canonical output collision for {role}",
                        code="COPYBACK_COLLISION",
                    )
            else:
                replace_provider(staged, destination)
            if _identity(destination) != identity:
                raise ExternalScratchError(
                    f"canonical output verification failed for {role}",
                    code="FAILED_COPYBACK_VERIFICATION",
                )

        listed = {
            Path(str(item["execution_path"])).name
            for item in (*record.get("inputs", ()), *output_records)  # type: ignore[arg-type]
            if isinstance(item, Mapping)
        } | {OWNER_RECORD}
        unlisted = sorted(
            item.name for item in execution.iterdir() if item.name not in listed
        )
        if staging.is_dir():
            marker = read_json(staging_marker)
            if marker.get("owner_token") != token:
                raise ExternalScratchError("copy-back staging ownership changed")
            shutil.rmtree(staging)
        record["status"] = "COPIED"
        record["copyback"] = {
            "completed_at": _now(),
            "files": {role: item for role, item in sorted(expected.items())},
            "unlisted_entry_count": len(unlisted),
            "unlisted_entry_sample": unlisted[:32],
        }
        record.pop("copyback_error", None)
        return _write_session(path, record)
    except Exception as exc:
        record["status"] = "COPY_FAILED"
        record["copyback_error"] = str(exc)
        record["scratch_preserved"] = execution.is_dir()
        _write_session(path, record)
        raise


def cleanup_external_scratch(
    record_path: Path | str,
    *,
    finalization_metadata_path: Path | str,
    mount_checker: Callable[[Path], bool] = os.path.ismount,
    cleanup_provider: Callable[[Path], object] = shutil.rmtree,
) -> dict[str, object]:
    """Delete exactly one owned directory after canonical scientific success."""

    path, record = _load_session(record_path)
    metadata_path = _absolute(
        finalization_metadata_path, name="finalization metadata"
    )
    try:
        finalized = read_json(metadata_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ExternalScratchError("canonical finalization metadata is invalid") from exc
    if (
        finalized.get("reusable") is not True
        or finalized.get("execution", {}).get("status") != "SUCCESS"
        or finalized.get("scientific", {}).get("status") != "VALID"
        or finalized.get("target", {}).get("target_id") != record.get("target_id")
        or finalized.get("attempt", {}).get("attempt_id") != record.get("attempt_id")
    ):
        raise ExternalScratchError(
            "canonical scientific finalization has not succeeded",
            code="FINALIZATION_REQUIRED_FOR_CLEANUP",
        )

    attempt = _direct_existing_path(
        str(record["canonical_attempt_directory"]),
        name="canonical attempt directory",
    )
    _strict_child(metadata_path, attempt, name="finalization metadata")
    copyback = record.get("copyback")
    if not isinstance(copyback, Mapping) or not isinstance(copyback.get("files"), Mapping):
        raise ExternalScratchError("verified copy-back evidence is missing")
    for role, raw in copyback["files"].items():
        if not isinstance(raw, Mapping):
            raise ExternalScratchError("copy-back evidence is malformed")
        canonical = _direct_existing_path(
            str(raw.get("canonical_path", "")), name=f"canonical output {role}"
        )
        _strict_child(canonical, attempt, name=f"canonical output {role}")
        if not canonical.is_file() or _identity(canonical) != {
            "size_bytes": raw.get("size_bytes"),
            "sha256": raw.get("sha256"),
        }:
            raise ExternalScratchError(
                f"canonical output is not independently complete: {role}",
                code="CANONICAL_OUTPUT_INCOMPLETE",
            )
    if record.get("status") == "CLEANED":
        return record

    mount = _direct_existing_path(str(record["mount_path"]), name="mount path")
    root = _direct_existing_path(str(record["scratch_root"]), name="scratch root")
    execution = _absolute(
        str(record["execution_directory"]), name="execution directory"
    )
    if not mount.is_dir() or not mount_checker(mount):
        raise ExternalScratchError("expected scratch volume is no longer mounted")
    if not root.is_dir():
        raise ExternalScratchError("scratch root is missing")
    _strict_child(root, mount, name="scratch root")
    _strict_child(execution, root, name="execution directory")
    if not _disjoint(execution, attempt):
        raise ExternalScratchError("execution directory overlaps canonical data")

    if not execution.exists():
        if record.get("status") != "FINALIZED":
            raise ExternalScratchError(
                "scratch disappeared before cleanup authorization",
                code="UNSAFE_SCRATCH_OWNERSHIP",
            )
    else:
        if (
            not execution.is_dir()
            or execution.is_symlink()
            or execution.resolve(strict=True) != execution
        ):
            raise ExternalScratchError("execution path is not a safe directory")
        _owned_marker(execution, str(record["owner_token"]), path)
        record["status"] = "FINALIZED"
        record["scientific_finalization"] = {
            "metadata_path": str(metadata_path),
            "validated_at": _now(),
        }
        _write_session(path, record)
        try:
            cleanup_provider(execution)
        except Exception as exc:
            record["status"] = "CLEANUP_FAILED"
            record["cleanup_error"] = str(exc)
            _write_session(path, record)
            raise ExternalScratchError("owned scratch deletion failed") from exc
        if execution.exists():
            raise ExternalScratchError("owned scratch directory still exists after deletion")

    record["status"] = "CLEANED"
    record["scratch_preserved"] = False
    record["cleaned_at"] = _now()
    record.pop("cleanup_error", None)
    return _write_session(path, record)


__all__ = [
    "EXTERNAL_SCRATCH_RECORD",
    "EXTERNAL_SCRATCH_SCHEMA_VERSION",
    "ExternalScratchError",
    "cleanup_external_scratch",
    "copy_back_external_scratch",
    "mark_external_scratch_failed",
    "mark_external_scratch_running",
    "prepare_external_scratch",
]
