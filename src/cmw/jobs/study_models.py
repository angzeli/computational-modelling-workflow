"""Bounded owner-node study observations; never scientific or execution authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re


@dataclass(frozen=True)
class StudyResult:
    schema: int
    node_id: str
    queue_id: str
    study_id: str
    operation: str
    status: str
    plan_digest: str
    payload_json: str

    def __post_init__(self):
        from cmw.nodes.protocol import bounded_value, identifier
        if type(self.schema) is not int or self.schema != 1:
            raise ValueError('Unsupported study response schema')
        identifier(self.node_id, 'node_id')
        identifier(self.queue_id, 'queue_id')
        identifier(self.study_id, 'study_id')
        states = {
            'plan': {'READY', 'BLOCKED'},
            'materialize': {'MATERIALIZED', 'PARTIAL'},
            'register': {'REGISTERED', 'PARTIAL'},
            'compare': {'COMPLETE', 'PARTIAL', 'BLOCKED'},
            'view': {'COMPLETE', 'PARTIAL', 'NOT_BOUND'},
        }
        if self.operation not in states or self.status not in states[self.operation]:
            raise ValueError('Unsupported study operation/status')
        if type(self.plan_digest) is not str or not re.fullmatch('[a-f0-9]{64}', self.plan_digest):
            raise ValueError('Invalid study plan digest')
        if type(self.payload_json) is not str or len(self.payload_json.encode()) > 256 * 1024:
            raise ValueError('Study response exceeds 256 KiB')
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('Duplicate study response field')
                result[key] = value
            return result
        try:
            value = json.loads(self.payload_json, object_pairs_hook=pairs,
                               parse_constant=lambda text: (_ for _ in ()).throw(ValueError(text)))
        except RecursionError as exc:
            raise ValueError('Study response nesting exceeds the protocol limit') from exc
        if type(value) is not dict:
            raise ValueError('Study response payload must be an object')
        bounded_value(value)

    def to_dict(self):
        value = asdict(self)
        value['payload'] = json.loads(value.pop('payload_json'))
        return value
