"""Read-only recovery proposals; persistent apply requires separate qualification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lifecycle import observe_execution, observation_key, read_job
from .store import ACTIVE, JobsError

APPLY_DEFERRED = ('P2 lifecycle recovery apply not implemented/qualified. '
                  'Persistent reconciliation is unavailable; this interface grants no recovery authority.')


@dataclass(frozen=True)
class ReconciliationPreview:
    job_id: str
    binding: tuple
    observation: dict[str, Any]
    proposed_action: str
    proposed_outcome: str | None
    rationale: tuple[str, ...]
    conditional_field_changes: dict[str, Any]
    prerequisites: tuple[str, ...]
    stale_preview: bool = False

    def to_dict(self):
        return {'schema': 1, 'job_id': self.job_id, 'binding': self.binding,
                'execution_observation': self.observation,
                'proposed_action': self.proposed_action, 'proposed_outcome': self.proposed_outcome,
                'rationale': list(self.rationale),
                'conditional_field_changes': self.conditional_field_changes,
                'prerequisites': list(self.prerequisites), 'stale_preview': self.stale_preview,
                'mutation_permitted': False, 'apply_supported': False,
                'apply_deferred_reason': APPLY_DEFERRED,
                'resource_commitments': 'retained', 'dispatch_enablement_permitted': False}


def preview_reconciliation(store, job, *, state=None, now=None):
    """Describe a conditional proposal without acquiring recovery authority.

    The binding excludes human notes but includes the attempt and lifecycle
    claims. A second read detects an in-flight worker update; this preview is
    never an authorization token and cannot be applied by this runtime.
    """
    if state is None:
        _, control = read_job(store, job['id'])
        state = {'schema': control['schema'], 'controller': control}
    if type(state.get('schema')) is not int or state['schema'] not in {1, 2, 3}:
        raise JobsError('Unsupported Jobs state schema for reconciliation preview')
    binding = observation_key(store, job)
    observation = observe_execution(store, job, state=state, now=now).to_dict(now=now)
    current, fresh_control = read_job(store, job['id'])
    controller_fields = ('schema', 'owner', 'dispatch', 'stop')
    authority_changed = any(fresh_control.get(key) != state.get('controller', {}).get(key)
                            for key in controller_fields)
    stale = (current is None or observation_key(store, current) != binding
             or authority_changed or observation.get('stale', False))
    evidence = observation['evidence']
    worker = evidence.get('worker', {}).get('verdict')
    lock = evidence.get('worker_lock', {}).get('verdict')
    controller = evidence.get('controller', {}).get('verdict')
    action, outcome, changes, prerequisites = 'none', None, {}, ()
    if stale:
        rationale = ('STALE_PREVIEW: the lifecycle identity or observation changed; request a fresh preview.',)
    elif (evidence.get('source', {}).get('verdict') != 'matched'
          or observation.get('observation_quality') == 'unavailable'):
        rationale = ('Observation source is unavailable, changed or unsupported; no transition is proposed.',)
    elif fresh_control['schema'] == 3:
        rationale = (APPLY_DEFERRED,
                     'Schema 3 previews describe evidence only; no persistent lifecycle transition is proposed.')
    elif job['status'] not in ACTIVE:
        rationale = ('No active managed attempt requires recovery.',)
    elif job['status'] == 'Unknown':
        rationale = ('The attempt is already Unknown; unresolved ownership and resource commitments remain retained.',)
    elif worker == 'matched' or lock == 'held' or controller in {'online', 'matched', 'verified', 'stale'}:
        rationale = ('Existing controller or worker authority may remain active; this preview cannot race or override it.',)
    elif worker == 'gone' and lock == 'unheld' and controller == 'offline':
        action, outcome = 'conditional_mark_unknown', 'Unknown'
        reason = 'Worker ownership/completion cannot be verified; automatic progression stopped'
        changes = {'job.status': 'Unknown', 'controller.dispatch': False, 'job.reason': reason,
                   'controller.reason': f"Needs attention: {job['display_id']}: {reason}",
                   'events.append': reason}
        prerequisites = ('A separately authorized and qualified recovery implementation plus an explicit --apply request.',
                         'Revalidate canonical state, attempt, claim and expected lifecycle fields at commit time.',
                         'Respect launch grace and the frozen cancellation/suspension protocol before any transition.',
                         'Reject changed evidence or active controller/worker authority without signalling or adoption.',
                         'Retain resource commitments and disable dispatch atomically; do not create a new attempt.')
        rationale = ('The recorded worker is absent and its existing lock is unheld.',
                     'Computational-domain or terminal evidence does not grant this preview completion authority.',
                     'Unknown is a conditional conservative proposal; no state or scheduling field has changed.')
    else:
        rationale = ('Available ownership evidence is incomplete or inaccessible; no concrete transition is proposed.',
                     'A receipt alone does not establish Done, Fail or Cancelled.')
    return ReconciliationPreview(job['display_id'], binding, observation, action, outcome,
                                 rationale, changes, prerequisites, stale)


def preview_text(preview):
    from .cli import execution_text

    lines = [execution_text(preview['execution_observation']), '',
             f"Proposed action: {preview['proposed_action']}",
             f"Proposed outcome: {preview['proposed_outcome'] or 'None'}",
             'Mutation permitted: No. Apply supported: No.',
             'Resource commitments: retained. Dispatch is not enabled by this preview.']
    lines.extend(f"Reason: {item}" for item in preview['rationale'])
    lines.extend(f"Conditional change: {key} → {value}" for key, value in preview['conditional_field_changes'].items())
    lines.extend(f"Required before any future apply: {item}" for item in preview['prerequisites'])
    lines.append(preview['apply_deferred_reason'])
    return '\n'.join(lines)
