"""Cell-aware scientific cards driven only by adapter presentation descriptors."""
from __future__ import annotations

from .formatting import age_text, cell_width, context_text, fit_text, value_text


def _metric(cell, snapshot):
    by_id = {item.latest.semantic_id: item.latest for item in snapshot.metrics}
    return next((by_id[key] for key in cell.metric_ids if key in by_id), None)


def _reference(cell, metric):
    if not cell.show_context or not metric.context:
        return ''
    labels = dict(cell.context_labels)
    levels = tuple(level for level in metric.context if not cell.context_kinds or level.kind in cell.context_kinds)
    return (' @' + '/'.join(labels.get(level.kind, level.kind) + str(level.index) for level in levels)) if levels else ''


def _provisional_target(metric, snapshot):
    return (metric.qualification == 'provisional' or 'PROVISIONAL' in (metric.relation or '')
            or metric.criterion is not None and any(setting.qualification == 'provisional'
                and setting.origin == metric.criterion.origin for setting in snapshot.settings))


def _cell(cell, snapshot, width):
    if cell.kind == 'text':
        return fit_text(cell.text, width)
    if cell.kind == 'context':
        levels = tuple(level for level in snapshot.iteration if not cell.context_kinds or level.kind in cell.context_kinds)
        return fit_text(context_text(levels) if levels else cell.text or 'Context unavailable', width)
    metric = _metric(cell, snapshot)
    if cell.kind == 'target':
        if metric is None or metric.criterion is None:
            return fit_text(cell.text or 'Target unavailable', width)
        target = metric.criterion
        text = f'{cell.label or "Target"} {target.comparator}{target.value:g} {target.unit.replace("Angstrom", "Å")}'
        if _provisional_target(metric, snapshot):
            text += '*'
        if 'UNCERTAIN' in (metric.relation or ''):
            text += '?'
        if target.kind != 'engine-stopping':
            text += ' ' + target.kind
        return fit_text(text, width)
    if metric is None:
        return fit_text(cell.text or f'{cell.label or "Value"} —', width)
    label = cell.label or metric.label
    # Units and complete-row labels take precedence over verbose metric labels.
    suffix = value_text(metric, compact=True)
    if metric.qualification == 'current-input-derived':
        suffix += '*'
    if metric.unit and metric.unit != 'native':
        unit = 'unit?' if metric.unit == 'unspecified' else metric.unit.replace('Angstrom', 'Å')
        suffix += ' ' + unit
    suffix += _reference(cell, metric)
    room = max(0, width - cell_width(suffix) - 1)
    label_width = min(room, max(8, cell_width(label)))
    shown_label = fit_text(label, label_width)
    text = shown_label + ' ' * (label_width - cell_width(shown_label) + 1) + suffix
    return fit_text(text, width)


def verification_summary(snapshot, *, width, now):
    """Keep checked scope, newer source-local evidence and data age distinct."""
    parts = []
    conflicts = [item for item in snapshot.verification if item.status == 'CONFLICT']
    checks = conflicts or list(snapshot.verification)
    if checks:
        source = checks[0].source_role or 'source'
        if conflicts:
            parts.append(source + ' · CONFLICT ' + '/'.join(dict.fromkeys(
                item.counterpart_role or 'counterpart' for item in conflicts)))
        else:
            frontiers = [item.observed_frontier for item in checks if item.observed_frontier is not None]
            observed = max(frontiers) if frontiers else None
            # Adapters order the primary correspondence check before optional
            # duplicate-stream checks; an empty duplicate cannot undo a match.
            primary = checks[0]
            source_only = primary.checked_frontier is None or (observed is not None and primary.checked_frontier < observed)
            parts.append(source + (f' {observed}' if observed is not None and source_only else '')
                         + (' source-only' if source_only else ''))
            for check in checks:
                counterpart = check.counterpart_role or 'counterpart'
                if check.checked_frontier is not None:
                    parts.append(f'{counterpart} matched {check.checked_frontier}')
                elif check.status == 'UNAVAILABLE':
                    parts.append(f'{counterpart} unavailable')
                elif check.status in ('UNSUPPORTED', 'UNRESOLVED'):
                    parts.append(f'{counterpart} {check.status.lower()}')
                else:
                    parts.append(f'{counterpart} pending')
    else:
        parts.append('source observation')
    if snapshot.context.source_conflicts or snapshot.status in ('SOURCE_CONFLICT', 'SOURCE_UNRESOLVED', 'UNSUPPORTED'):
        parts.insert(0, snapshot.status if snapshot.status != 'OBSERVED' else 'SOURCE_CONFLICT')
    if snapshot.read_health not in ('OK', 'PENDING'):
        parts.insert(0, snapshot.read_health)
    if any(source.has_more for source in snapshot.sources):
        parts.insert(0, 'CATCHING UP')
    age = 'data ' + age_text(snapshot.last_new_evidence_at, now)
    text = ' · '.join(dict.fromkeys(parts))
    return fit_text(text, max(0, width - cell_width(age) - 3)) + ' · ' + age


def card_text(snapshot, *, width, now, max_lines=None):
    """Render fixed logical groups; engine-specific meaning lives in their cells."""
    label_width = max((cell_width(group.label) for group in snapshot.card_groups), default=0)
    label_width = min(label_width, max(0, width // 4))
    body_width = max(1, width - label_width - 2)
    warnings = {event.event_id for event in snapshot.events
                if event.kind in ('native-fatal', 'native-failure', 'native-stop-request')}
    notices = []
    if warnings:
        notices.append(f'native warnings {len(warnings)}; Details')
    discrepancies = [setting.name for setting in snapshot.settings if setting.discrepancy]
    if discrepancies:
        notices.append('settings differ: ' + '/'.join(discrepancies))
    if any(item.latest.qualification == 'current-input-derived' for item in snapshot.metrics):
        notices.append('* current-input scope')
    if any(_provisional_target(item.latest, snapshot) for item in snapshot.metrics):
        notices.append('* provisional target' if any(item.latest.criterion for item in snapshot.metrics
                                                    if _provisional_target(item.latest, snapshot)) else '* provisional')
    if any(setting.qualification == 'provisional' for setting in snapshot.settings):
        notices.append('* provisional settings')
    if any('UNCERTAIN' in (item.latest.relation or '') for item in snapshot.metrics):
        notices.append('? uncertain')
    heading = ' · '.join(('SCIENTIFIC', *notices))
    lines = [heading]
    for group in snapshot.card_groups:
        for index, row in enumerate(group.rows):
            label = fit_text(group.label if index == 0 else '', label_width)
            widths = [max(1, (body_width - 2 * (len(row) - 1)) // max(1, len(row)))] * len(row)
            if widths:
                widths[-1] += max(0, body_width - sum(widths) - 2 * (len(row) - 1))
            values = [_cell(cell, snapshot, size) for cell, size in zip(row, widths)]
            body = '  '.join(value + ' ' * max(0, size - cell_width(value))
                             for value, size in zip(values, widths)).rstrip()
            lines.append(label + ' ' * (label_width - cell_width(label) + 2) + body)
    lines.append(verification_summary(snapshot, width=width, now=now))
    # Removing optional decoration may satisfy a short budget; never drop a metric
    # group or a safety qualification to create room for a heading.
    if max_lines is not None and len(lines) > max_lines and not notices:
        lines.pop(0)
    return '\n'.join(fit_text(line, width) for line in lines)
