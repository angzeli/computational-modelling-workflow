"""Plain-text progress presentation shared by CLI and optional console widgets."""
from __future__ import annotations

import math
import time
import unicodedata

from .models import ProgressSnapshot, comparable


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


def cell_width(text):
    """Terminal columns without importing the optional console dependencies."""
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
               for char in str(text))


def fit_text(text, width):
    if cell_width(text) <= width:
        return text
    used, result = 0, []
    for char in text:
        size = cell_width(char)
        if used + size > max(0, width - 1):
            break
        result.append(char)
        used += size
    return ''.join(result) + ('…' if width > 0 else '')


def comparison_reason(latest, previous):
    if previous is None:
        return 'No comparable previous observation in this snapshot.'
    if not comparable(latest, previous):
        return 'Not comparable: definition, unit, stage, segment, criterion, source or context differs.'
    if any(value.value is None or not math.isfinite(value.value) for value in (latest, previous)):
        return 'Trend unavailable: a numeric observation is unavailable.'
    return 'Descriptive delta within the adapter comparison scope; no scientific acceptance decision.'


def trend_text(latest, previous):
    """Describe only an already comparable pair; source resolution tempers direction."""
    if previous is None or not comparable(latest, previous):
        return ''
    if any(value.value is None or not math.isfinite(value.value) for value in (latest, previous)):
        return ''
    delta = latest.value - previous.value
    if not math.isfinite(delta):
        return ''
    if delta == 0:
        return '= 0'
    uncertainty = sum(value.precision or 0 for value in (latest, previous))
    if abs(delta) <= uncertainty:
        return f'≈ Δ{delta:+.3g}'
    return ('↑ ' if delta > 0 else '↓ ') + f'{delta:+.3g}'


def _context_key(levels):
    # Display labels and budgets can become more informative without changing identity.
    return tuple((level.kind, level.index) for level in levels)


def _current(metric, snapshot):
    levels, active = _context_key(metric.context), _context_key(snapshot.iteration)
    return (metric.segment == snapshot.segment and metric.stage in (None, snapshot.context.stage, snapshot.calculation)
            and bool(levels) and levels == active[:len(levels)])


def card_series(snapshot, limit):
    """Use adapter priority within current contexts, retaining one historical summary."""
    ordered = sorted(snapshot.metrics, key=lambda item: (item.latest.priority, item.latest.semantic_id))
    current = [item for item in ordered if _current(item.latest, snapshot)]
    historical = [item for item in ordered if item not in current]
    if current and historical and limit > 1:
        return current[:limit - 1] + historical[:1]
    return (current + historical)[:limit]


def _metric_mark(metric):
    if metric is None:
        return ''
    if metric.qualification == 'provisional' or 'PROVISIONAL' in (metric.relation or ''):
        return '*'
    if metric.qualification not in ('observed', 'derived') and not metric.qualification.startswith('native-'):
        return '!'
    return ''


def _position(metric, digits=3):
    value = value_text(metric, compact=True)
    if metric is not None and metric.value is not None and math.isfinite(metric.value):
        value = f'{metric.value:.{digits}g}'
    return value + _metric_mark(metric) + (' @' + '/'.join(str(level.index) for level in metric.context)
                    if metric is not None and metric.context else '')


def _pair_positions(item):
    digits = 3
    latest, previous = item.latest, item.previous
    if (previous is not None and latest.value is not None and previous.value is not None
            and math.isfinite(latest.value) and math.isfinite(previous.value) and latest.value != previous.value):
        while digits < 16 and f'{latest.value:.{digits}g}' == f'{previous.value:.{digits}g}':
            digits += 1
    return _position(latest, digits), _position(previous, digits)



