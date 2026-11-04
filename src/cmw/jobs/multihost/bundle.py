"""Pure bundle/header/plan decisions; no extraction, staging or engine reads."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Iterable, Sequence

from .models import ContractError, HelloResult, Manifest

DEFAULT_MAX_BUNDLE_BYTES = 64 * 1024**2


class BundleError(ContractError):
    def __init__(self, message):
        super().__init__(message, code='validation_failed')


def _manifest(value):
    return Manifest.from_dict(value.to_dict() if isinstance(value, Manifest) else value).to_dict()


def _size(value, name, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise BundleError(f'{name} must be a {"positive" if positive else "nonnegative"} integer')
    return value


def validate_manifest(value: Manifest | dict, *, manifest_size: int,
                      max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES) -> Manifest:
    """Validate the four-file declaration and total *payload* bytes.

    The caller supplies the actual UTF-8 manifest size, not a reserialization's
    estimate. Tar framing overhead is deliberately outside this Phase 0 count.
    """
    parsed = Manifest.from_dict(value.to_dict() if isinstance(value, Manifest) else value)
    data = parsed.to_dict()
    _size(manifest_size, 'Manifest size', positive=True)
    _size(max_bundle_bytes, 'Bundle limit', positive=True)
    if manifest_size + sum(item['size'] for item in data['files']) > max_bundle_bytes:
        raise BundleError('Bundle payload exceeds the advertised size limit')
    return parsed


@dataclass(frozen=True)
class TarMemberHeader:
    name: str
    size: int
    typeflag: bytes = b'0'
    linkname: str = ''
    sparse: bool = False


def _safe_header(header: TarMemberHeader):
    name = header.name
    if (not isinstance(name, str) or not name or name.startswith('/') or '\\' in name
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise BundleError('Tar member has an unsafe or noncanonical relative path')
    if header.typeflag not in (b'0', b'\0') or header.linkname or header.sparse:
        raise BundleError('Only ordinary regular tar members are permitted')
    _size(header.size, 'Tar member size')


def validate_tar_headers(manifest: Manifest | dict, headers: Iterable[TarMemberHeader], *,
                         manifest_size: int,
                         max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES) -> None:
    """Validate a complete header sequence, including missing/truncated members.

    Header type flags are raw tar flags, so links, devices, directories, PAX/GNU
    extension members and sparse files cannot hide behind extraction filters.
    """
    parsed = validate_manifest(manifest, manifest_size=manifest_size, max_bundle_bytes=max_bundle_bytes)
    expected = {item['path']: item['size'] for item in parsed.to_dict()['files']}
    seen, total = set(), 0
    for index, header in enumerate(headers):
        if not isinstance(header, TarMemberHeader):
            raise BundleError('Expected a parsed tar member header')
        _safe_header(header)
        if index == 0 and header.name != 'manifest.json':
            raise BundleError('manifest.json must be the first tar member')
        if header.name in seen:
            raise BundleError('Duplicate tar member path')
        seen.add(header.name)
        if index == 0:
            expected_size = manifest_size
        elif header.name not in expected:
            raise BundleError('Tar member is not listed in the manifest')
        else:
            expected_size = expected[header.name]
        if header.size != expected_size:
            raise BundleError('Tar member size differs from the manifest')
        total += header.size
        if total > max_bundle_bytes:
            raise BundleError('Bundle payload exceeds the advertised size limit')
    if seen != {'manifest.json', *expected}:
        raise BundleError('Bundle is incomplete: a required member is missing')


def validate_file_payload(manifest: Manifest | dict, path: str, payload: bytes) -> None:
    """Validate caller-supplied bytes without reading files or exposing content."""
    data = _manifest(manifest)
    expected = next((item for item in data['files'] if item['path'] == path), None)
    if expected is None:
        raise BundleError('Payload path is not listed in the manifest')
    if not isinstance(payload, bytes):
        raise BundleError('Payload must be bytes')
    if len(payload) != expected['size']:
        raise BundleError('Payload is truncated or has a different declared size')
    if hashlib.sha256(payload).hexdigest() != expected['sha256']:
        raise BundleError('Payload SHA-256 differs from the manifest')


@dataclass(frozen=True)
class PlanFinding:
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class BundlePlan:
    build_id: str | None
    findings: tuple[PlanFinding, ...]
    unassessed: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.unassessed

    @property
    def permitted(self) -> bool:
        return self.complete and not any(item.severity == 'error' for item in self.findings)


def plan_bundle(manifest: Manifest | dict, target: HelloResult | dict, *,
                kpar: int | None, ncore: int | None, manifest_size: int,
                poscar_species: Sequence[str] | None = None) -> BundlePlan:
    """Check parsed declarations; absent scientific evidence stays unassessed.

    Callers must obtain POSCAR species from the existing VASP parser. File hash
    verification belongs to receive; the plan makes no claim of checking bytes.
    """
    data = _manifest(manifest)
    hello = HelloResult.from_dict(target.to_dict() if isinstance(target, HelloResult) else target).to_dict()
    caps = hello['capabilities']
    findings, unassessed = [], []
    def issue(code, severity, message):
        findings.append(PlanFinding(code, severity, message))
    receive = caps.get('receive')
    if not caps.get('submit') or not receive:
        issue('capability_unsupported', 'error', 'Target must offer submit and receive')
    limit = receive['max_bundle_bytes'] if receive else DEFAULT_MAX_BUNDLE_BYTES
    try:
        validate_manifest(data, manifest_size=manifest_size, max_bundle_bytes=limit)
    except BundleError as exc:
        issue('bundle_size', 'error', str(exc))
    builds = caps.get('engines', {}).get('vasp', {}).get('builds', [])
    requested = data['build']['build_id']
    candidates = [build for build in builds if build['build_id'] == requested] if requested is not None else [
        build for build in builds if build['default'] and not build['experimental']]
    selected = candidates[0] if len(candidates) == 1 else None
    if selected is None:
        issue('build_unavailable', 'error', 'Exactly one selected/default production build must be available')
    else:
        if data['executable'] not in selected['executables']:
            issue('executable_unavailable', 'error', 'Requested executable is absent from the selected build')
        if selected['experimental'] and not data['build']['allow_experimental']:
            issue('experimental_build', 'error', 'Experimental build requires explicit authorization')
    ranks = data['execution']['mpi_ranks']
    if ranks * data['execution']['omp_threads'] > hello['hardware']['physical_cores']:
        issue('physical_cores', 'error', 'Declared rank/thread product exceeds target physical cores')
    recommended = hello['hardware']['recommended_max_parallelism']
    if recommended is not None and ranks * data['execution']['omp_threads'] > recommended:
        issue('recommended_max_parallelism', 'warning', 'Declared rank/thread product exceeds target policy recommendation')
    for name, value in (('KPAR', kpar), ('NCORE', ncore)):
        if value is None:
            unassessed.append(name)
        elif type(value) is not int or value <= 0:
            issue(name.lower(), 'error', f'Parsed {name} must be a positive integer')
    if type(kpar) is int and kpar > 0:
        if ranks % kpar:
            issue('kpar_divisibility', 'error', 'MPI ranks must be divisible by parsed INCAR KPAR')
        elif type(ncore) is int and ncore > 0 and (ranks // kpar) % ncore:
            issue('ncore_compatibility', 'warning', 'NCORE does not divide MPI ranks per KPAR group')
    if poscar_species is None:
        unassessed.append('POSCAR/POTCAR species order')
    elif (not isinstance(poscar_species, (list, tuple)) or not poscar_species
          or any(not isinstance(item, str) or not item for item in poscar_species)):
        raise ContractError('Parsed POSCAR species must be a nonempty ordered sequence')
    elif list(poscar_species) != [item['element'] for item in data['provenance']['potcar_identity']]:
        issue('species_order', 'error', 'POSCAR species order differs from declared POTCAR dataset order')
    return BundlePlan(None if selected is None else selected['build_id'], tuple(findings), tuple(unassessed))


class IdempotencyAction(str, Enum):
    NEW = 'new'
    REUSE = 'reuse'
    CONFLICT = 'conflict'


@dataclass(frozen=True)
class IdempotencyDecision:
    action: IdempotencyAction
    original_result: object = None
    error_code: str | None = None


def decide_idempotency(manifest: Manifest | dict, previous: Manifest | dict | None, *,
                       original_result: object = None) -> IdempotencyDecision:
    """Compare complete parsed manifests; JSON whitespace/object order is inert.

    Arrays, creation time and all manifest values remain identity-relevant.
    The caller looks up an existing staged/submitted record by request_id. This
    function neither persists a record nor grants registration/execution authority.
    """
    current = _manifest(manifest)
    if previous is None:
        return IdempotencyDecision(IdempotencyAction.NEW)
    old = _manifest(previous)
    if current['request_id'] != old['request_id']:
        return IdempotencyDecision(IdempotencyAction.NEW)
    if current == old:
        return IdempotencyDecision(IdempotencyAction.REUSE, original_result)
    return IdempotencyDecision(IdempotencyAction.CONFLICT, error_code='conflict')
