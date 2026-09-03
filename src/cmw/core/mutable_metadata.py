"""Bounded, reversible mutations for payload-heavy JSON migration records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Mapping, Sequence

from .provenance import file_hash, stable_hash


DEFAULT_FULL_COPY_MAX_BYTES = 1024 * 1024
DEFAULT_STRUCTURED_PATCH_MAX_CHANGED_FIELDS = 4096
DEFAULT_STRUCTURED_PATCH_MAX_LINE_BYTES = 2 * 1024 * 1024
DEFAULT_STRUCTURED_PATCH_MAX_BACKUP_BYTES = 16 * 1024 * 1024
STRUCTURED_MUTATION_IMPLEMENTATION = "cmw-json-token-offset-patch/1"
LARGE_STRUCTURED_MUTATION_IMPLEMENTATION = (
    "cmw-indented-json-token-offset-patch/1"
)
LARGE_STRUCTURED_SCANNER_THRESHOLD_BYTES = 64 * 1024 * 1024


class MutableBackupStrategy(str, Enum):
    FULL_COPY = "FULL_COPY"
    STRUCTURED_INVERSE_PATCH = "STRUCTURED_INVERSE_PATCH"
    MIGRATION_OVERLAY_ONLY = "MIGRATION_OVERLAY_ONLY"
    BLOCK_UNSAFE_LARGE_RECORD = "BLOCK_UNSAFE_LARGE_RECORD"


class MutableRecordError(RuntimeError):
    """Fail-closed error raised by bounded mutable-record handling."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MutableBackupPolicy:
    """Operator-selectable limits; none of these values is scientific identity."""

    full_copy_max_bytes: int = DEFAULT_FULL_COPY_MAX_BYTES
    structured_patch_max_changed_fields: int = (
        DEFAULT_STRUCTURED_PATCH_MAX_CHANGED_FIELDS
    )
    structured_patch_max_line_bytes: int = DEFAULT_STRUCTURED_PATCH_MAX_LINE_BYTES
    structured_patch_max_backup_bytes: int = DEFAULT_STRUCTURED_PATCH_MAX_BACKUP_BYTES
    supported_record_schema_versions: tuple[int, ...] = (1,)
    large_record_policy: str = MutableBackupStrategy.STRUCTURED_INVERSE_PATCH.value
    overlay_only_records: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.full_copy_max_bytes < 0:
            raise ValueError("full_copy_max_bytes must be non-negative")
        if self.structured_patch_max_changed_fields < 1:
            raise ValueError("structured_patch_max_changed_fields must be positive")
        if self.structured_patch_max_line_bytes < 1024:
            raise ValueError("structured_patch_max_line_bytes must be at least 1024")
        if self.structured_patch_max_backup_bytes < 1024:
            raise ValueError("structured_patch_max_backup_bytes must be at least 1024")
        if self.large_record_policy not in {
            MutableBackupStrategy.STRUCTURED_INVERSE_PATCH.value,
            MutableBackupStrategy.BLOCK_UNSAFE_LARGE_RECORD.value,
        }:
            raise ValueError("large_record_policy must patch or block")

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["supported_record_schema_versions"] = list(
            self.supported_record_schema_versions
        )
        value["overlay_only_records"] = list(self.overlay_only_records)
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> "MutableBackupPolicy":
        if value is None:
            return cls()
        return cls(
            full_copy_max_bytes=int(
                value.get("full_copy_max_bytes", DEFAULT_FULL_COPY_MAX_BYTES)
            ),
            structured_patch_max_changed_fields=int(
                value.get(
                    "structured_patch_max_changed_fields",
                    DEFAULT_STRUCTURED_PATCH_MAX_CHANGED_FIELDS,
                )
            ),
            structured_patch_max_line_bytes=int(
                value.get(
                    "structured_patch_max_line_bytes",
                    DEFAULT_STRUCTURED_PATCH_MAX_LINE_BYTES,
                )
            ),
            structured_patch_max_backup_bytes=int(
                value.get(
                    "structured_patch_max_backup_bytes",
                    DEFAULT_STRUCTURED_PATCH_MAX_BACKUP_BYTES,
                )
            ),
            supported_record_schema_versions=tuple(
                int(item)
                for item in value.get("supported_record_schema_versions", (1,))
            ),
            large_record_policy=str(
                value.get(
                    "large_record_policy",
                    MutableBackupStrategy.STRUCTURED_INVERSE_PATCH.value,
                )
            ),
            overlay_only_records=tuple(
                str(item) for item in value.get("overlay_only_records", ())
            ),
        )


