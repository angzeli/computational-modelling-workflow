"""Purpose-scoped campaign observation contract; never production authority.

The adjacent plan is prospective. Its thresholds must be calibrated and frozen
before a qualification run. Historical reports retain their original rules.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import time

import psutil

PLAN = json.loads(Path(__file__).with_name('qualification-v2.json').read_text())
FIELDS = {'rss_bytes': 'memory_info', 'fds': 'num_fds', 'cpu_seconds': 'cpu_times'}


def identity_status(process):
    evidence = {'pid': process.pid, 'birth': None, 'status': None}
    try:
        evidence['birth'] = process.create_time()
        evidence['status'] = process.status()
    except (psutil.Error, OSError) as exc:
        evidence.update(exception=type(exc).__name__, errno=getattr(exc, 'errno', None), message=str(exc))
    return evidence


def field_sample(process, birth):
    """Keep each method's value or uncertainty, including its identity bracket."""
    values, fields = {'pid': process.pid, 'birth': birth}, {}
    for field, method in FIELDS.items():
        started = time.monotonic()
        before = identity_status(process)
        item = {'field': field, 'method': 'psutil.Process.' + method,
                'started_at': started, 'pre_identity': before, 'value': None}
        try:
            if before['birth'] != birth or before['status'] == psutil.STATUS_ZOMBIE:
                item['classification'] = ('identity_mismatch' if before['birth'] not in (None, birth)
                                          else 'zombie_pending_reap' if before['status'] == psutil.STATUS_ZOMBIE
                                          else 'unavailable_identity')
            else:
                value = getattr(process, method)()
                value = value.rss if field == 'rss_bytes' else value.user + value.system if field == 'cpu_seconds' else value
                item.update(value=value, classification='sampled_live')
        except (psutil.Error, OSError) as exc:
            item.update(classification='access_denied' if isinstance(exc, psutil.AccessDenied)
                        else 'transient_read_uncertainty', exception=type(exc).__name__,
                        errno=getattr(exc, 'errno', None), message=str(exc))
        after = identity_status(process)
        item.update(post_identity=after, ended_at=time.monotonic())
        if after['birth'] not in (None, birth):
            item['classification'] = 'identity_mismatch'
        elif after['birth'] is None and item['classification'] == 'sampled_live':
            item['classification'] = 'unavailable_identity'
        elif after['status'] == psutil.STATUS_ZOMBIE:
            item['classification'] = 'zombie_pending_reap'
        if item['classification'] == 'sampled_live':
            values[field] = item['value']
        else:
            item['value'] = None
        fields[field] = item
    return values, fields


def role_for(argv):
    args = [str(value) for value in argv] if isinstance(argv, (tuple, list)) else [str(argv)]
    if 'controller' in args:
        return 'controller'
    if 'worker' in args:
        return 'worker'
    if any('endpoint' in value for value in args):
        return 'endpoint'
    if any('pty' in value for value in args):
        return 'frontend'
    return 'helper'


