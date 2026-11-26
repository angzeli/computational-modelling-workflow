"""Pure node decisions: P1 observation wiring; cancellation remains unwired.

No function here polls, signals, writes STOPCAR or changes a node/Jobs record.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

from .protocol import NodeError

POLL_INTERVAL_SECONDS = 5.0
STALE_INTERVALS = 3  # D11: a receipt stays fresh for three poll intervals.
STALE_AFTER_SECONDS = STALE_INTERVALS * POLL_INTERVAL_SECONDS
# S05: a scheduler-backed lane polls no faster than every 30 s. Its interval is
# enrolled on the node and pinned in the frontend connection.
SCHEDULER_POLL_BOUNDS = (30, 600)


def scheduler_poll_interval(value):
    """An enrolled scheduler poll interval in whole seconds within the S05 bounds."""
    low, high = SCHEDULER_POLL_BOUNDS
    if type(value) is not int or not low <= value <= high:
        raise NodeError('INVALID_PARAMS', f'poll_interval_seconds must be an integer from {low} to {high}')
    return value


def _finite(value, name):
    if type(value) not in (int, float):
        raise NodeError('INVALID_PARAMS', f'{name} must be a finite number')
    try:
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise NodeError('INVALID_PARAMS', f'{name} must be a finite number')
    return value


class CancelMode(str, Enum):
    GRACEFUL = 'graceful'
    SOFT = 'soft'
    HARD = 'hard'


class CancelAction(str, Enum):
    START = 'START'
    ESCALATE = 'ESCALATE'
    NOOP = 'NOOP'
    REJECT = 'REJECT'


@dataclass(frozen=True)
class CancelDecision:
    action: CancelAction
    effective_mode: CancelMode | None
    error_code: str | None = None


def available_cancel_modes(engine: str) -> tuple[CancelMode, ...]:
    if not isinstance(engine, str):
        raise NodeError('INVALID_PARAMS', 'Engine must be a string')
    return {'VASP': tuple(CancelMode), 'ORCA': (CancelMode.HARD,)}.get(engine.upper(), ())


def decide_cancel(engine: str, current: CancelMode | str | None,
                  requested: CancelMode | str) -> CancelDecision:
    """Evaluate the explicit D13 escalation table; there is no timeout input."""
    try:
        current = None if current is None else CancelMode(current)
        requested = CancelMode(requested)
    except (TypeError, ValueError) as exc:
        raise NodeError('INVALID_PARAMS', 'Use graceful, soft or hard cancellation') from exc
    if requested not in available_cancel_modes(engine):
        return CancelDecision(CancelAction.REJECT, current, 'UNSUPPORTED_OPERATION')
    if current is None:
        return CancelDecision(CancelAction.START, requested)
    if current == requested:
        return CancelDecision(CancelAction.NOOP, current)
    modes = tuple(CancelMode)
    if modes.index(requested) < modes.index(current):
        return CancelDecision(CancelAction.REJECT, current, 'REQUEST_CONFLICT')
    return CancelDecision(CancelAction.ESCALATE, requested)


class NodeState(str, Enum):
    REACHABLE = 'REACHABLE'
    UNREACHABLE = 'UNREACHABLE'
    AUTH_FAILED = 'AUTH_FAILED'
    INCOMPATIBLE = 'INCOMPATIBLE'
    # S03: no user-opened SSH session to attach to. A frontend state, never a
    # protocol-1 error code, and never counted toward the D10 failure rule.
    AUTH_REQUIRED = 'AUTH_REQUIRED'


class NodeEvent(str, Enum):
    SUCCESS = 'SUCCESS'
    HANDSHAKE_SUCCESS = 'HANDSHAKE_SUCCESS'
    TRANSPORT_FAILURE = 'TRANSPORT_FAILURE'
    TIMEOUT = 'TIMEOUT'
    AUTH_FAILURE = 'AUTH_FAILURE'
    INCOMPATIBLE_HANDSHAKE = 'INCOMPATIBLE_HANDSHAKE'
    SESSION_REQUIRED = 'SESSION_REQUIRED'


@dataclass(frozen=True)
class Reachability:
    # None means capabilities have not yet been verified, not a wire node state.
    state: NodeState | None = None
    consecutive_failures: int = 0
    received_at: float | None = None

    def __post_init__(self):
        if self.state is not None and not isinstance(self.state, NodeState):
            raise NodeError('INVALID_PARAMS', 'Expected a NodeState')
        if type(self.consecutive_failures) is not int or self.consecutive_failures < 0:
            raise NodeError('INVALID_PARAMS', 'Failure count must be a nonnegative integer')
        if self.received_at is not None:
            _finite(self.received_at, 'Portal receive time')

    @property
    def requires_handshake(self) -> bool:
        return self.state != NodeState.REACHABLE


def transition(current: Reachability, event: NodeEvent | str, *,
               received_at: float | None = None) -> Reachability:
    """Pure reachability transition; successful capabilities clear failed states.

    Generic successes reset the failure counter but cannot replace a fresh
    compatible capabilities handshake after a disconnected/incompatible state.
    All receive times belong to the portal's own monotonic clock.
    """
    if not isinstance(current, Reachability):
        raise NodeError('INVALID_PARAMS', 'Expected reachability state')
    try:
        event = NodeEvent(event)
    except (TypeError, ValueError) as exc:
        raise NodeError('INVALID_PARAMS', 'Unknown reachability event') from exc
    if event == NodeEvent.AUTH_FAILURE:
        return Reachability(NodeState.AUTH_FAILED, 0, current.received_at)
    if event == NodeEvent.SESSION_REQUIRED:
        return Reachability(NodeState.AUTH_REQUIRED, 0, current.received_at)
    if event == NodeEvent.INCOMPATIBLE_HANDSHAKE:
        return Reachability(NodeState.INCOMPATIBLE, 0, current.received_at)
    if event in (NodeEvent.SUCCESS, NodeEvent.HANDSHAKE_SUCCESS):
        _finite(received_at, 'Portal receive time')
        if current.received_at is not None and received_at < current.received_at:
            raise NodeError('INVALID_PARAMS', 'Portal receive time moved backwards')
        state = NodeState.REACHABLE if event == NodeEvent.HANDSHAKE_SUCCESS else current.state
        return Reachability(state, 0, received_at)
    count = current.consecutive_failures + 1
    state = current.state
    if state not in (NodeState.AUTH_FAILED, NodeState.INCOMPATIBLE) and count >= 3:
        state = NodeState.UNREACHABLE
    return Reachability(state, count, current.received_at)


class Staleness(str, Enum):
    UNAVAILABLE = 'UNAVAILABLE'
    FRESH = 'FRESH'
    STALE = 'STALE'


def classify_staleness(received_at: float | None, portal_now: float, *,
                       poll_interval: float = POLL_INTERVAL_SECONDS) -> Staleness:
    """D11 keeps the inclusive freshness boundary: three poll intervals (15 s by default)."""
    _finite(portal_now, 'Portal time')
    _finite(poll_interval, 'Poll interval')
    if received_at is None:
        return Staleness.UNAVAILABLE
    _finite(received_at, 'Portal receive time')
    if portal_now < received_at:
        raise NodeError('INVALID_PARAMS', 'Portal time precedes its receive time')
    return Staleness.STALE if portal_now - received_at > STALE_INTERVALS * poll_interval else Staleness.FRESH


class SkewLevel(str, Enum):
    NONE = 'NONE'
    AMBER = 'AMBER'
    RED = 'RED'


@dataclass(frozen=True)
class ClockSkew:
    seconds: float
    level: SkewLevel
    uncertainty_seconds: float


def classify_clock_skew(seconds: float) -> SkewLevel:
    _finite(seconds, 'Clock skew')
    return SkewLevel.RED if abs(seconds) >= 60 else SkewLevel.AMBER if abs(seconds) >= 5 else SkewLevel.NONE


def estimate_clock_skew(portal_sent: float, portal_received: float,
                        node_observed_at: float) -> ClockSkew:
    """Display-only node-minus-portal midpoint estimate from wall-clock seconds.

    Existing source aging remains in connections.observed_machine_usage; this
    estimate must never substitute for portal monotonic receive-time freshness.
    """
    for name, value in (('Portal send time', portal_sent), ('Portal receive time', portal_received),
                        ('Node observation time', node_observed_at)):
        _finite(value, name)
    if portal_received < portal_sent:
        raise NodeError('INVALID_PARAMS', 'Portal receive time precedes send time')
    half = (portal_received - portal_sent) / 2
    seconds = node_observed_at - (portal_sent + half)
    return ClockSkew(seconds, classify_clock_skew(seconds), half)
