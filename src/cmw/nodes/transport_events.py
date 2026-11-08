"""Classify structured transport/refusal evidence without interpreting messages."""
from dataclasses import dataclass
from enum import Enum

from .contracts import NodeEvent
from .protocol import NodeError, is_mutation


class FailureKind(str, Enum):
    TRANSPORT = 'transport'
    AUTHENTICATION = 'authentication'
    SECURITY = 'security'
    INCOMPATIBLE = 'incompatible'
    READINESS = 'readiness'
    OUTCOME_UNKNOWN = 'outcome-unknown'
    OPERATION = 'operation'


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    event: NodeEvent | None
    reconnect: bool = False
    controls_refused: bool = False
    outcome_unknown: bool = False


class TransportFailure(NodeError):
    """Local evidence survives the unchanged protocol's {code,message} shape."""
    def __init__(self, code, message, *, event=NodeEvent.TRANSPORT_FAILURE, exit_status=None):
        super().__init__(code, message)
        self.event, self.exit_status = event, exit_status


def classify_failure(error, *, operation=None):
    code = error.code if isinstance(error, NodeError) else None
    evidence = error
    if isinstance(error.__cause__, TransportFailure) and error.__cause__.code == code:
        evidence = error.__cause__
    if code in {'AUTH_FAILED', 'SSH_AUTH_FAILED'}:
        return Failure(FailureKind.AUTHENTICATION, NodeEvent.AUTH_FAILURE, True, True)
    if code in {'SSH_IDENTITY_CHANGED', 'SSH_IDENTITY_UNVERIFIED', 'ENDPOINT_CHANGED',
                'IDENTITY_CHANGED', 'QUEUE_CHANGED', 'RUNTIME_MISMATCH', 'UNSAFE_CONFIGURATION'}:
        return Failure(FailureKind.SECURITY, None, True, True,
                       is_mutation(operation) and code not in {'SSH_IDENTITY_CHANGED', 'SSH_IDENTITY_UNVERIFIED'})
    if (code in {'INCOMPATIBLE_PROTOCOL', 'INCOMPATIBLE_CAPABILITIES'} or
            (operation == 'capabilities' and code in {'INVALID_RESPONSE', 'INVALID_FRAME', 'UNSUPPORTED_OPERATION'})):
        return Failure(FailureKind.INCOMPATIBLE, NodeEvent.INCOMPATIBLE_HANDSHAKE, True, True)
    if code in {'NODE_REFUSED', 'NODE_NOT_READY', 'QUEUE_UNAVAILABLE', 'UNKNOWN_QUEUE', 'INVALID_QUEUE',
                'HANDSHAKE_REQUIRED', 'OWNERSHIP_UNVERIFIABLE'}:
        return Failure(FailureKind.READINESS, None, True, True)
    if code in {'OUTCOME_UNKNOWN', 'DISCONNECTED', 'UNREACHABLE', 'CLOSED', 'INVALID_RESPONSE', 'INVALID_FRAME', 'TIMEOUT'} or isinstance(error, OSError):
        event = (evidence.event if isinstance(evidence, TransportFailure) else
                 NodeEvent.TIMEOUT if isinstance(error, TimeoutError) or code == 'TIMEOUT' else NodeEvent.TRANSPORT_FAILURE)
        unknown = code == 'OUTCOME_UNKNOWN' or is_mutation(operation)
        return Failure(FailureKind.OUTCOME_UNKNOWN if unknown else FailureKind.TRANSPORT,
                       event, True, True, unknown)
    return Failure(FailureKind.OPERATION, None)


def compatible_capabilities(envelope):
    """Require the existing protocol's observation contract, not future features."""
    result = envelope.get('result') if isinstance(envelope, dict) else None
    if not isinstance(result, dict):
        raise NodeError('INCOMPATIBLE_CAPABILITIES', 'Endpoint capabilities must be an object')
    operations = result.get('operations')
    if (not isinstance(operations, list) or any(not isinstance(item, str) for item in operations)
            or not {'capabilities', 'status'} <= set(operations)):
        raise NodeError('INCOMPATIBLE_CAPABILITIES', 'Endpoint lacks the compatible capabilities/status contract')
    readiness = result.get('readiness')
    if not isinstance(readiness, dict):
        raise NodeError('INCOMPATIBLE_CAPABILITIES', 'Endpoint lacks structured readiness evidence')
    if readiness.get('identity_ready') is not True:
        raise NodeError('IDENTITY_CHANGED', 'Endpoint identity readiness was refused')
    if readiness.get('runtime_ready') is not True:
        raise NodeError('RUNTIME_MISMATCH', 'Endpoint runtime readiness was refused')
    if readiness.get('ready') is not True:
        raise NodeError('NODE_NOT_READY', 'Endpoint readiness was refused')
    return result
