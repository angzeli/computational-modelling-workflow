"""Strict internal wire models; retain scientific display hints without code loading."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import json
import math
import types
from typing import get_args, get_origin, get_type_hints, Union

from cmw.progress import models
from cmw.progress.registry import SourcePlan
from cmw.jobs.source_selection import SourceInspection, SourceValidation, SessionSelection, SourceFingerprint


SCHEMA = 1
MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 24
MAX_ITEMS = 4096
MAX_STRING = 256 * 1024
_TYPES = {name: value for name, value in vars(models).items()
          if isinstance(value, type) and is_dataclass(value)}
_TYPES.update({value.__name__: value for value in (
    SourcePlan, SourceInspection, SourceValidation, SessionSelection, SourceFingerprint)})


class CodecError(ValueError):
    """Unsupported or malformed scientific wire evidence."""


def _primitive(value, depth=0):
    if depth > MAX_DEPTH:
        raise CodecError('Scientific model nesting exceeds the wire limit')
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise CodecError('Scientific numbers must be finite')
        return value
    if type(value) is str:
        if len(value) > MAX_STRING:
            raise CodecError('Scientific string exceeds the wire limit')
        return value
    if type(value) in (tuple, list):
        if len(value) > MAX_ITEMS:
            raise CodecError('Scientific sequence exceeds the wire limit')
        return [_primitive(item, depth + 1) for item in value]
    if is_dataclass(value) and type(value) in _TYPES.values():
        return {item.name: _primitive(getattr(value, item.name), depth + 1) for item in fields(value)}
    raise CodecError('Unsupported scientific wire value')


def _plain(value, depth):
    """Unparameterized identity tuples contain only bounded primitive identities."""
    if type(value) is list:
        if depth > MAX_DEPTH or len(value) > MAX_ITEMS:
            raise CodecError('Identity sequence exceeds the wire limit')
        return tuple(_plain(item, depth + 1) for item in value)
    if value is None or type(value) in (bool, int, float, str):
        return _primitive(value, depth)
    raise CodecError('Identity contains a non-primitive value')


def _decode(value, annotation, depth=0):
    if depth > MAX_DEPTH:
        raise CodecError('Scientific model nesting exceeds the wire limit')
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        for option in arguments:
            try:
                return _decode(value, option, depth + 1)
            except CodecError:
                pass
        raise CodecError('Scientific value has the wrong union type')
    if annotation is type(None):
        if value is not None:
            raise CodecError('Expected null scientific field')
        return None
    if annotation in (str, bool, int, float):
        valid = type(value) is annotation or annotation is float and type(value) is int
        if not valid:
            raise CodecError('Scientific field has the wrong scalar type')
        return _primitive(value, depth)
    if annotation is tuple or origin is tuple:
        if type(value) is not list or len(value) > MAX_ITEMS:
            raise CodecError('Scientific tuple must be a bounded JSON array')
        if not arguments:
            return _plain(value, depth)
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_decode(item, arguments[0], depth + 1) for item in value)
        if len(arguments) != len(value):
            raise CodecError('Scientific tuple has the wrong length')
        return tuple(_decode(item, expected, depth + 1) for item, expected in zip(value, arguments))
    if isinstance(annotation, type) and annotation in _TYPES.values():
        if type(value) is not dict:
            raise CodecError('Scientific model must be a JSON object')
        expected = {item.name for item in fields(annotation)}
        if set(value) != expected:
            raise CodecError('Scientific model fields differ from the negotiated schema')
        hints = get_type_hints(annotation)
        decoded = {key: _decode(item, hints[key], depth + 1) for key, item in value.items()}
        try:
            return annotation(**decoded)
        except (ValueError, TypeError) as exc:
            raise CodecError(f'Invalid {annotation.__name__}: {exc}') from exc
    raise CodecError('Unregistered scientific field type')


def encode_typed(value):
    """Encode a whitelisted dataclass, including internal CardCell.value_format."""
    if type(value) not in _TYPES.values():
        raise CodecError('Unregistered scientific model')
    result = {'schema': SCHEMA, 'type': type(value).__name__, 'value': _primitive(value)}
    if len(json.dumps(result, allow_nan=False, ensure_ascii=True).encode()) > MAX_BYTES:
        raise CodecError('Scientific model exceeds the wire byte limit')
    # Encoding also enforces annotations; malformed in-process fixtures cannot cross the seam.
    decode_typed(result, type(value))
    return result


def decode_typed(payload, expected_type=None):
    """Decode only exact known dataclass schemas; never imports caller-supplied types."""
    if type(payload) is not dict or set(payload) != {'schema', 'type', 'value'}:
        raise CodecError('Scientific envelope fields differ from schema 1')
    if type(payload['schema']) is not int or payload['schema'] != SCHEMA:
        raise CodecError('Unsupported scientific model schema')
    name = payload['type']
    if type(name) is not str or name not in _TYPES:
        raise CodecError('Unregistered scientific model type')
    model = _TYPES[name]
    if expected_type is not None and model is not expected_type:
        raise CodecError('Scientific response model differs from the requested type')
    try:
        length = len(json.dumps(payload, allow_nan=False, ensure_ascii=True).encode())
    except (ValueError, TypeError, RecursionError) as exc:
        raise CodecError('Scientific payload is not finite bounded JSON') from exc
    if length > MAX_BYTES:
        raise CodecError('Scientific model exceeds the wire byte limit')
    return _decode(payload['value'], model)
