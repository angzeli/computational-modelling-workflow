"""Deterministic, project-independent paths for executable workflow attempts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


EXECUTION_LAYOUT_SCHEMA_VERSION = 1
ATTEMPT_PATTERN = re.compile(r"attempt_(\d{3,})\Z")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class ExecutionLayoutError(ValueError):
    """Raised when execution paths or attempt identity are unresolved or conflicting."""

    code = "FAILED_EXECUTION_LAYOUT"


def _identifier(value: str, *, name: str) -> str:
    selected = str(value)
    if not IDENTIFIER_PATTERN.fullmatch(selected):
        raise ExecutionLayoutError(
            f"{ExecutionLayoutError.code}: {name} must be one path-safe identifier"
        )
    return selected


def _absolute_root(value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or "{" in str(path) or "}" in str(path):
        raise ExecutionLayoutError(
            f"{ExecutionLayoutError.code}: project root must be an absolute resolved path"
        )
    return path.resolve()


def execution_target_directory(
    project_root: Path | str,
    *,
    system_identifier: str,
    workflow_node_identifier: str,
    target_identifier: str,
) -> Path:
    """Resolve the stable directory shared by all attempts of one target."""

    root = _absolute_root(project_root)
    system = _identifier(system_identifier, name="system identifier")
    node = _identifier(workflow_node_identifier, name="workflow node identifier")
    target = _identifier(target_identifier, name="target identifier")
    return root / "calculation" / system / node / target


def next_attempt_identifier(target_directory: Path | str) -> str:
    """Return the next deterministic attempt_NNN identifier for one target."""

    target = _absolute_root(target_directory)
    attempts = target / "attempts"
    indices = []
    if attempts.is_dir():
        for item in attempts.iterdir():
            match = ATTEMPT_PATTERN.fullmatch(item.name)
            if match and item.is_dir():
                indices.append(int(match.group(1)))
    return f"attempt_{max(indices, default=0) + 1:03d}"


@dataclass(frozen=True)
class ExecutionLayout:
    """Resolved attempt layout derived only from stable execution identities."""

    project_root: Path
    system_identifier: str
    workflow_node_identifier: str
    target_identifier: str
    attempt_identifier: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", _absolute_root(self.project_root))
        for field_name, label in (
            ("system_identifier", "system identifier"),
            ("workflow_node_identifier", "workflow node identifier"),
            ("target_identifier", "target identifier"),
            ("attempt_identifier", "attempt identifier"),
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), name=label),
            )
        if ATTEMPT_PATTERN.fullmatch(self.attempt_identifier) is None:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: attempt identifier must use attempt_NNN"
            )

    @property
    def target_directory(self) -> Path:
        return execution_target_directory(
            self.project_root,
            system_identifier=self.system_identifier,
            workflow_node_identifier=self.workflow_node_identifier,
            target_identifier=self.target_identifier,
        )

    @property
    def attempts_directory(self) -> Path:
        return self.target_directory / "attempts"

    @property
    def working_directory(self) -> Path:
        return self.attempts_directory / self.attempt_identifier

    @property
    def output_directory(self) -> Path:
        return self.working_directory

    @property
    def provenance_directory(self) -> Path:
        return self.working_directory

    @property
    def logs_directory(self) -> Path:
        return self.working_directory

    @property
    def layout_path(self) -> Path:
        return self.provenance_directory / "execution-layout.json"

    def metadata_path(self, filename: str = "attempt.json") -> Path:
        return self.provenance_directory / _identifier(filename, name="metadata filename")

    def input_path(self, filename: str) -> Path:
        return self.working_directory / _identifier(filename, name="input filename")

    def output_path(self, filename: str) -> Path:
        return self.output_directory / _identifier(filename, name="output filename")

    def log_path(self, filename: str) -> Path:
        return self.logs_directory / _identifier(filename, name="log filename")

    def create_working_directory(self) -> None:
        self.working_directory.mkdir(parents=True, exist_ok=False)

    def validate(self, *, require_existing: bool = False) -> None:
        for path in (
            self.target_directory,
            self.attempts_directory,
            self.working_directory,
            self.output_directory,
            self.provenance_directory,
            self.logs_directory,
        ):
            if not path.is_absolute():
                raise ExecutionLayoutError(
                    f"{ExecutionLayoutError.code}: execution path is unresolved: {path}"
                )
        if require_existing and not self.working_directory.is_dir():
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: working directory is missing: "
                f"{self.working_directory}"
            )

    def validate_attempt_identity(self, attempt_identifier: str) -> None:
        if attempt_identifier != self.attempt_identifier:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: conflicting attempt identity "
                f"{attempt_identifier!r}; expected {self.attempt_identifier!r}"
            )

    def validate_paths(
        self,
        *,
        input_paths: Sequence[Path] = (),
        output_paths: Sequence[Path] = (),
        metadata_paths: Sequence[Path] = (),
        log_paths: Sequence[Path] = (),
    ) -> None:
        """Require resolved attempt-owned paths in their declared directories."""

        self.validate(require_existing=True)
        categories = (
            (input_paths, self.working_directory, "input"),
            (output_paths, self.output_directory, "output"),
            (metadata_paths, self.provenance_directory, "metadata"),
            (log_paths, self.logs_directory, "log"),
        )
        for paths, parent, category in categories:
            for path in paths:
                selected = Path(path)
                if not selected.is_absolute():
                    raise ExecutionLayoutError(
                        f"{ExecutionLayoutError.code}: {category} path is unresolved: "
                        f"{selected}"
                    )
                resolved = selected.resolve()
                try:
                    resolved.relative_to(parent)
                except ValueError as exc:
                    raise ExecutionLayoutError(
                        f"{ExecutionLayoutError.code}: {category} path is outside "
                        f"the execution layout: {resolved}"
                    ) from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EXECUTION_LAYOUT_SCHEMA_VERSION,
            "project_root": str(self.project_root),
            "system_identifier": self.system_identifier,
            "workflow_node_identifier": self.workflow_node_identifier,
            "target_identifier": self.target_identifier,
            "attempt_identifier": self.attempt_identifier,
            "target_directory": str(self.target_directory),
            "working_directory": str(self.working_directory),
            "output_directory": str(self.output_directory),
            "provenance_directory": str(self.provenance_directory),
            "logs_directory": str(self.logs_directory),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutionLayout":
        if value.get("schema_version") != EXECUTION_LAYOUT_SCHEMA_VERSION:
            raise ExecutionLayoutError(
                f"{ExecutionLayoutError.code}: unsupported execution layout schema"
            )
        layout = cls(
            project_root=Path(str(value["project_root"])),
            system_identifier=str(value["system_identifier"]),
            workflow_node_identifier=str(value["workflow_node_identifier"]),
            target_identifier=str(value["target_identifier"]),
            attempt_identifier=str(value["attempt_identifier"]),
        )
        expected = layout.to_dict()
        for key in (
            "target_directory",
            "working_directory",
            "output_directory",
            "provenance_directory",
            "logs_directory",
        ):
            if str(value.get(key, "")) != expected[key]:
                raise ExecutionLayoutError(
                    f"{ExecutionLayoutError.code}: stored {key} conflicts with identities"
                )
        return layout


__all__ = [
    "EXECUTION_LAYOUT_SCHEMA_VERSION",
    "ExecutionLayout",
    "ExecutionLayoutError",
    "execution_target_directory",
    "next_attempt_identifier",
]