def _target_cell(metric):
    if metric.criterion is None:
        return '—'
    criterion = metric.criterion
    text = f'{criterion.comparator}{criterion.value:g}'
    if criterion.unit != metric.unit:
        text += ' ' + criterion.unit
    if metric.qualification == 'provisional' or 'PROVISIONAL' in (metric.relation or ''):
        text += '*'
    if 'UNCERTAIN' in (metric.relation or ''):
        text += '?'
    if criterion.kind != 'engine-stopping':
        text += ' ' + criterion.kind
    if metric.qualification not in ('observed', 'derived', 'provisional') and not metric.qualification.startswith('native-'):
        text += '!'
    return text


def _metric_label(metric, width=None):
    unit = metric.unit.replace('Angstrom', 'Å')
    suffix = f' [{unit}]' if unit else ''
    label = metric.label if width is None else fit_text(metric.label, max(0, width - cell_width(suffix)))
    return label + suffix



def _compact_metric(item, width):
    metric = item.latest
    latest, previous = _pair_positions(item)
    tail = f'L:{latest} P:{previous}'
    target = f' T:{_target_cell(metric)}'
    trend = trend_text(metric, item.previous)
    # Keep values, context and targets before the optional descriptive trend.
    for delta in (' ' + trend if trend else '', ''):
        label_space = width - cell_width(tail + delta + target) - 2
        if label_space >= min(cell_width(_metric_label(metric)), 18):
            return _metric_label(metric, label_space) + ': ' + tail + delta + target
    return fit_text(_metric_label(metric) + ': ' + tail + target, width)


def _card_duration(timings, width):
    prefix = 'Completed: ' if all(timing.quantity == 'duration' for timing in timings[:2]) else 'Timing: '
    pair = prefix + duration_pair(timings, compact=True)
    if cell_width(pair) <= width:
        return pair
    timing = timings[0]
    position = '@' + '/'.join(str(level.index) for level in timing.context) + ' ' if timing.context else ''
    text = duration_text(timing.value, precision=timing.precision, unit=timing.unit)
    if timing.value is None and timing.lower_bound is not None:
        text = '≥' + duration_text(timing.lower_bound, precision=timing.precision, unit=timing.unit) + ' observed'
    clock = 'CPU' if timing.clock == 'cpu' else timing.clock
    label = 'Completed' if timing.quantity == 'duration' else 'Timing'
    qualification = '' if timing.quantity == 'duration' else ' interval' if timing.quantity == 'observation-interval' else ' ' + timing.quantity
    if timing.completeness not in ('COMPLETE', 'AVAILABLE'):
        qualification += ' (' + timing.completeness.lower().replace('_', ' ') + ')'
    return f'{label}: {position}{text} {clock}/{timing.source}{qualification}'


