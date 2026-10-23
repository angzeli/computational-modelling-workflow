"""Frozen, dependency-free handoff snapshots from already observed client data."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
import time
import sys
import unicodedata


_ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))')
_JOB_FIELDS = ('id', 'display_id', 'name', 'attempt_id', 'engine', 'status',
               'enqueued_at', 'started_at', 'finished_at', 'elapsed', 'reason',
               'usage', 'resources', 'suspension', 'cwd')
_EXCLUDED_KEYS = {'argv', 'env', 'environ', 'environment', 'cmdline', 'command', 'coordinates', 'stdout', 'stderr', 'log_tail', 'secret', 'token', 'password'}
_EXECUTION_FIELDS = ('schema', 'advisory', 'job_id', 'attempt_id', 'persisted_status', 'observed_execution',
                     'observation_quality', 'observed_at', 'age_seconds', 'stale', 'state_identity',
                     'evidence', 'limitations', 'reconciliation_required', 'summary')
_MACHINE_FIELDS = ('cpu_percent', 'cpu_quality', 'ram_used_bytes', 'ram_total_bytes',
                   'ram_available_bytes', 'memory_source', 'memory_semantics', 'memory_quality',
                   'swap_used_bytes', 'swap_total_bytes', 'memory_categories_bytes',
                   'observed_at', 'age_seconds', 'stale', 'reason')
_THERMAL_FIELDS = ('temperature_celsius', 'temperature_kind', 'source', 'units',
                   'sample_timestamp', 'sample_age', 'quality', 'availability_reason',
                   'freshness_seconds', 'unavailable_seconds', 'provider_version',
                   'provider_status', 'observation_status', 'failure_category',
                   'consecutive_failures', 'successful_samples', 'retry_after_seconds', 'retry_guidance',
                   'native_timestamp', 'invocation_started_at', 'received_at', 'invocation_elapsed_seconds')
_THERMAL_NESTED_FIELDS = {
    'last_successful_observation': ('temperature_celsius', 'sample_timestamp', 'sample_age'),
    'provider_identity': ('device', 'inode', 'size', 'modified_ns', 'changed_ns', 'owner_uid', 'group_gid', 'mode', 'sha256'),
}
_PATH_FIELDS = {'path', 'root_path', 'jobs_state', 'state_directory', 'cwd', 'executable', 'module', 'root', 'database'}


def safe_snapshot_text(value):
    """Strip terminal controls; keep literal labels and readable Unicode."""
    text = _ANSI.sub('', str(value))
    return ''.join(char for char in text if char in '\n\t' or unicodedata.category(char) not in {'Cc', 'Cf', 'Cs'})


def _clean(value):
    if isinstance(value, str):
        return safe_snapshot_text(value)
    if isinstance(value, dict):
        return {safe_snapshot_text(key): _clean(item) for key, item in value.items()
                if str(key).lower() not in _EXCLUDED_KEYS}
    if isinstance(value, (tuple, list)):
        return [_clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class HandoffSnapshot:
    """Canonical JSON owns a deep immutable copy, including arbitrary nested values."""
    payload: str

    def to_dict(self):
        return json.loads(self.payload)


def capture_snapshot(job, *, progress=None, execution=None, captured_at=None,
                     runtime=None, read_error=None, execution_error=None, machine_usage=None):
    from .suspension_presentation import effective_status
    now = time.time() if captured_at is None else captured_at
    if runtime is None:
        from importlib.metadata import PackageNotFoundError, version
        try:
            package_version = version('computational-modelling-workflow')
        except PackageNotFoundError:
            package_version = 'unavailable'
        runtime = {'version': package_version, 'python': sys.version.split()[0],
                   'executable': sys.executable, 'module': __file__}
    if progress is not None and (progress.context.attempt_id != job['attempt_id']
            or progress.context.job_id != job['id']):
        raise ValueError('Scientific snapshot belongs to a different attempt')
    if execution is not None and execution.get('attempt_id') not in (None, job['attempt_id']):
        raise ValueError('Execution snapshot belongs to a different attempt')
    payload = {'schema': 1, 'captured_at': now,
               'captured_timestamp': datetime.fromtimestamp(now, timezone.utc).isoformat(),
               'capture_scope': 'Coherent captured client snapshot; component observations are not atomic.',
               'job': {key: job.get(key) for key in _JOB_FIELDS if key in job},
               'effective_status': effective_status(job),
               'execution': {key: execution[key] for key in _EXECUTION_FIELDS if key in execution} if execution else None,
               'execution_error': execution_error,
               'progress': progress.to_dict() if progress is not None else None,
               'progress_error': read_error,
               'runtime': {key: runtime[key] for key in ('version', 'python', 'executable', 'module')
                           if runtime and key in runtime},
               'scientific_acceptance': 'not assessed'}
    if machine_usage is not None:
        from .thermal import age_thermal
        thermal = age_thermal(machine_usage.get('thermal'), now=0.)
        payload['machine_usage'] = {key: machine_usage[key] for key in _MACHINE_FIELDS if key in machine_usage}
        payload['machine_usage']['thermal'] = {key: thermal[key] for key in _THERMAL_FIELDS if key in thermal}
        for key, fields in _THERMAL_NESTED_FIELDS.items():
            value = thermal.get(key)
            if isinstance(value, dict):
                payload['machine_usage']['thermal'][key] = {name: value[name] for name in fields if name in value}
            elif key in thermal:
                payload['machine_usage']['thermal'][key] = None
    return HandoffSnapshot(json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False, sort_keys=True))


def _redacted(data):
    paths = set()
    def find(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _PATH_FIELDS and isinstance(item, str) and item.startswith('/'):
                    paths.add(item)
                find(item)
        elif isinstance(value, list):
            for item in value:
                find(item)
    find(data)
    ordered = sorted(paths, key=lambda path: (-len(path), path))
    replacements = {path: f'<path {index + 1}>' for index, path in enumerate(ordered)}
    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str):
            for path in ordered:
                value = value.replace(path, replacements[path])
        return value
    return replace(data)


def _context(levels):
    return ' → '.join(str(item.get('label') or item.get('kind', 'context')) +
                      (' ' + str(item['index']) if str(item.get('index')) not in str(item.get('label', '')).split() else '') +
                      (f" / {item['budget']} allowed" if item.get('budget') is not None else '')
                      for item in levels) or 'unavailable'


def _value(metric):
    value = metric.get('value')
    if value is None:
        return 'unavailable'
    printed = metric.get('printed')
    if printed:
        try:
            native = float(printed.replace('D', 'E').replace('d', 'e'))
            if native == value:
                return printed
            if abs(native) == value:
                return printed.lstrip('+-')
        except ValueError:
            pass
    return str(value)


def render_snapshot(snapshot, *, mode='summary', redact_paths=False):
    """Render the same captured facts without terminal-width truncation or I/O."""
    if mode not in {'summary', 'diagnostics', 'json'}:
        raise ValueError('Snapshot mode must be summary, diagnostics or json')
    data = snapshot.to_dict()
    if redact_paths:
        data = _redacted(data)
    if mode == 'json':
        return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False)
    job = data['job']
    lines = [f"CMW snapshot · {job.get('display_id')} / {job.get('name')}",
             f"Attempt: {job.get('attempt_id')} · internal job {job.get('id')} · Engine: {job.get('engine')}",
             f"Captured: {data['captured_timestamp']} (UTC)",
             data['capture_scope'],
             f"Execution: stored {job.get('status')} · effective {data['effective_status']}"]
    execution = data.get('execution')
    if execution:
        lines.append('Operational observation: ' + str(execution.get('summary', 'summary unavailable')))
        lines.append('Operational quality: ' + str(execution.get('observation_quality', 'unavailable'))
                     + ' · observed_at ' + str(execution.get('observed_at'))
                     + ' · ' + str(execution.get('observed_execution', 'unknown')))
        lines.extend('Operational scope: ' + item for item in execution.get('limitations', []))
    if data.get('execution_error'):
        lines.append('Execution read error: ' + data['execution_error'])
    if not data.get('execution'):
        lines.append('Operational scope: unavailable / not freshly verified; stored state is not execution proof.')
    if job.get('usage'):
        from .cli import usage_detail
        lines.append(usage_detail(job['usage']))
        lines.append('Usage observed_at: ' + str(job['usage'].get('observed_at')))
    if data.get('machine_usage') is not None:
        from .cli import machine_detail
        lines.extend(['Host telemetry (independent observations):', machine_detail(data['machine_usage'])])
    progress = data.get('progress')
    if progress is None:
        lines.append('Scientific observations: unavailable.')
    else:
        lines += [f"Scientific status: {progress['status']} · {progress['read_health']} · {progress['completeness']}",
                  f"Observed active context: {_context(progress['iteration'])} · segment {progress['segment']} · calculation {progress.get('calculation') or 'unknown'}"]
        for diagnostic in progress.get('source_diagnostics', []):
            lines.append(f"Source parser {diagnostic['role']}: {diagnostic['status']} · "
                         f"parsed lines {diagnostic['parsed_lines']} · latest evaluation complete rows {diagnostic['complete_rows']} · "
                         f"usable prefix {diagnostic['usable_rows']} · latest SCF {diagnostic.get('latest_iteration')} · "
                         f"last complete row observed {diagnostic.get('last_complete_row_at')} · {diagnostic['reason']}")
        for source in progress.get('sources', []):
            lines.append(f"Source clock {source['role']}: attempt {source.get('last_attempt_at')} · "
                         f"success {source.get('last_success_at')} · growth {source.get('last_growth_at')} · "
                         f"bytes {source.get('bytes_read')} · total bytes {source.get('total_bytes_read')}")
        for check in progress.get('verification', []):
            lines.append(f"Verification: {check['domain']} · {check['status']} · source {check.get('source_role')} "
                         f"through {check.get('observed_frontier')} · counterpart {check.get('counterpart_role')} "
                         f"checked through {check.get('checked_frontier')} · native frontier {check.get('native_frontier')} "
                         f"· @{_context(check.get('context', []))} · {check.get('reason', '')}")
            lines.append('Compared fields: ' + (', '.join(check.get('fields', [])) or 'none')
                         + ' · precision: ' + (check.get('precision_policy') or 'not compared'))
        for setting in progress.get('settings', []):
            line = f"Setting {setting['name']}: {setting['value']} · {setting['qualification']} · {setting['origin']}"
            if setting.get('compared_value') is not None:
                line += f" · compared input {setting['compared_value']} · discrepancy {setting['discrepancy']}"
            lines.append(line)
        for event in progress.get('events', []):
            if event.get('message'):
                lines.append(f"Observation event ({event['kind']}): {event['message']}")
        for series in progress.get('metrics', []):
            metric = series['latest']
            line = f"{metric['label']}: {_value(metric)} {metric['unit']} @{_context(metric['context'])}"
            line += f" · stage {metric.get('stage') or 'unspecified'} · segment {metric['segment']} · {metric['qualification']}"
            if metric.get('signed_value') is not None:
                line += f" · signed {metric['signed_value']}"
            if metric.get('observed_at') is not None:
                line += f" · observed_at {metric['observed_at']}"
            criterion = metric.get('criterion')
            if criterion:
                line += f" · Target {criterion['comparator']} {criterion['value']} {criterion['unit']} ({criterion['kind']}; {criterion['origin']})"
                if criterion.get('definition'):
                    line += '; ' + criterion['definition']
            if metric.get('relation'):
                line += ' · ' + metric['relation']
            lines.append(line)
        for key in ('last_observation_at', 'last_structural_evidence_at', 'last_correspondence_at',
                    'last_new_evidence_at', 'last_source_change_at', 'last_file_growth_at', 'last_success_at'):
            stamp = progress.get(key)
            age = 'unknown' if stamp is None else f"{max(0, data['captured_at'] - stamp):g}s ago"
            lines.append(f'{key}: {stamp} ({age})')
        lines.extend('Qualification: ' + message for message in (*progress.get('messages', []), *progress.get('limitations', []),
                                                                *progress.get('context', {}).get('limitations', [])))
    if data.get('progress_error'):
        lines.append('Read unavailable; cached evidence: ' + data['progress_error'])
    lines.append('Scientific acceptance: not assessed.')
    if mode == 'diagnostics':
        lines += ['', 'DIAGNOSTICS · paths may be sensitive; use path redaction before sharing.',
                  json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False)]
    return '\n'.join(lines)
