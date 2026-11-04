"""Pure, unwired D19/D20 checks over existing parsed VASP evidence.

This module performs no reads, input rewrites, submission or runtime discovery.
It does not replace the existing submission, input checker or POTCAR identities.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

from .protocol import NodeError, bounded_value, decode, encode


def _integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise NodeError('INVALID_PARAMS', f'{name} must be an integer >= {minimum}')
    return value


def _fields(value, names, name):
    if type(value) is not dict or set(value) != set(names):
        raise NodeError('INVALID_PARAMS', f'Unexpected {name} fields')
    bounded_value(value)


@dataclass(frozen=True)
class CoreTier:
    level: int
    name: str
    physical_cores: int

    def __post_init__(self):
        _integer(self.level, 'Core tier level', 0)
        _integer(self.physical_cores, 'Core tier physical cores')
        if type(self.name) is not str or not self.name:
            raise NodeError('INVALID_PARAMS', 'Core tier name must be nonempty native text')
        bounded_value(self.name)

    @classmethod
    def from_dict(cls, value):
        _fields(value, ('level', 'name', 'physical_cores'), 'core tier')
        return cls(**value)


@dataclass(frozen=True)
class Hardware:
    physical_cores: int
    core_tiers: tuple[CoreTier, ...]
    recommended_max_parallelism: int | None

    def __post_init__(self):
        _integer(self.physical_cores, 'Physical cores')
        if type(self.core_tiers) is not tuple or any(not isinstance(tier, CoreTier) for tier in self.core_tiers):
            raise NodeError('INVALID_PARAMS', 'Core tiers must be explicit native facts')
        if self.recommended_max_parallelism is not None:
            _integer(self.recommended_max_parallelism, 'Recommended maximum parallelism')

    @classmethod
    def from_dict(cls, value):
        _fields(value, ('physical_cores', 'core_tiers', 'recommended_max_parallelism'), 'hardware')
        if type(value['core_tiers']) is not list:
            raise NodeError('INVALID_PARAMS', 'Core tiers must be an array')
        return cls(value['physical_cores'], tuple(CoreTier.from_dict(tier) for tier in value['core_tiers']),
                   value['recommended_max_parallelism'])

    def to_dict(self):
        return {'physical_cores': self.physical_cores, 'core_tiers': [asdict(tier) for tier in self.core_tiers],
                'recommended_max_parallelism': self.recommended_max_parallelism}

    @classmethod
    def from_json(cls, frame: bytes):
        """Use the existing bounded, duplicate-rejecting JSON-line decoder."""
        return cls.from_dict(decode(frame))

    def to_json(self) -> bytes:
        return encode(self.to_dict())


@dataclass(frozen=True)
class PlanFinding:
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class VaspPlan:
    effective_kpar: int | None
    split_tag: str | None
    split_value: int | None
    findings: tuple[PlanFinding, ...]
    unassessed: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.unassessed

    @property
    def permitted(self) -> bool:
        return self.complete and not any(item.severity == 'ERROR' for item in self.findings)

    def require_submission(self) -> None:
        if not self.permitted:
            message = 'Submission requires a complete plan without validation errors'
            errors = [item.message for item in self.findings if item.severity == 'ERROR']
            if errors:
                message += ': ' + '; '.join(errors)
            raise NodeError('INVALID_SUBMISSION', message)


def _species(value, name):
    if value is None:
        return None
    if (not isinstance(value, (tuple, list)) or not value
            or any(type(item) is not str or not item for item in value)):
        raise NodeError('INVALID_PARAMS', f'{name} must be ordered parsed species evidence')
    return tuple(value)


def plan_vasp(parsed_incar: dict | None, *, poscar_species: Sequence[str] | None,
              potcar_species: Sequence[str] | None, mpi_ranks: int | None,
              threads_per_rank: int | None, hardware: Hardware | dict | None) -> VaspPlan:
    """Consume cmw.periodic.vasp.inputs.parse_incar's result without reparsing.

    None or an unsupported/invalid parser result is unavailable INCAR evidence.
    Only a successfully parsed document may supply absent-tag defaults. NPAR
    is rejected for every submission; its NCORE equivalent is diagnostic only.
    This plan checks declarations, not actual runtime inputs or content hashes.
    """
    findings, unassessed = [], []
    def issue(code, severity, message):
        findings.append(PlanFinding(code, severity, message))
    valid_resources = True
    for name, value in (('MPI ranks', mpi_ranks), ('Threads per rank', threads_per_rank)):
        if value is None:
            unassessed.append(name)
            valid_resources = False
        elif type(value) is not int or value <= 0:
            issue('INVALID_RESOURCES', 'ERROR', f'{name} must be a positive integer')
            valid_resources = False
    if hardware is None:
        unassessed.append('Target hardware')
    else:
        hardware = hardware if isinstance(hardware, Hardware) else Hardware.from_dict(hardware)
        if valid_resources:
            demand = mpi_ranks * threads_per_rank
            if demand > hardware.physical_cores:
                issue('PHYSICAL_CORES_EXCEEDED', 'ERROR', 'Rank/thread product exceeds target physical cores')
            if hardware.recommended_max_parallelism is not None and demand > hardware.recommended_max_parallelism:
                issue('RECOMMENDED_PARALLELISM_EXCEEDED', 'WARNING', 'Rank/thread product exceeds target policy recommendation')
    settings = None
    has_npar = False
    if parsed_incar is not None:
        bounded_value(parsed_incar)
        if (type(parsed_incar) is not dict or type(parsed_incar.get('settings')) is not dict
                or type(parsed_incar.get('findings')) is not list
                or parsed_incar.get('status') not in ('valid', 'invalid', 'unsupported')
                or any(type(item) is not dict or type(item.get('severity')) is not str
                       for item in parsed_incar['findings'])
                or any(not key or key != key.upper() for key in parsed_incar['settings'])):
            raise NodeError('INVALID_PARAMS', 'Expected the existing INCAR parser result')
        has_npar = ('NPAR' in parsed_incar['settings']
                    or any(type(item.get('evidence')) is dict and item['evidence'].get('tag') == 'NPAR'
                           for item in parsed_incar['findings']))
        failures = {item['severity'] for item in parsed_incar['findings']} & {'error', 'unsupported'}
        if parsed_incar['status'] == 'valid' and not failures:
            settings = parsed_incar['settings']
        else:
            issue('INCAR_UNAVAILABLE', 'ERROR', 'INCAR parser did not establish supported settings')
    kpar = split = split_tag = None
    if settings is None:
        unassessed.append('INCAR')
    else:
        kpar = settings.get('KPAR', 1)
        valid_kpar = type(kpar) is int and kpar > 0
        valid_split = False
        if not valid_kpar:
            issue('INVALID_KPAR', 'ERROR', 'Effective KPAR must be a positive integer')
        if not has_npar:
            split_tag, split = 'NCORE', settings.get('NCORE', 1)
            valid_split = type(split) is int and split > 0
            if not valid_split:
                issue('INVALID_NCORE', 'ERROR', 'Effective NCORE must be a positive integer')
        if type(mpi_ranks) is int and mpi_ranks > 0 and valid_kpar:
            if mpi_ranks % kpar:
                issue('KPAR_INCOMPATIBLE', 'ERROR', 'KPAR must divide MPI ranks')
            elif valid_split and (mpi_ranks // kpar) % split:
                issue(split_tag + '_INCOMPATIBLE', 'WARNING', split_tag + ' does not divide MPI ranks per KPAR group')
        if not valid_kpar:
            kpar = None
        if not valid_split:
            split = None
    if has_npar:
        message = 'NPAR is rejected for all local and remote submissions. '
        npar = settings.get('NPAR') if settings is not None else None
        if all(type(value) is int and value > 0 for value in (mpi_ranks, kpar, npar)):
            equivalent, remainder = divmod(mpi_ranks, kpar * npar)
            if remainder:
                message += (f'No integer equivalent NCORE exists: '
                            f'{mpi_ranks} / ({kpar} * {npar}) is not an integer.')
            else:
                message += f'Equivalent NCORE = {equivalent} ({mpi_ranks} / ({kpar} * {npar})).'
        else:
            message += ('Equivalent NCORE cannot be determined without positive integer MPI ranks, '
                        'KPAR and NPAR from supported parsed evidence.')
        issue('NPAR_REJECTED', 'ERROR', message)
    poscar = _species(poscar_species, 'POSCAR')
    potcar = _species(potcar_species, 'POTCAR')
    if poscar is None or potcar is None:
        unassessed.append('POSCAR/POTCAR species order')
    elif poscar != potcar:
        issue('SPECIES_MISMATCH', 'ERROR', 'POSCAR species order differs from POTCAR dataset order')
    return VaspPlan(kpar, split_tag, split, tuple(findings), tuple(unassessed))
