"""Small immutable contracts for bounded, engine-neutral live observation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class SourceDescriptor:
    role: str
    path: str
    qualification: str = 'declared-source'
    optional: bool = False
    root_path: str | None = None
    root_identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class Criterion:
    value: float
    unit: str
    comparator: str
    origin: str
    kind: str = 'engine-stopping'
    definition: str = ''
    signed_value: float | None = None


@dataclass(frozen=True)
class DiagnosticTarget:
    semantic_id: str
    criterion: Criterion


@dataclass(frozen=True)
class RunContext:
    binding_id: str
    engine: str
    sources: tuple[SourceDescriptor, ...] = ()
    stage: str | None = None
    attempt_id: str | None = None
    jobs_state: str | None = None
    job_id: int | None = None
    association: str = 'explicit-caller-declaration'
    binding_status: str = 'BOUND'
    limitations: tuple[str, ...] = ()
    diagnostic_targets: tuple[DiagnosticTarget, ...] = ()
    process_ownership: bool = False

    @property
    def key(self):
        return self.binding_id


@dataclass(frozen=True)
class IterationLevel:
    kind: str
    index: int | str
    label: str
    budget: int | None = None


@dataclass(frozen=True)
class Evidence:
    source_role: str
    path: str
    generation: int
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    file_identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class SourceLine:
    text: str
    evidence: Evidence
    observed_at: float


@dataclass(frozen=True)
class SourceState:
    role: str
    path: str
    generation: int = 0
    health: str = 'PENDING'
    offset: int = 0
    pending_bytes: int = 0
    has_more: bool = False
    last_success_at: float | None = None
    last_growth_at: float | None = None
    last_change_at: float | None = None
    bytes_read: int = 0
    total_bytes_read: int = 0
    limitations: tuple[str, ...] = ()
    identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class SourceUpdate:
    source: SourceDescriptor
    state: SourceState
    lines: tuple[SourceLine, ...] = ()
    reset: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class Metric:
    semantic_id: str
    label: str
    definition: str
    unit: str
    value: float | None
    context: tuple[IterationLevel, ...] = ()
    stage: str | None = None
    segment: int = 1
    precision: float | None = None
    printed: str | None = None
    signed_value: float | None = None
    qualification: str = 'observed'
    evidence: tuple[Evidence, ...] = ()
    observed_at: float | None = None
    criterion: Criterion | None = None
    diagnostic_targets: tuple[Criterion, ...] = ()
    relation: str | None = None
    priority: int = 100
    comparison_context: tuple[IterationLevel, ...] | None = None
    comparison_scope: str = ''


def comparable(left: Metric, right: Metric) -> bool:
    """Only compare an adapter-declared scope across compatible evidence."""
    fields = ('semantic_id', 'definition', 'unit', 'stage', 'segment', 'criterion',
              'diagnostic_targets', 'comparison_scope')
    def sources(metric):
        return {(item.source_role, item.path, item.generation, item.file_identity)
                for item in metric.evidence}
    return (all(getattr(left, key) == getattr(right, key) for key in fields)
            and sources(left) == sources(right)
            and (left.context if left.comparison_context is None else left.comparison_context)
            == (right.context if right.comparison_context is None else right.comparison_context))


@dataclass(frozen=True)
class MetricSeries:
    latest: Metric
    previous: Metric | None = None

    def __post_init__(self):
        if self.previous is not None and not comparable(self.latest, self.previous):
            raise ValueError('Latest and previous metric observations are not comparable')


@dataclass(frozen=True)
class ProgressEvent:
    event_id: str
    kind: str
    stage: str | None
    segment: int
    context: tuple[IterationLevel, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    message: str = ''


@dataclass(frozen=True)
class ProgressSnapshot:
    context: RunContext
    adapter: str
    status: str = 'PENDING'
    phase: str = 'Awaiting evidence'
    calculation: str | None = None
    segment: int = 1
    iteration: tuple[IterationLevel, ...] = ()
    metrics: tuple[MetricSeries, ...] = ()
    events: tuple[ProgressEvent, ...] = ()
    completeness: str = 'PENDING'
    read_health: str = 'PENDING'
    sources: tuple[SourceState, ...] = ()
    last_success_at: float | None = None
    last_source_change_at: float | None = None
    last_file_growth_at: float | None = None
    last_new_evidence_at: float | None = None
    messages: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    scientific_validation: str = 'not-assessed'

    def to_dict(self):
        return {'schema': 1, **asdict(self)}


class ProgressAdapter(Protocol):
    def update(self, updates: tuple[SourceUpdate, ...], now: float) -> ProgressSnapshot: ...
