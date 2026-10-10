"""Plain-text progress presentation shared by CLI and optional console widgets."""
from __future__ import annotations

import math
import time

from .models import ProgressSnapshot


def context_text(levels, *, budget=True):
    parts = []
    for level in levels:
        label = str(level.label)
        index = str(level.index)
        text = label if index in label.split() else f'{label} {index}'
        if budget and level.budget is not None:
            text += f' / {level.budget} allowed'
        parts.append(text)
    return ' → '.join(parts) or 'context unavailable'


def age_text(stamp, now):
    if stamp is None:
        return 'unknown'
    age = max(0, now - stamp)
    return f'{age:.0f}s ago' if age < 60 else f'{age / 60:.1f}m ago'


def value_text(metric, *, compact=False):
    if metric is None or metric.value is None:
        return '—'
    if not math.isfinite(metric.value):
        return 'unavailable'
    if metric.printed and not compact:
        try:
            printed_value = float(metric.printed.replace('D', 'E').replace('d', 'e'))
            if printed_value == metric.value:
                return metric.printed
            if abs(printed_value) == metric.value:
                return metric.printed.lstrip('+-')
        except ValueError:
            pass
    # Compact cards round display only; the snapshot and Details retain native precision.
    return f'{metric.value:.3g}' if compact else f'{metric.value:.6g}'


def target_text(metric, *, compact=False):
    criterion = metric.criterion
    if criterion is None:
        return '—'
    unit = '' if compact and criterion.unit == metric.unit else criterion.unit
    text = f'{criterion.comparator} {criterion.value:g} {unit}'.rstrip()
    if criterion.kind != 'engine-stopping':
        text += f' ({criterion.kind})'
    if compact:
        if metric.relation and 'UNCERTAIN' in metric.relation:
            text = 'uncertain ' + text
        if metric.qualification == 'provisional' or metric.relation and 'PROVISIONAL' in metric.relation:
            text = 'provisional ' + text
    elif metric.relation:
        text += f' [{metric.relation}]'
    return text


def metric_text(metric, *, concise=False):
    if metric is None:
        return '—'
    position = '/'.join(str(level.index) for level in metric.context) if concise else context_text(metric.context, budget=False)
    return value_text(metric) + (' @' + position if metric.context else '')


def progress_text(snapshot: ProgressSnapshot, *, compact=False, detailed=False, width=120, now=None):
    """Render adapter descriptors without engine-specific metric names or rules."""
    now = time.time() if now is None else now
    context = snapshot.context
    if snapshot.status == 'ADAPTER_UNAVAILABLE':
        return 'Progress adapter unavailable' + (f' ({context.engine})' if context.engine else '')
    if snapshot.status == 'SOURCE_UNRESOLVED':
        reason = '; '.join(snapshot.messages or context.limitations or snapshot.limitations)
        return 'Progress: SOURCE_UNRESOLVED' + (f' — {reason}' if reason else '')
    heading = f'PROGRESS · {snapshot.phase}'
    if snapshot.calculation:
        heading += f' · {snapshot.calculation}'
    if context.stage:
        heading += f' · {context.stage}'
    if snapshot.iteration:
        heading += '\nACTIVE: ' + context_text(snapshot.iteration)
    series = sorted(snapshot.metrics, key=lambda item: (item.latest.priority, item.latest.semantic_id))
    source = (f'Source: {context.association}; new evidence {age_text(snapshot.last_new_evidence_at, now)}; '
              f'{snapshot.read_health} / {snapshot.completeness}')
    if compact and not detailed:
        active = (snapshot.phase + ': ' + context_text(snapshot.iteration) if snapshot.iteration else heading)
        # Keep the main table and annotations visible. Enter retains every metric,
        # previous value, criterion and source location from this same snapshot.
        lines = ['Progress: ' + snapshot.phase if width < 54 else active]
        for item in series[:1 if width < 54 else 3]:
            metric = item.latest
            unit = f' [{metric.unit}]' if metric.unit else ''
            position = lambda value: value_text(value, compact=True) + (' @' + '/'.join(str(level.index) for level in value.context) if value and value.context else '')
            lines.append(f'{metric.label}{unit}: {position(metric)} ← {position(item.previous)} target {target_text(metric, compact=True)}')
        if not series:
            lines.append(snapshot.messages[0] if snapshot.messages else f'{snapshot.status}: {snapshot.completeness}')
        lines.append((f'Evidence {age_text(snapshot.last_new_evidence_at, now)} · Enter details' if width < 54 else
                      f'Evidence {age_text(snapshot.last_new_evidence_at, now)} · {snapshot.read_health} · Enter: contexts / targets / sources'))
        if any(source.has_more for source in snapshot.sources):
            lines[-1] = 'CATCHING UP · bounded historical reads · Enter: sources'
        return '\n'.join(line if len(line) <= width else line[:max(1, width - 1)] + '…' for line in lines)
    lines = [heading, 'METRIC / NATIVE UNIT                 LATEST                PREVIOUS              TARGET']
    for item in series if detailed else series[:6]:
        metric = item.latest
        label = metric.label + (f' [{metric.unit}]' if metric.unit else '')
        lines.append(f'{label:<34} {metric_text(metric, concise=not detailed):<21} {metric_text(item.previous, concise=not detailed):<21} {target_text(metric)}')
    lines.extend(snapshot.messages if detailed else snapshot.messages[:1])
    lines.append(source)
    if any(state.has_more for state in snapshot.sources):
        lines.insert(0, 'CATCHING UP · bounded historical reads; current evidence may be newer')
    if detailed:
        lines.extend((f'Adapter: {snapshot.adapter}; status: {snapshot.status}; stage: {context.stage or "unknown"}; segment: {snapshot.segment}',
                      f'Binding: {context.binding_id}; attempt: {context.attempt_id or "not a Jobs attempt"}',
                      f'Last successful observation: {age_text(snapshot.last_success_at, now)}; last file growth: {age_text(snapshot.last_file_growth_at, now)}',
                      'Observation only; process ownership: false; scientific validation: not assessed.'))
        for item in series:
            metric = item.latest
            lines.append(f'\n{metric.semantic_id}: {metric.definition}; {metric.qualification}')
            for name, value in (('Latest', metric), ('Previous', item.previous)):
                if value is None:
                    continue
                lines.append(f'{name}: {metric_text(value)} {value.unit}; source age {age_text(value.observed_at, now)}; '
                             f'printed resolution {value.precision if value.precision is not None else "unknown"}; signed value {value.signed_value}')
                for evidence in value.evidence:
                    lines.append(f'  {evidence.source_role}: {evidence.path} · generation {evidence.generation} · '
                                 f'lines {evidence.line_start}–{evidence.line_end} · bytes {evidence.byte_start}–{evidence.byte_end}')
            if metric.criterion:
                lines.append(f'Engine criterion: {target_text(metric)}; {metric.criterion.origin}; {metric.criterion.definition}')
            for target in metric.diagnostic_targets:
                lines.append(f'Caller diagnostic target: {target.comparator} {target.value:g} {target.unit}; {target.origin}; {target.definition}')
        for event in snapshot.events:
            lines.append(f'Evidence: {event.kind}: {event.message} ({context_text(event.context, budget=False)})')
        for state in snapshot.sources:
            lines.append(f'Source {state.role}: {state.path}; {state.health}; generation {state.generation}; offset {state.offset}; '
                         f'pending bytes {state.pending_bytes}; catch-up {state.has_more}; poll bytes {state.bytes_read}')
        lines.extend('Limitation: ' + item for item in dict.fromkeys(context.limitations + snapshot.limitations))
    return '\n'.join(lines)
