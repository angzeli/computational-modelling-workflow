"""Versioned, bounded result-review observations; never execution authority."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import math
import re

DOMAIN_NAMES = ('operational', 'source_binding', 'electronic', 'ionic_analysis', 'endpoint', 'policy', 'finalization')
STATUSES = {'PASS', 'FAIL', 'UNKNOWN', 'UNSUPPORTED', 'NOT ASSESSED', 'N/A'}

@dataclass(frozen=True)
class ReviewDomain:
    name: str
    status: str
    summary: str

    def __post_init__(self):
        if self.name not in DOMAIN_NAMES or self.status not in STATUSES or len(self.summary) > 4096:
            raise ValueError('Unsupported review domain or status')

@dataclass(frozen=True)
class ReviewRecord:
    schema: int
    node_id: str | None
    queue_id: str | None
    job_id: int
    attempt_id: str
    review_id: str
    status: str
    engine: str
    observed_at: float
    domains: tuple[ReviewDomain, ...]
    coverage: str
    max_bytes: int
    timeout_seconds: int
    sources_json: str
    evidence_json: str
    provenance_json: str
    message: str

    def __post_init__(self):
        if type(self.schema) is not int or self.schema != 1:
            raise ValueError('Unsupported review schema')
        if type(self.job_id) is not int or self.job_id < 1 or not re.fullmatch('[0-9a-f]{32}', self.attempt_id):
            raise ValueError('Invalid review attempt')
        if self.status not in {'PREVIEW','PENDING','COMPLETE','CACHED','STALE','CANCELLED','LIMIT','ERROR'}:
            raise ValueError('Unknown review task state')
        if not math.isfinite(self.observed_at) or self.observed_at < 0:
            raise ValueError('Invalid review observation time')
        if tuple(d.name for d in self.domains) != DOMAIN_NAMES:
            raise ValueError('Review domains must be complete and ordered')
        if not 1 <= self.max_bytes <= 2**30 or not 1 <= self.timeout_seconds <= 120:
            raise ValueError('Invalid review limits')
        for value in (self.sources_json, self.evidence_json, self.provenance_json):
            if len(value.encode()) > 128*1024:
                raise ValueError('Review evidence exceeds bounded wire limit')
            json.loads(value, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
        if not isinstance(json.loads(self.sources_json), list):
            raise ValueError('Review sources must be an array')

    def to_dict(self):
        value = asdict(self)
        for name in ('sources', 'evidence', 'provenance'):
            value[name] = json.loads(value.pop(name + '_json'))
        return value


def review_text(record):
    lines = [f'RESULT REVIEW · {record.engine} · J{record.job_id} · {record.status}',
             f'Attempt: {record.attempt_id}',
             f'Owner: {record.node_id or "local"} / {record.queue_id or "unenrolled queue"}',
             f'Coverage: {record.coverage}',
             f'Limit: {record.max_bytes // 1024} KiB per source; {record.timeout_seconds}s observer deadline',
             record.message, '']
    lines.extend(f'{d.name.replace("_", " ").upper()}: {d.status} — {d.summary}' for d in record.domains)
    lines.extend(['', 'No overall scientific PASS. Finalization is a separate explicit operation.',
                  'Cached observations require a fresh inspection before reuse or export.'])
    return '\n'.join(lines)


def cached_review(record):
    """Read-free historical projection for Details and Copy, never fresh acceptance."""
    from dataclasses import replace
    if record.status in {'COMPLETE','CACHED'}:
        return replace(record,status='CACHED',
            domains=tuple(replace(d,status='UNKNOWN') if d.status=='PASS' else d for d in record.domains),
            message='Historical cached evidence; hashes not reverified. Rerun before reuse.')
    return record
