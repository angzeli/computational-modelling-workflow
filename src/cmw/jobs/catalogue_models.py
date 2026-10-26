"""Bounded human research metadata; never scientific or scheduling authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
import unicodedata

FIELDS = ('alias', 'project', 'system', 'series', 'research_stage', 'tags')
STATUSES = frozenset({'Queue', 'Hold', 'Starting', 'Run', 'Cancelling', 'Unknown',
                      'Done', 'Fail', 'Cancelled'})


def text_value(value, label, limit=256, *, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise ValueError(f'{label} must be plain text of at most {limit} Unicode code points')
    if any(unicodedata.category(character) in {'Cc', 'Cf', 'Cs'} for character in value):
        raise ValueError(f'{label} contains unsupported control characters')
    return unicodedata.normalize('NFC', value)


def tags_value(value):
    if not isinstance(value, (tuple, list)) or len(value) > 16:
        raise ValueError('Tags must contain at most 16 plain-text values')
    tags = tuple(text_value(item, 'Tag', 64, empty=False) for item in value)
    if len(set(tags)) != len(tags):
        raise ValueError('Tags must be unique')
    return tuple(sorted(tags))


def _identity(value, label):
    if value is not None and (not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value)):
        raise ValueError(f'Invalid {label} identity')


@dataclass(frozen=True)
class CatalogueQuery:
    schema: int = 1
    text: str = ''
    node_id: str | None = None
    queue_id: str | None = None
    engine: str | None = None
    status: str | None = None
    project: str | None = None
    system: str | None = None
    series: str | None = None
    research_stage: str | None = None
    tags: tuple[str, ...] = ()
    limit: int = 50
    cursor: str | None = None
    sort: str = 'id'

    def __post_init__(self):
        if type(self.schema) is not int or self.schema != 1:
            raise ValueError('Unsupported catalogue query schema')
        object.__setattr__(self, 'text', text_value(self.text, 'Query text', 512))
        for key in ('node_id', 'queue_id'):
            _identity(getattr(self, key), key)
        for key in ('engine', 'project', 'system', 'series', 'research_stage'):
            value = getattr(self, key)
            if value is not None:
                object.__setattr__(self, key, text_value(value, key))
        if self.status is not None and self.status not in STATUSES:
            raise ValueError('Unsupported operational state')
        object.__setattr__(self, 'tags', tags_value(self.tags))
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ValueError('Catalogue page size must be between 1 and 100')
        if self.cursor is not None and (not isinstance(self.cursor, str) or len(self.cursor) > 2048):
            raise ValueError('Catalogue cursor must be bounded text')
        if self.sort not in {'id', 'id_desc', 'name'}:
            raise ValueError('Unsupported catalogue view sort')

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CatalogueRecord:
    schema: int
    node_id: str | None
    queue_id: str | None
    job_id: int
    attempt_id: str
    display_id: str
    name: str
    engine: str
    status: str
    note: str
    note_revision: int
    order: int | None
    alias: str
    project: str
    system: str
    series: str
    research_stage: str
    tags: tuple[str, ...]
    revision: int
    updated_at: float | None

    def __post_init__(self):
        if type(self.schema) is not int or self.schema != 1:
            raise ValueError('Unsupported catalogue record schema')
        for key in ('node_id', 'queue_id', 'attempt_id'):
            _identity(getattr(self, key), key)
        if self.attempt_id is None or (self.node_id is None) != (self.queue_id is None):
            raise ValueError('Incomplete catalogue target identity')
        if type(self.job_id) is not int or self.job_id < 1:
            raise ValueError('Invalid internal job ID')
        for key, limit in (('display_id', 256), ('name', 4096), ('engine', 256), ('note', 4096)):
            value = getattr(self, key)
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f'Catalogue {key} exceeds the bounded projection')
        if self.status not in STATUSES:
            raise ValueError('Unsupported operational state')
        for key in ('note_revision', 'revision'):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError('Invalid catalogue revision')
        if self.order is not None and (type(self.order) is not int or self.order < 1):
            raise ValueError('Invalid queue order')
        for key in FIELDS[:-1]:
            if text_value(getattr(self, key), key) != getattr(self, key):
                raise ValueError('Catalogue metadata must be NFC normalized')
        if tags_value(self.tags) != self.tags:
            raise ValueError('Catalogue tags must be normalized and sorted')
        if self.updated_at is not None and (isinstance(self.updated_at, bool)
                or not isinstance(self.updated_at, (float, int))
                or not math.isfinite(self.updated_at) or self.updated_at < 0):
            raise ValueError('Invalid catalogue update time')

    @property
    def target(self):
        if self.node_id is None:
            return None
        return {key: getattr(self, key) for key in ('node_id', 'queue_id', 'job_id', 'attempt_id')}

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CataloguePage:
    schema: int
    node_id: str | None
    queue_id: str | None
    records: tuple[CatalogueRecord, ...]
    next_cursor: str | None
    generation: str
    total_count: int
    matching_count: int
    status_counts: tuple[tuple[str, int], ...]
    coverage: str
    observed_at: float

    def __post_init__(self):
        if type(self.schema) is not int or self.schema != 1:
            raise ValueError('Unsupported catalogue page schema')
        for key in ('node_id', 'queue_id'):
            _identity(getattr(self, key), key)
        if (self.node_id is None) != (self.queue_id is None):
            raise ValueError('Incomplete catalogue queue identity')
        if not isinstance(self.records, tuple) or len(self.records) > 100:
            raise ValueError('Catalogue page exceeds its row bound')
        if any(not isinstance(record, CatalogueRecord) or (record.node_id, record.queue_id) !=
               (self.node_id, self.queue_id) for record in self.records):
            raise ValueError('Catalogue page contains a foreign target')
        if any(type(value) is not int or value < 0 for value in (self.total_count, self.matching_count)):
            raise ValueError('Invalid catalogue counts')
        if self.matching_count > self.total_count or len(self.records) > self.matching_count:
            raise ValueError('Catalogue counts disagree')
        if (not isinstance(self.status_counts, tuple) or len(self.status_counts) > len(STATUSES)
                or any(status not in STATUSES or type(count) is not int or count < 0
                       for status, count in self.status_counts)
                or len({status for status, _ in self.status_counts}) != len(self.status_counts)
                or sum(count for _, count in self.status_counts) != self.total_count):
            raise ValueError('Invalid unfiltered operational counts')
        if self.coverage not in {'COMPLETE', 'LOCAL_UNENROLLED', 'EMPTY'}:
            raise ValueError('Unsupported queue coverage')
        if (not isinstance(self.generation, str) or len(self.generation) > 256
                or self.next_cursor is not None and
                (not isinstance(self.next_cursor, str) or len(self.next_cursor) > 2048)):
            raise ValueError('Invalid catalogue generation or cursor')
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float)) or not math.isfinite(self.observed_at):
            raise ValueError('Invalid catalogue observation time')

    def to_dict(self):
        return asdict(self)
