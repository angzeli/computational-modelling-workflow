"""Bounded owner-node successor preparation responses; never execution authority."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import re

from .store import JobsError


class SuccessorUnverifiable(JobsError):
    """Required successor evidence could not be observed; retry the same request."""

    code = 'SUCCESSOR_UNVERIFIABLE'


@dataclass(frozen=True)
class SuccessorResult:
    schema: int
    node_id: str
    queue_id: str
    job_id: int
    attempt_id: str
    operation: str
    status: str
    plan_digest: str
    payload_json: str

    def __post_init__(self):
        if type(self.schema) is not int or self.schema != 1:
            raise ValueError('Unsupported successor response schema')
        from cmw.nodes.protocol import identifier
        identifier(self.node_id, 'node_id'); identifier(self.queue_id, 'queue_id')
        if type(self.job_id) is not int or self.job_id < 1 or not re.fullmatch('[a-f0-9]{32}', self.attempt_id):
            raise ValueError('Invalid successor parent attempt')
        states = {'plan': {'READY', 'BLOCKED', 'UNVERIFIABLE'},
                  'materialize': {'MATERIALIZED', 'PENDING_VERIFICATION'}, 'register': {'REGISTERED'}}
        if self.operation not in states or self.status not in states[self.operation]:
            raise ValueError('Unsupported successor operation/status')
        if not re.fullmatch('[a-f0-9]{64}', self.plan_digest):
            raise ValueError('Invalid successor plan digest')
        if not isinstance(self.payload_json, str) or len(self.payload_json.encode()) > 128 * 1024:
            raise ValueError('Successor response exceeds 128 KiB')
        value = json.loads(self.payload_json, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
        if type(value) is not dict:
            raise ValueError('Successor payload must be an object')

    @property
    def retry_code(self):
        if self.status == 'UNVERIFIABLE':
            return SuccessorUnverifiable.code
        if self.status == 'PENDING_VERIFICATION':
            return 'SUCCESSOR_PENDING_VERIFICATION'
        return None

    def to_dict(self):
        value = asdict(self)
        value['payload'] = json.loads(value.pop('payload_json'))
        return value
