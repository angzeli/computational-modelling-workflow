"""Versioned Multiwfn excited-state render, parse, and finalization contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from cmw.core.artifacts import (
    ArtifactValidation,
    ExcitedStateArtifact,
    HoleElectronArtifact,
    NTOArtifact,
    ValidationStatus,
    validate_artifact_compatibility,
)
from cmw.core.provenance import file_hash
from cmw.molecular.excited_states import (
    ExcitedStateIdentity,
    ExcitedStateRecord,
    ExcitedStateSelectionContractError,
    validate_excited_state_selection_contract,
)

from .adapter import (
    MultiwfnAuxiliaryInputSpec,
    MultiwfnCommandSpec,
    MultiwfnOutputSpec,
    build_command_spec,
    materialize_auxiliary_inputs,
    validate_auxiliary_input_specs,
    validate_output_specs,
)
from .runtime import MENU_CONTRACT, parse_version


MULTIWFN_EXCITED_STATE_RENDERER_VERSION = "1.0.0"
MULTIWFN_EXCITED_STATE_PARSER_VERSION = "1.0.0"
MULTIWFN38_NTO_GRAMMAR = "multiwfn_3_8_nto_v1"
MULTIWFN38_HEA_GRAMMAR = "multiwfn_3_8_nonfragment_hea_v1"
MULTIWFN2026_7_15_VERSION = "2026.7.15"
MULTIWFN2026_NTO_GRAMMAR = "multiwfn_2026_7_15_nto_v1"
MULTIWFN2026_HEA_GRAMMAR = "multiwfn_2026_7_15_nonfragment_hea_v1"
MULTIWFN2026_IFCT_GRAMMAR = "multiwfn_2026_7_15_ifct_hirshfeld_v1"
D_ROUNDING_POLICY = "decimal_rounding_interval_overlap_v1"
ORCA_OUTPUT_LOCAL_PATH = "cmw-orca-excited-state.out"

FLOAT = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?"
ANSI_ESCAPE = r"(?:\x1b\[[0-9;]*m)*"
VERSION_RE = re.compile(
    rf"^{ANSI_ESCAPE}\s*Version\s+([^,\s]+)", re.I | re.M
)
THREAD_RE = re.compile(r"Number of parallel threads:\s*(\d+)", re.I)
LOADED_WAVEFUNCTION_RE = re.compile(r"^\s*Loaded\s+(.+?)\s+successfully!\s*$", re.M)
SELECTED_STATE_RE = re.compile(
    r"Loading configuration coefficients of excited state\s+(\d+)\.\.\.",
    re.I,
)
SUMMARY_STATE_RE = re.compile(
    rf"^\s*State:\s*(\d+)\s+Exc\. Energy:\s*({FLOAT})\s+eV\s+"
    r"Multi\.:\s*(\d+)\s+MO pairs:\s*(\d+)\s*$",
    re.I | re.M,
)


class MultiwfnExcitedStateError(ValueError):
    """Base error carrying a stable machine-readable failure code."""

    code = "FAILED_MULTIWFN_EXCITED_STATE"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class UnsupportedMultiwfnFormatError(MultiwfnExcitedStateError):
    code = "UNSUPPORTED_MULTIWFN_FORMAT"


class MultiwfnSessionParseError(MultiwfnExcitedStateError):
    code = "FAILED_MULTIWFN_PARSE"


class MultiwfnStateIdentityError(MultiwfnExcitedStateError):
    code = "FAILED_MULTIWFN_STATE_IDENTITY"


class MultiwfnFinalizationError(MultiwfnExcitedStateError):
    code = "FAILED_MULTIWFN_FINALIZATION"


class DeferredFragmentAnalysisError(MultiwfnExcitedStateError):
    code = "DEFERRED_FRAGMENT_RESOLVED_HEA"


def _number(value: str) -> float:
    return float(value.replace("D", "E").replace("d", "e"))


def _decimal_places(value: str) -> int:
    mantissa = re.split(r"[EeDd]", value, maxsplit=1)[0]
    return len(mantissa.rsplit(".", 1)[1]) if "." in mantissa else 0


def _single_match(pattern: re.Pattern[str], text: str, label: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise MultiwfnSessionParseError(
            f"expected exactly one {label}; found {len(matches)}"
        )
    return matches[0]


def _required_match(pattern: re.Pattern[str], text: str, label: str) -> re.Match[str]:
    match = pattern.search(text)
    if match is None:
        raise MultiwfnSessionParseError(f"missing {label}")
    return match


def _version_fields(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(item) for item in version.split("."))
    except ValueError as exc:
        raise UnsupportedMultiwfnFormatError(
            f"invalid Multiwfn version {version!r}"
        ) from exc


def _grammar_for_version(version: str, analysis: str) -> str:
    fields = _version_fields(version)
    if analysis == "ifct":
        if version == MULTIWFN2026_7_15_VERSION:
            return MULTIWFN2026_IFCT_GRAMMAR
        raise UnsupportedMultiwfnFormatError(
            "fragment-resolved IFCT is fixture-tested only for exact "
            f"Multiwfn {MULTIWFN2026_7_15_VERSION}; found {version}"
        )
    if analysis not in {"nto", "hole_electron"}:
        raise UnsupportedMultiwfnFormatError(
            f"unsupported Multiwfn excited-state analysis {analysis!r}"
        )
    if fields[:2] == (3, 8):
        return (
            MULTIWFN38_NTO_GRAMMAR
            if analysis == "nto"
            else MULTIWFN38_HEA_GRAMMAR
        )
    if version == MULTIWFN2026_7_15_VERSION:
        return (
            MULTIWFN2026_NTO_GRAMMAR
            if analysis == "nto"
            else MULTIWFN2026_HEA_GRAMMAR
        )
    raise UnsupportedMultiwfnFormatError(
        "only fixture-tested Multiwfn 3.8.x and exact 2026.7.15 "
        f"excited-state grammars are supported; found {version}"
    )


def _require_grammar(version: str, analysis: str, grammar_id: str) -> None:
    resolved = _grammar_for_version(version, analysis)
    if resolved != grammar_id:
        raise UnsupportedMultiwfnFormatError(
            f"Multiwfn {version} requires {resolved}, not {grammar_id}"
        )


def _settings_identity(
    value: Mapping[str, object] | None,
    *,
    required: bool,
) -> dict[str, object]:
    if value is None:
        if required:
            raise MultiwfnExcitedStateError(
                "exact-version renderer requires immutable settings identity"
            )
        return {}
    settings = dict(value)
    path_value = settings.get("settings_path")
    digest = settings.get("settings_sha256")
    source_digest = settings.get("settings_source_sha256")
    threads = settings.get("requested_nthreads")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise MultiwfnExcitedStateError("settings_path must be absolute")
    try:
        path = Path(path_value).resolve(strict=True)
    except OSError as exc:
        raise MultiwfnExcitedStateError("settings_path is missing") from exc
    if not isinstance(digest, str) or digest != file_hash(path):
        raise MultiwfnExcitedStateError("settings identity hash mismatch")
    if not isinstance(source_digest, str) or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None:
        raise MultiwfnExcitedStateError("settings source hash is required")
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise MultiwfnExcitedStateError("settings thread count must be positive")
    settings["settings_path"] = str(path)
    return settings


def _safe_prefix(value: str) -> str:
    prefix = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", prefix):
        raise MultiwfnExcitedStateError(
            "output prefix must contain only letters, digits, dot, underscore, or dash"
        )
    return prefix


def _file_identity(path: Path) -> dict[str, object]:
    selected = path.expanduser().resolve(strict=True)
    if not selected.is_file():
        raise MultiwfnExcitedStateError(f"source file is not regular: {selected}")
    return {
        "path": str(selected),
        "sha256": file_hash(selected),
        "size_bytes": selected.stat().st_size,
    }


def _state_metadata(state: ExcitedStateRecord) -> dict[str, object]:
    return {
        "spin_manifold": state.spin_manifold,
        "local_state_index": state.local_state_index,
        "label": state.identity.label,
        "orca_local_state_index": state.local_state_index,
        "orca_global_state_index": state.orca_global_state_index,
        "multiwfn_local_state_index": state.local_state_index,
        "multiplicity": state.multiplicity,
        "excitation_energy_ev": state.excitation_energy_ev,
    }


@dataclass(frozen=True)
class RenderedMultiwfnExcitedStateInput:
    """Deterministic menu plus source/output/provenance contract."""

    analysis: str
    grammar_id: str
    selected_state: ExcitedStateRecord
    source_wavefunction_identity: Mapping[str, object]
    orca_output_identity: Mapping[str, object]
    menu_sequence: tuple[str, ...]
    outputs: tuple[MultiwfnOutputSpec, ...]
    scientific_protocol_hash: str
    source_geometry_hash: str
    execution_layout: Mapping[str, object]
    execution_attempt: Mapping[str, object]
    settings_metadata: Mapping[str, object]
    auxiliary_inputs: tuple[MultiwfnAuxiliaryInputSpec, ...] = ()
    renderer_version: str = MULTIWFN_EXCITED_STATE_RENDERER_VERSION

    def __post_init__(self) -> None:
        if self.analysis not in {"nto", "hole_electron", "ifct"}:
            raise MultiwfnExcitedStateError("unsupported excited-state analysis")
        if not self.menu_sequence or self.menu_sequence[-1] != "q":
            raise MultiwfnExcitedStateError(
                "Multiwfn menu sequence must exit gracefully with q"
            )
        if any("\n" in item or "\r" in item for item in self.menu_sequence):
            raise MultiwfnExcitedStateError("menu entries must be single lines")
        if not self.scientific_protocol_hash:
            raise MultiwfnExcitedStateError("scientific protocol hash is required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_geometry_hash):
            raise MultiwfnExcitedStateError(
                "source geometry hash must be a lowercase SHA-256 identity"
            )
        if not self.execution_attempt.get("attempt_id"):
            raise MultiwfnExcitedStateError("execution attempt identifier is required")
        working_directory = self.execution_layout.get("working_directory")
        if (
            not isinstance(working_directory, str)
            or not Path(working_directory).is_absolute()
        ):
            raise MultiwfnExcitedStateError(
                "execution layout requires an absolute working_directory"
            )
        layout_attempt = self.execution_layout.get("attempt_identifier")
        if layout_attempt is not None and layout_attempt != self.execution_attempt.get(
            "attempt_id"
        ):
            raise MultiwfnExcitedStateError(
                "execution layout and attempt identifiers do not match"
            )
        object.__setattr__(self, "menu_sequence", tuple(self.menu_sequence))
        object.__setattr__(self, "outputs", validate_output_specs(self.outputs))
        object.__setattr__(
            self,
            "auxiliary_inputs",
            validate_auxiliary_input_specs(self.auxiliary_inputs),
        )
        object.__setattr__(
            self,
            "source_wavefunction_identity",
            dict(self.source_wavefunction_identity),
        )
        object.__setattr__(self, "orca_output_identity", dict(self.orca_output_identity))
        object.__setattr__(self, "execution_layout", dict(self.execution_layout))
        object.__setattr__(self, "execution_attempt", dict(self.execution_attempt))
        object.__setattr__(self, "settings_metadata", dict(self.settings_metadata))

    @property
    def stdin_text(self) -> str:
        return "\n".join(self.menu_sequence) + "\n"

    @property
    def stdin_sha256(self) -> str:
        return hashlib.sha256(self.stdin_text.encode("utf-8")).hexdigest()

    @property
    def canonical_identity(self) -> ExcitedStateIdentity:
        return self.selected_state.identity

    def to_dict(self) -> dict[str, object]:
        return {
            "analysis": self.analysis,
            "grammar_id": self.grammar_id,
            "renderer_version": self.renderer_version,
            "selected_state": _state_metadata(self.selected_state),
            "source_wavefunction_identity": dict(self.source_wavefunction_identity),
            "orca_output_identity": dict(self.orca_output_identity),
            "menu_sequence": list(self.menu_sequence),
            "stdin_sha256": self.stdin_sha256,
            "outputs": [item.to_dict() for item in self.outputs],
            "scientific_protocol_hash": self.scientific_protocol_hash,
            "source_geometry_hash": self.source_geometry_hash,
            "execution_layout": dict(self.execution_layout),
            "execution_attempt": dict(self.execution_attempt),
            "settings_metadata": dict(self.settings_metadata),
            "auxiliary_inputs": [item.to_dict() for item in self.auxiliary_inputs],
        }


class Multiwfn38NtoRenderer:
    """Render the fixture-tested Multiwfn 3.8 NTO menu grammar."""

    grammar_id = MULTIWFN38_NTO_GRAMMAR
    exact_version: str | None = None
    settings_required = False

    def render(
        self,
        selected_state: ExcitedStateRecord,
        *,
        orca_output_path: Path,
        source_wavefunction_path: Path,
        scientific_protocol_hash: str,
        source_geometry_hash: str,
        execution_layout: Mapping[str, object],
        execution_attempt: Mapping[str, object],
        output_prefix: str | None = None,
        settings_identity: Mapping[str, object] | None = None,
    ) -> RenderedMultiwfnExcitedStateInput:
        prefix = _safe_prefix(output_prefix or selected_state.identity.label)
        orca = _file_identity(orca_output_path)
        wavefunction = _file_identity(source_wavefunction_path)
        output_name = f"{prefix}_nto.mwfn"
        if selected_state.spin_manifold == "singlet":
            state_menu = (str(selected_state.local_state_index),)
        else:
            state_menu = ("3", str(selected_state.local_state_index))
        menu = (
            "18",
            "6",
            ORCA_OUTPUT_LOCAL_PATH,
            *state_menu,
            "3",
            output_name,
            "0",
            "0",
            "q",
        )
        outputs = (
            MultiwfnOutputSpec(
                "session_log",
                "multiwfn.session.log",
                f"logs/{prefix}_nto.session.log",
                media_type="text/plain",
            ),
            MultiwfnOutputSpec(
                "nto_mwfn",
                output_name,
                f"analysis/{output_name}",
                media_type="chemical/x-mwfn",
            ),
        )
        return RenderedMultiwfnExcitedStateInput(
            "nto",
            self.grammar_id,
            selected_state,
            wavefunction,
            orca,
            menu,
            outputs,
            scientific_protocol_hash,
            source_geometry_hash,
            execution_layout,
            execution_attempt,
            {
                "required_menu_contract": MENU_CONTRACT,
                **(
                    {"exact_multiwfn_version": self.exact_version}
                    if self.exact_version is not None
                    else {"supported_multiwfn_series": "3.8"}
                ),
                **_settings_identity(
                    settings_identity,
                    required=self.settings_required,
                ),
            },
            auxiliary_inputs=(
                MultiwfnAuxiliaryInputSpec(
                    "orca_excited_state_output", orca, ORCA_OUTPUT_LOCAL_PATH
                ),
            ),
        )


class Multiwfn38HoleElectronRenderer:
    """Render the fixture-tested non-fragment Multiwfn 3.8 HEA grammar."""

    grammar_id = MULTIWFN38_HEA_GRAMMAR
    exact_version: str | None = None
    settings_required = False
    exit_menu = ("0", "0", "q")

    def render(
        self,
        selected_state: ExcitedStateRecord,
        *,
        orca_output_path: Path,
        source_wavefunction_path: Path,
        scientific_protocol_hash: str,
        source_geometry_hash: str,
        execution_layout: Mapping[str, object],
        execution_attempt: Mapping[str, object],
        output_prefix: str | None = None,
        fragment_definitions: Sequence[object] | None = None,
        settings_identity: Mapping[str, object] | None = None,
    ) -> RenderedMultiwfnExcitedStateInput:
        if fragment_definitions:
            raise DeferredFragmentAnalysisError(
                "fragment-resolved HEA requires a real two-fragment Multiwfn fixture"
            )
        prefix = _safe_prefix(output_prefix or selected_state.identity.label)
        orca = _file_identity(orca_output_path)
        wavefunction = _file_identity(source_wavefunction_path)
        if selected_state.spin_manifold == "singlet":
            state_menu = (str(selected_state.local_state_index),)
        else:
            state_menu = ("3", str(selected_state.local_state_index))
        menu = (
            "18",
            "1",
            ORCA_OUTPUT_LOCAL_PATH,
            *state_menu,
            "1",
            "2",
            "10",
            "1",
            "11",
            "1",
            *self.exit_menu,
        )
        outputs = (
            MultiwfnOutputSpec(
                "session_log",
                "multiwfn.session.log",
                f"logs/{prefix}_hea.session.log",
                media_type="text/plain",
            ),
            MultiwfnOutputSpec(
                "hole_density",
                "hole.cub",
                f"analysis/{prefix}_hole.cub",
            ),
            MultiwfnOutputSpec(
                "electron_density",
                "electron.cub",
                f"analysis/{prefix}_electron.cub",
            ),
        )
        return RenderedMultiwfnExcitedStateInput(
            "hole_electron",
            self.grammar_id,
            selected_state,
            wavefunction,
            orca,
            menu,
            outputs,
            scientific_protocol_hash,
            source_geometry_hash,
            execution_layout,
            execution_attempt,
            {
                "required_menu_contract": MENU_CONTRACT,
                **(
                    {"exact_multiwfn_version": self.exact_version}
                    if self.exact_version is not None
                    else {"supported_multiwfn_series": "3.8"}
                ),
                "grid_quality": "medium",
                "fragment_resolved": False,
                **_settings_identity(
                    settings_identity,
                    required=self.settings_required,
                ),
            },
            auxiliary_inputs=(
                MultiwfnAuxiliaryInputSpec(
                    "orca_excited_state_output", orca, ORCA_OUTPUT_LOCAL_PATH
                ),
            ),
        )


class Multiwfn2026NtoRenderer(Multiwfn38NtoRenderer):
    """Render the empirically validated exact Multiwfn 2026.7.15 NTO grammar."""

    grammar_id = MULTIWFN2026_NTO_GRAMMAR
    exact_version = MULTIWFN2026_7_15_VERSION
    settings_required = True


class Multiwfn2026HoleElectronRenderer(Multiwfn38HoleElectronRenderer):
    """Render the exact Multiwfn 2026.7.15 non-fragment HEA grammar."""

    grammar_id = MULTIWFN2026_HEA_GRAMMAR
    exact_version = MULTIWFN2026_7_15_VERSION
    settings_required = True
    exit_menu = ("0", "0", "0", "q")


def _multiwfn_atom_selection(atom_indices: Sequence[int]) -> str:
    values = tuple(atom_indices)
    if (
        not values
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in values
        )
        or min(values) < 0
        or len(set(values)) != len(values)
    ):
        raise MultiwfnExcitedStateError(
            "fragment definitions require unique zero-based atom indices"
        )
    one_based = tuple(value + 1 for value in sorted(values))
    ranges: list[str] = []
    start = previous = one_based[0]
    for value in one_based[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _ifct_fragment_contract(
    fragment_definitions: Sequence[object], atom_count: int
) -> tuple[dict[str, object], ...]:
    if (
        isinstance(atom_count, bool)
        or not isinstance(atom_count, int)
        or atom_count < 1
    ):
        raise MultiwfnExcitedStateError("IFCT source atom count must be positive")
    fragments = tuple(fragment_definitions)
    if len(fragments) != 2:
        raise MultiwfnExcitedStateError(
            "the validated IFCT contract requires exactly two fragments"
        )
    result: list[dict[str, object]] = []
    labels: list[str] = []
    covered: set[int] = set()
    for fragment in fragments:
        label = str(getattr(fragment, "fragment_id", "")).strip()
        indices = tuple(getattr(fragment, "atom_indices", ()))
        if not label:
            raise MultiwfnExcitedStateError("IFCT fragment labels are required")
        if label in labels:
            raise MultiwfnExcitedStateError("IFCT fragment labels must be unique")
        if covered.intersection(indices):
            raise MultiwfnExcitedStateError("IFCT fragment atom mappings overlap")
        selection = _multiwfn_atom_selection(indices)
        covered.update(indices)
        labels.append(label)
        result.append(
            {
                "fragment_label": label,
                "atom_indices_zero_based": list(indices),
                "multiwfn_atom_selection_one_based": selection,
            }
        )
    expected = set(range(atom_count))
    if covered != expected:
        missing = sorted(expected - covered)
        invalid = sorted(covered - expected)
        detail = f"missing={missing}" if missing else f"invalid={invalid}"
        raise MultiwfnExcitedStateError(
            "IFCT fragments must cover the source structure exactly: " + detail
        )
    return tuple(result)


class Multiwfn2026IfctRenderer:
    """Render exact-version, two-fragment Hirshfeld IFCT input."""

    grammar_id = MULTIWFN2026_IFCT_GRAMMAR
    exact_version = MULTIWFN2026_7_15_VERSION

    def render(
        self,
        selected_state: ExcitedStateRecord,
        *,
        orca_output_path: Path,
        source_wavefunction_path: Path,
        fragment_definitions: Sequence[object],
        atom_count: int,
        scientific_protocol_hash: str,
        source_geometry_hash: str,
        execution_layout: Mapping[str, object],
        execution_attempt: Mapping[str, object],
        output_prefix: str | None = None,
        settings_identity: Mapping[str, object] | None = None,
    ) -> RenderedMultiwfnExcitedStateInput:
        fragments = _ifct_fragment_contract(fragment_definitions, atom_count)
        prefix = _safe_prefix(output_prefix or selected_state.identity.label)
        orca = _file_identity(orca_output_path)
        wavefunction = _file_identity(source_wavefunction_path)
        menu = (
            "18",
            "8",
            "2",
            ORCA_OUTPUT_LOCAL_PATH,
            str(selected_state.local_state_index),
            str(len(fragments)),
            *(str(item["multiwfn_atom_selection_one_based"]) for item in fragments),
            "0",
            "0",
            "q",
        )
        outputs = (
            MultiwfnOutputSpec(
                "session_log",
                "multiwfn.session.log",
                f"logs/{prefix}_ifct.session.log",
                media_type="text/plain",
            ),
        )
        return RenderedMultiwfnExcitedStateInput(
            "ifct",
            self.grammar_id,
            selected_state,
            wavefunction,
            orca,
            menu,
            outputs,
            scientific_protocol_hash,
            source_geometry_hash,
            execution_layout,
            execution_attempt,
            {
                "required_menu_contract": MENU_CONTRACT,
                "exact_multiwfn_version": self.exact_version,
                "population_scheme": "hirshfeld",
                "fragment_resolved": True,
                "atom_count": atom_count,
                "fragment_definitions": list(fragments),
                **_settings_identity(settings_identity, required=True),
            },
            auxiliary_inputs=(
                MultiwfnAuxiliaryInputSpec(
                    "orca_excited_state_output", orca, ORCA_OUTPUT_LOCAL_PATH
                ),
            ),
        )


def build_excited_state_command_spec(
    rendered: RenderedMultiwfnExcitedStateInput,
    *,
    runtime: Mapping[str, object],
    attempt_directory: Path,
    stdin_path: Path,
) -> MultiwfnCommandSpec:
    """Materialize one rendered plan through the existing command adapter."""

    version = runtime.get("version")
    if not isinstance(version, str):
        raise MultiwfnExcitedStateError("Multiwfn runtime version is required")
    analysis = rendered.analysis
    _require_grammar(version, analysis, rendered.grammar_id)
    if runtime.get("menu_contract") != MENU_CONTRACT:
        raise MultiwfnExcitedStateError(
            "Multiwfn runtime menu contract does not match the renderer"
        )
    if version == MULTIWFN2026_7_15_VERSION:
        for key in (
            "settings_path",
            "settings_sha256",
            "settings_source_sha256",
            "requested_nthreads",
        ):
            if rendered.settings_metadata.get(key) != runtime.get(key):
                raise MultiwfnExcitedStateError(
                    f"runtime {key} does not match the rendered settings identity"
                )
    planned_directory = Path(
        str(rendered.execution_layout["working_directory"])
    ).resolve()
    if attempt_directory.resolve() != planned_directory:
        raise MultiwfnExcitedStateError(
            "command attempt directory differs from the rendered execution layout"
        )
    try:
        stored = stdin_path.expanduser().resolve(strict=True).read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        raise MultiwfnExcitedStateError(
            "rendered Multiwfn stdin is missing or unreadable"
        ) from exc
    if stored != rendered.stdin_text:
        raise MultiwfnExcitedStateError(
            "materialized Multiwfn stdin differs from the rendered menu"
        )
    materialize_auxiliary_inputs(attempt_directory, rendered.auxiliary_inputs)
    for identity in (
        rendered.source_wavefunction_identity,
        rendered.orca_output_identity,
    ):
        path = Path(str(identity["path"]))
        if file_hash(path) != identity["sha256"] or path.stat().st_size != identity[
            "size_bytes"
        ]:
            raise MultiwfnExcitedStateError(
                f"rendered source identity changed before execution: {path}"
            )
    return build_command_spec(
        runtime=runtime,
        source_path=Path(str(rendered.source_wavefunction_identity["path"])),
        attempt_directory=attempt_directory,
        stdin_path=stdin_path,
        outputs=rendered.outputs,
    )


@dataclass(frozen=True)
class MultiwfnSessionStateEvidence:
    multiwfn_version: str
    selected_identity: ExcitedStateIdentity
    selected_local_state_index: int
    selected_multiplicity: int
    selected_energy_ev: float
    selected_energy_decimal_places: int
    selected_mo_pair_count: int
    loaded_source_wavefunction_path: str
    parallel_threads: int | None
    energy_consistent_with_expected: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "multiwfn_version": self.multiwfn_version,
            "selected_state_identity": self.selected_identity.to_dict(),
            "selected_local_state_index": self.selected_local_state_index,
            "selected_multiplicity": self.selected_multiplicity,
            "selected_energy_ev": self.selected_energy_ev,
            "selected_energy_decimal_places": self.selected_energy_decimal_places,
            "selected_mo_pair_count": self.selected_mo_pair_count,
            "loaded_source_wavefunction_path": self.loaded_source_wavefunction_path,
            "parallel_threads": self.parallel_threads,
            "energy_consistent_with_expected": self.energy_consistent_with_expected,
        }


def _rounded_value_contains(
    printed: float,
    decimals: int,
    expected: float,
) -> bool:
    half_quantum = 0.5 * 10.0 ** (-decimals)
    return printed - half_quantum - 1.0e-12 <= expected <= printed + half_quantum + 1.0e-12


def _parse_state_evidence(
    text: str,
    expected_state: ExcitedStateRecord,
    *,
    analysis: str,
    grammar_id: str,
) -> MultiwfnSessionStateEvidence:
    version_match = _single_match(VERSION_RE, text, "Multiwfn version banner")
    version = version_match.group(1)
    parsed_by_runtime = parse_version(version_match.group(0))
    if parsed_by_runtime != version:
        raise MultiwfnSessionParseError("Multiwfn version banner is ambiguous")
    _require_grammar(version, analysis, grammar_id)
    selected = int(
        _single_match(
            SELECTED_STATE_RE,
            text,
            "selected excited-state evidence",
        ).group(1)
    )
    rows: dict[int, tuple[str, int, int]] = {}
    for match in SUMMARY_STATE_RE.finditer(text):
        index = int(match.group(1))
        if index in rows:
            raise MultiwfnSessionParseError(
                f"duplicate state-summary row for local state {index}"
            )
        rows[index] = (match.group(2), int(match.group(3)), int(match.group(4)))
    if selected not in rows:
        raise MultiwfnSessionParseError(
            "selected state is absent from the Multiwfn state summary"
        )
    energy_text, multiplicity, mo_pairs = rows[selected]
    spin = {1: "singlet", 3: "triplet"}.get(multiplicity)
    if spin is None:
        raise UnsupportedMultiwfnFormatError(
            f"unsupported selected-state multiplicity {multiplicity}"
        )
    identity = ExcitedStateIdentity(spin, selected)
    if identity != expected_state.identity:
        raise MultiwfnStateIdentityError(
            "transcript selected state "
            f"{identity.label} does not match expected {expected_state.identity.label}"
        )
    expected_multiplicity = 1 if expected_state.spin_manifold == "singlet" else 3
    if multiplicity != expected_multiplicity:
        raise MultiwfnStateIdentityError(
            "transcript multiplicity does not match the expected canonical state"
        )
    decimals = _decimal_places(energy_text)
    energy = _number(energy_text)
    energy_consistent = _rounded_value_contains(
        energy,
        decimals,
        expected_state.excitation_energy_ev,
    )
    if not energy_consistent:
        raise MultiwfnStateIdentityError(
            "transcript energy is inconsistent with the expected ORCA state"
        )
    wavefunction = _single_match(
        LOADED_WAVEFUNCTION_RE,
        text,
        "loaded source wavefunction",
    ).group(1)
    thread_match = THREAD_RE.search(text)
    return MultiwfnSessionStateEvidence(
        version,
        identity,
        selected,
        multiplicity,
        energy,
        decimals,
        mo_pairs,
        wavefunction,
        int(thread_match.group(1)) if thread_match is not None else None,
        energy_consistent,
    )


@dataclass(frozen=True)
class NtoPairRecord:
    pair_index: int
    weight: float
    cumulative_weight: float
    hole_orbital_identity: str | None = None
    electron_orbital_identity: str | None = None

    def __post_init__(self) -> None:
        if self.pair_index < 1:
            raise MultiwfnSessionParseError("NTO pair indices must be positive")
        if not 0.0 <= self.weight <= 1.0:
            raise MultiwfnSessionParseError("NTO pair weights must be within [0, 1]")
        if not 0.0 <= self.cumulative_weight <= 1.0 + 1.0e-9:
            raise MultiwfnSessionParseError(
                "NTO cumulative weights must be within [0, 1]"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "pair_index": self.pair_index,
            "weight": self.weight,
            "cumulative_weight": self.cumulative_weight,
            "hole_orbital_identity": self.hole_orbital_identity,
            "electron_orbital_identity": self.electron_orbital_identity,
        }


@dataclass(frozen=True)
class NtoPairSelection:
    cumulative_weight_cutoff: float
    retained_pair_count: int
    retained_pair_indices: tuple[int, ...]
    retained_cumulative_weight: float
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "cumulative_weight_cutoff": self.cumulative_weight_cutoff,
            "retained_pair_count": self.retained_pair_count,
            "retained_pair_indices": list(self.retained_pair_indices),
            "retained_cumulative_weight": self.retained_cumulative_weight,
            "status": self.status,
        }


def select_nto_pairs_for_cumulative_cutoff(
    pairs: Sequence[NtoPairRecord],
    cutoff: float,
) -> NtoPairSelection:
    selected_cutoff = float(cutoff)
    if not math.isfinite(selected_cutoff) or not 0.0 < selected_cutoff <= 1.0:
        raise MultiwfnSessionParseError(
            "NTO cumulative-weight cutoff must be within (0, 1]"
        )
    if not pairs:
        raise MultiwfnSessionParseError("NTO cutoff selection requires pair records")
    for pair in pairs:
        if pair.cumulative_weight >= selected_cutoff:
            retained = tuple(item.pair_index for item in pairs[: pair.pair_index])
            return NtoPairSelection(
                selected_cutoff,
                len(retained),
                retained,
                pair.cumulative_weight,
                "reached",
            )
    return NtoPairSelection(
        selected_cutoff,
        len(pairs),
        tuple(item.pair_index for item in pairs),
        pairs[-1].cumulative_weight,
        "printed_weights_below_cutoff",
    )


@dataclass(frozen=True)
class ParsedMultiwfnNto:
    state_evidence: MultiwfnSessionStateEvidence
    pairs: tuple[NtoPairRecord, ...]
    reported_printed_cumulative_weight: float
    printed_pair_count: int
    cutoff_selection: NtoPairSelection | None
    output_mwfn_path: str | None
    generated_output_references: tuple[str, ...]
    parser_complete: bool
    source_provenance: Mapping[str, object]
    parser_version: str = MULTIWFN_EXCITED_STATE_PARSER_VERSION
    grammar_id: str = MULTIWFN38_NTO_GRAMMAR

    def __post_init__(self) -> None:
        object.__setattr__(self, "pairs", tuple(self.pairs))
        object.__setattr__(
            self,
            "generated_output_references",
            tuple(self.generated_output_references),
        )
        object.__setattr__(self, "source_provenance", dict(self.source_provenance))

    def to_dict(self) -> dict[str, object]:
        return {
            "parser_version": self.parser_version,
            "grammar_id": self.grammar_id,
            "parser_complete": self.parser_complete,
            "state_evidence": self.state_evidence.to_dict(),
            "pairs": [item.to_dict() for item in self.pairs],
            "reported_printed_cumulative_weight": (
                self.reported_printed_cumulative_weight
            ),
            "printed_pair_count": self.printed_pair_count,
            "cutoff_selection": (
                self.cutoff_selection.to_dict()
                if self.cutoff_selection is not None
                else None
            ),
            "output_mwfn_path": self.output_mwfn_path,
            "generated_output_references": list(
                self.generated_output_references
            ),
            "source_provenance": dict(self.source_provenance),
        }


NTO_HEADER_RE = re.compile(
    r"^\s*The highest\s+(\d+)\s+eigenvalues of NTO pairs:\s*$",
    re.I | re.M,
)
NTO_SUM_RE = re.compile(
    rf"^\s*Sum of all eigenvalues:\s*({FLOAT})\s*$",
    re.I | re.M,
)


def _parse_multiwfn_nto_session(
    text: str,
    *,
    expected_state: ExcitedStateRecord,
    grammar_id: str,
    output_mwfn_path: str | None = None,
    generated_output_references: Sequence[str] = (),
    cumulative_weight_cutoff: float | None = None,
    source_provenance: Mapping[str, object] | None = None,
) -> ParsedMultiwfnNto:
    state = _parse_state_evidence(
        text,
        expected_state,
        analysis="nto",
        grammar_id=grammar_id,
    )
    header = _single_match(NTO_HEADER_RE, text, "NTO eigenvalue header")
    sum_matches = list(NTO_SUM_RE.finditer(text, header.end()))
    if len(sum_matches) != 1:
        raise MultiwfnSessionParseError(
            f"expected one NTO cumulative-weight line; found {len(sum_matches)}"
        )
    sum_match = sum_matches[0]
    weights_text = text[header.end() : sum_match.start()]
    weight_tokens = re.findall(FLOAT, weights_text)
    declared_count = int(header.group(1))
    weights = tuple(_number(item) for item in weight_tokens)
    if declared_count != 10 or len(weights) != declared_count:
        raise MultiwfnSessionParseError(
            "Multiwfn 3.8 NTO block must contain the declared top 10 weights"
        )
    if "Exporting .mwfn file finished!" not in text:
        raise MultiwfnSessionParseError("NTO mwfn export completion marker is missing")
    pairs: list[NtoPairRecord] = []
    cumulative = 0.0
    for index, weight in enumerate(weights, 1):
        cumulative += weight
        pairs.append(NtoPairRecord(index, weight, cumulative))
    reported_text = sum_match.group(1)
    reported = _number(reported_text)
    weight_decimals = max(_decimal_places(item) for item in weight_tokens)
    reported_decimals = _decimal_places(reported_text)
    uncertainty = len(weights) * 0.5 * 10.0 ** (-weight_decimals)
    uncertainty += 0.5 * 10.0 ** (-reported_decimals)
    if abs(cumulative - reported) > uncertainty + 1.0e-12:
        raise MultiwfnSessionParseError(
            "printed NTO weights are inconsistent with their reported cumulative value"
        )
    return ParsedMultiwfnNto(
        state,
        tuple(pairs),
        reported,
        len(pairs),
        (
            select_nto_pairs_for_cumulative_cutoff(
                pairs,
                cumulative_weight_cutoff,
            )
            if cumulative_weight_cutoff is not None
            else None
        ),
        output_mwfn_path,
        tuple(str(item) for item in generated_output_references),
        True,
        dict(source_provenance or {}),
        grammar_id=grammar_id,
    )


def parse_multiwfn38_nto_session(
    text: str,
    *,
    expected_state: ExcitedStateRecord,
    output_mwfn_path: str | None = None,
    generated_output_references: Sequence[str] = (),
    cumulative_weight_cutoff: float | None = None,
    source_provenance: Mapping[str, object] | None = None,
) -> ParsedMultiwfnNto:
    return _parse_multiwfn_nto_session(
        text,
        expected_state=expected_state,
        grammar_id=MULTIWFN38_NTO_GRAMMAR,
        output_mwfn_path=output_mwfn_path,
        generated_output_references=generated_output_references,
        cumulative_weight_cutoff=cumulative_weight_cutoff,
        source_provenance=source_provenance,
    )


def parse_multiwfn2026_nto_session(
    text: str,
    *,
    expected_state: ExcitedStateRecord,
    output_mwfn_path: str | None = None,
    generated_output_references: Sequence[str] = (),
    cumulative_weight_cutoff: float | None = None,
    source_provenance: Mapping[str, object] | None = None,
) -> ParsedMultiwfnNto:
    return _parse_multiwfn_nto_session(
        text,
        expected_state=expected_state,
        grammar_id=MULTIWFN2026_NTO_GRAMMAR,
        output_mwfn_path=output_mwfn_path,
        generated_output_references=generated_output_references,
        cumulative_weight_cutoff=cumulative_weight_cutoff,
        source_provenance=source_provenance,
    )


def _parse_multiwfn_nto_session_file(
    path: Path,
    *,
    expected_state: ExcitedStateRecord,
    grammar_id: str,
    output_mwfn_path: str | None = None,
    generated_output_references: Sequence[str] = (),
    cumulative_weight_cutoff: float | None = None,
) -> ParsedMultiwfnNto:
    source = path.expanduser().resolve(strict=True)
    data = source.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MultiwfnSessionParseError("NTO session log is not UTF-8 text") from exc
    return _parse_multiwfn_nto_session(
        text,
        expected_state=expected_state,
        grammar_id=grammar_id,
        output_mwfn_path=output_mwfn_path,
        generated_output_references=generated_output_references,
        cumulative_weight_cutoff=cumulative_weight_cutoff,
        source_provenance={
            "session_log_path": str(source),
            "session_log_sha256": hashlib.sha256(data).hexdigest(),
            "session_log_size_bytes": len(data),
            "parser_version": MULTIWFN_EXCITED_STATE_PARSER_VERSION,
            "fixture_tested_grammar_version": grammar_id,
            "process_exit_code_recorded": False,
        },
    )


def parse_multiwfn38_nto_session_file(
    path: Path,
    *,
    expected_state: ExcitedStateRecord,
    output_mwfn_path: str | None = None,
    generated_output_references: Sequence[str] = (),
    cumulative_weight_cutoff: float | None = None,
) -> ParsedMultiwfnNto:
    return _parse_multiwfn_nto_session_file(
        path,
        expected_state=expected_state,
        grammar_id=MULTIWFN38_NTO_GRAMMAR,
        output_mwfn_path=output_mwfn_path,
        generated_output_references=generated_output_references,
        cumulative_weight_cutoff=cumulative_weight_cutoff,
    )


def parse_multiwfn2026_nto_session_file(
    path: Path,
    *,
    expected_state: ExcitedStateRecord,
    output_mwfn_path: str | None = None,
    generated_output_references: Sequence[str] = (),
    cumulative_weight_cutoff: float | None = None,
) -> ParsedMultiwfnNto:
    return _parse_multiwfn_nto_session_file(
        path,
        expected_state=expected_state,
        grammar_id=MULTIWFN2026_NTO_GRAMMAR,
        output_mwfn_path=output_mwfn_path,
        generated_output_references=generated_output_references,
        cumulative_weight_cutoff=cumulative_weight_cutoff,
    )


@dataclass(frozen=True)
class DPrecisionValidation:
    reported_D_angstrom: float
    derived_D_from_reported_centroids_angstrom: float
    centroid_decimal_places: int
    reported_D_decimal_places: int
    centroid_norm_uncertainty_angstrom: float
    reported_rounding_half_width_angstrom: float
    derived_interval_angstrom: tuple[float, float]
    reported_interval_angstrom: tuple[float, float]
    consistent: bool
    policy: str = D_ROUNDING_POLICY

    def to_dict(self) -> dict[str, object]:
        return {
            "reported_D_angstrom": self.reported_D_angstrom,
            "derived_D_from_reported_centroids_angstrom": (
                self.derived_D_from_reported_centroids_angstrom
            ),
            "centroid_decimal_places": self.centroid_decimal_places,
            "reported_D_decimal_places": self.reported_D_decimal_places,
            "centroid_norm_uncertainty_angstrom": (
                self.centroid_norm_uncertainty_angstrom
            ),
            "reported_rounding_half_width_angstrom": (
                self.reported_rounding_half_width_angstrom
            ),
            "derived_interval_angstrom": list(self.derived_interval_angstrom),
            "reported_interval_angstrom": list(self.reported_interval_angstrom),
            "consistent": self.consistent,
            "policy": self.policy,
        }


def validate_reported_D_precision(
    hole_centroid: Sequence[float],
    electron_centroid: Sequence[float],
    *,
    reported_D_angstrom: float,
    centroid_decimal_places: int,
    reported_D_decimal_places: int,
) -> DPrecisionValidation:
    """Validate rounded D by overlap of mathematically bounded true-value intervals."""

    if len(hole_centroid) != 3 or len(electron_centroid) != 3:
        raise MultiwfnSessionParseError("hole/electron centroids must have three values")
    derived = math.sqrt(
        sum(
            (float(electron_centroid[index]) - float(hole_centroid[index])) ** 2
            for index in range(3)
        )
    )
    coordinate_uncertainty = math.sqrt(3.0) * 10.0 ** (-centroid_decimal_places)
    reported_half_width = 0.5 * 10.0 ** (-reported_D_decimal_places)
    derived_interval = (
        max(0.0, derived - coordinate_uncertainty),
        derived + coordinate_uncertainty,
    )
    reported_interval = (
        max(0.0, reported_D_angstrom - reported_half_width),
        reported_D_angstrom + reported_half_width,
    )
    consistent = max(derived_interval[0], reported_interval[0]) <= min(
        derived_interval[1], reported_interval[1]
    ) + 1.0e-12
    return DPrecisionValidation(
        reported_D_angstrom,
        derived,
        centroid_decimal_places,
        reported_D_decimal_places,
        coordinate_uncertainty,
        reported_half_width,
        derived_interval,
        reported_interval,
        consistent,
    )


@dataclass(frozen=True)
class ParsedMultiwfnHoleElectron:
    state_evidence: MultiwfnSessionStateEvidence
    sr: float
    reported_D_angstrom: float
    derived_D_from_reported_centroids_angstrom: float
    t_angstrom: float
    hole_centroid_angstrom: tuple[float, float, float]
    electron_centroid_angstrom: tuple[float, float, float]
    centroid_difference_angstrom: tuple[float, float, float]
    hole_extent_angstrom: float
    electron_extent_angstrom: float
    hole_extent_components_angstrom: tuple[float, float, float]
    electron_extent_components_angstrom: tuple[float, float, float]
    h_index_angstrom: float
    h_ct_angstrom: float
    hole_integral: float
    electron_integral: float
    transition_density_integral: float
    sm: float | None
    hdi: float | None
    edi: float | None
    grid_dimensions: tuple[int, int, int]
    total_grid_points: int
    grid_quality_label: str
    grid_quality_source: str
    grid_origin_bohr: tuple[float, float, float]
    grid_end_bohr: tuple[float, float, float]
    grid_spacing_bohr: tuple[float, float, float]
    coefficient_cross_term_threshold: float
    D_validation: DPrecisionValidation
    generated_output_references: tuple[str, ...]
    parser_complete: bool
    source_provenance: Mapping[str, object]
    parser_version: str = MULTIWFN_EXCITED_STATE_PARSER_VERSION
    grammar_id: str = MULTIWFN38_HEA_GRAMMAR
    units: Mapping[str, str] = field(
        default_factory=lambda: {
            "centroids": "angstrom",
            "D": "angstrom",
            "t": "angstrom",
            "spatial_extents": "angstrom",
            "H": "angstrom",
            "H_CT": "angstrom",
            "grid_coordinates": "bohr",
            "grid_spacing": "bohr",
            "Sr": "atomic_unit",
            "Sm": "atomic_unit",
            "integrals": "dimensionless",
        }
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_output_references",
            tuple(self.generated_output_references),
        )
        object.__setattr__(self, "source_provenance", dict(self.source_provenance))
        object.__setattr__(self, "units", dict(self.units))

    def to_dict(self) -> dict[str, object]:
        return {
            "parser_version": self.parser_version,
            "grammar_id": self.grammar_id,
            "parser_complete": self.parser_complete,
            "state_evidence": self.state_evidence.to_dict(),
            "Sr": self.sr,
            "Sm": self.sm,
            "reported_D_angstrom": self.reported_D_angstrom,
            "derived_D_from_reported_centroids_angstrom": (
                self.derived_D_from_reported_centroids_angstrom
            ),
            "t_angstrom": self.t_angstrom,
            "hole_centroid_angstrom": list(self.hole_centroid_angstrom),
            "electron_centroid_angstrom": list(self.electron_centroid_angstrom),
            "centroid_difference_angstrom": list(
                self.centroid_difference_angstrom
            ),
            "hole_extent_angstrom": self.hole_extent_angstrom,
            "electron_extent_angstrom": self.electron_extent_angstrom,
            "hole_extent_components_angstrom": list(
                self.hole_extent_components_angstrom
            ),
            "electron_extent_components_angstrom": list(
                self.electron_extent_components_angstrom
            ),
            "H_angstrom": self.h_index_angstrom,
            "H_CT_angstrom": self.h_ct_angstrom,
            "hole_integral": self.hole_integral,
            "electron_integral": self.electron_integral,
            "transition_density_integral": self.transition_density_integral,
            "HDI": self.hdi,
            "EDI": self.edi,
            "grid_dimensions": list(self.grid_dimensions),
            "total_grid_points": self.total_grid_points,
            "grid_quality_label": self.grid_quality_label,
            "grid_quality_source": self.grid_quality_source,
            "grid_origin_bohr": list(self.grid_origin_bohr),
            "grid_end_bohr": list(self.grid_end_bohr),
            "grid_spacing_bohr": list(self.grid_spacing_bohr),
            "coefficient_cross_term_threshold": (
                self.coefficient_cross_term_threshold
            ),
            "D_validation": self.D_validation.to_dict(),
            "generated_output_references": list(
                self.generated_output_references
            ),
            "units": dict(self.units),
            "source_provenance": dict(self.source_provenance),
        }

    def to_metrics(self) -> object:
        from cmw.molecular.stacking.hole_electron import HoleElectronMetrics

        return HoleElectronMetrics(
            self.hole_centroid_angstrom,
            self.electron_centroid_angstrom,
            self.reported_D_angstrom,
            self.sr,
            self.t_angstrom,
            reported_D_angstrom=self.reported_D_angstrom,
            centroid_decimal_places=self.D_validation.centroid_decimal_places,
            reported_D_decimal_places=(
                self.D_validation.reported_D_decimal_places
            ),
            hole_extent_angstrom=self.hole_extent_angstrom,
            electron_extent_angstrom=self.electron_extent_angstrom,
        )


GRID_ORIGIN_RE = re.compile(
    rf"Coordinate of origin in X,Y,Z is\s+({FLOAT})\s+({FLOAT})\s+({FLOAT})\s+Bohr",
    re.I,
)
GRID_END_RE = re.compile(
    rf"Coordinate of end point in X,Y,Z is\s+({FLOAT})\s+({FLOAT})\s+({FLOAT})\s+Bohr",
    re.I,
)
GRID_SPACING_RE = re.compile(
    rf"Grid spacing in X,Y,Z is\s+({FLOAT})\s+({FLOAT})\s+({FLOAT})\s+Bohr",
    re.I,
)
GRID_DIMENSIONS_RE = re.compile(
    r"Number of points in X,Y,Z is\s+(\d+)\s+(\d+)\s+(\d+)\s+Total:\s+(\d+)",
    re.I,
)
THRESHOLD_RE = re.compile(
    rf"absolute value of coefficient\s*<\s*({FLOAT})\s+will be ignored",
    re.I,
)


def _vector_match(pattern: re.Pattern[str], text: str, label: str) -> tuple[float, float, float]:
    match = _required_match(pattern, text, label)
    return (
        _number(match.group(1)),
        _number(match.group(2)),
        _number(match.group(3)),
    )


def _scalar(pattern: str, text: str, label: str) -> tuple[float, str]:
    match = _required_match(re.compile(pattern, re.I | re.M), text, label)
    return _number(match.group(1)), match.group(1)


def _parse_multiwfn_hole_electron_session(
    text: str,
    *,
    expected_state: ExcitedStateRecord,
    expected_grid_quality: str,
    grammar_id: str,
    source_provenance: Mapping[str, object] | None = None,
) -> ParsedMultiwfnHoleElectron:
    if expected_grid_quality != "medium":
        raise UnsupportedMultiwfnFormatError(
            "only the fixture-tested medium-grid HEA contract is supported"
        )
    state = _parse_state_evidence(
        text,
        expected_state,
        analysis="hole_electron",
        grammar_id=grammar_id,
    )
    origin = _vector_match(GRID_ORIGIN_RE, text, "grid origin")
    end = _vector_match(GRID_END_RE, text, "grid end point")
    spacing = _vector_match(GRID_SPACING_RE, text, "grid spacing")
    grid = _single_match(GRID_DIMENSIONS_RE, text, "grid dimensions")
    dimensions = tuple(int(grid.group(index)) for index in range(1, 4))
    total = int(grid.group(4))
    if any(item < 1 for item in dimensions) or math.prod(dimensions) != total:
        raise MultiwfnSessionParseError(
            "grid dimensions are invalid or conflict with the printed total"
        )
    threshold_match = _single_match(
        THRESHOLD_RE,
        text,
        "coefficient cross-term threshold",
    )
    threshold = _number(threshold_match.group(1))
    hole_integral, _ = _scalar(
        rf"^\s*Integral of hole:\s*({FLOAT})",
        text,
        "hole integral",
    )
    electron_integral, _ = _scalar(
        rf"^\s*Integral of electron:\s*({FLOAT})",
        text,
        "electron integral",
    )
    transition_integral, _ = _scalar(
        rf"^\s*Integral of transition density:\s*({FLOAT})",
        text,
        "transition-density integral",
    )
    sm_match = re.search(
        rf"^\s*Sm index \(integral of Sm function\):\s*({FLOAT})\s+a\.u\.",
        text,
        re.I | re.M,
    )
    sm = _number(sm_match.group(1)) if sm_match is not None else None
    sr, _ = _scalar(
        rf"^\s*Sr index \(integral of Sr function\):\s*({FLOAT})\s+a\.u\.",
        text,
        "Sr index",
    )
    hole_centroid_match = _required_match(
        re.compile(
            rf"Centroid of hole in X/Y/Z:\s*({FLOAT})\s+({FLOAT})\s+({FLOAT})\s+Angstrom",
            re.I,
        ),
        text,
        "hole centroid",
    )
    electron_centroid_match = _required_match(
        re.compile(
            rf"Centroid of electron in X/Y/Z:\s*({FLOAT})\s+({FLOAT})\s+({FLOAT})\s+Angstrom",
            re.I,
        ),
        text,
        "electron centroid",
    )
    hole_centroid = tuple(
        _number(hole_centroid_match.group(index)) for index in range(1, 4)
    )
    electron_centroid = tuple(
        _number(electron_centroid_match.group(index)) for index in range(1, 4)
    )
    centroid_decimals = min(
        *(
            _decimal_places(hole_centroid_match.group(index))
            for index in range(1, 4)
        ),
        *(
            _decimal_places(electron_centroid_match.group(index))
            for index in range(1, 4)
        ),
    )
    D_match = _required_match(
        re.compile(
            rf"D_x:\s*({FLOAT})\s+D_y:\s*({FLOAT})\s+D_z:\s*({FLOAT})"
            rf"\s+D index:\s*({FLOAT})\s+Angstrom",
            re.I,
        ),
        text,
        "D descriptor line",
    )
    centroid_difference = tuple(_number(D_match.group(index)) for index in range(1, 4))
    reported_D = _number(D_match.group(4))
    reported_D_decimals = _decimal_places(D_match.group(4))
    D_validation = validate_reported_D_precision(
        hole_centroid,
        electron_centroid,
        reported_D_angstrom=reported_D,
        centroid_decimal_places=centroid_decimals,
        reported_D_decimal_places=reported_D_decimals,
    )
    if not D_validation.consistent:
        raise MultiwfnSessionParseError(
            "printed D is inconsistent with centroid rounding intervals"
        )
    hole_extent_match = _required_match(
        re.compile(
            rf"RMSD of hole in X/Y/Z:\s*({FLOAT})\s+({FLOAT})\s+({FLOAT})"
            rf"\s+Norm:\s*({FLOAT})\s+Angstrom",
            re.I,
        ),
        text,
        "hole spatial extent",
    )
    electron_extent_match = _required_match(
        re.compile(
            rf"RMSD of electron in X/Y/Z:\s*({FLOAT})\s+({FLOAT})\s+({FLOAT})"
            rf"\s+Norm:\s*({FLOAT})\s+Angstrom",
            re.I,
        ),
        text,
        "electron spatial extent",
    )
    hole_components = tuple(
        _number(hole_extent_match.group(index)) for index in range(1, 4)
    )
    electron_components = tuple(
        _number(electron_extent_match.group(index)) for index in range(1, 4)
    )
    hole_extent = _number(hole_extent_match.group(4))
    electron_extent = _number(electron_extent_match.group(4))
    H_match = _required_match(
        re.compile(
            rf"H_x:\s*{FLOAT}\s+H_y:\s*{FLOAT}\s+H_z:\s*{FLOAT}"
            rf"\s+H_CT:\s*({FLOAT})\s+H index:\s*({FLOAT})\s+Angstrom",
            re.I,
        ),
        text,
        "H and H_CT descriptor line",
    )
    h_ct = _number(H_match.group(1))
    h_index = _number(H_match.group(2))
    t_value, _ = _scalar(
        rf"^\s*t index:\s*({FLOAT})\s+Angstrom",
        text,
        "t index",
    )
    hdi_match = re.search(
        rf"^\s*Hole delocalization index \(HDI\):\s*({FLOAT})",
        text,
        re.I | re.M,
    )
    edi_match = re.search(
        rf"^\s*Electron delocalization index \(EDI\):\s*({FLOAT})",
        text,
        re.I | re.M,
    )
    if "Outputting hole distribution to hole.cub" not in text:
        raise MultiwfnSessionParseError("hole cube output evidence is missing")
    if "Outputting electron distribution to electron.cub" not in text:
        raise MultiwfnSessionParseError("electron cube output evidence is missing")
    return ParsedMultiwfnHoleElectron(
        state,
        sr,
        reported_D,
        D_validation.derived_D_from_reported_centroids_angstrom,
        t_value,
        hole_centroid,  # type: ignore[arg-type]
        electron_centroid,  # type: ignore[arg-type]
        centroid_difference,  # type: ignore[arg-type]
        hole_extent,
        electron_extent,
        hole_components,  # type: ignore[arg-type]
        electron_components,  # type: ignore[arg-type]
        h_index,
        h_ct,
        hole_integral,
        electron_integral,
        transition_integral,
        sm,
        _number(hdi_match.group(1)) if hdi_match is not None else None,
        _number(edi_match.group(1)) if edi_match is not None else None,
        dimensions,  # type: ignore[arg-type]
        total,
        expected_grid_quality,
        "renderer_contract",
        origin,
        end,
        spacing,
        threshold,
        D_validation,
        ("hole.cub", "electron.cub"),
        True,
        dict(source_provenance or {}),
        grammar_id=grammar_id,
    )


def parse_multiwfn38_hole_electron_session(
    text: str,
    *,
    expected_state: ExcitedStateRecord,
    expected_grid_quality: str,
    source_provenance: Mapping[str, object] | None = None,
) -> ParsedMultiwfnHoleElectron:
    return _parse_multiwfn_hole_electron_session(
        text,
        expected_state=expected_state,
        expected_grid_quality=expected_grid_quality,
        grammar_id=MULTIWFN38_HEA_GRAMMAR,
        source_provenance=source_provenance,
    )


def parse_multiwfn2026_hole_electron_session(
    text: str,
    *,
    expected_state: ExcitedStateRecord,
    expected_grid_quality: str,
    source_provenance: Mapping[str, object] | None = None,
) -> ParsedMultiwfnHoleElectron:
    return _parse_multiwfn_hole_electron_session(
        text,
        expected_state=expected_state,
        expected_grid_quality=expected_grid_quality,
        grammar_id=MULTIWFN2026_HEA_GRAMMAR,
        source_provenance=source_provenance,
    )


def _parse_multiwfn_hole_electron_session_file(
    path: Path,
    *,
    expected_state: ExcitedStateRecord,
    expected_grid_quality: str,
    grammar_id: str,
) -> ParsedMultiwfnHoleElectron:
    source = path.expanduser().resolve(strict=True)
    data = source.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MultiwfnSessionParseError("HEA session log is not UTF-8 text") from exc
    return _parse_multiwfn_hole_electron_session(
        text,
        expected_state=expected_state,
        expected_grid_quality=expected_grid_quality,
        grammar_id=grammar_id,
        source_provenance={
            "session_log_path": str(source),
            "session_log_sha256": hashlib.sha256(data).hexdigest(),
            "session_log_size_bytes": len(data),
            "parser_version": MULTIWFN_EXCITED_STATE_PARSER_VERSION,
            "fixture_tested_grammar_version": grammar_id,
            "process_exit_code_recorded": False,
        },
    )


def parse_multiwfn38_hole_electron_session_file(
    path: Path,
    *,
    expected_state: ExcitedStateRecord,
    expected_grid_quality: str,
) -> ParsedMultiwfnHoleElectron:
    return _parse_multiwfn_hole_electron_session_file(
        path,
        expected_state=expected_state,
        expected_grid_quality=expected_grid_quality,
        grammar_id=MULTIWFN38_HEA_GRAMMAR,
    )


def parse_multiwfn2026_hole_electron_session_file(
    path: Path,
    *,
    expected_state: ExcitedStateRecord,
    expected_grid_quality: str,
) -> ParsedMultiwfnHoleElectron:
    return _parse_multiwfn_hole_electron_session_file(
        path,
        expected_state=expected_state,
        expected_grid_quality=expected_grid_quality,
        grammar_id=MULTIWFN2026_HEA_GRAMMAR,
    )


@dataclass(frozen=True)
class IfctFragmentPopulation:
    fragment_index: int
    fragment_label: str
    hole_fraction: float
    electron_fraction: float
    population_variation: float
    intrafragment_redistribution: float

    @property
    def delta_e_minus_h(self) -> float:
        return self.electron_fraction - self.hole_fraction


@dataclass(frozen=True)
class IfctTransferRecord:
    source_fragment_index: int
    target_fragment_index: int
    source_fragment_label: str
    target_fragment_label: str
    forward_electrons: float
    reverse_electrons: float
    net_source_to_target_electrons: float


@dataclass(frozen=True)
class ParsedMultiwfnIfct:
    state_evidence: MultiwfnSessionStateEvidence
    population_scheme: str
    fragments: tuple[IfctFragmentPopulation, ...]
    transfer: IfctTransferRecord
    intrinsic_ct_fraction: float
    intrinsic_le_fraction: float
    apparent_ct_fraction: float
    apparent_le_fraction: float
    parser_complete: bool
    source_provenance: Mapping[str, object]
    parser_version: str = MULTIWFN_EXCITED_STATE_PARSER_VERSION
    grammar_id: str = MULTIWFN2026_IFCT_GRAMMAR

    def __post_init__(self) -> None:
        object.__setattr__(self, "fragments", tuple(self.fragments))
        object.__setattr__(self, "source_provenance", dict(self.source_provenance))


IFCT_FRAGMENT_RE = re.compile(
    rf"^\s*(\d+)\s+Hole:\s*({FLOAT})\s*%\s+Electron:\s*({FLOAT})\s*%\s*$",
    re.I | re.M,
)
IFCT_VARIATION_RE = re.compile(
    rf"^\s*Variation of population number of fragment\s+(\d+):\s*({FLOAT})\s*$",
    re.I | re.M,
)
IFCT_INTRAFRAGMENT_RE = re.compile(
    rf"^\s*Intrafragment electron redistribution of fragment\s+(\d+):\s*({FLOAT})\s*$",
    re.I | re.M,
)
IFCT_TRANSFER_RE = re.compile(
    rf"^\s*(\d+)\s*->\s*(\d+):\s*({FLOAT})\s+"
    rf"\1\s*<-\s*\2:\s*({FLOAT})\s+Net\s+\1\s*->\s*\2:\s*({FLOAT})\s*$",
    re.I | re.M,
)


def _indexed_ifct_values(
    pattern: re.Pattern[str], text: str, *, label: str
) -> dict[int, float]:
    values: dict[int, float] = {}
    for match in pattern.finditer(text):
        index = int(match.group(1))
        if index in values:
            raise MultiwfnSessionParseError(
                f"duplicate IFCT {label} for fragment {index}"
            )
        values[index] = _number(match.group(2))
    return values


def parse_multiwfn2026_ifct_session(
    text: str,
    *,
    expected_state: ExcitedStateRecord,
    fragment_labels: Sequence[str],
    source_provenance: Mapping[str, object] | None = None,
) -> ParsedMultiwfnIfct:
    """Parse and validate the exact Multiwfn 2026.7.15 Hirshfeld IFCT grammar."""

    labels = tuple(str(value).strip() for value in fragment_labels)
    if len(labels) != 2 or any(not value for value in labels) or len(set(labels)) != 2:
        raise MultiwfnSessionParseError(
            "IFCT parsing requires exactly two unique fragment labels"
        )
    state = _parse_state_evidence(
        text,
        expected_state,
        analysis="ifct",
        grammar_id=MULTIWFN2026_IFCT_GRAMMAR,
    )
    if re.search(
        r"Radial grids:\s*\d+\s+Angular grids:\s*\d+\s+Total:\s*\d+",
        text,
        re.I,
    ) is None:
        raise MultiwfnSessionParseError(
            "Hirshfeld atomic-grid evidence is missing from IFCT output"
        )
    if (
        "Construction of interfragment charger-transfer matrix has finished!"
        not in text
    ):
        raise MultiwfnSessionParseError("IFCT matrix completion marker is missing")

    population_matches = list(IFCT_FRAGMENT_RE.finditer(text))
    if len(population_matches) != 2:
        raise MultiwfnSessionParseError(
            "expected two IFCT fragment-population rows; "
            f"found {len(population_matches)}"
        )
    populations: dict[int, tuple[float, float]] = {}
    for match in population_matches:
        index = int(match.group(1))
        if index in populations:
            raise MultiwfnSessionParseError(
                f"duplicate IFCT population for fragment {index}"
            )
        populations[index] = (
            _number(match.group(2)) / 100.0,
            _number(match.group(3)) / 100.0,
        )
    expected_indices = {1, 2}
    if set(populations) != expected_indices:
        raise MultiwfnSessionParseError(
            "IFCT population fragment indices are incomplete"
        )
    if any(
        not 0.0 <= value <= 1.0
        for pair in populations.values()
        for value in pair
    ):
        raise MultiwfnSessionParseError(
            "IFCT fragment populations must be within [0, 1]"
        )
    printed_population_tolerance = 2 * 0.5e-4 + 1.0e-12
    if (
        abs(sum(value[0] for value in populations.values()) - 1.0)
        > printed_population_tolerance
        or abs(sum(value[1] for value in populations.values()) - 1.0)
        > printed_population_tolerance
    ):
        raise MultiwfnSessionParseError(
            "IFCT fragment hole/electron populations do not close to unity"
        )

    variations = _indexed_ifct_values(
        IFCT_VARIATION_RE, text, label="population variation"
    )
    redistributions = _indexed_ifct_values(
        IFCT_INTRAFRAGMENT_RE, text, label="intrafragment redistribution"
    )
    if set(variations) != expected_indices or set(redistributions) != expected_indices:
        raise MultiwfnSessionParseError("IFCT per-fragment quantities are incomplete")
    if any(not math.isfinite(value) for value in variations.values()) or any(
        not math.isfinite(value) or value < 0.0
        for value in redistributions.values()
    ):
        raise MultiwfnSessionParseError(
            "IFCT per-fragment quantities are not finite physical values"
        )

    transfers = list(IFCT_TRANSFER_RE.finditer(text))
    if len(transfers) != 1:
        raise MultiwfnSessionParseError(
            f"expected one two-fragment IFCT transfer row; found {len(transfers)}"
        )
    transfer_match = transfers[0]
    source_index = int(transfer_match.group(1))
    target_index = int(transfer_match.group(2))
    if {source_index, target_index} != expected_indices:
        raise MultiwfnSessionParseError("IFCT transfer indices do not match fragments")
    forward = _number(transfer_match.group(3))
    reverse = _number(transfer_match.group(4))
    net = _number(transfer_match.group(5))
    if any(not math.isfinite(value) or value < 0.0 for value in (forward, reverse)):
        raise MultiwfnSessionParseError(
            "IFCT directional transfers must be non-negative"
        )
    if not math.isclose(forward - reverse, net, abs_tol=1.5e-5):
        raise MultiwfnSessionParseError(
            "IFCT net transfer conflicts with directional terms"
        )

    fragments: list[IfctFragmentPopulation] = []
    for index in (1, 2):
        hole, electron = populations[index]
        delta = electron - hole
        if not math.isclose(delta, variations[index], abs_tol=1.1e-4):
            raise MultiwfnSessionParseError(
                f"IFCT population variation conflicts for fragment {index}"
            )
        if not math.isclose(
            hole * electron,
            redistributions[index],
            abs_tol=1.2e-4,
        ):
            raise MultiwfnSessionParseError(
                f"IFCT intrafragment redistribution conflicts for fragment {index}"
            )
        fragments.append(
            IfctFragmentPopulation(
                index,
                labels[index - 1],
                hole,
                electron,
                variations[index],
                redistributions[index],
            )
        )
    if not math.isclose(sum(variations.values()), 0.0, abs_tol=1.1e-5):
        raise MultiwfnSessionParseError(
            "IFCT population variations do not conserve charge"
        )
    source_consistent = math.isclose(
        variations[source_index], -net, abs_tol=1.1e-5
    )
    target_consistent = math.isclose(
        variations[target_index], net, abs_tol=1.1e-5
    )
    if not source_consistent or not target_consistent:
        raise MultiwfnSessionParseError(
            "IFCT fragment variations conflict with net directional transfer"
        )

    def percentage(name: str) -> float:
        value, _ = _scalar(
            rf"^\s*{name} percentage, (?:CT|LE)\(%\):\s*({FLOAT})\s*%",
            text,
            name,
        )
        fraction = value / 100.0
        if not math.isfinite(fraction) or not 0.0 <= fraction <= 1.0:
            raise MultiwfnSessionParseError(
                f"{name} fraction must be within [0, 1]"
            )
        return fraction

    intrinsic_ct = percentage("Intrinsic charge transfer")
    intrinsic_le = percentage("Intrinsic local excitation")
    apparent_ct = percentage("Apparent charge transfer")
    apparent_le = percentage("Apparent local excitation")
    if not math.isclose(intrinsic_ct + intrinsic_le, 1.0, abs_tol=1.1e-5):
        raise MultiwfnSessionParseError("intrinsic IFCT and LE fractions do not close")
    if not math.isclose(apparent_ct + apparent_le, 1.0, abs_tol=1.1e-5):
        raise MultiwfnSessionParseError("apparent IFCT and LE fractions do not close")
    if not math.isclose(intrinsic_ct, forward + reverse, abs_tol=1.6e-5):
        raise MultiwfnSessionParseError(
            "intrinsic IFCT conflicts with directional terms"
        )
    if not math.isclose(apparent_ct, abs(net), abs_tol=1.1e-5):
        raise MultiwfnSessionParseError("apparent IFCT conflicts with net transfer")

    return ParsedMultiwfnIfct(
        state,
        "hirshfeld",
        tuple(fragments),
        IfctTransferRecord(
            source_index,
            target_index,
            labels[source_index - 1],
            labels[target_index - 1],
            forward,
            reverse,
            net,
        ),
        intrinsic_ct,
        intrinsic_le,
        apparent_ct,
        apparent_le,
        True,
        dict(source_provenance or {}),
    )


def parse_multiwfn2026_ifct_session_file(
    path: Path,
    *,
    expected_state: ExcitedStateRecord,
    fragment_labels: Sequence[str],
) -> ParsedMultiwfnIfct:
    source = path.expanduser().resolve(strict=True)
    data = source.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MultiwfnSessionParseError("IFCT session log is not UTF-8 text") from exc
    return parse_multiwfn2026_ifct_session(
        text,
        expected_state=expected_state,
        fragment_labels=fragment_labels,
        source_provenance={
            "session_log_path": str(source),
            "session_log_sha256": hashlib.sha256(data).hexdigest(),
            "session_log_size_bytes": len(data),
            "parser_version": MULTIWFN_EXCITED_STATE_PARSER_VERSION,
            "fixture_tested_grammar_version": MULTIWFN2026_IFCT_GRAMMAR,
            "process_exit_code_recorded": False,
        },
    )


def _excited_state_parent_check(
    excited_state: ExcitedStateArtifact,
    selected_state: ExcitedStateRecord,
) -> None:
    try:
        validate_excited_state_selection_contract(excited_state, selected_state)
    except ExcitedStateSelectionContractError as exc:
        raise MultiwfnFinalizationError(str(exc)) from exc


def _alias_manifest(
    runtime_provenance: Mapping[str, object],
) -> dict[str, str] | None:
    value = runtime_provenance.get("alias_manifest_path")
    if value is None:
        return None
    if not isinstance(value, str):
        raise MultiwfnFinalizationError("runtime alias manifest path is invalid")
    try:
        path = Path(value).expanduser().resolve(strict=True)
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise MultiwfnFinalizationError(
            "runtime alias manifest is missing or unreadable"
        ) from exc
    expected_hash = runtime_provenance.get("alias_manifest_sha256")
    if not isinstance(expected_hash, str) or hashlib.sha256(data).hexdigest() != expected_hash:
        raise MultiwfnFinalizationError("runtime alias manifest hash mismatch")
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line or "=" not in line:
            raise MultiwfnFinalizationError("runtime alias manifest is malformed")
        key, item = line.split("=", 1)
        if key in result or key not in {
            "settings",
            "settings_target",
            "source",
            "source_target",
        }:
            raise MultiwfnFinalizationError("runtime alias manifest is malformed")
        result[key] = item
    required = {"settings", "settings_target", "source", "source_target"}
    if set(result) != required:
        raise MultiwfnFinalizationError(
            "runtime alias manifest lacks required source/settings identities"
        )
    return result


def _validate_loaded_wavefunction(
    loaded_wavefunction_path: str,
    rendered: RenderedMultiwfnExcitedStateInput,
    runtime_provenance: Mapping[str, object],
) -> None:
    expected_wavefunction = Path(
        str(rendered.source_wavefunction_identity["path"])
    ).resolve()
    manifest = _alias_manifest(runtime_provenance)
    if manifest is None:
        loaded = Path(loaded_wavefunction_path).expanduser().resolve()
        if loaded != expected_wavefunction:
            raise MultiwfnFinalizationError(
                "session wavefunction does not match the rendered command source"
            )
        return
    if loaded_wavefunction_path != manifest["source"]:
        raise MultiwfnFinalizationError(
            "session wavefunction does not match the recorded runtime alias"
        )
    source_target = Path(manifest["source_target"]).expanduser().resolve()
    if source_target != expected_wavefunction:
        raise MultiwfnFinalizationError(
            "runtime alias source target differs from the rendered source"
        )
    settings_target = Path(manifest["settings_target"]).expanduser().resolve()
    expected_settings = Path(str(runtime_provenance.get("settings_path", ""))).resolve()
    if settings_target != expected_settings.parent:
        raise MultiwfnFinalizationError(
            "runtime alias settings target differs from runtime provenance"
        )


def _finalization_evidence(
    excited_state: ExcitedStateArtifact,
    rendered: RenderedMultiwfnExcitedStateInput,
    *,
    parsed_identity: ExcitedStateIdentity,
    parsed_version: str,
    loaded_wavefunction_path: str,
    process_exit_code: int | None,
    files: Mapping[str, str | Path],
    runtime_provenance: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    if isinstance(process_exit_code, bool) or process_exit_code != 0:
        raise MultiwfnFinalizationError(
            "artifact finalization requires a captured zero process exit code"
        )
    analysis = "nto" if rendered.analysis == "nto" else "hole_electron"
    _require_grammar(parsed_version, analysis, rendered.grammar_id)
    runtime_version = runtime_provenance.get("version")
    if not isinstance(runtime_version, str) or runtime_version != parsed_version:
        raise MultiwfnFinalizationError(
            "runtime and parsed Multiwfn versions do not match"
        )
    if runtime_provenance.get("menu_contract") != MENU_CONTRACT:
        raise MultiwfnFinalizationError("runtime menu contract is incompatible")
    if parsed_identity != rendered.canonical_identity:
        raise MultiwfnFinalizationError("parsed and rendered state identities differ")
    _validate_loaded_wavefunction(
        loaded_wavefunction_path,
        rendered,
        runtime_provenance,
    )
    if rendered.scientific_protocol_hash != excited_state.metadata.get(
        "scientific_protocol_hash"
    ):
        raise MultiwfnFinalizationError("scientific protocol hash mismatch")
    if rendered.source_geometry_hash != excited_state.metadata.get(
        "source_geometry_hash"
    ):
        raise MultiwfnFinalizationError("source geometry hash mismatch")
    source_identity = excited_state.metadata.get("source_output_identity")
    if not isinstance(source_identity, Mapping) or source_identity.get(
        "source_sha256"
    ) != rendered.orca_output_identity.get("sha256"):
        raise MultiwfnFinalizationError("ORCA source output identity mismatch")
    required_roles = {item.role for item in rendered.outputs if item.required}
    if not required_roles.issubset(files):
        raise MultiwfnFinalizationError(
            "required Multiwfn outputs are absent: "
            + ", ".join(sorted(required_roles - set(files)))
        )
    normalized: dict[str, str] = {}
    identities: dict[str, dict[str, object]] = {}
    specs = {item.role: item for item in rendered.outputs}
    working_directory = Path(
        str(rendered.execution_layout["working_directory"])
    ).resolve()
    for role in required_roles:
        try:
            path = Path(files[role]).expanduser().resolve(strict=True)
        except OSError as exc:
            raise MultiwfnFinalizationError(
                f"required Multiwfn output is missing or unreadable: {role}"
            ) from exc
        if not path.is_file() or path.stat().st_size < 1:
            raise MultiwfnFinalizationError(
                f"required Multiwfn output is missing or empty: {role}"
            )
        expected_path = (working_directory / specs[role].output_path).resolve()
        if path != expected_path:
            raise MultiwfnFinalizationError(
                f"Multiwfn output path differs from the rendered specification: {role}"
            )
        normalized[role] = str(path)
        identities[role] = _file_identity(path)
    return normalized, identities


def finalize_multiwfn_nto_artifact(
    excited_state: ExcitedStateArtifact,
    rendered: RenderedMultiwfnExcitedStateInput,
    parsed: ParsedMultiwfnNto,
    *,
    process_exit_code: int | None,
    files: Mapping[str, str | Path],
    runtime_provenance: Mapping[str, object],
) -> NTOArtifact:
    if rendered.analysis != "nto" or rendered.grammar_id not in {
        MULTIWFN38_NTO_GRAMMAR,
        MULTIWFN2026_NTO_GRAMMAR,
    }:
        raise MultiwfnFinalizationError("NTO finalization received another grammar")
    if parsed.grammar_id != rendered.grammar_id:
        raise MultiwfnFinalizationError("NTO parser and renderer grammars differ")
    if not parsed.parser_complete:
        raise MultiwfnFinalizationError("NTO parser evidence is incomplete")
    _excited_state_parent_check(excited_state, rendered.selected_state)
    normalized, output_identities = _finalization_evidence(
        excited_state,
        rendered,
        parsed_identity=parsed.state_evidence.selected_identity,
        parsed_version=parsed.state_evidence.multiwfn_version,
        loaded_wavefunction_path=(
            parsed.state_evidence.loaded_source_wavefunction_path
        ),
        process_exit_code=process_exit_code,
        files=files,
        runtime_provenance=runtime_provenance,
    )
    if parsed.source_provenance.get("session_log_sha256") != output_identities[
        "session_log"
    ]["sha256"]:
        raise MultiwfnFinalizationError(
            "parsed NTO session identity differs from the finalized session log"
        )
    artifact = NTOArtifact(
        producing_calculation="multiwfn_nto",
        method=excited_state.method,
        basis=excited_state.basis,
        protocol={
            "analysis": "natural_transition_orbitals",
            "selected_state": rendered.canonical_identity.to_dict(),
            "renderer_grammar": rendered.grammar_id,
        },
        parent_artifacts=(excited_state.artifact_id,),
        files=normalized,
        validation=ArtifactValidation(
            ValidationStatus.PASSED,
            {
                "process_exit_code": True,
                "parser_complete": True,
                "required_outputs": True,
                "state_identity": True,
                "source_identity": True,
            },
            "VALID_MULTIWFN_NTO_ARTIFACT",
            "Multiwfn NTO execution, parser, outputs, and lineage are complete",
        ),
        provenance={
            "runtime": dict(runtime_provenance),
            "renderer": rendered.to_dict(),
            "parser": dict(parsed.source_provenance),
            "execution_attempt": {
                **dict(rendered.execution_attempt),
                "process_exit_code": process_exit_code,
            },
            "output_identities": output_identities,
        },
        metadata={
            "nto_contract": "multiwfn_nto_v1",
            "excited_state_artifact": excited_state.artifact_id,
            "selected_state_identity": rendered.canonical_identity.to_dict(),
            "state_source_indices": _state_metadata(rendered.selected_state),
            "generation_method": (
                f"Multiwfn {parsed.state_evidence.multiwfn_version} "
                "natural transition orbitals"
            ),
            "orbital_pairs": [item.to_dict() for item in parsed.pairs],
            "printed_pair_count": parsed.printed_pair_count,
            "printed_cumulative_weight": (
                parsed.reported_printed_cumulative_weight
            ),
            "pair_selection": (
                parsed.cutoff_selection.to_dict()
                if parsed.cutoff_selection is not None
                else None
            ),
            "output_mwfn_file": normalized["nto_mwfn"],
            "generated_output_references": list(
                parsed.generated_output_references
            ),
            "source_geometry_hash": rendered.source_geometry_hash,
            "scientific_protocol_hash": rendered.scientific_protocol_hash,
            "renderer_version": rendered.renderer_version,
            "renderer_grammar": rendered.grammar_id,
            "stdin_sha256": rendered.stdin_sha256,
            "parser_version": parsed.parser_version,
            "parser_grammar": parsed.grammar_id,
            "source_wavefunction_identity": dict(
                rendered.source_wavefunction_identity
            ),
            "orca_source_output_identity": dict(rendered.orca_output_identity),
        },
    )
    validate_artifact_compatibility(artifact, (excited_state,))
    return artifact


def finalize_multiwfn_hole_electron_artifact(
    excited_state: ExcitedStateArtifact,
    rendered: RenderedMultiwfnExcitedStateInput,
    parsed: ParsedMultiwfnHoleElectron,
    *,
    process_exit_code: int | None,
    files: Mapping[str, str | Path],
    runtime_provenance: Mapping[str, object],
) -> HoleElectronArtifact:
    if (
        rendered.analysis != "hole_electron"
        or rendered.grammar_id not in {
            MULTIWFN38_HEA_GRAMMAR,
            MULTIWFN2026_HEA_GRAMMAR,
        }
    ):
        raise MultiwfnFinalizationError("HEA finalization received another grammar")
    if parsed.grammar_id != rendered.grammar_id:
        raise MultiwfnFinalizationError("HEA parser and renderer grammars differ")
    if not parsed.parser_complete:
        raise MultiwfnFinalizationError("HEA parser evidence is incomplete")
    _excited_state_parent_check(excited_state, rendered.selected_state)
    normalized, output_identities = _finalization_evidence(
        excited_state,
        rendered,
        parsed_identity=parsed.state_evidence.selected_identity,
        parsed_version=parsed.state_evidence.multiwfn_version,
        loaded_wavefunction_path=(
            parsed.state_evidence.loaded_source_wavefunction_path
        ),
        process_exit_code=process_exit_code,
        files=files,
        runtime_provenance=runtime_provenance,
    )
    if parsed.source_provenance.get("session_log_sha256") != output_identities[
        "session_log"
    ]["sha256"]:
        raise MultiwfnFinalizationError(
            "parsed HEA session identity differs from the finalized session log"
        )
    metrics = parsed.to_metrics()
    artifact = HoleElectronArtifact(
        producing_calculation="multiwfn_hole_electron",
        method=excited_state.method,
        basis=excited_state.basis,
        protocol={
            "analysis": "nonfragment_hole_electron",
            "selected_state": rendered.canonical_identity.to_dict(),
            "renderer_grammar": rendered.grammar_id,
        },
        parent_artifacts=(excited_state.artifact_id,),
        files=normalized,
        validation=ArtifactValidation(
            ValidationStatus.PASSED,
            {
                "process_exit_code": True,
                "parser_complete": True,
                "required_outputs": True,
                "state_identity": True,
                "source_identity": True,
                "D_rounding_consistency": parsed.D_validation.consistent,
            },
            "VALID_MULTIWFN_NONFRAGMENT_HEA_ARTIFACT",
            "Multiwfn HEA execution, parser, outputs, metrics, and lineage are complete",
        ),
        provenance={
            "runtime": dict(runtime_provenance),
            "renderer": rendered.to_dict(),
            "parser": dict(parsed.source_provenance),
            "execution_attempt": {
                **dict(rendered.execution_attempt),
                "process_exit_code": process_exit_code,
            },
            "output_identities": output_identities,
        },
        metadata={
            "hole_electron_contract": "multiwfn_nonfragment_hea_v1",
            "multiwfn_protocol": rendered.to_dict(),
            "excited_state_artifact": excited_state.artifact_id,
            "selected_state_identity": rendered.canonical_identity.to_dict(),
            "state_source_indices": _state_metadata(rendered.selected_state),
            **metrics.to_dict(),
            **parsed.to_dict(),
            "source_geometry_hash": rendered.source_geometry_hash,
            "scientific_protocol_hash": rendered.scientific_protocol_hash,
            "renderer_version": rendered.renderer_version,
            "renderer_grammar": rendered.grammar_id,
            "stdin_sha256": rendered.stdin_sha256,
            "parser_version": parsed.parser_version,
            "parser_grammar": parsed.grammar_id,
            "source_wavefunction_identity": dict(
                rendered.source_wavefunction_identity
            ),
            "orca_source_output_identity": dict(rendered.orca_output_identity),
            "fragment_resolved": False,
        },
    )
    validate_artifact_compatibility(artifact, (excited_state,))
    return artifact


__all__ = [
    "DPrecisionValidation",
    "D_ROUNDING_POLICY",
    "DeferredFragmentAnalysisError",
    "MULTIWFN38_HEA_GRAMMAR",
    "MULTIWFN38_NTO_GRAMMAR",
    "MULTIWFN2026_7_15_VERSION",
    "MULTIWFN2026_HEA_GRAMMAR",
    "MULTIWFN2026_IFCT_GRAMMAR",
    "MULTIWFN2026_NTO_GRAMMAR",
    "MULTIWFN_EXCITED_STATE_PARSER_VERSION",
    "MULTIWFN_EXCITED_STATE_RENDERER_VERSION",
    "Multiwfn38HoleElectronRenderer",
    "Multiwfn38NtoRenderer",
    "Multiwfn2026HoleElectronRenderer",
    "Multiwfn2026IfctRenderer",
    "Multiwfn2026NtoRenderer",
    "MultiwfnExcitedStateError",
    "MultiwfnFinalizationError",
    "MultiwfnSessionParseError",
    "MultiwfnSessionStateEvidence",
    "MultiwfnStateIdentityError",
    "NtoPairRecord",
    "NtoPairSelection",
    "IfctFragmentPopulation",
    "IfctTransferRecord",
    "ParsedMultiwfnHoleElectron",
    "ParsedMultiwfnIfct",
    "ParsedMultiwfnNto",
    "RenderedMultiwfnExcitedStateInput",
    "UnsupportedMultiwfnFormatError",
    "build_excited_state_command_spec",
    "finalize_multiwfn_hole_electron_artifact",
    "finalize_multiwfn_nto_artifact",
    "parse_multiwfn38_hole_electron_session",
    "parse_multiwfn38_hole_electron_session_file",
    "parse_multiwfn38_nto_session",
    "parse_multiwfn38_nto_session_file",
    "parse_multiwfn2026_hole_electron_session",
    "parse_multiwfn2026_hole_electron_session_file",
    "parse_multiwfn2026_ifct_session",
    "parse_multiwfn2026_ifct_session_file",
    "parse_multiwfn2026_nto_session",
    "parse_multiwfn2026_nto_session_file",
    "select_nto_pairs_for_cumulative_cutoff",
    "validate_reported_D_precision",
]