def _card_text(snapshot, *, compact, width, now, max_lines):
    limit = max(3, max_lines)
    active = 'ACTIVE: ' + (context_text(snapshot.iteration) if snapshot.iteration else snapshot.phase)
    # Phase is retained where it adds context and space permits.
    if snapshot.iteration:
        with_phase = f'ACTIVE: {snapshot.phase} · ' + context_text(snapshot.iteration)
        if not compact or cell_width(with_phase) <= width:
            active = with_phase
    lines = [active]
    current_present = any(_current(item.latest, snapshot) and _context_key(item.latest.context) == _context_key(snapshot.iteration)
                          for item in snapshot.metrics)
    pending = bool(snapshot.iteration and not current_present and
                   not any(_current(item.latest, snapshot) for item in snapshot.metrics))
    if pending:
        if limit >= 5:
            lines.append('Current metrics: — (pending / unavailable)')
        else:
            suffix = ' · metrics pending'
            lines[0] = fit_text(lines[0], max(0, width - cell_width(suffix))) + suffix
    duration = bool(snapshot.completed_durations and limit >= 5)
    header = not compact and width >= 110
    count = max(1, limit - len(lines) - 1 - int(duration) - int(header))
    selected = card_series(snapshot, count)
    if header:
        # The table is intentionally small; Details retains full descriptors and precision.
        label_width = max(24, min(38, width - 85))
        widths = (label_width, 19, 19, 17, max(8, width - label_width - 63))
        def row(values):
            cells = [fit_text(str(value), size) for value, size in zip(values, widths)]
            return '  '.join(value + ' ' * (size - cell_width(value)) for value, size in zip(cells, widths)).rstrip()
        lines.append(row(('METRIC / UNIT', 'LATEST', 'PREVIOUS', 'TARGET', 'DELTA')))
        for item in selected:
            latest, previous = _pair_positions(item)
            lines.append(row((_metric_label(item.latest, label_width), latest, previous,
                              _target_cell(item.latest), trend_text(item.latest, item.previous) or '—')))
    else:
        lines.extend(_compact_metric(item, width) for item in selected)
    if not selected:
        lines.append(snapshot.messages[0] if snapshot.messages else f'{snapshot.status}: {snapshot.completeness}')
    if duration:
        lines.append(_card_duration(snapshot.completed_durations, width))
    qualifications = []
    provisional = [item.latest for item in selected if _metric_mark(item.latest) == '*']
    if provisional:
        qualifications.append('* provisional ' + ('target' if all(value.criterion for value in provisional) else 'evidence'))
    if any('?' in _target_cell(item.latest) for item in selected):
        qualifications.append('? uncertain')
    qualifications.extend(dict.fromkeys('! ' + item.latest.qualification for item in selected
                          if _metric_mark(item.latest) == '!'))
    association = snapshot.context.association
    source_label = ('SOURCE_CONFLICT · session-local' if snapshot.context.source_conflicts else
                    'session-local · unverified' if 'session-local' in association else
                    'user-declared-at-add' if 'user-declared-at-add' in association else
                    'recognized declaration' if snapshot.context.declarations else '')
    parts = ([source_label] if source_label else []) + qualifications + [f'Evidence {age_text(snapshot.last_new_evidence_at, now)}', snapshot.read_health]
    if snapshot.status not in ('OBSERVED', 'OBSERVING', 'PENDING', 'READY'):
        parts.insert(0, snapshot.status)
    if any(source.has_more for source in snapshot.sources):
        parts.insert(0, 'CATCHING UP')
    if not qualifications:
        parts.append('Enter details')
    evidence = ' · '.join(parts)
    if cell_width(evidence) > width and qualifications:
        parts = [part.replace('* provisional target', '* provisional').replace('* provisional evidence', '* provisional')
                 for part in parts]
        evidence = ' · '.join(parts)
    lines.append(evidence)
    return '\n'.join(fit_text(line, width) for line in lines[:-1][:limit-1] + [lines[-1]])


def _unresolved_card(snapshot, *, width, max_lines):
    reasons = tuple(dict.fromkeys((*snapshot.messages, *snapshot.context.limitations, *snapshot.limitations)))
    # The association qualification stays in JSON and Details; reserve the card
    # for actionable failures instead of repeating its generic ownership limit.
    reasons = tuple(reason for reason in reasons
                    if reason != 'Location declarations do not establish process ownership or scientific validity.')
    limit = max(3, max_lines)
    available = limit - 2
    shown = list(reasons[:available])
    heading = 'Scientific: ' + snapshot.context.binding_status
    if len(reasons) > available:
        heading += f' (+{len(reasons) - available}; Enter details)'
    action = ('[S] Review Sources' if snapshot.context.binding_status == 'SOURCE_CONFLICT'
              else '[S] Configure Sources') + ' · Enter details'
    lines = [heading, *shown, action]
    return '\n'.join(fit_text(line, width) for line in lines)


