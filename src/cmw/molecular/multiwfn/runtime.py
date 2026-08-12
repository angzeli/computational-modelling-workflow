"""Portable Multiwfn discovery, immutable settings, and runtime provenance.

Runtime choices describe an execution attempt. They deliberately do not enter
the identity of a scientific FMO, ESP, or IGMH target.
"""

from __future__ import annotations

import hashlib
import math
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from cmw.core.process_health import HealthReport, monitor_once
from cmw.core.provenance import file_hash


DEFAULT_MULTIWFN_NTHREADS = 8
SUPPORTED_VERSION_SERIES = (3, 8)
SETTINGS_MECHANISM = "run_local_multiwfnpath"


class OpenMPCapability(str, Enum):
    VERIFIED = "VERIFIED"
    NOT_DETECTED = "NOT_DETECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MultiwfnRuntime:
    """Resolved, immutable launch configuration for one attempt."""

    executable: str
    executable_sha256: str
    version: str
    requested_nthreads: int
    settings_mechanism: str
    settings_source: str
    settings_source_sha256: str
    settings_path: str
    settings_sha256: str
    openmp_capability: str
    prepared_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ParallelismObservation:
    effective_cores: float | None
    warning: str | None


def parse_threads(value: str | int | None) -> int:
    if value is None:
        return DEFAULT_MULTIWFN_NTHREADS
    text = str(value)
    if re.fullmatch(r"[0-9]+", text) is None or int(text) < 1:
        raise ValueError(
            f"Multiwfn threads must be a positive integer; received {text!r}"
        )
    return int(text)


def resolve_threads(
    *,
    cli_value: str | int | None = None,
    environment: Mapping[str, str] | None = None,
    config_value: str | int | None = None,
) -> int:
    """Resolve CLI > environment > configuration > 8, rejecting conflicts."""

    env = os.environ if environment is None else environment
    supplied = [
        ("CLI", cli_value),
        ("MULTIWFN_NTHREADS", env.get("MULTIWFN_NTHREADS")),
        ("configuration", config_value),
    ]
    parsed = [(name, parse_threads(value)) for name, value in supplied if value is not None]
    if parsed and len({value for _, value in parsed}) > 1:
        detail = ", ".join(f"{name}={value}" for name, value in parsed)
        raise ValueError(f"contradictory Multiwfn thread settings: {detail}")
    return parsed[0][1] if parsed else DEFAULT_MULTIWFN_NTHREADS


def _resolve_executable_value(value: str) -> Path:
    if not value:
        raise ValueError("Multiwfn executable was not specified")
    candidate = value if os.sep in value else shutil.which(value)
    if candidate is None:
        raise FileNotFoundError(f"Multiwfn executable was not found: {value}")
    path = Path(candidate).expanduser().resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise PermissionError(f"Multiwfn executable is not executable: {path}")
    return path