@dataclass(frozen=True)
class StructuredFieldPatch:
    pointer: str
    preimage_start: int
    preimage_end: int
    postimage_start: int
    postimage_end: int
    old_json: str
    new_json: str
    old_value: str
    new_value: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "StructuredFieldPatch":
        return cls(
            pointer=str(value["pointer"]),
            preimage_start=int(value["preimage_start"]),
            preimage_end=int(value["preimage_end"]),
            postimage_start=int(value["postimage_start"]),
            postimage_end=int(value["postimage_end"]),
            old_json=str(value["old_json"]),
            new_json=str(value["new_json"]),
            old_value=str(value["old_value"]),
            new_value=str(value["new_value"]),
        )


@dataclass(frozen=True)
class StructuredPatchPlan:
    record_schema_version: int
    mutation_implementation: str
    preimage_sha256: str
    postimage_sha256: str
    source_size_bytes: int
    patches: tuple[StructuredFieldPatch, ...]
    patch_sha256: str
    projected_backup_bytes: int


_TOKEN = re.compile(
    rb'"(?:\\["\\/bfnrt]|\\u[0-9a-fA-F]{4}|[^"\\\x00-\x1f])*"'
    rb"|[{}\[\]:,]"
    rb"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
    rb"|true|false|null"
)
_WHITESPACE = re.compile(rb"^[\x20\x09\x0a\x0d]*$")


def _pointer(parts: Sequence[str | int]) -> str:
    if not parts:
        return ""
    return "/" + "/".join(
        str(item).replace("~", "~0").replace("/", "~1") for item in parts
    )


class _JsonPathState:
    """Small streaming JSON grammar tracker used only to identify value paths."""

    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []
        self.root_consumed = False
        self.changed_pointers: set[str] = set()

    def _consume_value(self) -> tuple[str | int, ...]:
        if not self.frames:
            if self.root_consumed:
                raise MutableRecordError("UNSAFE_LARGE_RECORD", "multiple JSON roots")
            self.root_consumed = True
            return ()
        frame = self.frames[-1]
        path = tuple(frame["path"])
        if frame["kind"] == "object":
            if frame["state"] != "value":
                raise MutableRecordError("UNSAFE_LARGE_RECORD", "unexpected object value")
            key = str(frame["key"])
            frame["key"] = None
            frame["state"] = "comma_or_end"
            return path + (key,)
        if frame["state"] != "value_or_end":
            raise MutableRecordError("UNSAFE_LARGE_RECORD", "unexpected array value")
        index = int(frame["index"])
        frame["index"] = index + 1
        frame["state"] = "comma_or_end"
        return path + (index,)

    def consume(self, raw: bytes) -> tuple[tuple[str | int, ...] | None, object | None]:
        token = raw[:1]
        if token == b"{":
            path = self._consume_value()
            self.frames.append(
                {"kind": "object", "path": path, "state": "key_or_end", "key": None}
            )
            return None, None
        if token == b"[":
            path = self._consume_value()
            self.frames.append(
                {"kind": "array", "path": path, "state": "value_or_end", "index": 0}
            )
            return None, None
        if token == b"}":
            if not self.frames or self.frames[-1]["kind"] != "object":
                raise MutableRecordError("UNSAFE_LARGE_RECORD", "unexpected object end")
            if self.frames[-1]["state"] not in {"key_or_end", "comma_or_end"}:
                raise MutableRecordError("UNSAFE_LARGE_RECORD", "incomplete object")
            self.frames.pop()
            return None, None
        if token == b"]":
            if not self.frames or self.frames[-1]["kind"] != "array":
                raise MutableRecordError("UNSAFE_LARGE_RECORD", "unexpected array end")
            if self.frames[-1]["state"] not in {"value_or_end", "comma_or_end"}:
                raise MutableRecordError("UNSAFE_LARGE_RECORD", "incomplete array")
            self.frames.pop()
            return None, None
        if token == b":":
            if not self.frames or self.frames[-1]["kind"] != "object" or self.frames[-1]["state"] != "colon":
                raise MutableRecordError("UNSAFE_LARGE_RECORD", "unexpected colon")
            self.frames[-1]["state"] = "value"
            return None, None
        if token == b",":
            if not self.frames or self.frames[-1]["state"] != "comma_or_end":
                raise MutableRecordError("UNSAFE_LARGE_RECORD", "unexpected comma")
            self.frames[-1]["state"] = (
                "key_or_end" if self.frames[-1]["kind"] == "object" else "value_or_end"
            )
            return None, None
        value = json.loads(raw)
        if (
            token == b'"'
            and self.frames
            and self.frames[-1]["kind"] == "object"
            and self.frames[-1]["state"] == "key_or_end"
        ):
            frame = self.frames[-1]
            frame["key"] = value
            frame["state"] = "colon"
            return None, None
        return self._consume_value(), value

    def finish(self) -> None:
        if self.frames or not self.root_consumed:
            raise MutableRecordError("UNSAFE_LARGE_RECORD", "incomplete JSON record")