def progress_text(snapshot: ProgressSnapshot, *, compact=False, detailed=False, width=120, now=None, max_lines=None):
    """Render adapter descriptors without engine-specific metric names or rules."""
    now = time.time() if now is None else now
    context = snapshot.context
    if snapshot.status == 'ADAPTER_UNAVAILABLE':
        return 'Progress adapter unavailable' + (f' ({context.engine})' if context.engine else '')
    unresolved = snapshot.status in {'SOURCE_UNRESOLVED', 'SOURCE_CONFLICT'}
    if not detailed:
        limit = max_lines or (3 if width < 54 else 6 if compact else 11)
        if unresolved and not snapshot.metrics:
            return _unresolved_card(snapshot, width=width, max_lines=limit)
        if snapshot.card_groups:
            from .card import card_text
            card = card_text(snapshot, width=width, now=now, max_lines=limit)
        else:
            card = _card_text(snapshot, compact=compact, width=width, now=now, max_lines=limit)
        if unresolved and not snapshot.card_groups:
            lines = card.splitlines()
            lines[0] = fit_text('Cached scientific: ' + snapshot.status, width)
            card = '\n'.join(lines)
        return card
    heading = 'SCIENTIFIC PROGRESS · ' + snapshot.status if unresolved else f'PROGRESS · {snapshot.phase}'
    if snapshot.calculation:
        heading += f' · {snapshot.calculation}'
    if context.stage:
        heading += f' · {context.stage}'
    if snapshot.iteration:
        heading += '\nACTIVE: ' + context_text(snapshot.iteration)
    series = sorted(snapshot.metrics, key=lambda item: (item.latest.priority, item.latest.semantic_id))
    source = (f'Source: {context.association}; new evidence {age_text(snapshot.last_new_evidence_at, now)}; '
              f'{snapshot.read_health} / {snapshot.completeness}')
    lines = [heading, 'METRIC / NATIVE UNIT                 LATEST                PREVIOUS              TARGET']
    for item in series:
        metric = item.latest
        label = metric.label + (f' [{metric.unit}]' if metric.unit else '')
        lines.append(f'{label:<34} {metric_text(metric, concise=not detailed):<21} {metric_text(item.previous, concise=not detailed):<21} {target_text(metric)}')
    if snapshot.completed_durations:
        lines.append('Completed evaluation timing · latest ← previous: ' + duration_pair(snapshot.completed_durations))
    lines.extend(dict.fromkeys(snapshot.messages))
    if unresolved:
        lines.append('Action: [S] Sources in the dashboard, or cmw jobs --state STATE progress JOB --source ROLE=/absolute/FILE. '
                     'Use the same Jobs state and attempt. Explicit sources apply only to that invocation; '
                     'they do not update the dashboard binding.')
    lines.append(source)
    if any(state.has_more for state in snapshot.sources):
        lines.insert(0, 'CATCHING UP · bounded historical reads; current evidence may be newer')
    if detailed:
        lines.extend((f'Adapter: {snapshot.adapter}; status: {snapshot.status}; stage: {context.stage or "unknown"}; segment: {snapshot.segment}',
                      f'Binding: {context.binding_id}; generation: {context.binding_generation}; attempt: {context.attempt_id or "not a Jobs attempt"}',
                      f'Last successful observation: {age_text(snapshot.last_success_at, now)}; last file growth: {age_text(snapshot.last_file_growth_at, now)}',
                      'Observation only; process ownership: false; scientific validation: not assessed.'))
        for setting in snapshot.settings:
            lines.append(f'Setting {setting.name}: {setting.value}; {setting.qualification}; origin: {setting.origin}; '
                         f'compared input: {setting.compared_value}; discrepancy: {setting.discrepancy}')
        for check in snapshot.verification:
            lines.append(f'Verification {check.domain}: {check.status}; {check.source_role} generation {check.source_generation} '
                         f'vs {check.counterpart_role} generation {check.counterpart_generation}; '
                         f'observed {check.observed_frontier}; checked {check.checked_frontier}; native {check.native_frontier}; '
                         f'context: {context_text(check.context)}; fields: {", ".join(check.fields)}; '
                         f'comparison: {check.precision_policy}; {check.reason}')
        for declaration in context.declarations:
            lines.append(f'Declaration {declaration.role}: {declaration.path}; stage: {declaration.stage or "unknown"}; '
                         f'origin: {", ".join(declaration.origins)}; {declaration.qualification}')
        for conflict in context.source_conflicts:
            lines.append('SOURCE_CONFLICT: ' + conflict)
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
            lines.append('Comparison: ' + comparison_reason(metric, item.previous))
            if metric.criterion:
                lines.append(f'Engine criterion: {target_text(metric)}; {metric.criterion.origin}; {metric.criterion.definition}')
            for target in metric.diagnostic_targets:
                lines.append(f'Caller diagnostic target: {target.comparator} {target.value:g} {target.unit}; {target.origin}; {target.definition}')
        for timing in snapshot.completed_durations:
            lines.append(f'Timing {timing.label}: {timing_text(timing)}; raw value {timing.value} {timing.unit}; '
                         f'printed resolution {timing.precision}; source {timing.source}; {timing.reason}')
            for evidence in dict.fromkeys((*timing.evidence, *timing.start_evidence, *timing.end_evidence)):
                lines.append(f'  {evidence.source_role}: {evidence.path} · generation {evidence.generation} · '
                             f'lines {evidence.line_start}–{evidence.line_end} · bytes {evidence.byte_start}–{evidence.byte_end}')
        for event in snapshot.events:
            lines.append(f'Evidence: {event.kind}: {event.message} ({context_text(event.context, budget=False)})')
        for state in snapshot.sources:
            lines.append(f'Source {state.role}: {state.path}; {state.health}; generation {state.generation}; offset {state.offset}; '
                         f'pending bytes {state.pending_bytes}; catch-up {state.has_more}; poll bytes {state.bytes_read}')
        lines.extend('Limitation: ' + item for item in dict.fromkeys(context.limitations + snapshot.limitations)
                     if item not in snapshot.messages)
    return '\n'.join(lines)


