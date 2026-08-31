"""Generic, non-chemical Multiwfn command and output contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from cmw.core.provenance import file_hash


MULTIWFN_ADAPTER_SCHEMA_VERSION = 1


def _relative_path(value: str, *, name: str) -> str:
    path = Path(value)
    if not value or not path.parts or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be a safe relative path")
    return path.as_posix()


@dataclass(frozen=True)
class MultiwfnOutputSpec:
    """One semantic output role and its adapter-visible file locations."""

    role: str
    raw_path: str
    output_path: str
    required: bool = True
    media_type: str = "application/x-gaussian-cube"
    visualization: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.role or not self.role.strip():
            raise ValueError("Multiwfn output role is required")
        if not self.media_type or not self.media_type.strip():
            raise ValueError("Multiwfn output media_type is required")
        object.__setattr__(self, "role", self.role.strip())
        object.__setattr__(
            self, "raw_path", _relative_path(self.raw_path, name="raw_path")
        )
        object.__setattr__(
            self, "output_path", _relative_path(self.output_path, name="output_path")
        )
        object.__setattr__(self, "media_type", self.media_type.strip())
        object.__setattr__(self, "visualization", dict(self.visualization))

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "raw_path": self.raw_path,
            "output_path": self.output_path,
            "required": self.required,
            "media_type": self.media_type,
            "visualization": dict(self.visualization),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MultiwfnOutputSpec":
        role = value.get("role")
        raw_path = value.get("raw_path", value.get("raw_filename"))
        output_path = value.get("output_path", value.get("filename", raw_path))
        if not isinstance(role, str):
            raise ValueError("Multiwfn output role must be a string")
        if not isinstance(raw_path, str) or not isinstance(output_path, str):
            raise ValueError("Multiwfn raw_path and output_path must be strings")
        required = value.get("required", True)
        if not isinstance(required, bool):
            raise ValueError("Multiwfn output required must be a boolean")
        media_type = value.get("media_type", "application/x-gaussian-cube")
        if not isinstance(media_type, str):
            raise ValueError("Multiwfn output media_type must be a string")
        visualization = value.get("visualization", {})
        if not isinstance(visualization, Mapping):
            raise ValueError("Multiwfn output visualization must be a mapping")
        return cls(
            role,
            raw_path,
            output_path,
            required,
            media_type,
            dict(visualization),
        )


@dataclass(frozen=True)
class MultiwfnAuxiliaryInputSpec:
    """Immutable source identity exposed to Multiwfn under a short local path."""

    role: str
    source_identity: Mapping[str, object]
    local_path: str

    def __post_init__(self) -> None:
        if not self.role or not self.role.strip():
            raise ValueError("Multiwfn auxiliary input role is required")
        local_path = _relative_path(self.local_path, name="local_path")
        if len(Path(local_path).parts) != 1:
            raise ValueError("Multiwfn auxiliary local_path must be a filename")
        source = dict(self.source_identity)
        path = source.get("path")
        sha256 = source.get("sha256")
        size_bytes = source.get("size_bytes")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("Multiwfn auxiliary source path must be absolute")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ValueError("Multiwfn auxiliary source SHA-256 is required")
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError("Multiwfn auxiliary source size is required")
        object.__setattr__(self, "role", self.role.strip())
        object.__setattr__(self, "source_identity", source)
        object.__setattr__(self, "local_path", local_path)

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "source_identity": dict(self.source_identity),
            "local_path": self.local_path,
        }


def validate_auxiliary_input_specs(
    values: Sequence[MultiwfnAuxiliaryInputSpec],
) -> tuple[MultiwfnAuxiliaryInputSpec, ...]:
    inputs = tuple(values)
    roles = [item.role for item in inputs]
    paths = [item.local_path for item in inputs]
    if len(set(roles)) != len(roles):
        raise ValueError("Multiwfn auxiliary input roles must be unique")
    if len(set(paths)) != len(paths):
        raise ValueError("Multiwfn auxiliary local paths must be unique")
    return inputs


def materialize_auxiliary_inputs(
    attempt_directory: Path,
    values: Sequence[MultiwfnAuxiliaryInputSpec],
) -> dict[str, dict[str, object]]:
    """Create deterministic attempt-local aliases without weakening provenance."""

    attempt = attempt_directory.expanduser().resolve(strict=True)
    if not attempt.is_dir():
        raise ValueError("Multiwfn attempt directory is not a directory")
    materialized: dict[str, dict[str, object]] = {}
    for item in validate_auxiliary_input_specs(values):
        source = Path(str(item.source_identity["path"])).expanduser().resolve(
            strict=True
        )
        if not source.is_file():
            raise ValueError(f"Multiwfn auxiliary source is not a file: {source}")
        if (
            file_hash(source) != item.source_identity["sha256"]
            or source.stat().st_size != item.source_identity["size_bytes"]
        ):
            raise ValueError(
                f"Multiwfn auxiliary source identity changed before execution: {source}"
            )
        alias = attempt / item.local_path
        if alias.is_symlink():
            if alias.resolve(strict=True) != source:
                raise ValueError(
                    f"Multiwfn auxiliary alias conflicts with its source: {alias}"
                )
        elif alias.exists():
            raise ValueError(f"Multiwfn auxiliary alias already exists: {alias}")
        else:
            alias.symlink_to(source)
        materialized[item.role] = {
            **item.to_dict(),
            "materialized_path": str(alias),
        }
    return materialized


def runtime_provenance_with_alias_manifest(
    runtime: Mapping[str, object], attempt_directory: Path
) -> dict[str, object]:
    """Attach the immutable shell alias record needed for finalization."""

    attempt = attempt_directory.expanduser().resolve(strict=True)
    manifest = (attempt / "multiwfn-runtime-alias.txt").resolve(strict=True)
    if not manifest.is_file() or manifest.stat().st_size == 0:
        raise ValueError("Multiwfn runtime alias manifest is missing or empty")
    result = dict(runtime)
    result.update(
        {
            "alias_manifest_path": str(manifest),
            "alias_manifest_sha256": file_hash(manifest),
            "alias_manifest_size_bytes": manifest.stat().st_size,
        }
    )
    return result


def validate_output_specs(
    values: Sequence[MultiwfnOutputSpec],
) -> tuple[MultiwfnOutputSpec, ...]:
    outputs = tuple(values)
    if not outputs:
        raise ValueError("Multiwfn analysis requires at least one output")
    roles = [item.role for item in outputs]
    paths = [item.output_path for item in outputs]
    raw_paths = [item.raw_path for item in outputs]
    if len(set(roles)) != len(roles):
        raise ValueError("Multiwfn output roles must be unique")
    if len(set(paths)) != len(paths):
        raise ValueError("Multiwfn output paths must be unique")
    if len(set(raw_paths)) != len(raw_paths):
        raise ValueError("Multiwfn raw output paths must be unique")
    if not any(item.required for item in outputs):
        raise ValueError("Multiwfn analysis requires at least one required output")
    return outputs


class MultiwfnAdapterError(ValueError):
    """Structured failure from command or output validation."""

    def __init__(
        self,
        code: str,
        reason: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "FAILED",
            "code": self.code,
            "reason": self.reason,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class MultiwfnCommandSpec:
    """Deterministic launch intent plus immutable runtime provenance."""

    argv: tuple[str, ...]
    cwd: str
    stdin_path: str
    environment: Mapping[str, str]
    runtime: Mapping[str, object]
    outputs: tuple[MultiwfnOutputSpec, ...]

    def __post_init__(self) -> None:
        if len(self.argv) != 2 or not all(self.argv):
            raise ValueError("Multiwfn command must contain executable and source")
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "environment", dict(self.environment))
        object.__setattr__(self, "runtime", dict(self.runtime))
        object.__setattr__(self, "outputs", validate_output_specs(self.outputs))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": MULTIWFN_ADAPTER_SCHEMA_VERSION,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "stdin_path": self.stdin_path,
            "environment": dict(self.environment),
            "runtime": dict(self.runtime),
            "outputs": [item.to_dict() for item in self.outputs],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MultiwfnCommandSpec":
        if value.get("schema_version") != MULTIWFN_ADAPTER_SCHEMA_VERSION:
            raise ValueError("unsupported Multiwfn command schema")
        argv = value.get("argv")
        if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
            raise ValueError("Multiwfn command argv must be a list")
        environment = value.get("environment")
        runtime = value.get("runtime")
        outputs = value.get("outputs")
        if not isinstance(environment, Mapping) or not isinstance(runtime, Mapping):
            raise ValueError(
                "Multiwfn command environment and runtime must be mappings"
            )
        if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
            raise ValueError("Multiwfn command outputs must be a list")
        if not all(isinstance(item, Mapping) for item in outputs):
            raise ValueError("Multiwfn command outputs must contain mappings")
        return cls(
            tuple(str(item) for item in argv),
            str(value.get("cwd", "")),
            str(value.get("stdin_path", "")),
            {str(key): str(item) for key, item in environment.items()},
            dict(runtime),
            tuple(MultiwfnOutputSpec.from_mapping(item) for item in outputs),
        )


def _runtime_path(
    runtime: Mapping[str, object], key: str, *, executable: bool = False
) -> Path:
    value = runtime.get(key)
    if not isinstance(value, str) or not value:
        raise MultiwfnAdapterError(
            "RUNTIME_PROVENANCE_INCOMPLETE", f"Multiwfn runtime lacks {key}"
        )
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise MultiwfnAdapterError(
            "RUNTIME_PATH_INVALID", f"Multiwfn runtime path is not a file: {path}"
        )
    if executable and not path.stat().st_mode & 0o111:
        raise MultiwfnAdapterError(
            "COMMAND_NOT_EXECUTABLE", f"Multiwfn executable is not executable: {path}"
        )
    return path


def build_command_spec(
    *,
    runtime: Mapping[str, object],
    source_path: Path,
    attempt_directory: Path,
    stdin_path: Path,
    outputs: Sequence[MultiwfnOutputSpec],
) -> MultiwfnCommandSpec:
    """Build and validate the command contract without launching Multiwfn."""

    executable = _runtime_path(runtime, "executable", executable=True)
    settings = _runtime_path(runtime, "settings_path")
    source = source_path.expanduser().resolve(strict=True)
    menu = stdin_path.expanduser().resolve(strict=True)
    cwd = attempt_directory.expanduser().resolve(strict=True)
    if not source.is_file() or not menu.is_file() or not cwd.is_dir():
        raise MultiwfnAdapterError(
            "COMMAND_INPUT_INVALID",
            "Multiwfn source, menu, and working directory are required",
        )
    checks = (
        (executable, "executable_sha256"),
        (settings, "settings_sha256"),
    )
    for path, key in checks:
        expected = runtime.get(key)
        if not isinstance(expected, str) or file_hash(path) != expected:
            raise MultiwfnAdapterError(
                "RUNTIME_INTEGRITY_MISMATCH",
                f"Multiwfn runtime hash does not match {key}",
                details={"path": str(path), "hash_field": key},
            )
    threads = runtime.get("requested_nthreads")
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise MultiwfnAdapterError(
            "RUNTIME_PROVENANCE_INCOMPLETE",
            "Multiwfn runtime requested_nthreads must be a positive integer",
        )
    if not isinstance(runtime.get("version"), str) or not runtime["version"]:
        raise MultiwfnAdapterError(
            "RUNTIME_PROVENANCE_INCOMPLETE", "Multiwfn runtime version is required"
        )
    return MultiwfnCommandSpec(
        argv=(str(executable), str(source)),
        cwd=str(cwd),
        stdin_path=str(menu),
        environment={
            "OMP_NUM_THREADS": str(threads),
            "Multiwfnpath": "<attempt-local-runtime-alias>",
        },
        runtime=dict(runtime),
        outputs=tuple(outputs),
    )


def validate_command_spec(command: MultiwfnCommandSpec) -> None:
    """Revalidate a stored command against its recorded runtime files."""

    rebuilt = build_command_spec(
        runtime=command.runtime,
        source_path=Path(command.argv[1]),
        attempt_directory=Path(command.cwd),
        stdin_path=Path(command.stdin_path),
        outputs=command.outputs,
    )
    if rebuilt.to_dict() != command.to_dict():
        raise MultiwfnAdapterError(
            "COMMAND_PROVENANCE_MISMATCH",
            "stored Multiwfn command does not match its runtime provenance",
        )


def normalize_output_paths(
    directory: Path, outputs: Sequence[MultiwfnOutputSpec]
) -> dict[str, str]:
    """Move known raw files to configured public paths without guessing names."""

    root = directory.resolve(strict=True)
    normalized: dict[str, str] = {}
    for output in validate_output_specs(outputs):
        raw = root / output.raw_path
        public = root / output.output_path
        if raw == public:
            if public.exists():
                normalized[output.raw_path] = output.output_path
            continue
        if raw.exists() and public.exists():
            raise MultiwfnAdapterError(
                "OUTPUT_CONFLICT",
                f"both raw and public Multiwfn outputs exist for {output.role}",
                details={"raw_path": str(raw), "output_path": str(public)},
            )
        if raw.exists():
            public.parent.mkdir(parents=True, exist_ok=True)
            raw.replace(public)
            normalized[output.raw_path] = output.output_path
        elif public.exists():
            normalized[output.raw_path] = output.output_path
    return normalized


def discover_outputs(
    directory: Path, outputs: Sequence[MultiwfnOutputSpec]
) -> dict[str, Path]:
    """Resolve declared outputs and report every missing required role together."""

    root = directory.resolve(strict=True)
    discovered: dict[str, Path] = {}
    missing: dict[str, str] = {}
    for output in validate_output_specs(outputs):
        path = root / output.output_path
        if path.is_file() and path.stat().st_size > 0:
            discovered[output.role] = path
        elif output.required:
            missing[output.role] = str(path)
    if missing:
        raise MultiwfnAdapterError(
            "MISSING_REQUIRED_OUTPUT",
            "Multiwfn did not produce all required outputs",
            details={"missing_outputs": missing},
        )
    return discovered


__all__ = [
    "MULTIWFN_ADAPTER_SCHEMA_VERSION",
    "MultiwfnAdapterError",
    "MultiwfnAuxiliaryInputSpec",
    "MultiwfnCommandSpec",
    "MultiwfnOutputSpec",
    "build_command_spec",
    "discover_outputs",
    "materialize_auxiliary_inputs",
    "runtime_provenance_with_alias_manifest",
    "normalize_output_paths",
    "validate_command_spec",
    "validate_auxiliary_input_specs",
    "validate_output_specs",
]