def _replacement(value: str, replacements: Mapping[str, str]) -> str | None:
    for source in sorted(replacements, key=len, reverse=True):
        if value == source or value.startswith(source + os.sep):
            return replacements[source] + value[len(source) :]
    return None


def _large_source_prefix_pattern(replacements: Mapping[str, str]) -> re.Pattern[bytes]:
    alternatives = sorted(
        (
            re.escape(json.dumps(source, ensure_ascii=True)[1:-1].encode("ascii"))
            for source in replacements
        ),
        key=len,
        reverse=True,
    )
    return re.compile(b'"(?:' + b"|".join(alternatives) + b")")


def _plan_large_indented_json_patch(
    path: Path,
    replacements: Mapping[str, str],
    *,
    policy: MutableBackupPolicy,
    expected_pointers: Sequence[str],
) -> StructuredPatchPlan:
    """Fast bounded scanner for records written by the indented JSON contract.

    Each pointer combines the mutated field name with its exact byte offset.
    The locator is unambiguous under the required preimage hash without walking
    millions of unrelated scalar array elements.
    """

    source_prefix = _large_source_prefix_pattern(replacements)
    preimage = hashlib.sha256()
    postimage = hashlib.sha256()
    provisional: list[tuple[str, int, int, bytes, bytes, str, str]] = []
    patch_bytes = 0
    changed_pointers: set[str] = set()
    schema_version: int | None = None
    carry = b""
    absolute = 0
    first = True
    tail = b""

    def process(data: bytes, base: int) -> None:
        nonlocal schema_version, first, patch_bytes
        if first:
            if not data.startswith(b"{\n"):
                raise MutableRecordError(
                    "UNSAFE_LARGE_RECORD", f"record is not indented JSON: {path}"
                )
            first = False
        local_patches: list[tuple[int, int, bytes]] = []
        for match in source_prefix.finditer(data):
            start = match.start()
            token = _TOKEN.match(data, start)
            if token is None or token.group(0)[:1] != b'"':
                raise MutableRecordError(
                    "UNSAFE_LARGE_RECORD", f"unsupported path token near byte {base + start}"
                )
            raw = token.group(0)
            value = json.loads(raw)
            if not isinstance(value, str):
                raise MutableRecordError("UNSAFE_LARGE_RECORD", str(path))
            updated = _replacement(value, replacements)
            if updated is None:
                continue
            line_start = data.rfind(b"\n", 0, start) + 1
            indent = len(data[line_start:start]) - len(data[line_start:start].lstrip(b" "))
            stripped_start = line_start + indent
            key_token = _TOKEN.match(data, stripped_start)
            if key_token is not None and key_token.end() < start:
                between = data[key_token.end():start]
                if not re.fullmatch(rb"\s*:\s*", between):
                    raise MutableRecordError(
                        "UNSAFE_LARGE_RECORD", f"path is not a complete JSON field near byte {base + start}"
                    )
                key = str(json.loads(key_token.group(0)))
                pointer = _pointer(("@byte", base + start, key))
            elif stripped_start == start:
                pointer = _pointer(("@byte", base + start))
            else:
                raise MutableRecordError(
                    "UNSAFE_LARGE_RECORD", f"path is not a canonical JSON value near byte {base + start}"
                )
            line_end = data.find(b"\n", token.end())
            if line_end < 0 or not re.fullmatch(rb"\s*,?\s*", data[token.end():line_end]):
                raise MutableRecordError(
                    "UNSAFE_LARGE_RECORD", f"path token has trailing content near byte {base + start}"
                )
            if pointer in changed_pointers:
                raise MutableRecordError(
                    "AMBIGUOUS_STRUCTURED_TARGET", f"duplicate mutable pointer: {pointer}"
                )
            changed_pointers.add(pointer)
            if pointer == "/schema_version":
                schema_version = int(value)
            new_raw = json.dumps(updated, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            provisional.append(
                (pointer, base + start, base + token.end(), raw, new_raw, value, updated)
            )
            patch_bytes += len(pointer.encode("utf-8")) + len(raw) + len(new_raw)
            if patch_bytes > policy.structured_patch_max_backup_bytes:
                raise MutableRecordError(
                    "UNSAFE_LARGE_RECORD", "structured patch bytes exceed policy"
                )
            local_patches.append((start, token.end(), new_raw))
            if len(provisional) > policy.structured_patch_max_changed_fields:
                raise MutableRecordError(
                    "UNSAFE_LARGE_RECORD", "structured changed-field count exceeds policy"
                )
        output_cursor = 0
        for start, end, replacement in local_patches:
            postimage.update(data[output_cursor:start])
            postimage.update(replacement)
            output_cursor = end
        postimage.update(data[output_cursor:])

    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            preimage.update(chunk)
            data = carry + chunk
            last_newline = data.rfind(b"\n")
            if last_newline < 0:
                if len(data) > policy.structured_patch_max_line_bytes:
                    raise MutableRecordError("UNSAFE_LARGE_RECORD", "JSON line exceeds policy")
                carry = data
                absolute += len(chunk)
                continue
            complete = data[: last_newline + 1]
            base = absolute - len(carry)
            process(complete, base)
            carry = data[last_newline + 1 :]
            if len(carry) > policy.structured_patch_max_line_bytes:
                raise MutableRecordError("UNSAFE_LARGE_RECORD", "JSON line exceeds policy")
            tail = complete[-64:]
            absolute += len(chunk)
    if carry:
        process(carry, absolute - len(carry))
        tail = carry[-64:]
    if not tail.rstrip().endswith(b"}"):
        raise MutableRecordError("UNSAFE_LARGE_RECORD", "incomplete indented JSON record")
    with path.open("rb") as handle:
        schema_prefix = handle.read(1024 * 1024)
    schema_match = re.search(
        rb'(?m)^  "schema_version": ([0-9]+),?\r?$', schema_prefix
    )
    if schema_match is not None:
        schema_version = int(schema_match.group(1))
    if schema_version not in policy.supported_record_schema_versions:
        raise MutableRecordError(
            "MUTABLE_RECORD_SCHEMA_MISMATCH",
            f"unsupported or absent schema_version in {path}: {schema_version!r}",
        )
    missing = sorted(set(expected_pointers) - changed_pointers)
    if missing:
        raise MutableRecordError("MUTABLE_RECORD_EXPECTED_FIELD_ABSENT", ", ".join(missing))
    if not provisional:
        raise MutableRecordError("MUTABLE_RECORD_EXPECTED_FIELD_ABSENT", str(path))
    return _finalize_patch_plan(
        schema_version,
        LARGE_STRUCTURED_MUTATION_IMPLEMENTATION,
        preimage.hexdigest(),
        postimage.hexdigest(),
        absolute,
        provisional,
    )


def _finalize_patch_plan(
    schema_version: int,
    implementation: str,
    preimage_sha256: str,
    postimage_sha256: str,
    source_size_bytes: int,
    provisional: Sequence[tuple[str, int, int, bytes, bytes, str, str]],
) -> StructuredPatchPlan:
    patches: list[StructuredFieldPatch] = []
    delta = 0
    for pointer, start, end, old_raw, new_raw, old_value, new_value in provisional:
        post_start = start + delta
        post_end = post_start + len(new_raw)
        patches.append(
            StructuredFieldPatch(
                pointer=pointer,
                preimage_start=start,
                preimage_end=end,
                postimage_start=post_start,
                postimage_end=post_end,
                old_json=old_raw.decode("utf-8"),
                new_json=new_raw.decode("ascii"),
                old_value=old_value,
                new_value=new_value,
            )
        )
        delta += len(new_raw) - len(old_raw)
    patch_payload = {
        "record_schema_version": schema_version,
        "mutation_implementation": implementation,
        "preimage_sha256": preimage_sha256,
        "postimage_sha256": postimage_sha256,
        "changes": [item.to_dict() for item in patches],
    }
    return StructuredPatchPlan(
        record_schema_version=schema_version,
        mutation_implementation=implementation,
        preimage_sha256=preimage_sha256,
        postimage_sha256=postimage_sha256,
        source_size_bytes=source_size_bytes,
        patches=tuple(patches),
        patch_sha256=stable_hash(patch_payload),
        projected_backup_bytes=len(
            json.dumps(patch_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ),
    )


def plan_structured_json_patch(
    path: Path,
    replacements: Mapping[str, str],
    *,
    policy: MutableBackupPolicy,
    expected_pointers: Sequence[str] = (),
) -> StructuredPatchPlan:
    """Plan an exact token-offset patch with bounded memory and strict grammar."""

    if path.stat().st_size >= LARGE_STRUCTURED_SCANNER_THRESHOLD_BYTES:
        return _plan_large_indented_json_patch(
            path,
            replacements,
            policy=policy,
            expected_pointers=expected_pointers,
        )

    preimage = hashlib.sha256()
    postimage = hashlib.sha256()
    state = _JsonPathState()
    provisional: list[tuple[str, int, int, bytes, bytes, str, str]] = []
    patch_bytes = 0
    schema_version: object | None = None
    absolute = 0
    with path.open("rb") as handle:
        while True:
            line = handle.readline(policy.structured_patch_max_line_bytes + 1)
            if not line:
                break
            if len(line) > policy.structured_patch_max_line_bytes:
                raise MutableRecordError(
                    "UNSAFE_LARGE_RECORD",
                    f"JSON line exceeds bounded parser policy: {path}",
                )
            preimage.update(line)
            local_patches: list[tuple[int, int, bytes]] = []
            cursor = 0
            for match in _TOKEN.finditer(line):
                if not _WHITESPACE.fullmatch(line[cursor : match.start()]):
                    raise MutableRecordError("UNSAFE_LARGE_RECORD", f"unsupported JSON token in {path}")
                raw = match.group(0)
                value_path, value = state.consume(raw)
                if value_path is not None:
                    pointer = _pointer(value_path)
                    if pointer == "/schema_version":
                        schema_version = value
                    if isinstance(value, str):
                        updated = _replacement(value, replacements)
                        if updated is not None:
                            if pointer in state.changed_pointers:
                                raise MutableRecordError(
                                    "AMBIGUOUS_STRUCTURED_TARGET",
                                    f"duplicate mutable pointer: {pointer}",
                                )
                            state.changed_pointers.add(pointer)
                            new_raw = json.dumps(
                                updated, ensure_ascii=True, separators=(",", ":")
                            ).encode("ascii")
                            provisional.append(
                                (
                                    pointer,
                                    absolute + match.start(),
                                    absolute + match.end(),
                                    raw,
                                    new_raw,
                                    value,
                                    updated,
                                )
                            )
                            patch_bytes += len(pointer.encode("utf-8")) + len(raw) + len(new_raw)
                            if patch_bytes > policy.structured_patch_max_backup_bytes:
                                raise MutableRecordError(
                                    "UNSAFE_LARGE_RECORD",
                                    "structured patch bytes exceed policy",
                                )
                            local_patches.append((match.start(), match.end(), new_raw))
                            if len(provisional) > policy.structured_patch_max_changed_fields:
                                raise MutableRecordError(
                                    "UNSAFE_LARGE_RECORD",
                                    "structured changed-field count exceeds policy",
                                )
                cursor = match.end()
            if not _WHITESPACE.fullmatch(line[cursor:]):
                raise MutableRecordError("UNSAFE_LARGE_RECORD", f"unsupported JSON bytes in {path}")
            output_cursor = 0
            for start, end, replacement in local_patches:
                postimage.update(line[output_cursor:start])
                postimage.update(replacement)
                output_cursor = end
            postimage.update(line[output_cursor:])
            absolute += len(line)
    state.finish()
    if not isinstance(schema_version, int) or schema_version not in policy.supported_record_schema_versions:
        raise MutableRecordError(
            "MUTABLE_RECORD_SCHEMA_MISMATCH",
            f"unsupported or absent schema_version in {path}: {schema_version!r}",
        )
    missing = sorted(set(expected_pointers) - state.changed_pointers)
    if missing:
        raise MutableRecordError(
            "MUTABLE_RECORD_EXPECTED_FIELD_ABSENT",
            ", ".join(missing),
        )
    if not provisional:
        raise MutableRecordError("MUTABLE_RECORD_EXPECTED_FIELD_ABSENT", str(path))
    return _finalize_patch_plan(
        schema_version,
        STRUCTURED_MUTATION_IMPLEMENTATION,
        preimage.hexdigest(),
        postimage.hexdigest(),
        absolute,
        provisional,
    )


def apply_structured_json_patch(
    path: Path,
    patches: Sequence[StructuredFieldPatch],
    *,
    reverse: bool = False,
    expected_source_sha256: str,
    expected_result_sha256: str,
) -> None:
    """Atomically apply a forward or inverse patch after exact hash guarding."""

    actual = file_hash(path)
    if actual != expected_source_sha256:
        code = "MUTABLE_RECORD_POSTIMAGE_MISMATCH" if reverse else "MUTABLE_RECORD_PREIMAGE_MISMATCH"
        raise MutableRecordError(code, f"unexpected mutable record hash: {path}")
    ordered = sorted(
        patches,
        key=lambda item: item.postimage_start if reverse else item.preimage_start,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".metadata-patch.tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    result_hash = hashlib.sha256()

    def write(destination, value: bytes) -> None:
        destination.write(value)
        result_hash.update(value)

    try:
        with path.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
            cursor = 0
            for item in ordered:
                start = item.postimage_start if reverse else item.preimage_start
                end = item.postimage_end if reverse else item.preimage_end
                expected = (item.new_json if reverse else item.old_json).encode("utf-8")
                replacement = (item.old_json if reverse else item.new_json).encode("utf-8")
                remaining = start - cursor
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise MutableRecordError("STRUCTURED_PATCH_MISMATCH", str(path))
                    write(destination, chunk)
                    remaining -= len(chunk)
                observed = source.read(end - start)
                if observed != expected:
                    raise MutableRecordError(
                        "STRUCTURED_PATCH_MISMATCH", f"token differs at {item.pointer}"
                    )
                write(destination, replacement)
                cursor = end
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                write(destination, chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if result_hash.hexdigest() != expected_result_sha256:
            raise MutableRecordError(
                "STRUCTURED_PATCH_RESULT_MISMATCH", f"result hash differs: {path}"
            )
        shutil.copystat(path, temporary, follow_symlinks=False)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "DEFAULT_FULL_COPY_MAX_BYTES",
    "DEFAULT_STRUCTURED_PATCH_MAX_CHANGED_FIELDS",
    "DEFAULT_STRUCTURED_PATCH_MAX_LINE_BYTES",
    "DEFAULT_STRUCTURED_PATCH_MAX_BACKUP_BYTES",
    "MutableBackupPolicy",
    "MutableBackupStrategy",
    "MutableRecordError",
    "STRUCTURED_MUTATION_IMPLEMENTATION",
    "StructuredFieldPatch",
    "StructuredPatchPlan",
    "apply_structured_json_patch",
    "plan_structured_json_patch",
]