def resolve_executable(
    *,
    explicit: str | None = None,
    config: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve CLI > configuration > MULTIWFN_EXE > PATH without private defaults."""

    env = os.environ if environment is None else environment
    for value in (explicit, config, env.get("MULTIWFN_EXE")):
        if value:
            return _resolve_executable_value(value)
    candidates = [name for name in ("Multiwfn", "multiwfn") if shutil.which(name)]
    resolved = {str(_resolve_executable_value(name)) for name in candidates}
    if not resolved:
        raise FileNotFoundError(
            "Multiwfn executable was not found; use --multiwfn-exe or MULTIWFN_EXE"
        )
    if len(resolved) != 1:
        raise ValueError(
            "multiple conflicting Multiwfn executables are on PATH; select one explicitly"
        )
    return Path(resolved.pop())


def parse_version(text: str) -> str | None:
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    match = re.search(r"\bVersion\s+([0-9]+(?:\.[0-9]+)+)", clean, re.I)
    return match.group(1) if match else None


def require_supported_version(version: str) -> None:
    try:
        fields = tuple(int(value) for value in version.split("."))
    except ValueError as exc:
        raise ValueError(f"invalid Multiwfn version: {version!r}") from exc
    if fields[:2] != SUPPORTED_VERSION_SERIES:
        raise ValueError(
            "unsupported Multiwfn menu contract: "
            f"{version}; supported series is 3.8.x"
        )


def detect_version(executable: Path) -> str:
    """Detect the banner without entering an operation-specific menu."""

    outputs: list[str] = []
    for command in ((str(executable),), (str(executable), "--version")):
        completed = subprocess.run(
            command,
            input="q\n",
            capture_output=True,
            text=True,
            check=False,
        )
        outputs.append(f"{completed.stdout}\n{completed.stderr}")
        version = parse_version(outputs[-1])
        if version is not None:
            break
    else:
        version = None
    if version is None:
        raise ValueError("unable to detect Multiwfn version")
    require_supported_version(version)
    return version


def resolve_settings_source(
    executable: Path,
    *,
    explicit: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environment is None else environment
    if explicit is not None:
        candidate = explicit.expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(f"Multiwfn settings file is missing: {candidate}")
        return candidate.resolve()
    if env.get("Multiwfnpath"):
        candidate = Path(env["Multiwfnpath"]).expanduser() / "settings.ini"
        if not candidate.is_file():
            raise FileNotFoundError(f"Multiwfnpath has no settings.ini: {candidate}")
        return candidate.resolve()
    prefix = executable.parent.parent
    candidates = (
        executable.parent / "settings.ini",
        prefix / "etc" / "multiwfn" / "settings.ini",
        prefix / "share" / "multiwfn" / "settings.ini",
    )
    matches = [path.resolve() for path in candidates if path.is_file()]
    if not matches:
        raise FileNotFoundError(
            "Multiwfn settings.ini was not found; use --settings-source or Multiwfnpath"
        )
    if len(set(matches)) != 1:
        raise ValueError("multiple Multiwfn settings sources were found; select one explicitly")
    return matches[0]


_THREAD_LINE = re.compile(
    r"^([ \t]*nthreads[ \t]*=[ \t]*)[^ \t/]+([ \t]*(?://.*)?)$", re.I
)


def render_settings(source: bytes, threads: int) -> bytes:
    parsed_threads = parse_threads(threads)
    rendered: list[str] = []
    replaced = 0
    for line in source.decode("utf-8").splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        match = _THREAD_LINE.fullmatch(body)
        if match:
            body = f"{match.group(1)}{parsed_threads}{match.group(2)}"
            replaced += 1
        rendered.append(body + ending)
    if replaced != 1:
        raise ValueError(
            "Multiwfn settings must contain exactly one active nthreads entry; "
            f"found {replaced}"
        )
    return "".join(rendered).encode("utf-8")


def prepare_settings(source: Path, attempt_directory: Path, threads: int) -> Path:
    """Derive a read-only settings file without modifying the installation."""

    source = source.resolve(strict=True)
    attempt = attempt_directory.resolve(strict=True)
    if (attempt / "settings.ini").exists():
        raise FileExistsError("working-directory settings.ini would override Multiwfnpath")
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    directory = attempt / ".multiwfn-runtime" / source_hash[:16] / f"threads-{threads}"
    settings = directory / "settings.ini"
    rendered = render_settings(source_bytes, threads)
    directory.mkdir(parents=True, exist_ok=True)
    if settings.exists() and settings.read_bytes() != rendered:
        raise FileExistsError(f"run-local settings conflict: {settings}")
    if not settings.exists():
        settings.write_bytes(rendered)
        settings.chmod(0o444)
    for resource in source.parent.iterdir():
        if resource.name == "settings.ini":
            continue
        link = directory / resource.name
        if not link.exists() and not link.is_symlink():
            link.symlink_to(resource.resolve(), target_is_directory=resource.is_dir())
    return settings


def detect_openmp(executable: Path) -> OpenMPCapability:
    system = platform.system()
    if system == "Darwin":
        command: Sequence[str] = ("/usr/bin/otool", "-L", str(executable))
    elif system == "Linux":
        command = ("ldd", str(executable))
    else:
        return OpenMPCapability.UNKNOWN
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return OpenMPCapability.UNKNOWN
    if completed.returncode != 0:
        return OpenMPCapability.UNKNOWN
    linkage = f"{completed.stdout}\n{completed.stderr}".lower()
    return (
        OpenMPCapability.VERIFIED
        if re.search(r"lib(?:gomp|omp|iomp)", linkage)
        else OpenMPCapability.NOT_DETECTED
    )


def prepare_runtime(
    *,
    attempt_directory: Path,
    executable: str | None = None,
    config_executable: str | None = None,
    settings_source: Path | None = None,
    cli_threads: str | int | None = None,
    config_threads: str | int | None = None,
    environment: Mapping[str, str] | None = None,
) -> MultiwfnRuntime:
    env = os.environ if environment is None else environment
    resolved_executable = resolve_executable(
        explicit=executable, config=config_executable, environment=env
    )
    threads = resolve_threads(
        cli_value=cli_threads, environment=env, config_value=config_threads
    )
    source = resolve_settings_source(
        resolved_executable, explicit=settings_source, environment=env
    )
    settings = prepare_settings(source, attempt_directory, threads)
    return MultiwfnRuntime(
        executable=str(resolved_executable),
        executable_sha256=file_hash(resolved_executable),
        version=detect_version(resolved_executable),
        requested_nthreads=threads,
        settings_mechanism=SETTINGS_MECHANISM,
        settings_source=str(source),
        settings_source_sha256=file_hash(source),
        settings_path=str(settings),
        settings_sha256=file_hash(settings),
        openmp_capability=detect_openmp(resolved_executable).value,
        prepared_at=datetime.now(timezone.utc).isoformat(),
    )


def parallelism_warning(
    requested_threads: int, report: HealthReport
) -> ParallelismObservation:
    interval = report.assessment.sample_interval_seconds
    cpu_delta = report.assessment.cpu_delta_seconds
    if interval is None or cpu_delta is None or interval <= 0:
        return ParallelismObservation(None, None)
    effective = cpu_delta / interval
    warning = None
    if requested_threads > 1 and math.isfinite(effective) and 0.5 <= effective <= 1.25:
        warning = (
            f"Multiwfn requested {requested_threads} threads but this interval "
            f"observed {effective:.2f} effective cores; diagnostic only"
        )
    return ParallelismObservation(effective, warning)


def sample_health(
    pid: int, state_path: Path, activity_paths: Sequence[Path]
) -> tuple[HealthReport, ParallelismObservation]:
    report = monitor_once(pid, state_path, activity_paths)
    return report, ParallelismObservation(None, None)
