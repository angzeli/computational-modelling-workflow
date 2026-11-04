"""Phase 0 transport contract and deterministic, entirely in-memory test peer.

No implementation here opens a channel, sleeps, or calls an existing Jobs API.
Fake latency advances a virtual UTC clock; it does not delay the test process.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math
from typing import Mapping

from .models import Capabilities, ContractError, Envelope


class FailureKind(str, Enum):
    CONNECT_ERROR = 'connect_error'
    TIMEOUT = 'timeout'
    AUTH_FAILURE = 'auth_failure'


@dataclass(frozen=True)
class TransportFailure:
    """Channel evidence only; never an assertion that a mutation did not occur."""

    kind: FailureKind
    message: str
    elapsed_seconds: float = 0.0


class Transport(ABC):
    """Run one future ``cmw jobs node <command>`` request.

    Arguments are structured data, never a shell string. Unknown/malformed wire
    versions raise the model's typed ContractError; channel failures are values.
    A caller must retain request_id across uncertain submissions/cancellations.
    """

    @abstractmethod
    def run(self, command: str, *, arguments: Mapping | None = None,
            request_id: str | None = None, stdin: bytes | None = None,
            timeout: float = 4.0) -> Envelope | TransportFailure:
        raise NotImplementedError


class LocalTransport(Transport):
    def run(self, command: str, *, arguments: Mapping | None = None,
            request_id: str | None = None, stdin: bytes | None = None,
            timeout: float = 4.0) -> Envelope | TransportFailure:
        raise NotImplementedError('LocalTransport is implemented in Phase 1')


class SshTransport(Transport):
    def run(self, command: str, *, arguments: Mapping | None = None,
            request_id: str | None = None, stdin: bytes | None = None,
            timeout: float = 4.0) -> Envelope | TransportFailure:
        raise NotImplementedError('SshTransport is implemented in Phase 3')


def _seconds(value, name, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or (positive and value == 0)):
        raise ValueError(f'{name} must be finite and {"positive" if positive else "nonnegative"}')
    return float(value)


@dataclass(frozen=True)
class FakeStep:
    """One response or channel failure, consumed by one attempted request.

    None generates a hello response. Other commands require an explicit response.
    Wire dictionaries/JSON go through the same strict parser as a real response.
    """

    response: Envelope | dict | str | bytes | None = None
    latency_seconds: float = 0.0
    failure: FailureKind | None = None

    def __post_init__(self):
        _seconds(self.latency_seconds, 'latency_seconds')
        if self.failure is not None and not isinstance(self.failure, FailureKind):
            raise ValueError('failure must be a FailureKind')
        if self.failure is not None and self.response is not None:
            raise ValueError('A fake step cannot have both a response and a failure')


class FakeTransport(Transport):
    """Scriptable peer with virtual latency, clock offset and channel failures.

    ``disconnect_after=2`` permits two requests, then reports connect errors.
    ``protocol_version`` controls generated hello responses, including mismatch
    scenarios. Explicit scripted responses retain their exact wire versions.
    ``auth_failure`` persists until the test explicitly clears it. No retry or
    automatic mutation is performed by this transport.
    """

    def __init__(self, steps=(), *, node_id='mm_m5pro', protocol_version='1.0',
                 capabilities: Capabilities | dict | None = None,
                 physical_cores=18, core_tiers=(), recommended_max_parallelism=None,
                 clock_offset_seconds=0.0,
                 start_time=1793793600.0, latency_seconds=0.0,
                 disconnect_after=None, auth_failure=False):
        if (type(clock_offset_seconds) not in (int, float)
                or not math.isfinite(clock_offset_seconds)):
            raise ValueError('clock_offset_seconds must be finite')
        if type(start_time) not in (int, float) or not math.isfinite(start_time):
            raise ValueError('start_time must be finite')
        if disconnect_after is not None and (type(disconnect_after) is not int or disconnect_after < 0):
            raise ValueError('disconnect_after must be a nonnegative integer')
        if type(auth_failure) is not bool:
            raise ValueError('auth_failure must be boolean')
        self.steps = deque(steps)
        if any(not isinstance(step, FakeStep) for step in self.steps):
            raise ValueError('steps must contain FakeStep objects')
        self.node_id = node_id
        self.protocol_version = protocol_version
        self.capabilities = (capabilities.to_dict() if isinstance(capabilities, Capabilities)
                             else deepcopy(capabilities if capabilities is not None else {}))
        self.physical_cores = physical_cores
        self.core_tiers = deepcopy(list(core_tiers))
        self.recommended_max_parallelism = recommended_max_parallelism
        self.clock_offset_seconds = float(clock_offset_seconds)
        self.now = float(start_time)
        self.latency_seconds = _seconds(latency_seconds, 'latency_seconds')
        self.disconnect_after = disconnect_after
        self.auth_failure = auth_failure
        self.requests = 0
        self.last_sent_at = None
        self.last_received_at = None

    def _hello(self, command, request_id, latency):
        # The virtual peer samples at the midpoint of this synthetic round trip.
        instant = self.last_sent_at + latency / 2 + self.clock_offset_seconds
        stamp = datetime.fromtimestamp(instant, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        value = {
            'protocol_version': self.protocol_version, 'node_id': self.node_id,
            'command': command, 'ok': True, 'error': None,
            'result': {
                'node_id': self.node_id, 'protocol_version': self.protocol_version,
                'cmw_version': '0.1.0', 'cmw_commit': 'synthetic',
                'node_time_utc': stamp, 'hardware': {
                    'physical_cores': self.physical_cores,
                    'core_tiers': deepcopy(self.core_tiers),
                    'recommended_max_parallelism': self.recommended_max_parallelism,
                },
                'capabilities': deepcopy(self.capabilities),
            },
        }
        if request_id is not None:
            value['request_id'] = request_id
        return value

    def run(self, command: str, *, arguments: Mapping | None = None,
            request_id: str | None = None, stdin: bytes | None = None,
            timeout: float = 4.0) -> Envelope | TransportFailure:
        timeout = _seconds(timeout, 'timeout', positive=True)
        if command not in {'hello', 'snapshot', 'receive', 'submit', 'cancel', 'tail'}:
            raise ContractError('Unsupported node command')
        if arguments is not None and not isinstance(arguments, Mapping):
            raise ContractError('arguments must be a mapping')
        if stdin is not None and not isinstance(stdin, bytes):
            raise ContractError('stdin must be bytes')
        if request_id is not None and (not isinstance(request_id, str) or not request_id):
            raise ContractError('request_id must be a nonempty string')
        self.last_sent_at = self.now
        self.requests += 1
        if self.auth_failure:
            step = FakeStep(failure=FailureKind.AUTH_FAILURE)
        elif self.disconnect_after is not None and self.requests > self.disconnect_after:
            step = FakeStep(failure=FailureKind.CONNECT_ERROR)
        elif self.steps:
            step = self.steps.popleft()
        else:
            step = FakeStep(latency_seconds=self.latency_seconds)
        elapsed = min(step.latency_seconds, timeout)
        if step.failure == FailureKind.TIMEOUT:
            elapsed = timeout
        self.now += elapsed
        self.last_received_at = self.now
        if step.latency_seconds > timeout or step.failure == FailureKind.TIMEOUT:
            return TransportFailure(FailureKind.TIMEOUT, 'Synthetic request timed out', elapsed)
        if step.failure is not None:
            return TransportFailure(step.failure, 'Synthetic channel failure', elapsed)
        response = step.response
        if response is None:
            if command != 'hello':
                raise ContractError('FakeTransport needs a scripted response for this command')
            response = self._hello(command, request_id, elapsed)
        if isinstance(response, Envelope):
            response = response.to_dict()
        result = (Envelope.from_json(response) if isinstance(response, (str, bytes))
                  else Envelope.from_dict(deepcopy(response)))
        if result.command != command or result.node_id != self.node_id or result.request_id != request_id:
            raise ContractError('Response does not match the requested command, node and request_id')
        return result
