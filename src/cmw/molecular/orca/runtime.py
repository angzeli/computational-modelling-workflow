"""Fail-closed ORCA MPI runtime discovery and dynamic-link validation."""

from __future__ import annotations

import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from cmw.core.execution_profiles import ExecutionProfile, MpiRuntimePolicy
from cmw.core.provenance import file_hash, stable_hash


ORCA_RUNTIME_SCHEMA_VERSION = 3
SYSTEM_LIBRARY_PREFIXES = (Path("/usr/lib"), Path("/System/Library"))
ORCA_REQUIRED_MPI_DATATYPES = (
    "integer4",
    "integer8",
    "real4",
    "real8",
    "complex8",
    "complex16",
)


class OrcaRuntimeError(ValueError):
    """Raised when a parallel ORCA runtime cannot be resolved completely."""

    code = "FAILED_RUNTIME_CONTRACT"


def _failure(reason: str) -> OrcaRuntimeError:
    return OrcaRuntimeError(f"{OrcaRuntimeError.code}: {reason}")


def _run(command: Sequence[str], *, environment: Mapping[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            tuple(command),
            check=True,
            capture_output=True,
            text=True,
            env=dict(environment) if environment is not None else None,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise _failure(f"runtime inspection command failed: {' '.join(command)}") from exc
    return (result.stdout or result.stderr).strip()


def _parse_otool_dependencies(value: str) -> tuple[str, ...]:
    dependencies: list[str] = []
    for line in value.splitlines()[1:]:
        selected = line.strip()
        if not selected:
            continue
        dependencies.append(selected.split(" (compatibility version", 1)[0])
    return tuple(dependencies)


def _parse_otool_rpaths(value: str) -> tuple[str, ...]:
    lines = value.splitlines()
    rpaths: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for candidate in lines[index + 1 : index + 6]:
            match = re.match(r"\s*path\s+(.+?)\s+\(offset\s+\d+\)", candidate)
            if match:
                rpaths.append(match.group(1))
                break
    return tuple(rpaths)


def _expand_loader_token(value: str, *, owner: Path, executable: Path) -> Path | None:
    replacements = {
        "@loader_path": owner.parent,
        "@executable_path": executable.parent,
    }
    for token, directory in replacements.items():
        if value == token:
            return directory
        prefix = token + "/"
        if value.startswith(prefix):
            return directory / value.removeprefix(prefix)
    if value.startswith("@"):
        return None
    return Path(value)


def _is_system_library(path: Path) -> bool:
    return any(path == prefix or prefix in path.parents for prefix in SYSTEM_LIBRARY_PREFIXES)


def _resolve_macho_dependency(
    dependency: str,
    *,
    owner: Path,
    executable: Path,
    library_directories: Sequence[Path],
    rpaths: Sequence[str],
) -> Path | None:
    if dependency.startswith("@rpath/"):
        suffix = dependency.removeprefix("@rpath/")
        candidates: list[Path] = []
        for value in rpaths:
            expanded = _expand_loader_token(value, owner=owner, executable=executable)
            if expanded is not None:
                candidates.append(expanded / suffix)
        candidates.extend(directory / suffix for directory in library_directories)
        candidates.extend((owner.parent / suffix, executable.parent / "lib" / suffix))
    elif dependency.startswith("@"):
        expanded = _expand_loader_token(
            dependency, owner=owner, executable=executable
        )
        candidates = [expanded] if expanded is not None else []
    else:
        path = Path(dependency)
        if path.is_absolute():
            if _is_system_library(path):
                return path
            candidates = [path]
        else:
            candidates = [
                *(directory / path for directory in library_directories),
                owner.parent / path,
                executable.parent / path,
                executable.parent / "lib" / path,
            ]
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    return None


def _macho_dependencies(binary: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    dependencies = _parse_otool_dependencies(_run(("otool", "-L", str(binary))))
    rpaths = _parse_otool_rpaths(_run(("otool", "-l", str(binary))))
    return dependencies, rpaths


def _inspect_macos_dependencies(
    executable: Path,
    helpers: Sequence[Path],
    library_directories: Sequence[Path],
) -> dict[str, object]:
    queue = list(helpers)
    visited: set[Path] = set()
    edges: list[dict[str, str]] = []
    artifacts: dict[str, dict[str, object]] = {}
    missing: list[dict[str, str]] = []
    while queue:
        owner = queue.pop(0).resolve()
        if owner in visited:
            continue
        visited.add(owner)
        dependencies, rpaths = _macho_dependencies(owner)
        for dependency in dependencies:
            resolved = _resolve_macho_dependency(
                dependency,
                owner=owner,
                executable=executable,
                library_directories=library_directories,
                rpaths=rpaths,
            )
            if resolved is None:
                missing.append({"owner": str(owner), "dependency": dependency})
                continue
            edge = {
                "owner": str(owner),
                "dependency": dependency,
                "resolved": str(resolved),
            }
            edges.append(edge)
            if _is_system_library(resolved):
                continue
            artifacts[str(resolved)] = {
                "size_bytes": resolved.stat().st_size,
                "sha256": file_hash(resolved),
            }
            if resolved.suffix == ".dylib" and resolved not in visited:
                queue.append(resolved)
    if missing:
        example = missing[0]
        raise _failure(
            "unresolved dynamic library "
            f"{example['dependency']} required by {example['owner']}"
        )
    return {
        "format": "mach_o_otool",
        "dependency_edges": sorted(
            edges,
            key=lambda item: (item["owner"], item["dependency"], item["resolved"]),
        ),
        "library_artifacts": dict(sorted(artifacts.items())),
    }


def _launch_overlay(
    dependencies: Mapping[str, object], *, system_name: str
) -> dict[str, object]:
    """Describe files needed when the platform strips dynamic-link variables."""

    if system_name != "Darwin":
        return {"strategy": "environment_library_path", "files": []}
    raw_edges = dependencies.get("dependency_edges")
    if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, (str, bytes)):
        raise _failure("dynamic-library dependency edges are malformed")
    selected: dict[str, Path] = {}
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, Mapping):
            raise _failure("dynamic-library dependency edge is malformed")
        dependency = str(raw_edge.get("dependency", ""))
        if (
            not dependency
            or dependency.startswith("@")
            or Path(dependency).is_absolute()
            or "/" in dependency
        ):
            continue
        source = Path(str(raw_edge.get("resolved", ""))).resolve()
        if not source.is_file() or _is_system_library(source):
            continue
        prior = selected.get(dependency)
        if prior is not None and prior != source:
            raise _failure(
                f"launch overlay dependency {dependency} resolves inconsistently"
            )
        selected[dependency] = source
    return {
        "strategy": "working_directory_symlink",
        "files": [
            {
                "name": name,
                "source": str(source),
                "size_bytes": source.stat().st_size,
                "sha256": file_hash(source),
            }
            for name, source in sorted(selected.items())
        ],
    }


def _parse_ldd_dependencies(value: str) -> tuple[tuple[str, str | None], ...]:
    dependencies: list[tuple[str, str | None]] = []
    for line in value.splitlines():
        selected = line.strip()
        if not selected or selected.startswith("linux-vdso"):
            continue
        if " => " in selected:
            name, resolved = selected.split(" => ", 1)
            path = resolved.split(" ", 1)[0]
            dependencies.append((name, None if path == "not" else path))
        elif selected.startswith("/"):
            dependencies.append((selected.split(" ", 1)[0], selected.split(" ", 1)[0]))
    return tuple(dependencies)


def _inspect_linux_dependencies(
    helpers: Sequence[Path], library_directories: Sequence[Path]
) -> dict[str, object]:
    environment = dict(os.environ)
    prefix = ":".join(str(path) for path in library_directories)
    environment["LD_LIBRARY_PATH"] = prefix + (
        f":{environment['LD_LIBRARY_PATH']}" if environment.get("LD_LIBRARY_PATH") else ""
    )
    edges: list[dict[str, str]] = []
    artifacts: dict[str, dict[str, object]] = {}
    for helper in helpers:
        for dependency, resolved_value in _parse_ldd_dependencies(
            _run(("ldd", str(helper)), environment=environment)
        ):
            if resolved_value is None:
                raise _failure(
                    f"unresolved dynamic library {dependency} required by {helper}"
                )
            resolved = Path(resolved_value).resolve()
            edges.append(
                {
                    "owner": str(helper),
                    "dependency": dependency,
                    "resolved": str(resolved),
                }
            )
            if resolved.is_file() and not _is_system_library(resolved):
                artifacts[str(resolved)] = {
                    "size_bytes": resolved.stat().st_size,
                    "sha256": file_hash(resolved),
                }
    return {
        "format": "elf_ldd",
        "dependency_edges": sorted(
            edges,
            key=lambda item: (item["owner"], item["dependency"], item["resolved"]),
        ),
        "library_artifacts": dict(sorted(artifacts.items())),
    }


def _runtime_environment(
    mpi: MpiRuntimePolicy,
    *,
    system_name: str,
    variables: Mapping[str, str] | None = None,
) -> dict[str, object]:
    if system_name == "Darwin":
        library_variable = "DYLD_LIBRARY_PATH"
    elif system_name == "Linux":
        library_variable = "LD_LIBRARY_PATH"
    else:
        raise _failure(f"unsupported dynamic-link platform: {system_name}")
    result: dict[str, object] = {
        "path_prepend": [str(mpi.bin_directory)],
        "library_path_variable": library_variable,
        "library_path_prepend": [str(path) for path in mpi.library_directories],
    }
    if variables:
        result["variables"] = dict(sorted(variables.items()))
    return result


def _parse_embedded_pmix_version(value: str) -> str | None:
    prefix = "mca:pmix:pmix3x:param:pmix_pmix3x_library_version:value:"
    for line in value.splitlines():
        if line.startswith(prefix):
            version = line.removeprefix(prefix).strip()
            return version or None
    return None


def _darwin_openmpi_compatibility_environment(
    mpi: MpiRuntimePolicy,
    *,
    mpi_version: str,
    pmix_version: str | None,
    system_name: str,
) -> dict[str, str]:
    """Avoid affected macOS PMIx datastore and default-plog shutdown crashes."""

    if (
        system_name != "Darwin"
        or re.search(r"(?<!\d)4\.1\.6(?!\d)", mpi_version) is None
        or pmix_version is None
        or re.search(r"(?<!\d)3\.2\.5(?!\d)", pmix_version) is None
    ):
        return {}
    required_components = {
        "mca_gds_hash": "pmix/mca_gds_hash.*",
        "mca_plog_stdfd": "pmix/mca_plog_stdfd.*",
    }
    for component, pattern in required_components.items():
        candidates = tuple(
            path
            for directory in mpi.library_directories
            for path in directory.glob(pattern)
            if path.is_file() and path.suffix in {".dylib", ".so"}
        )
        if not candidates:
            raise _failure(
                "OpenMPI 4.1.6 on macOS requires PMIx compatibility "
                f"component {component}, but it is missing"
            )
    return {"PMIX_MCA_gds": "hash", "PMIX_MCA_plog": "stdfd"}


def _parse_openmpi_fortran_datatypes(value: str) -> dict[str, bool]:
    """Extract size-specific MPI datatype support from parsable ompi_info."""

    prefix = "compiler:fortran:have:"
    capabilities: dict[str, bool] = {}
    for line in value.splitlines():
        if not line.startswith(prefix):
            continue
        name, separator, availability = line.removeprefix(prefix).rpartition(":")
        if not separator or not name:
            continue
        normalized = availability.strip().casefold()
        if normalized in {"yes", "no"}:
            capabilities[name.casefold()] = normalized == "yes"
    return capabilities


def _inspect_openmpi_capabilities(
    mpi: MpiRuntimePolicy, *, environment: Mapping[str, str]
) -> dict[str, object]:
    """Require the size-specific MPI datatypes used by ORCA parallel helpers."""

    ompi_info = mpi.bin_directory / "ompi_info"
    if not ompi_info.is_file() or not os.access(ompi_info, os.X_OK):
        raise _failure(f"OpenMPI capability inspector is missing: {ompi_info}")
    output = _run(
        (str(ompi_info), "--parsable", "--all"), environment=environment
    )
    available = _parse_openmpi_fortran_datatypes(output)
    missing = [
        name for name in ORCA_REQUIRED_MPI_DATATYPES if not available.get(name, False)
    ]
    if missing:
        raise _failure(
            "OpenMPI lacks ORCA-required Fortran datatype support: "
            + ", ".join(missing)
        )
    return {
        "inspector": str(ompi_info.resolve()),
        "inspector_sha256": file_hash(ompi_info.resolve()),
        "required_fortran_datatypes": list(ORCA_REQUIRED_MPI_DATATYPES),
        "available_fortran_datatypes": {
            name: available[name] for name in ORCA_REQUIRED_MPI_DATATYPES
        },
        "embedded_pmix_version": _parse_embedded_pmix_version(output),
        "status": "PASSED",
    }


def _prepare_runtime(
    *,
    profile_name: str,
    execution_profile_hash: str,
    nprocs: int,
    mpi: MpiRuntimePolicy,
    orca_executable: Path,
) -> dict[str, object]:
    executable = orca_executable.expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise _failure(f"ORCA executable is missing or not executable: {executable}")
    if not mpi.bin_directory.is_dir():
        raise _failure(f"MPI bin directory is missing: {mpi.bin_directory}")
    for directory in mpi.library_directories:
        if not directory.is_dir():
            raise _failure(f"MPI library directory is missing: {directory}")
    mpirun = mpi.bin_directory / "mpirun"
    if not mpirun.is_file() or not os.access(mpirun, os.X_OK):
        raise _failure(f"MPI launcher is missing or not executable: {mpirun}")

    system_name = platform.system()
    environment_contract = _runtime_environment(mpi, system_name=system_name)
    environment = dict(os.environ)
    path_prefix = ":".join(environment_contract["path_prepend"])
    environment["PATH"] = path_prefix + (
        f":{environment['PATH']}" if environment.get("PATH") else ""
    )
    library_variable = str(environment_contract["library_path_variable"])
    library_prefix = ":".join(environment_contract["library_path_prepend"])
    environment[library_variable] = library_prefix + (
        f":{environment[library_variable]}" if environment.get(library_variable) else ""
    )
    mpi_version = _run((str(mpirun), "--version"), environment=environment).splitlines()[0]
    mpi_capabilities = _inspect_openmpi_capabilities(
        mpi, environment=environment
    )
    compatibility_variables = _darwin_openmpi_compatibility_environment(
        mpi,
        mpi_version=mpi_version,
        pmix_version=mpi_capabilities["embedded_pmix_version"],
        system_name=system_name,
    )
    environment_contract = _runtime_environment(
        mpi,
        system_name=system_name,
        variables=compatibility_variables,
    )
    environment.update(compatibility_variables)

    helpers = tuple(
        sorted(
            path.resolve()
            for path in executable.parent.glob("orca_*_mpi")
            if path.is_file() and os.access(path, os.X_OK)
        )
    )
    startup_helper = executable.parent / "orca_startup_mpi"
    if nprocs > 1 and startup_helper.resolve() not in helpers:
        raise _failure(f"parallel ORCA startup helper is missing: {startup_helper}")
    if system_name == "Darwin":
        dependencies = _inspect_macos_dependencies(
            executable, helpers, mpi.library_directories
        )
    elif system_name == "Linux":
        dependencies = _inspect_linux_dependencies(
            helpers, mpi.library_directories
        )
    else:  # pragma: no cover - guarded by _runtime_environment
        raise _failure(f"unsupported dynamic-link platform: {system_name}")
    launch_overlay = _launch_overlay(dependencies, system_name=system_name)

    identity: dict[str, object] = {
        "schema_version": ORCA_RUNTIME_SCHEMA_VERSION,
        "execution_profile": profile_name,
        "execution_profile_hash": execution_profile_hash,
        "nprocs": nprocs,
        "platform": system_name,
        "architecture": platform.machine(),
        "orca": {
            "executable": str(executable),
            "sha256": file_hash(executable),
        },
        "mpi": {
            **mpi.to_dict(),
            "launcher": str(mpirun.resolve()),
            "launcher_sha256": file_hash(mpirun.resolve()),
            "version": mpi_version,
            "capabilities": mpi_capabilities,
        },
        "environment": environment_contract,
        "launch_overlay": launch_overlay,
        "dependency_validation": {
            "helpers_inspected": [str(path) for path in helpers],
            "helper_count": len(helpers),
            **dependencies,
        },
    }
    return {
        **identity,
        "runtime_id": stable_hash(identity),
        "validation": {
            "status": "PASSED",
            "code": "VALID_ORCA_MPI_RUNTIME",
            "checks": {
                "orca_executable": True,
                "mpi_launcher": True,
                "mpi_version": True,
                "mpi_fortran_datatypes": True,
                "mpi_finalize_compatibility": True,
                "dynamic_libraries_resolved": True,
                "parallel_helpers_inspected": bool(helpers),
            },
        },
    }


def prepare_orca_runtime(
    profile: ExecutionProfile, *, orca_executable: Path
) -> dict[str, object]:
    """Build deterministic runtime provenance after resolving all MPI libraries."""

    mpi = profile.orca.mpi
    if profile.orca.nprocs > 1 and mpi is None:
        raise _failure("parallel ORCA execution requires an MPI runtime profile")
    if mpi is None:
        raise _failure("ORCA runtime preparation requires configured MPI paths")
    return _prepare_runtime(
        profile_name=profile.name,
        execution_profile_hash=profile.execution_profile_hash,
        nprocs=profile.orca.nprocs,
        mpi=mpi,
        orca_executable=orca_executable,
    )


def validate_orca_runtime_contract(
    value: Mapping[str, Any], *, orca_executable: Path
) -> dict[str, object]:
    """Rebuild a stored runtime contract and reject any path or binary drift."""

    try:
        if value.get("schema_version") != ORCA_RUNTIME_SCHEMA_VERSION:
            raise _failure("unsupported ORCA runtime schema")
        mpi_value = value["mpi"]
        if not isinstance(mpi_value, Mapping):
            raise _failure("stored MPI runtime is malformed")
        raw_libraries = mpi_value["library_directories"]
        if not isinstance(raw_libraries, Sequence) or isinstance(
            raw_libraries, (str, bytes)
        ):
            raise _failure("stored MPI library directories are malformed")
        mpi = MpiRuntimePolicy(
            Path(str(mpi_value["bin_directory"])),
            tuple(Path(str(path)) for path in raw_libraries),
        )
        rebuilt = _prepare_runtime(
            profile_name=str(value["execution_profile"]),
            execution_profile_hash=str(value["execution_profile_hash"]),
            nprocs=int(value["nprocs"]),
            mpi=mpi,
            orca_executable=orca_executable,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, OrcaRuntimeError):
            raise
        raise _failure("stored ORCA runtime contract is malformed") from exc
    if rebuilt["runtime_id"] != value.get("runtime_id"):
        raise _failure("stored ORCA runtime identity no longer matches this machine")
    return rebuilt


def _probe_macos_loader(
    value: Mapping[str, Any], *, working_directory: Path
) -> dict[str, object]:
    dependency_validation = value.get("dependency_validation")
    if not isinstance(dependency_validation, Mapping):
        raise _failure("stored ORCA runtime lacks dependency validation")
    raw_helpers = dependency_validation.get("helpers_inspected")
    if not isinstance(raw_helpers, Sequence) or isinstance(raw_helpers, (str, bytes)):
        raise _failure("stored ORCA helper list is malformed")
    startup_helpers = [
        Path(str(path)).resolve()
        for path in raw_helpers
        if Path(str(path)).name == "orca_startup_mpi"
    ]
    if len(startup_helpers) != 1 or not startup_helpers[0].is_file():
        raise _failure("stored ORCA runtime lacks a unique startup helper")
    helper = startup_helpers[0]
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith("DYLD_"):
            environment.pop(name)
    runtime = runtime_environment(value)
    path_prefix = os.pathsep.join(runtime["path_prepend"])
    environment["PATH"] = path_prefix + (
        f"{os.pathsep}{environment['PATH']}" if environment.get("PATH") else ""
    )
    command = (
        "/bin/sh",
        "-c",
        'exec "$1"',
        "cmw-orca-loader-probe",
        str(helper),
    )
    try:
        result = subprocess.run(
            command,
            cwd=working_directory,
            env=environment,
            input="",
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _failure("ORCA MPI loader probe could not complete") from exc
    combined = "\n".join((result.stdout, result.stderr)).lower()
    dyld_markers = (
        "library not loaded:",
        "dyld[",
        "image not found",
        "no suitable image found",
    )
    if any(marker in combined for marker in dyld_markers):
        raise _failure("ORCA MPI loader probe found an unresolved dynamic library")
    return {
        "status": "PASSED",
        "code": "VALID_ORCA_MPI_LOADER",
        "helper": str(helper),
        "shell": "/bin/sh",
        "exit_code": result.returncode,
        "dyld_failure": False,
        "stdout_hash": stable_hash({"text": result.stdout}),
        "stderr_hash": stable_hash({"text": result.stderr}),
    }


def materialize_orca_runtime_contract(
    value: Mapping[str, Any],
    *,
    orca_executable: Path,
    working_directory: Path,
    copy_working_directory_overlay: bool = False,
) -> dict[str, object]:
    """Materialize and probe the platform-specific launch-time runtime contract."""

    record = validate_orca_runtime_contract(
        value, orca_executable=orca_executable
    )
    directory = working_directory.expanduser().resolve()
    if not directory.is_dir():
        raise _failure(f"ORCA working directory is missing: {directory}")
    overlay = record.get("launch_overlay")
    if not isinstance(overlay, Mapping):
        raise _failure("stored ORCA runtime lacks a launch overlay")
    strategy = overlay.get("strategy")
    raw_files = overlay.get("files")
    if strategy not in {"working_directory_symlink", "environment_library_path"}:
        raise _failure("stored ORCA launch-overlay strategy is unsupported")
    if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
        raise _failure("stored ORCA launch-overlay files are malformed")

    effective_strategy = (
        "working_directory_copy"
        if copy_working_directory_overlay
        and strategy == "working_directory_symlink"
        else strategy
    )
    materialized: list[dict[str, object]] = []
    created: list[Path] = []
    try:
        for raw_file in raw_files:
            if not isinstance(raw_file, Mapping):
                raise _failure("stored ORCA launch-overlay file is malformed")
            name = str(raw_file.get("name", ""))
            if not name or name in {".", ".."} or Path(name).name != name:
                raise _failure("stored ORCA launch-overlay name is unsafe")
            source = Path(str(raw_file.get("source", ""))).resolve()
            if (
                not source.is_file()
                or source.stat().st_size != int(raw_file.get("size_bytes", -1))
                or file_hash(source) != raw_file.get("sha256")
            ):
                raise _failure(f"ORCA launch-overlay source changed: {source}")
            destination = directory / name
            if destination.is_symlink():
                if destination.resolve() != source:
                    raise _failure(
                        f"ORCA launch-overlay symlink conflicts: {destination}"
                    )
            elif destination.exists():
                raise _failure(
                    f"ORCA launch-overlay path already exists: {destination}"
                )
            else:
                if effective_strategy == "working_directory_copy":
                    shutil.copyfile(source, destination)
                    with destination.open("rb") as copied:
                        os.fsync(copied.fileno())
                    if (
                        destination.stat().st_size
                        != int(raw_file.get("size_bytes", -1))
                        or file_hash(destination) != raw_file.get("sha256")
                    ):
                        raise _failure(
                            f"ORCA launch-overlay copy verification failed: {destination}"
                        )
                else:
                    destination.symlink_to(source)
                created.append(destination)
            materialized.append(
                {
                    "name": name,
                    "path": str(destination),
                    "source": str(source),
                    "size_bytes": source.stat().st_size,
                    "sha256": file_hash(source),
                }
            )
        if record.get("platform") == "Darwin":
            loader_probe = _probe_macos_loader(
                record, working_directory=directory
            )
        else:
            loader_probe = {
                "status": "NOT_REQUIRED",
                "code": "PLATFORM_USES_LIBRARY_PATH",
            }
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise

    identity = {
        "schema_version": 1,
        "runtime_id": record["runtime_id"],
        "working_directory": str(directory),
        "strategy": effective_strategy,
        "files": materialized,
        "loader_probe": loader_probe,
    }
    return {
        **identity,
        "launch_id": stable_hash(identity),
        "validation": {
            "status": "PASSED",
            "code": "VALID_ORCA_RUNTIME_LAUNCH",
            "checks": {
                "runtime_revalidated": True,
                "overlay_materialized": True,
                "loader_probe_passed": loader_probe["status"]
                in {"PASSED", "NOT_REQUIRED"},
            },
        },
    }


def runtime_environment(value: Mapping[str, Any]) -> dict[str, object]:
    environment = value.get("environment")
    if not isinstance(environment, Mapping):
        raise _failure("stored ORCA runtime lacks an environment contract")
    path_prepend = environment.get("path_prepend")
    library_prepend = environment.get("library_path_prepend")
    variable = environment.get("library_path_variable")
    raw_variables = environment.get("variables", {})
    if (
        not isinstance(path_prepend, list)
        or not all(isinstance(path, str) for path in path_prepend)
        or not isinstance(library_prepend, list)
        or not all(isinstance(path, str) for path in library_prepend)
        or variable not in {"DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"}
        or not isinstance(raw_variables, Mapping)
    ):
        raise _failure("stored ORCA runtime environment is malformed")
    variables: dict[str, str] = {}
    for name, item in raw_variables.items():
        if (
            not isinstance(name, str)
            or not name
            or not (name[0].isalpha() or name[0] == "_")
            or not all(
                character.isalnum() or character == "_" for character in name
            )
            or not isinstance(item, str)
            or "\x00" in item
            or "\n" in item
            or "\r" in item
        ):
            raise _failure("stored ORCA runtime environment variables are malformed")
        variables[name] = item
    return {
        "path_prepend": path_prepend,
        "library_path_variable": variable,
        "library_path_prepend": library_prepend,
        "variables": variables,
    }


__all__ = [
    "ORCA_RUNTIME_SCHEMA_VERSION",
    "OrcaRuntimeError",
    "materialize_orca_runtime_contract",
    "prepare_orca_runtime",
    "runtime_environment",
    "validate_orca_runtime_contract",
]
