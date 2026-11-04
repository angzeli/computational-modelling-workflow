"""Pure Phase 0 decisions; these contracts do not control existing Jobs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import math

from .models import CancelMode, Capabilities, ContractError, parse_version


def _number(value, name):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ContractError(f'{name} must be finite')
    return value


def _capabilities(value):
    return Capabilities.from_dict(value.to_dict() if isinstance(value, Capabilities) else value).to_dict()


@dataclass(frozen=True)
class Compatibility:
    compatible: bool
    features: frozenset[str]
    max_bundle_bytes: int | None
    reason: str


def negotiate(local_version: str, remote_version: str,
              local_capabilities: Capabilities | dict,
              remote_capabilities: Capabilities | dict) -> Compatibility:
    """Intersect supported operations/modes, never equate host-specific builds.

    Build inventories describe the target, not protocol features. A local build
    need not exist on the target; build/executable selection belongs in the plan.
    Unknown capability extensions remain inert until their semantics are defined.
    """
    local_major, _ = parse_version(local_version)
    remote_major, _ = parse_version(remote_version)
    if local_major != remote_major:
        return Compatibility(False, frozenset(), None, 'Protocol MAJOR versions differ')
    local, remote = _capabilities(local_capabilities), _capabilities(remote_capabilities)
    features = set()
    if local.get('submit') and remote.get('submit'):
        features.add('submit')
    limit = None
    if local.get('receive') and remote.get('receive'):
        features.add('receive')
        limit = min(local['receive']['max_bundle_bytes'], remote['receive']['max_bundle_bytes'])
    left_modes, right_modes = local.get('cancel_modes', {}), remote.get('cancel_modes', {})
    for engine in left_modes.keys() & right_modes.keys():
        modes = set(available_cancel_modes(engine, local)) & set(available_cancel_modes(engine, remote))
        features.update(f'cancel:{engine}:{mode.value}' for mode in modes)
    features.update(f'engine:{engine}' for engine in local.get('engines', {}).keys() & remote.get('engines', {}).keys())
    return Compatibility(True, frozenset(features), limit, 'Same MAJOR; common capabilities only')


class NodeState(str, Enum):
    REACHABLE = 'REACHABLE'
    UNREACHABLE = 'UNREACHABLE'
    INCOMPATIBLE = 'INCOMPATIBLE'
    AUTH_FAILED = 'AUTH_FAILED'


class NodeEvent(str, Enum):
    SUCCESS = 'success'
    HANDSHAKE_SUCCESS = 'handshake_success'
    CONNECT_ERROR = 'connect_error'
    TIMEOUT = 'timeout'
    AUTH_FAILURE = 'auth_failure'
    INCOMPATIBLE_HANDSHAKE = 'incompatible_handshake'


@dataclass(frozen=True)
class Reachability:
    # None is an unhandshaken client, not a fifth advertised node state.
    state: NodeState | None = None
    consecutive_failures: int = 0
    last_received_at: float | None = None

    def __post_init__(self):
        if self.state is not None and not isinstance(self.state, NodeState):
            raise ContractError('Reachability state must be a NodeState')
        if type(self.consecutive_failures) is not int or self.consecutive_failures < 0:
            raise ContractError('Failure count must be a nonnegative integer')
        if self.last_received_at is not None:
            _number(self.last_received_at, 'Portal receive time')

    @property
    def operations_enabled(self) -> bool:
        return self.state == NodeState.REACHABLE

    @property
    def polling_enabled(self) -> bool:
        return self.state != NodeState.AUTH_FAILED

    @property
    def handshake_required(self) -> bool:
        return self.state != NodeState.REACHABLE

    def age(self, portal_now: float) -> float | None:
        _number(portal_now, 'Portal time')
        if self.last_received_at is None:
            return None
        if portal_now < self.last_received_at:
            raise ContractError('Portal time precedes its receive time')
        return portal_now - self.last_received_at


def transition(current: Reachability, event: NodeEvent | str, *,
               received_at: float | None = None, manual_retry: bool = False) -> Reachability:
    """One portal-side observation; no retry, polling or clocks are performed."""
    try:
        event = NodeEvent(event)
    except (TypeError, ValueError) as exc:
        raise ContractError('Unknown reachability event') from exc
    if type(manual_retry) is not bool:
        raise ContractError('Manual retry must be boolean')
    if current.state == NodeState.AUTH_FAILED and not manual_retry:
        return current
    if event == NodeEvent.AUTH_FAILURE:
        return Reachability(NodeState.AUTH_FAILED, 0, current.last_received_at)
    if event == NodeEvent.INCOMPATIBLE_HANDSHAKE:
        return Reachability(NodeState.INCOMPATIBLE, 0, current.last_received_at)
    if event in (NodeEvent.SUCCESS, NodeEvent.HANDSHAKE_SUCCESS):
        _number(received_at, 'Portal receive time')
        if current.last_received_at is not None and received_at < current.last_received_at:
            raise ContractError('Portal receive times must not move backwards')
        state = NodeState.REACHABLE if event == NodeEvent.HANDSHAKE_SUCCESS else current.state
        return Reachability(state, 0, received_at)
    failures = current.consecutive_failures + 1
    state = current.state
    if state not in (NodeState.INCOMPATIBLE, NodeState.AUTH_FAILED) and failures >= 3:
        state = NodeState.UNREACHABLE
    return Reachability(state, failures, current.last_received_at)


class SkewLevel(str, Enum):
    NONE = 'none'
    AMBER = 'amber'
    RED = 'red'


@dataclass(frozen=True)
class ClockSkew:
    seconds: float
    level: SkewLevel
    round_trip_seconds: float
    uncertainty_seconds: float


def estimate_clock_skew(portal_sent: float, portal_received: float,
                        node_time_utc: str | float) -> ClockSkew:
    """Estimate node-minus-portal skew; warning thresholds use absolute skew."""
    _number(portal_sent, 'Portal send time')
    _number(portal_received, 'Portal receive time')
    if portal_received < portal_sent:
        raise ContractError('Portal receive time precedes send time')
    if isinstance(node_time_utc, str):
        try:
            value = datetime.fromisoformat(node_time_utc.replace('Z', '+00:00'))
            if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
                raise ValueError('UTC required')
            node_time = value.timestamp()
        except (ValueError, OverflowError) as exc:
            raise ContractError('Node clock must be a UTC timestamp') from exc
    else:
        node_time = _number(node_time_utc, 'Node time')
    elapsed = portal_received - portal_sent
    skew = node_time - (portal_sent + elapsed / 2)
    level = SkewLevel.RED if abs(skew) >= 60 else SkewLevel.AMBER if abs(skew) >= 5 else SkewLevel.NONE
    return ClockSkew(skew, level, elapsed, elapsed / 2)


class CancelAction(str, Enum):
    START = 'start'
    ESCALATE = 'escalate'
    NOOP = 'noop'
    REJECT = 'reject'


@dataclass(frozen=True)
class CancelDecision:
    action: CancelAction
    effective_mode: CancelMode | None
    error_code: str | None = None


def available_cancel_modes(engine: str, capabilities: Capabilities | dict) -> tuple[CancelMode, ...]:
    """A missing engine offers no cancellation modes; no implicit fallback."""
    offered = _capabilities(capabilities).get('cancel_modes', {}).get(engine, [])
    # §11.5 bounds known engines even if a peer advertises an invalid extension.
    supported = {'vasp': tuple(CancelMode), 'orca': (CancelMode.HARD,)}.get(engine, ())
    return tuple(mode for mode in supported if mode.value in offered)


def decide_cancel(current: CancelMode | str | None, requested: CancelMode | str, *,
                  engine: str | None = None,
                  capabilities: Capabilities | dict | None = None) -> CancelDecision:
    """The complete explicit escalation table, with no elapsed-time input."""
    try:
        old = None if current is None else CancelMode(current)
        new = CancelMode(requested)
    except (TypeError, ValueError) as exc:
        raise ContractError('Unknown cancellation mode') from exc
    if (engine is None) != (capabilities is None):
        raise ContractError('Engine and capabilities must be supplied together')
    if engine is not None and new not in available_cancel_modes(engine, capabilities):
        return CancelDecision(CancelAction.REJECT, old, 'capability_unsupported')
    if old is None:
        return CancelDecision(CancelAction.START, new)
    if old == new:
        return CancelDecision(CancelAction.NOOP, old)
    order = (CancelMode.GRACEFUL, CancelMode.SOFT, CancelMode.HARD)
    if order.index(new) < order.index(old):
        return CancelDecision(CancelAction.REJECT, old, 'conflict')
    return CancelDecision(CancelAction.ESCALATE, new)
