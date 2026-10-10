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
        if snapshot.completed_durations and width >= 54:
            lines[-1:] = ['Completed duration: ' + duration_pair(snapshot.completed_durations, compact=True)]
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
    if snapshot.completed_durations:
        lines.append('Completed evaluation duration · latest ← previous: ' + duration_pair(snapshot.completed_durations))
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


def timing_text(timing, *, compact=False):
    """Keep measured clock, provenance and unavailable values explicit."""
    if timing is None:
        return 'unavailable'
    if timing.value is None:
        value = (f'≥{timing.lower_bound:g} {timing.unit} observed' if timing.lower_bound is not None
                 else 'unavailable')
    else:
        value = f'{timing.value:.4g} {timing.unit}'
    clock = 'CPU' if timing.clock == 'cpu' else timing.clock
    if compact and timing.value is not None and timing.quantity == 'duration' and timing.completeness in ('COMPLETE', 'AVAILABLE'):
        return f'{timing.value:.4g}{timing.unit} {clock}/{timing.source}'
    suffix = f'{clock}, {timing.source}'
    if timing.quantity != 'duration':
        suffix += ', ' + timing.quantity
    if timing.completeness not in ('COMPLETE', 'AVAILABLE'):
        suffix += ', ' + timing.completeness.lower().replace('_', ' ')
    return value + ' (' + suffix + ')'


def duration_pair(timings, *, compact=False):
    values = [timing_text(value, compact=compact) + (' @' + '/'.join(str(level.index) for level in value.context) if value.context else '')
              for value in timings[:2]]
    if len(values) == 1:
        values.append('unavailable')
    return ' ← '.join(values)


def history_cell(entry, column):
    """Adapter descriptors select fields; this function knows no engine grammar."""
    if column.kind == 'metric' or column.metric_id is not None:
        metric = next((value for value in entry.metrics if value.semantic_id == column.metric_id), None)
        return value_text(metric, compact=True)
    if column.kind == 'timing':
        timing = next((value for value in entry.timings if value.timing_id == column.key), None)
        if timing is None:
            for quantity in ('duration', 'observation-interval', 'cumulative'):
                timing = next((value for value in entry.timings if value.clock in ('wall', 'monotonic') and value.quantity == quantity), None)
                if timing is not None:
                    break
        return timing_text(timing, compact=True)
    if column.kind in ('count', 'children'):
        return str(entry.child_count)
    if column.kind == 'native':
        return entry.native_label
    if column.kind == 'context':
        return context_text(entry.context)
    if column.kind == 'state':
        return entry.state
    if column.kind == 'evidence':
        return '; '.join(entry.annotations) or ', '.join(dict.fromkeys(value.source_role for value in entry.evidence)) or 'unavailable'
    value = getattr(entry, column.key, None)
    if value is None:
        return '—'
    if isinstance(value, (tuple, list)):
        return '; '.join(str(part) for part in value) or '—'
    return str(value)


def history_columns(page):
    if page.columns:
        kinds = {entry.kind for entry in page.entries}
        return tuple(column for column in page.columns if not kinds or not column.kinds or kinds.intersection(column.kinds))
    from .models import HistoryColumn
    return (HistoryColumn('native_label', 'CONTEXT'), HistoryColumn('state', 'STATE'),
            HistoryColumn('child_count', 'CHILDREN', kind='count'))


def history_entry_text(entry):
    lines = [f'{entry.native_label or entry.kind} · {entry.state} / {entry.coverage}',
             context_text(entry.context),
             f'Kind: {entry.kind}; sequence: {entry.sequence}',
             f'Entry: {entry.entry_id}; parent: {entry.parent_id or "root"}',
             f'Attempt: {entry.attempt_id or "not a Jobs attempt"}; binding: {entry.binding_id}',
             f'Stage: {entry.stage or "undeclared"}; run segment: {entry.segment}; children: {entry.child_count}',
             'Observation only; scientific validation: not assessed.']
    for metric in entry.metrics:
        lines.extend((f'\n{metric.label}: {value_text(metric)} {metric.unit} · {metric.qualification}',
                      f'{metric.semantic_id}: {metric.definition}',
                      f'Criterion: {target_text(metric)}',
                      f'Printed token: {metric.printed}; signed: {metric.signed_value}; precision: {metric.precision}'))
        if metric.criterion:
            lines.append(f'Criterion origin: {metric.criterion.origin}; {metric.criterion.definition}')
        for target in metric.diagnostic_targets:
            lines.append(f'Diagnostic target: {target.comparator} {target.value:g} {target.unit}; {target.origin}')
    for timing in entry.timings:
        lines.extend((f'\n{timing.label}: {timing_text(timing)}',
                      f'{timing.timing_id}; scope: {context_text(timing.context)}',
                      f'Clock identity: {timing.clock_id or "unavailable"}; bounds: {timing.lower_bound}–{timing.upper_bound}',
                      'Timing qualification: ' + (timing.reason or timing.completeness)))
        for label, evidence in (('start', timing.start_evidence), ('end', timing.end_evidence)):
            for source in evidence:
                lines.append(f'Timing {label}: {source.source_role} lines {source.line_start}–{source.line_end}')
    evidence = tuple(dict.fromkeys((*entry.evidence,
        *(source for metric in entry.metrics for source in metric.evidence),
        *(source for timing in entry.timings for source in (*timing.evidence, *timing.start_evidence, *timing.end_evidence)))))
    lines.append('\nSource references:')
    for source in evidence:
        lines.append(f'{source.source_role}: {source.path} · generation {source.generation} · '
                     f'lines {source.line_start}–{source.line_end} · bytes {source.byte_start}–{source.byte_end}')
    lines.extend(entry.annotations)
    return '\n'.join(lines)


def history_text(page):
    columns = history_columns(page)
    lines = [f'PROGRESS HISTORY · {page.status} / {page.coverage}',
             ' | '.join(column.label for column in columns)]
    for entry in page.entries:
        lines.append(' | '.join(history_cell(entry, column) for column in columns)
                     + f' [{entry.state}] · {entry.entry_id}')
    if not page.entries:
        lines.append('No history entries in this page; ' + ('bounded reconstruction is loading.' if page.status == 'LOADING' else 'await native evidence or choose another context.'))
    lines.extend((f'Page entries: {len(page.entries)}; retained: {page.retained}; total: {page.total if page.total is not None else "unknown"}; coverage: {page.coverage}',
                  f'Current cursor: {page.cursor or "start"}', f'Next cursor: {page.next_cursor or "none"}',
                  f'Previous cursor: {page.previous_cursor or "none"}', 'Source revision: ' + page.source_revision))
    lines.extend(page.messages)
    lines.extend('Limitation: ' + message for message in page.limitations)
    return '\n'.join(lines)