def duration_text(value, *, precision=None, unit='s'):
    """Format elapsed values with unbounded hours; unknown/coarse precision is approximate."""
    if value is None:
        return 'unavailable'
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 'unavailable (invalid duration)'
    if unit != 's':
        return f'{value:g} {unit}'
    known = precision is not None and math.isfinite(precision) and precision > 0
    digits = 0
    # Source precision conventions include both a last place and a half-last-place.
    # Never print more fractional digits than either convention supports.
    if known:
        digits = min(6, max(0, int(-math.log10(precision))))
    rounded = round(value, digits)
    whole = int(rounded)
    hours, rest = divmod(whole, 3600)
    minutes, seconds = divmod(rest, 60)
    fractional = f'{rounded - whole:.{digits}f}'[1:] if digits else ''
    text = f'{hours:02d}:{minutes:02d}:{seconds:02d}' + fractional
    approximate = not known or precision > 1 or abs(rounded - value) > max(1e-12, abs(value) * 1e-15)
    return ('~' if approximate else '') + text


def timing_text(timing, *, compact=False):
    """Keep measured clock, provenance and unavailable values explicit."""
    if timing is None:
        return 'unavailable'
    if timing.value is None:
        value = (f'≥{duration_text(timing.lower_bound, precision=timing.precision, unit=timing.unit)} observed'
                 if timing.lower_bound is not None else 'unavailable')
    else:
        value = duration_text(timing.value, precision=timing.precision, unit=timing.unit)
    clock = 'CPU' if timing.clock == 'cpu' else timing.clock
    if compact and timing.value is not None and timing.quantity == 'duration' and timing.completeness in ('COMPLETE', 'AVAILABLE'):
        return f'{value} {clock}/{timing.source}'
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
                      f'Raw timing: {timing.value} {timing.unit}; source {timing.source}; printed resolution {timing.precision}',
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