def coverage(records, observations, opportunities, *, ended_at, sampling_errors=()):
    """Evaluate each owner/field lifetime, never an overall success ratio."""
    policy = PLAN['coverage']
    rows = []
    for record in records:
        selected = [item for item in observations if item['ledger_id'] == record['ledger_id']]
        start = record['recorded_at']
        # An observed disappearance bounds the lifecycle from above; it does
        # not establish the exact OS death instant or recover earlier fields.
        terminal_times = [item['at'] for item in selected if item['classification'] == 'verified_exited']
        end = min(ended_at, record.get('exit', {}).get('at', ended_at), *terminal_times)
        lifetime = max(0., end - start)
        zombie_times = [item['at'] for item in selected
            if item['classification'] == 'zombie_reaping_pending'
            and record.get('birth') is not None and item.get('birth') == record['birth']]
        # A same-identity zombie is no longer a live resource-observation target.
        # It remains owned and pending reap; cleanup and lifecycle end stay separate.
        resource_end = min(end, *zombie_times) if zombie_times else end
        resource_lifetime = max(0., resource_end-start)
        long_lived = resource_lifetime >= policy['long_lived_seconds']
        for field in FIELDS:
            relevant = [item for item in selected if item.get('required_live') and item.get('at', start) <= resource_end]
            valid = sorted(item['fields'][field]['ended_at'] for item in relevant
                           if item.get('fields', {}).get(field, {}).get('classification') == 'sampled_live')
            valid = [at for at in valid if start <= at <= resource_end]
            points = [start, *valid, resource_end]
            intervals = [{'start': before, 'end': after, 'seconds': after-before}
                         for before, after in zip(points, points[1:])]
            longest = max((item['seconds'] for item in intervals), default=resource_lifetime)
            counts = Counter(item.get('fields', {}).get(field, {}).get('classification',
                             item['classification']) for item in relevant)
            required = field in policy['required_fields'] and (long_lived or record['role'] in {'frontend', 'controller', 'endpoint'})
            failures = []
            if required:
                if len(valid) < (policy['minimum_valid_reads'] if long_lived else 1):
                    failures.append('insufficient_valid_reads')
                if not valid or valid[0]-start > policy['startup_seconds']:
                    failures.append('startup_blind_window')
                if longest > policy['max_blind_seconds']:
                    failures.append('required_blind_interval')
            rows.append({'ledger_id': record['ledger_id'], 'role': record['role'], 'field': field,
                'capture_origin': record['capture_origin'], 'lifetime_seconds': lifetime,
                'first_observed_limit': record['capture_origin'] == 'first-observed-descendant',
                'resource_window_seconds': resource_lifetime, 'resource_end_upper_bound': resource_end,
                'resource_end_basis': 'same-birth zombie observed; reap still required' if zombie_times else 'lifecycle observation end',
                'zombie_pending_reap_seconds': max(0., end-resource_end) if zombie_times else None,
                'lifecycle_end_upper_bound': end, 'end_basis': 'observed verified exit' if terminal_times or record.get('exit') else 'observation window end',
                'required': required, 'valid_reads': len(valid), 'classifications': dict(counts),
                'first_valid_after_seconds': valid[0]-start if valid else None,
                'longest_blind_seconds': longest, 'blind_intervals': intervals,
                'failures': failures, 'resource_claim': 'sampled maxima only'})
    accounting_ok = not sampling_errors and all(item['classification'] in
        {'sampled', 'skipped_lag', 'sampler_crash'} for item in opportunities)
    # Unavailable resource identity is uncertainty, not an unsafe action.
    # Cleanup separately requires fresh signal authority or the original wait.
    unsafe = [item for item in observations if item['classification'] == 'identity_mismatch']
    failures = [row for row in rows if row['failures']]
    return {'contract_version': 2, 'plan_status': PLAN['status'],
        'observation_accounting': 'PASS' if accounting_ok else 'FAIL',
        'resource_observability': 'RESOURCE_QUALIFICATION_INCOMPLETE' if failures or sampling_errors else 'PASS',
        'control_identity_safety': 'FAIL' if unsafe else 'PASS',
        'field_coverage': rows, 'opportunities': opportunities,
        'sampler_errors': list(sampling_errors),
        'opportunity_counts': dict(Counter(item['classification'] for item in opportunities))}


def outcomes(*, functional_passed, safety_passed, cleanup_passed, observation,
             resource_limits_passed=True, performance_passed=None, visual_passed=None):
    result = {'functional_result': 'PASS' if functional_passed else 'FAIL',
        'control_identity_safety': 'PASS' if safety_passed else 'FAIL',
        'cleanup_result': 'PASS' if cleanup_passed else 'FAIL',
        'observation_accounting': observation['observation_accounting'],
        'resource_observability': observation['resource_observability'] if resource_limits_passed else 'FAIL',
        'performance_result': 'NOT_ASSESSED' if performance_passed is None else 'PASS' if performance_passed else 'FAIL',
        'visual_result': 'NOT_ASSESSED' if visual_passed is None else 'PASS' if visual_passed else 'FAIL'}
    result['execution_passed'] = all(result[key] == 'PASS' for key in
                                   ('functional_result', 'control_identity_safety', 'cleanup_result'))
    result['qualified'] = all(value in {'PASS', 'NOT_ASSESSED'} for key, value in result.items()
                              if key not in {'execution_passed'})
    return result
